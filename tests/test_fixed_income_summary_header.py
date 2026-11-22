from pathlib import Path
import unittest

HTML = (Path(__file__).resolve().parents[1] / "app/templates/index.html").read_text(encoding="utf-8")

class FixedIncomeSummaryHeaderTests(unittest.TestCase):
    def test_summary_uses_all_category_rows(self):
        self.assertIn("getFixedIncomeDiscountSummary(allCategoryItems)", HTML)
        self.assertIn("rows.reduce((sum,item)=>sum+Number(item.saldo_com_descontos),0)", HTML)

    def test_header_metric_is_immediately_after_gross_total(self):
        marker = '<span class="asset-category-metric">Valor total<b>${brl(total)}</b></span>${fixedIncomeDiscountMetric}'
        self.assertIn(marker, HTML)

    def test_fixed_income_header_class_is_applied(self):
        self.assertIn("' fixed-income-category-head'", HTML)
        self.assertIn(".asset-category-head.fixed-income-category-head", HTML)

    def test_unknown_line_displays_placeholder_instead_of_partial_sum(self):
        self.assertIn("value!==null&&value!==undefined&&value!==''&&Number.isFinite(Number(value))", HTML)
        self.assertIn("fixedIncomeDiscountSummary.complete?brl(fixedIncomeDiscountSummary.total):'—'", HTML)

if __name__ == "__main__":
    unittest.main()
