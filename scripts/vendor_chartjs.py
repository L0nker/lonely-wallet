#!/usr/bin/env python3
"""Download and vendor the pinned Chart.js UMD bundle.

Offline builds may continue with the committed lightweight renderer, but a
received package with an invalid SHA-512 digest is a hard build failure.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import sys
import tarfile
from pathlib import Path

import requests

VERSION = '4.6.1'
ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / 'VERSION'
try:
    APP_VERSION = VERSION_FILE.read_text(encoding='utf-8').strip() or 'desconhecida'
except OSError:
    APP_VERSION = 'desconhecida'
URL = f'https://registry.npmjs.org/chart.js/-/chart.js-{VERSION}.tgz'
# Matches npm's dist.integrity metadata for chart.js@4.6.1.
EXPECTED_SRI = 'sha512-hJyniucsFmq//EvXYvJ6c6mahPoI3kaxdjVH8awTaJ/fDNWeSqqqArixMJWkWNN6fcYvPU3HneSANFNYtRi61g=='
VENDOR = ROOT / 'app' / 'static' / 'vendor'
TARGET = VENDOR / 'chart.umd.min.js'
CHECKSUM = VENDOR / 'chart.umd.min.js.sha256'
LICENSE = VENDOR / 'CHARTJS-LICENSE.txt'
FALLBACK = VENDOR / 'chart-fallback.js'
STATUS = VENDOR / 'chartjs-build-status.json'
MIN_BUNDLE_SIZE = 100_000


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _write_status(mode: str, reason: str | None = None) -> None:
    document = {
        'application_version': APP_VERSION,
        'chartjs_version': VERSION,
        'mode': mode,
        'integrity_verified': mode == 'chartjs',
        'fallback_available': FALLBACK.is_file(),
        'reason': reason,
    }
    temp = STATUS.with_suffix(STATUS.suffix + '.tmp')
    temp.write_text(json.dumps(document, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temp.replace(STATUS)


def _existing_asset_is_valid() -> bool:
    """Validate an existing vendored UMD file against its generated sidecar."""
    if not TARGET.exists() and not CHECKSUM.exists():
        return False
    if not TARGET.is_file() or not CHECKSUM.is_file():
        raise RuntimeError('Bundle Chart.js ou arquivo SHA-256 está ausente/incompleto.')
    try:
        expected = CHECKSUM.read_text(encoding='ascii').strip().split()[0].lower()
        payload = TARGET.read_bytes()
    except (OSError, ValueError, IndexError) as exc:
        raise RuntimeError('Não foi possível validar o bundle Chart.js existente.') from exc
    if len(payload) < MIN_BUNDLE_SIZE or len(expected) != 64 or _sha256(payload) != expected:
        raise RuntimeError('O bundle Chart.js local não corresponde ao SHA-256 registrado. Refaça o pacote a partir de uma origem confiável.')
    return True


def _install_verified_bundle(archive_bytes: bytes) -> None:
    digest = 'sha512-' + base64.b64encode(hashlib.sha512(archive_bytes).digest()).decode('ascii')
    if digest != EXPECTED_SRI:
        raise RuntimeError('O pacote Chart.js recebido não corresponde ao SRI SHA-512 publicado no npm. Build interrompido.')

    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode='r:gz') as archive:
        try:
            member = archive.getmember('package/dist/chart.umd.min.js')
        except KeyError as exc:
            raise RuntimeError('O pacote Chart.js não contém package/dist/chart.umd.min.js.') from exc
        if not member.isfile() or member.size < MIN_BUNDLE_SIZE or member.size > 10_000_000:
            raise RuntimeError('A distribuição UMD do Chart.js tem tamanho ou tipo inesperado.')
        source = archive.extractfile(member)
        if source is None:
            raise RuntimeError('Não foi possível ler a distribuição UMD do Chart.js.')
        payload = source.read(member.size + 1)
        if len(payload) != member.size:
            raise RuntimeError('A leitura da distribuição UMD do Chart.js foi incompleta.')

    if b'Chart.js' not in payload[:4096] or len(payload) < MIN_BUNDLE_SIZE:
        raise RuntimeError('O conteúdo UMD não passou pela validação estrutural do Chart.js.')

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
        encoding='utf-8',
    )


def main() -> int:
    VENDOR.mkdir(parents=True, exist_ok=True)
    try:
        if _existing_asset_is_valid():
            _write_status('chartjs')
            print(f'Chart.js {VERSION} local e verificado por SHA-256.')
            return 0

        try:
            response = requests.get(
                URL,
                timeout=(8, 25),
                headers={'User-Agent': f'LonelyWallet-Build/{APP_VERSION}'},
            )
            response.raise_for_status()
            archive_bytes = response.content
        except (requests.ConnectionError, requests.Timeout) as exc:
            if not FALLBACK.is_file():
                raise RuntimeError('Sem conexão para baixar Chart.js e o renderer alternativo não está no pacote.') from exc
            TARGET.unlink(missing_ok=True)
            CHECKSUM.unlink(missing_ok=True)
            _write_status('fallback', 'network_unavailable')
            print(
                f'Aviso: não foi possível acessar o registry npm ({type(exc).__name__}). '
                'Build continuará em modo degradado: fallback com tooltips básicos e animação.',
                file=sys.stderr,
            )
            return 0

        # A digest mismatch, archive corruption, unexpected HTTP status (such as
        # 404), or missing UMD file is a hard failure, never a silent fallback.
        _install_verified_bundle(archive_bytes)
        _write_status('chartjs')
        print(f'Chart.js {VERSION} verificado por SHA-512 e instalado em {TARGET}.')
        return 0
    except Exception as exc:
        print(f'ERRO: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
