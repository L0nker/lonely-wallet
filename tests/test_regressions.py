"""Regression tests for Lonely Wallet 4.2.x security and financial fixes."""
import os
import tempfile
import unittest
from pathlib import Path

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
            con = wallet.token_db()
            con.execute('DELETE FROM app_auth')
            con.execute('DELETE FROM auth_attempts')
            con.commit()
            # This suite owns a temporary database: fiscal fixtures must never leak across tests.
            assets = wallet.db()
            assets.execute('DELETE FROM irpf_lancamentos')
            assets.execute('DELETE FROM darfs')
            assets.commit()

    def cleanup_ticker_data(self, ticker):
        with wallet.app.app_context():
            con = wallet.db()
            con.execute('DELETE FROM operacoes WHERE upper(ticker)=?', (ticker.upper(),))
            con.execute('DELETE FROM saldos_importados WHERE upper(ticker)=?', (ticker.upper(),))
            con.commit()

    def test_pluggy_sync_interval_is_required(self):
        response = self.client.post('/api/pluggy/settings', json={})
        self.assertEqual(response.status_code, 400)
        self.assertIn('interval_minutes', response.get_json()['erro'])

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

    def test_retired_market_apis_are_not_available(self):
        self.assertNotIn('twelvedata', wallet.API_PROVIDERS)
        self.assertNotIn('alphavantage', wallet.API_PROVIDERS)

    def test_health_endpoint(self):
        response = self.client.get('/api/health')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['ok'])

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
        self.addCleanup(self.cleanup_ticker_data, 'BASEONLY4')
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


    def test_b3_rejects_non_finite_cells(self):
        for value in ('NaN', 'Infinity', '-Infinity'):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    wallet._b3_number(value)

    def test_b3_semantic_duplicate_requires_same_date_and_exact_fill(self):
        self.addCleanup(self.cleanup_ticker_data, 'SEMANTIC4')
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
        self.addCleanup(self.cleanup_ticker_data, 'IRPFBASE4')
        with wallet.app.app_context():
            con = wallet.db()
            con.execute("DELETE FROM operacoes WHERE ticker=?", ('IRPFBASE4',))
            con.execute("DELETE FROM saldos_importados WHERE ticker=?", ('IRPFBASE4',))
            con.execute('INSERT INTO saldos_importados(ticker,tipo,quantidade,preco_medio,preco_base,data_base,nome,origem) VALUES(?,?,?,?,?,?,?,?)',
                        ('IRPFBASE4','acao',10,20,20,'2025-12-31','IRPF base','Teste'))
            con.execute("INSERT INTO operacoes(ticker,tipo,lado,quantidade,preco,data,custos,status,data_registro,data_execucao,detalhes) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        ('IRPFBASE4','acao','venda',4,30,'2026-02-10',0,'executada','2026-02-10','2026-02-10T10:00:00-03:00','{}'))
            con.commit()
            summary = wallet.build_irpf_summary(2026)
            self.assertGreater(summary['resumo']['resultado_vendas'], 0)
            self.assertTrue(any(x['ticker']=='IRPFBASE4' and x['quantidade']==6 for x in summary['posicoes']))


    def test_retroactive_sale_cannot_make_later_position_negative(self):
        self.addCleanup(self.cleanup_ticker_data, 'RETRO4')
        self.addCleanup(self.cleanup_ticker_data, 'OTHER4')
        with wallet.app.app_context():
            con = wallet.db()
            con.execute("DELETE FROM operacoes WHERE ticker=?", ('RETRO4',))
            con.execute("DELETE FROM saldos_importados WHERE ticker=?", ('RETRO4',))
            con.execute("DELETE FROM saldos_importados WHERE ticker=?", ('OTHER4',))
            con.execute("INSERT INTO saldos_importados(ticker,tipo,quantidade,preco_medio,preco_base,data_base,nome,origem) VALUES(?,?,?,?,?,?,?,?)",
                        ('OTHER4','acao',1,10,10,'2026-02-20','Other asset','Teste'))
            con.execute("INSERT INTO operacoes(ticker,tipo,lado,quantidade,preco,data,custos,status,data_registro,data_execucao,detalhes) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        ('RETRO4','acao','compra',10,10,'2026-01-10',0,'executada','2026-01-10','2026-01-10T10:00:00-03:00','{}'))
            con.execute("INSERT INTO operacoes(ticker,tipo,lado,quantidade,preco,data,custos,status,data_registro,data_execucao,detalhes) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        ('RETRO4','acao','venda',8,12,'2026-03-02',0,'executada','2026-03-02','2026-03-02T10:00:00-03:00','{}'))
            con.commit()
            result = wallet.sale_breaks_future_position('RETRO4', {
                'lado':'venda','quantidade':5,'data':'2026-02-01',
                'data_execucao':'2026-02-01T10:00:00-03:00','status':'executada'
            })
            self.assertTrue(result)
            con.execute("DELETE FROM operacoes WHERE ticker=?", ('RETRO4',))
            con.commit()

    def test_provider_error_redacts_urls_and_tokens(self):
        message = wallet.safe_error_text('Request failed https://api.example/v1?token=secret123&x=1 api_key=abc123')
        self.assertNotIn('api.example', message)
        self.assertNotIn('secret123', message)
        self.assertNotIn('abc123', message)
        self.assertIn('[URL removida]', message)
        quoted = wallet.safe_error_text('{"clientSecret":"s3cr3t","apiKey":"k-123"} Authorization: Bearer topsecret')
        for secret in ('s3cr3t', 'k-123', 'topsecret'):
            self.assertNotIn(secret, quoted)

    def test_csp_does_not_allow_generic_https_for_connect_style_or_font(self):
        response = self.client.get('/')
        policy = response.headers.get('Content-Security-Policy', '')
        for directive in ('connect-src', 'style-src', 'font-src'):
            value = next(part for part in policy.split(';') if part.strip().startswith(directive))
            self.assertNotRegex(value, r'(?<![A-Za-z0-9.-])https:(?!//)')
        self.assertIn("script-src 'self'", policy)
        img_src = next(part for part in policy.split(';') if part.strip().startswith('img-src'))
        self.assertNotRegex(img_src, r'(?<![A-Za-z0-9.-])https:(?!//)')
        self.assertIn('https://icons.brapi.dev', img_src)

    def test_dynamic_string_click_arguments_use_safe_json_encoder(self):
        template = (Path(__file__).resolve().parents[1] / 'app' / 'templates' / 'index.html').read_text()
        self.assertIn('function jsAttrArg(value)', template)
        import re
        for match in re.finditer(r'on(?:click|change)="([^"]*)"', template):
            handler = match.group(1)
            self.assertNotRegex(handler, r"'\$\{(?:escapeHTML\()?[^}]+\}\)?'")
        self.assertIn('selectFixedIncomeSale(${jsAttrArg(item.ticker)})', template)
        self.assertIn('setReturnCardBenchmark(${jsAttrArg(cardKey)},this.value)', template)


    def test_pluggy_auto_sync_is_not_driven_by_browser_timer(self):
        template = (Path(__file__).resolve().parents[1] / 'app' / 'templates' / 'index.html').read_text()
        self.assertIn('mesmo com o navegador fechado', template)
        self.assertNotIn('setInterval(runPluggyAutoSync', template)
        self.assertNotIn('lonelyWalletPluggyAutoSyncAt', template)
        self.assertIn("body:JSON.stringify({force:true,item_id:id})", template)  # explicit manual sync only

    def test_quantity_ledger_accepts_purchase_between_retroactive_and_future_sales(self):
        self.addCleanup(self.cleanup_ticker_data, 'TIMELINE4')
        with wallet.app.app_context():
            con = wallet.db()
            con.execute('DELETE FROM operacoes WHERE upper(ticker)=?', ('TIMELINE4',))
            con.execute('DELETE FROM saldos_importados WHERE upper(ticker)=?', ('TIMELINE4',))
            con.execute('INSERT INTO saldos_importados(ticker,tipo,quantidade,preco_medio,preco_base,data_base,nome,origem) VALUES(?,?,?,?,?,?,?,?)',
                        ('TIMELINE4','acao',10,10,10,'2026-01-01','Timeline','Teste'))
            con.execute("INSERT INTO operacoes(ticker,tipo,lado,quantidade,preco,data,custos,status,data_registro,data_execucao,detalhes) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        ('TIMELINE4','acao','compra',5,10,'2026-02-20',0,'executada','2026-02-20','2026-02-20T10:00:00-03:00','{}'))
            con.execute("INSERT INTO operacoes(ticker,tipo,lado,quantidade,preco,data,custos,status,data_registro,data_execucao,detalhes) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        ('TIMELINE4','acao','venda',8,12,'2026-03-02',0,'agendada','2026-03-02','2026-03-02T10:00:00-03:00','{}'))
            con.commit()
            result = wallet.sale_breaks_future_position('TIMELINE4', {
                'lado':'venda','quantidade':5,'data':'2026-02-01',
                'data_execucao':'2026-02-01T10:00:00-03:00','status':'executada'
            })
            self.assertFalse(result)

    def test_removing_opening_purchase_cannot_leave_future_sales_uncovered(self):
        self.addCleanup(self.cleanup_ticker_data, 'REMOVE4')
        with wallet.app.app_context():
            con = wallet.db()
            con.execute('DELETE FROM operacoes WHERE upper(ticker)=?', ('REMOVE4',))
            con.execute('DELETE FROM saldos_importados WHERE upper(ticker)=?', ('REMOVE4',))
            con.execute("INSERT INTO operacoes(ticker,tipo,lado,quantidade,preco,data,custos,status,data_registro,data_execucao,detalhes) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        ('REMOVE4','acao','compra',10,10,'2026-01-10',0,'executada','2026-01-10','2026-01-10T10:00:00-03:00','{}'))
            con.execute("INSERT INTO operacoes(ticker,tipo,lado,quantidade,preco,data,custos,status,data_registro,data_execucao,detalhes) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        ('REMOVE4','acao','venda',8,12,'2026-03-02',0,'agendada','2026-03-02','2026-03-02T10:00:00-03:00','{}'))
            con.commit()
            self.assertTrue(wallet.sale_breaks_future_position('REMOVE4', None, exclude_id=con.execute("SELECT id FROM operacoes WHERE ticker='REMOVE4' AND lado='compra'").fetchone()[0]))


if __name__ == '__main__':
    unittest.main()
