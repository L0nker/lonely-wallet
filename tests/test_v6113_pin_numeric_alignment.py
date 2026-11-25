"""Regression tests for numeric-only PIN entry, form alignment and cache deletion races."""
import ast
import json
import re
import sqlite3
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN_PATH = ROOT / "app" / "main.py"
INDEX_PATH = ROOT / "app" / "templates" / "index.html"
AUTH_PATH = ROOT / "app" / "templates" / "auth.html"
MAIN = MAIN_PATH.read_text(encoding="utf-8")
INDEX = INDEX_PATH.read_text(encoding="utf-8")
AUTH = AUTH_PATH.read_text(encoding="utf-8")
TREE = ast.parse(MAIN)


def function_node(name):
    for node in TREE.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"Function {name} not found")


class WalletDeletionInProgress(RuntimeError):
    pass


def load_function(name, **globals_):
    node = function_node(name)
    module = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(module)
    scope = {"WalletDeletionInProgress": WalletDeletionInProgress}
    scope.update(globals_)
    exec(compile(module, str(MAIN_PATH), "exec"), scope)
    return scope[name]


class PinNumericAndAlignmentTests(unittest.TestCase):
    def test_dashboard_pin_inputs_are_bound_to_numeric_only_guard(self):
        self.assertIn("function sanitizePinValue(value,maxLength=12)", INDEX)
        self.assertIn("initializePinNumericInputs();", INDEX)
        self.assertIn("input[data-pin-input]", INDEX)
        self.assertIn("addEventListener('beforeinput'", INDEX)
        self.assertIn("addEventListener('input'", INDEX)
        self.assertIn("addEventListener('paste'", INDEX)
        self.assertIn("preparePinNumericInput(input)", INDEX)

    def test_all_dashboard_pin_markup_uses_numeric_inputmode(self):
        pin_labels = ("securityUsernamePin", "securityAddPin", "securityAddPinConfirm",
                      "securityAddCurrentPin", "securityCurrentPin", "securityNewPin",
                      "securityConfirmPin", "managedUserNewPin", "managedUserConfirmPin",
                      "managedUserActorPin")
        for element_id in pin_labels:
            with self.subTest(element_id=element_id):
                start = INDEX.index(f'id="{element_id}"')
                field = INDEX[start:INDEX.find('>', start) + 1]
                self.assertIn('type="password"', field)
                self.assertIn('inputmode="numeric"', field)

    def test_backend_pin_validation_accepts_only_ascii_digits(self):
        validate = load_function("_validate_pin_pair", re=re)
        self.assertEqual(validate("123456", "123456"), (True, ""))
        self.assertFalse(validate("１２３４５６", "１２３４５６")[0])
        self.assertFalse(validate("١٢٣٤٥٦", "١٢٣٤٥٦")[0])
        self.assertFalse(validate("12345a", "12345a")[0])
        self.assertNotIn(r"r'\d{6,12}'", MAIN)

    def test_custom_pin_prompts_are_numeric_and_capped(self):
        prompt = INDEX[INDEX.index("function appPrompt("):INDEX.index("function appNotice(")]
        self.assertIn("if(type==='password')", prompt)
        self.assertIn("preparePinNumericInput(input)", prompt)
        self.assertIn("input.maxLength=12", INDEX)

    def test_auth_and_first_run_pin_fields_reject_non_digits(self):
        self.assertEqual(len(re.findall(r'<input[^>]*data-pin-input[^>]*>', AUTH)), 3)
        self.assertIn("replace(/[^0-9]/g,'')", AUTH)
        self.assertIn("addEventListener('beforeinput'", AUTH)
        self.assertIn("addEventListener('paste'", AUTH)

    def test_security_and_user_editor_controls_have_matching_dimensions(self):
        self.assertIn("#page-seguranca .security-panel .field label", INDEX)
        self.assertIn("#managedUserEditorForm .field label", INDEX)
        self.assertIn("min-height:32px", INDEX)
        self.assertIn("height:44px;min-height:44px", INDEX)
        self.assertIn("grid-template-columns:repeat(2,minmax(0,1fr))", INDEX)
        self.assertIn("@media(max-width:620px)", INDEX)

    def test_logout_button_is_named_sair_without_native_tooltip(self):
        match = re.search(r'<button class="ghost logout-top-btn"[^>]*>(.*?)</button>', INDEX)
        self.assertIsNotNone(match, "logout button must remain in the top header")
        button = match.group(0)
        self.assertIn('aria-label="Sair"', button)
        self.assertNotRegex(button, r'\btitle\s*=')
        self.assertIn('↪ Sair</button>', button)
        self.assertNotIn('Sair e bloquear', button)

    def test_missing_wallet_cache_during_background_write_is_silently_skipped(self):
        class Logger:
            def __init__(self):
                self.warnings = []
            def warning(self, *args):
                self.warnings.append(args)
        class App:
            def __init__(self):
                self.logger = Logger()
        app = App()

        def missing_cache_path():
            raise FileNotFoundError("wallet cache directory was moved during deletion")

        persist = load_function(
            "_persist_performance_history_snapshot",
            DASHBOARD_CACHE_LOCK=threading.RLock(),
            active_dashboard_cache_path=missing_cache_path,
            app=app,
            json=json,
            sqlite3=sqlite3,
        )
        self.assertFalse(persist("signature", {}))
        self.assertEqual(app.logger.warnings, [])

    def test_wallet_deletion_marker_during_background_write_is_silently_skipped(self):
        class Logger:
            def __init__(self):
                self.warnings = []
            def warning(self, *args):
                self.warnings.append(args)
        class App:
            def __init__(self):
                self.logger = Logger()
        app = App()

        def deleting_cache_path():
            raise WalletDeletionInProgress("Esta carteira está sendo excluída")

        persist = load_function(
            "_persist_performance_history_snapshot",
            DASHBOARD_CACHE_LOCK=threading.RLock(),
            active_dashboard_cache_path=deleting_cache_path,
            app=app,
            json=json,
            sqlite3=sqlite3,
        )
        self.assertFalse(persist("signature", {}))
        self.assertEqual(app.logger.warnings, [])

    def test_unrelated_runtime_errors_are_still_logged(self):
        class Logger:
            def __init__(self):
                self.warnings = []
            def warning(self, *args):
                self.warnings.append(args)
        class App:
            def __init__(self):
                self.logger = Logger()
        app = App()

        def unexpected_runtime_error():
            raise RuntimeError("unexpected storage failure")

        persist = load_function(
            "_persist_performance_history_snapshot",
            DASHBOARD_CACHE_LOCK=threading.RLock(),
            active_dashboard_cache_path=unexpected_runtime_error,
            app=app,
            json=json,
            sqlite3=sqlite3,
        )
        self.assertFalse(persist("signature", {}))
        self.assertEqual(len(app.logger.warnings), 1)

    def test_real_cache_permission_errors_are_still_logged(self):
        class Logger:
            def __init__(self):
                self.warnings = []
            def warning(self, *args):
                self.warnings.append(args)
        class App:
            def __init__(self):
                self.logger = Logger()
        app = App()

        def inaccessible_cache_path():
            raise PermissionError("permission denied")

        persist = load_function(
            "_persist_performance_history_snapshot",
            DASHBOARD_CACHE_LOCK=threading.RLock(),
            active_dashboard_cache_path=inaccessible_cache_path,
            app=app,
            json=json,
            sqlite3=sqlite3,
        )
        self.assertFalse(persist("signature", {}))
        self.assertEqual(len(app.logger.warnings), 1)


if __name__ == "__main__":
    unittest.main()
