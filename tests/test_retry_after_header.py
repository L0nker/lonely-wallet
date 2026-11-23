"""Regression tests for Retry-After seconds and HTTP-date parsing."""
import ast
import math
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / 'app' / 'main.py').read_text(encoding='utf-8')
TREE = ast.parse(SOURCE)
NODE = next(node for node in TREE.body if isinstance(node, ast.FunctionDef) and node.name == '_retry_after_seconds')
MODULE = ast.Module(body=[NODE], type_ignores=[])
NAMESPACE = {
    'math': math,
    'datetime': datetime,
    'timezone': timezone,
    'parsedate_to_datetime': parsedate_to_datetime,
}
exec(compile(MODULE, '<retry-after-helper>', 'exec'), NAMESPACE)
retry_after_seconds = NAMESPACE['_retry_after_seconds']


class RetryAfterHeaderTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 23, 16, 0, 0, tzinfo=timezone.utc)

    def test_delay_seconds_integer(self):
        self.assertEqual(retry_after_seconds('120', minimum=1, now=self.now), 120)

    def test_delay_seconds_fraction_is_rounded_up(self):
        self.assertEqual(retry_after_seconds('2.1', minimum=1, now=self.now), 3)

    def test_http_date_is_parsed(self):
        header = format_datetime(self.now + timedelta(seconds=180), usegmt=True)
        self.assertEqual(retry_after_seconds(header, minimum=1, now=self.now), 180)

    def test_http_date_in_past_uses_minimum_cooldown(self):
        header = format_datetime(self.now - timedelta(seconds=15), usegmt=True)
        self.assertEqual(retry_after_seconds(header, default=60, minimum=60, now=self.now), 60)

    def test_malformed_header_uses_default(self):
        self.assertEqual(retry_after_seconds('tomorrow-ish', default=75, minimum=1, now=self.now), 75)

    def test_maximum_cooldown_is_enforced(self):
        self.assertEqual(retry_after_seconds('999999999999999999', minimum=1, maximum=2_147_483_647, now=self.now), 2_147_483_647)

    def test_missing_header_uses_default(self):
        self.assertEqual(retry_after_seconds(None, default=90, minimum=1, now=self.now), 90)

    def test_all_provider_429_paths_use_shared_parser(self):
        self.assertIn("_retry_after_seconds(retry_after", SOURCE)
        self.assertIn("_retry_after_seconds(response.headers.get('Retry-After')", SOURCE)
        self.assertIn("_retry_after_seconds(result.headers.get('Retry-After')", SOURCE)
        self.assertEqual(SOURCE.count('_retry_after_seconds('), 7)  # definition + provider response/cooldown call sites
        self.assertNotIn("int(float(response.headers.get('Retry-After')", SOURCE)
        self.assertNotIn("int(float(result.headers.get('Retry-After')", SOURCE)
        self.assertGreaterEqual(SOURCE.count("_retry_after_seconds(retry_header, default=30"), 3)

    def test_official_indicator_providers_share_retry_after_handling(self):
        self.assertIn("_provider_json_get(\n                'bcb'", SOURCE)
        self.assertIn("_provider_json_get(\n        'ibge'", SOURCE)
        provider_get = next(node for node in TREE.body if isinstance(node, ast.FunctionDef) and node.name == '_provider_get')
        provider_source = ast.get_source_segment(SOURCE, provider_get)
        self.assertIn('response.status_code in (408, 425) or response.status_code >= 500', provider_source)


if __name__ == '__main__':
    unittest.main()
