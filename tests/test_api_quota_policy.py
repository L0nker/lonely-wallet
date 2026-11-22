"""Dependency-free static guardrails for the free-tier API quota policy."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
MAIN = (ROOT / 'app' / 'main.py').read_text(encoding='utf-8')
README = (ROOT / 'README.md').read_text(encoding='utf-8')


def _function_body(source, name, next_def=None):
    start = source.index(f'def {name}(')
    if next_def:
        end = source.index(f'\ndef {next_def}(', start)
    else:
        match = re.search(r'\n(?:def |@app\.route)', source[start + 1:])
        end = start + 1 + match.start() if match else len(source)
    return source[start:end]


def test_free_plan_defaults_to_one_symbol_and_reserves_ten_percent():
    assert "BRAPI_ROLLING_30D_BUDGET', '13500'" in MAIN
    assert "BRAPI_MAX_TICKERS_PER_REQUEST', '1'" in MAIN


def test_dashboard_quote_refresh_does_not_bypass_persistent_cache():
    body = _function_body(MAIN, 'cotacao', '_fallback_market_quote')
    assert 'bypass_cache=False' in body
    assert 'bypass_cache=force_refresh' not in body


def test_forced_provento_sync_keeps_http_response_cache():
    body = _function_body(MAIN, '_fetch_brapi_dividend_events', 'sync_brapi_proventos')
    assert body.count('bypass_cache=False') == 2
    assert 'bypass_cache=force' not in body


def test_brapi_counts_outbound_attempts_before_network_io():
    body = _function_body(MAIN, '_brapi_get_json', '_normalize_income_event_type')
    assert body.index("_brapi_usage_record('calls')") < body.index('response = requests.get(')
    assert "except requests.RequestException:" in body
    assert "_brapi_usage_record('errors', error=True)" in body


def test_forbidden_dividend_endpoint_does_not_trigger_include_raw_retry():
    body = _function_body(MAIN, '_fetch_brapi_dividend_events', 'sync_brapi_proventos')
    assert "getattr(exc.response, 'status_code', None) != 400" in body
    assert 'in (400, 403)' not in body


def test_one_time_pluggy_event_recheck_has_a_persistent_migration_marker():
    assert "migration_pluggy_provento_type_recheck_v433" in MAIN
    assert "if not already_rechecked:" in MAIN


def test_readme_documents_local_counter_scope_and_existing_pluggy_preferences():
    assert 'outros aplicativos que compartilhem a mesma credencial' in README
    assert 'frequência já configurada pelo usuário é preservada' in README
