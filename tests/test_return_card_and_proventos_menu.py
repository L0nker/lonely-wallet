from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "app" / "templates" / "index.html").read_text(encoding="utf-8")
MAIN = (ROOT / "app" / "main.py").read_text(encoding="utf-8")


class ReturnCardAndProventosMenuTests(unittest.TestCase):
    def test_total_return_uses_same_value_as_total_patrimony_card(self):
        render_start = HTML.index("function render(){")
        render_end = HTML.index("\n\nconst rows=state.itens||[];", render_start)
        renderer = HTML[render_start:render_end]
        self.assertIn("const portfolioReturn=summaryApplied>0?(summaryCapitalGain/summaryApplied)*100:null;", renderer)
        self.assertIn("renderConsolidatedReturnSparklines(portfolioReturn, return12m);", renderer)

        helper_start = HTML.index("function renderConsolidatedReturnSparklines(")
        helper_end = HTML.index("\n}\n\nfunction operationQuantity", helper_start) + 2
        helper = HTML[helper_start:helper_end]
        self.assertIn("const lastTotal=totalReturn!==null&&totalReturn!==undefined&&Number.isFinite(Number(totalReturn))?Number(totalReturn):null;", helper)
        self.assertNotIn("const lastTotal=totalSeries.length?", helper)

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
