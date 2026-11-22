import ast
import json
import math
import re
import unicodedata
import unittest
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / 'app' / 'main.py').read_text(encoding='utf-8')
TREE = ast.parse(SOURCE)
WANTED = {
    '_rolling_monthly_arithmetic_average',
    '_fixed_income_iof_percent',
    '_fixed_income_ir_percent',
    '_fixed_income_tax_profile',
    '_fixed_income_application_date',
    '_fixed_income_tax_amounts_for_gain',
    '_fixed_income_net_balance_estimate',
    '_fixed_income_position_value',
}
NAMESPACE = {
    'json': json, 'math': math, 're': re, 'unicodedata': unicodedata,
    'date': date, 'datetime': datetime, 'timedelta': timedelta,
    'TZ': ZoneInfo('America/Sao_Paulo'),
    '_FIXED_INCOME_IOF_PERCENT': (96, 93, 90, 86, 83, 80, 76, 73, 70, 66, 63, 60, 56, 53, 50, 46, 43, 40, 36, 33, 30, 26, 23, 20, 16, 13, 10, 6, 3),
    'snapshot_date_for': lambda cutoff=None: None,
    'imported_balance': lambda ticker, cutoff=None: None,
    '_fixed_income_factor': lambda details, start, end: (1.1, None),
}

for node in TREE.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in WANTED:
        module = ast.Module(body=[node], type_ignores=[])
        exec(compile(module, str(ROOT / 'app' / 'main.py'), 'exec'), NAMESPACE)


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows
    def fetchall(self):
        return self.rows


class FakeDB:
    def __init__(self, rows):
        self.rows = rows
    def execute(self, query, params=()):
        ticker = params[0] if params else None
        rows = [row for row in self.rows if str(row.get('ticker', '')).upper() == str(ticker).upper()]
        if 'data>?' in query and len(params) >= 2:
            rows = [row for row in rows if row['data'] > params[1]]
        if 'data<=?' in query:
            cutoff = params[-1]
            rows = [row for row in rows if row['data'] <= cutoff]
        return FakeCursor(rows)


