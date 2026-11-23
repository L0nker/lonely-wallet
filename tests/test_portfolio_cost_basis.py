"""Regression tests for the portfolio evolution chart's invested-capital basis."""
import ast
from datetime import date
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)
HELPER_NODE = next(
    node for node in TREE.body
    if isinstance(node, ast.FunctionDef) and node.name == "_market_cost_basis_at_date"
)
NAMESPACE = {"date": date}
exec(compile(ast.Module(body=[HELPER_NODE], type_ignores=[]), str(ROOT / "app" / "main.py"), "exec"), NAMESPACE)
market_cost_basis = NAMESPACE["_market_cost_basis_at_date"]


def operation(op_id, day, side, qty, price, costs=0.0, ticker="ABC3"):
    return {
        "id": op_id,
        "_date": date.fromisoformat(day),
        "_ticker": ticker,
        "lado": side,
        "_quantity": float(qty),
        "_price": float(price),
        "_costs": float(costs),
    }


class PortfolioCostBasisTests(unittest.TestCase):
    def test_sale_reduces_invested_capital_by_average_cost_not_sale_proceeds(self):
        ops = [
            operation(1, "2026-01-10", "compra", 10, 10),
            operation(2, "2026-02-10", "compra", 10, 20),
            operation(3, "2026-03-10", "venda", 5, 30),
        ]
        actual = market_cost_basis(ops, {}, None, date(2026, 3, 31), {"ABC3"})
        self.assertAlmostEqual(actual, 225.0, places=2)

    def test_snapshot_anchor_reconstructs_prior_basis_and_sales(self):
        ops = [
            operation(1, "2026-05-15", "compra", 50, 12),
            operation(2, "2026-08-10", "venda", 20, 14),
        ]
        snapshot = {"ABC3": {"quantidade": 80, "preco_medio": 10}}
        before_buy = market_cost_basis(ops, snapshot, date(2026, 10, 31), date(2026, 5, 1), {"ABC3"})
        after_buy = market_cost_basis(ops, snapshot, date(2026, 10, 31), date(2026, 7, 1), {"ABC3"})
        at_snapshot = market_cost_basis(ops, snapshot, date(2026, 10, 31), date(2026, 10, 31), {"ABC3"})
        self.assertAlmostEqual(before_buy, 400.0, places=2)
        self.assertAlmostEqual(after_buy, 1000.0, places=2)
        self.assertAlmostEqual(at_snapshot, 800.0, places=2)

    def test_reversing_purchase_removes_actual_purchase_cost_not_later_average(self):
        ops = [
            operation(1, "2026-05-15", "compra", 50, 12),
            operation(2, "2026-08-10", "venda", 20, 14),
        ]
        snapshot = {"ABC3": {"quantidade": 80, "preco_medio": 10}}
        actual = market_cost_basis(ops, snapshot, date(2026, 10, 31), date(2026, 5, 1), {"ABC3"})
        self.assertAlmostEqual(actual, 400.0, places=2)

    def test_post_snapshot_buy_and_sale_preserve_average_cost_basis(self):
        ops = [
            operation(1, "2026-11-05", "compra", 10, 15),
            operation(2, "2026-11-12", "venda", 5, 20),
        ]
        snapshot = {"ABC3": {"quantidade": 80, "preco_medio": 10}}
        actual = market_cost_basis(ops, snapshot, date(2026, 10, 31), date(2026, 11, 30), {"ABC3"})
        self.assertAlmostEqual(actual, 897.222222, places=2)

    def test_fully_sold_position_has_zero_remaining_cost_basis(self):
        ops = [
            operation(1, "2026-01-10", "compra", 10, 10),
            operation(2, "2026-02-10", "venda", 10, 20),
        ]
        actual = market_cost_basis(ops, {}, None, date(2026, 2, 28), {"ABC3"})
        self.assertAlmostEqual(actual, 0.0, places=2)

    def test_fixed_income_ticker_is_excluded_from_market_basis(self):
        ops = [operation(1, "2026-01-10", "compra", 10, 10)]
        actual = market_cost_basis(ops, {}, None, date(2026, 1, 31), set())
        self.assertEqual(actual, 0.0)

    def test_sale_without_known_acquisition_cost_is_marked_unknown_not_sale_price(self):
        ops = [operation(1, "2026-02-10", "venda", 5, 30)]
        result = market_cost_basis(ops, {}, None, date(2026, 2, 28), {"ABC3"}, include_details=True)
        self.assertEqual(result["basis"], 0.0)
        self.assertEqual(result["basis_by_ticker"]["ABC3"], 0.0)
        self.assertEqual(result["unknown_tickers"], ["ABC3"])

    def test_sale_larger_than_known_position_marks_cost_incomplete(self):
        ops = [
            operation(1, "2026-01-10", "compra", 3, 10),
            operation(2, "2026-02-10", "venda", 5, 30),
        ]
        result = market_cost_basis(ops, {}, None, date(2026, 2, 28), {"ABC3"}, include_details=True)
        self.assertEqual(result["unknown_tickers"], ["ABC3"])
        self.assertAlmostEqual(result["basis"], 0.0, places=2)



if __name__ == "__main__":
    unittest.main()
