"""Regression tests for overdue, unconfirmed proventos."""
from __future__ import annotations

import ast
import math
from datetime import date, datetime
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
MAIN = (ROOT / 'app' / 'main.py').read_text(encoding='utf-8')
HTML = (ROOT / 'app' / 'templates' / 'index.html').read_text(encoding='utf-8')


def _extract_monthly_builder():
    tree = ast.parse(MAIN)
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                and n.name == '_build_proventos_monthly_values')
    namespace = {
        'math': math,
        '_api_date': lambda value: str(value or '')[:10] if value else '',
        '_is_income_event': lambda event_type: 'AMORTIZA' not in str(event_type or '').upper(),
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<monthly-builder>', 'exec'), namespace)
    return namespace['_build_proventos_monthly_values']


def _extract_filter():
    tree = ast.parse(MAIN)
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                and n.name == '_filter_upcoming_proventos_by_month')
    namespace = {
        'date': date,
        'datetime': datetime,
        'math': math,
        'TZ': None,
        '_api_date': lambda value: str(value or '')[:10] if value else '',
        '_is_income_event': lambda event_type: 'AMORTIZA' not in str(event_type or '').upper(),
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<overdue-filter>', 'exec'), namespace)
    return namespace['_filter_upcoming_proventos_by_month']


class OverdueProventosTests(unittest.TestCase):
    def setUp(self):
        self.filter_rows = _extract_filter()
        self.build_monthly = _extract_monthly_builder()
        self.today = date(2026, 11, 23)

    def test_past_payment_is_overdue_not_received(self):
        rows = self.filter_rows([{
            'ticker': 'MXRF11', 'status': 'A receber',
            'data_pagamento': '2026-11-16', 'valor_total': 8.64,
            'tipo_evento': 'RENDIMENTO'
        }], as_of=self.today)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['status'], 'Atrasado')
        self.assertEqual(rows[0]['dias_atraso'], 7)
        self.assertEqual(rows[0]['confirmacao'], 'aguardando_confirmacao_pagamento')
        self.assertIn('não confirmado', rows[0]['remarks'])

    def test_payment_due_today_remains_upcoming_through_the_day(self):
        rows = self.filter_rows([{
            'ticker': 'TODAY11', 'status': 'A receber',
            'data_pagamento': '2026-11-23', 'valor_total': 5,
            'tipo_evento': 'DIVIDENDO'
        }], as_of=self.today)
        self.assertEqual(rows[0]['status'], 'A receber')
        self.assertNotIn('dias_atraso', rows[0])

    def test_future_payment_remains_upcoming(self):
        rows = self.filter_rows([{
            'ticker': 'FUTURE11', 'status': 'A receber',
            'data_pagamento': '2026-12-15', 'valor_total': 5,
            'tipo_evento': 'JCP'
        }], as_of=self.today)
        self.assertEqual(rows[0]['status'], 'A receber')

    def test_invalid_and_amortization_events_are_excluded(self):
        rows = self.filter_rows([
            {'ticker':'BAD11','status':'A receber','data_pagamento':'bad','valor_total':10,'tipo_evento':'DIVIDENDO'},
            {'ticker':'AMORT11','status':'A receber','data_pagamento':'2026-11-16','valor_total':10,'tipo_evento':'AMORTIZAÇÃO'},
        ], as_of=self.today)
        self.assertEqual(rows, [])

    def test_dashboard_separates_overdue_from_future_and_confirmed(self):
        self.assertIn("overdue = [row for row in classified_pending if row.get('status') == 'Atrasado']", MAIN)
        self.assertIn("upcoming = [row for row in classified_pending if row.get('status') == 'A receber']", MAIN)
        self.assertIn("'atrasados':sorted(overdue,key=lambda x:x['data_pagamento'])", MAIN)
        self.assertIn("'a_receber':sorted(upcoming,key=lambda x:x['data_pagamento'])", MAIN)

    def test_monthly_payload_separates_received_upcoming_and_overdue(self):
        rows = self.build_monthly(
            ['2026-11', '2026-12'],
            {'2026-11': 12.50},
            upcoming=[
                {'status': 'A receber', 'data_pagamento': '2026-11-23', 'valor_total': 5.25, 'tipo_evento': 'DIVIDENDO'},
                {'status': 'A receber', 'data_pagamento': '2026-12-15', 'valor_total': 7.00, 'tipo_evento': 'JCP'},
            ],
            overdue=[
                {'status': 'Atrasado', 'data_pagamento': '2026-11-16', 'valor_total': 8.64, 'tipo_evento': 'RENDIMENTO'},
            ],
        )
        self.assertEqual(rows, [
            {'mes': '2026-11', 'recebido': 12.50, 'a_receber': 5.25, 'atrasado': 8.64, 'total': 26.39},
            {'mes': '2026-12', 'recebido': 0.00, 'a_receber': 7.00, 'atrasado': 0.00, 'total': 7.00},
        ])

    def test_monthly_payload_never_counts_overdue_as_received(self):
        rows = self.build_monthly(
            ['2026-11'], {},
            upcoming=[],
            overdue=[{'status': 'Atrasado', 'data_pagamento': '2026-11-16', 'valor_total': 8.64, 'tipo_evento': 'RENDIMENTO'}],
        )
        self.assertEqual(rows[0]['recebido'], 0.0)
        self.assertEqual(rows[0]['a_receber'], 0.0)
        self.assertEqual(rows[0]['atrasado'], 8.64)

    def test_monthly_payload_ignores_invalid_rows_and_amortization(self):
        rows = self.build_monthly(
            ['2026-11'], {'2026-11': 10},
            upcoming=[
                {'status': 'Atrasado', 'data_pagamento': '2026-11-16', 'valor_total': 5, 'tipo_evento': 'DIVIDENDO'},
                {'status': 'A receber', 'data_pagamento': 'bad', 'valor_total': 9, 'tipo_evento': 'DIVIDENDO'},
                {'status': 'A receber', 'data_pagamento': '2026-11-24', 'valor_total': -2, 'tipo_evento': 'DIVIDENDO'},
                {'status': 'A receber', 'data_pagamento': '2026-11-24', 'valor_total': 3, 'tipo_evento': 'AMORTIZAÇÃO'},
            ],
            overdue=[{'status': 'Atrasado', 'data_pagamento': '2026-11-16', 'valor_total': float('nan'), 'tipo_evento': 'DIVIDENDO'}],
        )
        self.assertEqual(rows, [{'mes': '2026-11', 'recebido': 10.0, 'a_receber': 0.0, 'atrasado': 0.0, 'total': 10.0}])

    def test_dashboard_defines_monthly_values_before_serializing_response(self):
        tree = ast.parse(MAIN)
        dashboard = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'dashboard')
        assignments = [n for n in ast.walk(dashboard) if isinstance(n, ast.Assign)
                       and any(isinstance(target, ast.Name) and target.id == 'monthly_values' for target in n.targets)]
        self.assertTrue(assignments, 'dashboard() must define monthly_values')
        self.assertIn("'mensal':monthly_values", MAIN)
        self.assertIn('_build_proventos_monthly_values(', MAIN)
        assignment_line = min(n.lineno for n in assignments)
        response_use_line = MAIN.index("'mensal':monthly_values")
        line_number = MAIN[:response_use_line].count('\n') + 1
        self.assertLess(assignment_line, line_number, 'monthly_values must be assigned before jsonify uses it')

    def test_frontend_has_overdue_filter_and_separate_chart_series(self):
        self.assertIn("['atrasados','Atrasados']", HTML)
        self.assertIn("status==='Atrasado'", HTML)
        self.assertIn("label:'Pagamentos atrasados'", HTML)
        self.assertIn("...(d.atrasados||[])", HTML)
        self.assertIn("Atrasado · aguardando confirmação", HTML)

    def test_dashboard_snapshot_signature_changes_at_local_day_boundary(self):
        self.assertIn("records.append(('local_date', datetime.now(TZ).date().isoformat()))", MAIN)
        self.assertIn('DASHBOARD_CACHE_FORMAT = 9', MAIN)


if __name__ == '__main__':
    unittest.main()
