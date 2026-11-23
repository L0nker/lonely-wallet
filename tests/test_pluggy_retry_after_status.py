"""Functional regression tests for Pluggy Retry-After cooldown handling."""
import ast
import math
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / 'app' / 'main.py').read_text(encoding='utf-8')
TREE = ast.parse(SOURCE)


def _function(name):
    return next(node for node in TREE.body if isinstance(node, ast.FunctionDef) and node.name == name)


class PluggyRetryAfterTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 23, 16, 0, 0, tzinfo=timezone.utc)
        self.cooldowns = []
        namespace = {
            'math': math,
            'datetime': datetime,
            'timezone': timezone,
            'parsedate_to_datetime': parsedate_to_datetime,
            '_provider_set_cooldown': lambda *args: self.cooldowns.append(args),
        }
        nodes = [_function('_retry_after_seconds'), _function('_pluggy_apply_retry_cooldown')]
        module = ast.Module(body=nodes, type_ignores=[])
        exec(compile(module, '<pluggy-retry-after>', 'exec'), namespace)
        self.parse = namespace['_retry_after_seconds']
        self.apply = namespace['_pluggy_apply_retry_cooldown']

    class Response:
        def __init__(self, status_code, retry_after=None):
            self.status_code = status_code
            self.headers = {}
            if retry_after is not None:
                self.headers['Retry-After'] = retry_after

    def test_408_honors_retry_after_seconds(self):
        self.assertEqual(self.apply(self.Response(408, '90')), 90)
        self.assertEqual(self.cooldowns[-1], ('pluggy', 90, 408, 'upstream_timeout'))

    def test_425_honors_retry_after_http_date(self):
        header = format_datetime(self.now + timedelta(seconds=180), usegmt=True)
        self.apply.__globals__['_retry_after_seconds'] = lambda value, **kwargs: self.parse(value, now=self.now, **kwargs)
        self.assertEqual(self.apply(self.Response(425, header)), 180)
        self.assertEqual(self.cooldowns[-1], ('pluggy', 180, 425, 'upstream_timeout'))

    def test_429_keeps_minimum_sixty_seconds(self):
        self.assertEqual(self.apply(self.Response(429, '20')), 60)
        self.assertEqual(self.cooldowns[-1], ('pluggy', 60, 429, 'rate_limited'))

    def test_503_honors_retry_after_and_uses_upstream_reason(self):
        self.assertEqual(self.apply(self.Response(503, '240')), 240)
        self.assertEqual(self.cooldowns[-1], ('pluggy', 240, 503, 'upstream_5xx'))

    def test_500_without_header_uses_default_thirty_seconds(self):
        self.assertEqual(self.apply(self.Response(500)), 30)
        self.assertEqual(self.cooldowns[-1], ('pluggy', 30, 500, 'upstream_5xx'))

    def test_408_without_header_uses_default_thirty_seconds(self):
        self.assertEqual(self.apply(self.Response(408)), 30)
        self.assertEqual(self.cooldowns[-1], ('pluggy', 30, 408, 'upstream_timeout'))

    def test_malformed_retry_after_uses_default(self):
        self.assertEqual(self.apply(self.Response(425, 'not-a-date')), 30)
        self.assertEqual(self.cooldowns[-1], ('pluggy', 30, 425, 'upstream_timeout'))

    def test_non_transient_client_error_does_not_set_cooldown(self):
        self.assertEqual(self.apply(self.Response(400, '120')), 0)
        self.assertEqual(self.cooldowns, [])

    def test_authentication_and_api_request_both_use_shared_handler(self):
        auth_source = ast.get_source_segment(SOURCE, _function('_pluggy_api_key'))
        request_source = ast.get_source_segment(SOURCE, _function('_pluggy_request'))
        self.assertIn('_pluggy_apply_retry_cooldown(response)', auth_source)
        self.assertIn('_pluggy_apply_retry_cooldown(result)', request_source)


if __name__ == '__main__':
    unittest.main()
