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
import os
import sys
import tarfile
import time
import math
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
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
# All mirrors must deliver the exact npm tarball pinned by EXPECTED_SRI. The
# digest check is mandatory regardless of the source used for the download.
DOWNLOAD_URLS = (
    URL,
    f'https://registry.yarnpkg.com/chart.js/-/chart.js-{VERSION}.tgz',
    f'https://registry.npmmirror.com/chart.js/-/chart.js-{VERSION}.tgz',
)
# Direct prebuilt UMD endpoints are a final transport option when npm registries
# are blocked. Their bytes are accepted only if they match the published SRI.
DIRECT_BUNDLE_URLS = (
    f'https://cdn.jsdelivr.net/npm/chart.js@{VERSION}/dist/chart.umd.min.js',
    f'https://cdnjs.cloudflare.com/ajax/libs/Chart.js/{VERSION}/chart.umd.min.js',
)
try:
    DOWNLOAD_ATTEMPTS = min(3, max(1, int(os.environ.get('CHARTJS_DOWNLOAD_ATTEMPTS', '2'))))
except (TypeError, ValueError):
    DOWNLOAD_ATTEMPTS = 2
DOWNLOAD_TIMEOUT = (8, 25)
# Matches npm's dist.integrity metadata for chart.js@4.6.1.
EXPECTED_SRI = 'sha512-hJyniucsFmq//EvXYvJ6c6mahPoI3kaxdjVH8awTaJ/fDNWeSqqqArixMJWkWNN6fcYvPU3HneSANFNYtRi61g=='
# Published SRI for chart.umd.min.js served by jsDelivr for Chart.js 4.6.1.
EXPECTED_BUNDLE_SRI = 'sha384-Emf5ZVIUiarAdhkgtqK+5/Z5qIvM6QoVrS+qAVtfyZVuD5lIHrac0me485+Antp0'
VENDOR = ROOT / 'app' / 'static' / 'vendor'
TARGET = VENDOR / 'chart.umd.min.js'
CHECKSUM = VENDOR / 'chart.umd.min.js.sha256'
LICENSE = VENDOR / 'CHARTJS-LICENSE.txt'
FALLBACK = VENDOR / 'chart-fallback.js'
STATUS = VENDOR / 'chartjs-build-status.json'
MIN_BUNDLE_SIZE = 100_000


