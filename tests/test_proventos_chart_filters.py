from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / 'app' / 'templates' / 'index.html').read_text(encoding='utf-8')


class ProventosChartFiltersTests(unittest.TestCase):
    def test_chart_aggregates_filtered_rows(self):
        start = HTML.index('function renderProventosDashboard()')
        end = HTML.index('\nfunction ', start + 12)
        renderer = HTML[start:end]
        self.assertIn('const chartRows=filterProventoRowsByAsset(rows,selectedType,selectedAsset);', renderer)
        self.assertIn('const allChartRows=chartRows.filter(x=>{', renderer)
        self.assertNotIn('const allChartRows=rows.filter(x=>{', renderer)

    def test_type_options_are_limited_to_income_event_categories(self):
        start = HTML.index('function renderProventosDashboard()')
        end = HTML.index('\nfunction ', start + 12)
        renderer = HTML[start:end]
        self.assertIn('const typeMap=new Map();', renderer)
        self.assertIn('rows.forEach(x=>{const kind=getProventoAssetType(x);', renderer)
        self.assertNotIn('new Map((state.itens||[]).map', renderer)

    def test_fixed_income_events_are_excluded_from_page_rows_and_summary(self):
        start = HTML.index('function renderProventosDashboard()')
        end = HTML.index('\nfunction ', start + 12)
        renderer = HTML[start:end]
        self.assertIn('const rows=filterProventoRowsByAsset([...(d.recebidos||[])', renderer)
        self.assertIn('const receivedRows=filterProventoRowsByAsset(d.recebidos||[]);', renderer)

    def test_distribution_and_month_detail_exclude_fixed_income(self):
        self.assertIn("!isFixedIncomeProventoType(getProventoAssetType(x))", HTML)
        self.assertIn("!isFixedIncomeProventoType(kind)&&bucket===key", HTML)


if __name__ == '__main__':
    unittest.main()
