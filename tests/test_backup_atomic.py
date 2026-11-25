import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'backup.py'


class BackupAtomicTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data = self.root / 'data'
        self.out = self.root / 'out'
        self.data.mkdir()
        database_paths = [
            self.data / 'ativos.db',
            self.data / 'tokens.db',
            self.data / 'users.db',
            self.data / 'users' / 'user-1' / 'wallets' / 'wallet-1' / 'ativos.db',
            self.data / 'users' / 'user-1' / 'wallets' / 'wallet-1' / 'tokens.db',
            self.data / 'users' / 'user-2' / 'wallets' / 'wallet-2' / 'ativos.db',
        ]
        for index, path in enumerate(database_paths):
            path.parent.mkdir(parents=True, exist_ok=True)
            con = sqlite3.connect(path)
            con.execute('CREATE TABLE sample(value INTEGER)')
            con.execute('INSERT INTO sample VALUES (?)', (index + 1,))
            con.commit()
            con.close()
        (self.data / '.api-credentials.key').write_text('test-key')
        (self.data / 'quotes_last_valid.json').write_text('{"PETR4": {"price": 10}}')
        self.env = dict(os.environ, DATA_DIR=str(self.data))

    def tearDown(self):
        self.tmp.cleanup()

    def run_backup(self):
        return subprocess.run(['python', str(SCRIPT), '--destination', str(self.out)], env=self.env, capture_output=True, text=True)

    def test_success_publishes_manifest_and_files(self):
        result = self.run_backup()
        self.assertEqual(result.returncode, 0, result.stderr)
        folders = list(self.out.glob('lonely-wallet-*'))
        self.assertEqual(len(folders), 1)
        manifest = json.loads((folders[0] / 'manifest.json').read_text())
        self.assertIn('ativos.db', manifest['files'])
        self.assertIn('tokens.db', manifest['files'])
        self.assertIn('users.db', manifest['files'])
        self.assertIn('users/user-1/wallets/wallet-1/ativos.db', manifest['files'])
        self.assertIn('users/user-1/wallets/wallet-1/tokens.db', manifest['files'])
        self.assertIn('users/user-2/wallets/wallet-2/ativos.db', manifest['files'])
        self.assertTrue((folders[0] / 'tokens.db').exists())
        self.assertTrue((folders[0] / 'users/user-1/wallets/wallet-1/tokens.db').exists())
        self.assertFalse((folders[0] / 'secrets/.api-credentials.key').exists())
        for path in folders[0].rglob('*.db'):
            con = sqlite3.connect(path)
            try:
                self.assertEqual(con.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            finally:
                con.close()


    def test_include_keys_stores_global_keys_with_private_permissions(self):
        result = subprocess.run(
            ['python', str(SCRIPT), '--destination', str(self.out), '--include-keys'],
            env=self.env, capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        folder = next(self.out.glob('lonely-wallet-*'))
        key = folder / 'secrets' / '.api-credentials.key'
        self.assertEqual(key.read_text(), 'test-key')
        self.assertEqual(key.stat().st_mode & 0o777, 0o600)
        self.assertTrue(json.loads((folder / 'manifest.json').read_text())['includes_encryption_keys'])

    def test_backup_skips_existing_backup_directories(self):
        nested = self.data / 'backups' / 'old' / 'old.db'
        nested.parent.mkdir(parents=True)
        con = sqlite3.connect(nested)
        con.execute('CREATE TABLE sample(value INTEGER)')
        con.commit(); con.close()
        result = self.run_backup()
        self.assertEqual(result.returncode, 0, result.stderr)
        folder = next(self.out.glob('lonely-wallet-*'))
        self.assertFalse((folder / 'backups/old/old.db').exists())

    def test_backup_database_supports_special_path_characters(self):
        from scripts.backup import backup_database
        root = self.data / 'origem ? # % com espaco'
        root.mkdir()
        source = root / 'carteira ? # %.db'
        target = root / 'backup ? # %.db'
        con = sqlite3.connect(source)
        con.execute('CREATE TABLE sample(value TEXT)')
        con.execute('INSERT INTO sample VALUES(?)', ('backup persistido',))
        con.commit(); con.close()
        backup_database(source, target)
        verify = sqlite3.connect(target)
        try:
            self.assertEqual(verify.execute('SELECT value FROM sample').fetchone()[0], 'backup persistido')
        finally:
            verify.close()

    def test_invalid_cache_leaves_no_partial_backup(self):
        self.data.joinpath('quotes_last_valid.json').write_text('{invalid')
        result = self.run_backup()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(self.out.glob('lonely-wallet-*')), [])
        self.assertEqual(list(self.out.glob('.lonely-wallet-staging-*')), [])


if __name__ == '__main__':
    unittest.main()
