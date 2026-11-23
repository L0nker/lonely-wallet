"""Healthcheck strict mode tests without requiring Flask at test collection."""
from __future__ import annotations

import ast
import copy
import importlib.util
import json
import io
import contextlib
from urllib.error import HTTPError
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / 'app' / 'main.py').read_text(encoding='utf-8')
TREE = ast.parse(SOURCE)
HEALTH = next(node for node in TREE.body if isinstance(node, ast.FunctionDef) and node.name == 'health')


class HealthcheckStrictModeTests(unittest.TestCase):
    def run_health(self, strict, chart_status):
        namespace = {
            'db': lambda: SimpleNamespace(execute=lambda *_: SimpleNamespace(fetchone=lambda: (1,))),
            'token_db': lambda: SimpleNamespace(execute=lambda *_: SimpleNamespace(fetchone=lambda: (1,))),
            '_chart_assets_status': lambda: {'status': chart_status},
            'request': SimpleNamespace(args={'strict': '1' if strict else ''}),
            'APP_VERSION': '4.2.49',
            'jsonify': lambda **kwargs: kwargs,
            'app': SimpleNamespace(logger=SimpleNamespace(exception=lambda *_: None)),
        }
        function = copy.deepcopy(HEALTH)
        function.decorator_list = []
        exec(compile(ast.Module(body=[function], type_ignores=[]), '<healthcheck>', 'exec'), namespace)
        return namespace['health']()

    def test_normal_endpoint_reports_degraded_without_breaking_usable_fallback(self):
        payload, status = self.run_health(False, 'degraded')
        self.assertEqual(status, 200)
        self.assertEqual(payload['status'], 'degraded')
        self.assertTrue(payload['ok'])

    def test_strict_endpoint_returns_503_for_degraded_renderer(self):
        payload, status = self.run_health(True, 'degraded')
        self.assertEqual(status, 503)
        self.assertEqual(payload['status'], 'degraded')
        self.assertFalse(payload['ok'])

    def test_strict_endpoint_returns_200_for_healthy_chartjs(self):
        payload, status = self.run_health(True, 'ready')
        self.assertEqual(status, 200)
        self.assertEqual(payload['status'], 'healthy')
        self.assertTrue(payload['ok'])

    def test_compose_healthcheck_uses_service_availability_endpoint(self):
        compose = (ROOT / 'docker-compose.yml').read_text(encoding='utf-8')
        self.assertIn('/srv/scripts/healthcheck.py', compose)
        self.assertIn("URL = 'http://127.0.0.1:8080/api/health'", (ROOT / 'scripts' / 'healthcheck.py').read_text(encoding='utf-8'))

    def test_docker_health_script_logs_degraded_renderer_but_accepts_usable_fallback(self):
        module_path = ROOT / 'scripts' / 'healthcheck.py'
        spec = importlib.util.spec_from_file_location('lonely_healthcheck', module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        body = json.dumps({
            'status': 'degraded', 'charts': {
                'engine': 'fallback', 'fallback_available': True, 'warning': 'Chart.js ausente; fallback ativo.'
            }
        }).encode('utf-8')
        class Response(io.BytesIO):
            status = 200

        response = Response(body)
        output = io.StringIO()
        with patch.object(module, 'urlopen', return_value=response), contextlib.redirect_stdout(output):
            self.assertEqual(module.main(), 0)
        self.assertIn('degraded', output.getvalue())
        self.assertIn('fallback', output.getvalue())

    def test_docker_health_script_accepts_fully_healthy_renderer(self):
        module_path = ROOT / 'scripts' / 'healthcheck.py'
        spec = importlib.util.spec_from_file_location('lonely_healthcheck_healthy', module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        class Response(io.BytesIO):
            status = 200

        response = Response(json.dumps({
            'status': 'healthy', 'charts': {'engine': 'chartjs', 'warning': None}
        }).encode('utf-8'))
        output = io.StringIO()
        with patch.object(module, 'urlopen', return_value=response), contextlib.redirect_stdout(output):
            self.assertEqual(module.main(), 0)
        self.assertIn('healthy', output.getvalue())

    def test_docker_health_script_rejects_missing_fallback(self):
        module_path = ROOT / 'scripts' / 'healthcheck.py'
        spec = importlib.util.spec_from_file_location('lonely_healthcheck_missing_fallback', module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        class Response(io.BytesIO):
            status = 200

        response = Response(json.dumps({
            'status': 'unhealthy', 'charts': {'engine': 'unavailable', 'fallback_available': False}
        }).encode('utf-8'))
        output = io.StringIO()
        with patch.object(module, 'urlopen', return_value=response), contextlib.redirect_stdout(output):
            self.assertEqual(module.main(), 1)
        self.assertIn('unhealthy', output.getvalue())


if __name__ == '__main__':
    unittest.main()
