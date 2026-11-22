"""Regression tests for Lonely Wallet 4.2.x security and financial fixes."""
import os
import tempfile
import unittest
import datetime
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
            con = wallet.token_db()
            con.execute('DELETE FROM app_auth')
            con.execute('DELETE FROM auth_attempts')
            con.execute("DELETE FROM operacoes WHERE UPPER(ticker)='IRPFBASE4'")
            con.execute("DELETE FROM saldos_importados WHERE UPPER(ticker)='IRPFBASE4'")
            con.execute("DELETE FROM irpf_lancamentos WHERE upper(fonte_pagadora) LIKE '%TESTE DE ISOLAMENTO%'")
            con.commit()

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


if __name__ == '__main__':
    unittest.main()
