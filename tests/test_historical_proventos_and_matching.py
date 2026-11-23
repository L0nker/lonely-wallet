"""Regression tests for lifetime dividend history and strict Pluggy matching."""
from __future__ import annotations

import ast
import json
import math
import re
import sqlite3
import unicodedata
import unittest
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / 'app' / 'main.py').read_text(encoding='utf-8')


def _api_date(value):
    if value in (None, ''):
        return ''
    raw = str(value).strip()
    try:
        return date.fromisoformat(raw[:10]).isoformat()
    except ValueError:
        try:
            return datetime.fromisoformat(raw.replace('Z', '+00:00')).date().isoformat()
        except ValueError:
            return ''


class HistoricalProventosSourceTests(unittest.TestCase):
    def test_dashboard_uses_lifetime_receipts_for_historical_summary(self):
        dashboard = SOURCE[SOURCE.index("@app.route('/api/dashboard')"):SOURCE.index("@app.route('/api/ativos/busca')")]
        self.assertIn('_received_proventos_records()', dashboard)
        self.assertIn("'escopo_recebidos': 'histórico integral, incluindo ativos vendidos'", dashboard)
        self.assertIn("'maiores_pagadores':top_paid_payers", dashboard)
        # Current-asset ranking remains intentionally scoped to active positions.
        asset_summary = SOURCE[SOURCE.index('def received_proventos_by_ticker('):SOURCE.index('def _historical_points(')]
        self.assertIn('_received_proventos_records(active)', asset_summary)

    def test_dividend_sync_includes_assets_from_historical_operations(self):
        sync = SOURCE[SOURCE.index('def sync_brapi_proventos('):SOURCE.index('def _is_income_event(')]
        self.assertIn('types = _asset_type_map()', sync)
        self.assertIn('quote = cotacao(ticker) if ticker in active_quote_symbols else cotacao_cacheada(ticker)', sync)
        self.assertIn('ticker not in types', sync)

    def test_private_financial_snapshot_rows_are_not_embedded_in_source(self):
        self.assertNotIn('snapshot_rows = [', SOURCE)
        self.assertNotIn("('PETR4','acao',43,40.22,52.66", SOURCE)
        self.assertIn('Preserve lifecycle handling for legacy user-imported snapshots', SOURCE)


