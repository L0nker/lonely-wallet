#!/usr/bin/env python3
"""Fetch a pinned Chart.js npm artifact, verify its SRI digest and vendor it locally.

When the build has no network, the application uses the bundled lightweight canvas
fallback. The fallback is intentionally part of the source package, so an offline
build does not fail solely because Chart.js cannot be downloaded.
"""
from __future__ import annotations

import base64
import hashlib
import io
import tarfile
from pathlib import Path

import requests

VERSION = '4.6.1'
URL = f'https://registry.npmjs.org/chart.js/-/chart.js-{VERSION}.tgz'
# npm's published dist.integrity for chart.js@4.6.1 (SHA-512 over the exact tarball).
EXPECTED_SRI = 'sha512-hJyniucsFmq//EvXYvJ6c6mahPoI3kaxdjVH8awTaJ/fDNWeSqqqArixMJWkWNN6fcYvPU3HneSANFNYtRi61g=='
ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / 'app' / 'static' / 'vendor' / 'chart.umd.min.js'
CHECKSUM = TARGET.with_suffix(TARGET.suffix + '.sha256')
LICENSE = ROOT / 'app' / 'static' / 'vendor' / 'CHARTJS-LICENSE.txt'


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _existing_asset_is_valid() -> bool:
    try:
        expected = CHECKSUM.read_text(encoding='ascii').strip().split()[0]
        payload = TARGET.read_bytes()
        return len(payload) > 100_000 and _sha256(payload) == expected
    except (OSError, ValueError, IndexError):
        return False


def main() -> int:
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    if _existing_asset_is_valid():
        print(f'Chart.js {VERSION} local e verificado por SHA-256.')
        return 0

    try:
        response = requests.get(URL, timeout=(8, 25), headers={'User-Agent': 'LonelyWallet-Build/4.2.31'})
        response.raise_for_status()
        archive_bytes = response.content
        digest = 'sha512-' + base64.b64encode(hashlib.sha512(archive_bytes).digest()).decode('ascii')
        if digest != EXPECTED_SRI:
            raise RuntimeError('O pacote Chart.js não corresponde ao SRI SHA-512 publicado no npm.')
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode='r:gz') as archive:
            member = archive.getmember('package/dist/chart.umd.min.js')
            if not member.isfile() or member.size < 100_000:
                raise RuntimeError('O pacote Chart.js não contém a distribuição UMD esperada.')
            source = archive.extractfile(member)
            if source is None:
                raise RuntimeError('Não foi possível ler o arquivo UMD do Chart.js.')
            payload = source.read()
        if b'Chart.js' not in payload[:4096] or len(payload) < 100_000:
            raise RuntimeError('O conteúdo Chart.js baixado não passou pela validação estrutural.')
        temp = TARGET.with_suffix(TARGET.suffix + '.tmp')
        temp.write_bytes(payload)
        temp.replace(TARGET)
        checksum_temp = CHECKSUM.with_suffix(CHECKSUM.suffix + '.tmp')
        checksum_temp.write_text(f'{_sha256(payload)}  {TARGET.name}\n', encoding='ascii')
        checksum_temp.replace(CHECKSUM)
        LICENSE.write_text(
            f'Chart.js {VERSION}\nProject: https://www.chartjs.org/\nLicense: MIT\n'
            'The vendored UMD distribution is retrieved from the npm package and verified\n'
            'against the package integrity metadata (SHA-512) published by npm.\n',
            encoding='utf-8'
        )
        print(f'Chart.js {VERSION} verificado e instalado em {TARGET}.')
        return 0
    except Exception as exc:
        # The fallback renderer is shipped in the source package and loaded by
        # index.html when the primary local Chart.js file is unavailable.
        print(f'Aviso: Chart.js não foi instalado durante o build ({exc}). O fallback local será utilizado.')
        TARGET.unlink(missing_ok=True)
        CHECKSUM.unlink(missing_ok=True)
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
