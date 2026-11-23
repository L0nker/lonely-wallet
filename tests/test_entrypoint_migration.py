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
