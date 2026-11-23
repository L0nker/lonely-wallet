"""BCB/IBGE must go through the shared provider cooldown and Retry-After path."""
from __future__ import annotations

import ast
import importlib.util
import json
import requests
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / 'app' / 'main.py').read_text(encoding='utf-8')
TREE = ast.parse(SOURCE)
NODE = next(node for node in TREE.body if isinstance(node, ast.FunctionDef) and node.name == '_provider_json_get')


def load_helper(provider_get, cache_delete, sleep=time.sleep):
    namespace = {
        '_api_response_cache_key': lambda provider, url, params, headers: f'{provider}|{url}',
        '_provider_get': provider_get,
        '_api_cache_delete': cache_delete,
        'time': type('Clock', (), {'sleep': staticmethod(sleep)})(),
    }
    exec(compile(ast.Module(body=[NODE], type_ignores=[]), '<provider-json-helper>', 'exec'), namespace)
    return namespace['_provider_json_get']


def make_response(status, content=b'[]', headers=None):
    response = requests.Response()
    response.status_code = status
    response._content = content
    response._content_consumed = True
    response.headers.update(headers or {})
    response.url = 'https://example.invalid/test'
    return response


class OfficialIndicatorRetryAfterTests(unittest.TestCase):
    def test_bcb_and_ibge_use_shared_provider_layer_not_direct_requests(self):
        sgs = next(node for node in TREE.body if isinstance(node, ast.FunctionDef) and node.name == '_sgs_series')
        ibge = next(node for node in TREE.body if isinstance(node, ast.FunctionDef) and node.name == '_benchmark_monthly_ibge_ipca')
        sgs_source = ast.get_source_segment(SOURCE, sgs)
        ibge_source = ast.get_source_segment(SOURCE, ibge)
        self.assertIn("_provider_json_get(\n                'bcb'", sgs_source)
        self.assertIn("_provider_json_get(\n        'ibge'", ibge_source)
        self.assertNotIn('requests.get(', sgs_source)
        self.assertNotIn('requests.get(', ibge_source)

    def test_provider_layer_persists_cooldowns_for_408_425_and_5xx(self):
        provider_get = next(node for node in TREE.body if isinstance(node, ast.FunctionDef) and node.name == '_provider_get')
        source = ast.get_source_segment(SOURCE, provider_get)
        self.assertIn('response.status_code in (408, 425) or response.status_code >= 500', source)
        self.assertIn("response.status_code == 429", source)
        self.assertIn('_retry_after_seconds', source)
        self.assertIn('_provider_set_cooldown', source)
        # For HTTP 429, the cooldown starts when the response is received,
        # not from the timestamp captured before a potentially slow request.
        self.assertIn('(provider, time.time() + cooldown_seconds, 429, message[:300])', source)

    def test_invalid_json_is_invalidated_and_retried(self):
        responses = [make_response(200, b''), make_response(200, b'[{"data":1}]')]
        provider_get = Mock(side_effect=responses)
        deleted = []
        sleeps = []
        helper = load_helper(provider_get, deleted.append, sleeps.append)
        payload, response = helper('bcb', 'https://bcb.invalid/sgs', expected_type=list, invalid_retries=2)
        self.assertEqual(payload, [{'data': 1}])
        self.assertEqual(provider_get.call_count, 2)
        self.assertEqual(deleted, ['bcb|https://bcb.invalid/sgs'])
        self.assertEqual(sleeps, [0.35])
        self.assertEqual(response.status_code, 200)

    def test_rate_limit_is_not_retried_before_provider_retry_after(self):
        response = make_response(429, b'{"error":"limited"}', {'Retry-After': '3600'})
        provider_get = Mock(return_value=response)
        helper = load_helper(provider_get, Mock(), Mock())
        with self.assertRaises(requests.HTTPError):
            helper('ibge', 'https://ibge.invalid/sidra', expected_type=list, invalid_retries=2)
        self.assertEqual(provider_get.call_count, 1)

    def test_service_unavailable_http_date_is_not_retried_immediately(self):
        from datetime import datetime, timedelta, timezone
        from email.utils import format_datetime
        retry_at = datetime.now(timezone.utc) + timedelta(hours=1)
        response = make_response(503, b'{"error":"maintenance"}', {
            'Retry-After': format_datetime(retry_at, usegmt=True)
        })
        provider_get = Mock(return_value=response)
        helper = load_helper(provider_get, Mock(), Mock())
        with self.assertRaises(requests.HTTPError):
            helper('bcb', 'https://bcb.invalid/sgs', expected_type=list, invalid_retries=2)
        self.assertEqual(provider_get.call_count, 1)

    def test_unexpected_json_shape_is_invalidated_and_retried(self):
        responses = [make_response(200, b'{"erro":true}'), make_response(200, b'[]')]
        provider_get = Mock(side_effect=responses)
        deleted = []
        helper = load_helper(provider_get, deleted.append, Mock())
        payload, _ = helper('bcb', 'https://bcb.invalid/sgs', expected_type=list)
        self.assertEqual(payload, [])
        self.assertEqual(provider_get.call_count, 2)
        self.assertEqual(len(deleted), 1)


if __name__ == '__main__':
    unittest.main()
