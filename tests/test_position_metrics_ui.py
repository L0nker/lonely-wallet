import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = (ROOT / 'app' / 'templates' / 'index.html').read_text(encoding='utf-8')
MAIN = (ROOT / 'app' / 'main.py').read_text(encoding='utf-8')


class PositionMetricsUITests(unittest.TestCase):
    def test_visible_variation_uses_price_change_not_daily_quote_change(self):
        self.assertIn("const change=i.variacao_pct", TEMPLATE)
        self.assertIn("item.variacao_pct", TEMPLATE)
        self.assertNotIn("map(item=>item.variacao_dia)", TEMPLATE)

    def test_profitability_tooltip_is_compact_and_uses_total_paid_return(self):
        self.assertIn("Quanto uma rentabilidade de ${pct(profitability)} representa?\\n${brl(amount)}", TEMPLATE)
        self.assertNotIn("O percentual exibido na coluna é calculado separadamente", TEMPLATE)
        self.assertIn("rentabilidade_valor", TEMPLATE)
        self.assertIn("function profitPositionReturnPct(item){\n return item&&item.lucro_pct!==null", TEMPLATE)
        self.assertNotIn("?Number(item.pluggy_retorno_pct)", TEMPLATE)

    def test_backend_adds_only_received_dividends_to_individual_return(self):
        self.assertIn("calculate_position_performance(", MAIN)
        self.assertIn("dividends, quote_available=quote_available", MAIN)
        self.assertIn("WHERE status='Pago'", MAIN)
        self.assertIn("is_paid_provento(row.get('status'), payment_date, today)", MAIN)


if __name__ == '__main__':
    unittest.main()
