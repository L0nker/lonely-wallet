"""Account deletion contract and filesystem-scope regression tests."""
import ast
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import unittest
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "app" / "main.py"
SOURCE = MAIN.read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


def node(name):
    for item in TREE.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name:
            return item
    raise AssertionError(f"function not found: {name}")


def load(*names, **values):
    scope = {
        "Path": Path,
        "os": os,
        "re": re,
        "json": json,
        "shutil": shutil,
        "uuid": uuid,
        "datetime": datetime,
        "TZ": ZoneInfo("America/Sao_Paulo"),
        "DATA_ROOT": Path("/tmp/lonely-test-data"),
        "USER_DB_PATH": "/tmp/lonely-test-data/users.db",
        "app": SimpleNamespace(logger=logging.getLogger("account-deletion-tests")),
    }
    scope.update(values)
    module = ast.Module(body=[node(name) for name in names], type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(MAIN), "exec"), scope)
    return scope


class AccountDeletionFilesystemTests(unittest.TestCase):
    def test_user_deletion_erases_all_owned_databases_but_preserves_other_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "users" / "alice" / "wallets" / "wallet-1").mkdir(parents=True)
            (root / "users" / "alice" / "tokens.db").write_bytes(b"alice tokens")
            (root / "users" / "alice" / "wallets" / "wallet-1" / "ativos.db").write_bytes(b"alice assets")
            (root / "cache" / "alice" ).mkdir(parents=True)
            (root / "cache" / "alice" / "dashboard_cache.json").write_text("{}")
            (root / "users" / "bob" / "wallets" / "wallet-2").mkdir(parents=True)
            (root / "users" / "bob" / "tokens.db").write_bytes(b"bob tokens")
            (root / "users" / "bob" / "wallets" / "wallet-2" / "ativos.db").write_bytes(b"bob assets")
            (root / "users.db").write_bytes(b"identity registry")
            scope = load("_deletion_stage_root", "_stage_account_storage", "_finalize_staged_account_storage",
                         DATA_ROOT=root, USER_DB_PATH=str(root / "users.db"))
            stage = scope["_stage_account_storage"]("alice", reset_installation=False)
            self.assertFalse((root / "users" / "alice").exists())
            self.assertFalse((root / "cache" / "alice").exists())
            self.assertTrue((root / "users" / "bob" / "wallets" / "wallet-2" / "ativos.db").is_file())
            self.assertTrue((root / "users.db").is_file())
            scope["_finalize_staged_account_storage"](stage)
            self.assertFalse((root / "users" / "alice").exists())
            self.assertFalse((root / "cache" / "alice").exists())
            self.assertTrue((root / "users" / "bob" / "tokens.db").is_file())

    def test_master_reset_stages_all_installation_files_but_keeps_empty_identity_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for rel in ("ativos.db", "tokens.db", "carteira.db", ".api-credentials.key",
                        "users/alice/tokens.db", "users/bob/wallets/w2/ativos.db",
                        "cache/alice/dashboard_cache.json", "backups/backup-1/users.db"):
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"private")
            identity = root / "users.db"
            identity.write_bytes(b"empty registry schema")
            scope = load("_deletion_stage_root", "_stage_account_storage", "_finalize_staged_account_storage",
                         DATA_ROOT=root, USER_DB_PATH=str(identity))
            stage = scope["_stage_account_storage"]("master-id", reset_installation=True)
            self.assertTrue(identity.is_file())
            self.assertFalse((root / "ativos.db").exists())
            self.assertFalse((root / "users").exists())
            self.assertFalse((root / "backups").exists())
            scope["_finalize_staged_account_storage"](stage)
            self.assertEqual([p.name for p in root.iterdir()], ["users.db"])

    def test_failed_deletion_can_restore_staged_user_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / "users" / "u-123" / "wallets" / "w1" / "ativos.db"
            original.parent.mkdir(parents=True)
            original.write_bytes(b"portfolio")
            scope = load("_deletion_stage_root", "_stage_account_storage", "_restore_staged_account_storage",
                         DATA_ROOT=root, USER_DB_PATH=str(root / "users.db"))
            stage = scope["_stage_account_storage"]("u-123", reset_installation=False)
            self.assertFalse(original.exists())
            scope["_restore_staged_account_storage"](stage)
            self.assertEqual(original.read_bytes(), b"portfolio")

    def test_master_succession_stages_only_master_legacy_databases_and_preserves_global_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files = (
                "ativos.db", "tokens.db", "carteira.db", "dashboard_cache.json",
                ".api-credentials.key", ".lonely-wallet-session.key",
                "users/master-1/tokens.db", "users/master-1/wallets/w1/ativos.db",
                "cache/master-1/dashboard_cache.json",
                "users/next-user/tokens.db", "users/next-user/wallets/w2/ativos.db",
                "users.db",
            )
            for rel in files:
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(rel.encode())
            scope = load(
                "_deletion_stage_root", "_stage_account_storage", "_restore_staged_account_storage",
                DATA_ROOT=root, USER_DB_PATH=str(root / "users.db"),
                DB_PATH=str(root / "ativos.db"), LEGACY_DB_PATH=str(root / "carteira.db"),
                TOKEN_DB_PATH=str(root / "tokens.db"), DASHBOARD_CACHE_PATH=str(root / "dashboard_cache.json"),
            )
            stage = scope["_stage_account_storage"]("master-1", reset_installation=False, include_legacy_data=True)
            for rel in ("ativos.db", "tokens.db", "carteira.db", "dashboard_cache.json", "users/master-1", "cache/master-1"):
                self.assertFalse((root / rel).exists(), rel)
            for rel in (".api-credentials.key", ".lonely-wallet-session.key", "users/next-user/tokens.db", "users/next-user/wallets/w2/ativos.db", "users.db"):
                self.assertTrue((root / rel).exists(), rel)
            scope["_restore_staged_account_storage"](stage)
            self.assertTrue((root / "ativos.db").is_file())
            self.assertTrue((root / "users/master-1/wallets/w1/ativos.db").is_file())
            self.assertTrue((root / ".api-credentials.key").is_file())

    def test_master_legacy_alias_never_stages_identity_database_or_sidecars(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            identity = root / "users.db"
            identity.write_bytes(b"identity database")
            (root / "users.db-wal").write_bytes(b"identity wal")
            (root / "ativos.db").write_bytes(b"legacy assets")
            scope = load(
                "_deletion_stage_root", "_stage_account_storage", "_restore_staged_account_storage",
                DATA_ROOT=root, USER_DB_PATH=str(identity), DB_PATH=str(identity),
                LEGACY_DB_PATH=str(root / "ativos.db"), TOKEN_DB_PATH=str(identity),
                DASHBOARD_CACHE_PATH=str(root / "dashboard_cache.json"),
            )
            stage = scope["_stage_account_storage"]("master-id", reset_installation=False, include_legacy_data=True)
            self.assertTrue(identity.is_file())
            self.assertTrue((root / "users.db-wal").is_file())
            self.assertFalse((root / "ativos.db").exists())
            scope["_restore_staged_account_storage"](stage)
            self.assertTrue((root / "ativos.db").is_file())
            self.assertEqual(identity.read_bytes(), b"identity database")

    def test_symlink_parent_cannot_redirect_user_database_deletion_outside_data_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "data"
            outside = Path(tmp) / "outside"
            root.mkdir(); outside.mkdir()
            victim = outside / "user-a"
            victim.mkdir()
            (victim / "ativos.db").write_bytes(b"do not delete")
            (root / "users").symlink_to(outside, target_is_directory=True)
            scope = load("_deletion_stage_root", "_stage_account_storage", DATA_ROOT=root,
                         USER_DB_PATH=str(root / "users.db"))
            with self.assertRaises(RuntimeError):
                scope["_stage_account_storage"]("user-a", reset_installation=False)
            self.assertEqual((victim / "ativos.db").read_bytes(), b"do not delete")


class MasterSuccessionTests(unittest.TestCase):
    def test_master_delete_promotes_oldest_remaining_account_and_invalidates_session(self):
        scope = load("_delete_master_and_promote_successor", sqlite3=__import__("sqlite3"))
        con = __import__("sqlite3").connect(":memory:")
        con.row_factory = __import__("sqlite3").Row
        con.executescript("""
            CREATE TABLE users(id TEXT PRIMARY KEY, username TEXT, username_norm TEXT, role TEXT,
                auth_version INTEGER, created_at TEXT, updated_at TEXT);
            CREATE TABLE auth_attempts(username_norm TEXT);
            CREATE UNIQUE INDEX idx_users_single_master ON users(role) WHERE role='master';
            CREATE TRIGGER protect_master_delete BEFORE DELETE ON users WHEN OLD.role='master'
                BEGIN SELECT RAISE(ABORT, 'protected'); END;
            CREATE TRIGGER protect_master_role BEFORE UPDATE OF role ON users WHEN OLD.role='master' AND NEW.role<>'master'
                BEGIN SELECT RAISE(ABORT, 'protected'); END;
        """)
        con.executemany("INSERT INTO users VALUES(?,?,?,?,?,?,?)", [
            ("master-1", "owner", "owner", "master", 4, "2026-01-01", "2026-01-01"),
            ("admin-1", "admin", "admin", "admin", 2, "2026-02-01", "2026-02-01"),
            ("user-1", "member", "member", "user", 1, "2026-03-01", "2026-03-01"),
        ])
        con.executemany("INSERT INTO auth_attempts VALUES(?)", [("owner",), ("admin",), ("member",)])
        con.commit()
        result = scope["_delete_master_and_promote_successor"](con, "master-1", now="2026-11-25T12:00:00-03:00")
        con.commit()
        self.assertEqual(result["id"], "admin-1")
        self.assertEqual(result["username"], "admin")
        self.assertEqual(result["auth_version"], 3)
        rows = con.execute("SELECT id,role,auth_version FROM users ORDER BY created_at,id").fetchall()
        self.assertEqual([(r["id"], r["role"]) for r in rows], [("admin-1", "master"), ("user-1", "user")])
        self.assertEqual(con.execute("SELECT COUNT(*) FROM users WHERE role='master'").fetchone()[0], 1)
        self.assertEqual([row[0] for row in con.execute("SELECT username_norm FROM auth_attempts ORDER BY username_norm").fetchall()], ["member"])
        with self.assertRaises(__import__("sqlite3").IntegrityError):
            con.execute("UPDATE users SET role='user' WHERE id='admin-1'")
        with self.assertRaises(__import__("sqlite3").IntegrityError):
            con.execute("DELETE FROM users WHERE id='admin-1'")
        con.close()

    def test_master_promotion_helper_returns_none_if_no_successor_exists(self):
        scope = load("_delete_master_and_promote_successor", sqlite3=__import__("sqlite3"))
        con = __import__("sqlite3").connect(":memory:")
        con.row_factory = __import__("sqlite3").Row
        con.executescript("""
            CREATE TABLE users(id TEXT PRIMARY KEY, username TEXT, username_norm TEXT, role TEXT,
                auth_version INTEGER, created_at TEXT, updated_at TEXT);
            CREATE TABLE auth_attempts(username_norm TEXT);
            INSERT INTO users VALUES('master','owner','owner','master',1,'2026-01-01','2026-01-01');
        """)
        self.assertIsNone(scope["_delete_master_and_promote_successor"](con, "master"))
        self.assertEqual(con.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)
        self.assertEqual(con.execute("SELECT role FROM users WHERE id='master'").fetchone()[0], "master")
        con.close()


class AccountDeletionContractTests(unittest.TestCase):
    def test_managed_self_delete_route_redirects_users_to_self_service_settings(self):
        source = ast.unparse(node("users_management_delete"))
        self.assertIn("Configurações → Excluir minha conta", source)
        self.assertNotIn("disponível somente ao Master", source)

    def test_all_roles_can_delete_self_and_master_can_transfer_or_reset(self):
        source = ast.unparse(node("auth_delete_own_account"))
        self.assertIn("actor_role not in ('master', 'admin', 'user')", source)
        self.assertIn("ORDER BY created_at,id LIMIT 1", source)
        self.assertIn("EXCLUIR MINHA CONTA", source)
        self.assertIn("_confirm_identity_pin", source)
        self.assertIn("APAGAR TODA A INSTALAÇÃO", source)
        self.assertIn("DELETE FROM users WHERE id=?", source)
        self.assertIn("_stage_account_storage", source)
        self.assertIn("include_legacy_data=include_legacy_data", source)
        self.assertIn("con.execute('DELETE FROM users')", source)
        self.assertIn("session.clear()", source)

    def test_self_delete_endpoint_allows_all_three_roles_but_scopes_non_master_data(self):
        source = ast.unparse(node("auth_delete_own_account"))
        self.assertIn("actor_role not in ('master', 'admin', 'user')", source)
        self.assertIn("required_phrase = 'EXCLUIR MINHA CONTA'", source)
        self.assertIn("_stage_account_storage", source)
        self.assertIn("con.execute('DELETE FROM users WHERE id=?', (actor_id,))", source)
        self.assertIn("all_users=reset_installation", source)
        self.assertIn("As demais contas, carteiras, conexões Pluggy e credenciais próprias foram preservadas", source)

    def test_master_self_delete_transfers_master_to_oldest_remaining_account(self):
        source = ast.unparse(node("auth_delete_own_account"))
        self.assertIn("ORDER BY created_at,id LIMIT 1", source)
        self.assertIn("required_phrase = 'TRANSFERIR MASTER E EXCLUIR MINHA CONTA'", source)
        self.assertIn("_delete_master_and_promote_successor", source)
        self.assertIn("required_phrase = 'APAGAR TODA A INSTALAÇÃO'", source)
        self.assertIn("A conta mais antiga restante", source)

    def test_managed_deletion_removes_scoped_storage_and_enforces_role_matrix(self):
        source = ast.unparse(node("users_management_delete"))
        self.assertIn("target_user_id == actor_id", source)
        self.assertIn("_user_management_permission(actor_role, target_role, 'delete')", source)
        self.assertIn("_stage_account_storage(str(target_user_id), reset_installation=False)", source)
        self.assertIn("con.execute('DELETE FROM users WHERE id=?', (str(target_user_id),))", source)

    def test_ui_explains_personal_and_master_deletion_scopes(self):
        html = (ROOT / "app/templates/index.html").read_text(encoding="utf-8")
        self.assertIn('id="securityDeleteAccountButton"', html)
        self.assertIn("EXCLUIR MINHA CONTA", html)
        self.assertIn("{% if current_user_role == 'master' %}", html)
        self.assertIn("APAGAR TODA A INSTALAÇÃO", html)
        self.assertIn("conta mais antiga restante será promovida automaticamente a Master", html)
        self.assertIn("ativos.db", html)
        self.assertIn("tokens.db", html)
        self.assertIn("/api/account/self", html)

    def test_database_and_backup_paths_are_included_in_master_wipe(self):
        source = ast.unparse(node("_stage_account_storage"))
        self.assertIn("for child in DATA_ROOT.iterdir()", source)
        self.assertIn(".deletion-staging", source)
        self.assertIn("USER_DB_PATH", source)
        backup = (ROOT / "scripts/backup.py").read_text(encoding="utf-8")
        self.assertIn('"backups"', backup)

    def test_managed_account_deletion_also_removes_auth_attempt_history(self):
        source = ast.unparse(node("users_management_delete"))
        self.assertIn("DELETE FROM auth_attempts WHERE username_norm=?", source)
        self.assertIn("_clear_runtime_state_for_deleted_account(user_id=str(target_user_id))", source)

    def test_cached_database_connections_recheck_account_deletion_marker(self):
        self.assertIn("_assert_wallet_context_not_deleting(context)", ast.unparse(node("db")))
        self.assertIn("_assert_wallet_context_not_deleting(context)", ast.unparse(node("token_db")))

    def test_managed_credential_reset_clears_old_auth_lockout(self):
        source = ast.unparse(node("users_management_update"))
        self.assertIn("DELETE FROM auth_attempts WHERE username_norm=?", source)
        self.assertIn("if new_username is not None or new_pin", source)

    def test_self_deletion_clears_user_or_installation_runtime_caches(self):
        source = ast.unparse(node("auth_delete_own_account"))
        self.assertIn("_clear_runtime_state_for_deleted_account(user_id=actor_id, all_users=reset_installation)", source)

    def test_deleted_user_runtime_caches_are_removed_without_touching_other_users(self):
        scope = load(
            "_clear_runtime_state_for_deleted_account",
            _SCOPED_WALLET_STATE={
                "alice:w1": {"pluggy_auth": {"api_key":"secret-a"}, "proventos_cache": {"result":"a"}},
                "alice:w2": {"pluggy_auth": {"api_key":"secret-b"}, "portfolio_history_cache": {"x":1}},
                "bob:w1": {"pluggy_auth": {"api_key":"secret-c"}, "proventos_cache": {"result":"c"}},
            },
            _SCOPED_WALLET_STATE_LOCK=threading.RLock(),
            _SCOPED_USER_STATE={"alice":{"pluggy_auth":{"api_key":"alice-token"}},"bob":{"pluggy_auth":{"api_key":"bob-token"}}},
            _SCOPED_USER_STATE_LOCK=threading.RLock(),
        )
        scope["_clear_runtime_state_for_deleted_account"](user_id="alice")
        states = scope["_SCOPED_WALLET_STATE"]
        self.assertEqual(set(states), {"bob:w1"})
        user_states = scope["_SCOPED_USER_STATE"]
        self.assertEqual(set(user_states), {"bob"})
        self.assertEqual(user_states["bob"]["pluggy_auth"]["api_key"], "bob-token")

    def test_master_runtime_cache_clear_erases_all_wallet_tokens_and_states(self):
        scope = load(
            "_clear_runtime_state_for_deleted_account",
            _SCOPED_WALLET_STATE={"alice:w1":{"pluggy_auth":{"api_key":"a"}},"bob:w1":{"pluggy_auth":{"api_key":"b"}}},
            _SCOPED_WALLET_STATE_LOCK=threading.RLock(),
            _SCOPED_USER_STATE={"alice":{"pluggy_auth":{"api_key":"a"}},"bob":{"pluggy_auth":{"api_key":"b"}}},
            _SCOPED_USER_STATE_LOCK=threading.RLock(),
            CACHE={"portfolio":"value"}, PORTFOLIO_HISTORY_CACHE={"alice":"value"},
            SGS_CACHE={"rates":"value"}, BENCHMARK_CACHE={"time":1,"data":{"x":1}},
            PROVENTOS_SYNC_CACHE={"time":1,"result":{"x":1},"signature":"x"},
            PROVENTOS_SYNC_STATE={"status":"running","inputs":{"private":1}},
            PERFORMANCE_HISTORY_STATE={"status":"running","result":{"private":1}},
            PLUGGY_AUTH_CACHE={"api_key":"legacy-secret","expires_at":999},
            _ASSET_CLASS_CACHE={"alice":"private"},
            PROVENTOS_SYNC_PENDING_INPUTS={"private":1},
            PERFORMANCE_HISTORY_PENDING_INPUTS={"private":1},
        )
        scope["_clear_runtime_state_for_deleted_account"](all_users=True)
        self.assertEqual(scope["_SCOPED_WALLET_STATE"], {})
        self.assertEqual(scope["_SCOPED_USER_STATE"], {})
        self.assertEqual(scope["CACHE"], {})
        self.assertEqual(scope["PORTFOLIO_HISTORY_CACHE"], {})
        self.assertEqual(scope["SGS_CACHE"], {})
        self.assertEqual(scope["BENCHMARK_CACHE"], {"time":0,"data":{}})
        self.assertEqual(scope["PROVENTOS_SYNC_CACHE"], {"time":0,"result":None,"signature":None})
        self.assertEqual(scope["PROVENTOS_SYNC_STATE"]["status"], "idle")
        self.assertEqual(scope["PERFORMANCE_HISTORY_STATE"]["status"], "idle")
        self.assertEqual(scope["PLUGGY_AUTH_CACHE"], {})
        self.assertEqual(scope["_ASSET_CLASS_CACHE"], {})
        self.assertIsNone(scope["PROVENTOS_SYNC_PENDING_INPUTS"])
        self.assertIsNone(scope["PERFORMANCE_HISTORY_PENDING_INPUTS"])


if __name__ == "__main__":
    unittest.main()
