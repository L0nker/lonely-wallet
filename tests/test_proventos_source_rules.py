"""Focused unit tests for the Pluggy-paid / all-providers-pending rule."""
import ast
import datetime
import json
import math
import sqlite3
import unicodedata
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / 'app' / 'main.py').read_text(encoding='utf-8')
TREE = ast.parse(SOURCE)
WANTED = {
    '_received_proventos_records', '_provento_event_kind', '_provento_same_payment',
    '_provento_same_payment', '_dedupe_pending_proventos', '_is_income_event'
}
NODES = [node for node in TREE.body if isinstance(node, ast.FunctionDef) and node.name in WANTED]

class FakeRow(dict):
    pass

class ProventosSourceRulesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.con = sqlite3.connect(':memory:')
        cls.con.row_factory = sqlite3.Row
        cls.con.execute('''CREATE TABLE pluggy_investment_transactions (
            transaction_id TEXT PRIMARY KEY, ticker TEXT, transaction_date TEXT,
            description TEXT, movement_type TEXT, amount REAL, import_state TEXT, raw_json TEXT
        )''')
        cls.namespace = {
            'db': lambda: cls.con,
            'datetime': datetime.datetime,
            'date': datetime.date,
            'TZ': ZoneInfo('America/Sao_Paulo'),
            'math': math,
            'json': json,
            'unicodedata': unicodedata,
            'typesafe_asset_type': lambda ticker: 'fii' if str(ticker).endswith('11') else 'acao',
            '_api_date': lambda value: str(value or '')[:10] or None,
        }
        exec(compile(ast.Module(body=NODES, type_ignores=[]), '<proventos-source-rules>', 'exec'), cls.namespace)

    def setUp(self):
        self.con.execute('DELETE FROM pluggy_investment_transactions')
        self.con.commit()

    @classmethod
    def tearDownClass(cls):
        cls.con.close()

    def test_paid_events_are_read_only_from_pluggy_transactions(self):
        self.con.execute('''INSERT INTO pluggy_investment_transactions
            (transaction_id,ticker,transaction_date,description,movement_type,amount,import_state,raw_json)
            VALUES(?,?,?,?,?,?,?,?)''',
            ('pluggy-1','MXRF11','2026-01-15','Rendimento mensal','INTEREST',20.0,'provento_duplicado_ignorado','{}'))
        self.con.commit()
        records = self.namespace['_received_proventos_records'](['MXRF11'])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['origem'], 'Pluggy')
        self.assertEqual(records[0]['status'], 'Pago')
        self.assertEqual(records[0]['valor_total'], 20.0)

    def test_non_pluggy_paid_rows_cannot_enter_received_records(self):
        # The paid-receipt reader only queries pluggy_investment_transactions.
        # An API-only event has no matching Pluggy transaction and is not returned.
        records = self.namespace['_received_proventos_records'](['VALE3'])
        self.assertEqual(records, [])

    def test_distinct_pluggy_transaction_ids_remain_separate_receipts(self):
        # Distinct Pluggy transaction IDs can represent real receipts in
        # separate investment accounts, even when ticker/date/amount match.
        rows = [
            ('pluggy-1','MXRF11','2026-01-15','Rendimento mensal','INTEREST',20.0,'provento_duplicado_ignorado','{}'),
            ('pluggy-2','MXRF11','2026-01-15','Rendimento mensal','INTEREST',20.0,'imported_provento','{}'),
        ]
        self.con.executemany('''INSERT INTO pluggy_investment_transactions
            (transaction_id,ticker,transaction_date,description,movement_type,amount,import_state,raw_json)
            VALUES(?,?,?,?,?,?,?,?)''', rows)
        self.con.commit()
        records = self.namespace['_received_proventos_records'](['MXRF11'])
        self.assertEqual(len(records), 2)
        self.assertEqual({item['external_id'] for item in records}, {'pluggy-1', 'pluggy-2'})

    def test_pending_providers_dedupe_and_pluggy_confirmation_suppresses_pending_copy(self):
        paid = [{'ticker':'MXRF11','status':'Pago','data_pagamento':'2026-01-15',
                 'valor_total':20.0,'tipo_evento':'RENDIMENTO','origem':'Pluggy'}]
        pending = [
            {'ticker':'MXRF11','status':'A receber','data_pagamento':'2026-01-15',
             'valor_total':20.0,'tipo_evento':'Rendimento','origem':'BRAPI'},
            {'ticker':'MXRF11','status':'A receber','data_pagamento':'2026-01-15',
             'valor_total':20.0,'tipo_evento':'RENDIMENTO','origem':'Dados B3'},
            {'ticker':'MXRF11','status':'Pago','data_pagamento':'2026-12-15',
             'valor_total':25.0,'tipo_evento':'DIVIDENDO','origem':'BRAPI'},
            {'ticker':'MXRF11','status':'A receber','data_pagamento':'2026-12-15',
             'valor_total':25.10,'tipo_evento':'RENDIMENTO','origem':'HG Brasil'},
        ]
        deduped = self.namespace['_dedupe_pending_proventos'](pending, paid)
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0]['status'], 'A receber')
        self.assertEqual(deduped[0]['valor_total'], 25.0)

    def test_pluggy_interest_description_jcp_suppresses_pending_jcp(self):
        self.con.execute('''INSERT INTO pluggy_investment_transactions
            (transaction_id,ticker,transaction_date,description,movement_type,amount,import_state,raw_json)
            VALUES(?,?,?,?,?,?,?,?)''',
            ('pluggy-jcp','VALE3','2026-01-15','Juros Sobre Capital Proprio','INTEREST',25.0,'imported_provento','{}'))
        self.con.commit()
        paid = self.namespace['_received_proventos_records'](['VALE3'])
        self.assertEqual(paid[0]['tipo_evento'], 'JCP')
        pending = [{'ticker':'VALE3','status':'A receber','data_pagamento':'2026-01-15',
                    'valor_total':25.0,'tipo_evento':'JCP','origem':'BRAPI'}]
        self.assertEqual(self.namespace['_dedupe_pending_proventos'](pending, paid), [])

    def test_jcp_remains_distinct_from_regular_dividend(self):
        pending = [
            {'ticker':'VALE3','status':'A receber','data_pagamento':'2026-12-15',
             'valor_total':25.0,'tipo_evento':'DIVIDENDO','origem':'BRAPI'},
            {'ticker':'VALE3','status':'A receber','data_pagamento':'2026-12-15',
             'valor_total':25.0,'tipo_evento':'JCP','origem':'Dados B3'},
        ]
        deduped = self.namespace['_dedupe_pending_proventos'](pending, [])
        self.assertEqual(len(deduped), 2)
        self.assertEqual({item['tipo_evento'] for item in deduped}, {'DIVIDENDO', 'JCP'})

if __name__ == '__main__':
    unittest.main()
