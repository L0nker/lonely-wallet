from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / 'app' / 'templates' / 'index.html').read_text(encoding='utf-8')


class EvolutionChartFinancialScopeTests(unittest.TestCase):
    def test_evolution_chart_uses_capital_gain_only(self):
        start = HTML.index('function renderEvolutionChart(')
        end = HTML.index('function getDistinctChartColors(', start)
        renderer = HTML[start:end]

        self.assertNotIn('paidDividendCumulativeByMonth', renderer)
        self.assertNotIn('proventos_recebidos_acumulados', renderer)
        self.assertNotIn('Patrimônio + proventos', renderer)
        self.assertIn("const gains=visibleHistory.map(row=>row.mes===currentMonthKey?currentCapitalGain:Number(row.ganho||0));", renderer)
        self.assertIn("{label:'Ganho de Capital'", renderer)
        self.assertIn("{label:'Valor aplicado'", renderer)
        self.assertIn('Patrimônio: ${brl(patrimony[index]||0)}', renderer)
        self.assertIn('Number(state.resumo?.lucro||0)', renderer)
        self.assertIn('Patrimônio: ${brl(fallbackPatrimony[index]||0)}', renderer)

    def test_fallback_chart_does_not_add_paid_dividends(self):
        start = HTML.index('function renderEvolutionChart(')
        end = HTML.index('function getDistinctChartColors(', start)
        renderer = HTML[start:end]

        self.assertNotIn('paidNow', renderer)
        self.assertIn("const gain=selectedType==='todos'", renderer)
        self.assertIn('const fallbackPatrimony=cumulative.map', renderer)

    def test_price_update_tooltip_keeps_hover_but_uses_normal_cursor(self):
        css = re.search(r"\.price-update-tooltip-trigger\{([^}]*)\}", HTML)
        self.assertIsNotNone(css)
        self.assertIn('cursor:default', css.group(1))
        self.assertNotIn('cursor:help', css.group(1))
        self.assertIn('onmouseenter="showVariationMoneyTooltip(event,this)"', HTML)


if __name__ == '__main__':
    unittest.main()
