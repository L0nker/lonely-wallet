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

    def test_pluggy_importer_checks_raw_movement_type_before_creating_receipt(self):
        importer = SOURCE[SOURCE.index('def _pluggy_import_investment_transaction('):SOURCE.index('def _pluggy_sync_interval(')]
        self.assertIn('_is_pluggy_income_movement(', importer)
        self.assertIn('movement_type, description, movement', importer)
        self.assertIn('if (is_income_movement or is_income_description)', importer)


def _load_matching_helpers(connection):
    tree = ast.parse(SOURCE)
    names = {
        '_normalize_provento_category', '_pluggy_record_date',
        '_pluggy_match_tolerance', '_pluggy_confirmed_provento_exists',
        '_mark_matching_api_forecast_paid', '_pluggy_amount_candidates',
        '_pluggy_income_receipt_amount', '_is_pluggy_income_movement',
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
                transaction_id TEXT, ticker TEXT, transaction_date TEXT, amount REAL, movement_type TEXT, description TEXT, raw_json TEXT, import_state TEXT
            );
            CREATE TABLE proventos_api(
                event_key TEXT PRIMARY KEY, ticker TEXT, tipo_evento TEXT,
                data_com TEXT, data_pagamento TEXT, valor_total REAL, status TEXT
            );
            CREATE TABLE proventos(id INTEGER PRIMARY KEY, ticker TEXT);
            INSERT INTO proventos(id,ticker) VALUES(1,'PETR4');
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
            'INSERT INTO pluggy_investment_transactions VALUES(?,?,?,?,?,?,?,?)',
            (event_id, ticker, event_date, amount, movement_type, description, raw, 'imported_provento')
        )
        self.connection.commit()

    def test_matching_accepts_only_cent_precision_and_one_day_drift(self):
        exists = self.helpers['_pluggy_confirmed_provento_exists']
        self.add_pluggy()
        self.assertTrue(exists('PETR4', '2026-10-16', 20.02, event_type='DIVIDENDO'))
        self.assertFalse(exists('PETR4', '2026-10-18', 20.00, event_type='DIVIDENDO'))
        self.assertFalse(exists('PETR4', '2026-10-15', 20.03, event_type='DIVIDENDO'))

    def test_imported_event_must_reference_an_existing_matching_provento(self):
        exists = self.helpers['_pluggy_confirmed_provento_exists']
        self.add_pluggy()
        self.connection.execute(
            'UPDATE pluggy_imported_events SET target_id=? WHERE external_id=?',
            (999, 'pluggy-1')
        )
        self.connection.commit()
        self.assertFalse(exists('PETR4', '2026-10-15', 20.00, event_type='DIVIDENDO'))
        self.connection.execute(
            'UPDATE pluggy_imported_events SET target_id=? WHERE external_id=?',
            (1, 'pluggy-1')
        )
        self.connection.execute('UPDATE proventos SET ticker=? WHERE id=1', ('VALE3',))
        self.connection.commit()
        self.assertFalse(exists('PETR4', '2026-10-15', 20.00, event_type='DIVIDENDO'))

    def test_transaction_ticker_and_date_must_match_imported_event(self):
        exists = self.helpers['_pluggy_confirmed_provento_exists']
        self.add_pluggy(ticker='PETR4', event_date='2026-10-15')
        self.connection.execute(
            'UPDATE pluggy_investment_transactions SET ticker=?,transaction_date=? WHERE transaction_id=?',
            ('VALE3', '2026-10-15', 'pluggy-1')
        )
        self.connection.commit()
        self.assertFalse(exists('PETR4', '2026-10-15', 20.00, event_type='DIVIDENDO'))
        self.connection.execute(
            'UPDATE pluggy_investment_transactions SET ticker=?,transaction_date=? WHERE transaction_id=?',
            ('PETR4', '2026-10-20', 'pluggy-1')
        )
        self.connection.commit()
        self.assertFalse(exists('PETR4', '2026-10-15', 20.00, event_type='DIVIDENDO'))
        self.connection.execute(
            'UPDATE pluggy_investment_transactions SET transaction_date=? WHERE transaction_id=?',
            ('2026-10-16', 'pluggy-1')
        )
        self.connection.execute(
            'UPDATE pluggy_imported_events SET event_date=? WHERE external_id=?',
            ('2026-10-15', 'pluggy-1')
        )
        self.connection.commit()
        self.assertFalse(exists('PETR4', '2026-10-16', 20.00, event_type='DIVIDENDO'))

    def test_transaction_must_be_imported_as_a_provento(self):
        exists = self.helpers['_pluggy_confirmed_provento_exists']
        self.add_pluggy()
        self.connection.execute(
            'UPDATE pluggy_investment_transactions SET import_state=? WHERE transaction_id=?',
            ('imported_only', 'pluggy-1')
        )
        self.connection.commit()
        self.assertFalse(exists('PETR4', '2026-10-15', 20.00, event_type='DIVIDENDO'))


    def test_generic_interest_does_not_confirm_competing_jcp_and_dividend_forecasts(self):
        exists = self.helpers['_pluggy_confirmed_provento_exists']
        self.connection.executemany(
            'INSERT INTO proventos_api VALUES(?,?,?,?,?,?,?)',
            [
                ('ambiguous-jcp', 'PETR4', 'JCP', '2026-10-01', '2026-10-15', 20.00, 'A receber'),
                ('ambiguous-div', 'PETR4', 'DIVIDENDO', '2026-10-01', '2026-10-15', 20.00, 'A receber'),
            ],
        )
        self.connection.commit()
        self.add_pluggy(movement_type='INTEREST', description='Investment income')
        self.assertFalse(exists('PETR4', '2026-10-15', 20.00, event_type='JCP'))
        self.assertFalse(exists('PETR4', '2026-10-15', 20.00, event_type='DIVIDENDO'))

    def test_known_income_category_conflict_is_rejected(self):
        exists = self.helpers['_pluggy_confirmed_provento_exists']
        self.add_pluggy(movement_type='DIVIDEND', description='Dividend payment')
        self.assertFalse(exists('PETR4', '2026-10-15', 20.00, event_type='JCP'))
        self.assertTrue(exists('PETR4', '2026-10-15', 20.00, event_type='DIVIDENDO'))


    def test_interest_description_that_identifies_amortization_is_not_a_receipt(self):
        exists = self.helpers['_pluggy_confirmed_provento_exists']
        self.add_pluggy(movement_type='INTEREST', description='Amortization of principal')
        self.assertFalse(exists('PETR4', '2026-10-15', 20.00, event_type='JCP'))

    def test_raw_movement_type_amortization_overrides_generic_interest(self):
        exists = self.helpers['_pluggy_confirmed_provento_exists']
        self.add_pluggy(movement_type='INTEREST', description='Investment income',
                        raw=json.dumps({'movementType': 'AMORTIZATION'}))
        self.assertFalse(exists('PETR4', '2026-10-15', 20.00, event_type='JCP'))

    def test_raw_movement_type_classifies_generic_interest_as_jcp(self):
        exists = self.helpers['_pluggy_confirmed_provento_exists']
        self.add_pluggy(movement_type='INTEREST', description='Investment income',
                        raw=json.dumps({'movementType': 'JCP'}))
        self.assertTrue(exists('PETR4', '2026-10-15', 20.00, event_type='JCP'))
        self.assertFalse(exists('PETR4', '2026-10-15', 20.00, event_type='DIVIDENDO'))

    def test_record_date_conflict_is_rejected_when_both_sources_have_it(self):
        exists = self.helpers['_pluggy_confirmed_provento_exists']
        self.add_pluggy(raw=json.dumps({'recordDate': '2026-10-01'}))
        self.assertFalse(exists('PETR4', '2026-10-15', 20.00,
                                event_type='DIVIDENDO', record_date='2026-10-02'))
        self.assertTrue(exists('PETR4', '2026-10-15', 20.00,
                               event_type='DIVIDENDO', record_date='2026-10-01'))

    def test_jcp_forecast_matches_gross_while_receipt_can_be_net(self):
        mark = self.helpers['_mark_matching_api_forecast_paid']
        self.connection.execute(
            'INSERT INTO proventos_api VALUES(?,?,?,?,?,?,?)',
            ('jcp-gross', 'PETR4', 'JCP', '2026-10-01', '2026-10-15', 100.00, 'A receber'),
        )
        self.connection.commit()
        movement = {'type': 'INTEREST', 'description': 'JCP', 'amount': 100.00, 'netAmount': 85.00}
        self.add_pluggy(event_id='jcp-gross-tx', amount=85.00, movement_type='INTEREST',
                        description='JCP', raw=json.dumps(movement))
        changed = mark('jcp-gross-tx', 'PETR4', '2026-10-15', 100.00, movement_type='INTEREST',
                       description='JCP', raw_movement=movement,
                       alternate_amounts=[100.00, 85.00])
        self.assertEqual(changed, 1)
        status = self.connection.execute(
            'SELECT status FROM proventos_api WHERE event_key=?', ('jcp-gross',)
        ).fetchone()[0]
        self.assertEqual(status, 'Pago')

    def test_income_receipt_prefers_pluggy_net_amount_when_available(self):
        receipt_amount = self.helpers['_pluggy_income_receipt_amount']
        self.assertEqual(receipt_amount({'amount': 100.00, 'netAmount': 85.00}), 85.00)
        self.assertEqual(receipt_amount({'amount': 100.00}), 100.00)

    def test_pluggy_confirmation_checks_gross_and_net_values_from_raw_transaction(self):
        exists = self.helpers['_pluggy_confirmed_provento_exists']
        self.add_pluggy(amount=85.00, movement_type='INTEREST', description='JCP',
                        raw=json.dumps({'type':'INTEREST','description':'JCP',
                                        'amount':100.00,'netAmount':85.00}))
        self.assertTrue(exists('PETR4', '2026-10-15', 100.00, event_type='JCP'))
        self.assertTrue(exists('PETR4', '2026-10-15', 85.00, event_type='JCP'))

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
        self.add_pluggy(event_id='div-forecast-tx', amount=20.00,
                        movement_type='DIVIDEND', description='Dividend payment')
        changed = mark('div-forecast-tx', 'PETR4', '2026-10-15', 20.00,
                       movement_type='DIVIDEND', description='Dividend payment')
        self.assertEqual(changed, 1)
        states = dict(self.connection.execute('SELECT event_key,status FROM proventos_api').fetchall())
        self.assertEqual(states['forecast-div'], 'Pago')
        self.assertEqual(states['forecast-jcp'], 'A receber')


    def test_generic_interest_does_not_choose_arbitrarily_between_forecast_categories(self):
        mark = self.helpers['_mark_matching_api_forecast_paid']
        self.connection.executemany(
            'INSERT INTO proventos_api VALUES(?,?,?,?,?,?,?)',
            [
                ('mark-ambiguous-jcp', 'PETR4', 'JCP', '2026-10-01', '2026-10-15', 20.00, 'A receber'),
                ('mark-ambiguous-div', 'PETR4', 'DIVIDENDO', '2026-10-01', '2026-10-15', 20.00, 'A receber'),
            ],
        )
        self.connection.commit()
        self.add_pluggy(event_id='generic-interest', movement_type='INTEREST',
                        description='Investment income')
        changed = mark('generic-interest', 'PETR4', '2026-10-15', 20.00)
        self.assertEqual(changed, 0)
        statuses = dict(self.connection.execute('SELECT event_key,status FROM proventos_api').fetchall())
        self.assertEqual(statuses['mark-ambiguous-jcp'], 'A receber')
        self.assertEqual(statuses['mark-ambiguous-div'], 'A receber')

    def test_forecast_cannot_be_marked_paid_without_a_persisted_linked_transaction(self):
        mark = self.helpers['_mark_matching_api_forecast_paid']
        self.connection.execute(
            'INSERT INTO proventos_api VALUES(?,?,?,?,?,?,?)',
            ('forecast-orphan', 'PETR4', 'DIVIDENDO', '2026-10-01', '2026-10-15', 20.00, 'A receber'),
        )
        self.connection.commit()
        self.assertEqual(mark('missing-external-id', 'PETR4', '2026-10-15', 20.00,
                              movement_type='DIVIDEND', description='Dividend payment'), 0)
        status = self.connection.execute(
            'SELECT status FROM proventos_api WHERE event_key=?', ('forecast-orphan',)
        ).fetchone()[0]
        self.assertEqual(status, 'A receber')

