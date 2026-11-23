import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "entrypoint.py"
spec = importlib.util.spec_from_file_location("wallet_entrypoint", SCRIPT)
entrypoint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entrypoint)


class PermissionMigrationTests(unittest.TestCase):
    def test_migration_visits_directory_and_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "nested").mkdir()
            (root / "nested" / "assets.db").write_text("sample")
            visited = []
            with patch.object(entrypoint.os, "chown",
                              side_effect=lambda path, uid, gid, **kw: visited.append(Path(path))):
                entrypoint.migrate_permissions(root, 10001, 10001)
            self.assertIn(root, visited)
            self.assertIn(root / "nested", visited)
            self.assertIn(root / "nested" / "assets.db", visited)

    def test_permission_error_aborts_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "assets.db").write_text("sample")
            with patch.object(entrypoint.os, "chown", side_effect=PermissionError("denied")):
                with self.assertRaises(RuntimeError):
                    entrypoint.migrate_permissions(root, 10001, 10001)

    def test_main_sets_writable_home_and_runtime_directory_for_gunicorn(self):
        import os
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory(prefix='lw-runtime-home-') as tmp:
            root = Path(tmp)
            home = root / 'home'
            runtime = root / 'runtime'
            data = root / 'data'
            fake_uid = os.getuid()
            fake_gid = os.getgid()
            with patch.object(entrypoint, 'DATA', data), patch.object(
                entrypoint, 'MARKER', data / '.marker'
            ), patch.object(entrypoint.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=fake_uid, pw_dir=str(home))), patch.object(
                entrypoint.grp, 'getgrnam', return_value=SimpleNamespace(gr_gid=fake_gid)
            ), patch.object(entrypoint.os, 'geteuid', return_value=0), patch.object(
                entrypoint.os, 'chown'
            ), patch.object(entrypoint, 'migrate_permissions'), patch.object(
                entrypoint, '_write_marker'
            ), patch.object(entrypoint.os, 'setgroups'), patch.object(
                entrypoint.os, 'setgid'
            ), patch.object(entrypoint.os, 'setuid'), patch.object(
                entrypoint.os, 'execvp'
            ), patch.dict(os.environ, {'XDG_RUNTIME_DIR': str(runtime)}, clear=False):
                entrypoint.sys.argv = ['entrypoint.py', 'gunicorn']
                entrypoint.main()
                self.assertEqual(os.environ['HOME'], str(home))
                self.assertEqual(os.environ['XDG_RUNTIME_DIR'], str(runtime))
                self.assertTrue(home.is_dir())
                self.assertTrue(runtime.is_dir())

    def test_marker_is_not_left_when_marker_write_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(entrypoint, "DATA", root), patch.object(
                entrypoint, "MARKER", root / ".marker"
            ), patch.object(
                entrypoint.os, "chown", side_effect=PermissionError("denied")
            ):
                with self.assertRaises(PermissionError):
                    entrypoint._write_marker(10001, 10001)
                self.assertFalse((root / ".marker").exists())
                self.assertFalse((root / ".marker.tmp").exists())


if __name__ == "__main__":
    unittest.main()
