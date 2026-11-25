"""Regression checks for safe deletion of additional portfolio wallets."""
import ast
import json
import os
import re
import shutil
import tempfile
import unittest
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
MAIN_PATH = ROOT / "app" / "main.py"
TEMPLATE_PATH = ROOT / "app" / "templates" / "index.html"
SOURCE = MAIN_PATH.read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)
TEMPLATE = TEMPLATE_PATH.read_text(encoding="utf-8")


def function_node(name):
    for node in TREE.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"Function {name} not found")


def load_function(name, **globals_):
    node = function_node(name)
    module = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(module)
    scope = dict(globals_)
    exec(compile(module, str(MAIN_PATH), "exec"), scope)
    return scope[name]


class WalletDeletionContractTests(unittest.TestCase):
    def test_delete_endpoint_is_owner_scoped_and_protects_default_wallet(self):
        source = ast.unparse(function_node("wallet_delete"))
        self.assertIn("session.get('user_id')", source)
        self.assertIn("WHERE id=? AND user_id=?", source)
        self.assertIn("bool(wallet['is_default'])", source)
        self.assertIn("storage_key", source)
        self.assertIn("_confirm_identity_pin", source)
        self.assertIn("_stage_wallet_storage", source)
        self.assertIn("DELETE FROM wallets", source)
        self.assertIn("UPDATE users SET last_wallet_id", source)
        self.assertIn("_clear_runtime_state_for_deleted_wallet", source)

    def test_wallet_staging_removes_only_wallet_database_and_cache_not_shared_token_vault(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wallet_dir = root / "users" / "u1" / "wallets" / "w2"
            wallet_dir.mkdir(parents=True)
            (wallet_dir / "ativos.db").write_bytes(b"wallet financial data")
            cache_dir = root / "cache" / "u1" / "w2"
            cache_dir.mkdir(parents=True)
            (cache_dir / "dashboard_cache.json").write_text("{}", encoding="utf-8")
            token_vault = root / "users" / "u1" / "tokens.db"
            token_vault.parent.mkdir(parents=True, exist_ok=True)
            token_vault.write_bytes(b"shared user API tokens")

            scope = {
                "DATA_ROOT": root,
                "Path": Path,
                "os": os,
                "re": re,
                "uuid": uuid,
                "json": json,
                "datetime": datetime,
                "TZ": ZoneInfo("America/Sao_Paulo"),
                "shutil": shutil,
                "_deletion_stage_root": load_function(
                    "_deletion_stage_root", DATA_ROOT=root, Path=Path, os=os
                ),
            }
            scope["_stage_wallet_storage"] = load_function("_stage_wallet_storage", **scope)
            scope["_restore_staged_account_storage"] = load_function(
                "_restore_staged_account_storage", **scope
            )
            scope["_finalize_staged_account_storage"] = load_function(
                "_finalize_staged_account_storage", **scope
            )
            context = {
                "user_id": "u1",
                "wallet_id": "w2",
                "storage_key": "w2",
                "wallet_dir": str(wallet_dir),
                "dashboard_cache_path": str(cache_dir / "dashboard_cache.json"),
            }

            stage = scope["_stage_wallet_storage"](context)
            self.assertFalse(wallet_dir.exists())
            self.assertFalse(cache_dir.exists())
            self.assertTrue(token_vault.is_file())
            scope["_restore_staged_account_storage"](stage)
            self.assertTrue((wallet_dir / "ativos.db").is_file())
            self.assertTrue((cache_dir / "dashboard_cache.json").is_file())
            self.assertEqual(token_vault.read_bytes(), b"shared user API tokens")

    def test_wallet_staging_rejects_legacy_default_wallet(self):
        stage = load_function(
            "_stage_wallet_storage",
            DATA_ROOT=Path("/tmp/lonely-wallet-test"),
            Path=Path,
            os=os,
            re=re,
            uuid=uuid,
            json=json,
            datetime=datetime,
            TZ=ZoneInfo("America/Sao_Paulo"),
            shutil=shutil,
        )
        with self.assertRaisesRegex(ValueError, "Padrão"):
            stage({"storage_key": "legacy"})

    def test_wallet_recovery_uses_wallet_registry_presence(self):
        recovery = ast.unparse(function_node("recover_pending_account_deletions"))
        self.assertIn("scope_type", recovery)
        self.assertIn("SELECT 1 FROM wallets WHERE id=? AND user_id=?", recovery)
        self.assertIn("_restore_staged_account_storage", recovery)
        self.assertIn("_finalize_staged_account_storage", recovery)

    def test_wallet_picker_exposes_delete_only_for_non_default_wallets(self):
        self.assertIn("id=\"deleteWalletAction\"", TEMPLATE)
        self.assertIn("deleteAction.hidden=!active||active.is_default===true", TEMPLATE)
        self.assertIn("async function deleteSelectedWallet()", TEMPLATE)
        self.assertIn("/api/wallets/'+encodeURIComponent(walletId)", TEMPLATE)

    def test_delete_confirmations_use_project_theme_dialog_not_native_prompts(self):
        self.assertIn("function appPrompt(", TEMPLATE)
        self.assertIn("function appNotice(", TEMPLATE)
        self.assertIn("async function deleteOwnAccount()", TEMPLATE)
        self.assertIn("async function deleteManagedUser(user)", TEMPLATE)
        self.assertNotIn("window.confirm(warning)", TEMPLATE)
        self.assertNotIn("window.prompt('Digite seu PIN atual para autorizar a exclusão:'", TEMPLATE)

    def test_logout_button_is_in_header_and_not_pin_settings(self):
        header = TEMPLATE[TEMPLATE.index('<header class="top">'):TEMPLATE.index('</header>', TEMPLATE.index('<header class="top">'))]
        self.assertIn('class="ghost logout-top-btn"', header)
        self.assertIn('onclick="securityLogout()"', header)
        self.assertNotIn('Sair e bloquear</button>', TEMPLATE[TEMPLATE.index('id="securityPinActions"'):TEMPLATE.index('id="securityPinMessage"')])


if __name__ == "__main__":
    unittest.main()