class LegacyForecastMigrationTests(unittest.TestCase):
    def test_migration_keeps_only_narrow_category_compatible_confirmations(self):
        tree = ast.parse(SOURCE)
        node = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == '_migrate_unconfirmed_proventos_api_status')
        namespace = {'json': json, 'math': math, 'unicodedata': unicodedata}
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<forecast-migration>', 'exec'), namespace)
        connection = sqlite3.connect(':memory:')
        try:
            connection.executescript('''
                CREATE TABLE proventos_api(
                    event_key TEXT PRIMARY KEY, ticker TEXT, tipo_evento TEXT,
                    data_pagamento TEXT, valor_total REAL, status TEXT, data_com TEXT DEFAULT ''
                );
                CREATE TABLE pluggy_imported_events(
                    external_id TEXT, event_type TEXT, ticker TEXT, event_date TEXT,
                    amount REAL, target_id INTEGER, import_state TEXT
                );
                CREATE TABLE proventos(id INTEGER PRIMARY KEY, ticker TEXT);
                INSERT INTO proventos(id,ticker) VALUES(1,'PETR4'),(2,'PETR4'),(3,'PETR4'),(4,'PETR4'),(5,'PETR4'),(6,'PETR4'),(7,'PETR4'),(8,'PETR4'),(9,'PETR4'),(10,'PETR4'),(11,'PETR4'),(12,'PETR4'),(13,'PETR4'),(14,'PETR4'),(15,'PETR4'),(16,'PETR4');
                CREATE TABLE pluggy_investment_transactions(
                    transaction_id TEXT, ticker TEXT, transaction_date TEXT, amount REAL,
                    movement_type TEXT, description TEXT, raw_json TEXT, import_state TEXT
                );
            ''')
            forecasts = [
                ('jcp-good', 'PETR4', 'JCP', '2026-10-15', 20.00, 'Pago'),
                ('jcp-wrong-type', 'PETR4', 'JCP', '2026-10-15', 30.00, 'Pago'),
                ('div-good', 'PETR4', 'DIVIDENDO', '2026-10-15', 30.00, 'Pago'),
                ('far-date', 'PETR4', 'DIVIDENDO', '2026-10-15', 40.00, 'Pago'),
                ('jcp-orphan', 'PETR4', 'JCP', '2026-10-15', 50.00, 'Pago'),
                ('div-orphan', 'PETR4', 'DIVIDENDO', '2026-10-15', 60.00, 'Pago'),
                ('rend-orphan', 'PETR4', 'RENDIMENTO', '2026-10-15', 70.00, 'Pago'),
                ('missing-target', 'PETR4', 'DIVIDENDO', '2026-10-15', 80.00, 'Pago'),
                ('jcp-gross-net', 'PETR4', 'JCP', '2026-10-15', 100.00, 'Pago'),
                ('wrong-ticker-tx', 'PETR4', 'DIVIDENDO', '2026-10-15', 90.00, 'Pago'),
                ('wrong-date-tx', 'PETR4', 'DIVIDENDO', '2026-10-15', 91.00, 'Pago'),
                ('jcp-category-conflict', 'PETR4', 'JCP', '2026-10-15', 92.00, 'Pago'),
                ('div-category-conflict', 'PETR4', 'DIVIDENDO', '2026-10-15', 93.00, 'Pago'),
                ('split-dates', 'PETR4', 'DIVIDENDO', '2026-10-15', 94.00, 'Pago'),
                ('ambig-jcp', 'PETR4', 'JCP', '2026-10-15', 95.00, 'Pago'),
                ('ambig-div', 'PETR4', 'DIVIDENDO', '2026-10-15', 95.00, 'Pago'),
                ('record-date-mismatch', 'PETR4', 'DIVIDENDO', '2026-10-15', 96.00, 'Pago'),
                ('amortized-interest', 'PETR4', 'JCP', '2026-10-15', 97.00, 'Pago'),
            ]
            connection.executemany(
                'INSERT INTO proventos_api(event_key,ticker,tipo_evento,data_pagamento,valor_total,status) VALUES(?,?,?,?,?,?)',
                forecasts
            )
            events = [
                ('tx-jcp', 'provento', 'PETR4', '2026-10-15', 20.00, 1, 'imported'),
                ('tx-div', 'provento', 'PETR4', '2026-10-15', 30.00, 2, 'imported'),
                ('tx-far', 'provento', 'PETR4', '2026-10-18', 40.00, 3, 'imported'),
                ('orphan-jcp', 'provento', 'PETR4', '2026-10-15', 50.00, 4, 'imported'),
                ('orphan-div', 'provento', 'PETR4', '2026-10-15', 60.00, 5, 'imported'),
                ('orphan-rend', 'provento', 'PETR4', '2026-10-15', 70.00, 6, 'imported'),
                ('missing-target-tx', 'provento', 'PETR4', '2026-10-15', 80.00, 999, 'imported'),
                ('tx-jcp-net', 'provento', 'PETR4', '2026-10-15', 85.00, 8, 'imported'),
                ('tx-wrong-ticker', 'provento', 'PETR4', '2026-10-15', 90.00, 9, 'imported'),
                ('tx-wrong-date', 'provento', 'PETR4', '2026-10-15', 91.00, 10, 'imported'),
                ('tx-jcp-conflict', 'provento', 'PETR4', '2026-10-15', 92.00, 11, 'imported'),
                ('tx-div-conflict', 'provento', 'PETR4', '2026-10-15', 93.00, 12, 'imported'),
                ('tx-split-dates', 'provento', 'PETR4', '2026-10-14', 94.00, 13, 'imported'),
                ('tx-ambiguous', 'provento', 'PETR4', '2026-10-15', 95.00, 14, 'imported'),
                ('tx-record-date', 'provento', 'PETR4', '2026-10-15', 96.00, 15, 'imported'),
                ('tx-amortized-interest', 'provento', 'PETR4', '2026-10-15', 97.00, 16, 'imported'),
            ]
            connection.executemany('INSERT INTO pluggy_imported_events VALUES(?,?,?,?,?,?,?)', events)
            connection.executemany('INSERT INTO pluggy_investment_transactions VALUES(?,?,?,?,?,?,?,?)', [
                ('tx-jcp', 'PETR4', '2026-10-15', 20.00, 'INTEREST', 'Investment income', '{}', 'imported_provento'),
                ('tx-div', 'PETR4', '2026-10-15', 30.00, 'DIVIDEND', 'Dividend payment', '{}', 'imported_provento'),
                ('tx-far', 'PETR4', '2026-10-18', 40.00, 'DIVIDEND', 'Dividend payment', '{}', 'imported_provento'),
                ('missing-target-tx', 'PETR4', '2026-10-15', 80.00, 'DIVIDEND', 'Dividend payment', '{}', 'imported_provento'),
                ('tx-jcp-net', 'PETR4', '2026-10-15', 85.00, 'INTEREST', 'JCP', '{"grossAmount":100.0,"netAmount":85.0}', 'imported_provento'),
                ('tx-wrong-ticker', 'VALE3', '2026-10-15', 90.00, 'DIVIDEND', 'Dividend payment', '{}', 'imported_provento'),
                ('tx-wrong-date', 'PETR4', '2026-10-30', 91.00, 'DIVIDEND', 'Dividend payment', '{}', 'imported_provento'),
                ('tx-jcp-conflict', 'PETR4', '2026-10-15', 92.00, 'INTEREST', 'Dividend payment', '{}', 'imported_provento'),
                ('tx-div-conflict', 'PETR4', '2026-10-15', 93.00, 'INTEREST', 'JCP', '{}', 'imported_provento'),
                ('tx-split-dates', 'PETR4', '2026-10-16', 94.00, 'DIVIDEND', 'Dividend payment', '{}', 'imported_provento'),
                ('tx-ambiguous', 'PETR4', '2026-10-15', 95.00, 'INTEREST', 'Investment income', '{}', 'imported_provento'),
                ('tx-record-date', 'PETR4', '2026-10-15', 96.00, 'DIVIDEND', 'Dividend payment', '{"recordDate":"2026-10-02"}', 'imported_provento'),
                ('tx-amortized-interest', 'PETR4', '2026-10-15', 97.00, 'INTEREST', 'Investment income', '{"movementType":"AMORTIZATION"}', 'imported_provento'),
            ])
            connection.execute(
                'UPDATE proventos_api SET data_com=? WHERE event_key=?',
                ('2026-10-01', 'record-date-mismatch')
            )
            changed = namespace['_migrate_unconfirmed_proventos_api_status'](connection)
            connection.commit()
            self.assertEqual(changed, 15)
            statuses = dict(connection.execute('SELECT event_key,status FROM proventos_api').fetchall())
            self.assertEqual(statuses['jcp-good'], 'Pago')
            self.assertEqual(statuses['div-good'], 'Pago')
            self.assertEqual(statuses['jcp-wrong-type'], 'A receber')
            self.assertEqual(statuses['far-date'], 'A receber')
            self.assertEqual(statuses['jcp-orphan'], 'A receber')
            self.assertEqual(statuses['div-orphan'], 'A receber')
            self.assertEqual(statuses['rend-orphan'], 'A receber')
            self.assertEqual(statuses['missing-target'], 'A receber')
            self.assertEqual(statuses['jcp-gross-net'], 'Pago')
            self.assertEqual(statuses['wrong-ticker-tx'], 'A receber')
            self.assertEqual(statuses['wrong-date-tx'], 'A receber')
            self.assertEqual(statuses['jcp-category-conflict'], 'A receber')
            self.assertEqual(statuses['div-category-conflict'], 'A receber')
            self.assertEqual(statuses['split-dates'], 'A receber')
            self.assertEqual(statuses['ambig-jcp'], 'A receber')
            self.assertEqual(statuses['ambig-div'], 'A receber')
            self.assertEqual(statuses['record-date-mismatch'], 'A receber')
            self.assertEqual(statuses['amortized-interest'], 'A receber')
            self.assertEqual(namespace['_migrate_unconfirmed_proventos_api_status'](connection), 0)
        finally:
            connection.close()


