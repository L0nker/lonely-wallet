"""Static regression checks for API token/quota protection in v4.2.34."""
from pathlib import Path
import re
ROOT = Path(__file__).resolve().parents[1]
MAIN = (ROOT / 'app' / 'main.py').read_text(encoding='utf-8')
TEMPLATE = (ROOT / 'app' / 'templates' / 'index.html').read_text(encoding='utf-8')
def body(name, next_name=None):
    start = MAIN.index(f'def {name}(')
    if next_name:
        return MAIN[start:MAIN.index(f'\ndef {next_name}(', start)]
    match = re.search(r'\n(?:def |@app\.route)', MAIN[start+1:])
    return MAIN[start:start+1+match.start()] if match else MAIN[start:]
def test_budget_defaults_and_single_flight():
    assert "BRAPI_ROLLING_30D_BUDGET', '13500'" in MAIN
    assert "BRAPI_MAX_TICKERS_PER_REQUEST', '1'" in MAIN
    assert 'BRAPI_REQUEST_SEMAPHORE = threading.Semaphore(1)' in MAIN
def test_count_is_incremented_before_network_io():
    code=body('_brapi_get_json')
    assert code.index("_brapi_usage_record('calls')") < code.index('requests.get(url')
def test_cache_identity_fingerprints_token_instead_of_storing_it():
    code=body('_brapi_cache_identity','_brapi_cache_read')
    assert 'auth_fingerprint' in code and 'hashlib.sha256(auth.encode' in code
    assert "not in ('token', 'apikey', 'api_key')" in code
def test_quotes_and_dividends_respect_persistent_cache():
    assert 'bypass_cache=False' in body('cotacao','_fallback_market_quote')
    assert 'bypass_cache=False' in body('_fetch_brapi_dividend_events','sync_brapi_proventos')
def test_dividend_fallback_only_runs_for_failed_tickers():
    code=body('sync_brapi_proventos','_is_income_event')
    assert 'failed_stock_symbols | failed_fii_symbols' in code
    assert 'for ticker in sorted(symbol for symbol in failed_symbols if symbol)' in code
def test_usage_endpoint_has_no_secret_fields_and_ui_loads_on_api_page():
    assert "@app.route('/api/integracoes/brapi/uso'" in MAIN
    assert 'loadBrapiUsage();' in TEMPLATE
    assert 'counter_scope' in MAIN
    assert 'time.time() - 30 * 86400' in MAIN and 'now - 30 * 86400' in MAIN
def test_pluggy_default_is_four_hours_but_saved_values_are_preserved():
    code=body('_pluggy_sync_interval','pluggy_settings')
    assert "value = int(row['value']) if row else 240" in code
    assert 'else 240' in code
    assert 'value="240" selected' in TEMPLATE
    assert 'let pluggyAutoSyncIntervalMinutes=240;' in TEMPLATE
def test_financial_schema_is_still_from_432():
    assert MAIN.count("tipo_evento TEXT NOT NULL DEFAULT 'Provento'") == 1
    assert '_normalize_income_event_type' not in MAIN
    assert 'previous_fixed_by_id' not in MAIN
    assert "CREATE TABLE IF NOT EXISTS api_response_cache" in MAIN