def _retry_after_delay(response, fallback: float) -> float:
    """Return a Retry-After delay for build downloads.

    If a source asks for a long wait, the caller skips that source and tries
    another mirror rather than retrying early or blocking a Docker build for
    hours. A missing/invalid header uses a small transient-error backoff,
    except HTTP 429 which defaults conservatively to 60 seconds.
    """
    status = int(getattr(response, 'status_code', 0) or 0)
    default = 60.0 if status == 429 else max(0.0, float(fallback))
    headers = getattr(response, 'headers', {}) or {}
    raw = str(headers.get('Retry-After') or '').strip()
    if not raw:
        return default
    try:
        seconds = math.ceil(float(raw))
        if not math.isfinite(float(raw)):
            return default
        return max(0.0, float(seconds))
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        retry_at = parsedate_to_datetime(raw)
        if retry_at is None:
            return default
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(0.0, math.ceil((retry_at.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds()))
    except (TypeError, ValueError, OverflowError, IndexError):
        return default


def _wait_or_skip_source(response, attempt: int) -> bool:
    """Wait briefly when safe; return False when another mirror should be tried."""
    fallback = min(3.0, 0.5 * attempt)
    delay = _retry_after_delay(response, fallback)
    if delay > 10:
        return False
    if delay > 0:
        time.sleep(delay)
    return True


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _write_status(mode: str, reason: str | None = None, source: str | None = None) -> None:
    document = {
        'application_version': APP_VERSION,
        'chartjs_version': VERSION,
        'mode': mode,
        'integrity_verified': mode == 'chartjs',
        'fallback_available': FALLBACK.is_file(),
        'reason': reason,
        'source': source,
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


class ChartJsNetworkUnavailable(Exception):
    """All configured package registries were temporarily unreachable."""


def _download_chartjs_tarball() -> bytes:
    """Download the pinned npm tarball from registries and verify its SRI.

    Mirrors are only transport alternatives: no artifact is accepted unless
    its SHA-512 exactly matches the package integrity published for 4.6.1.
    Transient network/5xx/429 failures may use the local fallback. Permanent
    HTTP errors or integrity mismatches fail the build instead of silently
    shipping an untrusted bundle.
    """
    transient_errors = []
    permanent_errors = []
    integrity_errors = []
    for source_url in DOWNLOAD_URLS:
        for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
            try:
                response = requests.get(
                    source_url,
                    timeout=DOWNLOAD_TIMEOUT,
                    headers={
                        'User-Agent': f'LonelyWallet-Build/{APP_VERSION}',
                        'Accept': 'application/octet-stream, application/gzip;q=0.9, */*;q=0.8',
                    },
                )
                status_code = int(getattr(response, 'status_code', 200) or 200)
                try:
                    response.raise_for_status()
                except requests.HTTPError as exc:
                    detail = f'{source_url} respondeu HTTP {status_code}: {exc}'
                    if status_code in (408, 425, 429) or status_code >= 500:
                        transient_errors.append(detail)
                        if attempt < DOWNLOAD_ATTEMPTS and _wait_or_skip_source(response, attempt):
                            continue
                    else:
                        permanent_errors.append(detail)
                    break
                archive_bytes = bytes(response.content or b'')
                if not archive_bytes:
                    permanent_errors.append(f'{source_url} retornou um arquivo vazio.')
                    break
                digest = 'sha512-' + base64.b64encode(hashlib.sha512(archive_bytes).digest()).decode('ascii')
                if digest != EXPECTED_SRI:
                    integrity_errors.append(f'{source_url} não corresponde ao SRI SHA-512 fixado.')
                    break
                print(f'Chart.js {VERSION}: pacote obtido de {source_url}.')
                return archive_bytes
            except (requests.ConnectionError, requests.Timeout) as exc:
                transient_errors.append(f'{source_url} tentativa {attempt}/{DOWNLOAD_ATTEMPTS}: {type(exc).__name__}')
            except requests.RequestException as exc:
                transient_errors.append(f'{source_url} tentativa {attempt}/{DOWNLOAD_ATTEMPTS}: {type(exc).__name__}: {exc}')
            if attempt < DOWNLOAD_ATTEMPTS:
                time.sleep(min(3, 0.5 * attempt))

    if integrity_errors:
        raise RuntimeError('Falha de integridade ao obter Chart.js: ' + '; '.join(integrity_errors))
    if permanent_errors:
        raise RuntimeError('Os registries retornaram erros permanentes ao obter Chart.js: ' + '; '.join(permanent_errors))
    raise ChartJsNetworkUnavailable('; '.join(transient_errors) or 'nenhum registry disponível')



def _download_direct_chartjs_bundle() -> tuple[bytes, str]:
    """Try prebuilt UMD CDNs when npm tarball registries are unreachable.

    Direct files are trusted only when their SHA-384 SRI exactly matches the
    published integrity value for Chart.js 4.6.1. A different file is never
    installed, even if it has a plausible name or size.
    """
    transient_errors = []
    permanent_errors = []
    integrity_errors = []
    for source_url in DIRECT_BUNDLE_URLS:
        for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
            try:
                response = requests.get(
                    source_url,
                    timeout=DOWNLOAD_TIMEOUT,
                    headers={
                        'User-Agent': f'LonelyWallet-Build/{APP_VERSION}',
                        'Accept': 'application/javascript, text/javascript;q=0.9, */*;q=0.8',
                    },
                )
                status_code = int(getattr(response, 'status_code', 200) or 200)
                try:
                    response.raise_for_status()
                except requests.HTTPError as exc:
                    detail = f'{source_url} respondeu HTTP {status_code}: {exc}'
                    if status_code in (408, 425, 429) or status_code >= 500:
                        transient_errors.append(detail)
                        if attempt < DOWNLOAD_ATTEMPTS and _wait_or_skip_source(response, attempt):
                            continue
                    else:
                        permanent_errors.append(detail)
                    break
                payload = bytes(response.content or b'')
                digest = 'sha384-' + base64.b64encode(hashlib.sha384(payload).digest()).decode('ascii')
                if len(payload) < MIN_BUNDLE_SIZE or digest != EXPECTED_BUNDLE_SRI:
                    integrity_errors.append(f'{source_url} não corresponde ao SRI SHA-384 fixado.')
                    break
                if b'Chart.js' not in payload[:4096]:
                    integrity_errors.append(f'{source_url} não passou pela validação estrutural do Chart.js.')
                    break
                return payload, source_url
            except (requests.ConnectionError, requests.Timeout) as exc:
                transient_errors.append(f'{source_url} tentativa {attempt}/{DOWNLOAD_ATTEMPTS}: {type(exc).__name__}')
            except requests.RequestException as exc:
                transient_errors.append(f'{source_url} tentativa {attempt}/{DOWNLOAD_ATTEMPTS}: {type(exc).__name__}: {exc}')
            if attempt < DOWNLOAD_ATTEMPTS:
                time.sleep(min(3, 0.5 * attempt))

    if integrity_errors:
        raise RuntimeError('Falha de integridade ao obter bundle direto do Chart.js: ' + '; '.join(integrity_errors))
    if permanent_errors:
        raise RuntimeError('Os CDNs retornaram erros permanentes ao obter Chart.js: ' + '; '.join(permanent_errors))
    raise ChartJsNetworkUnavailable('; '.join(transient_errors) or 'nenhum CDN direto disponível')


def _write_bundle_files(payload: bytes, source_description: str) -> None:
    if len(payload) < MIN_BUNDLE_SIZE or len(payload) > 10_000_000:
        raise RuntimeError('A distribuição UMD do Chart.js tem tamanho inesperado.')
    if b'Chart.js' not in payload[:4096]:
        raise RuntimeError('O conteúdo UMD não passou pela validação estrutural do Chart.js.')
    temp = TARGET.with_suffix(TARGET.suffix + '.tmp')
    temp.write_bytes(payload)
    temp.replace(TARGET)
    checksum_temp = CHECKSUM.with_suffix(CHECKSUM.suffix + '.tmp')
    checksum_temp.write_text(f'{_sha256(payload)}  {TARGET.name}\n', encoding='ascii')
    checksum_temp.replace(CHECKSUM)
    LICENSE.write_text(
        f'Chart.js {VERSION}\nProject: https://www.chartjs.org/\nLicense: MIT\n'
        f'The vendored UMD distribution was obtained from {source_description} and verified\n'
        'against the pinned package/CDN integrity metadata before installation.\n',
        encoding='utf-8',
    )

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

    _write_bundle_files(payload, 'the npm package tarball (SHA-512 SRI)')


def _install_direct_bundle(payload: bytes) -> None:
    digest = 'sha384-' + base64.b64encode(hashlib.sha384(payload).digest()).decode('ascii')
    if digest != EXPECTED_BUNDLE_SRI:
        raise RuntimeError('O bundle direto recebido não corresponde ao SRI SHA-384 fixado.')
    _write_bundle_files(payload, 'a pinned CDN bundle (SHA-384 SRI)')


def main() -> int:
    VENDOR.mkdir(parents=True, exist_ok=True)
    try:
        if _existing_asset_is_valid():
            _write_status('chartjs')
            print(f'Chart.js {VERSION} local e verificado por SHA-256.')
            return 0

        try:
            archive_bytes = _download_chartjs_tarball()
        except ChartJsNetworkUnavailable as registry_error:
            try:
                bundle, bundle_source = _download_direct_chartjs_bundle()
            except ChartJsNetworkUnavailable as cdn_error:
                if not FALLBACK.is_file():
                    raise RuntimeError('Sem conexão com registries/CDNs Chart.js e o renderer alternativo não está no pacote.') from cdn_error
                TARGET.unlink(missing_ok=True)
                CHECKSUM.unlink(missing_ok=True)
                _write_status('fallback', 'network_unavailable')
                print(
                    f'Aviso: registries e CDNs Chart.js indisponíveis ({registry_error}; {cdn_error}). '
                    'Build continuará em modo degradado com o renderer alternativo.',
                    file=sys.stderr,
                )
                return 0
            _install_direct_bundle(bundle)
            _write_status('chartjs', source=bundle_source)
            print(f'Chart.js {VERSION} obtido de CDN e verificado por SHA-384 em {TARGET}.')
            return 0

        # Digest mismatch, archive corruption, permanent HTTP errors, or a
        # missing UMD file are hard failures, never silent fallback conditions.
        _install_verified_bundle(archive_bytes)
        _write_status('chartjs', source='npm-tarball')
        print(f'Chart.js {VERSION} verificado por SHA-512 e instalado em {TARGET}.')
        return 0
    except Exception as exc:
        print(f'ERRO: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
