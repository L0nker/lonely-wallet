import sqlite3
import tempfile
import unittest
from pathlib import Path
from scripts.database_migration import migrate_legacy_database


class LegacyMigrationAtomicTests(unittest.TestCase):
    def test_valid_legacy_is_published_only_after_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / 'legacy.db'
            destination = root / 'assets.db'
            con = sqlite3.connect(legacy)
            con.execute('CREATE TABLE sample(value TEXT)')
            con.execute('INSERT INTO sample VALUES(?)', ('preservado',))
            con.commit()
            con.close()
            migrate_legacy_database(legacy, destination)
            self.assertTrue(destination.exists())
            copied = sqlite3.connect(destination)
            try:
                self.assertEqual(copied.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                self.assertEqual(copied.execute('SELECT value FROM sample').fetchone()[0], 'preservado')
            finally:
                copied.close()
            self.assertEqual(list(root.glob('.lonely-wallet-migration-*')), [])

    def test_corrupt_legacy_does_not_publish_partial_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / 'legacy.db'
            destination = root / 'assets.db'
            legacy.write_bytes(b'not a sqlite database')
            with self.assertRaises((sqlite3.DatabaseError, RuntimeError)):
                migrate_legacy_database(legacy, destination)
            self.assertFalse(destination.exists())
            self.assertEqual(list(root.glob('.lonely-wallet-migration-*')), [])


if __name__ == '__main__':
    unittest.main()