class ReceivedProventosLedgerTests(unittest.TestCase):
    def test_only_consistent_existing_pluggy_transaction_targets_become_receipts(self):
        tree = ast.parse(SOURCE)
        node = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == '_received_proventos_records')
        income_node = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                           and node.name == '_is_pluggy_income_movement')
        category_node = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                             and node.name == '_normalize_provento_category')
        amount_node = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                           and node.name == '_pluggy_amount_candidates')
        tolerance_node = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                              and node.name == '_pluggy_match_tolerance')
        connection = sqlite3.connect(':memory:')
        connection.row_factory = sqlite3.Row
        try:
            connection.executescript('''
                CREATE TABLE proventos(id INTEGER PRIMARY KEY, ticker TEXT, valor_total REAL, data TEXT);
                CREATE TABLE pluggy_imported_events(
                    external_id TEXT, event_type TEXT, event_date TEXT, amount REAL,
                    ticker TEXT, target_id INTEGER, import_state TEXT
                );
                CREATE TABLE pluggy_investment_transactions(
                    transaction_id TEXT, ticker TEXT, transaction_date TEXT, amount REAL,
                    description TEXT, movement_type TEXT, raw_json TEXT, import_state TEXT
                );
                INSERT INTO proventos(id,ticker,valor_total,data) VALUES
                    (1,'PETR4',10.00,'2026-01-15'),
                    (2,'VALE3',20.00,'2026-01-15'),
                    (3,'PETR4',30.00,'2026-01-15'),
                    (4,'PETR4',40.00,'2026-01-15'),
                    (5,'PETR4',50.00,'2026-12-15'),
                    (6,'PETR4',60.00,'2026-01-15'),
                    (7,'PETR4',70.00,'2026-01-15'),
                    (8,'PETR4',80.00,'2026-01-15'),
                    (9,'PETR4',90.00,'2026-01-15'),
                    (10,'PETR4',100.00,'2026-01-15'),
                    (11,'PETR4',66.00,'2026-01-15'),
                    (12,'PETR4',85.00,'2026-01-15');
                INSERT INTO pluggy_imported_events VALUES
                    ('good','provento','2026-01-15',10.00,'PETR4',1,'imported'),
                    ('wrong-target','provento','2026-01-15',20.00,'PETR4',2,'imported'),
                    ('wrong-date','provento','2026-01-15',30.00,'PETR4',3,'imported'),
                    ('orphan','provento','2026-01-15',40.00,'PETR4',4,'imported'),
                    ('future','provento','2026-12-15',50.00,'PETR4',5,'imported'),
                    ('amortization','provento','2026-01-15',60.00,'PETR4',6,'imported'),
                    ('other-income','provento','2026-01-15',70.00,'PETR4',7,'imported'),
                    ('buy-not-income','provento','2026-01-15',80.00,'PETR4',8,'imported'),
                    ('not-imported','provento','2026-01-15',90.00,'PETR4',9,'imported'),
                    ('amortization-interest','provento','2026-01-15',100.00,'PETR4',10,'imported'),
                    ('amount-mismatch','provento','2026-01-15',66.00,'PETR4',11,'imported'),
                    ('jcp-gross-net','provento','2026-01-15',85.00,'PETR4',12,'imported');
                INSERT INTO pluggy_investment_transactions VALUES
                    ('good','PETR4','2026-01-15',10.00,'Investment income','INTEREST','{"movementType":"JCP","amount":10.0}','imported_provento'),
                    ('wrong-target','PETR4','2026-01-15',20.00,'Dividend','DIVIDEND','{"amount":20.0}','imported_provento'),
                    ('wrong-date','PETR4','2026-02-15',30.00,'Dividend','DIVIDEND','{"amount":30.0}','imported_provento'),
                    ('future','PETR4','2026-12-15',50.00,'Dividend','DIVIDEND','{"amount":50.0}','imported_provento'),
                    ('amortization','PETR4','2026-01-15',60.00,'Amortization','AMORTIZATION','{"amount":60.0}','imported_provento'),
                    ('other-income','PETR4','2026-01-15',70.00,'Dividend payment','OTHER','{"amount":70.0}','imported_provento'),
                    ('buy-not-income','PETR4','2026-01-15',80.00,'Dividend payment','BUY','{"amount":80.0}','imported_provento'),
                    ('not-imported','PETR4','2026-01-15',90.00,'Dividend payment','DIVIDEND','{"amount":90.0}','imported_only'),
                    ('amortization-interest','PETR4','2026-01-15',100.00,'Investment income','INTEREST','{"movementType":"AMORTIZATION","amount":100.0}','imported_provento'),
                    ('amount-mismatch','PETR4','2026-01-15',77.00,'Dividend payment','DIVIDEND','{"grossAmount":77.0,"netAmount":75.0}','imported_provento'),
                    ('jcp-gross-net','PETR4','2026-01-15',100.00,'Investment income','INTEREST','{"movementType":"JCP","grossAmount":100.0,"amount":100.0,"netAmount":85.0}','imported_provento');
            ''')
            namespace = {
                'db': lambda: connection,
                'datetime': datetime,
                'date': date,
                'TZ': __import__('zoneinfo').ZoneInfo('America/Sao_Paulo'),
                'math': math,
                'json': json,
                're': re,
                'unicodedata': unicodedata,
                '_api_date': _api_date,
                '_is_income_event': lambda value: 'AMORTIZA' not in str(value or '').upper(),
                'typesafe_asset_type': lambda ticker: 'fii' if str(ticker).upper().endswith('11') else 'acao',
            }
            exec(compile(ast.Module(body=[amount_node, tolerance_node, category_node, income_node, node], type_ignores=[]), '<received-proventos>', 'exec'), namespace)
            rows = namespace['_received_proventos_records'](['PETR4'])
            self.assertEqual([row['pluggy_transaction_id'] for row in rows], ['good', 'jcp-gross-net', 'other-income'])
            self.assertEqual([row['valor_total'] for row in rows], [10.0, 85.0, 70.0])
            self.assertEqual(rows[0]['tipo_evento'], 'JCP')
            self.assertEqual(rows[1]['tipo_evento'], 'JCP')
        finally:
            connection.close()


if __name__ == '__main__':
    unittest.main()
