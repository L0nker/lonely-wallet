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
        self.assertIn('const currentCapitalGain=sumAssetCapitalGain(selectedItems);', renderer)
        self.assertIn('const gain=sumAssetCapitalGain(selectedItems);', renderer)
        self.assertNotIn('state.resumo?.lucro', renderer)
        self.assertIn("{label:'Ganho de Capital'", renderer)
        self.assertIn("{label:'Valor aplicado'", renderer)
        self.assertIn('Patrimônio: ${brl(patrimony[index]||0)}', renderer)
        self.assertIn('Patrimônio: ${brl(fallbackPatrimony[index]||0)}', renderer)

    def test_capital_gain_helper_uses_only_asset_price_variation(self):
        start = HTML.index('function sumAssetCapitalGain(')
        end = HTML.index('\n}', start) + 2
        helper = HTML[start:end]
        self.assertIn('item?.variacao_valor??item?.lucro', helper)
        self.assertNotIn('provento', helper.lower())
        self.assertNotIn('dividend', helper.lower())

    def test_summary_patrimony_is_applied_plus_capital_gain(self):
        start = HTML.index('function render(){')
        end = HTML.index('\n\nconst rows=state.itens||[];', start)
        renderer = HTML[start:end]
        self.assertIn('const summaryApplied=Number(r.investido||0);', renderer)
        self.assertIn('const summaryCapitalGain=sumAssetCapitalGain(state.itens||[]);', renderer)
        self.assertIn('const summaryPatrimony=Number((summaryApplied+summaryCapitalGain).toFixed(2));', renderer)
        self.assertIn('(summaryCapitalGain/summaryApplied)*100', renderer)
        self.assertIn('<div class="statdetail"><span>Ganho de capital</span><b>${brl(summaryCapitalGain)}</b></div>', renderer)
        self.assertNotIn("const summaryPatrimony=latestEvolution?Number(latestEvolution.patrimonio||0)", renderer)

    def test_fallback_chart_does_not_add_paid_dividends(self):
        start = HTML.index('function renderEvolutionChart(')
        end = HTML.index('function getDistinctChartColors(', start)
        renderer = HTML[start:end]
        self.assertNotIn('paidNow', renderer)
        self.assertIn('const gains=monthKeys.map', renderer)
        self.assertIn('const fallbackPatrimony=cumulative.map', renderer)

    def test_price_update_tooltip_keeps_hover_but_uses_normal_cursor(self):
        css = re.search(r"\.price-update-tooltip-trigger\{([^}]*)\}", HTML)
        self.assertIsNotNone(css)
        self.assertIn('cursor:default', css.group(1))
        self.assertNotIn('cursor:help', css.group(1))
        self.assertIn('onmouseenter="showVariationMoneyTooltip(event,this)"', HTML)


if __name__ == '__main__':
    unittest.main()
