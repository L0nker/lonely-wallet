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
    def __init__(self, content, status_code=200):
        self.content = content
        self.status_code = status_code

    def raise_for_status(self):
        return None


class VendorChartJsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original_expected_sri = vendor.EXPECTED_SRI
        self.original_download_urls = vendor.DOWNLOAD_URLS
        self.original_direct_bundle_urls = vendor.DIRECT_BUNDLE_URLS
        self.original_expected_bundle_sri = vendor.EXPECTED_BUNDLE_SRI
        self.original_download_attempts = vendor.DOWNLOAD_ATTEMPTS
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
        vendor.EXPECTED_SRI = self.original_expected_sri
        vendor.DOWNLOAD_URLS = self.original_download_urls
        vendor.DIRECT_BUNDLE_URLS = self.original_direct_bundle_urls
        vendor.EXPECTED_BUNDLE_SRI = self.original_expected_bundle_sri
        vendor.DOWNLOAD_ATTEMPTS = self.original_download_attempts
        self.temp.cleanup()

    def test_offline_build_records_degraded_fallback_mode(self):
        with patch.object(vendor.requests, 'get', side_effect=vendor.requests.ConnectionError('offline')):
            with patch.object(vendor.time, 'sleep'):
                result = vendor.main()
        self.assertEqual(result, 0)
        status = json.loads(vendor.STATUS.read_text(encoding='utf-8'))
        self.assertEqual(status['mode'], 'fallback')
        self.assertEqual(status['reason'], 'network_unavailable')
        self.assertTrue(status['fallback_available'])
        self.assertFalse(status['integrity_verified'])

    def test_downloaded_integrity_mismatch_is_a_hard_build_failure(self):
        vendor.DOWNLOAD_ATTEMPTS = 1
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


    def test_primary_registry_network_error_uses_verified_mirror(self):
        bundle = b'/*! Chart.js generated package */\n' + (b'x' * 120_000)
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w:gz') as archive:
            info = tarfile.TarInfo('package/dist/chart.umd.min.js')
            info.size = len(bundle)
            archive.addfile(info, io.BytesIO(bundle))
        tarball = stream.getvalue()
        vendor.EXPECTED_SRI = 'sha512-' + base64.b64encode(hashlib.sha512(tarball).digest()).decode('ascii')
        vendor.DOWNLOAD_URLS = ('https://registry.npmjs.org/chart.js/-/chart.js-4.6.1.tgz', 'https://registry.yarnpkg.com/chart.js/-/chart.js-4.6.1.tgz')
        vendor.DOWNLOAD_ATTEMPTS = 1
        with patch.object(vendor.requests, 'get', side_effect=[vendor.requests.ConnectionError('primary offline'), FakeResponse(tarball)]) as get:
            result = vendor.main()
        self.assertEqual(result, 0)
        self.assertEqual(get.call_args_list[0].args[0], vendor.DOWNLOAD_URLS[0])
        self.assertEqual(get.call_args_list[1].args[0], vendor.DOWNLOAD_URLS[1])
        self.assertEqual(vendor.TARGET.read_bytes(), bundle)
        status = json.loads(vendor.STATUS.read_text(encoding='utf-8'))
        self.assertEqual(status['mode'], 'chartjs')
        self.assertTrue(status['integrity_verified'])

    def test_transient_http_503_retries_same_registry(self):
        bundle = b'/*! Chart.js generated package */\n' + (b'x' * 120_000)
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w:gz') as archive:
            info = tarfile.TarInfo('package/dist/chart.umd.min.js')
            info.size = len(bundle)
            archive.addfile(info, io.BytesIO(bundle))
        tarball = stream.getvalue()
        vendor.EXPECTED_SRI = 'sha512-' + base64.b64encode(hashlib.sha512(tarball).digest()).decode('ascii')
        vendor.DOWNLOAD_URLS = ('https://registry.npmjs.org/chart.js/-/chart.js-4.6.1.tgz',)
        vendor.DOWNLOAD_ATTEMPTS = 2
        retry_response = FakeResponse(b'busy', status_code=503)
        def raise_503():
            error = vendor.requests.HTTPError('503 server error')
            error.response = retry_response
            raise error
        retry_response.raise_for_status = raise_503
        with patch.object(vendor.requests, 'get', side_effect=[retry_response, FakeResponse(tarball)]) as get:
            with patch.object(vendor.time, 'sleep'):
                result = vendor.main()
        self.assertEqual(result, 0)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(vendor.TARGET.read_bytes(), bundle)

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


    def test_direct_cdn_bundle_is_used_when_npm_registries_are_offline(self):
        bundle = b'/*! Chart.js 4.6.1 generated UMD bundle */\n' + (b'x' * 120_000)
        vendor.EXPECTED_BUNDLE_SRI = 'sha384-' + base64.b64encode(hashlib.sha384(bundle).digest()).decode('ascii')
        vendor.DOWNLOAD_URLS = ('https://registry.npmjs.org/chart.js/-/chart.js-4.6.1.tgz',)
        vendor.DIRECT_BUNDLE_URLS = ('https://cdn.jsdelivr.net/npm/chart.js@4.6.1/dist/chart.umd.min.js',)
        vendor.DOWNLOAD_ATTEMPTS = 1
        with patch.object(vendor.requests, 'get', side_effect=[vendor.requests.ConnectionError('npm offline'), FakeResponse(bundle)]) as get:
            result = vendor.main()
        self.assertEqual(result, 0)
        self.assertEqual(get.call_args_list[1].args[0], vendor.DIRECT_BUNDLE_URLS[0])
        self.assertEqual(vendor.TARGET.read_bytes(), bundle)
        status = json.loads(vendor.STATUS.read_text(encoding='utf-8'))
        self.assertEqual(status['mode'], 'chartjs')
        self.assertTrue(status['integrity_verified'])
        self.assertEqual(status['source'], vendor.DIRECT_BUNDLE_URLS[0])

    def test_direct_cdn_bundle_integrity_mismatch_is_hard_failure(self):
        vendor.DOWNLOAD_URLS = ('https://registry.npmjs.org/chart.js/-/chart.js-4.6.1.tgz',)
        vendor.DIRECT_BUNDLE_URLS = ('https://cdn.jsdelivr.net/npm/chart.js@4.6.1/dist/chart.umd.min.js',)
        vendor.DOWNLOAD_ATTEMPTS = 1
        with patch.object(vendor.requests, 'get', side_effect=[vendor.requests.ConnectionError('npm offline'), FakeResponse(b'not a verified bundle')]):
            result = vendor.main()
        self.assertEqual(result, 1)
        self.assertFalse(vendor.STATUS.exists())

    def test_application_version_is_read_from_version_file(self):
        version_file = MODULE_PATH.parents[1] / 'VERSION'
        self.assertEqual(vendor.APP_VERSION, version_file.read_text(encoding='utf-8').strip())

    def test_npm_integrity_pin_matches_expected_metadata(self):
        self.assertEqual(
            vendor.EXPECTED_SRI,
            'sha512-hJyniucsFmq//EvXYvJ6c6mahPoI3kaxdjVH8awTaJ/fDNWeSqqqArixMJWkWNN6fcYvPU3HneSANFNYtRi61g==',
        )


if __name__ == '__main__':
    unittest.main()
