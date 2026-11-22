import unittest
from scripts.quantity_ledger import quantity_ledger_breaks_position


class QuantityLedgerTests(unittest.TestCase):
    def test_retroactive_sale_cannot_break_later_sale(self):
        snapshots = []
        operations = [
            {'id': 1, 'lado': 'compra', 'quantidade': 10, 'data': '2026-01-10'},
            {'id': 2, 'lado': 'venda', 'quantidade': 8, 'data': '2026-03-02'},
        ]
        proposed = {'lado': 'venda', 'quantidade': 5, 'data': '2026-02-01'}
        self.assertTrue(quantity_ledger_breaks_position(snapshots, operations, proposed))

    def test_validates_operations_after_multiple_future_snapshots(self):
        snapshots = [
            {'data_base': '2026-02-01', 'quantidade': 10},
            {'data_base': '2026-02-28', 'quantidade': 5},
        ]
        operations = [
            {'id': 1, 'lado': 'venda', 'quantidade': 8, 'data': '2026-03-02'},
        ]
        proposed = {'lado': 'venda', 'quantidade': 6, 'data': '2026-02-15'}
        self.assertTrue(quantity_ledger_breaks_position(snapshots, operations, proposed))

    def test_editing_purchase_to_later_date_checks_intervening_sales(self):
        snapshots = []
        operations = [
            {'id': 1, 'lado': 'compra', 'quantidade': 10, 'data': '2026-01-10'},
            {'id': 2, 'lado': 'venda', 'quantidade': 8, 'data': '2026-02-10'},
        ]
        proposed = {'lado': 'compra', 'quantidade': 10, 'data': '2026-03-10'}
        self.assertTrue(quantity_ledger_breaks_position(snapshots, operations, proposed, exclude_id=1))

    def test_deleting_purchase_checks_future_sales(self):
        snapshots = []
        operations = [
            {'id': 1, 'lado': 'compra', 'quantidade': 10, 'data': '2026-01-10'},
            {'id': 2, 'lado': 'venda', 'quantidade': 8, 'data': '2026-02-10'},
        ]
        self.assertTrue(quantity_ledger_breaks_position(snapshots, operations, exclude_id=1))

    def test_snapshot_resets_quantity_before_later_operations(self):
        snapshots = [
            {'data_base': '2026-02-01', 'quantidade': 10},
            {'data_base': '2026-02-28', 'quantidade': 12},
        ]
        operations = [
            {'id': 1, 'lado': 'venda', 'quantidade': 8, 'data': '2026-02-15'},
            {'id': 2, 'lado': 'venda', 'quantidade': 10, 'data': '2026-03-02'},
        ]
        proposed = {'lado': 'compra', 'quantidade': 1, 'data': '2026-02-10'}
        self.assertFalse(quantity_ledger_breaks_position(snapshots, operations, proposed))


if __name__ == '__main__':
    unittest.main()
