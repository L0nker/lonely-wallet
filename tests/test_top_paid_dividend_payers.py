from pathlib import Path
import json
import re
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "app" / "templates" / "index.html").read_text(encoding="utf-8")
MAIN = (ROOT / "app" / "main.py").read_text(encoding="utf-8")


class TopPaidDividendPayersTests(unittest.TestCase):
    def test_dashboard_card_is_replaced_and_uses_paid_ranking(self):
        render_start = HTML.index("function render(){")
        render_end = HTML.index("\n\nconst rows=state.itens||[];", render_start)
        renderer = HTML[render_start:render_end]
        self.assertIn("Ranking de proventos", renderer)
        self.assertIn("provDash.maiores_pagadores||[]", renderer)
        self.assertIn('class="insight-title">Ranking de proventos</div>', renderer)
        self.assertIn('class="insight-recent-list"', renderer)
        self.assertIn('class="insight-recent-row"', renderer)
        self.assertIn('class="top-dividend-payer-total"', renderer)
        self.assertNotIn("Maiores pagadores de proventos", renderer)
        self.assertNotIn('return-stat-card', renderer)

    def test_backend_ranking_uses_confirmed_paid_records_for_all_tickers(self):
        self.assertIn("all_confirmed_received = _confirmed_received_proventos(", MAIN)
        self.assertIn("_received_proventos_records(), as_of=today", MAIN)
        self.assertIn("'maiores_pagadores':top_paid_payers", MAIN)
        self.assertIn("'status': 'Pago'", MAIN)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for runtime JavaScript test")
    def test_ranking_excludes_pending_and_future_records(self):
        helper_start = HTML.index("function getTopPaidDividendPayers(")
        helper_end = HTML.index("\n}\n\nfunction render(){", helper_start) + 2
        helper = HTML[helper_start:helper_end]
        records = [
            {"ticker": "PETR4", "tipo_ativo": "acao", "status": "Pago", "data_pagamento": "2026-02-10", "valor_total": 60},
            {"ticker": "PETR4", "tipo_ativo": "acao", "status": "Pago", "data_pagamento": "2026-07-10", "valor_total": 40},
            {"ticker": "BBAS3", "tipo_ativo": "acao", "status": "Pago", "data_pagamento": "2026-06-10", "valor_total": 75},
            {"ticker": "VALE3", "tipo_ativo": "acao", "status": "Pago", "data_pagamento": "2026-12-10", "valor_total": 999},
            {"ticker": "HGLG11", "tipo_ativo": "fii", "status": "A receber", "data_pagamento": "2026-05-10", "valor_total": 888},
            {"ticker": "MXRF11", "tipo_ativo": "fii", "status": "Pago", "tipo_evento": "AMORTIZAÇÃO", "data_pagamento": "2026-05-10", "valor_total": 777},
            {"ticker": "KNRI11", "tipo_ativo": "fii", "status": "Pago", "data_pagamento": "2026-01-10", "valor_total": 50},
        ]
        script = helper + "\nconst result=getTopPaidDividendPayers(" + json.dumps(records) + ", [], '2026-11-22');\nprocess.stdout.write(JSON.stringify(result));"
        result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
        parsed = json.loads(result.stdout)
        self.assertEqual([item["ticker"] for item in parsed], ["PETR4", "BBAS3", "KNRI11"])
        self.assertEqual([item["total"] for item in parsed], [100, 75, 50])


if __name__ == "__main__":
    unittest.main()
