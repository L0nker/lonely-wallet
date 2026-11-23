"""Ensure request counters remain informational and do not impose local quotas."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ProviderQuotaRemovalTests(unittest.TestCase):
    def test_local_quota_environment_variables_are_not_configured(self):
        main = (ROOT / 'app' / 'main.py').read_text(encoding='utf-8')
        compose = (ROOT / 'docker-compose.yml').read_text(encoding='utf-8')
        for name in ('BRAPI_LOCAL_30D_BUDGET', 'HGBRASIL_LOCAL_24H_BUDGET', 'DADOSB3_LOCAL_24H_BUDGET'):
            self.assertNotIn(name, main)
            self.assertNotIn(name, compose)

    def test_provider_get_does_not_compare_usage_to_local_budgets(self):
        main = (ROOT / 'app' / 'main.py').read_text(encoding='utf-8')
        provider_get = main[main.index('def _provider_get('):main.index('def _brapi_market_cache_ttl(')]
        self.assertNotIn('local_budget', provider_get)
        self.assertNotIn('Orçamento local', provider_get)
        self.assertIn("if provider == 'pluggy' and not request_id", provider_get)

    def test_usage_panel_is_informational_only(self):
        template = (ROOT / 'app' / 'templates' / 'index.html').read_text(encoding='utf-8')
        self.assertIn('Sem cotas artificiais locais.', template)
        self.assertNotIn('Orçamento interno brapi', template)

    def test_disabled_internal_budget_is_not_rendered_in_dashboard(self):
        template = (ROOT / 'app' / 'templates' / 'index.html').read_text(encoding='utf-8')
        self.assertIn('const budgetEnabled = item.budget_enabled === true;', template)
        self.assertIn("const budgetMetric = budgetEnabled ?", template)
        self.assertNotIn('<strong>Desativado</strong>', template)

    def test_internal_budget_metric_only_appears_when_backend_enables_it(self):
        template = (ROOT / 'app' / 'templates' / 'index.html').read_text(encoding='utf-8')
        self.assertIn('const budgetMetric = budgetEnabled ?', template)
        self.assertIn('api-usage-grid${budgetEnabled?', template)


if __name__ == '__main__':
    unittest.main()
