"""Tests for expiring pending dividends at the end of their calendar month."""
import ast
import datetime
import math
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / 'app' / 'main.py').read_text(encoding='utf-8')
TREE = ast.parse(SOURCE)
WANTED = {'_current_and_future_pending_proventos', '_confirmed_proventos_total_12m'}
NODES = [node for node in TREE.body if isinstance(node, ast.FunctionDef) and node.name in WANTED]


def api_date(value):
    return str(value or '')[:10] or None


class PendingProventosWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.namespace = {
            'date': datetime.date,
            'datetime': datetime.datetime,
            'TZ': None,
            'math': math,
            '_api_date': api_date,
            '_is_income_event': lambda value: 'AMORTIZA' not in str(value or '').upper(),
            '_b3_normalize': lambda value: str(value or '').upper(),
        }
        exec(compile(ast.Module(body=NODES, type_ignores=[]), '<pending-proventos-window>', 'exec'), cls.namespace)

    def test_expired_previous_month_is_removed_but_current_and_future_remain(self):
        rows = [
            {'ticker': 'MXRF11', 'status': 'A receber', 'data_pagamento': '2026-10-30', 'valor_total': 10, 'tipo_evento': 'RENDIMENTO'},
            {'ticker': 'MXRF11', 'status': 'A receber', 'data_pagamento': '2026-11-03', 'valor_total': 11, 'tipo_evento': 'RENDIMENTO'},
            {'ticker': 'MXRF11', 'status': 'A receber', 'data_pagamento': '2026-12-15', 'valor_total': 12, 'tipo_evento': 'RENDIMENTO'},
            {'ticker': 'MXRF11', 'status': 'A receber', 'data_pagamento': '2027-02-15', 'valor_total': 13, 'tipo_evento': 'RENDIMENTO'},
        ]
        result = self.namespace['_current_and_future_pending_proventos'](rows, as_of=datetime.date(2026, 11, 22))
        self.assertEqual([row['data_pagamento'] for row in result], ['2026-11-03', '2026-12-15', '2027-02-15'])

    def test_paid_rows_amortizations_invalid_dates_and_nonpositive_values_are_excluded(self):
        rows = [
            {'ticker': 'MXRF11', 'status': 'Pago', 'data_pagamento': '2026-11-10', 'valor_total': 10, 'tipo_evento': 'RENDIMENTO'},
            {'ticker': 'MXRF11', 'status': 'A receber', 'data_pagamento': '2026-11-10', 'valor_total': 10, 'tipo_evento': 'AMORTIZACAO'},
            {'ticker': 'MXRF11', 'status': 'A receber', 'data_pagamento': 'invalid', 'valor_total': 10, 'tipo_evento': 'RENDIMENTO'},
            {'ticker': 'MXRF11', 'status': 'A receber', 'data_pagamento': '2026-11-10', 'valor_total': 0, 'tipo_evento': 'RENDIMENTO'},
        ]
        result = self.namespace['_current_and_future_pending_proventos'](rows, as_of=datetime.date(2026, 11, 22))
        self.assertEqual(result, [])

    def test_current_month_pending_is_included_in_confirmed_total_even_if_day_passed(self):
        rows = [
            {'ticker': 'MXRF11', 'status': 'A receber', 'data_pagamento': '2026-11-05', 'valor_total': 20, 'tipo_evento': 'RENDIMENTO'},
            {'ticker': 'MXRF11', 'status': 'A receber', 'data_pagamento': '2026-10-28', 'valor_total': 30, 'tipo_evento': 'RENDIMENTO'},
            {'ticker': 'MXRF11', 'status': 'A receber', 'data_pagamento': '2026-12-05', 'valor_total': 10, 'tipo_evento': 'RENDIMENTO'},
        ]
        total = self.namespace['_confirmed_proventos_total_12m'](100, rows, as_of=datetime.date(2026, 11, 22))
        self.assertEqual(total, 130.0)

    def test_month_end_expires_the_previous_month_events(self):
        rows = [
            {'ticker': 'MXRF11', 'status': 'A receber', 'data_pagamento': '2026-11-30', 'valor_total': 20, 'tipo_evento': 'RENDIMENTO'},
        ]
        before_close = self.namespace['_current_and_future_pending_proventos'](rows, as_of=datetime.date(2026, 11, 30))
        after_close = self.namespace['_current_and_future_pending_proventos'](rows, as_of=datetime.date(2026, 12, 1))
        self.assertEqual(len(before_close), 1)
        self.assertEqual(after_close, [])


if __name__ == '__main__':
    unittest.main()
