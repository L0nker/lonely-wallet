"""Independent regression checks for the multi-user / multi-portfolio contract.

These checks deliberately avoid importing the Flask application so they can run
in minimal CI environments without installing the web dependencies.
"""
import ast
import contextvars
import hashlib
import re
import sqlite3
import unicodedata
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "app" / "main.py"
SOURCE = MAIN.read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


def _function_node(name):
    for node in TREE.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"function not found: {name}")


def _load_functions(*names, extra=None):
    nodes = [_function_node(name) for name in names]
    module = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(module)
    scope = {
        "Path": Path,
        "DATA_ROOT": Path("/srv/lonely-data"),
        "DB_PATH": "/srv/lonely-data/ativos.db",
        "TOKEN_DB_PATH": "/srv/lonely-data/tokens.db",
        "ACTIVE_WALLET_CONTEXT": contextvars.ContextVar("test_wallet_context", default=None),
        "unicodedata": unicodedata,
        "re": re,
        "hashlib": hashlib,
    }
    if extra:
        scope.update(extra)
    exec(compile(module, str(MAIN), "exec"), scope)
    return scope


class MultiUserIsolationContractTests(unittest.TestCase):
    def test_wallet_database_paths_are_unique_per_user_and_wallet(self):
        scope = _load_functions("_active_wallet_context", "active_db_path", "active_token_db_path", "active_dashboard_cache_path", "_wallet_context_from_row")
        make_context = scope["_wallet_context_from_row"]
        first = make_context({
            "storage_key": "wallet-a", "user_id": "user-a", "id": "wallet-a",
            "name": "Padrão", "is_default": 1,
        })
        second = make_context({
            "storage_key": "wallet-b", "user_id": "user-a", "id": "wallet-b",
            "name": "Reserva", "is_default": 0,
        })
        third = make_context({
            "storage_key": "wallet-c", "user_id": "user-b", "id": "wallet-c",
            "name": "Padrão", "is_default": 1,
        })
        all_assets = {first["assets_db_path"], second["assets_db_path"], third["assets_db_path"]}
        all_tokens = {first["token_db_path"], second["token_db_path"], third["token_db_path"]}
        all_caches = {first["dashboard_cache_path"], second["dashboard_cache_path"], third["dashboard_cache_path"]}
        self.assertEqual(len(all_assets), 3)
        self.assertEqual(len(all_tokens), 3)
        self.assertEqual(len(all_caches), 3)
        self.assertIn("/users/user-a/wallets/wallet-a/ativos.db", first["assets_db_path"])
        self.assertIn("/users/user-a/wallets/wallet-b/tokens.db", second["token_db_path"])
        self.assertIn("/cache/user-b/wallet-c/", third["dashboard_cache_path"])

    def test_legacy_default_wallet_keeps_existing_database_pair(self):
        scope = _load_functions("_wallet_context_from_row")
        context = scope["_wallet_context_from_row"]({
            "storage_key": "legacy", "user_id": "admin-id", "id": "wallet-id",
            "name": "Padrão", "is_default": 1,
        })
        self.assertEqual(context["assets_db_path"], "/srv/lonely-data/ativos.db")
        self.assertEqual(context["token_db_path"], "/srv/lonely-data/tokens.db")

    def test_database_registry_enforces_username_and_wallet_name_uniqueness(self):
        node = _function_node("init_user_registry")
        sql = None
        for child in ast.walk(node):
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute) and child.func.attr == "executescript":
                if child.args and isinstance(child.args[0], ast.Constant):
                    sql = child.args[0].value
                    break
        self.assertIsNotNone(sql, "registry schema SQL not found")
        con = sqlite3.connect(":memory:")
        con.execute("PRAGMA foreign_keys=ON")
        con.executescript(sql)
        con.execute("INSERT INTO users(id,username,username_norm,pin_salt,pin_hash,role,created_at,updated_at) VALUES('u1','alice','alice','s','h','admin','now','now')")
        con.execute("INSERT INTO wallets(id,user_id,name,name_norm,storage_key,is_default,created_at,updated_at) VALUES('w1','u1','Padrão','padrão','w1',1,'now','now')")
        with self.assertRaises(sqlite3.IntegrityError):
            con.execute("INSERT INTO users(id,username,username_norm,pin_salt,pin_hash,role,created_at,updated_at) VALUES('u2','ALICE','alice','s','h','user','now','now')")
        with self.assertRaises(sqlite3.IntegrityError):
            con.execute("INSERT INTO wallets(id,user_id,name,name_norm,storage_key,is_default,created_at,updated_at) VALUES('w2','u1','padrão','padrão','w2',0,'now','now')")
        con.close()

    def test_new_portfolios_have_no_provider_credentials_and_clear_default_targets(self):
        self.assertIn("CREATE TABLE IF NOT EXISTS api_credentials", SOURCE)
        self.assertIn("DELETE FROM distribuicao_alvo", SOURCE)
        self.assertIn("_initialize_wallet_storage(context)", SOURCE)
        self.assertIn("storage_key='legacy' if legacy else None", SOURCE)
        self.assertIn("message='Usuário criado com uma carteira Padrão vazia e sem conexões Pluggy.'", SOURCE)

    def test_protected_account_creation_is_not_a_public_auth_endpoint(self):
        start = SOURCE.index("def require_user_authentication():")
        end = SOURCE.index("@app.route('/api/auth/status')", start)
        middleware = SOURCE[start:end]
        self.assertIn("'auth_status', 'auth_setup', 'auth_login', 'health', 'static'", middleware)
        self.assertNotIn("'auth_register'", middleware)
        start = SOURCE.index("def auth_register():")
        end = SOURCE.index("def auth_login():", start)
        register = SOURCE[start:end]
        self.assertIn("creator['role']", register)
        self.assertIn("!= 'admin'", register)
        self.assertIn("current_pin", register)

    def test_all_wallet_management_queries_are_scoped_to_session_user(self):
        for route_name in ("wallets_collection", "wallet_rename", "wallet_select"):
            node = _function_node(route_name)
            rendered = ast.unparse(node)
            self.assertIn("user_id", rendered, route_name)
        self.assertIn("WHERE id=? AND user_id=?", ast.unparse(_function_node("wallet_select")))
        self.assertIn("WHERE id=? AND user_id=?", ast.unparse(_function_node("wallet_rename")))

    def test_account_creation_and_wallet_creation_have_three_wallet_cap(self):
        wallets = ast.unparse(_function_node("wallets_collection"))
        auth_status = ast.unparse(_function_node("auth_status"))
        self.assertIn("count >= 3", wallets)
        self.assertIn("max_wallets=3", auth_status)

    def test_legacy_pin_setup_has_a_fixed_rate_limit_bucket(self):
        setup = ast.unparse(_function_node("auth_setup"))
        self.assertIn("__legacy_setup__", setup)
        self.assertIn("_identity_auth_failure", setup)
        self.assertIn("_identity_lock_response", setup)

    def test_identity_and_wallet_databases_and_existing_keys_are_restricted_to_owner(self):
        self.assertIn("os.chmod(USER_DB_PATH, 0o600)", SOURCE)
        self.assertIn("os.chmod(database_path, 0o600)", SOURCE)
        self.assertIn("os.chmod(token_path, 0o600)", SOURCE)
        self.assertIn("os.chmod(path, 0o600)", SOURCE)
        self.assertIn("os.chmod(key_path, 0o600)", SOURCE)
        self.assertIn("current.mkdir(exist_ok=True, mode=0o700)", SOURCE)

    def test_sensitive_pin_confirmations_share_persistent_rate_limit(self):
        self.assertIn("def _confirm_identity_pin(", SOURCE)
        for name in ("auth_register", "auth_change_pin", "auth_change_username"):
            self.assertIn("_confirm_identity_pin(", ast.unparse(_function_node(name)), name)
        self.assertIn("if new_pin != confirm_pin", ast.unparse(_function_node("auth_change_pin")))
        self.assertIn("confirm:confirmPin", (ROOT / "app" / "templates" / "index.html").read_text(encoding="utf-8"))

    def test_admin_account_panel_is_hidden_until_role_is_confirmed(self):
        html = (ROOT / "app" / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="securityAddUserPanel" hidden', html)
        self.assertIn("securityAddUserPanel').hidden=d.role!=='admin'", html)

    def test_sensitive_pin_gate_rejects_wrong_pin_and_resets_on_success(self):
        import hmac
        import re as regex
        state = {"locked": None, "failures": 0, "resets": 0}

        def lock_response(_con, _key):
            return state["locked"]

        def failure(_con, _key):
            state["failures"] += 1
            if state["failures"] >= 5:
                return 300
            return 0

        def reset(_con, _key):
            state["resets"] += 1

        def json_response(**kwargs):
            return kwargs

        scope = _load_functions(
            "_confirm_identity_pin",
            extra={
                "_identity_lock_response": lock_response,
                "_identity_auth_failure": failure,
                "_identity_auth_reset": reset,
                "pin_digest": lambda pin, salt: f"{salt}:{pin}",
                "re": regex,
                "hmac": hmac,
                "jsonify": json_response,
            },
        )
        verify = scope["_confirm_identity_pin"]
        self.assertEqual(verify(object(), "alice", "salt", "salt:123456", "000000", "PIN incorreto."), ({"ok":False,"error":"PIN incorreto."}, 401))
        self.assertEqual(verify(object(), "alice", "salt", "salt:123456", "123456", "PIN incorreto."), None)
        self.assertEqual(state["resets"], 1)
        state["locked"] = ({"ok":False,"error":"bloqueado"}, 429)
        self.assertEqual(verify(object(), "alice", "salt", "salt:123456", "123456", "PIN incorreto."), state["locked"])


class MultiUserRuntimeIsolationTests(unittest.TestCase):
    def test_financial_and_credential_databases_are_physically_isolated(self):
        import tempfile
        scope = _load_functions(
            "_active_wallet_context", "active_db_path", "active_token_db_path",
            "active_dashboard_cache_path", "_wallet_context_from_row"
        )
        base = Path(tempfile.mkdtemp(prefix="lw-user-wallet-test-"))
        scope["DATA_ROOT"] = base
        active_context = scope["ACTIVE_WALLET_CONTEXT"]
        make_context = scope["_wallet_context_from_row"]
        contexts = [
            make_context({"storage_key":"a", "user_id":"u1", "id":"w1", "name":"Padrão", "is_default":1}),
            make_context({"storage_key":"b", "user_id":"u1", "id":"w2", "name":"Reserva", "is_default":0}),
            make_context({"storage_key":"c", "user_id":"u2", "id":"w3", "name":"Padrão", "is_default":1}),
        ]
        try:
            for index, context in enumerate(contexts, start=1):
                Path(context["wallet_dir"]).mkdir(parents=True, exist_ok=True)
                token = active_context.set(context)
                try:
                    assets = sqlite3.connect(scope["active_db_path"]())
                    assets.execute("CREATE TABLE private_value(value TEXT)")
                    assets.execute("INSERT INTO private_value VALUES(?)", (f"owner-{index}",))
                    assets.commit(); assets.close()
                    secrets = sqlite3.connect(scope["active_token_db_path"]())
                    secrets.execute("CREATE TABLE api_credentials(provider TEXT PRIMARY KEY, token_encrypted TEXT NOT NULL)")
                    secrets.execute("INSERT INTO api_credentials VALUES('pluggy', ?)", (f"credential-{index}",))
                    secrets.commit(); secrets.close()
                finally:
                    active_context.reset(token)
            for index, context in enumerate(contexts, start=1):
                token = active_context.set(context)
                try:
                    assets = sqlite3.connect(scope["active_db_path"]())
                    self.assertEqual(assets.execute("SELECT value FROM private_value").fetchone()[0], f"owner-{index}")
                    assets.close()
                    secrets = sqlite3.connect(scope["active_token_db_path"]())
                    self.assertEqual(secrets.execute("SELECT token_encrypted FROM api_credentials WHERE provider='pluggy'").fetchone()[0], f"credential-{index}")
                    secrets.close()
                finally:
                    active_context.reset(token)
        finally:
            import shutil
            shutil.rmtree(base, ignore_errors=True)

    def test_startup_runs_schema_migrations_for_every_nonlegacy_wallet(self):
        source = SOURCE
        self.assertIn("def prepare_registered_wallet_databases():", source)
        self.assertIn("for context in _all_wallet_contexts():", source)
        self.assertIn("if not context or context.get('storage_key') == 'legacy':", source)
        self.assertIn("init_db()", ast.unparse(_function_node("prepare_registered_wallet_databases")))
        self.assertIn("migrate_api_credentials()", ast.unparse(_function_node("prepare_registered_wallet_databases")))
        self.assertLess(source.index("init_user_registry()"), source.index("prepare_registered_wallet_databases()"))
        self.assertLess(source.index("\nprepare_registered_wallet_databases()\n"), source.index("@app.before_request\ndef require_user_authentication"))

    def test_additional_wallets_do_not_receive_default_distribution_targets(self):
        init_db = ast.unparse(_function_node("init_db"))
        self.assertIn("seed_legacy_targets = not wallet_context or wallet_context.get('storage_key') == 'legacy'", init_db)
        self.assertIn("if count == 0 and seed_legacy_targets", init_db)

    def test_login_validates_username_and_equalizes_unknown_user_hash_path(self):
        login = ast.unparse(_function_node("auth_login"))
        self.assertIn("_validate_username", login)
        self.assertIn("_DUMMY_AUTH_SALT", login)
        self.assertIn("_DUMMY_AUTH_HASH", login)
        self.assertIn("pin_digest(pin if pin_shape_valid else '000000'", login)

    def test_per_ip_lockout_cannot_be_bypassed_by_changing_usernames(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from types import SimpleNamespace
        import time as time_module

        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute("""CREATE TABLE auth_attempts(
            client_ip TEXT NOT NULL, username_norm TEXT NOT NULL,
            failed_attempts INTEGER NOT NULL DEFAULT 0, locked_until REAL NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL, PRIMARY KEY(client_ip,username_norm))""")
        fixed_now = datetime.now(ZoneInfo("America/Sao_Paulo")).timestamp()
        scope = _load_functions(
            "_identity_auth_failure", "_identity_lock_response",
            extra={
                "time": SimpleNamespace(time=lambda: fixed_now),
                "datetime": datetime,
                "timedelta": __import__("datetime").timedelta,
                "TZ": ZoneInfo("America/Sao_Paulo"),
                "_identity_client_ip": lambda: "192.0.2.10",
                "jsonify": lambda **kwargs: kwargs,
            },
        )
        fail = scope["_identity_auth_failure"]
        lock = scope["_identity_lock_response"]
        for index in range(19):
            self.assertEqual(fail(connection, f"random-user-{index}", now=fixed_now), 0)
        self.assertEqual(fail(connection, "random-user-19", now=fixed_now), fixed_now + 300)
        response = lock(connection, "another-random-user", now=fixed_now)
        self.assertEqual(response[1], 429)
        connection.close()

    def test_auth_attempt_history_cleanup_keeps_recent_rows_and_removes_old_rows(self):
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        from types import SimpleNamespace

        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute("""CREATE TABLE auth_attempts(
            client_ip TEXT NOT NULL, username_norm TEXT NOT NULL,
            failed_attempts INTEGER NOT NULL DEFAULT 0, locked_until REAL NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL, PRIMARY KEY(client_ip,username_norm))""")
        zone = ZoneInfo("America/Sao_Paulo")
        now_dt = datetime.now(zone)
        connection.execute(
            "INSERT INTO auth_attempts VALUES(?,?,?,?,?)",
            ("192.0.2.1", "stale-user", 1, 0, (now_dt - timedelta(days=2)).isoformat())
        )
        connection.execute(
            "INSERT INTO auth_attempts VALUES(?,?,?,?,?)",
            ("192.0.2.2", "recent-user", 1, 0, (now_dt - timedelta(hours=1)).isoformat())
        )
        fixed_now = now_dt.timestamp()
        scope = _load_functions(
            "_identity_auth_failure",
            extra={
                "time": SimpleNamespace(time=lambda: fixed_now),
                "datetime": datetime,
                "timedelta": timedelta,
                "TZ": zone,
                "_identity_client_ip": lambda: "192.0.2.3",
            },
        )
        scope["_identity_auth_failure"](connection, "alice", now=fixed_now)
        remaining = {
            (row["client_ip"], row["username_norm"])
            for row in connection.execute("SELECT client_ip,username_norm FROM auth_attempts")
        }
        self.assertNotIn(("192.0.2.1", "stale-user"), remaining)
        self.assertIn(("192.0.2.2", "recent-user"), remaining)
        self.assertIn(("192.0.2.3", "alice"), remaining)
        connection.close()

    def test_registration_attempt_log_is_pruned_before_new_attempt(self):
        register = ast.unparse(_function_node("_create_additional_user"))
        self.assertIn("DELETE FROM registration_attempts WHERE created_at < ?", register)
        self.assertIn("now_epoch - 86400", register)
        self.assertIn("now_epoch - 3600", register)
        self.assertIn("con.commit()", register)


if __name__ == "__main__":
    unittest.main()
