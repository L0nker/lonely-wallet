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
        for name in ('ativos.db', 'tokens.db'):
            con = sqlite3.connect(self.data / name)
            con.execute('CREATE TABLE sample(value INTEGER)')
            con.execute('INSERT INTO sample VALUES (1)')
            con.commit()
            con.close()
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
        self.assertTrue((folders[0] / 'tokens.db').exists())

    def test_invalid_cache_leaves_no_partial_backup(self):
        self.data.joinpath('quotes_last_valid.json').write_text('{invalid')
        result = self.run_backup()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(self.out.glob('lonely-wallet-*')), [])
        self.assertEqual(list(self.out.glob('.lonely-wallet-staging-*')), [])


if __name__ == '__main__':
    unittest.main()
