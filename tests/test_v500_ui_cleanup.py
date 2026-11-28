from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / 'app' / 'templates' / 'index.html').read_text(encoding='utf-8')


class V500UICleanupTests(unittest.TestCase):
    def test_proventos_sync_technical_status_is_not_rendered(self):
        self.assertNotIn('id="proventosSyncStatus"', HTML)
        self.assertNotIn('evento(s) novo(s)', HTML)
        self.assertNotIn('Detalhes técnicos (', HTML)
        self.assertNotIn('prov-sync-details', HTML)

    def test_api_usage_panel_and_frontend_loader_are_removed(self):
        self.assertNotIn('id="apiUsagePanel"', HTML)
        self.assertNotIn('function loadAPIUsage();', HTML)
        start = HTML.index('async function loadAPIProviders()')
        end = HTML.index('function renderAPIProviders()', start)
        self.assertNotIn('loadAPIUsage()', HTML[start:end])

    def test_pluggy_recommendation_is_an_inline_green_badge(self):
        self.assertIn('class="api-recommended-badge">(Recomendado)</span>', HTML)
        self.assertIn('.api-recommended-badge', HTML)
        self.assertNotIn('Recomendação: altamente indicada.', HTML)
        self.assertNotIn('class="api-recommendation"', HTML)
        self.assertIn('.api-provider-card.api-pluggy{border-color:var(--line)!important;box-shadow:none;outline:0}', HTML)

    def test_version_and_cache_are_kept_in_sync_for_current_release(self):
        version = (ROOT / 'VERSION').read_text(encoding='utf-8').strip()
        self.assertEqual(version, '6.1.18')
        main = (ROOT / 'app' / 'main.py').read_text(encoding='utf-8')
        self.assertIn('DASHBOARD_CACHE_FORMAT = 28', main)
        compose = (ROOT / 'docker-compose.yml').read_text(encoding='utf-8')
        self.assertIn('v=6.1.18', compose)


if __name__ == '__main__':
    unittest.main()
