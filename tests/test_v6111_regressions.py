from pathlib import Path
import sqlite3
import unittest

ROOT = Path(__file__).resolve().parents[1]
MAIN = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
HTML = (ROOT / "app" / "templates" / "index.html").read_text(encoding="utf-8")


class V6111RegressionTests(unittest.TestCase):
    def test_username_normalized_unique_index_is_created_for_existing_registries(self):
        self.assertIn("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username_norm ON users(username_norm)", MAIN)
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE users(id TEXT PRIMARY KEY, username_norm TEXT NOT NULL)")
        con.execute("CREATE UNIQUE INDEX idx_users_username_norm ON users(username_norm)")
        con.execute("INSERT INTO users VALUES('1','kelvin')")
        with self.assertRaises(sqlite3.IntegrityError):
            con.execute("INSERT INTO users VALUES('2','kelvin')")
        con.close()

    def test_register_and_username_change_check_duplicates_after_pin_confirmation(self):
        register = MAIN[MAIN.index("def auth_register():"):MAIN.index("@app.route('/api/users', methods=['GET'])")]
        self.assertIn("Esse nome de usuário já está em uso. Escolha outro nome.", register)
        self.assertLess(register.index("if pin_response:"), register.index("duplicate = con.execute"))
        change = MAIN[MAIN.index("def auth_change_username():"):MAIN.index("@app.route('/api/wallets', methods=['GET', 'POST'])")]
        self.assertIn("WHERE username_norm=? AND id<>?", change)
        self.assertLess(change.index("if pin_response:"), change.index("duplicate = con.execute"))

    def test_wallet_editor_modal_visibility_is_implemented(self):
        self.assertIn(".modalback.open{display:flex!important}", HTML)
        self.assertIn("back.classList.add('open')", HTML)
        self.assertIn("walletEditorForm.addEventListener('submit',submitWalletEditor);", HTML)
        self.assertIn("const url=isRename?('/api/wallets/'+encodeURIComponent(picker.dataset.activeWalletId)+'/rename'):'/api/wallets'", HTML)

    def test_pluggy_provider_card_uses_same_border_token_as_other_api_cards(self):
        self.assertIn(".api-provider-card.api-pluggy{border-color:var(--line)!important;box-shadow:none;outline:0}", HTML)


if __name__ == "__main__":
    unittest.main()
