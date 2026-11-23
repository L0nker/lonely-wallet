"""Regression tests for Lonely Wallet 4.2.x security and financial fixes."""
import os
import tempfile
import unittest
import datetime
import io
import sqlite3
from pathlib import Path
from unittest.mock import patch

ROOT = Path(tempfile.mkdtemp(prefix='lonely-wallet-tests-'))
os.environ['ASSETS_DB_PATH'] = str(ROOT / 'ativos.db')
os.environ['DB_PATH'] = str(ROOT / 'legacy.db')
os.environ['TOKEN_DB_PATH'] = str(ROOT / 'tokens.db')
os.environ['SESSION_SECRET_FILE'] = str(ROOT / '.session.key')
os.environ['API_KEYS_KEY_FILE'] = str(ROOT / '.api.key')

from app import main as wallet  # noqa: E402


class RegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        wallet.prepare_database_files()
        wallet.init_db()

    def setUp(self):
        self.client = wallet.app.test_client()
        with wallet.app.app_context():
            # Auth data lives in tokens.db; financial fixtures live in ativos.db.
            secrets_db = wallet.token_db()
            secrets_db.execute('DELETE FROM app_auth')
            secrets_db.execute('DELETE FROM auth_attempts')
            secrets_db.commit()

            con = wallet.db()
            # Each test starts with clean financial source tables. This prevents
            # imported opening balances from leaking into IRPF/position tests.
            for table in (
                'b3_importados', 'b3_import_lotes', 'proventos_api', 'proventos',
                'operacoes', 'saldos_importados', 'irpf_lancamentos',
                'api_response_cache', 'api_request_log', 'api_cache_stats', 'api_provider_state'
            ):
                con.execute(f'DELETE FROM {table}')
            con.execute("DELETE FROM app_settings WHERE key='pluggy_sync_interval'")
            con.commit()

    def test_00_setup_fixture_leaves_a_balance_for_next_test_to_check(self):
        with wallet.app.app_context():
            con = wallet.db()
            con.execute('INSERT INTO saldos_importados(ticker,tipo,quantidade,preco_medio,preco_base,data_base,nome,origem) VALUES(?,?,?,?,?,?,?,?)',
                        ('ISOLATION4','acao',1,10,10,'2026-01-01','Fixture isolation','Teste'))
            con.commit()

    def test_01_setup_clears_all_imported_balances_between_tests(self):
        with wallet.app.app_context():
            count = wallet.db().execute('SELECT COUNT(*) FROM saldos_importados').fetchone()[0]
            self.assertEqual(count, 0)

    def test_pluggy_sync_interval_persists_after_new_request(self):
        saved = self.client.post('/api/pluggy/settings', json={'interval_minutes': 15})
        self.assertEqual(saved.status_code, 200)
        self.assertTrue(saved.get_json()['ok'])
        self.assertEqual(saved.get_json()['interval_minutes'], 15)
        loaded = self.client.get('/api/pluggy/settings')
        self.assertEqual(loaded.status_code, 200)
        self.assertEqual(loaded.get_json()['interval_minutes'], 15)
        with wallet.app.app_context():
            row = wallet.db().execute("SELECT value FROM app_settings WHERE key='pluggy_sync_interval'").fetchone()
            self.assertEqual(row['value'], '15')
            wallet.db().execute("UPDATE app_settings SET value='30' WHERE key='pluggy_sync_interval'")
            wallet.db().commit()

    def test_pluggy_new_install_default_is_four_hours(self):
        with wallet.app.app_context():
            wallet.db().execute("DELETE FROM app_settings WHERE key='pluggy_sync_interval'")
            wallet.db().commit()
            self.assertEqual(wallet._pluggy_sync_interval(), 240)
        response = self.client.get('/api/pluggy/settings')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['interval_minutes'], 240)

    def test_api_response_cache_migration_adds_payload_without_losing_rows(self):
        import sqlite3
        legacy = sqlite3.connect(':memory:')
        legacy.execute('''CREATE TABLE api_response_cache (
            cache_key TEXT PRIMARY KEY, provider TEXT NOT NULL, endpoint TEXT NOT NULL DEFAULT '',
            status_code INTEGER NOT NULL DEFAULT 200, fetched_at REAL NOT NULL DEFAULT 0,
            expires_at REAL NOT NULL DEFAULT 0, stale_until REAL NOT NULL DEFAULT 0
        )''')
        legacy.execute(
            'INSERT INTO api_response_cache(cache_key,provider,endpoint,status_code) VALUES(?,?,?,?)',
            ('legacy-cache-key', 'brapi', '/api/quote/TEST4', 200)
        )
        wallet._migrate_api_response_cache_schema(legacy)
        columns = {row[1] for row in legacy.execute('PRAGMA table_info(api_response_cache)').fetchall()}
        self.assertIn('payload', columns)
        row = legacy.execute(
            'SELECT cache_key,provider,endpoint,payload FROM api_response_cache WHERE cache_key=?',
            ('legacy-cache-key',)
        ).fetchone()
        self.assertEqual(row[:3], ('legacy-cache-key', 'brapi', '/api/quote/TEST4'))
        self.assertEqual(bytes(row[3] or b''), b'')
        legacy.close()

    def test_sgs_series_retries_empty_response_and_parses_valid_json(self):
        start, end = '2026-01-01', '2026-01-03'
        key = (12, start, end)
        wallet.SGS_CACHE.pop(key, None)
        empty = wallet.requests.Response()
        empty.status_code = 200
        empty._content = b''
        valid = wallet.requests.Response()
        valid.status_code = 200
        valid._content = wallet.json.dumps([{'data': '02/01/2026', 'valor': '0,05'}]).encode()
        valid.headers['Content-Type'] = 'application/json'
        with patch.object(wallet.requests, 'get', side_effect=[empty, valid]) as mocked, \
             patch.object(wallet.time, 'sleep'):
            points = wallet._sgs_series(12, start, end)
        self.assertEqual(mocked.call_count, 2)
        self.assertEqual(points, [(wallet.date(2026, 1, 2), 0.05)])

    def test_sgs_series_uses_recent_stale_cache_when_bcb_returns_empty(self):
        start, end = '2026-01-01', '2026-01-03'
        key = (12, start, end)
        cached_points = [(wallet.date(2026, 1, 2), 0.05)]
        wallet.SGS_CACHE[key] = (wallet.time.time() - wallet.SGS_CACHE_TTL - 60, cached_points)
        empty = wallet.requests.Response()
        empty.status_code = 200
        empty._content = b''
        with patch.object(wallet.requests, 'get', return_value=empty) as mocked, \
             patch.object(wallet.time, 'sleep'):
            points = wallet._sgs_series(12, start, end)
        self.assertEqual(mocked.call_count, 3)
        self.assertEqual(points, cached_points)
        wallet.SGS_CACHE.pop(key, None)

    def test_api_cache_row_treats_empty_migrated_payload_as_cache_miss(self):
        with wallet.app.app_context():
            con = wallet.db()
            con.execute(
                '''INSERT INTO api_response_cache(cache_key,provider,endpoint,payload,status_code,
                   fetched_at,expires_at,stale_until) VALUES(?,?,?,?,?,?,?,?)''',
                ('empty-migrated-cache', 'brapi', '/api/quote/OLD4', sqlite3.Binary(b''),
                 200, wallet.time.time(), wallet.time.time() + 3600, wallet.time.time() + 86400)
            )
            con.commit()
            self.assertIsNone(wallet._api_cache_row('empty-migrated-cache'))

    def test_sgs_series_rejects_repeated_empty_responses_without_fabricating_data(self):
        start, end = '2026-03-01', '2026-03-03'
        key = (12, start, end)
        wallet.SGS_CACHE.pop(key, None)
        empty = wallet.requests.Response()
        empty.status_code = 200
        empty._content = b''
        with patch.object(wallet.requests, 'get', return_value=empty) as mocked, \
             patch.object(wallet.time, 'sleep'):
            with self.assertRaisesRegex(ValueError, 'resposta vazia ou não JSON'):
                wallet._sgs_series(12, start, end)
        self.assertEqual(mocked.call_count, 3)

    def test_brapi_response_cache_persists_and_avoids_repeat_request(self):
        payload = {'results': [{'symbol': 'CACHE4', 'regularMarketPrice': 12.34}]}
        response = wallet.requests.Response()
        response.status_code = 200
        response._content = wallet.json.dumps(payload).encode('utf-8')
        response.headers['Content-Type'] = 'application/json'
        with wallet.app.app_context(), patch.object(wallet.requests, 'get', return_value=response) as mocked:
            first = wallet._brapi_get('https://brapi.dev/api/quote/CACHE4',
                params={'token': 'test-token-cache'}, headers={'Authorization': 'Bearer test-token-cache'},
                cache_ttl=3600, stale_ttl=86400)
            second = wallet._brapi_get('https://brapi.dev/api/quote/CACHE4',
                params={'token': 'test-token-cache'}, headers={'Authorization': 'Bearer test-token-cache'},
                cache_ttl=3600, stale_ttl=86400)
            self.assertEqual(first.status_code, 200)
            self.assertEqual(second.status_code, 200)
            self.assertEqual(second.headers.get('X-LonelyWallet-Cache'), 'hit')
            self.assertEqual(mocked.call_count, 1)
            cache_key = wallet._api_response_cache_key('brapi', 'https://brapi.dev/api/quote/CACHE4',
                {'token': 'test-token-cache'}, {'Authorization': 'Bearer test-token-cache'})
            self.assertNotIn('test-token-cache', cache_key)
            usage = wallet._api_usage_snapshot()
            self.assertGreaterEqual(usage['cache_hits'], 1)

    def test_api_request_registration_succeeds_and_returns_id(self):
        with wallet.app.app_context():
            request_id = wallet._api_record_request('pluggy', 'AUTH:regression-test', 0)
            self.assertIsNotNone(request_id)
            row = wallet.db().execute(
                'SELECT provider,endpoint,status_code FROM api_request_log WHERE id=?',
                (request_id,)
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row['provider'], 'pluggy')
            self.assertEqual(row['endpoint'], 'AUTH:regression-test')

    def test_pluggy_request_gap_uses_persisted_request_log(self):
        with wallet.app.app_context():
            con = wallet.db()
            now = wallet.time.time()
            con.execute(
                'INSERT INTO api_request_log(provider,endpoint,requested_at,status_code) VALUES(?,?,?,?)',
                ('pluggy', 'GET:investments', now - 0.2, 200)
            )
            con.commit()
            with patch.object(wallet.time, 'sleep') as sleep:
                wallet._provider_wait_for_gap('pluggy')
            self.assertEqual(sleep.call_count, 1)
            self.assertGreater(sleep.call_args.args[0], 0)
            self.assertLessEqual(sleep.call_args.args[0], wallet.PLUGGY_REQUEST_GAP_SECONDS)

    def test_pluggy_429_cooldown_blocks_requests_even_if_gap_is_disabled(self):
        with wallet.app.app_context(), patch.object(wallet, 'PLUGGY_REQUEST_GAP_SECONDS', 0):
            wallet._provider_set_cooldown('pluggy', 60, 429, 'rate_limited')
            with self.assertRaises(ValueError):
                wallet._provider_wait_for_gap('pluggy')

    def test_api_provider_cooldown_is_persisted_after_network_failure(self):
        with wallet.app.app_context():
            wallet._provider_set_cooldown('brapi', 45, 0, 'network_error')
            row = wallet.db().execute(
                'SELECT cooldown_until,last_status_code,last_error FROM api_provider_state WHERE provider=?',
                ('brapi',)
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertGreater(row['cooldown_until'], wallet.time.time())
            self.assertEqual(row['last_error'], 'network_error')

    def test_brapi_budget_blocks_network_when_local_limit_is_reached(self):
        with wallet.app.app_context(), patch.object(wallet, 'BRAPI_LOCAL_30D_BUDGET', 1),              patch.object(wallet.requests, 'get') as mocked:
            wallet._api_record_request('brapi', 'https://brapi.dev/api/quote/USED4', 200)
            response = wallet._brapi_get('https://brapi.dev/api/quote/BLOCK4',
                params={'unique': 'budget-test'}, cache_ttl=3600, stale_ttl=3600)
            self.assertEqual(response.status_code, 429)
            mocked.assert_not_called()

    def test_hgbrasil_and_dadosb3_local_budgets_block_network(self):
        with wallet.app.app_context():
            cases = (
                ('hgbrasil', 'https://api.hgbrasil.com/finance', 'HGBRASIL_LOCAL_24H_BUDGET'),
                ('dadosb3', 'https://dadosb3.com/indices/IFIX/precos', 'DADOSB3_LOCAL_24H_BUDGET'),
            )
            for provider, url, setting in cases:
                with self.subTest(provider=provider), patch.object(wallet, setting, 1), patch.object(wallet.requests, 'get') as mocked:
                    wallet._api_record_request(provider, 'previous-request', 200)
                    response = wallet._provider_get(provider, url, params={'unique': provider}, cache_ttl=3600)
                    self.assertEqual(response.status_code, 429)
                    mocked.assert_not_called()

    def test_api_usage_endpoint_uses_only_local_database(self):
        response = self.client.get('/api/integracoes/consumo')
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()['brapi']
        self.assertIn('requests_last_30_days', payload)
        self.assertIn('remaining_local_budget', payload)
        self.assertIn('cache_hits', payload)

    def test_retired_market_apis_are_not_available(self):
        self.assertNotIn('twelvedata', wallet.API_PROVIDERS)
        self.assertNotIn('alphavantage', wallet.API_PROVIDERS)

    def test_health_endpoint(self):
        response = self.client.get('/api/health')
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload['ok'])
        self.assertIn(payload['status'], ('healthy', 'degraded'))
        self.assertIn('charts', payload)
        self.assertIn(payload['charts']['engine'], ('chartjs', 'fallback'))
        self.assertTrue(payload['charts']['fallback_available'])

    def test_dashboard_loads_fallback_as_a_separate_script(self):
        template = (Path(wallet.__file__).resolve().parent / 'templates' / 'index.html').read_text(encoding='utf-8')
        primary = template.index("filename='vendor/chart.umd.min.js'")
        fallback = template.index("filename='vendor/chart-fallback.js'")
        self.assertLess(primary, fallback)
        self.assertNotIn('onerror=', template[primary - 80:fallback + 100])

    def test_non_finite_operation_values_rejected(self):
        with wallet.app.test_request_context('/'):
            for value in ('NaN', 'Infinity', '-Infinity'):
                payload = {
                    'ticker': 'PETR4', 'tipo': 'acao', 'lado': 'compra',
                    'quantidade': value, 'preco': '10', 'custos': '0',
                    'data': '2026-01-02'
                }
                result, error = wallet.valid_operation(payload)
                self.assertIsNone(result)
                self.assertIsNotNone(error)

    def test_disable_pin_has_shared_lockout(self):
        salt = '11' * 16
        digest = wallet.pin_digest('123456', salt)
        with wallet.app.app_context():
            con = wallet.token_db()
            con.execute(
                'INSERT INTO app_auth(id,pin_salt,pin_hash,failed_attempts,locked_until,updated_at,enabled) VALUES(1,?,?,0,0,?,1)',
                (salt, digest, '2026-01-01T00:00:00')
            )
            con.commit()
        statuses = []
        for _ in range(5):
            response = self.client.post('/api/auth/disable', json={'pin': '000000'})
            statuses.append(response.status_code)
        self.assertEqual(statuses[-1], 429)
        response = self.client.post('/api/auth/disable', json={'pin': '123456'})
        self.assertEqual(response.status_code, 429)


    def test_pin_lockout_is_scoped_to_client_ip(self):
        salt = '22' * 16
        digest = wallet.pin_digest('123456', salt)
        with wallet.app.app_context():
            con = wallet.token_db()
            con.execute(
                'INSERT INTO app_auth(id,pin_salt,pin_hash,failed_attempts,locked_until,updated_at,enabled) VALUES(1,?,?,0,0,?,1)',
                (salt, digest, '2026-01-01T00:00:00')
            )
            con.commit()
        for _ in range(5):
            self.client.post('/api/auth/login', json={'pin':'000000'})
        response = self.client.post('/api/auth/login', json={'pin':'123456'}, environ_base={'REMOTE_ADDR':'192.0.2.44'})
        self.assertEqual(response.status_code, 200)

    def test_cross_origin_auth_post_rejected(self):
        response = self.client.post(
            '/api/auth/disable', json={'pin': '123456'},
            headers={'Origin': 'https://attacker.example'}
        )
        self.assertEqual(response.status_code, 403)

    def test_non_finite_values_rejected_by_financial_endpoints(self):
        cases = [
            ('/api/proventos', {'ticker':'TEST4','valor_total':'Infinity','data':'2026-01-02'}),
            ('/api/metas', {'nome':'Teste','objetivo':'NaN','aporte':'0','tipo':'patrimonio'}),
            ('/api/irpf/lancamentos', {'grupo':'isento','natureza':'Teste','fonte_pagadora':'Teste','valor':'Infinity','data':'2026-01-02'}),
            ('/api/darfs', {'competencia':'2026-01','codigo':'6015','valor':'Infinity','vencimento':'2026-02-20','status':'pendente'}),
        ]
        for url, payload in cases:
            with self.subTest(url=url):
                response = self.client.post(url, json=payload)
                self.assertEqual(response.status_code, 400)

    def test_imported_balance_asset_can_be_sold(self):
        with wallet.app.app_context():
            con = wallet.db()
            con.execute('DELETE FROM saldos_importados WHERE ticker=?', ('BASEONLY4',))
            con.execute('INSERT INTO saldos_importados(ticker,tipo,quantidade,preco_medio,preco_base,data_base,nome,origem) VALUES(?,?,?,?,?,?,?,?)',
                        ('BASEONLY4','acao',10,20,20,'2026-01-01','Base only','Teste'))
            con.commit()
        with wallet.app.test_request_context('/'):
            result, error = wallet.valid_operation({'ticker':'BASEONLY4','tipo':'acao','lado':'venda',
                'quantidade':'2','preco':'25','custos':'0','data':'2026-02-01'})
            self.assertIsNone(error)
            self.assertIsNotNone(result)



    def test_carteira_route_is_bound_to_portfolio_view(self):
        rules = [rule for rule in wallet.app.url_map.iter_rules() if rule.rule == '/api/carteira']
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0].endpoint, 'carteira')
        self.assertIs(wallet.app.view_functions[rules[0].endpoint], wallet.carteira)

    def test_retroactive_sale_cannot_break_later_sale(self):
        with wallet.app.app_context():
            con = wallet.db()
            con.execute("DELETE FROM operacoes WHERE UPPER(ticker)='RETROTEST4'")
            con.execute("DELETE FROM saldos_importados WHERE UPPER(ticker)='RETROTEST4'")
            con.execute('INSERT INTO saldos_importados(ticker,tipo,quantidade,preco_medio,preco_base,data_base,nome,origem) VALUES(?,?,?,?,?,?,?,?)',
                        ('RETROTEST4','acao',10,20,20,'2026-01-01','Retro Test','Teste'))
            con.execute("INSERT INTO operacoes(ticker,tipo,lado,quantidade,preco,data,custos,status,data_registro,data_execucao,detalhes) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        ('RETROTEST4','acao','venda',8,25,'2026-03-02',0,'executada','2026-03-02','2026-03-02T10:00:00-03:00','{}'))
            con.commit()
            self.assertTrue(wallet.sale_breaks_future_position('RETROTEST4','2026-02-01',-5))
            self.assertFalse(wallet.sale_breaks_future_position('RETROTEST4','2026-02-01',-2))


    def test_removing_purchase_cannot_leave_future_sale_unfunded(self):
        with wallet.app.app_context():
            con = wallet.db()
            con.execute("DELETE FROM operacoes WHERE UPPER(ticker)='DELETEBUY4'")
            con.execute("DELETE FROM saldos_importados WHERE UPPER(ticker)='DELETEBUY4'")
            con.execute('INSERT INTO saldos_importados(ticker,tipo,quantidade,preco_medio,preco_base,data_base,nome,origem) VALUES(?,?,?,?,?,?,?,?)',
                        ('DELETEBUY4','acao',10,20,20,'2026-01-01','Delete Buy Test','Teste'))
            purchase = con.execute("INSERT INTO operacoes(ticker,tipo,lado,quantidade,preco,data,custos,status,data_registro,data_execucao,detalhes) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        ('DELETEBUY4','acao','compra',5,20,'2026-02-01',0,'executada','2026-02-01','2026-02-01T10:00:00-03:00','{}'))
            con.execute("INSERT INTO operacoes(ticker,tipo,lado,quantidade,preco,data,custos,status,data_registro,data_execucao,detalhes) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        ('DELETEBUY4','acao','venda',12,25,'2026-03-02',0,'executada','2026-03-02','2026-03-02T10:00:00-03:00','{}'))
            con.commit()
            self.assertTrue(wallet.sale_breaks_future_position('DELETEBUY4','2026-02-01',0.0,{int(purchase.lastrowid)}))

    def test_paid_api_proventos_are_included_and_future_receipts_excluded(self):
        with wallet.app.app_context():
            con = wallet.db()
            con.execute("DELETE FROM proventos_api WHERE upper(ticker)='PROVTEST4'")
            con.execute("DELETE FROM proventos WHERE upper(ticker)='PROVTEST4'")
            stamp = wallet.datetime.now(wallet.TZ).isoformat(timespec='seconds')
            con.execute("INSERT INTO proventos_api(event_key,ticker,tipo_ativo,tipo_evento,data_com,data_ex,data_pagamento,valor_por_ativo,quantidade,valor_total,status,remarks,sincronizado_em) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        ('PROVTEST4:PAID','PROVTEST4','fii','Rendimento','2026-01-10','2026-01-11','2026-01-15',1.0,20,20.0,'Pago','Teste',stamp))
            con.execute("INSERT INTO proventos_api(event_key,ticker,tipo_ativo,tipo_evento,data_com,data_ex,data_pagamento,valor_por_ativo,quantidade,valor_total,status,remarks,sincronizado_em) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        ('PROVTEST4:FUTURE','PROVTEST4','fii','Rendimento','2026-12-10','2026-12-11','2026-12-15',1.0,20,20.0,'A receber','Teste',stamp))
            con.commit()
            records = wallet._received_proventos_records(['PROVTEST4'])
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]['valor_total'], 20.0)
            self.assertEqual(records[0]['status'], 'Pago')

    def test_upcoming_proventos_filter_keeps_current_and_future_months_only(self):
        as_of = datetime.date(2026, 11, 23)
        rows = [
            {'ticker':'OVERDUE11','status':'A receber','data_pagamento':'2026-10-31','valor_total':10.00,'tipo_evento':'DIVIDENDO'},
            {'ticker':'CURRENT11','status':'A receber','data_pagamento':'2026-11-02','valor_total':12.00,'tipo_evento':'DIVIDENDO'},
            {'ticker':'FUTURE11','status':'A receber','data_pagamento':'2026-12-15','valor_total':13.00,'tipo_evento':'JCP'},
            {'ticker':'PAID11','status':'Pago','data_pagamento':'2026-11-22','valor_total':14.00,'tipo_evento':'DIVIDENDO'},
            {'ticker':'AMORT11','status':'A receber','data_pagamento':'2026-12-01','valor_total':15.00,'tipo_evento':'AMORTIZAÇÃO'},
        ]
        filtered = wallet._filter_upcoming_proventos_by_month(rows, as_of=as_of)
        self.assertEqual([row['ticker'] for row in filtered], ['CURRENT11', 'FUTURE11'])
        self.assertEqual(sum(row['valor_total'] for row in filtered), 25.00)

    def test_confirmed_proventos_total_adds_only_announced_payments_within_next_12_months(self):
        as_of = datetime.date(2026, 11, 22)
        upcoming = [
            {'ticker':'ABCD11','status':'A receber','data_pagamento':'2026-11-22','valor_total':5.00,'tipo_evento':'RENDIMENTO'},
            {'ticker':'ABCD11','status':'A receber','data_pagamento':'2026-11-23','valor_total':20.00,'tipo_evento':'RENDIMENTO'},
            {'ticker':'ABCD11','status':'A receber','data_pagamento':'2026-11-23','valor_total':20.00,'tipo_evento':'RENDIMENTO'},
            {'ticker':'EFGH11','status':'A receber','data_pagamento':'2027-11-22','valor_total':30.00,'tipo_evento':'DIVIDENDO'},
            {'ticker':'IJKL11','status':'A receber','data_pagamento':'2027-11-23','valor_total':999.00,'tipo_evento':'DIVIDENDO'},
            {'ticker':'MNOP11','status':'A receber','data_pagamento':'2026-11-21','valor_total':500.00,'tipo_evento':'DIVIDENDO'},
            {'ticker':'QRST11','status':'Pago','data_pagamento':'2026-12-01','valor_total':600.00,'tipo_evento':'DIVIDENDO'},
            {'ticker':'UVWX11','status':'Cancelado','data_pagamento':'2026-12-01','valor_total':700.00,'tipo_evento':'DIVIDENDO'},
            {'ticker':'YZAA11','status':'A receber','data_pagamento':'2026-12-01','valor_total':800.00,'tipo_evento':'AMORTIZAÇÃO'},
            {'ticker':'BCDE11','status':'A receber','data_pagamento':'2026-12-01','valor_total':'NaN','tipo_evento':'DIVIDENDO'},
        ]
        total = wallet._confirmed_proventos_total_12m(100.00, upcoming, as_of=as_of)
        self.assertEqual(total, 155.00)

    def test_proventos_received_card_excludes_upcoming_and_unconfirmed_events(self):
        as_of = datetime.date(2026, 11, 22)
        records = [
            {'ticker':'PAID4','status':'Pago','data_pagamento':'2026-11-22','valor_total':12.50,'tipo_evento':'DIVIDENDO'},
            {'ticker':'PAID4','status':'Pago','data_pagamento':'2026-10-15','valor_total':7.50,'tipo_evento':'JCP'},
            {'ticker':'PAID4','status':'Pago','data_pagamento':'2026-11-23','valor_total':100.00,'tipo_evento':'DIVIDENDO'},
            {'ticker':'PAID4','status':'A receber','data_pagamento':'2026-11-20','valor_total':200.00,'tipo_evento':'DIVIDENDO'},
            {'ticker':'PAID4','status':'Pago','data_pagamento':'2025-08-01','valor_total':300.00,'tipo_evento':'DIVIDENDO'},
            {'ticker':'PAID4','status':'Pago','data_pagamento':'2026-11-01','valor_total':400.00,'tipo_evento':'AMORTIZAÇÃO'},
        ]
        confirmed = wallet._confirmed_received_proventos(records, as_of=as_of)
        self.assertEqual([row['valor_total'] for row in confirmed], [12.50, 7.50, 300.00])
        monthly = wallet._confirmed_received_proventos_by_month(
            records, ['2026-10', '2026-11'], as_of=as_of
        )
        self.assertEqual(monthly, {'2026-11': 12.50, '2026-10': 7.50})
        self.assertEqual(round(sum(monthly.values()), 2), 20.00)

    def test_error_text_redacts_tokens_in_urls(self):
        message = wallet.safe_error_text('Request failed https://api.example.test/path?api_key=secret123&token=other')
        self.assertNotIn('secret123', message)
        self.assertNotIn('other', message)
        self.assertIn('api.example.test', message)

    def test_quote_parser_uses_last_valid_yahoo_daily_close(self):
        timestamp = 1795219200
        payload = {'chart': {'result': [{
            'meta': {'symbol':'RBRR11.SA','regularMarketPrice':0,'previousClose':62.5,'chartPreviousClose':62.5,'longName':'RBR Rendimento'},
            'timestamp':[timestamp],
            'indicators': {'quote':[{'close':[63.10]}]}
        }]}}
        class FakeResponse:
            status_code = 200
            content = b'{"chart":{"result":[]}}'
            headers = {'content-type': 'application/json'}
            def raise_for_status(self):
                return None
            def json(self):
                return payload
        with patch.object(wallet, '_get_api_token', return_value=None), patch.object(wallet.requests, 'get', return_value=FakeResponse()):
            quote = wallet._fallback_market_quote('RBRR11')
        self.assertEqual(quote['regularMarketPrice'], 63.10)
        self.assertTrue(quote['quoteIsStale'])
        self.assertEqual(quote['quoteProvider'], 'Yahoo Finance · último fechamento')
        self.assertEqual(quote['quoteLastUpdated'], '2026-11-21')

    def test_predelete_backup_rotation_keeps_ten_newest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index in range(12):
                folder = root / f'pre-exclusao-{index:02d}'
                folder.mkdir()
                (folder / 'backup.db').write_text(str(index))
                os.utime(folder, (index + 1, index + 1))
            removed = wallet.rotate_predelete_backups(root, keep=10)
            remaining = sorted(path.name for path in root.glob('pre-exclusao-*'))
            self.assertEqual(len(remaining), 10)
            self.assertIn('pre-exclusao-00', removed)
            self.assertIn('pre-exclusao-01', removed)

    def test_legacy_db_migration_does_not_publish_invalid_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            invalid_source = root / 'legacy.db'
            invalid_source.write_bytes(b'not a sqlite database')
            destination = root / 'carteira.db'
            with self.assertRaises(Exception):
                wallet.migrate_legacy_database(invalid_source, destination)
            self.assertFalse(destination.exists())
            self.assertEqual(list(root.glob('.carteira.db.migration-*.tmp')), [])

    def test_b3_rejects_non_finite_cells(self):
        for value in ('NaN', 'Infinity', '-Infinity'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    wallet._b3_number(value)

    def test_b3_semantic_duplicate_requires_same_date_and_exact_fill(self):
        with wallet.app.app_context():
            con = wallet.db()
            con.execute("INSERT INTO operacoes(ticker,tipo,lado,quantidade,preco,data,custos,status,data_registro,data_execucao,detalhes) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        ('SEMANTIC4','acao','compra',10,10,'2026-03-10',0,'executada','2026-03-10','2026-03-10T10:00:00-03:00','{}'))
            con.commit()
            same = {'categoria':'operacao','registro':{'ticker':'SEMANTIC4','tipo':'acao','lado':'compra','quantidade':10,'preco':10,'data':'2026-03-10'}}
            near = {'categoria':'operacao','registro':{'ticker':'SEMANTIC4','tipo':'acao','lado':'compra','quantidade':10,'preco':10,'data':'2026-03-11'}}
            self.assertIsNotNone(wallet._b3_semantic_duplicate(same))
            self.assertIsNone(wallet._b3_semantic_duplicate(near))

    def test_irpf_counts_sale_from_imported_opening_balance(self):
        with wallet.app.app_context():
            con = wallet.db()
            con.execute("DELETE FROM saldos_importados WHERE ticker=?", ('IRPFBASE4',))
            con.execute('INSERT INTO saldos_importados(ticker,tipo,quantidade,preco_medio,preco_base,data_base,nome,origem) VALUES(?,?,?,?,?,?,?,?)',
                        ('IRPFBASE4','acao',10,20,20,'2025-12-31','IRPF base','Teste'))
            con.execute("INSERT INTO operacoes(ticker,tipo,lado,quantidade,preco,data,custos,status,data_registro,data_execucao,detalhes) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        ('IRPFBASE4','acao','venda',4,30,'2026-02-10',0,'executada','2026-02-10','2026-02-10T10:00:00-03:00','{}'))
            con.commit()
            summary = wallet.build_irpf_summary(2026)
            self.assertGreater(summary['resumo']['resultado_vendas'], 0)
            self.assertTrue(any(x['ticker']=='IRPFBASE4' and x['quantidade']==6 for x in summary['posicoes']))

    def test_confirmed_proventos_same_ticker_date_amount_different_event_types_are_not_merged(self):
        as_of = datetime.date(2026, 11, 22)
        upcoming = [
            {'ticker':'DUPL11','status':'A receber','data_pagamento':'2026-12-10','valor_total':12.50,'tipo_evento':'DIVIDENDO'},
            {'ticker':'DUPL11','status':'A receber','data_pagamento':'2026-12-10','valor_total':12.50,'tipo_evento':'JCP'},
        ]
        self.assertEqual(wallet._confirmed_proventos_total_12m(0, upcoming, as_of=as_of), 25.00)

    def test_upload_over_limit_returns_413_with_clear_message(self):
        payload = b'x' * (16 * 1024 * 1024 + 1)
        response = self.client.post('/api/importar-b3', data={
            'arquivo': (io.BytesIO(payload), 'planilha-grande.xlsx')
        }, content_type='multipart/form-data')
        self.assertEqual(response.status_code, 413)
        self.assertIn('Arquivo grande demais', response.get_json()['erro'])

    def test_legacy_db_migration_supports_special_path_characters(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'diretorio ? hash # porcento % e espaco'
            root.mkdir()
            source = root / 'origem #1?.db'
            destination = root / 'destino %2F.db'
            con = __import__('sqlite3').connect(source)
            con.execute('CREATE TABLE sample(value TEXT)')
            con.execute('INSERT INTO sample VALUES(?)', ('persistido',))
            con.commit(); con.close()
            self.assertTrue(wallet.migrate_legacy_database(source, destination))
            verify = __import__('sqlite3').connect(destination)
            try:
                self.assertEqual(verify.execute('SELECT value FROM sample').fetchone()[0], 'persistido')
            finally:
                verify.close()

    def test_backup_rotation_tolerates_candidate_removed_during_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index in range(3):
                folder = root / f'pre-exclusao-{index:02d}'
                folder.mkdir()
                (folder / 'backup.db').write_text(str(index))
                os.utime(folder, (index + 1, index + 1))
            candidate = root / 'pre-exclusao-00'
            original_stat = Path.stat
            def race_stat(path, *args, **kwargs):
                if path == candidate:
                    raise FileNotFoundError('simulated concurrent cleanup')
                return original_stat(path, *args, **kwargs)
            with patch.object(Path, 'stat', race_stat):
                removed = wallet.rotate_predelete_backups(root, keep=1)
            self.assertIsInstance(removed, list)

    def test_version_is_dynamic_in_footer_and_user_agent(self):
        version = (Path(wallet.__file__).resolve().parents[1] / 'VERSION').read_text().strip()
        self.assertEqual(wallet.APP_VERSION, version)
        self.assertIn(version, wallet.USER_AGENT)
        template = (Path(wallet.__file__).resolve().parents[1] / 'app' / 'templates' / 'index.html').read_text(encoding='utf-8')
        self.assertIn('Lonely Wallet v{{ app_version }}', template)

    def test_pluggy_api_log_commits_pending_portfolio_writes(self):
        with wallet.app.app_context():
            con = wallet.db()
            key = 'test_pluggy_commit_before_api_log'
            con.execute('INSERT OR REPLACE INTO app_settings(key,value) VALUES(?,?)', (key, 'pending'))
            self.assertTrue(con.in_transaction)
            request_id = wallet._api_record_request('pluggy', 'GET:regression-test', 0)
            self.assertIsNotNone(request_id)
            self.assertFalse(con.in_transaction)
            row = con.execute('SELECT value FROM app_settings WHERE key=?', (key,)).fetchone()
            self.assertEqual(row['value'], 'pending')
            con.execute('DELETE FROM app_settings WHERE key=?', (key,))
            con.commit()

    def test_process_due_operations_does_not_write_when_nothing_is_due(self):
        with wallet.app.app_context():
            con = wallet.db()
            before = con.total_changes
            self.assertEqual(wallet.process_due_operations(), 0)
            self.assertEqual(con.total_changes, before)
            self.assertFalse(con.in_transaction)

    def test_process_due_operations_promotes_due_rows(self):
        with wallet.app.app_context():
            con = wallet.db()
            due_at = (datetime.datetime.now(wallet.TZ) - datetime.timedelta(minutes=5)).isoformat(timespec='minutes')
            con.execute(
                "INSERT INTO operacoes(ticker,tipo,lado,quantidade,preco,data,custos,status,data_registro,data_execucao,detalhes) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                ('LOCK4','acao','compra',1,10,due_at[:10],0,'agendada',due_at,due_at,'{}')
            )
            con.commit()
            self.assertEqual(wallet.process_due_operations(), 1)
            row = con.execute("SELECT status,data FROM operacoes WHERE ticker='LOCK4'").fetchone()
            self.assertEqual(row['status'], 'executada')
            self.assertEqual(row['data'], due_at[:10])


if __name__ == '__main__':
    unittest.main()