def _load_matching_helpers(connection):
    tree = ast.parse(SOURCE)
    names = {
        '_normalize_provento_category', '_pluggy_record_date',
        '_pluggy_match_tolerance', '_pluggy_confirmed_provento_exists',
        '_mark_matching_api_forecast_paid',
    }
    nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    namespace = {
        'unicodedata': unicodedata, 're': re, 'json': json, 'math': math,
        'date': date, '_api_date': _api_date,
        'db': lambda: connection,
        'typesafe_asset_type': lambda ticker: 'fii' if str(ticker).upper().endswith('11') else 'acao',
        'abs': abs,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<extracted-pluggy-match-helpers>', 'exec'), namespace)
    return namespace


class StrictPluggyMatchTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(':memory:')
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript('''
            CREATE TABLE pluggy_imported_events(
                external_id TEXT, event_date TEXT, amount REAL, ticker TEXT,
                event_type TEXT, target_id INTEGER, import_state TEXT
            );
            CREATE TABLE pluggy_investment_transactions(
                transaction_id TEXT, movement_type TEXT, description TEXT, raw_json TEXT
            );
            CREATE TABLE proventos_api(
                event_key TEXT PRIMARY KEY, ticker TEXT, tipo_evento TEXT,
                data_com TEXT, data_pagamento TEXT, valor_total REAL, status TEXT
            );
        ''')
        self.helpers = _load_matching_helpers(self.connection)

    def tearDown(self):
        self.connection.close()

    def add_pluggy(self, event_id='pluggy-1', event_date='2026-10-15', amount=20.00,
                   ticker='PETR4', movement_type='DIVIDEND', description='Dividend payment',
                   raw='{}'):
        self.connection.execute(
            'INSERT INTO pluggy_imported_events VALUES(?,?,?,?,?,?,?)',
            (event_id, event_date, amount, ticker, 'provento', 1, 'imported')
        )
        self.connection.execute(
            'INSERT INTO pluggy_investment_transactions VALUES(?,?,?,?)',
            (event_id, movement_type, description, raw)
        )
        self.connection.commit()

    def test_matching_accepts_only_cent_precision_and_one_day_drift(self):
        exists = self.helpers['_pluggy_confirmed_provento_exists']
        self.add_pluggy()
        self.assertTrue(exists('PETR4', '2026-10-16', 20.02, event_type='DIVIDENDO'))
        self.assertFalse(exists('PETR4', '2026-10-18', 20.00, event_type='DIVIDENDO'))
        self.assertFalse(exists('PETR4', '2026-10-15', 20.03, event_type='DIVIDENDO'))

    def test_known_income_category_conflict_is_rejected(self):
        exists = self.helpers['_pluggy_confirmed_provento_exists']
        self.add_pluggy(movement_type='DIVIDEND', description='Dividend payment')
        self.assertFalse(exists('PETR4', '2026-10-15', 20.00, event_type='JCP'))
        self.assertTrue(exists('PETR4', '2026-10-15', 20.00, event_type='DIVIDENDO'))

    def test_record_date_conflict_is_rejected_when_both_sources_have_it(self):
        exists = self.helpers['_pluggy_confirmed_provento_exists']
        self.add_pluggy(raw=json.dumps({'recordDate': '2026-10-01'}))
        self.assertFalse(exists('PETR4', '2026-10-15', 20.00,
                                event_type='DIVIDENDO', record_date='2026-10-02'))
        self.assertTrue(exists('PETR4', '2026-10-15', 20.00,
                               event_type='DIVIDENDO', record_date='2026-10-01'))

    def test_only_best_matching_forecast_is_marked_paid(self):
        mark = self.helpers['_mark_matching_api_forecast_paid']
        self.connection.executemany(
            'INSERT INTO proventos_api VALUES(?,?,?,?,?,?,?)',
            [
                ('forecast-div', 'PETR4', 'DIVIDENDO', '2026-10-01', '2026-10-15', 20.00, 'A receber'),
                ('forecast-jcp', 'PETR4', 'JCP', '2026-10-01', '2026-10-15', 20.00, 'A receber'),
            ],
        )
        self.connection.commit()
        changed = mark('PETR4', '2026-10-15', 20.00,
                       movement_type='DIVIDEND', description='Dividend payment')
        self.assertEqual(changed, 1)
        states = dict(self.connection.execute('SELECT event_key,status FROM proventos_api').fetchall())
        self.assertEqual(states['forecast-div'], 'Pago')
        self.assertEqual(states['forecast-jcp'], 'A receber')


class LegacyForecastMigrationTests(unittest.TestCase):
    def test_migration_keeps_only_narrow_category_compatible_confirmations(self):
        tree = ast.parse(SOURCE)
        node = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == '_migrate_unconfirmed_proventos_api_status')
        namespace = {}
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<forecast-migration>', 'exec'), namespace)
        connection = sqlite3.connect(':memory:')
        try:
            connection.executescript('''
                CREATE TABLE proventos_api(
                    event_key TEXT PRIMARY KEY, ticker TEXT, tipo_evento TEXT,
                    data_pagamento TEXT, valor_total REAL, status TEXT
                );
                CREATE TABLE pluggy_imported_events(
                    external_id TEXT, event_type TEXT, ticker TEXT, event_date TEXT,
                    amount REAL, target_id INTEGER, import_state TEXT
                );
                CREATE TABLE pluggy_investment_transactions(
                    transaction_id TEXT, movement_type TEXT, description TEXT
                );
            ''')
            forecasts = [
                ('jcp-good', 'PETR4', 'JCP', '2026-10-15', 20.00, 'Pago'),
                ('jcp-wrong-type', 'PETR4', 'JCP', '2026-10-15', 30.00, 'Pago'),
                ('div-good', 'PETR4', 'DIVIDENDO', '2026-10-15', 30.00, 'Pago'),
                ('far-date', 'PETR4', 'DIVIDENDO', '2026-10-15', 40.00, 'Pago'),
            ]
            connection.executemany('INSERT INTO proventos_api VALUES(?,?,?,?,?,?)', forecasts)
            events = [
                ('tx-jcp', 'provento', 'PETR4', '2026-10-15', 20.00, 1, 'imported'),
                ('tx-div', 'provento', 'PETR4', '2026-10-15', 30.00, 2, 'imported'),
                ('tx-far', 'provento', 'PETR4', '2026-10-18', 40.00, 3, 'imported'),
            ]
            connection.executemany('INSERT INTO pluggy_imported_events VALUES(?,?,?,?,?,?,?)', events)
            connection.executemany('INSERT INTO pluggy_investment_transactions VALUES(?,?,?)', [
                ('tx-jcp', 'INTEREST', 'Investment income'),
                ('tx-div', 'DIVIDEND', 'Dividend payment'),
                ('tx-far', 'DIVIDEND', 'Dividend payment'),
            ])
            changed = namespace['_migrate_unconfirmed_proventos_api_status'](connection)
            connection.commit()
            self.assertEqual(changed, 2)
            statuses = dict(connection.execute('SELECT event_key,status FROM proventos_api').fetchall())
            self.assertEqual(statuses['jcp-good'], 'Pago')
            self.assertEqual(statuses['div-good'], 'Pago')
            self.assertEqual(statuses['jcp-wrong-type'], 'A receber')
            self.assertEqual(statuses['far-date'], 'A receber')
        finally:
            connection.close()


if __name__ == '__main__':
    unittest.main()
