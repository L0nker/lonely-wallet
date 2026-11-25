"""Regression tests for the Master/Admin/User authorization model.

The tests avoid importing Flask so they run in the project's minimal CI context.
"""
import ast
import sqlite3
import tempfile
import unittest
import uuid
import secrets
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
MAIN_PATH = ROOT / "app" / "main.py"
SOURCE = MAIN_PATH.read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


def function_node(name):
    for node in TREE.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"Function {name} not found")


def load_function(name):
    node = function_node(name)
    module = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(module)
    scope = {}
    exec(compile(module, str(MAIN_PATH), "exec"), scope)
    return scope[name]


class UserRoleHierarchyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.permission = staticmethod(load_function("_user_management_permission"))
        cls.ensure_master = staticmethod(load_function("_ensure_single_master"))

    def test_master_can_manage_and_delete_users_and_admins_but_not_master(self):
        for target in ("user", "admin"):
            for action in ("view", "reset_pin", "rename", "change_role", "delete"):
                self.assertTrue(self.permission("master", target, action))
        for action in ("view", "reset_pin", "rename", "change_role", "delete"):
            self.assertFalse(self.permission("master", "master", action))

    def test_admin_can_only_list_reset_pin_and_delete_common_users(self):
        for action in ("view", "reset_pin", "delete"):
            self.assertTrue(self.permission("admin", "user", action))
        for action in ("rename", "change_role"):
            self.assertFalse(self.permission("admin", "user", action))
        for target in ("admin", "master"):
            for action in ("view", "reset_pin", "rename", "change_role", "delete"):
                self.assertFalse(self.permission("admin", target, action))

    def test_master_can_promote_or_demote_any_non_master_account(self):
        for target in ("user", "admin"):
            self.assertTrue(self.permission("master", target, "view"))
            self.assertTrue(self.permission("master", target, "reset_pin"))
            self.assertTrue(self.permission("master", target, "change_role"))
            self.assertTrue(self.permission("master", target, "delete"))
        self.assertFalse(self.permission("master", "master", "change_role"))

    def test_common_user_cannot_view_manage_or_delete_any_account(self):
        for target in ("master", "admin", "user"):
            for action in ("view", "reset_pin", "rename", "change_role", "delete"):
                self.assertFalse(self.permission("user", target, action))

    def test_invalid_roles_and_actions_are_denied_by_default(self):
        self.assertFalse(self.permission("owner", "user", "reset_pin"))
        self.assertFalse(self.permission("admin", "user", "promote_to_master"))
        self.assertFalse(self.permission("", "", "delete"))

    def test_oldest_existing_account_is_promoted_to_single_master(self):
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.execute("CREATE TABLE users(id TEXT PRIMARY KEY, role TEXT NOT NULL, created_at TEXT NOT NULL)")
        con.executemany(
            "INSERT INTO users(id,role,created_at) VALUES(?,?,?)",
            [("older-admin", "admin", "2024-01-01"), ("newer-user", "user", "2024-02-01")],
        )
        master_id = self.ensure_master(con)
        self.assertEqual(master_id, "older-admin")
        roles = dict(con.execute("SELECT id,role FROM users").fetchall())
        self.assertEqual(roles, {"older-admin": "master", "newer-user": "user"})
        con.close()

    def test_first_created_account_remains_master_even_if_a_later_row_was_marked_master(self):
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.execute("CREATE TABLE users(id TEXT PRIMARY KEY, role TEXT NOT NULL, created_at TEXT NOT NULL)")
        con.executemany(
            "INSERT INTO users(id,role,created_at) VALUES(?,?,?)",
            [("first-admin", "admin", "2024-01-01T08:00:00-03:00"),
             ("later-master", "master", "2024-02-01T08:00:00-03:00"),
             ("common", "user", "2024-03-01T08:00:00-03:00")],
        )
        master_id = self.ensure_master(con)
        self.assertEqual(master_id, "first-admin")
        roles = dict(con.execute("SELECT id,role FROM users").fetchall())
        self.assertEqual(roles, {"first-admin":"master", "later-master":"admin", "common":"user"})
        with self.assertRaises(sqlite3.IntegrityError):
            con.execute("DELETE FROM users WHERE id='first-admin'")
        con.close()

    def test_registry_normalizes_unknown_legacy_roles_to_common_user(self):
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.execute("CREATE TABLE users(id TEXT PRIMARY KEY, role TEXT NOT NULL, created_at TEXT NOT NULL)")
        con.executemany(
            "INSERT INTO users(id,role,created_at) VALUES(?,?,?)",
            [("first-master", "master", "2024-01-01"), ("legacy-owner", "owner", "2024-02-01")],
        )
        self.assertEqual(self.ensure_master(con), "first-master")
        roles = dict(con.execute("SELECT id,role FROM users").fetchall())
        self.assertEqual(roles, {"first-master":"master", "legacy-owner":"user"})
        con.close()

    def test_duplicate_master_rows_are_normalized_and_database_protects_master(self):
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.execute("CREATE TABLE users(id TEXT PRIMARY KEY, role TEXT NOT NULL, created_at TEXT NOT NULL)")
        con.executemany(
            "INSERT INTO users(id,role,created_at) VALUES(?,?,?)",
            [("master-first", "master", "2024-01-01"), ("master-second", "master", "2024-02-01"), ("admin", "admin", "2024-03-01")],
        )
        self.assertEqual(self.ensure_master(con), "master-first")
        roles = dict(con.execute("SELECT id,role FROM users").fetchall())
        self.assertEqual(roles, {"master-first": "master", "master-second": "admin", "admin": "admin"})
        with self.assertRaises(sqlite3.IntegrityError):
            con.execute("UPDATE users SET role='admin' WHERE id='master-first'")
        with self.assertRaises(sqlite3.IntegrityError):
            con.execute("DELETE FROM users WHERE id='master-first'")
        with self.assertRaises(sqlite3.IntegrityError):
            con.execute("INSERT INTO users(id,role,created_at) VALUES('second-master','master','2024-04-01')")
        con.close()

    def test_registry_installs_single_master_guard_and_initial_user_is_master(self):
        init_registry = ast.unparse(function_node("init_user_registry"))
        create_first = ast.unparse(function_node("_create_user_account"))
        self.assertIn("_ensure_single_master(con)", init_registry)
        self.assertIn("'master'", create_first)
        self.assertIn("idx_users_single_master", SOURCE)
        self.assertIn("protect_master_delete", SOURCE)
        self.assertIn("protect_master_role", SOURCE)

    def test_admin_and_master_are_the_only_roles_allowed_by_account_creation_endpoint(self):
        register = ast.unparse(function_node("auth_register"))
        status = ast.unparse(function_node("auth_status"))
        self.assertIn("('master', 'admin')", register)
        self.assertIn("requested_role not in ('user', 'admin')", register)
        self.assertIn("creator_role == 'admin' and requested_role != 'user'", register)
        self.assertIn("('master', 'admin')", status)
        additional = ast.unparse(function_node("_create_additional_user"))
        self.assertIn("normalized_role not in ('user', 'admin')", additional)
        self.assertIn("VALUES(?,?,?,?,?,1,'master',?,?,?)", ast.unparse(function_node("_create_user_account")))

    def test_account_creation_rechecks_actor_role_and_auth_version_inside_insert_transaction(self):
        create = ast.unparse(function_node("_create_additional_user"))
        register = ast.unparse(function_node("auth_register"))
        self.assertIn("creator_id", create)
        self.assertIn("creator_auth_version", create)
        self.assertIn("BEGIN IMMEDIATE", create)
        self.assertIn("creator['role']", create)
        self.assertIn("creator['auth_version']", create)
        self.assertIn("creator_auth_version=int(session.get('auth_version') or 0)", register)

    def test_account_storage_helper_rejects_calls_without_creator_or_session_version(self):
        creator = load_function("_create_additional_user")
        with self.assertRaises(PermissionError):
            creator("new-user", "123456", role="user")
        with self.assertRaises(PermissionError):
            creator("new-user", "123456", role="user", creator_id="actor-1")
        source = ast.unparse(function_node("_create_additional_user"))
        self.assertIn("if not str(creator_id or '').strip() or creator_auth_version is None", source)
        self.assertIn("if not creator", source)
        self.assertIn("supplied_auth_version = int(creator_auth_version)", source)

    def test_account_creation_rate_limit_is_distinct_from_authorization_denial(self):
        register = ast.unparse(function_node("auth_register"))
        self.assertIn("except UserCreationRateLimited as exc", register)
        self.assertIn("), 429)", register)
        self.assertIn("except PermissionError as exc", register)
        self.assertIn("), 403)", register)

    def _run_create_additional_user(self, creator_role, requested_role="user", auth_version=4):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "users.db"
            con = sqlite3.connect(database)
            con.executescript("""
                CREATE TABLE users(
                    id TEXT PRIMARY KEY, username TEXT NOT NULL, username_norm TEXT NOT NULL UNIQUE,
                    pin_salt TEXT NOT NULL, pin_hash TEXT NOT NULL, auth_version INTEGER NOT NULL,
                    role TEXT NOT NULL, token_storage_key TEXT NOT NULL, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, last_wallet_id TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE wallets(
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL, name_norm TEXT NOT NULL,
                    storage_key TEXT NOT NULL, is_default INTEGER NOT NULL, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, UNIQUE(user_id,name_norm), UNIQUE(user_id,storage_key)
                );
                CREATE TABLE registration_attempts(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, client_ip TEXT NOT NULL, created_at REAL NOT NULL
                );
            """)
            con.execute(
                "INSERT INTO users(id,username,username_norm,pin_salt,pin_hash,auth_version,role,token_storage_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("actor", "actor", "actor", "salt", "hash", 4, creator_role, "actor", "2026-01-01", "2026-01-01"),
            )
            con.commit()
            con.close()

            def connection():
                item = sqlite3.connect(database, timeout=15)
                item.row_factory = sqlite3.Row
                item.execute("PRAGMA foreign_keys=ON")
                return item

            def create_wallet(con, user_id, name, is_default=False):
                wallet_id = str(uuid.uuid4())
                now = datetime.now(ZoneInfo("America/Sao_Paulo")).isoformat(timespec="seconds")
                con.execute(
                    "INSERT INTO wallets(id,user_id,name,name_norm,storage_key,is_default,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (wallet_id, user_id, name, name.casefold(), wallet_id, int(is_default), now, now),
                )
                return wallet_id

            scope = {
                "uuid": uuid,
                "secrets": secrets,
                "pin_digest": lambda pin, salt: f"{salt}:{pin}",
                "datetime": datetime,
                "TZ": ZoneInfo("America/Sao_Paulo"),
                "identity_connection": connection,
                "time": SimpleNamespace(time=lambda: 1_800_000_000.0),
                "_identity_client_ip": lambda: "192.0.2.25",
                "_username_normalized": lambda value: str(value).strip().casefold(),
                "_create_wallet_record": create_wallet,
            }
            module = ast.Module(body=[function_node("_create_additional_user")], type_ignores=[])
            ast.fix_missing_locations(module)
            exec(compile(module, str(MAIN_PATH), "exec"), scope)
            result = scope["_create_additional_user"](
                "new-user", "123456", role=requested_role,
                creator_id="actor", creator_auth_version=auth_version,
            )
            check = connection()
            users = check.execute("SELECT username,role FROM users ORDER BY username").fetchall()
            wallets = check.execute("SELECT name,is_default FROM wallets").fetchall()
            attempts = check.execute("SELECT COUNT(*) FROM registration_attempts").fetchone()[0]
            check.close()
            return result, [(row["username"], row["role"]) for row in users], [(row["name"], row["is_default"]) for row in wallets], attempts

    def test_storage_helper_allows_master_to_create_admin_with_own_empty_default_wallet(self):
        result, users, wallets, attempts = self._run_create_additional_user("master", "admin")
        self.assertTrue(result[0] and result[1])
        self.assertEqual(users, [("actor", "master"), ("new-user", "admin")])
        self.assertEqual(wallets, [("Padrão", 1)])
        self.assertEqual(attempts, 1)

    def test_storage_helper_allows_admin_to_create_only_common_user(self):
        result, users, wallets, attempts = self._run_create_additional_user("admin", "user")
        self.assertTrue(result[0] and result[1])
        self.assertEqual(users, [("actor", "admin"), ("new-user", "user")])
        self.assertEqual(wallets, [("Padrão", 1)])
        self.assertEqual(attempts, 1)

    def test_storage_helper_rejects_common_user_and_admin_role_escalation(self):
        for creator_role, requested_role in (("user", "user"), ("admin", "admin")):
            with self.subTest(creator_role=creator_role, requested_role=requested_role):
                with self.assertRaises(PermissionError):
                    self._run_create_additional_user(creator_role, requested_role)

    def test_storage_helper_rejects_revoked_authentication_version(self):
        with self.assertRaises(PermissionError):
            self._run_create_additional_user("master", "user", auth_version=3)

    def test_user_management_endpoints_recheck_role_and_target_permissions_server_side(self):
        listing = ast.unparse(function_node("users_management_list"))
        update = ast.unparse(function_node("users_management_update"))
        delete = ast.unparse(function_node("users_management_delete"))
        self.assertIn("('master', 'admin')", listing)
        self.assertIn("_user_management_permission(actor_role, target_role, 'reset_pin')", update)
        self.assertIn("_user_management_permission(actor_role, target_role, 'rename')", update)
        self.assertIn("_user_management_permission(actor_role, target_role, 'change_role')", update)
        self.assertIn("_user_management_permission(actor_role, target_role, 'delete')", delete)
        self.assertIn("target_role == 'master'", update)
        self.assertIn("target_role == 'master'", delete)
        self.assertIn("new_role is not None and (not _user_management_permission(actor_role, target_role, 'change_role'))", update)
        self.assertIn("can_change_role", listing)
        self.assertIn("can_reset_pin", listing)
        self.assertIn("can_rename", listing)

    def test_only_master_can_change_roles_after_account_creation(self):
        update = ast.unparse(function_node("users_management_update"))
        register = ast.unparse(function_node("auth_register"))
        listing = ast.unparse(function_node("users_management_list"))
        self.assertIn("new_role is not None and (not _user_management_permission(actor_role, target_role, 'change_role'))", update)
        self.assertIn("requested_role not in ('user', 'admin')", register)
        self.assertIn("role=requested_role", register)
        self.assertIn("can_change_role", listing)

    def test_management_controls_are_not_rendered_for_common_users_and_creation_refreshes_list(self):
        html = (ROOT / "app" / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertIn("{% if current_user_role in ['master','admin'] %}", html)
        self.assertIn("if(addPanel)addPanel.hidden=d.can_manage_users!==true", html)
        self.assertIn("if(usersPanel)usersPanel.hidden=d.can_manage_users!==true", html)
        self.assertIn("roleSelect.disabled=user.can_change_role!==true", html)
        self.assertIn("payload.role=role", html)
        self.assertIn("await loadManagedUsers()", html)
        self.assertIn("user.role==='master'?'Master'", html)
        self.assertIn("user.can_delete", html)


    def test_admin_creation_rejects_admin_role_at_both_endpoint_and_storage_layer(self):
        register = ast.unparse(function_node("auth_register"))
        creator = ast.unparse(function_node("_create_additional_user"))
        self.assertIn("creator_role == 'admin' and requested_role != 'user'", register)
        self.assertIn("creator_role == 'admin' and str(role or 'user').strip().lower() != 'user'", creator)

    def test_admin_update_is_limited_to_pin_reset_of_common_users(self):
        update = ast.unparse(function_node("users_management_update"))
        self.assertIn("actor_role == 'admin' and (new_username is not None or new_role is not None)", update)
        self.assertIn("Admins só podem redefinir o PIN e excluir usuários comuns", update)

    def test_admin_listing_does_not_disclose_other_admins_or_master(self):
        listing = ast.unparse(function_node("users_management_list"))
        self.assertIn("if actor_role == 'master'", listing)
        self.assertIn("WHERE u.role='user' OR u.id=?", listing)

    def test_common_user_cannot_change_own_username(self):
        change = ast.unparse(function_node("auth_change_username"))
        self.assertIn("str(row['role'] or 'user') == 'user'", change)
        self.assertIn("podem redefinir o próprio PIN", change)

    def test_auth_status_exposes_self_delete_for_all_roles_but_hides_global_user_count_from_non_master(self):
        status = ast.unparse(function_node("auth_status"))
        self.assertIn("can_delete_own_account=bool(authenticated)", status)
        self.assertIn("other_user_count=max(0, count - 1) if authenticated and str(user['role'] or 'user') == 'master' else 0", status)

    def test_status_exposes_username_change_only_for_master_and_admin(self):
        status = ast.unparse(function_node("auth_status"))
        self.assertIn("can_change_username", status)
        self.assertIn("('master', 'admin')", status)

    def test_common_user_can_change_own_pin_and_delete_only_own_account(self):
        change_pin = ast.unparse(function_node("auth_change_pin"))
        self.assertIn("_confirm_identity_pin", change_pin)
        delete_self = ast.unparse(function_node("auth_delete_own_account"))
        self.assertIn("EXCLUIR MINHA CONTA", delete_self)
        self.assertIn("_stage_account_storage", delete_self)
        self.assertIn("include_legacy_data=include_legacy_data", delete_self)
        self.assertIn("TRANSFERIR MASTER E EXCLUIR MINHA CONTA", delete_self)
        self.assertIn('id="securityDeleteAccountPanel"', (ROOT / "app/templates/index.html").read_text(encoding="utf-8"))

    def test_both_jinja_templates_parse_after_role_conditionals(self):
        from jinja2 import Environment
        env = Environment()
        for template_name in ("index.html", "auth.html"):
            source = (ROOT / "app/templates" / template_name).read_text(encoding="utf-8")
            env.parse(source)

    def test_settings_render_exact_controls_for_each_role(self):
        from jinja2 import Environment, ChainableUndefined
        env = Environment(undefined=ChainableUndefined, autoescape=False)
        template = env.from_string((ROOT / "app/templates/index.html").read_text(encoding="utf-8"))
        render = lambda role: template.render(current_user_role=role, current_user_username="teste", url_for=lambda *a, **k: "/static/example")
        master = render("master")
        admin = render("admin")
        common = render("user")
        # Master alone gets admin-role creation and installation reset.
        self.assertIn('<select id="securityAddRole">', master)
        self.assertIn('id="securityDeleteAccountPanel"', master)
        # Admin can create common users, manage common-user records, but cannot choose Admin or reset the installation.
        self.assertIn('type="hidden" id="securityAddRole" value="user"', admin)
        self.assertIn('id="securityUsersPanel"', admin)
        self.assertIn('id="securityDeleteAccountPanel"', admin)
        # Common users get only self-service controls, including deletion of their own account.
        self.assertNotIn('id="securityAddUserPanel"', common)
        self.assertNotIn('id="securityUsersPanel"', common)
        self.assertNotIn('id="managedUserEditorBack"', common)
        self.assertNotIn('id="securityUsernameEditor"', common)
        self.assertIn('id="securityDeleteAccountPanel"', common)
        self.assertIn('id="securitySavePin"', common)


    def test_admin_can_delete_common_accounts_and_self_but_not_admin_or_master(self):
        self.assertTrue(self.permission("admin", "user", "delete"))
        self.assertFalse(self.permission("admin", "admin", "delete"))
        self.assertFalse(self.permission("admin", "master", "delete"))
        delete_self = ast.unparse(function_node("auth_delete_own_account"))
        self.assertIn("EXCLUIR MINHA CONTA", delete_self)



    def test_admin_permission_matrix_matches_only_common_user_actions(self):
        for action in ("view", "reset_pin", "delete"):
            self.assertTrue(self.permission("admin", "user", action), action)
        for action in ("rename", "change_role"):
            self.assertFalse(self.permission("admin", "user", action), action)
        for target in ("admin", "master"):
            for action in ("view", "reset_pin", "rename", "change_role", "delete"):
                self.assertFalse(self.permission("admin", target, action), (target, action))

    def test_master_permission_matrix_covers_all_non_master_actions(self):
        for target in ("admin", "user"):
            for action in ("view", "reset_pin", "rename", "change_role", "delete"):
                self.assertTrue(self.permission("master", target, action), (target, action))
        for action in ("view", "reset_pin", "rename", "change_role", "delete"):
            self.assertFalse(self.permission("master", "master", action), action)

    def test_database_rejects_unknown_account_roles(self):
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.execute("CREATE TABLE users(id TEXT PRIMARY KEY, role TEXT NOT NULL, created_at TEXT NOT NULL)")
        con.execute("INSERT INTO users(id,role,created_at) VALUES('master-first','master','2024-01-01')")
        self.ensure_master(con)
        with self.assertRaises(sqlite3.IntegrityError):
            con.execute("INSERT INTO users(id,role,created_at) VALUES('bad','owner','2024-02-01')")
        with self.assertRaises(sqlite3.IntegrityError):
            con.execute("UPDATE users SET role='owner' WHERE id='master-first'")
        con.close()

    def test_all_roles_have_self_delete_in_settings_and_endpoint_removes_only_owned_scope(self):
        html = (ROOT / "app" / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="securityDeleteAccountPanel"', html)
        self.assertIn('onclick="deleteOwnAccount()"', html)
        template = (ROOT / "app" / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertIn("Você pode apagar sua própria conta", template)
        delete_self = ast.unparse(function_node("auth_delete_own_account"))
        self.assertIn("actor_role not in ('master', 'admin', 'user')", delete_self)
        self.assertIn("_stage_account_storage", delete_self)
        self.assertIn("include_legacy_data=include_legacy_data", delete_self)
        self.assertIn("TRANSFERIR MASTER E EXCLUIR MINHA CONTA", delete_self)
        self.assertIn("DELETE FROM users WHERE id=?", delete_self)
        self.assertIn("all_users=reset_installation", delete_self)

    def test_admin_cannot_manage_admin_or_master_but_can_delete_common(self):
        self.assertTrue(self.permission("admin", "user", "reset_pin"))
        self.assertTrue(self.permission("admin", "user", "delete"))
        for target in ("admin", "master"):
            for action in ("view", "reset_pin", "rename", "change_role", "delete"):
                self.assertFalse(self.permission("admin", target, action), (target, action))

    def test_master_can_promote_demote_and_delete_non_master_accounts_only(self):
        for target in ("user", "admin"):
            self.assertTrue(self.permission("master", target, "change_role"))
            self.assertTrue(self.permission("master", target, "reset_pin"))
            self.assertTrue(self.permission("master", target, "delete"))
        for action in ("change_role", "reset_pin", "delete"):
            self.assertFalse(self.permission("master", "master", action))


if __name__ == "__main__":
    unittest.main()
