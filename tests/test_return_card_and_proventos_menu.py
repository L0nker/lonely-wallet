from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "app" / "templates" / "index.html").read_text(encoding="utf-8")
MAIN = (ROOT / "app" / "main.py").read_text(encoding="utf-8")


class ReturnCardAndProventosMenuTests(unittest.TestCase):
    def test_dashboard_rentability_card_is_replaced_by_paid_dividend_ranking(self):
        render_start = HTML.index("function render(){")
        render_end = HTML.index("\n\nconst rows=state.itens||[];", render_start)
        renderer = HTML[render_start:render_end]
        self.assertIn("Ranking de proventos", renderer)
        self.assertIn("provDash.maiores_pagadores||[]", renderer)
        self.assertIn('class="insight-recent-row"', renderer)
        self.assertIn('class="top-dividend-payer-total"', renderer)
        self.assertNotIn("return-stat-card", renderer)
        self.assertNotIn("renderConsolidatedReturnSparklines", renderer)

    def test_twelve_month_card_uses_rolling_twelve_month_price_returns(self):
        helper_start = HTML.index("function renderConsolidatedReturnSparklines(")
        helper_end = HTML.index("\n}\n\nfunction operationQuantity", helper_start) + 2
        helper = HTML[helper_start:helper_end]
        self.assertIn("performance.series_12m", helper)
        self.assertIn("const last12=twelveMonths.length?Number(twelveMonths[twelveMonths.length-1].valor)", helper)
        self.assertNotIn("const twelveMonths=totalSeries.slice(-12)", helper)
        self.assertIn("twelveMonthReturn!==null&&twelveMonthReturn!==undefined", helper)
        self.assertIn("totalReturn!==null&&totalReturn!==undefined", helper)

        performance_start = MAIN.index("def build_portfolio_performance(")
        performance_end = MAIN.index("\n\ndef _benchmark_monthly_from_yahoo", performance_start)
        performance = MAIN[performance_start:performance_end]
        self.assertIn("proventos não entram na rentabilidade nem no ganho de capital", performance)
        self.assertIn("# Retorno exclusivamente de preço: proventos ficam fora deste cálculo.", performance)

    def test_asset_options_menu_no_longer_offers_edit_columns(self):
        menu_start = HTML.index("function openAssetMenu(")
        menu_end = HTML.index("\nasync function investor10Url", menu_start)
        menu = HTML[menu_start:menu_end]
        self.assertNotIn("Editar colunas", menu)
        self.assertNotIn('assetMenuAction("columns"', menu)


if __name__ == "__main__":
    unittest.main()
