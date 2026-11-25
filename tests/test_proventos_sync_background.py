"""Runtime tests for durable background proventos synchronization."""
from __future__ import annotations

import ast
import contextvars
import hashlib
import json
import sqlite3
import threading
import time
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / 'app' / 'main.py').read_text(encoding='utf-8')
TREE = ast.parse(SOURCE)
NAMES = {
    '_proventos_sync_signature', '_proventos_sync_persist_state',
    '_start_proventos_sync_worker', '_proventos_sync_worker',
    '_queue_proventos_sync', '_restore_proventos_sync_queue',
    '_proventos_sync_status',
}
FUNCTIONS = [node for node in TREE.body if isinstance(node, ast.FunctionDef) and node.name in NAMES]


class DummyLogger:
    def exception(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass


class DummyApp:
    logger = DummyLogger()

    @contextmanager
    def app_context(self):
        yield self


def build_namespace(sync_function, connection=None):
    connection = connection or sqlite3.connect(':memory:', check_same_thread=False)
    connection.row_factory = sqlite3.Row
    tracked_threads = []

    class TrackingThread(threading.Thread):
        def start(self):
            tracked_threads.append(self)
            return super().start()

    thread_api = SimpleNamespace(Thread=TrackingThread, RLock=threading.RLock)
    connection.execute('''CREATE TABLE IF NOT EXISTS proventos_sync_queue_state (
      id INTEGER PRIMARY KEY CHECK(id=1), status TEXT NOT NULL DEFAULT 'idle',
      signature TEXT NOT NULL DEFAULT '', inputs_json TEXT NOT NULL DEFAULT '{}',
      pending_signature TEXT NOT NULL DEFAULT '', pending_inputs_json TEXT NOT NULL DEFAULT '{}',
      result_json TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT ''
    )''')
    connection.commit()
    namespace = {
        'app': DummyApp(),
        'threading': thread_api,
        'contextvars': contextvars,
        'time': time,
        'datetime': datetime,
        'TZ': timezone.utc,
        'hashlib': hashlib,
        'json': json,
        'db': lambda: connection,
        'PROVENTOS_SYNC_CACHE': {'time': 0, 'result': None, 'signature': None},
        'PROVENTOS_SYNC_TTL': 3600,
        'PROVENTOS_SYNC_LOCK': threading.RLock(),
        'PROVENTOS_SYNC_STATE': {
            'status': 'idle', 'signature': '', 'pending_signature': '',
            'result': None, 'error': '', 'updated_at': '', 'inputs': None
        },
        'PROVENTOS_SYNC_PENDING_INPUTS': None,
        '_asset_type_map': lambda: {},
        'sync_brapi_proventos': sync_function,
        'safe_error_text': lambda exc, limit=300: str(exc)[:limit],
    }
    # Multi-wallet routing uses small accessors around the legacy globals. Keep
    # this isolated worker test focused on one unscoped wallet, as before.
    namespace['_proventos_sync_state'] = lambda: namespace['PROVENTOS_SYNC_STATE']
    namespace['_proventos_sync_pending'] = lambda: namespace.get('PROVENTOS_SYNC_PENDING_INPUTS')
    namespace['_set_proventos_sync_pending'] = lambda value: namespace.__setitem__('PROVENTOS_SYNC_PENDING_INPUTS', value)
    namespace['_proventos_sync_cache'] = lambda: namespace['PROVENTOS_SYNC_CACHE']
    exec(compile(ast.Module(body=FUNCTIONS, type_ignores=[]), '<proventos-sync-worker>', 'exec'), namespace)
    namespace['_test_connection'] = connection
    namespace['_test_threads'] = tracked_threads
    return namespace


def close_test_namespace(namespace):
    # The worker commits its final state before returning. Join it before
    # closing the shared in-memory/file-backed test connection.
    for worker in namespace.get('_test_threads', []):
        worker.join(timeout=5)
        if worker.is_alive():
            raise AssertionError('A thread de sincronização não terminou antes da limpeza do teste.')
    connection = namespace.get('_test_connection')
    if connection is not None:
        connection.close()


class ProventosBackgroundQueueTests(unittest.TestCase):
    def test_queue_returns_immediately_persists_current_and_pending(self):
        first_started = threading.Event()
        release_first = threading.Event()
        second_finished = threading.Event()
        calls = []

        def sync(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                first_started.set()
                release_first.wait(5)
                return {'ok': True, 'importados': 1, 'mensagem': 'primeira'}
            second_finished.set()
            return {'ok': True, 'importados': 2, 'mensagem': 'segunda'}

        ns = build_namespace(sync)
        self.addCleanup(close_test_namespace, ns)
        started = time.monotonic()
        first = ns['_queue_proventos_sync'](
            force=False,
            quote_items=[{'ticker': 'AAA3', 'tipo': 'acao'}],
            active_items=[{'ticker': 'AAA3', 'tipo': 'acao'}],
        )
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(first['status'], 'running')
        self.assertTrue(first_started.wait(2))

        row = ns['_test_connection'].execute('SELECT * FROM proventos_sync_queue_state WHERE id=1').fetchone()
        self.assertEqual(row['status'], 'running')
        self.assertEqual(json.loads(row['inputs_json'])['active_items'][0]['ticker'], 'AAA3')

        same = ns['_queue_proventos_sync'](
            force=True,
            quote_items=[{'ticker': 'AAA3', 'tipo': 'acao'}],
            active_items=[{'ticker': 'AAA3', 'tipo': 'acao'}],
        )
        self.assertEqual(same['status'], 'running')

        queued = ns['_queue_proventos_sync'](
            force=False,
            quote_items=[{'ticker': 'BBB4', 'tipo': 'acao'}],
            active_items=[{'ticker': 'BBB4', 'tipo': 'acao'}],
        )
        self.assertEqual(queued['status'], 'queued')
        row = ns['_test_connection'].execute('SELECT * FROM proventos_sync_queue_state WHERE id=1').fetchone()
        self.assertEqual(row['pending_signature'], queued['signature'])
        self.assertEqual(json.loads(row['pending_inputs_json'])['active_items'][0]['ticker'], 'BBB4')

        release_first.set()
        self.assertTrue(second_finished.wait(3))
        deadline = time.monotonic() + 3
        while not (ns['PROVENTOS_SYNC_STATE']['status'] == 'completed'
                   and ns['PROVENTOS_SYNC_STATE']['signature'] == queued['signature']) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(calls), 2)
        self.assertEqual(ns['PROVENTOS_SYNC_STATE']['status'], 'completed')
        self.assertEqual(ns['PROVENTOS_SYNC_STATE']['result']['mensagem'], 'segunda')
        # The worker publishes its in-memory state while holding the queue lock;
        # wait for the durable SQLite write as well, rather than racing the final commit.
        persist_deadline = time.monotonic() + 2
        row = ns['_test_connection'].execute('SELECT * FROM proventos_sync_queue_state WHERE id=1').fetchone()
        while row['status'] != 'completed' and time.monotonic() < persist_deadline:
            time.sleep(0.01)
            row = ns['_test_connection'].execute('SELECT * FROM proventos_sync_queue_state WHERE id=1').fetchone()
        self.assertEqual(row['status'], 'completed')
        self.assertEqual(row['pending_signature'], '')
        self.assertEqual(json.loads(row['inputs_json'])['active_items'][0]['ticker'], 'BBB4')

    def test_interrupted_running_job_is_restored_after_restart(self):
        finished = threading.Event()
        calls = []

        def sync(**kwargs):
            calls.append(kwargs)
            finished.set()
            return {'ok': True, 'importados': 3, 'mensagem': 'recuperada'}

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        database_path = str(Path(temporary.name) / 'assets.db')
        connection = sqlite3.connect(database_path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        ns = build_namespace(sync, connection)
        inputs = {'force': True, 'quote_items': [{'ticker': 'ITSA4', 'tipo': 'acao'}],
                  'active_items': [{'ticker': 'ITSA4', 'tipo': 'acao'}]}
        signature = 'persisted-signature'
        connection.execute(
            '''INSERT INTO proventos_sync_queue_state
               (id,status,signature,inputs_json,pending_signature,pending_inputs_json,result_json,error,updated_at)
               VALUES(1,'running',?,?, '', '{}', '{}', '', ?)''',
            (signature, json.dumps(inputs), datetime.now(timezone.utc).isoformat())
        )
        connection.commit()
        connection.close()
        # A new connection simulates a new app process opening the same mounted DB.
        connection = sqlite3.connect(database_path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        ns = build_namespace(sync, connection)
        self.addCleanup(close_test_namespace, ns)

        self.assertTrue(ns['_restore_proventos_sync_queue']())
        self.assertTrue(finished.wait(2))
        deadline = time.monotonic() + 2
        while ns['PROVENTOS_SYNC_STATE']['status'] == 'running' and time.monotonic() < deadline:
            time.sleep(0.01)

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['active_items'][0]['ticker'], 'ITSA4')
        self.assertEqual(ns['PROVENTOS_SYNC_STATE']['status'], 'completed')
        row = connection.execute('SELECT status,result_json FROM proventos_sync_queue_state WHERE id=1').fetchone()
        self.assertEqual(row['status'], 'completed')
        self.assertEqual(json.loads(row['result_json'])['mensagem'], 'recuperada')

    def test_interrupted_current_and_pending_jobs_are_both_restored(self):
        calls = []
        finished = threading.Event()

        def sync(**kwargs):
            calls.append(kwargs)
            if len(calls) == 2:
                finished.set()
            return {'ok': True, 'mensagem': kwargs['active_items'][0]['ticker']}

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        database_path = str(Path(temporary.name) / 'assets.db')
        connection = sqlite3.connect(database_path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        ns = build_namespace(sync, connection)
        current = {'force': True, 'quote_items': [{'ticker': 'AAA3', 'tipo': 'acao'}],
                   'active_items': [{'ticker': 'AAA3', 'tipo': 'acao'}]}
        pending = {'force': False, 'quote_items': [{'ticker': 'BBB4', 'tipo': 'acao'}],
                   'active_items': [{'ticker': 'BBB4', 'tipo': 'acao'}]}
        connection.execute(
            "INSERT INTO proventos_sync_queue_state"
            "(id,status,signature,inputs_json,pending_signature,pending_inputs_json,result_json,error,updated_at) "
            "VALUES(1,'running',?,?,?,?,'{}','',?)",
            ('sig-current', json.dumps(current), 'sig-pending', json.dumps(pending), datetime.now(timezone.utc).isoformat())
        )
        connection.commit()
        connection.close()
        connection = sqlite3.connect(database_path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        ns = build_namespace(sync, connection)
        self.addCleanup(close_test_namespace, ns)

        self.assertTrue(ns['_restore_proventos_sync_queue']())
        self.assertTrue(finished.wait(2))
        deadline = time.monotonic() + 2
        while ns['PROVENTOS_SYNC_STATE']['status'] == 'running' and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual([call['active_items'][0]['ticker'] for call in calls], ['AAA3', 'BBB4'])
        deadline = time.monotonic() + 2
        row = None
        while time.monotonic() < deadline:
            row = connection.execute('SELECT status,signature,pending_signature FROM proventos_sync_queue_state WHERE id=1').fetchone()
            if row and row['status'] == 'completed':
                break
            time.sleep(0.01)
        self.assertIsNotNone(row)
        self.assertEqual(row['status'], 'completed')
        self.assertEqual(row['signature'], 'sig-pending')
        self.assertEqual(row['pending_signature'], '')

    def test_dashboard_and_manual_route_use_background_queue(self):
        dashboard = SOURCE[SOURCE.index("@app.route('/api/dashboard')"):SOURCE.index("@app.route('/api/ativos/busca')")]
        route = SOURCE[SOURCE.index("@app.route('/api/proventos/sincronizar', methods=['POST'])"):SOURCE.index('def _b3_normalize(')]
        self.assertIn('_queue_proventos_sync(', dashboard)
        self.assertNotIn('sync_brapi_proventos(', dashboard)
        self.assertIn('_queue_proventos_sync(', route)
        self.assertIn("@app.route('/api/proventos/sincronizar/status', methods=['GET'])", route)


if __name__ == '__main__':
    unittest.main()
