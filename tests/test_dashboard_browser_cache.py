"""Static regression checks for the dashboard's fast reload and version metadata."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = (ROOT / 'app' / 'templates' / 'index.html').read_text(encoding='utf-8')
MAIN = (ROOT / 'app' / 'main.py').read_text(encoding='utf-8')
VERSION = (ROOT / 'VERSION').read_text(encoding='utf-8').strip()


class DashboardBrowserCacheTests(unittest.TestCase):
    def test_dashboard_snapshot_uses_indexeddb_and_tracks_app_version(self):
        self.assertIn("window.indexedDB.open(DASHBOARD_BROWSER_CACHE_DB,1)", TEMPLATE)
        self.assertIn("const DASHBOARD_BROWSER_CACHE_SCHEMA='dashboard-v1'", TEMPLATE)
        self.assertIn("const DASHBOARD_BROWSER_APP_VERSION='{{ app_version }}'", TEMPLATE)
        self.assertIn('record.schema===DASHBOARD_BROWSER_CACHE_SCHEMA', TEMPLATE)
        self.assertIn('writeDashboardBrowserCache(state)', TEMPLATE)
        self.assertIn('restoreDashboardBrowserCache()', TEMPLATE)
        self.assertIn("loader.classList.add('done')", TEMPLATE)
        self.assertIn("await restoreDashboardBrowserCache();", TEMPLATE)

    def test_background_refresh_cannot_cover_a_visible_dashboard(self):
        wait_func = re.search(r'function setDashboardDataWait\(message\)\{(.*?)\n\}', TEMPLATE, re.S)
        self.assertIsNotNone(wait_func)
        self.assertRegex(wait_func.group(1), r'if\(dashboardHasVisibleState\)return;')
        self.assertIn('scheduleDashboardFullRetry(showWait&&!dashboardHasVisibleState)', TEMPLATE)
        self.assertIn("refreshDashboardInBackground(!dashboardHasVisibleState)", TEMPLATE)

    def test_root_html_is_not_cached_and_footer_uses_version_file(self):
        self.assertIn("response = make_response(render_template('index.html', app_version=application_version()))", MAIN)
        self.assertIn("response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'", MAIN)
        self.assertIn('Lonely Wallet v{{ app_version }}', TEMPLATE)
        self.assertIn("version=application_version()", MAIN)
        self.assertEqual(VERSION, '4.2.19')


if __name__ == '__main__':
    unittest.main()
