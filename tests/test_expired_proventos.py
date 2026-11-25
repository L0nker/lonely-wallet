"""Regression tests for excluding unpaid proventos after their payment date."""
from __future__ import annotations

import ast
import math
from datetime import date, datetime
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
MAIN = (ROOT / 'app' / 'main.py').read_text(encoding='utf-8')
HTML = (ROOT / 'app' / 'templates' / 'index.html').read_text(encoding='utf-8')


def _extract_function(name):
    tree = ast.parse(MAIN)
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    namespace = {
        'date': date,
        'datetime': datetime,
        'math': math,
        'TZ': None,
        '_api_date': lambda value: str(value or '')[:10] if value else '',
        '_is_income_event': lambda event_type: 'AMORTIZA' not in str(event_type or '').upper(),
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), f'<{name}>', 'exec'), namespace)
    return namespace[name]


class ExpiredProventosTests(unittest.TestCase):
    def setUp(self):
        self.filter_rows = _extract_function('_filter_current_future_proventos')
        self.build_monthly = _extract_function('_build_proventos_monthly_values')
        self.today = date(2026, 11, 23)

    def test_past_unpaid_event_is_discarded_without_mutating_source(self):
        original = {
            'ticker': 'MXRF11', 'status': 'A receber',
            'data_pagamento': '2026-11-16', 'valor_total': 8.64,
            'tipo_evento': 'RENDIMENTO'
        }
        rows = self.filter_rows([original], as_of=self.today)
        self.assertEqual(rows, [])
        self.assertEqual(original['status'], 'A receber')
        self.assertEqual(original['data_pagamento'], '2026-11-16')

    def test_payment_due_today_remains_upcoming_through_the_day(self):
        rows = self.filter_rows([{
            'ticker': 'TODAY11', 'status': 'A receber',
            'data_pagamento': '2026-11-23', 'valor_total': 5,
            'tipo_evento': 'DIVIDENDO'
        }], as_of=self.today)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['status'], 'A receber')

    def test_future_payment_remains_upcoming(self):
        rows = self.filter_rows([{
            'ticker': 'FUTURE11', 'status': 'A receber',
            'data_pagamento': '2026-12-15', 'valor_total': 5,
            'tipo_evento': 'JCP'
        }], as_of=self.today)
        self.assertEqual(rows[0]['status'], 'A receber')

    def test_invalid_amortization_and_expired_events_are_excluded(self):
        rows = self.filter_rows([
            {'ticker':'BAD11','status':'A receber','data_pagamento':'bad','valor_total':10,'tipo_evento':'DIVIDENDO'},
            {'ticker':'AMORT11','status':'A receber','data_pagamento':'2026-12-01','valor_total':10,'tipo_evento':'AMORTIZAÇÃO'},
            {'ticker':'OLD11','status':'A receber','data_pagamento':'2026-11-16','valor_total':10,'tipo_evento':'DIVIDENDO'},
        ], as_of=self.today)
        self.assertEqual(rows, [])

    def test_monthly_payload_contains_only_received_and_active_forecasts(self):
        rows = self.build_monthly(
            ['2026-11', '2026-12'],
            {'2026-11': 12.50},
            upcoming=[
                {'status': 'A receber', 'data_pagamento': '2026-11-23', 'valor_total': 5.25, 'tipo_evento': 'DIVIDENDO'},
                {'status': 'A receber', 'data_pagamento': '2026-12-15', 'valor_total': 7.00, 'tipo_evento': 'JCP'},
                {'status': 'A receber', 'data_pagamento': '2026-11-16', 'valor_total': 8.64, 'tipo_evento': 'RENDIMENTO'},
            ],
            as_of=self.today,
        )
        self.assertEqual(rows, [
            {'mes': '2026-11', 'recebido': 12.50, 'a_receber': 5.25, 'total': 17.75},
            {'mes': '2026-12', 'recebido': 0.00, 'a_receber': 7.00, 'total': 7.00},
        ])

    def test_dashboard_contract_has_no_overdue_fields(self):
        self.assertIn('_filter_current_future_proventos(upcoming, as_of=today)', MAIN)
        self.assertIn("'a_receber':sorted(upcoming,key=lambda x:x['data_pagamento'])", MAIN)
        self.assertNotIn("'atrasados':", MAIN)
        self.assertNotIn("'total_atrasado':", MAIN)
        self.assertNotIn("'quantidade_atrasados':", MAIN)

    def test_frontend_has_no_overdue_ui_and_uses_two_chart_series(self):
        self.assertNotIn('Pagamento atrasado', HTML)
        self.assertNotIn("['atrasados','Atrasados']", HTML)
        self.assertNotIn("status==='Atrasado'", HTML)
        self.assertNotIn("label:'Pagamentos atrasados'", HTML)
        self.assertNotIn('...(d.atrasados||[])', HTML)
        self.assertIn("...(d.a_receber||[]).filter(x=>String(x.data_pagamento||'')>=todayRowsISO)", HTML)
        self.assertIn("const isAnyClassifiedEvent=x=>isConfirmedPaid(x)||isCurrentOrFuturePending(x);", HTML)
        self.assertIn("datasets:[{label:'Proventos recebidos'", HTML)
        self.assertNotIn('overdue-dot', HTML)

    def test_dashboard_cache_signature_changes_at_local_day_boundary(self):
        self.assertIn("records.append(('local_date', datetime.now(TZ).date().isoformat()))", MAIN)
        self.assertIn('DASHBOARD_CACHE_FORMAT = 19', MAIN)
        self.assertNotIn('estimado_pela_data_de_pagamento_sem_confirmacao_pluggy', MAIN)
        self.assertNotIn('APIs alternativas de meses encerrados', MAIN)
        self.assertNotIn('estimado_pela_data_de_pagamento_sem_confirmacao_pluggy', HTML)
        self.assertNotIn('Pago · API', HTML)


if __name__ == '__main__':
    unittest.main()
