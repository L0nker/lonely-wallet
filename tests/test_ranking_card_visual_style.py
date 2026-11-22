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

    def test_primary_values_are_slightly_larger_on_dashboard_cards(self):
        self.assertIn('.summary-investor10>.stat:not(.top-dividend-payers-card) .statvalue{font-size:25px}', HTML)
        self.assertIn('@media(max-width:560px){.summary-investor10>.stat:not(.top-dividend-payers-card) .statvalue{font-size:20px}}', HTML)


if __name__ == "__main__":
    unittest.main()
