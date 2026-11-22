from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "app" / "templates" / "index.html").read_text(encoding="utf-8")


class RankingCardVisualStyleTests(unittest.TestCase):
    def test_ranking_uses_same_stat_header_pattern_as_neighbor_cards(self):
        start = HTML.index("function render(){")
        end = HTML.index("\n\nconst rows=state.itens||[];", start)
        renderer = HTML[start:end]
        self.assertIn('class="card stat top-dividend-payers-card"', renderer)
        self.assertIn('class="statlabel-row">Ranking de proventos', renderer)
        self.assertIn('class="info-tip"', renderer)

    def test_latest_assets_card_has_matching_stat_label_and_info_tooltip(self):
        self.assertIn('Últimos ativos lançados<span class="info-tip" tabindex="0" aria-label="Sobre os últimos ativos lançados"', HTML)
        self.assertIn('Mostra os dois ativos com lançamentos mais recentes na carteira', HTML)
        self.assertIn('.insight-recent-card .insight-title{font-size:12px;font-weight:400;color:var(--muted);}', HTML)
        self.assertIn('.slice(0,2));\n const recentDateLabel', HTML)

    def test_ranking_card_shows_only_two_items(self):
        self.assertIn('Os dois ativos com maior total acumulado', HTML)
        helper_start = HTML.index("function getTopPaidDividendPayers(")
        helper_end = HTML.index("\n}\n\nfunction render(){", helper_start) + 2
        helper = HTML[helper_start:helper_end]
        self.assertIn(".slice(0,2)", helper)

    def test_primary_values_are_slightly_larger_on_dashboard_cards(self):
        self.assertIn('.summary-investor10>.stat:not(.top-dividend-payers-card) .statvalue{font-size:25px}', HTML)
        self.assertIn('@media(max-width:560px){.summary-investor10>.stat:not(.top-dividend-payers-card) .statvalue{font-size:20px}}', HTML)


if __name__ == "__main__":
    unittest.main()
