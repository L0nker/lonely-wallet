"""Regression coverage for Pluggy-only positions in the portfolio history."""
import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
COMPOSE = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")


class PluggyPortfolioHistoryTests(unittest.TestCase):
    def test_current_pluggy_positions_are_added_to_history_universe(self):
        self.assertIn("pluggy_current_map = {", SOURCE)
        self.assertIn("for row in current_positions:", SOURCE)
        self.assertIn("pluggy_snapshot_at", SOURCE)
        self.assertIn("'current_positions': [dict(row) for row in portfolio.get('itens', [])]", SOURCE)
        self.assertIn('current_positions=current_positions', SOURCE)

    def test_pluggy_snapshots_anchor_only_current_and_later_dates(self):
        self.assertIn("if cutoff >= anchor:", SOURCE)
        self.assertIn("holdings[ticker] = max(0.0, float(row.get('quantidade') or 0))", SOURCE)

    def test_brapi_free_plan_defaults_to_one_ticker_per_request(self):
        self.assertIn("BRAPI_MAX_TICKERS_PER_REQUEST', '1'", SOURCE)
        self.assertIn('BRAPI_MAX_TICKERS_PER_REQUEST: "1"', COMPOSE)

    def test_unknown_market_costs_are_not_filled_with_sale_price(self):
        helper = SOURCE[SOURCE.index('def _market_cost_basis_at_date('):SOURCE.index('def build_portfolio_performance(', SOURCE.index('def _market_cost_basis_at_date('))]
        self.assertIn('return None', helper)
        self.assertNotIn("return max(0.0, float(target.get('_price') or 0))", helper)
        self.assertIn('include_details=True', SOURCE)
        self.assertIn('capital_incompleto', SOURCE)

    def test_historical_price_cache_key_tracks_earliest_coverage_date(self):
        self.assertIn("cache_key = ('price-only-v3', coverage_start)", SOURCE)
        self.assertIn("min(first_dates).isoformat()", SOURCE)

    def test_pluggy_transaction_replay_is_idempotent_by_external_id(self):
        self.assertIn('transaction_id TEXT PRIMARY KEY', SOURCE)
        self.assertIn('ON CONFLICT(transaction_id) DO UPDATE', SOURCE)
        self.assertIn("'imported_operation','imported_provento','matched_existing','provento_duplicado_ignorado'", SOURCE)

    def test_dashboard_queues_historical_quotes_in_background(self):
        dashboard = SOURCE[SOURCE.index("@app.route('/api/dashboard')"):SOURCE.index("@app.route('/api/ativos/busca')")]
        self.assertIn('_queue_performance_history(performance_inputs)', dashboard)
        self.assertNotIn('fetch_portfolio_price_history(', dashboard)
        self.assertIn('performance_history_pending=performance_pending', dashboard)
        self.assertIn("@app.route('/api/performance-history')", SOURCE)
        self.assertIn("name='portfolio-performance-history', daemon=True", SOURCE)

    def test_pluggy_partial_sync_runs_are_persistently_tracked(self):
        self.assertIn('CREATE TABLE IF NOT EXISTS pluggy_sync_runs', SOURCE)
        self.assertIn("status='completed'", SOURCE)
        self.assertIn("status='failed'", SOURCE)
        self.assertIn('pluggy_imported_events', SOURCE)


if __name__ == "__main__":
    unittest.main()
