import unittest
from datetime import date

from scripts.portfolio_metrics import calculate_position_performance, is_paid_provento


class IndividualReturnTests(unittest.TestCase):
    def test_variation_is_price_difference_from_average_cost(self):
        result = calculate_position_performance(50, 10, 12, 25)
        self.assertEqual(result['variation_value'], 100.00)
        self.assertEqual(result['variation_pct'], 20.00)

    def test_profitability_adds_only_paid_income_total_supplied_by_caller(self):
        result = calculate_position_performance(50, 10, 12, 25)
        self.assertEqual(result['profitability_value'], 125.00)
        self.assertEqual(result['profitability_pct'], 25.00)

    def test_negative_price_variation_can_be_offset_by_paid_dividends(self):
        result = calculate_position_performance(10, 100, 90, 150)
        self.assertEqual(result['variation_value'], -100.00)
        self.assertEqual(result['variation_pct'], -10.00)
        self.assertEqual(result['profitability_value'], 50.00)
        self.assertEqual(result['profitability_pct'], 5.00)

    def test_missing_quote_does_not_invent_zero_variation(self):
        result = calculate_position_performance(10, 100, 100, 20, quote_available=False)
        self.assertIsNone(result['variation_pct'])
        self.assertIsNone(result['profitability_pct'])

    def test_only_explicitly_paid_events_with_non_future_date_are_eligible(self):
        today = date(2026, 11, 22)
        self.assertTrue(is_paid_provento('Pago', '2026-11-20', today))
        self.assertTrue(is_paid_provento('Pago', '2026-11-22', today))
        self.assertFalse(is_paid_provento('A receber', '2026-11-20', today))
        self.assertFalse(is_paid_provento('A receber', '2026-12-10', today))
        self.assertFalse(is_paid_provento('Pago', '2026-12-10', today))
        self.assertFalse(is_paid_provento('Pago', '', today))


if __name__ == '__main__':
    unittest.main()
