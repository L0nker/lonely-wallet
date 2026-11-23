import base64
import hashlib
import importlib.util
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[1] / 'scripts' / 'vendor_chartjs.py'
SPEC = importlib.util.spec_from_file_location('vendor_chartjs', MODULE_PATH)
vendor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(vendor)


class FakeResponse:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        return None


class VendorChartJsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        vendor.VENDOR = root / 'vendor'
        vendor.VENDOR.mkdir()
        vendor.TARGET = vendor.VENDOR / 'chart.umd.min.js'
        vendor.CHECKSUM = vendor.VENDOR / 'chart.umd.min.js.sha256'
        vendor.LICENSE = vendor.VENDOR / 'CHARTJS-LICENSE.txt'
        vendor.FALLBACK = vendor.VENDOR / 'chart-fallback.js'
        vendor.STATUS = vendor.VENDOR / 'chartjs-build-status.json'
        vendor.FALLBACK.write_text('/* offline fallback renderer */\n' + ('x' * 1500), encoding='utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def test_offline_build_records_degraded_fallback_mode(self):
        with patch.object(vendor.requests, 'get', side_effect=vendor.requests.ConnectionError('offline')):
            result = vendor.main()
        self.assertEqual(result, 0)
        status = json.loads(vendor.STATUS.read_text(encoding='utf-8'))
        self.assertEqual(status['mode'], 'fallback')
        self.assertEqual(status['reason'], 'network_unavailable')
        self.assertTrue(status['fallback_available'])
        self.assertFalse(status['integrity_verified'])

    def test_downloaded_integrity_mismatch_is_a_hard_build_failure(self):
        with patch.object(vendor.requests, 'get', return_value=FakeResponse(b'not-the-chartjs-tarball')):
            result = vendor.main()
        self.assertEqual(result, 1)
        self.assertFalse(vendor.STATUS.exists())

    def test_existing_bundle_with_wrong_sidecar_is_a_hard_failure(self):
        vendor.TARGET.write_bytes(b'x' * 150_001)
        vendor.CHECKSUM.write_text('0' * 64 + '  chart.umd.min.js\n', encoding='ascii')
        with patch.object(vendor.requests, 'get') as get:
            result = vendor.main()
        self.assertEqual(result, 1)
        get.assert_not_called()


    def test_verified_tarball_installs_complete_chartjs_bundle(self):
        bundle = b'/*! Chart.js generated package */\n' + (b'x' * 120_000)
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w:gz') as archive:
            info = tarfile.TarInfo('package/dist/chart.umd.min.js')
            info.size = len(bundle)
            archive.addfile(info, io.BytesIO(bundle))
        tarball = stream.getvalue()
        expected = 'sha512-' + base64.b64encode(hashlib.sha512(tarball).digest()).decode('ascii')
        vendor.EXPECTED_SRI = expected
        with patch.object(vendor.requests, 'get', return_value=FakeResponse(tarball)):
            result = vendor.main()
        self.assertEqual(result, 0)
        self.assertEqual(vendor.TARGET.read_bytes(), bundle)
        self.assertEqual(vendor.STATUS.exists(), True)
        status = json.loads(vendor.STATUS.read_text(encoding='utf-8'))
        self.assertEqual(status['mode'], 'chartjs')
        self.assertTrue(status['integrity_verified'])

    def test_npm_integrity_pin_matches_expected_metadata(self):
        self.assertEqual(
            vendor.EXPECTED_SRI,
            'sha512-hJyniucsFmq//EvXYvJ6c6mahPoI3kaxdjVH8awTaJ/fDNWeSqqqArixMJWkWNN6fcYvPU3HneSANFNYtRi61g==',
        )

    def test_build_user_agent_and_status_use_project_version_file(self):
        expected = vendor.VERSION_FILE.read_text(encoding='utf-8').strip()
        self.assertEqual(vendor.APP_VERSION, expected)
        self.assertEqual(expected, '4.2.39')


if __name__ == '__main__':
    unittest.main()