class FinancialHelperTests(unittest.TestCase):
    def setUp(self):
        self.old_db = NAMESPACE.get('db')
        self.old_factor = NAMESPACE.get('_fixed_income_factor')
        self.old_snapshot = NAMESPACE.get('snapshot_date_for')
        self.old_imported = NAMESPACE.get('imported_balance')
        NAMESPACE['snapshot_date_for'] = lambda cutoff=None: None
        NAMESPACE['imported_balance'] = lambda ticker, cutoff=None: None
        NAMESPACE['_fixed_income_factor'] = lambda details, start, end: (1.1, None)

    def tearDown(self):
        for name, old in (('db', self.old_db), ('_fixed_income_factor', self.old_factor),
                          ('snapshot_date_for', self.old_snapshot), ('imported_balance', self.old_imported)):
            if old is None:
                NAMESPACE.pop(name, None)
            else:
                NAMESPACE[name] = old

    def test_average_is_arithmetic_over_available_months(self):
        series, result, count = NAMESPACE['_rolling_monthly_arithmetic_average']([
            {'mes': '2026-01', 'retorno': 0.04, 'resultado': 40},
            {'mes': '2026-02', 'retorno': -0.02, 'resultado': -20},
            {'mes': '2026-03', 'retorno': 0.01, 'resultado': 10},
        ], 12)
        self.assertEqual(count, 3)
        self.assertEqual(result, 1.0)
        self.assertEqual(series[-1]['meses_base'], 3)
        self.assertAlmostEqual(series[-1]['valor'], 1.0)

    def test_average_uses_only_last_twelve_calculable_months(self):
        rows = [{'mes': f'2025-{i:02d}', 'retorno': i / 1000, 'resultado': i} for i in range(1, 14)]
        _, result, count = NAMESPACE['_rolling_monthly_arithmetic_average'](rows, 12)
        self.assertEqual(count, 12)
        self.assertAlmostEqual(result, 0.75, places=2)

    def test_iof_table_edges(self):
        fn = NAMESPACE['_fixed_income_iof_percent']
        self.assertEqual(fn(1), 96.0)
        self.assertEqual(fn(29), 3.0)
        self.assertEqual(fn(30), 0.0)

    def test_ir_table_edges(self):
        fn = NAMESPACE['_fixed_income_ir_percent']
        self.assertEqual(fn(180), 22.5)
        self.assertEqual(fn(181), 20.0)
        self.assertEqual(fn(360), 20.0)
        self.assertEqual(fn(361), 17.5)
        self.assertEqual(fn(720), 17.5)
        self.assertEqual(fn(721), 15.0)

    def test_tax_profile_depends_on_title_not_bank_name(self):
        profile = NAMESPACE['_fixed_income_tax_profile']({'titulo': 'CDB', 'emissor': 'Nubank'}, 'ATIVO1')
        self.assertEqual(profile, {'titulo': 'CDB', 'ir': True, 'iof': True})
        lca = NAMESPACE['_fixed_income_tax_profile']({'titulo': 'LCA', 'emissor': 'Banco Original'}, 'ATIVO2')
        self.assertEqual(lca, {'titulo': 'LCA', 'ir': False, 'iof': False})
        lci = NAMESPACE['_fixed_income_tax_profile']({'titulo': 'LCI', 'emissor': 'Banco Original'}, 'ATIVO2B')
        self.assertEqual(lci, {'titulo': 'LCI', 'ir': False, 'iof': True})
        cra = NAMESPACE['_fixed_income_tax_profile']({'titulo': 'CRA'}, 'ATIVO2C')
        self.assertEqual(cra, {'titulo': 'CRA', 'ir': False, 'iof': False})
        lf = NAMESPACE['_fixed_income_tax_profile']({'titulo': 'LF'}, 'ATIVO2D')
        self.assertEqual(lf, {'titulo': 'LF', 'ir': True, 'iof': False})
        self.assertIsNone(NAMESPACE['_fixed_income_tax_profile']({'titulo': 'Outro'}, 'ATIVO3'))
        self.assertIsNone(NAMESPACE['_fixed_income_tax_profile']({'emissor': 'Banco CDB S.A.'}, 'ATIVO4'))

    def test_cdb_net_balance_applies_iof_before_ir(self):
        NAMESPACE['db'] = lambda: FakeDB([{
            'ticker': 'CDB1', 'tipo': 'renda_fixa', 'status': 'executada', 'lado': 'compra',
            'quantidade': 1, 'preco': 100, 'custos': 0, 'data': '2026-11-21',
            'detalhes': json.dumps({'titulo': 'CDB', 'emissor': 'Nubank', 'indexador': 'CDI', 'taxa': '120% do CDI', 'forma': 'Pós-fixado'})
        }])
        result = NAMESPACE['_fixed_income_net_balance_estimate']('CDB1', 110, '2026-11-22')
        self.assertEqual(result['iof'], 9.6)
        self.assertEqual(result['ir'], 0.09)
        self.assertEqual(result['saldo'], 100.31)

    def test_tax_exempt_lca_returns_gross_balance_without_fake_tax(self):
        NAMESPACE['db'] = lambda: FakeDB([{
            'ticker': 'LCA1', 'tipo': 'renda_fixa', 'status': 'executada', 'lado': 'compra',
            'quantidade': 1, 'preco': 100, 'custos': 0, 'data': '2026-10-01',
            'detalhes': json.dumps({'titulo': 'LCA', 'emissor': 'Banco Original', 'indexador': 'CDI', 'taxa': '93% do CDI'})
        }])
        result = NAMESPACE['_fixed_income_net_balance_estimate']('LCA1', 103, '2026-11-22')
        self.assertEqual(result['saldo'], 103.0)
        self.assertEqual(result['iof'], 0.0)
        self.assertEqual(result['ir'], 0.0)

    def test_cdb_snapshot_without_original_application_date_is_not_guessed(self):
        NAMESPACE['db'] = lambda: FakeDB([{
            'ticker': 'CDB2', 'tipo': 'renda_fixa', 'status': 'executada', 'lado': 'compra',
            'quantidade': 1, 'preco': 100, 'custos': 0, 'data': '2026-11-21',
            'detalhes': json.dumps({'titulo': 'CDB', 'emissor': 'Nubank', 'indexador': 'CDI',
                                    'taxa': '120% do CDI', 'pluggy_snapshot': 'snapshot'})
        }])
        result = NAMESPACE['_fixed_income_net_balance_estimate']('CDB2', 110, '2026-11-22')
        self.assertIsNone(result['saldo'])
        self.assertIn('Data original', result['mensagem'])

    def test_partial_fixed_income_sale_reduces_principal_by_units_not_cash_proceeds(self):
        NAMESPACE['db'] = lambda: FakeDB([
            {'ticker': 'CDB3', 'tipo': 'renda_fixa', 'status': 'executada', 'lado': 'compra',
             'quantidade': 1, 'preco': 1000, 'custos': 0, 'data': '2026-01-01',
             'detalhes': json.dumps({'titulo': 'CDB', 'emissor': 'Nubank', 'indexador': 'CDI', 'taxa': '100% do CDI'})},
            {'ticker': 'CDB3', 'tipo': 'renda_fixa', 'status': 'executada', 'lado': 'venda',
             'quantidade': 0.5, 'preco': 1200, 'custos': 0, 'data': '2026-06-01', 'detalhes': '{}'},
        ])
        cost, value, ok, warning = NAMESPACE['_fixed_income_position_value']('CDB3', 500, 550, '2026-11-22')
        self.assertTrue(ok, warning)
        self.assertEqual(cost, 500.0)
        self.assertEqual(value, 550.0)


    def test_lci_short_term_applies_iof_but_not_ir(self):
        NAMESPACE['db'] = lambda: FakeDB([{
            'ticker': 'LCI1', 'tipo': 'renda_fixa', 'status': 'executada', 'lado': 'compra',
            'quantidade': 1, 'preco': 100, 'custos': 0, 'data': '2026-11-21',
            'detalhes': json.dumps({'titulo': 'LCI', 'emissor': 'Banco Original', 'indexador': 'CDI', 'taxa': '93% do CDI'})
        }])
        result = NAMESPACE['_fixed_income_net_balance_estimate']('LCI1', 110, '2026-11-22')
        self.assertEqual(result['iof'], 9.6)
        self.assertEqual(result['ir'], 0.0)
        self.assertEqual(result['saldo'], 100.4)

    def test_pluggy_snapshot_without_application_date_uses_its_current_balance(self):
        NAMESPACE['db'] = lambda: FakeDB([{
            'ticker': 'RFCURRENT', 'tipo': 'renda_fixa', 'status': 'executada', 'lado': 'compra',
            'quantidade': 1, 'preco': 1000, 'custos': 0, 'data': '2026-11-22',
            'detalhes': json.dumps({'titulo': 'CDB', 'emissor': 'Nubank', 'indexador': 'CDI',
                                    'taxa': '120% do CDI', 'pluggy_snapshot': 'snapshot', 'saldo_atual': 1137.45})
        }])
        NAMESPACE['_fixed_income_factor'] = lambda details, start, end: (1.5, None)
        cost, value, ok, warning = NAMESPACE['_fixed_income_position_value']('RFCURRENT', 1000, 1000, '2026-11-22')
        self.assertTrue(ok)
        self.assertEqual(cost, 1000.0)
        self.assertEqual(value, 1137.45)
        self.assertIn('instituição', warning)


    def test_pluggy_snapshot_balance_is_reduced_after_partial_redemption(self):
        NAMESPACE['db'] = lambda: FakeDB([
            {'ticker': 'RFCURRENT2', 'tipo': 'renda_fixa', 'status': 'executada', 'lado': 'compra',
             'quantidade': 1, 'preco': 1000, 'custos': 0, 'data': '2026-11-20',
             'detalhes': json.dumps({'titulo': 'CDB', 'emissor': 'Nubank', 'indexador': 'CDI',
                                     'taxa': '120% do CDI', 'pluggy_snapshot': 'snapshot', 'saldo_atual': 1200})},
            {'ticker': 'RFCURRENT2', 'tipo': 'renda_fixa', 'status': 'executada', 'lado': 'venda',
             'quantidade': 0.25, 'preco': 1250, 'custos': 0, 'data': '2026-11-21', 'detalhes': '{}'},
        ])
        cost, value, ok, warning = NAMESPACE['_fixed_income_position_value']('RFCURRENT2', 750, 900, '2026-11-22')
        self.assertTrue(ok)
        self.assertEqual(cost, 750.0)
        self.assertEqual(value, 900.0)

    def test_fixed_income_net_column_is_present_after_gross_balance(self):
        html = (ROOT / 'app' / 'templates' / 'index.html').read_text(encoding='utf-8')
        self.assertLess(html.index("['valor_atual','Saldo'"), html.index("['saldo_liquido_estimado','Saldo com descontos'"))
        self.assertIn("'saldo_liquido_estimado'", html[html.index('const fixedIncomeSummaryColumns'):html.index('const shown=')])
        self.assertIn("saldo_liquido_estimado:'saldo_com_descontos'", html.replace(' ', ''))

    def test_unknown_title_returns_unavailable_instead_of_guessing(self):
        NAMESPACE['db'] = lambda: FakeDB([{
            'ticker': 'RF4', 'tipo': 'renda_fixa', 'status': 'executada', 'lado': 'compra',
            'quantidade': 1, 'preco': 100, 'custos': 0, 'data': '2026-01-01',
            'detalhes': json.dumps({'titulo': 'Outro', 'emissor': 'Instituição'})
        }])
        result = NAMESPACE['_fixed_income_net_balance_estimate']('RF4', 110, '2026-11-22')
        self.assertIsNone(result['saldo'])
        self.assertIn('Tipo de título', result['mensagem'])


if __name__ == '__main__':
    unittest.main()
