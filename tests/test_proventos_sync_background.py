"""Focused runtime tests for the background proventos synchronization queue."""
from __future__ import annotations

import ast
import threading
import time
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / 'app' / 'main.py').read_text(encoding='utf-8')
TREE = ast.parse(SOURCE)
NAMES = {'_proventos_sync_signature', '_proventos_sync_worker', '_queue_proventos_sync', '_proventos_sync_status'}
FUNCTIONS = [node for node in TREE.body if isinstance(node, ast.FunctionDef) and node.name in NAMES]


class DummyLogger:
    def exception(self, *args, **kwargs):
        pass


class DummyApp:
    logger = DummyLogger()

    @contextmanager
    def app_context(self):
        yield self


def build_namespace(sync_function):
    namespace = {
        'app': DummyApp(),
        'threading': threading,
        'time': time,
        'datetime': datetime,
        'TZ': timezone.utc,
        'hashlib': __import__('hashlib'),
        'json': __import__('json'),
        'PROVENTOS_SYNC_CACHE': {'time': 0, 'result': None, 'signature': None},
        'PROVENTOS_SYNC_TTL': 3600,
        'PROVENTOS_SYNC_LOCK': threading.RLock(),
        'PROVENTOS_SYNC_STATE': {
            'status': 'idle', 'signature': '', 'pending_signature': '',
            'result': None, 'error': '', 'updated_at': ''
        },
        'PROVENTOS_SYNC_PENDING_INPUTS': None,
        '_asset_type_map': lambda: {},
        'sync_brapi_proventos': sync_function,
        'safe_error_text': lambda exc, limit=300: str(exc)[:limit],
    }
    exec(compile(ast.Module(body=FUNCTIONS, type_ignores=[]), '<proventos-sync-worker>', 'exec'), namespace)
    return namespace


class ProventosBackgroundQueueTests(unittest.TestCase):
    def test_queue_returns_immediately_and_serializes_new_asset_signature(self):
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
        with patch.object(threading, 'Thread', wraps=threading.Thread):
            started = time.monotonic()
            first = ns['_queue_proventos_sync'](
                force=False,
                quote_items=[{'ticker': 'AAA3', 'tipo': 'acao'}],
                active_items=[{'ticker': 'AAA3', 'tipo': 'acao'}],
            )
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertEqual(first['status'], 'running')
            self.assertTrue(first_started.wait(2))

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
            release_first.set()
            self.assertTrue(second_finished.wait(3))

            deadline = time.monotonic() + 3
            while ns['PROVENTOS_SYNC_STATE']['status'] == 'running' and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(len(calls), 2)
            self.assertEqual(ns['PROVENTOS_SYNC_STATE']['status'], 'completed')
            self.assertEqual(ns['PROVENTOS_SYNC_STATE']['result']['mensagem'], 'segunda')

    def test_dashboard_and_manual_route_use_background_queue(self):
        dashboard = SOURCE[SOURCE.index("@app.route('/api/dashboard')"):SOURCE.index("@app.route('/api/ativos/busca')")]
        route = SOURCE[SOURCE.index("@app.route('/api/proventos/sincronizar', methods=['POST'])"):SOURCE.index('def _b3_normalize(')]
        self.assertIn('_queue_proventos_sync(', dashboard)
        self.assertNotIn('sync_brapi_proventos(', dashboard)
        self.assertIn('_queue_proventos_sync(', route)
        self.assertIn("@app.route('/api/proventos/sincronizar/status', methods=['GET'])", route)


if __name__ == '__main__':
    unittest.main()
