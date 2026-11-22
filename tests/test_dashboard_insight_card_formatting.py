from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / 'app' / 'templates' / 'index.html').read_text(encoding='utf-8')


class DashboardInsightCardFormattingTests(unittest.TestCase):
    def test_insight_titles_match_stat_label_typography(self):
        self.assertIn('.position-insights .insight-title{', HTML)
        block = HTML.split('.position-insights .insight-title{', 1)[1].split('}', 1)[0]
        self.assertIn('color:var(--muted)', block)
        self.assertIn('font-size:12px', block)
        self.assertIn('font-weight:400', block)
        self.assertIn('min-height:18px', block)

    def test_all_insight_cards_use_stat_card_padding(self):
        self.assertIn('.position-insights > .insight-card{\n  padding:18px 20px;\n}', HTML)

    def test_latest_assets_card_has_consistent_list_spacing(self):
        self.assertIn('.position-insights .insight-recent-card .insight-recent-list{\n  margin-top:8px;', HTML)
        self.assertIn('.position-insights .insight-recent-card .insight-recent-row{\n  min-height:40px;\n  padding:8px 0;', HTML)

    def test_target_cards_are_all_present(self):
        for title in ('Destaques da carteira', 'Posições no lucro', 'Oportunidades de compra'):
            self.assertIn(f'<div class="insight-title">{title}', HTML)


if __name__ == '__main__':
    unittest.main()
