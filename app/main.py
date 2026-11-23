import os, sqlite3, time, json, threading, hashlib, io, re, unicodedata, uuid, shutil, secrets, hmac, math, tempfile, zipfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
from datetime import date, datetime, time as clock_time, timedelta, timezone
from zoneinfo import ZoneInfo
import requests
from cryptography.fernet import Fernet, InvalidToken
from openpyxl import load_workbook, Workbook
from flask import Flask, g, has_app_context, jsonify, render_template, request, send_file, session, redirect, url_for
from werkzeug.exceptions import RequestEntityTooLarge

app = Flask(__name__)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax', SESSION_COOKIE_SECURE=os.environ.get('SESSION_COOKIE_SECURE', '0').lower() in ('1', 'true', 'yes'), PERMANENT_SESSION_LIFETIME=timedelta(minutes=30), MAX_CONTENT_LENGTH=(16 * 1024 * 1024) + (256 * 1024), MAX_FORM_MEMORY_SIZE=500_000, MAX_FORM_PARTS=100)
# Dados da carteira ficam em um banco próprio; segredos de APIs ficam em tokens.db.
DB_PATH = os.environ.get('ASSETS_DB_PATH', '/data/ativos.db')
LEGACY_DB_PATH = os.environ.get('DB_PATH', '/data/carteira.db')
TOKEN_DB_PATH = os.environ.get('TOKEN_DB_PATH', '/data/tokens.db')
VERSION_FILE = Path(__file__).resolve().parents[1] / 'VERSION'
try:
    APP_VERSION = VERSION_FILE.read_text(encoding='utf-8').strip() or 'desconhecida'
except OSError:
    APP_VERSION = 'desconhecida'
USER_AGENT = f'Mozilla/5.0 LonelyWallet/{APP_VERSION}'


def sqlite_readonly_uri(path):
    """Build a correctly escaped SQLite file URI for paths containing ?, #, %, or spaces."""
    return Path(path).expanduser().resolve().as_uri() + '?mode=ro'


def _chart_assets_status():
    """Describe chart assets actually available to the browser.

    Chart.js is only advertised when its bundled file matches its generated
    SHA-256 sidecar. The lightweight fallback is always loaded after it.
    """
    vendor_dir = Path(app.static_folder or 'static') / 'vendor'
    bundle = vendor_dir / 'chart.umd.min.js'
    checksum = vendor_dir / 'chart.umd.min.js.sha256'
    fallback = vendor_dir / 'chart-fallback.js'
    chartjs_available = False
    try:
        expected = checksum.read_text(encoding='ascii').strip().split()[0].lower()
        payload = bundle.read_bytes()
        actual = hashlib.sha256(payload).hexdigest()
        chartjs_available = len(payload) >= 100_000 and len(expected) == 64 and hmac.compare_digest(actual, expected)
    except (OSError, ValueError, IndexError):
        chartjs_available = False
    try:
        fallback_available = fallback.is_file() and fallback.stat().st_size > 1_000
    except OSError:
        fallback_available = False
    if chartjs_available:
        engine, status, warning = 'chartjs', 'ready', None
    elif fallback_available:
        engine, status = 'fallback', 'degraded'
        warning = 'Chart.js local ausente ou sem integridade válida; renderer offline simplificado em uso.'
    else:
        engine, status = 'unavailable', 'unhealthy'
        warning = 'Chart.js e renderer de fallback não estão disponíveis.'
    return {
        'engine': engine,
        'status': status,
        'chartjs_version': '4.6.1',
        'chartjs_available': bool(chartjs_available),
        'chartjs_integrity_verified': bool(chartjs_available),
        'fallback_available': bool(fallback_available),
        'warning': warning,
    }


@app.context_processor
def inject_app_version():
    return {'app_version': APP_VERSION, 'chart_assets': _chart_assets_status()}
TOKEN = os.environ.get('BRAPI_TOKEN') or os.environ.get('BRAPI_API_KEY', '')
API_KEY_FILE = os.environ.get('API_KEYS_KEY_FILE', '/data/.api-credentials.key')
TZ = ZoneInfo('America/Sao_Paulo')
MARKET_OPEN = clock_time(10, 0)
MARKET_CLOSE = clock_time(17, 55)
CACHE, CACHE_TTL = {}, 300
QUOTE_HISTORY_PATH = os.environ.get('QUOTE_HISTORY_PATH', '/data/quotes_last_valid.json')
QUOTE_HISTORY_LOCK = threading.RLock()
DASHBOARD_CACHE_PATH = os.environ.get('DASHBOARD_CACHE_PATH', '/data/dashboard_cache.json')
DASHBOARD_CACHE_LOCK = threading.RLock()
DASHBOARD_CACHE_FORMAT = 7
PORTFOLIO_HISTORY_CACHE, PORTFOLIO_HISTORY_TTL = {}, 1800
PORTFOLIO_HISTORY_EMPTY_TTL = 120
def _dashboard_cache_signature():
    """Assinatura estável dos arquivos persistentes que alteram o dashboard.

    Arquivos WAL vazios são ignorados: o SQLite pode removê-los ao encerrar a
    conexão após um checkpoint sem que o conteúdo lógico do banco tenha mudado.
    WALs não vazios são incluídos para invalidar o cache após gravações recentes.
    """
    records = []
    for path in (DB_PATH, TOKEN_DB_PATH):
        try:
            stat = os.stat(path)
            records.append((path, int(stat.st_mtime_ns), int(stat.st_size)))
        except OSError:
            records.append((path, 0, 0))
        wal_path = path + '-wal'
        try:
            wal_stat = os.stat(wal_path)
            if wal_stat.st_size > 0:
                records.append((wal_path, int(wal_stat.st_mtime_ns), int(wal_stat.st_size)))
        except OSError:
            pass
    try:
        quote_stat = os.stat(QUOTE_HISTORY_PATH)
        records.append((QUOTE_HISTORY_PATH, int(quote_stat.st_mtime_ns), int(quote_stat.st_size)))
    except OSError:
        records.append((QUOTE_HISTORY_PATH, 0, 0))
    raw = json.dumps(records, separators=(',', ':'), ensure_ascii=True)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _checkpoint_dashboard_cache_sources():
    """Consolida WALs após a leitura completa do dashboard, antes de assinar o cache."""
    for path in (DB_PATH, TOKEN_DB_PATH):
        if not os.path.exists(path):
            continue
        connection = None
        try:
            connection = sqlite3.connect(path, timeout=15)
            connection.execute('PRAGMA busy_timeout=15000')
            connection.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        except sqlite3.Error as exc:
            app.logger.warning('Não foi possível consolidar o banco %s antes do cache: %s', path, exc)
        finally:
            if connection is not None:
                connection.close()


def _read_dashboard_snapshot():
    """Carrega apenas um snapshot completo que ainda corresponde aos dados persistidos."""
    try:
        with DASHBOARD_CACHE_LOCK:
            with open(DASHBOARD_CACHE_PATH, 'r', encoding='utf-8') as handle:
                record = json.load(handle)
        if not isinstance(record, dict) or record.get('format') != DASHBOARD_CACHE_FORMAT:
            return None
        if record.get('signature') != _dashboard_cache_signature():
            return None
        payload = record.get('payload')
        if not isinstance(payload, dict) or not isinstance(payload.get('itens'), list):
            return None
        return {
            'payload': payload,
            'saved_at': str(record.get('saved_at') or ''),
            'age_seconds': max(0, int(time.time() - float(record.get('saved_at_epoch') or time.time())))
        }
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _write_dashboard_snapshot(payload):
    """Grava atomicamente o resultado completo; nunca publica dados parciais."""
    if not isinstance(payload, dict):
        return ''
    _checkpoint_dashboard_cache_sources()
    saved_at = datetime.now(TZ).isoformat(timespec='seconds')
    record = {
        'format': DASHBOARD_CACHE_FORMAT,
        'saved_at': saved_at,
        'saved_at_epoch': time.time(),
        'signature': _dashboard_cache_signature(),
        'payload': payload
    }
    directory = os.path.dirname(DASHBOARD_CACHE_PATH) or '.'
    os.makedirs(directory, exist_ok=True)
    tmp_path = DASHBOARD_CACHE_PATH + '.tmp'
    with DASHBOARD_CACHE_LOCK:
        try:
            with open(tmp_path, 'w', encoding='utf-8') as handle:
                json.dump(record, handle, ensure_ascii=False, separators=(',', ':'))
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, DASHBOARD_CACHE_PATH)
        finally:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass
    return saved_at


PROVENTOS_SYNC_CACHE = {'time': 0, 'result': None, 'signature': None}
PROVENTOS_SYNC_TTL = 3600
SGS_CACHE = {}
SGS_CACHE_TTL = 12 * 3600
BENCHMARK_CACHE = {'time': 0, 'data': {}}
BENCHMARK_CACHE_TTL = 6 * 3600

# A reconstrução do histórico patrimonial pode exigir dezenas de chamadas à
# brapi no plano gratuito (1 ticker por requisição). Ela roda fora da resposta
# HTTP do dashboard para não bloquear a interface.
PERFORMANCE_HISTORY_LOCK = threading.RLock()
PERFORMANCE_HISTORY_STATE = {
    'status': 'idle', 'signature': '', 'pending_signature': '',
    'result': None, 'error': '', 'updated_at': ''
}
PERFORMANCE_HISTORY_PENDING_INPUTS = None

# A cota gratuita da brapi permite uma chamada simultânea e 1 ticker por chamada.
# O orçamento local usa 10.000 requisições em 30 dias para reservar margem para
# outras aplicações que compartilhem o mesmo token.
try:
    BRAPI_LOCAL_30D_BUDGET = max(0, int(os.environ.get('BRAPI_LOCAL_30D_BUDGET', '10000')))
except (TypeError, ValueError):
    BRAPI_LOCAL_30D_BUDGET = 10000
try:
    HGBRASIL_LOCAL_24H_BUDGET = max(0, int(os.environ.get('HGBRASIL_LOCAL_24H_BUDGET', '300')))
except (TypeError, ValueError):
    HGBRASIL_LOCAL_24H_BUDGET = 300
try:
    DADOSB3_LOCAL_24H_BUDGET = max(0, int(os.environ.get('DADOSB3_LOCAL_24H_BUDGET', '150')))
except (TypeError, ValueError):
    DADOSB3_LOCAL_24H_BUDGET = 150


def _provider_budget_config(provider):
    if provider == 'brapi':
        return BRAPI_LOCAL_30D_BUDGET, 30 * 86400
    if provider == 'hgbrasil':
        return HGBRASIL_LOCAL_24H_BUDGET, 24 * 3600
    if provider == 'dadosb3':
        return DADOSB3_LOCAL_24H_BUDGET, 24 * 3600
    return 0, 30 * 86400


try:
    BRAPI_MAX_TICKERS_PER_REQUEST = min(20, max(1, int(os.environ.get('BRAPI_MAX_TICKERS_PER_REQUEST', '1'))))
except (TypeError, ValueError):
    BRAPI_MAX_TICKERS_PER_REQUEST = 1
try:
    BRAPI_REQUEST_GAP_SECONDS = min(5.0, max(0.0, float(os.environ.get('BRAPI_REQUEST_GAP_SECONDS', '0.35'))))
except (TypeError, ValueError):
    BRAPI_REQUEST_GAP_SECONDS = 0.35
BRAPI_REQUEST_LOCK = threading.RLock()
_PROVIDER_REQUEST_LOCKS = {name: threading.RLock() for name in ('brapi', 'hgbrasil', 'dadosb3', 'yahoo', 'bcb', 'ibge', 'fred')}
_API_CACHE_CLEANUP_LOCK = threading.Lock()
_API_CACHE_LAST_CLEANUP = 0.0
_API_CACHE_MAX_BODY = 8 * 1024 * 1024


def _api_cache_connection():
    connection = sqlite3.connect(DB_PATH, timeout=15)
    connection.execute('PRAGMA busy_timeout=15000')
    return connection


def _api_request_fingerprint(params=None, headers=None):
    """Identifica a credencial sem armazenar token ou chave em cache/log."""
    secret_parts = []
    for key, value in (headers or {}).items():
        if str(key).lower() in ('authorization', 'x-api-key') and value:
            secret_parts.append(str(value))
    for key, value in (params or {}).items():
        if str(key).lower() in ('token', 'key', 'api_key', 'apikey') and value:
            secret_parts.append(str(value))
    material = '|'.join(sorted(secret_parts))
    return hashlib.sha256(material.encode('utf-8')).hexdigest()[:24] if material else 'public'


def _api_response_cache_key(provider, url, params=None, headers=None):
    safe_params = {
        str(key): value for key, value in (params or {}).items()
        if str(key).lower() not in ('token', 'key', 'api_key', 'apikey')
    }
    canonical = json.dumps({
        'provider': provider,
        'url': str(url),
        'params': safe_params,
        'credential': _api_request_fingerprint(params, headers),
    }, ensure_ascii=True, sort_keys=True, separators=(',', ':'), default=str)
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def _api_cache_row(cache_key):
    connection = None
    try:
        connection = _api_cache_connection()
        row = connection.execute(
            'SELECT payload,status_code,fetched_at,expires_at,stale_until FROM api_response_cache WHERE cache_key=?',
            (cache_key,)
        ).fetchone()
        if not row:
            return None
        payload = bytes(row[0] or b'')
        # Older installations may gain the payload column through migration.
        # Rows created with the empty migration default are not usable cache hits.
        if not payload:
            return None
        return {
            'payload': payload, 'status_code': int(row[1]), 'fetched_at': float(row[2]),
            'expires_at': float(row[3]), 'stale_until': float(row[4])
        }
    except (sqlite3.Error, TypeError, ValueError, OSError):
        return None
    finally:
        if connection is not None:
            connection.close()


def _api_cache_record_hit(provider, stale=False):
    connection = None
    try:
        connection = _api_cache_connection()
        connection.execute(
            "INSERT INTO api_cache_stats(provider,fresh_hits,stale_hits) VALUES(?,?,?) ON CONFLICT(provider) DO UPDATE SET fresh_hits=fresh_hits+excluded.fresh_hits, stale_hits=stale_hits+excluded.stale_hits",
            (provider, 0 if stale else 1, 1 if stale else 0)
        )
        connection.commit()
    except sqlite3.Error:
        pass
    finally:
        if connection is not None:
            connection.close()


def _provider_wait_for_gap(provider):
    # Persiste o intervalo entre chamadas para evitar rajadas após reinícios.
    gap = BRAPI_REQUEST_GAP_SECONDS if provider == 'brapi' else (PLUGGY_REQUEST_GAP_SECONDS if provider == 'pluggy' else 0.0)
    if gap <= 0 and provider not in ('brapi', 'pluggy'):
        return
    connection = None
    last_request = None
    try:
        connection = _api_cache_connection()
        row = connection.execute(
            'SELECT MAX(requested_at) FROM api_request_log WHERE provider=?', (provider,)
        ).fetchone()
        last_request = float(row[0]) if row and row[0] else None
        state = connection.execute(
            'SELECT cooldown_until FROM api_provider_state WHERE provider=?', (provider,)
        ).fetchone()
        cooldown_until = float(state[0] or 0) if state else 0
        if cooldown_until > time.time():
            raise ValueError(f'Consultas ao provedor {provider} pausadas por limite temporário. Tente novamente após {int(cooldown_until - time.time()) + 1}s.')
    except sqlite3.Error:
        # O orçamento brapi falha fechado se o banco não puder ser consultado.
        if provider in ('brapi', 'pluggy') and (provider != 'brapi' or BRAPI_LOCAL_30D_BUDGET > 0):
            raise
    finally:
        if connection is not None:
            connection.close()
    if gap > 0 and last_request is not None:
        remaining = gap - (time.time() - last_request)
        if remaining > 0:
            time.sleep(remaining)


def _api_cache_store(cache_key, provider, endpoint, payload, status_code, ttl, stale_ttl):
    if not isinstance(payload, (bytes, bytearray)) or len(payload) > _API_CACHE_MAX_BODY:
        return
    now = time.time()
    connection = None
    try:
        connection = _api_cache_connection()
        connection.execute(
            '''INSERT INTO api_response_cache(cache_key,provider,endpoint,payload,status_code,fetched_at,expires_at,stale_until)
               VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(cache_key) DO UPDATE SET
               provider=excluded.provider,endpoint=excluded.endpoint,payload=excluded.payload,
               status_code=excluded.status_code,fetched_at=excluded.fetched_at,
               expires_at=excluded.expires_at,stale_until=excluded.stale_until''',
            (cache_key, provider, endpoint, sqlite3.Binary(payload), int(status_code), now, now + ttl, now + max(ttl, stale_ttl))
        )
        connection.commit()
    except sqlite3.Error as exc:
        app.logger.warning('Não foi possível persistir o cache da API %s: %s', provider, exc)
    finally:
        if connection is not None:
            connection.close()


_API_REQUEST_LOG_LOCK = threading.RLock()

def _commit_pending_pluggy_writes_before_request_log(provider):
    """Release the portfolio DB write lock before logging a Pluggy HTTP call.

    Pluggy synchronization imports data into the same SQLite file used by the
    request log. Without this commit, a sync could write an investment row and
    then wait for a separate connection to insert its API log entry, while its
    own uncommitted transaction held SQLite's single-writer lock.
    """
    if str(provider or '').lower() != 'pluggy' or not has_app_context():
        return False
    try:
        connection = g.get('db')
        if connection is not None and connection.in_transaction:
            connection.commit()
            return True
    except (sqlite3.Error, RuntimeError) as exc:
        app.logger.warning('Não foi possível concluir a gravação local antes da chamada Pluggy: %s', exc)
        raise
    return False


def _api_record_request(provider, endpoint, status_code=0):
    """Registra o consumo com retries curtos para contornar locks SQLite transitórios.


    Nunca libera a chamada externa quando o registro de segurança falha.
    """
    request_id = None
    last_error = None
    # A sincronização Pluggy precisa encerrar suas gravações pendentes antes
    # de usar outra conexão para registrar a chamada na mesma base SQLite.
    _commit_pending_pluggy_writes_before_request_log(provider)
    # Serializa writes locais e tenta novamente quando outro worker está gravando.
    with _API_REQUEST_LOG_LOCK:
        for attempt in range(4):
            connection = None
            try:
                connection = _api_cache_connection()
                connection.execute('PRAGMA busy_timeout=15000')
                cursor = connection.execute(
                    'INSERT INTO api_request_log(provider,endpoint,requested_at,status_code) VALUES(?,?,?,?)',
                    (provider, endpoint, time.time(), int(status_code or 0))
                )
                connection.commit()
                request_id = int(cursor.lastrowid)
                last_error = None
                break
            except sqlite3.OperationalError as exc:
                last_error = exc
                # Retry only transient locking/busy errors; other errors fail closed.
                message = str(exc).lower()
                if not any(term in message for term in ('locked', 'busy')) or attempt >= 3:
                    break
                time.sleep(0.08 * (attempt + 1))
            except sqlite3.Error as exc:
                last_error = exc
                break
            finally:
                if connection is not None:
                    connection.close()
    if last_error is not None:
        app.logger.warning('Não foi possível registrar consumo da API %s (endpoint %s): %s',
                         provider, str(endpoint)[:80], last_error)
    return request_id


def _api_update_request_status(request_id, status_code):
    if not request_id:
        return
    connection = None
    try:
        connection = _api_cache_connection()
        connection.execute('UPDATE api_request_log SET status_code=? WHERE id=?', (int(status_code or 0), request_id))
        connection.commit()
    except sqlite3.Error:
        pass
    finally:
        if connection is not None:
            connection.close()


def _api_budget_used(provider='brapi', window_seconds=30 * 86400):
    connection = None
    try:
        connection = _api_cache_connection()
        row = connection.execute(
            'SELECT COUNT(*) FROM api_request_log WHERE provider=? AND requested_at>=?',
            (provider, time.time() - window_seconds)
        ).fetchone()
        return int(row[0] or 0)
    except sqlite3.Error:
        return _provider_budget_config(provider)[0] if _provider_budget_config(provider)[0] > 0 else 0
    finally:
        if connection is not None:
            connection.close()


def _cached_http_response(payload, url, status_code=200, stale=False):
    response = requests.Response()
    response.status_code = int(status_code)
    response._content = bytes(payload)
    response._content_consumed = True
    response.url = str(url)
    response.reason = 'OK' if status_code == 200 else 'Cached response'
    response.headers['Content-Type'] = 'application/json; charset=utf-8'
    response.headers['X-LonelyWallet-Cache'] = 'stale' if stale else 'hit'
    return response


def _synthetic_api_response(status_code, message, url, retry_after=None):
    response = requests.Response()
    response.status_code = int(status_code)
    response._content = json.dumps({'error': message}, ensure_ascii=False).encode('utf-8')
    response._content_consumed = True
    response.url = str(url)
    response.reason = 'Rate limit protection'
    response.headers['Content-Type'] = 'application/json; charset=utf-8'
    if retry_after:
        response.headers['Retry-After'] = str(retry_after)
    return response


def _provider_set_cooldown(provider, seconds, status_code, reason):
    """Pausa temporariamente chamadas após falhas transitórias, sem armazenar dados sensíveis."""
    connection = None
    try:
        now = time.time()
        connection = _api_cache_connection()
        connection.execute(
            """INSERT INTO api_provider_state(provider,cooldown_until,last_status_code,last_error)
               VALUES(?,?,?,?) ON CONFLICT(provider) DO UPDATE SET
               cooldown_until=MAX(api_provider_state.cooldown_until,excluded.cooldown_until),
               last_status_code=excluded.last_status_code,last_error=excluded.last_error""",
            (provider, now + max(1, int(seconds)), int(status_code or 0), str(reason or 'temporarily_unavailable')[:120])
        )
        connection.commit()
    except sqlite3.Error:
        pass
    finally:
        if connection is not None:
            connection.close()


def _provider_get(provider, url, params=None, headers=None, timeout=12, cache_ttl=0, stale_ttl=None,
                  budgeted=False):
    """GET cacheado em SQLite, compartilhado entre workers e reinicializações."""
    params = dict(params or {})
    headers = dict(headers or {})
    endpoint = str(url).split('?', 1)[0]
    cache_key = _api_response_cache_key(provider, url, params, headers)
    now = time.time()
    cached = _api_cache_row(cache_key)
    if cached and cached['expires_at'] > now:
        _api_cache_record_hit(provider, stale=False)
        return _cached_http_response(cached['payload'], url, cached['status_code'], stale=False)

    lock = BRAPI_REQUEST_LOCK if provider == 'brapi' else _PROVIDER_REQUEST_LOCKS.setdefault(provider, threading.RLock())
    with lock:
        now = time.time()
        cached = _api_cache_row(cache_key)
        if cached and cached['expires_at'] > now:
            _api_cache_record_hit(provider, stale=False)
            return _cached_http_response(cached['payload'], url, cached['status_code'], stale=False)

        stale_ttl = float(stale_ttl if stale_ttl is not None else max(cache_ttl * 4, 24 * 3600))
        local_budget, budget_window = _provider_budget_config(provider)
        if (budgeted or local_budget > 0) and local_budget > 0:
            used = _api_budget_used(provider, window_seconds=budget_window)
            if used >= local_budget:
                if cached and cached['stale_until'] > now:
                    _api_cache_record_hit(provider, stale=True)
                    return _cached_http_response(cached['payload'], url, cached['status_code'], stale=True)
                window_label = '30 dias' if budget_window >= 30 * 86400 else '24 horas'
                env_name = {'brapi':'BRAPI_LOCAL_30D_BUDGET','hgbrasil':'HGBRASIL_LOCAL_24H_BUDGET','dadosb3':'DADOSB3_LOCAL_24H_BUDGET'}.get(provider, 'LOCAL_API_BUDGET')
                return _synthetic_api_response(
                    429,
                    f'Orçamento local de segurança de {provider} atingido ({local_budget} chamadas em {window_label}). Aguarde a janela ou ajuste {env_name}.',
                    url, retry_after=3600
                )

        connection = None
        try:
            connection = _api_cache_connection()
            state = connection.execute(
                'SELECT cooldown_until FROM api_provider_state WHERE provider=?', (provider,)
            ).fetchone()
            cooldown_until = float(state[0] or 0) if state else 0
        except sqlite3.Error:
            cooldown_until = 0
        finally:
            if connection is not None:
                connection.close()
        if cooldown_until > now:
            if cached and cached['stale_until'] > now:
                _api_cache_record_hit(provider, stale=True)
                return _cached_http_response(cached['payload'], url, cached['status_code'], stale=True)
            return _synthetic_api_response(429, f'Consultas ao provedor {provider} pausadas temporariamente após limite de requisições.', url, retry_after=max(1, int(cooldown_until - now)))

        try:
            _provider_wait_for_gap(provider)
        except ValueError as exc:
            return _synthetic_api_response(429, str(exc), url, retry_after=60)
        except sqlite3.Error:
            return _synthetic_api_response(503, 'Não foi possível validar o intervalo seguro de consultas; a chamada externa foi bloqueada.', url, retry_after=60)
        request_id = _api_record_request(provider, endpoint, 0)
        if (budgeted or local_budget > 0 or provider == 'pluggy') and not request_id:
            if cached and cached['stale_until'] > now:
                _api_cache_record_hit(provider, stale=True)
                return _cached_http_response(cached['payload'], url, cached['status_code'], stale=True)
            return _synthetic_api_response(503, 'Não foi possível registrar o consumo da API com segurança; a chamada externa foi bloqueada.', url, retry_after=300)
        try:
            response = requests.get(url, params=params, headers=headers, timeout=timeout)
        except requests.RequestException:
            _api_update_request_status(request_id, 0)
            _provider_set_cooldown(provider, 30, 0, 'network_error')
            if cached and cached['stale_until'] > now:
                _api_cache_record_hit(provider, stale=True)
                return _cached_http_response(cached['payload'], url, cached['status_code'], stale=True)
            raise

        _api_update_request_status(request_id, response.status_code)
        if response.status_code == 429:
            retry_after = response.headers.get('Retry-After', '60')
            try:
                cooldown_seconds = min(6 * 3600, max(60, int(float(retry_after))))
            except (TypeError, ValueError):
                cooldown_seconds = 60
            try:
                detail = response.json()
                message = json.dumps(detail, ensure_ascii=False).lower()
            except (ValueError, TypeError):
                message = (response.text or '').lower()
            if provider == 'brapi' and any(word in message for word in ('quota', 'monthly', 'plano', 'plan limit')):
                cooldown_seconds = max(cooldown_seconds, 6 * 3600)
            connection = None
            try:
                connection = _api_cache_connection()
                connection.execute(
                    '''INSERT INTO api_provider_state(provider,cooldown_until,last_status_code,last_error)
                       VALUES(?,?,?,?) ON CONFLICT(provider) DO UPDATE SET cooldown_until=excluded.cooldown_until,
                       last_status_code=excluded.last_status_code,last_error=excluded.last_error''',
                    (provider, now + cooldown_seconds, 429, message[:300])
                )
                connection.commit()
            except sqlite3.Error:
                pass
            finally:
                if connection is not None:
                    connection.close()
            if cached and cached['stale_until'] > now:
                _api_cache_record_hit(provider, stale=True)
                return _cached_http_response(cached['payload'], url, cached['status_code'], stale=True)
        elif response.status_code >= 500:
            _provider_set_cooldown(provider, 30, response.status_code, 'upstream_5xx')
            if cached and cached['stale_until'] > now:
                _api_cache_record_hit(provider, stale=True)
                return _cached_http_response(cached['payload'], url, cached['status_code'], stale=True)
        elif 200 <= response.status_code < 300:
            _api_cache_store(cache_key, provider, endpoint, response.content, response.status_code, cache_ttl, stale_ttl)
        return response


def _brapi_market_cache_ttl():
    now = datetime.now(TZ)
    if now.weekday() < 5 and MARKET_OPEN <= now.time() <= MARKET_CLOSE:
        return 30 * 60
    return 12 * 60 * 60


def _brapi_get(url, params=None, headers=None, timeout=12, cache_ttl=None, stale_ttl=None):
    if cache_ttl is None:
        lowered = str(url).lower()
        if '/dividends' in lowered:
            cache_ttl = 12 * 3600
        elif '/historical' in lowered:
            cache_ttl = 24 * 3600
        elif '/quote/list' in lowered:
            cache_ttl = 6 * 3600
        elif '/user/usage' in lowered:
            cache_ttl = 15 * 60
        else:
            cache_ttl = _brapi_market_cache_ttl()
    if stale_ttl is None:
        stale_ttl = 7 * 86400 if cache_ttl <= 12 * 3600 else 30 * 86400
    return _provider_get('brapi', url, params=params, headers=headers, timeout=timeout,
                         cache_ttl=float(cache_ttl), stale_ttl=float(stale_ttl), budgeted=True)


def _api_cache_cleanup():
    global _API_CACHE_LAST_CLEANUP
    now = time.time()
    if now - _API_CACHE_LAST_CLEANUP < 3600:
        return
    if not _API_CACHE_CLEANUP_LOCK.acquire(blocking=False):
        return
    connection = None
    try:
        connection = _api_cache_connection()
        connection.execute('DELETE FROM api_response_cache WHERE stale_until<?', (now,))
        connection.execute('DELETE FROM api_request_log WHERE requested_at<?', (now - 40 * 86400,))
        connection.commit()
        _API_CACHE_LAST_CLEANUP = now
    except sqlite3.Error as exc:
        app.logger.debug('Limpeza do cache de APIs adiada: %s', exc)
    finally:
        if connection is not None:
            connection.close()
        _API_CACHE_CLEANUP_LOCK.release()


def _api_usage_snapshot():
    used = _api_budget_used('brapi')
    connection = None
    cache_entries = 0
    fresh_hits = stale_hits = 0
    last_request = None
    providers = {}
    try:
        connection = _api_cache_connection()
        cache_entries = int(connection.execute('SELECT COUNT(*) FROM api_response_cache WHERE provider=?', ('brapi',)).fetchone()[0])
        row = connection.execute('SELECT MAX(requested_at) FROM api_request_log WHERE provider=?', ('brapi',)).fetchone()
        last_request = float(row[0]) if row and row[0] else None
        stats = connection.execute('SELECT fresh_hits,stale_hits FROM api_cache_stats WHERE provider=?', ('brapi',)).fetchone()
        if stats:
            fresh_hits, stale_hits = int(stats[0] or 0), int(stats[1] or 0)
        rows = connection.execute(
            'SELECT provider,COUNT(*),MAX(requested_at) FROM api_request_log WHERE requested_at>=? GROUP BY provider',
            (time.time() - 30 * 86400,)
        ).fetchall()
        providers = {
            str(row[0]): {'requests_last_30_days': int(row[1] or 0),
                          'last_request_epoch': float(row[2]) if row[2] else None}
            for row in rows
        }
    except sqlite3.Error:
        pass
    finally:
        if connection is not None:
            connection.close()
    budget = BRAPI_LOCAL_30D_BUDGET
    provider_budgets = {}
    for name in ('brapi', 'hgbrasil', 'dadosb3'):
        limit, window_seconds = _provider_budget_config(name)
        count = _api_budget_used(name, window_seconds=window_seconds)
        provider_budgets[name] = {
            'requests': count, 'local_budget': limit,
            'remaining': max(0, limit - count) if limit > 0 else None,
            'window_hours': int(window_seconds / 3600), 'enabled': limit > 0
        }
    return {
        'provider': 'brapi', 'window_days': 30, 'requests_last_30_days': used,
        'local_budget': budget, 'remaining_local_budget': max(0, budget - used) if budget > 0 else None,
        'budget_enabled': budget > 0, 'provider_budgets': provider_budgets,
        'cache_entries': cache_entries, 'cache_hits': fresh_hits, 'stale_cache_hits': stale_hits,
        'last_request_epoch': last_request, 'providers': providers,
        'note': 'Contagem local desta instalação; outras aplicações que compartilham os tokens não são contabilizadas.'
    }




@app.after_request
def security_headers(response):
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    response.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
    response.headers.setdefault('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')
    response.headers.setdefault('Content-Security-Policy', "default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'self'; form-action 'self'; img-src 'self' data: blob: https://icons.brapi.dev; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; font-src 'self' data:")
    return response


def db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=15)
        g.db.execute('PRAGMA busy_timeout=15000')
        g.db.execute('PRAGMA synchronous=NORMAL')
        g.db.row_factory = sqlite3.Row
        g.db.execute('PRAGMA foreign_keys=ON')
    return g.db


def token_db():
    if 'token_db' not in g:
        os.makedirs(os.path.dirname(TOKEN_DB_PATH) or '.', exist_ok=True)
        g.token_db = sqlite3.connect(TOKEN_DB_PATH, timeout=15)
        g.token_db.execute('PRAGMA busy_timeout=15000')
        g.token_db.row_factory = sqlite3.Row
    return g.token_db


@app.teardown_appcontext
def close_db(_):
    con = g.pop('db', None)
    if con:
        con.close()
    secrets = g.pop('token_db', None)
    if secrets:
        secrets.close()


def migrate_legacy_database(source_path, destination_path):
    """Copy a legacy SQLite database consistently, validate it, then publish atomically."""
    source = Path(source_path)
    destination = Path(destination_path)
    if source.resolve() == destination.resolve() or destination.exists() or not source.exists():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f'.{destination.name}.migration-', suffix='.tmp', dir=str(destination.parent))
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        # Use SQLite's online backup API so committed WAL pages are included.
        src = sqlite3.connect(sqlite_readonly_uri(source), uri=True, timeout=30)
        dst = sqlite3.connect(str(temporary), timeout=30)
        try:
            src.backup(dst, pages=256, sleep=0.05)
            check = dst.execute('PRAGMA integrity_check').fetchone()
            if not check or str(check[0]).lower() != 'ok':
                raise RuntimeError(f'Banco legado inválido: {check[0] if check else "sem resultado"}')
            dst.commit()
        finally:
            dst.close()
            src.close()
        # Reopen and explicitly close the published snapshot before replacement.
        verify = sqlite3.connect(sqlite_readonly_uri(temporary), uri=True, timeout=30)
        try:
            check = verify.execute('PRAGMA integrity_check').fetchone()
            if not check or str(check[0]).lower() != 'ok':
                raise RuntimeError(f'Banco temporário inválido: {check[0] if check else "sem resultado"}')
        finally:
            verify.close()
        os.replace(temporary, destination)
        return True
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def prepare_database_files():
    os.makedirs(os.path.dirname(DB_PATH) or '.', exist_ok=True)
    if os.path.abspath(DB_PATH) != os.path.abspath(LEGACY_DB_PATH):
        migrate_legacy_database(LEGACY_DB_PATH, DB_PATH)
    os.makedirs(os.path.dirname(TOKEN_DB_PATH) or '.', exist_ok=True)
    con = sqlite3.connect(TOKEN_DB_PATH, timeout=15)
    con.execute('PRAGMA busy_timeout=15000')
    con.execute('''CREATE TABLE IF NOT EXISTS app_auth (
        id INTEGER PRIMARY KEY CHECK(id=1), pin_salt TEXT NOT NULL, pin_hash TEXT NOT NULL,
        failed_attempts INTEGER NOT NULL DEFAULT 0, locked_until REAL NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1
    )''')
    auth_cols = {row[1] for row in con.execute('PRAGMA table_info(app_auth)').fetchall()}
    if 'enabled' not in auth_cols: con.execute('ALTER TABLE app_auth ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1')
    con.execute('''CREATE TABLE IF NOT EXISTS auth_attempts (
        client_ip TEXT PRIMARY KEY, failed_attempts INTEGER NOT NULL DEFAULT 0, locked_until REAL NOT NULL DEFAULT 0, updated_at TEXT NOT NULL
    )''')
    con.execute('''CREATE TABLE IF NOT EXISTS api_credentials (
        provider TEXT PRIMARY KEY, token_encrypted TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL
    )''')
    con.commit()
    con.close()


def migrate_api_credentials():
    if not os.path.exists(DB_PATH):
        return
    assets = sqlite3.connect(DB_PATH)
    assets.row_factory = sqlite3.Row
    try:
        exists = assets.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='api_credentials'").fetchone()
        if not exists:
            return
        rows = assets.execute('SELECT provider,token_encrypted,enabled,updated_at FROM api_credentials').fetchall()
        if rows:
            secrets = sqlite3.connect(TOKEN_DB_PATH)
            secrets.executemany(
                'INSERT INTO api_credentials(provider,token_encrypted,enabled,updated_at) VALUES(?,?,?,?) '
                'ON CONFLICT(provider) DO UPDATE SET token_encrypted=excluded.token_encrypted, '
                'enabled=excluded.enabled,updated_at=excluded.updated_at',
                [(r['provider'],r['token_encrypted'],r['enabled'],r['updated_at']) for r in rows]
            )
            secrets.commit()
            secrets.close()
        assets.execute('DELETE FROM api_credentials')
        assets.commit()
    finally:
        assets.close()


def _migrate_api_response_cache_schema(connection):
    """Add cache columns missing from older persistent SQLite databases.

    ALTER TABLE preserves existing rows. Empty payload defaults are treated as
    cache misses by _api_cache_row, so they are naturally repopulated on demand.
    """
    columns = {row[1] for row in connection.execute('PRAGMA table_info(api_response_cache)').fetchall()}
    additions = {
        'endpoint': "TEXT NOT NULL DEFAULT ''",
        'payload': "BLOB NOT NULL DEFAULT X''",
        'status_code': 'INTEGER NOT NULL DEFAULT 200',
        'fetched_at': 'REAL NOT NULL DEFAULT 0',
        'expires_at': 'REAL NOT NULL DEFAULT 0',
        'stale_until': 'REAL NOT NULL DEFAULT 0',
    }
    for name, definition in additions.items():
        if name not in columns:
            connection.execute(f'ALTER TABLE api_response_cache ADD COLUMN {name} {definition}')
            columns.add(name)
    return columns


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or '.', exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.execute('PRAGMA busy_timeout=15000')
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA synchronous=NORMAL')
    con.executescript('''
    CREATE TABLE IF NOT EXISTS operacoes (
      id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL,
      tipo TEXT NOT NULL, lado TEXT NOT NULL, quantidade REAL NOT NULL,
      preco REAL NOT NULL, data TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS proventos (
      id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL,
      valor_total REAL NOT NULL, data TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS metas (
      id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT NOT NULL,
      objetivo REAL NOT NULL, aporte REAL DEFAULT 0, tipo TEXT DEFAULT 'patrimonio',
      categoria TEXT DEFAULT '', ticker TEXT DEFAULT '', variacao_anual REAL DEFAULT 0, classes TEXT DEFAULT '[]'
    );
    CREATE TABLE IF NOT EXISTS distribuicao_alvo (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      nome TEXT NOT NULL,
      percentual REAL NOT NULL DEFAULT 0,
      ordem INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS irpf_lancamentos (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      grupo TEXT NOT NULL CHECK(grupo IN ('isento','exclusiva')),
      natureza TEXT NOT NULL,
      fonte_pagadora TEXT NOT NULL,
      documento TEXT DEFAULT '',
      valor REAL NOT NULL CHECK(valor >= 0),
      data TEXT NOT NULL,
      observacoes TEXT DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS darfs (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      competencia TEXT NOT NULL,
      codigo TEXT NOT NULL,
      valor REAL NOT NULL CHECK(valor >= 0),
      vencimento TEXT NOT NULL,
      pago_em TEXT DEFAULT '',
      status TEXT NOT NULL DEFAULT 'pendente' CHECK(status IN ('pendente','pago')),
      observacoes TEXT DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS saldos_importados (
      ticker TEXT NOT NULL,
      tipo TEXT NOT NULL,
      quantidade REAL NOT NULL DEFAULT 0,
      preco_medio REAL NOT NULL DEFAULT 0,
      preco_base REAL NOT NULL DEFAULT 0,
      data_base TEXT NOT NULL,
      nome TEXT NOT NULL DEFAULT '',
      origem TEXT NOT NULL DEFAULT '',
      PRIMARY KEY (ticker, data_base)
    );
    CREATE INDEX IF NOT EXISTS idx_saldos_importados_data ON saldos_importados(data_base);
    CREATE TABLE IF NOT EXISTS proventos_api (
      event_key TEXT PRIMARY KEY,
      ticker TEXT NOT NULL,
      tipo_ativo TEXT NOT NULL DEFAULT 'outros',
      tipo_evento TEXT NOT NULL DEFAULT 'Provento',
      data_aprovacao TEXT DEFAULT '',
      data_com TEXT NOT NULL,
      data_ex TEXT DEFAULT '',
      data_pagamento TEXT NOT NULL,
      valor_por_ativo REAL NOT NULL DEFAULT 0,
      quantidade REAL NOT NULL DEFAULT 0,
      valor_total REAL NOT NULL DEFAULT 0,
      status TEXT NOT NULL DEFAULT 'A receber',
      remarks TEXT DEFAULT '',
      sincronizado_em TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_proventos_api_ticker_pagamento ON proventos_api(ticker,data_pagamento);
    CREATE TABLE IF NOT EXISTS integracao_estado (
      chave TEXT PRIMARY KEY,
      valor TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS api_response_cache (
      cache_key TEXT PRIMARY KEY, provider TEXT NOT NULL, endpoint TEXT NOT NULL,
      payload BLOB NOT NULL, status_code INTEGER NOT NULL DEFAULT 200,
      fetched_at REAL NOT NULL, expires_at REAL NOT NULL, stale_until REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_api_response_cache_expiry ON api_response_cache(provider, expires_at, stale_until);
    CREATE TABLE IF NOT EXISTS api_request_log (
      id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL, endpoint TEXT NOT NULL,
      requested_at REAL NOT NULL, status_code INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_api_request_log_provider_time ON api_request_log(provider, requested_at);
    CREATE TABLE IF NOT EXISTS api_cache_stats (
      provider TEXT PRIMARY KEY, fresh_hits INTEGER NOT NULL DEFAULT 0, stale_hits INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS api_provider_state (
      provider TEXT PRIMARY KEY, cooldown_until REAL NOT NULL DEFAULT 0,
      last_status_code INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS b3_importados (
      fingerprint TEXT PRIMARY KEY,
      categoria TEXT NOT NULL,
      importado_em TEXT NOT NULL, lote_id TEXT, registro_id INTEGER
    );
    CREATE TABLE IF NOT EXISTS b3_import_lotes (
      id TEXT PRIMARY KEY, arquivo TEXT NOT NULL DEFAULT '', importado_em TEXT NOT NULL,
      operacoes INTEGER NOT NULL DEFAULT 0, proventos INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '');
    CREATE TABLE IF NOT EXISTS pluggy_items (
      item_id TEXT PRIMARY KEY,
      label TEXT NOT NULL DEFAULT '',
      connector_name TEXT NOT NULL DEFAULT '',
      status TEXT NOT NULL DEFAULT '',
      consent_expires_at TEXT DEFAULT '',
      active INTEGER NOT NULL DEFAULT 1,
      last_synced_at TEXT DEFAULT '',
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS pluggy_accounts (
      account_id TEXT PRIMARY KEY,
      item_id TEXT NOT NULL,
      name TEXT NOT NULL DEFAULT '',
      account_type TEXT NOT NULL DEFAULT '',
      subtype TEXT NOT NULL DEFAULT '',
      number TEXT NOT NULL DEFAULT '',
      balance REAL NOT NULL DEFAULT 0,
      currency_code TEXT NOT NULL DEFAULT 'BRL',
      raw_json TEXT NOT NULL DEFAULT '{}',
      updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_pluggy_accounts_item ON pluggy_accounts(item_id);
    CREATE TABLE IF NOT EXISTS pluggy_investments (
      investment_id TEXT PRIMARY KEY,
      item_id TEXT NOT NULL,
      code TEXT NOT NULL DEFAULT '',
      name TEXT NOT NULL DEFAULT '',
      investment_type TEXT NOT NULL DEFAULT '',
      subtype TEXT NOT NULL DEFAULT '',
      quantity REAL NOT NULL DEFAULT 0,
      amount_original REAL NOT NULL DEFAULT 0,
      balance REAL NOT NULL DEFAULT 0,
      unit_value REAL NOT NULL DEFAULT 0,
      status TEXT NOT NULL DEFAULT '',
      raw_json TEXT NOT NULL DEFAULT '{}',
      updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_pluggy_investments_item ON pluggy_investments(item_id);
    CREATE TABLE IF NOT EXISTS pluggy_position_snapshots (
      item_id TEXT NOT NULL,
      investment_id TEXT NOT NULL,
      ticker TEXT NOT NULL,
      asset_type TEXT NOT NULL DEFAULT 'outros',
      quantity REAL NOT NULL DEFAULT 0,
      amount_original REAL NOT NULL DEFAULT 0,
      balance REAL NOT NULL DEFAULT 0,
      snapshot_at TEXT NOT NULL,
      PRIMARY KEY(item_id,investment_id)
    );
    CREATE INDEX IF NOT EXISTS idx_pluggy_position_snapshots_ticker ON pluggy_position_snapshots(ticker);
    CREATE TABLE IF NOT EXISTS pluggy_sync_runs (
      run_id TEXT PRIMARY KEY,
      item_id TEXT NOT NULL,
      started_at TEXT NOT NULL,
      finished_at TEXT DEFAULT '',
      status TEXT NOT NULL DEFAULT 'running',
      last_investment_id TEXT DEFAULT '',
      error TEXT DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_pluggy_sync_runs_item_started ON pluggy_sync_runs(item_id,started_at);
    CREATE TABLE IF NOT EXISTS pluggy_transactions (
      transaction_id TEXT PRIMARY KEY,
      item_id TEXT NOT NULL,
      account_id TEXT NOT NULL,
      transaction_date TEXT NOT NULL DEFAULT '',
      description TEXT NOT NULL DEFAULT '',
      amount REAL NOT NULL DEFAULT 0,
      direction TEXT NOT NULL DEFAULT '',
      status TEXT NOT NULL DEFAULT '',
      raw_json TEXT NOT NULL DEFAULT '{}',
      updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_pluggy_transactions_item ON pluggy_transactions(item_id);
    CREATE TABLE IF NOT EXISTS pluggy_investment_transactions (
      transaction_id TEXT PRIMARY KEY,
      item_id TEXT NOT NULL,
      investment_id TEXT NOT NULL,
      ticker TEXT NOT NULL DEFAULT '',
      transaction_date TEXT NOT NULL DEFAULT '',
      description TEXT NOT NULL DEFAULT '',
      movement_type TEXT NOT NULL DEFAULT '',
      quantity REAL NOT NULL DEFAULT 0,
      unit_value REAL NOT NULL DEFAULT 0,
      amount REAL NOT NULL DEFAULT 0,
      import_state TEXT NOT NULL DEFAULT 'imported_only',
      imported_operation_id INTEGER,
      raw_json TEXT NOT NULL DEFAULT '{}',
      updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_pluggy_invtx_item ON pluggy_investment_transactions(item_id);
    CREATE TABLE IF NOT EXISTS pluggy_imported_events (
      external_id TEXT PRIMARY KEY,
      event_type TEXT NOT NULL,
      ticker TEXT NOT NULL DEFAULT '',
      event_date TEXT NOT NULL DEFAULT '',
      amount REAL NOT NULL DEFAULT 0,
      target_id INTEGER,
      import_state TEXT NOT NULL DEFAULT '',
      created_at TEXT NOT NULL
    );
    ''')
    # Preserve all Pluggy history and account cache across restarts and upgrades.
    goal_columns = [row[1] for row in con.execute('PRAGMA table_info(metas)').fetchall()]
    for name, definition in [('categoria', "TEXT DEFAULT ''"), ('ticker', "TEXT DEFAULT ''"), ('variacao_anual', 'REAL DEFAULT 0'), ('classes', "TEXT DEFAULT '[]'")]:
        if name not in goal_columns:
            con.execute(f'ALTER TABLE metas ADD COLUMN {name} {definition}')
    columns = [row[1] for row in con.execute('PRAGMA table_info(operacoes)').fetchall()]
    if 'custos' not in columns:
        con.execute('ALTER TABLE operacoes ADD COLUMN custos REAL NOT NULL DEFAULT 0')
    for name, definition in [('status', "TEXT NOT NULL DEFAULT 'executada'"), ('data_registro', 'TEXT'), ('data_execucao', 'TEXT'), ('detalhes', "TEXT NOT NULL DEFAULT '{}'")]:
        if name not in columns:
            con.execute(f'ALTER TABLE operacoes ADD COLUMN {name} {definition}')
    con.execute("UPDATE operacoes SET data_registro=COALESCE(data_registro,data), data_execucao=COALESCE(data_execucao,data||'T10:00:00-03:00') WHERE data_registro IS NULL OR data_execucao IS NULL")
    count = con.execute('SELECT COUNT(*) FROM distribuicao_alvo').fetchone()[0]
    if count == 0:
        defaults = [
            ('Renda Fixa', 40, 1), ('Ações', 20, 2), ('FIIs', 20, 3),
            ('ETFs', 10, 4), ('ETFs Internacionais', 10, 5)
        ]
        con.executemany('INSERT INTO distribuicao_alvo(nome,percentual,ordem) VALUES(?,?,?)', defaults)

    # Saldo-base capturado em 31/10/2026. Esta fotografia foi importada para
    # reconciliar a posição do usuário, mas não é um lançamento. Ela só pode
    # existir enquanto houver operações na carteira. Na versão 45, se o usuário
    # apagar todas as operações, removemos essa fotografia uma única vez e
    # gravamos o estado para que ela nunca seja recriada em reinicializações.
    snapshot_origin = 'Posição final informada pelo usuário (captura de 31/10/2026)'
    snapshot_date = '2026-10-31'
    snapshot_state_key = 'snapshot_20261031_lifecycle_v45'
    snapshot_rows = [
        ('PETR4','acao',43,40.22,52.66,'Petrobras PN'),
        ('VALE3','acao',23,85.62,62.09,'Vale ON'),
        ('BBDC4','acao',67,18.23,13.86,'Bradesco PN'),
        ('BBAS3','acao',53,22.90,16.45,'Banco do Brasil ON'),
        ('SANB4','acao',38,14.16,11.96,'Santander PN'),
        ('BBSE3','acao',7,34.55,41.24,'BB Seguridade ON'),
        ('WEGE3','acao',2,42.88,48.39,'WEG ON'),
        ('KLBN4','acao',23,3.70,3.40,'Klabin PN'),
        ('HGLG11','fii',6,152.95,149.47,'Pátria Logística FII'),
        ('RBRR11','fii',13,69.81,65.01,'RBR Rendimento High Grade FII'),
        ('MXRF11','fii',92,9.60,8.90,'Maxi Renda FII'),
        ('HGRU11','fii',7,116.48,114.00,'Pátria Renda Urbana FII'),
        ('BTLG11','fii',8,99.91,98.10,'BTG Pactual Logística FII'),
        ('XPML11','fii',8,106.40,96.80,'XP Malls FII'),
        ('CPTS11','fii',109,7.86,7.00,'Capitânia Securities II FII'),
        ('KNRI11','fii',5,156.90,150.50,'Kinea Renda Imobiliária FII'),
        ('VGIR11','fii',81,9.64,8.92,'Valora RE III FII'),
        ('VISC11','fii',5,104.10,99.40,'Vinci Shopping Centers FII'),
        ('KNSC11','fii',11,9.04,8.78,'Kinea Securities FII'),
        ('HGCR11','fii',1,93.23,92.98,'Patria Crédito Imobiliário FII'),
        # Renda fixa: uma aplicação por registro. O saldo é exato; o custo foi
        # reconstituído pelo saldo e pela rentabilidade exibidos na captura.
        ('CDB_NUBANK_120_CDI','renda_fixa',1,19991.77,21695.07,'CDB - Nubank - Pós-Fixado - 120% CDI'),
        ('LCA_BANCO_ORIGINAL_93_CDI','renda_fixa',1,7995.83,8499.57,'LCA - Banco Original - Pós-Fixado - 93% CDI'),
        ('CDB_NUBANK_100_CDI','renda_fixa',1,1999.96,2009.16,'CDB - Nubank - Pós-Fixado - 100% CDI'),
        ('CDB_MERCADO_PAGO_120_CDI','renda_fixa',1,114.69,125.05,'CDB - Mercado Pago - Pós-Fixado - 120% CDI')
    ]
    lifecycle = con.execute(
        'SELECT valor FROM integracao_estado WHERE chave=?', (snapshot_state_key,)
    ).fetchone()
    operations_count = int(con.execute('SELECT COUNT(*) FROM operacoes').fetchone()[0])
    existing_snapshot_count = int(con.execute(
        'SELECT COUNT(*) FROM saldos_importados WHERE data_base=? AND origem=?',
        (snapshot_date, snapshot_origin)
    ).fetchone()[0])

    # Migração de carteiras v44: se todos os lançamentos já foram apagados,
    # o saldo-base fixo antigo não pode continuar aparecendo como patrimônio.
    if lifecycle is None:
        if existing_snapshot_count and operations_count == 0:
            con.execute(
                'DELETE FROM saldos_importados WHERE data_base=? AND origem=?',
                (snapshot_date, snapshot_origin)
            )
            lifecycle_value = 'reset_empty'
        elif existing_snapshot_count:
            lifecycle_value = 'active'
        elif operations_count > 0:
            # Não recriar posições históricas fixas em uma carteira nova/limpa.
            lifecycle_value = 'empty'
        else:
            # Instalação vazia: não plantar posições pré-cadastradas.
            lifecycle_value = 'empty'
        con.execute(
            'INSERT INTO integracao_estado(chave,valor) VALUES(?,?)',
            (snapshot_state_key, lifecycle_value)
        )
    elif operations_count == 0 and lifecycle[0] == 'active':
        con.execute(
            'DELETE FROM saldos_importados WHERE data_base=? AND origem=?',
            (snapshot_date, snapshot_origin)
        )
        con.execute(
            'UPDATE integracao_estado SET valor=? WHERE chave=?',
            ('reset_empty', snapshot_state_key)
        )

    # Instalação limpa: não importar capturas nem inserir operações/proventos de demonstração.
    # Os registros já existentes em um banco persistente são preservados.

    b3cols = {row[1] for row in con.execute('PRAGMA table_info(b3_importados)').fetchall()}
    if 'lote_id' not in b3cols: con.execute('ALTER TABLE b3_importados ADD COLUMN lote_id TEXT')
    if 'registro_id' not in b3cols: con.execute('ALTER TABLE b3_importados ADD COLUMN registro_id INTEGER')

    # Upgrade older persistent installations without discarding their API cache.
    _migrate_api_response_cache_schema(con)
    request_columns = {row[1] for row in con.execute('PRAGMA table_info(api_request_log)').fetchall()}
    if 'endpoint' not in request_columns:
        con.execute("ALTER TABLE api_request_log ADD COLUMN endpoint TEXT NOT NULL DEFAULT ''")
    con.commit(); con.close()


prepare_database_files()
init_db()
migrate_api_credentials()


def remove_retired_api_credentials():
    """Remove credenciais de provedores descontinuados, sem tocar nas demais APIs."""
    con = sqlite3.connect(TOKEN_DB_PATH, timeout=15)
    try:
        con.execute('PRAGMA busy_timeout=15000')
        con.execute("DELETE FROM api_credentials WHERE provider IN ('twelvedata','alphavantage')")
        con.commit()
    finally:
        con.close()


remove_retired_api_credentials()

# Persist the Flask signing key in the mounted data directory so container recreation
# does not invalidate the PIN setup or expose a predictable development secret.
def load_session_secret():
    path = os.environ.get('SESSION_SECRET_FILE', '/data/.lonely-wallet-session.key')
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    if not os.path.exists(path):
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='ascii') as f:
            f.write(secrets.token_urlsafe(48))
        os.chmod(tmp, 0o600)
        try:
            os.replace(tmp, path)
        except FileNotFoundError:
            pass
    with open(path, 'r', encoding='ascii') as f:
        return f.read().strip()

app.secret_key = load_session_secret()

# A new boot identifier is generated on every application process start.
# Sessions created by a previous process are rejected after a Docker restart.
BOOT_ID = os.environ.get('LONELY_WALLET_BOOT_ID') or secrets.token_urlsafe(24)
IDLE_TIMEOUT_SECONDS = 30 * 60


def auth_record():
    con = sqlite3.connect(TOKEN_DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    try:
        return con.execute('SELECT * FROM app_auth WHERE id=1').fetchone()
    finally:
        con.close()


def pin_digest(pin, salt):
    return hashlib.pbkdf2_hmac('sha256', pin.encode('utf-8'), bytes.fromhex(salt), 310000).hex()


def _auth_client_ip():
    # Usa o IP observado pelo Flask; não confia em X-Forwarded-For sem proxy confiável configurado.
    return str(request.remote_addr or 'unknown')[:64]


def _auth_failure(con, row=None, now=None):
    now = time.time() if now is None else now
    ip = _auth_client_ip()
    current = con.execute('SELECT failed_attempts,updated_at FROM auth_attempts WHERE client_ip=?', (ip,)).fetchone()
    attempts = 0
    if current:
        try:
            age = (datetime.now(TZ) - datetime.fromisoformat(str(current['updated_at']))).total_seconds()
        except Exception:
            age = 10**9
        attempts = int(current['failed_attempts'] or 0) if age <= 900 else 0
    attempts += 1
    locked_until = now + 300 if attempts >= 5 else 0
    con.execute("""INSERT INTO auth_attempts(client_ip,failed_attempts,locked_until,updated_at) VALUES(?,?,?,?)
        ON CONFLICT(client_ip) DO UPDATE SET failed_attempts=excluded.failed_attempts,
        locked_until=excluded.locked_until,updated_at=excluded.updated_at""",
        (ip, 0 if locked_until else attempts, locked_until, datetime.now(TZ).isoformat()))
    # Limpa o contador legado global para que uma origem não bloqueie todos os usuários.
    con.execute('UPDATE app_auth SET failed_attempts=0,locked_until=0 WHERE id=1')
    con.commit()
    return locked_until


def _auth_lock_response(row=None, now=None, con=None):
    now = time.time() if now is None else now
    if con is not None:
        state = con.execute('SELECT locked_until FROM auth_attempts WHERE client_ip=?', (_auth_client_ip(),)).fetchone()
        locked_until = float(state[0] or 0) if state else 0
    else:
        locked_until = 0
    if locked_until > now:
        wait = max(1, int(locked_until - now))
        return jsonify(ok=False, error=f'Muitas tentativas deste endereço. Aguarde {wait} segundos.'), 429
    return None


def _auth_reset_client(con):
    con.execute('DELETE FROM auth_attempts WHERE client_ip=?', (_auth_client_ip(),))


def _origin_is_same_host():
    origin = request.headers.get('Origin')
    if not origin:
        return True  # Clientes não-browser podem não enviar Origin; SameSite continua ativo.
    from urllib.parse import urlparse
    try:
        return urlparse(origin).netloc.lower() == request.host.lower()
    except Exception:
        return False


@app.before_request
def require_pin_authentication():
    # Verifica origem também nos endpoints públicos de autenticação para reduzir CSRF.
    if request.method in ('POST', 'PUT', 'PATCH', 'DELETE') and not _origin_is_same_host():
        return jsonify(ok=False, error='Origem da requisição não autorizada.'), 403
    if request.endpoint in ('auth_status', 'auth_setup', 'auth_login', 'auth_disable', 'health', 'static') or request.path.startswith('/static/'):
        return None
    record = auth_record()
    if not record or not record['enabled'] or not record['pin_hash']:
        return None

    now = time.time()
    authenticated = bool(session.get('authenticated'))
    same_process = session.get('boot_id') == BOOT_ID
    last_activity = float(session.get('last_activity') or 0)
    within_idle_limit = last_activity > 0 and (now - last_activity) <= IDLE_TIMEOUT_SECONDS

    if authenticated and (not same_process or not within_idle_limit):
        session.clear()
        authenticated = False

    if not authenticated:
        if request.path == '/' or not request.path.startswith('/api/'):
            return render_template('auth.html')
        return jsonify(ok=False, error='Autenticação necessária.', auth_required=True), 401

    # Sliding inactivity timeout: activity renews the 30-minute idle window.
    session['last_activity'] = now
    session['boot_id'] = BOOT_ID
    session.permanent = True

    return None


@app.route('/api/auth/status')
def auth_status():
    row = auth_record()
    configured = bool(row and row['pin_hash'])
    enabled = bool(row and row['enabled'] and configured)
    return jsonify(configured=configured, enabled=enabled, authenticated=bool(session.get('authenticated')), idle_timeout_minutes=30)


@app.route('/api/auth/setup', methods=['POST'])
def auth_setup():
    payload = request.get_json(silent=True) or {}
    pin = str(payload.get('pin') or '')
    confirm = str(payload.get('confirm') or '')
    existing = auth_record()
    if existing is not None and existing['pin_hash'] and existing['enabled']:
        return jsonify(ok=False, error='O PIN já está ativo. Use a opção de alteração.'), 409
    if not re.fullmatch(r'\d{6,12}', pin) or pin != confirm:
        return jsonify(ok=False, error='Use um PIN de 6 a 12 números e confirme corretamente.'), 400
    salt = secrets.token_bytes(16).hex()
    digest = pin_digest(pin, salt)
    con = sqlite3.connect(TOKEN_DB_PATH, timeout=15)
    try:
        if existing is None:
            con.execute('INSERT INTO app_auth(id,pin_salt,pin_hash,updated_at,enabled) VALUES(1,?,?,?,1)', (salt, digest, datetime.now(TZ).isoformat()))
        else:
            con.execute('UPDATE app_auth SET pin_salt=?,pin_hash=?,updated_at=?,enabled=1,failed_attempts=0,locked_until=0 WHERE id=1', (salt, digest, datetime.now(TZ).isoformat()))
        con.commit()
    except sqlite3.IntegrityError:
        return jsonify(ok=False, error='O PIN já foi configurado.'), 409
    finally:
        con.close()
    session.clear(); session['authenticated'] = True; session['boot_id'] = BOOT_ID; session['last_activity'] = time.time(); session.permanent = True
    return jsonify(ok=True)


@app.route('/api/auth/disable', methods=['POST'])
def auth_disable():
    payload = request.get_json(silent=True) or {}
    con = sqlite3.connect(TOKEN_DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    try:
        con.execute('BEGIN IMMEDIATE')
        row = con.execute('SELECT * FROM app_auth WHERE id=1').fetchone()
        if row and row['pin_hash']:
            lock_response = _auth_lock_response(row, con=con)
            if lock_response:
                return lock_response
            current = str(payload.get('pin') or '')
            candidate = pin_digest(current, row['pin_salt']) if re.fullmatch(r"\d{6,12}", current) else ''
            if not hmac.compare_digest(candidate, row['pin_hash']):
                locked_until = _auth_failure(con, row)
                if locked_until:
                    return jsonify(ok=False, error='Muitas tentativas. Proteção temporariamente bloqueada por 5 minutos.'), 429
                return jsonify(ok=False, error='PIN incorreto. Não foi possível desativar a proteção.'), 401
            _auth_reset_client(con)
            con.execute('UPDATE app_auth SET enabled=0,failed_attempts=0,locked_until=0,updated_at=? WHERE id=1',
                        (datetime.now(TZ).isoformat(),))
            con.commit()
        else:
            con.execute("INSERT OR REPLACE INTO app_auth(id,pin_salt,pin_hash,failed_attempts,locked_until,updated_at,enabled) VALUES(1,'','',0,0,?,0)",
                        (datetime.now(TZ).isoformat(),))
            con.commit()
    finally:
        con.close()
    if not session.get('authenticated'):
        session.clear()
    return jsonify(ok=True)


@app.route('/api/auth/login', methods=['POST'])
def auth_login():
    payload = request.get_json(silent=True) or {}
    pin = str(payload.get('pin') or '')
    con = sqlite3.connect(TOKEN_DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    try:
        con.execute('BEGIN IMMEDIATE')
        row = con.execute('SELECT * FROM app_auth WHERE id=1').fetchone()
        if not row:
            return jsonify(ok=False, setup_required=True, error='Configure seu PIN de acesso.'), 409
        now = time.time()
        lock_response = _auth_lock_response(row, now, con)
        if lock_response:
            return lock_response
        candidate = pin_digest(pin, row['pin_salt']) if re.fullmatch(r'\d{6,12}', pin) else ''
        if not hmac.compare_digest(candidate, row['pin_hash']):
            lock_until = _auth_failure(con, row, now)
            if lock_until:
                return jsonify(ok=False, error='Muitas tentativas. Proteção temporariamente bloqueada por 5 minutos.'), 429
            return jsonify(ok=False, error='PIN incorreto.'), 401
        _auth_reset_client(con)
        con.execute('UPDATE app_auth SET failed_attempts=0,locked_until=0 WHERE id=1')
        con.commit()
    finally:
        con.close()
    session.clear(); session['authenticated'] = True; session['boot_id'] = BOOT_ID; session['last_activity'] = time.time(); session.permanent = True
    return jsonify(ok=True)


@app.route('/api/auth/logout', methods=['POST'])
def auth_logout():
    session.clear()
    return jsonify(ok=True)


@app.route('/api/auth/change-pin', methods=['POST'])
def auth_change_pin():
    payload = request.get_json(silent=True) or {}
    current, new_pin = str(payload.get('current') or ''), str(payload.get('new') or '')
    con = sqlite3.connect(TOKEN_DB_PATH, timeout=15); con.row_factory = sqlite3.Row
    try:
        con.execute('BEGIN IMMEDIATE')
        row = con.execute('SELECT * FROM app_auth WHERE id=1').fetchone()
        if not row:
            return jsonify(ok=False, error='PIN não configurado.'), 409
        lock_response = _auth_lock_response(row, con=con)
        if lock_response:
            return lock_response
        candidate = pin_digest(current, row['pin_salt']) if re.fullmatch(r'\d{6,12}', current) else ''
        if not hmac.compare_digest(candidate, row['pin_hash']):
            locked_until = _auth_failure(con, row)
            if locked_until:
                return jsonify(ok=False, error='Muitas tentativas. Proteção temporariamente bloqueada por 5 minutos.'), 429
            return jsonify(ok=False, error='PIN atual incorreto.'), 401
        if not re.fullmatch(r'\d{6,12}', new_pin):
            return jsonify(ok=False, error='O novo PIN deve ter de 6 a 12 números.'), 400
        _auth_reset_client(con)
        salt = secrets.token_bytes(16).hex()
        con.execute('UPDATE app_auth SET pin_salt=?,pin_hash=?,failed_attempts=0,locked_until=0,updated_at=? WHERE id=1',
                    (salt, pin_digest(new_pin, salt), datetime.now(TZ).isoformat()))
        con.commit()
    finally:
        con.close()
    return jsonify(ok=True)

API_PROVIDERS = {
    'pluggy': {'name':'Pluggy', 'category':'Open Finance · contas e investimentos', 'description':'Integração principal para conectar bancos e corretoras via Open Finance. Contas, posições e transações dependem dos produtos e dados disponibilizados por cada instituição.', 'auth':True, 'credential_type':'client_id_secret', 'url':'https://dashboard.pluggy.ai/', 'docs':'https://docs.pluggy.ai/'},
    'brapi': {'name':'brapi', 'category':'Brasil · cotações e histórico', 'description':'Ações, FIIs, BDRs, ETFs, índices cobertos, dividendos, fundos, Tesouro Direto e séries macroeconômicas. A cobertura e os limites dependem do plano.', 'auth':True, 'url':'https://brapi.dev/dashboard', 'docs':'https://brapi.dev/docs'},
    'hgbrasil': {'name':'HG Brasil', 'category':'Brasil · mercado e indicadores', 'description':'Cotações B3, índices, moedas, juros e dados históricos. Recursos históricos e cotações por ativo dependem do plano contratado.', 'auth':True, 'url':'https://console.hgbrasil.com/', 'docs':'https://console.hgbrasil.com/documentation/finance'},
    'fred': {'name':'FRED', 'category':'Macroeconomia internacional', 'description':'Séries macroeconômicas e indicadores econômicos internacionais. Não substitui dados de preços de ativos da B3.', 'auth':True, 'url':'https://fred.stlouisfed.org/docs/api/api_key.html', 'docs':'https://fred.stlouisfed.org/docs/api/fred/'},
    'bcb': {'name':'Banco Central · SGS', 'category':'Brasil · fonte oficial', 'description':'Séries oficiais como CDI, Selic, IPCA e outros indicadores. API pública sem token.', 'auth':False, 'url':'https://www.bcb.gov.br/estabilidadefinanceira/seriestemporais', 'docs':'https://www.bcb.gov.br/gestao_site/webservices.asp'},
    'ibge': {'name':'IBGE · SIDRA', 'category':'Brasil · estatísticas e inflação', 'description':'Séries estatísticas oficiais, indicadores de preços e atividade econômica. API pública sem token; não fornece cotação de ativos.', 'auth':False, 'url':'https://servicodados.ibge.gov.br/api/docs/agregados?versao=3', 'docs':'https://apisidra.ibge.gov.br/'},
    'tesouro': {'name':'Tesouro Transparente', 'category':'Brasil · títulos públicos', 'description':'Dados abertos de taxas e preços diários de títulos do Tesouro Direto, além de relatórios e séries da dívida pública. Sem token.', 'auth':False, 'url':'https://www.tesourotransparente.gov.br/temas/divida-publica-federal/tesouro-direto', 'docs':'https://www.tesourotransparente.gov.br/ckan/dataset'},
    'cvm': {'name':'CVM Dados Abertos', 'category':'Brasil · fundos e demonstrações', 'description':'Dados públicos de fundos, informes e demonstrações financeiras. Não é uma API de cotação em tempo real; acesso aos conjuntos públicos não exige token.', 'auth':False, 'url':'https://dados.cvm.gov.br/', 'docs':'https://dados.cvm.gov.br/'},
    'b3': {'name':'B3 · APIs oficiais', 'category':'Brasil · dados oficiais de mercado e investidor', 'description':'Posições, movimentações, eventos provisionados e negociação. Exige contratação/licença e autorização do investidor; a B3 informa que as APIs do catálogo são voltadas a clientes B2B.', 'auth':False, 'url':'https://www.b3.com.br/pt_br/market-data-e-indices/servicos-de-dados/b3-for-developers/', 'docs':'https://developers.b3.com.br/apis'},
    'yahoo': {'name':'Yahoo Finance', 'category':'Global · fonte complementar', 'description':'Histórico e cotações usados como fonte complementar por endpoints não oficiais. Não há API pública oficial com garantia de disponibilidade ou licença para uso comercial.', 'auth':False, 'url':'https://finance.yahoo.com/', 'docs':'https://finance.yahoo.com/'},
    'dadosb3': {'name':'Dados B3 · Índices históricos', 'category':'Brasil · índices de retorno total', 'description':'Séries diárias de IBOV, IFIX, SMLL e IDIV, com retorno total. Sem chave, a API libera apenas os últimos 30 pregões; a chave grátis libera a série completa (200 consultas/dia). Serviço independente que organiza dados públicos da B3, não é a API oficial da B3.', 'auth':True, 'url':'https://dadosb3.com/chave-gratis?de=api', 'docs':'https://dadosb3.com/api'},
}


def _api_fernet():
    key_path = API_KEY_FILE
    os.makedirs(os.path.dirname(key_path) or '.', exist_ok=True)
    if not os.path.exists(key_path):
        key = Fernet.generate_key()
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(key)
    with open(key_path, 'rb') as handle:
        return Fernet(handle.read().strip())


def _get_api_token(provider):
    try:
        row = token_db().execute('SELECT token_encrypted,enabled FROM api_credentials WHERE provider=?', (provider,)).fetchone()
        if row and row['enabled']:
            return _api_fernet().decrypt(row['token_encrypted'].encode()).decode()
    except (InvalidToken, OSError, ValueError):
        return ''
    if provider == 'brapi':
        return TOKEN
    return ''


def _brapi_token():
    # Um token cadastrado e desativado deve prevalecer sobre o fallback do ambiente.
    try:
        row = token_db().execute('SELECT enabled FROM api_credentials WHERE provider=?', ('brapi',)).fetchone()
        if row is not None and not row['enabled']:
            return ''
    except Exception:
        pass
    return _get_api_token('brapi') or TOKEN


def _get_pluggy_credentials():
    raw = _get_api_token('pluggy')
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return {'client_id':str(value.get('client_id') or ''),'client_secret':str(value.get('client_secret') or '')}
    except (ValueError, TypeError):
        return {}



# Pluggy personal-use integration (Meu Pluggy).
# The personal/free flow does not expose GET /v2/items. Keep itemIds locally:
# the user copies each itemId from the Pluggy Dashboard Demo page.
PLUGGY_AUTH_CACHE = {'api_key': '', 'expires_at': 0}
PLUGGY_AUTH_LOCK = threading.Lock()
PLUGGY_REQUEST_LOCK = threading.RLock()
try:
    PLUGGY_REQUEST_GAP_SECONDS = min(5.0, max(0.0, float(os.environ.get('PLUGGY_REQUEST_GAP_SECONDS', '1.0'))))
except (TypeError, ValueError):
    PLUGGY_REQUEST_GAP_SECONDS = 1.0


def _pluggy_api_key(force=False):
    with PLUGGY_AUTH_LOCK:
        now = time.time()
        if not force and PLUGGY_AUTH_CACHE.get('api_key') and PLUGGY_AUTH_CACHE.get('expires_at', 0) > now + 60:
            return PLUGGY_AUTH_CACHE['api_key']
        credentials = _get_pluggy_credentials()
        if not credentials.get('client_id') or not credentials.get('client_secret'):
            raise ValueError('Cadastre Client ID e Client Secret da Pluggy.')
        _provider_wait_for_gap('pluggy')
        request_id = _api_record_request('pluggy', 'AUTH', 0)
        if not request_id:
            raise RuntimeError('Não foi possível registrar a chamada Pluggy com segurança.')
        try:
            response = requests.post(
                'https://api.pluggy.ai/auth',
                json={'clientId': credentials['client_id'], 'clientSecret': credentials['client_secret']},
                timeout=25
            )
        except requests.RequestException:
            _api_update_request_status(request_id, 0)
            _provider_set_cooldown('pluggy', 30, 0, 'network_error')
            raise
        _api_update_request_status(request_id, response.status_code)
        if response.status_code == 429:
            try:
                pause = min(3600, max(60, int(float(response.headers.get('Retry-After') or '60'))))
            except (TypeError, ValueError):
                pause = 60
            _provider_set_cooldown('pluggy', pause, 429, 'rate_limited')
        elif response.status_code >= 500:
            _provider_set_cooldown('pluggy', 30, response.status_code, 'upstream_5xx')
        response.raise_for_status()
        api_key = str((response.json() or {}).get('apiKey') or '')
        if not api_key:
            raise ValueError('A Pluggy não retornou uma API Key.')
        PLUGGY_AUTH_CACHE.update(api_key=api_key, expires_at=now + 6900)
        return api_key


def _pluggy_request(method, path, params=None, force_auth=False):
    url = 'https://api.pluggy.ai' + path
    key = _pluggy_api_key(force=force_auth)

    def perform(current_key):
        with PLUGGY_REQUEST_LOCK:
            _provider_wait_for_gap('pluggy')
            endpoint_group = str(path).strip('/').split('/', 1)[0] or 'root'
            request_id = _api_record_request('pluggy', f'{str(method).upper()}:{endpoint_group}', 0)
            if not request_id:
                raise RuntimeError('Não foi possível registrar a chamada Pluggy com segurança.')
            try:
                result = requests.request(method, url, headers={'X-API-KEY': current_key, 'Accept': 'application/json'}, params=params, timeout=35)
            except requests.RequestException:
                _api_update_request_status(request_id, 0)
                _provider_set_cooldown('pluggy', 30, 0, 'network_error')
                raise
            _api_update_request_status(request_id, result.status_code)
            if result.status_code == 429:
                try:
                    pause = min(3600, max(60, int(float(result.headers.get('Retry-After') or '60'))))
                except (TypeError, ValueError):
                    pause = 60
                _provider_set_cooldown('pluggy', pause, 429, 'rate_limited')
            elif result.status_code >= 500:
                _provider_set_cooldown('pluggy', 30, result.status_code, 'upstream_5xx')
            return result

    response = perform(key)
    if response.status_code == 401 and not force_auth:
        key = _pluggy_api_key(force=True)
        response = perform(key)
    response.raise_for_status()
    return response.json() if response.content else {}


def _pluggy_results(path, params=None):
    params = dict(params or {})
    params.setdefault('pageSize', 500)
    page = 1
    output = []
    while page <= 100:
        current = dict(params)
        current['page'] = page
        data = _pluggy_request('GET', path, params=current)
        rows = data.get('results') or []
        output.extend(rows)
        total_pages = int(data.get('totalPages') or 1)
        if page >= total_pages or not rows:
            break
        page += 1
    return output


def _pluggy_date(value):
    raw = str(value or '').strip()
    if not raw:
        return ''
    try:
        return datetime.fromisoformat(raw.replace('Z', '+00:00')).astimezone(TZ).date().isoformat()
    except Exception:
        try:
            return date.fromisoformat(raw[:10]).isoformat()
        except Exception:
            return ''


def _pluggy_asset_type(investment):
    """Normaliza os tipos/subtipos da Pluggy sem converter produtos em ações."""
    kind = str(investment.get('type') or '').strip().upper().replace('-', '_').replace(' ', '_')
    subtype = str(investment.get('subtype') or '').strip().upper().replace('-', '_').replace(' ', '_')
    name = str(investment.get('name') or '').upper()
    code = str(investment.get('code') or '').upper()
    fixed_markers = ('FIXED_INCOME', 'CDB', 'RDB', 'LCI', 'LCA', 'LC', 'CRI', 'CRA',
                     'DEBENTURE', 'DEBÊNTURE', 'TESOURO', 'TREASURY', 'LFT', 'LTN',
                     'NTN', 'LF', 'POUPANCA', 'POUPANÇA')
    if any(marker in kind or marker in subtype for marker in fixed_markers):
        return 'renda_fixa'
    if any(marker in name or marker in code for marker in ('CDB', 'RDB', 'LCI', 'LCA', 'TESOURO', 'DEBENTURE', 'DEBÊNTURE')):
        return 'renda_fixa'
    # A Pluggy pode retornar type=EQUITY para diferentes classes.
    # O subtype específico (ex.: REAL_ESTATE_FUND) deve prevalecer.
    if subtype in ('REAL_ESTATE_FUND', 'REIT', 'FII'):
        return 'fii'
    if subtype in ('ETF', 'EXCHANGE_TRADED_FUND'):
        return 'etf'
    if subtype in ('BDR', 'BRAZILIAN_DEPOSITARY_RECEIPT'):
        return 'bdr'
    if subtype in ('MUTUAL_FUND', 'FUND', 'INVESTMENT_FUND', 'FUND_QUOTA'):
        return 'fundo'
    if subtype in ('STOCK', 'SHARE', 'SHARES'):
        return 'acao'
    if kind in ('EQUITY', 'STOCK', 'SHARE', 'SHARES'):
        return 'acao'
    if kind in ('ETF', 'EXCHANGE_TRADED_FUND'):
        return 'etf'
    if kind in ('MUTUAL_FUND', 'FUND', 'INVESTMENT_FUND', 'FUND_QUOTA'):
        return 'fundo'
    if kind in ('REAL_ESTATE_FUND', 'REIT', 'FII'):
        return 'fii'
    if kind in ('BDR', 'BRAZILIAN_DEPOSITARY_RECEIPT'):
        return 'bdr'
    if kind == 'COE' or subtype == 'COE':
        return 'outros'
    if kind in ('SECURITY', 'BOND'):
        return 'renda_fixa' if any(x in subtype for x in ('BOND','TREASURY','FIXED')) else 'outros'
    return 'outros'


def _pluggy_ticker(investment):
    candidates = [investment.get('code'), investment.get('ticker'), investment.get('symbol'), investment.get('name')]
    for candidate in candidates:
        value = str(candidate or '').strip().upper()
        # Only map unambiguous exchange-style tickers into the stock ledger.
        if re.fullmatch(r'[A-Z]{4}[0-9]{1,2}', value):
            return value
    return ''


_ASSET_CLASS_CACHE = {}
_ASSET_CLASS_CACHE_TTL = 24 * 60 * 60


def _refine_pluggy_asset_type(investment):
    """Classifica pelo subtipo Pluggy e valida com o cadastro de ativos da brapi."""
    kind = _pluggy_asset_type(investment)
    ticker = _pluggy_ticker(investment)
    if not ticker:
        return kind
    now = time.time()
    cached = _ASSET_CLASS_CACHE.get(ticker)
    if cached and now - cached[0] < _ASSET_CLASS_CACHE_TTL:
        return cached[1]
    cache_key = 'pluggy_asset_class:' + ticker
    saved = db().execute('SELECT valor FROM integracao_estado WHERE chave=?', (cache_key,)).fetchone()
    if saved and saved['valor']:
        _ASSET_CLASS_CACHE[ticker] = (now, saved['valor'])
        return saved['valor']
    token = _brapi_token()
    headers = {'Authorization': f'Bearer {token}'} if token else {}
    resolved = kind
    try:
        response = _brapi_get('https://brapi.dev/api/quote/list',
            params={'search': ticker, 'limit': 50}, headers=headers, timeout=10, cache_ttl=7 * 86400, stale_ttl=30 * 86400)
        response.raise_for_status()
        stocks = response.json().get('stocks') or []
        match = next((row for row in stocks if str(row.get('stock') or row.get('symbol') or '').upper() == ticker), None)
        if match:
            subtype = str(match.get('subType') or '').strip().lower().replace('_', '-')
            market_type = str(match.get('type') or '').strip().lower()
            mapping = {'fii':'fii','etf':'etf','bdr':'bdr','stock':'acao','unit':'acao',
                       'fi-infra':'fundo','fi-agro':'fundo','fidc':'fundo','fip':'fundo'}
            if subtype in mapping: resolved = mapping[subtype]
            elif market_type == 'bdr': resolved = 'bdr'
            elif market_type == 'fund': resolved = 'fundo'
            elif market_type == 'stock': resolved = 'acao'
    except (requests.RequestException, ValueError, TypeError, AttributeError):
        pass
    _ASSET_CLASS_CACHE[ticker] = (now, resolved)
    db().execute('INSERT INTO integracao_estado(chave,valor) VALUES(?,?) ON CONFLICT(chave) DO UPDATE SET valor=excluded.valor',
                 (cache_key, resolved))
    return resolved


def _pluggy_expenses_total(movement):
    expenses = movement.get('expenses')
    if not isinstance(expenses, dict): return 0.0
    total = 0.0
    for value in expenses.values():
        try: total += abs(float(value or 0))
        except (TypeError, ValueError): pass
    return round(total, 2)


def _pluggy_average_purchase_price(ticker):
    """Calcula preço médio ponderado com compras/vendas efetivamente importadas."""
    rows = db().execute(
        "SELECT movement_type,quantity,unit_value,amount,raw_json FROM pluggy_investment_transactions "
        "WHERE UPPER(ticker)=? ORDER BY transaction_date,transaction_id", (str(ticker).upper(),)
    ).fetchall()
    quantity = cost = 0.0
    found_buy = False
    for row in rows:
        movement = str(row['movement_type'] or '').upper()
        qty = abs(float(row['quantity'] or 0))
        if qty <= 0: continue
        price = abs(float(row['unit_value'] or 0))
        if price <= 0: price = abs(float(row['amount'] or 0)) / qty
        if movement == 'BUY':
            try: raw = json.loads(row['raw_json'] or '{}')
            except (TypeError, ValueError): raw = {}
            # Preço médio ponderado: soma dos valores de compra dividida
            # pela quantidade de cotas, sem incorporar proventos ou taxas.
            quantity += qty
            cost += qty * price
            found_buy = True
        elif movement == 'SELL' and quantity > 0:
            sold = min(qty, quantity)
            average = cost / quantity
            quantity -= sold
            cost = max(0.0, cost - sold * average)
    return (cost / quantity) if found_buy and quantity > 1e-8 else None


def _pluggy_fixed_income_snapshot_rows(item_id, investments, snapshot_date, now_text):
    """Converte posições sincronizadas em lançamentos por classe.

    Renda fixa só entra quando a Pluggy fornece os dados contratuais mínimos.
    Posições incompletas são ignoradas para não criar um ativo sem indexador/taxa.
    """
    if not snapshot_date:
        return 0, 0, 0
    fixed_count = market_count = skipped_fixed = 0
    origin_tag = 'PluggySnapshot:' + item_id
    # Recria somente os lançamentos-snapshot desta conexão, sem tocar nos manuais.
    previous_snapshots = [dict(row) for row in db().execute(
        'SELECT * FROM pluggy_position_snapshots WHERE item_id=?', (item_id,)
    ).fetchall()]
    current_investment_ids = {str(row.get('id') or '') for row in investments}
    db().execute("DELETE FROM operacoes WHERE detalhes LIKE ?", ('%' + origin_tag + '%',))
    db().execute('DELETE FROM pluggy_position_snapshots WHERE item_id=?', (item_id,))
    for investment in investments:
        asset_type = _refine_pluggy_asset_type(investment)
        investment_id = str(investment.get('id') or '').strip()
        if not investment_id:
            continue
        balance = float(investment.get('balance') or investment.get('amount') or investment.get('value') or 0)
        principal = float(investment.get('amountOriginal') or investment.get('amountInvested') or balance)
        details = investment.get('details') if isinstance(investment.get('details'), dict) else {}
        if asset_type == 'renda_fixa':
            if balance <= 0 or principal <= 0:
                continue
            indexer = str(investment.get('indexer') or investment.get('indexerName') or details.get('indexer') or details.get('indexerName') or '').strip().upper()
            rate = investment.get('rate') or investment.get('percentage') or details.get('rate') or details.get('percentage')
            maturity = _pluggy_date(investment.get('maturityDate') or investment.get('dueDate') or details.get('maturityDate') or details.get('dueDate'))
            # Sem indexador e taxa não há cálculo confiável de evolução do título.
            if not indexer or rate in (None, ''):
                skipped_fixed += 1
                continue
            try:
                rate = float(str(rate).replace(',', '.'))
            except (TypeError, ValueError):
                skipped_fixed += 1
                continue
            symbol = 'RF' + re.sub(r'[^A-Z0-9]', '', investment_id.upper())[:18]
            quantity, price = 1.0, principal
            name = str(investment.get('name') or investment.get('subtype') or 'Renda fixa')
            application_date = _pluggy_date(
                investment.get('applicationDate') or investment.get('investmentDate') or
                investment.get('startDate') or investment.get('appliedAt') or
                details.get('applicationDate') or details.get('investmentDate') or
                details.get('startDate') or details.get('appliedAt')
            )
            issuer = str(investment.get('issuerName') or investment.get('issuer') or
                         details.get('issuerName') or details.get('issuer') or '').strip()
            contract = {'origem': 'Pluggy', 'pluggy_snapshot': origin_tag, 'investment_id': investment_id,
                        'nome': name, 'emissor': issuer, 'titulo': str(investment.get('subtype') or details.get('subtype') or ''),
                        'data_aplicacao': application_date, 'indexador': indexer, 'taxa': rate, 'vencimento': maturity,
                        'saldo_atual': balance, 'subtipo': investment.get('subtype') or ''}
            fixed_count += 1
        elif asset_type in ('acao', 'fii', 'etf', 'bdr', 'fundo', 'stocks', 'internacional', 'cripto', 'reit'):
            # Snapshot autoritativo inclusive com quantidade zero para posições encerradas.
            symbol = _pluggy_ticker(investment)
            quantity = max(0.0, float(investment.get('quantity') or 0))
            status = str(investment.get('status') or '').upper()
            if status in ('RETIRADA_TOTAL', 'TOTAL_WITHDRAWAL', 'CLOSED', 'INACTIVE'):
                quantity = 0.0
            if symbol:
                db().execute(
                    "INSERT OR REPLACE INTO pluggy_position_snapshots "
                    "(item_id,investment_id,ticker,asset_type,quantity,amount_original,balance,snapshot_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (item_id, investment_id, symbol, asset_type, quantity, principal, balance, now_text)
                )
                market_count += 1
            continue
        else:
            status = str(investment.get('status') or '').upper()
            symbol = _pluggy_ticker(investment)
            if status in ('RETIRADA_TOTAL', 'TOTAL_WITHDRAWAL', 'CLOSED', 'INACTIVE') and symbol:
                db().execute(
                    "INSERT OR REPLACE INTO pluggy_position_snapshots "
                    "(item_id,investment_id,ticker,asset_type,quantity,amount_original,balance,snapshot_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (item_id, investment_id, symbol, asset_type, 0.0, 0.0, 0.0, now_text)
                )
                market_count += 1
            continue
        db().execute(
            """INSERT INTO operacoes(ticker,tipo,lado,quantidade,preco,data,custos,status,
               data_registro,data_execucao,detalhes) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (symbol, asset_type, 'compra', quantity, price, snapshot_date, 0.0, 'executada',
             snapshot_date, snapshot_date + 'T10:00:00-03:00', json.dumps(contract, ensure_ascii=False))
        )
    # Se uma posição deixou de constar na lista atual da corretora, ela não pode
    # reaparecer por compras antigas: grava uma posição encerrada para aquele id.
    for old in previous_snapshots:
        if old['investment_id'] not in current_investment_ids:
            db().execute(
                "INSERT OR REPLACE INTO pluggy_position_snapshots "
                "(item_id,investment_id,ticker,asset_type,quantity,amount_original,balance,snapshot_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (item_id, old['investment_id'], old['ticker'], old['asset_type'], 0.0, 0.0, 0.0, now_text)
            )
    return fixed_count, market_count, skipped_fixed


def _reconcile_existing_pluggy_b3_duplicates():
    """Remove apenas operações criadas pela Pluggy que duplicam uma linha B3 inequívoca."""
    pluggy_rows = db().execute(
        "SELECT * FROM operacoes WHERE detalhes LIKE '%Pluggy%' ORDER BY id"
    ).fetchall()
    removed = 0
    for pluggy_op in pluggy_rows:
        details = str(pluggy_op['detalhes'] or '')
        if 'Importação B3' in details or 'Importacao B3' in details:
            continue
        ticker = str(pluggy_op['ticker'] or '').upper()
        side = str(pluggy_op['lado'] or '').lower()
        trade_date = str(pluggy_op['data'] or '')[:10]
        qty = float(pluggy_op['quantidade'] or 0)
        amount = qty * float(pluggy_op['preco'] or 0)
        if not ticker or not trade_date or qty <= 0 or amount <= 0:
            continue
        candidates = db().execute(
            """SELECT * FROM operacoes
               WHERE id<>? AND UPPER(ticker)=? AND lower(lado)=?
                 AND detalhes LIKE '%Importação B3%'
                 AND date(data) BETWEEN date(?,'-3 day') AND date(?,'+3 day')
                 AND ABS(quantidade-?)<=?
                 AND ABS((quantidade*preco)-?)<=MAX(1.00,?*0.01)""",
            (pluggy_op['id'], ticker, side, trade_date, trade_date, qty,
             max(0.00001, qty * 0.001), amount, amount)
        ).fetchall()
        if len(candidates) != 1:
            continue
        b3_op = candidates[0]
        db().execute(
            """UPDATE pluggy_investment_transactions
               SET import_state='matched_existing', imported_operation_id=?, updated_at=?
               WHERE imported_operation_id=?""",
            (int(b3_op['id']), datetime.now(TZ).isoformat(timespec='seconds'), int(pluggy_op['id']))
        )
        db().execute('DELETE FROM operacoes WHERE id=?', (int(pluggy_op['id']),))
        removed += 1
    if removed:
        db().commit()
    return removed


def _pluggy_import_investment_transaction(item_id, investment, movement, now_text):
    external_id = str(movement.get('id') or '').strip()
    if not external_id:
        return 'sem_id'
    ticker = _pluggy_ticker(investment)
    trade_date = _pluggy_date(movement.get('tradeDate') or movement.get('date'))
    movement_type = str(movement.get('type') or '').upper()
    qty = abs(float(movement.get('quantity') or 0))
    unit_value = abs(float(movement.get('value') or 0))
    amount = abs(float(movement.get('amount') or movement.get('netAmount') or (qty * unit_value)))
    description = str(movement.get('description') or movement_type)
    state = 'imported_only'
    imported_operation_id = None

    if movement_type in ('BUY', 'SELL') and ticker and trade_date and qty > 0:
        side = 'compra' if movement_type == 'BUY' else 'venda'
        price = unit_value if unit_value > 0 else (amount / qty if qty else 0)
        asset_type = _refine_pluggy_asset_type(investment)
        costs = _pluggy_expenses_total(movement)
        # Match manual/B3 entries before inserting. A small tolerance handles
        # rounding differences between the broker and the spreadsheet export.
        # A B3 e a Pluggy podem registrar a mesma execução com pequena diferença
        # de data (negociação/liquidação) e arredondamento do preço unitário.
        # Exigimos ticker, lado, quantidade e valor financeiro próximos para evitar
        # associar operações diferentes do mesmo ativo.
        amount_for_match = amount if amount > 0 else qty * price
        existing = db().execute(
            """SELECT id FROM operacoes
               WHERE UPPER(ticker)=? AND lower(lado)=?
                 AND date(data) BETWEEN date(?,'-3 day') AND date(?,'+3 day')
                 AND ABS(quantidade-?)<=?
                 AND (
                   ABS(preco-?)<=0.03 OR
                   ABS((quantidade*preco)-?)<=MAX(1.00,?*0.01)
                 )
               ORDER BY ABS(julianday(data)-julianday(?)), id LIMIT 1""",
            (ticker, side, trade_date, trade_date, qty, max(0.00001, qty * 0.001),
             price, amount_for_match, amount_for_match, trade_date)
        ).fetchone()
        if existing:
            imported_operation_id = int(existing['id'])
            state = 'matched_existing'
        else:
            # Importa o histórico completo, inclusive anterior ao saldo-base.
            # A posição atual permanece autoritativa no snapshot da Pluggy.
            cursor = db().execute(
                """INSERT INTO operacoes
                   (ticker,tipo,lado,quantidade,preco,data,custos,status,data_registro,data_execucao,detalhes)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (ticker, asset_type, side, qty, price, trade_date, costs, 'executada',
                 trade_date, trade_date + 'T10:00:00-03:00',
                 json.dumps({'origem':'Pluggy','pluggy_transaction_id':external_id,'item_id':item_id,
                             'descricao':description,'valor_bruto':amount,
                             'valor_liquido':float(movement.get('netAmount') or amount),
                             'corretagem_despesas':costs,'numero_nota':movement.get('brokerageNumber')}, ensure_ascii=False))
            )
            imported_operation_id = int(cursor.lastrowid)
            state = 'imported_operation'

    # Income is only imported as a provento when the investment transaction
    # explicitly identifies a dividend/JCP/rendimento event.
    income_markers = ('DIVIDEND', 'INTEREST', 'INTEREST_ON_EQUITY', 'JCP', 'RENDIMENTO', 'DISTRIBUTION', 'INCOME')
    text_for_income = (movement_type + ' ' + str(movement.get('movementType') or '') + ' ' + description).upper()
    # Pluggy documents INTEREST as the investment-transaction type for
    # dividend/JCP/FII rent/coupon distributions. Do not infer income from
    # TAX, TRANSFER or AMORTIZATION descriptions, which can contain similar words.
    is_income_movement = movement_type in ('INTEREST', 'DIVIDEND', 'INTEREST_ON_EQUITY', 'JCP', 'DISTRIBUTION', 'INCOME')
    is_income_description = movement_type in ('', 'UNKNOWN', 'OTHER') and any(
        marker in text_for_income for marker in income_markers
    )
    if (is_income_movement or is_income_description) and ticker and trade_date and amount > 0:
        already_imported = db().execute(
            'SELECT external_id FROM pluggy_imported_events WHERE external_id=?', (external_id,)
        ).fetchone()
        duplicate = db().execute(
            """SELECT id FROM proventos WHERE UPPER(ticker)=?
               AND date(data) BETWEEN date(?,'-3 day') AND date(?,'+3 day')
               AND ABS(valor_total-?)<=MAX(0.05,?*0.01) LIMIT 1""",
            (ticker, trade_date, trade_date, amount, amount)
        ).fetchone()
        api_duplicate = db().execute(
            """SELECT event_key FROM proventos_api WHERE UPPER(ticker)=?
               AND date(data_pagamento) BETWEEN date(?,'-3 day') AND date(?,'+3 day')
               AND ABS(valor_total-?)<=MAX(0.05,?*0.01) LIMIT 1""",
            (ticker, trade_date, trade_date, amount, amount)
        ).fetchone()
        if already_imported or duplicate or api_duplicate:
            state = 'provento_duplicado_ignorado'
        else:
            cursor = db().execute(
                'INSERT INTO proventos(ticker,valor_total,data) VALUES(?,?,?)',
                (ticker, amount, trade_date)
            )
            db().execute(
                """INSERT OR IGNORE INTO pluggy_imported_events
                   (external_id,event_type,ticker,event_date,amount,target_id,import_state,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (external_id, 'provento', ticker, trade_date, amount, int(cursor.lastrowid), 'imported', now_text)
            )
            state = 'imported_provento'

    db().execute(
        """INSERT INTO pluggy_investment_transactions
           (transaction_id,item_id,investment_id,ticker,transaction_date,description,movement_type,
            quantity,unit_value,amount,import_state,imported_operation_id,raw_json,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(transaction_id) DO UPDATE SET
             item_id=excluded.item_id, investment_id=excluded.investment_id, ticker=excluded.ticker,
             transaction_date=excluded.transaction_date, description=excluded.description,
             movement_type=excluded.movement_type, quantity=excluded.quantity, unit_value=excluded.unit_value,
             amount=excluded.amount, import_state=CASE
               WHEN pluggy_investment_transactions.import_state IN ('imported_operation','imported_provento','matched_existing','provento_duplicado_ignorado')
               THEN pluggy_investment_transactions.import_state ELSE excluded.import_state END,
             imported_operation_id=COALESCE(pluggy_investment_transactions.imported_operation_id,excluded.imported_operation_id),
             raw_json=excluded.raw_json, updated_at=excluded.updated_at""",
        (external_id, item_id, str(investment.get('id') or ''), ticker, trade_date, description,
         movement_type, qty, unit_value, amount, state, imported_operation_id,
         json.dumps(movement, ensure_ascii=False), now_text)
    )
    return state



def _pluggy_sync_interval():
    row = db().execute("SELECT value FROM app_settings WHERE key='pluggy_sync_interval'").fetchone()
    try:
        value = int(row['value']) if row else 240
    except (TypeError, ValueError):
        value = 30
    return value if value in (0, 15, 30, 60, 240, 720, 1440) else 240


@app.route('/api/pluggy/settings', methods=['GET', 'POST'])
def pluggy_settings():
    if request.method == 'GET':
        response = jsonify(interval_minutes=_pluggy_sync_interval())
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        return response
    payload = request.get_json(silent=True) or {}
    try:
        
        if 'interval_minutes' not in payload:
            return json_error('Informe explicitamente interval_minutes.')
        interval = int(payload.get('interval_minutes'))
    except (TypeError, ValueError):
        return json_error('Intervalo inválido.')
    if interval not in (0, 15, 30, 60, 240, 720, 1440):
        return json_error('Escolha um intervalo disponível.')
    connection = db()
    try:
        connection.execute('BEGIN IMMEDIATE')
        connection.execute(
            "INSERT INTO app_settings(key,value) VALUES('pluggy_sync_interval',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(interval),)
        )
        connection.commit()
        saved = connection.execute(
            "SELECT value FROM app_settings WHERE key='pluggy_sync_interval'"
        ).fetchone()
        saved_interval = int(saved['value']) if saved else None
    except (sqlite3.Error, TypeError, ValueError) as exc:
        try: connection.rollback()
        except sqlite3.Error: pass
        app.logger.exception('Falha ao salvar a frequência de sincronização Pluggy: %s', exc)
        return json_error('Não foi possível gravar a frequência de sincronização.')
    if saved_interval != interval:
        return json_error('A frequência não foi confirmada no banco de dados.')
    response = jsonify(ok=True, interval_minutes=saved_interval)
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    return response


@app.route('/api/pluggy/state', methods=['GET'])
def pluggy_state():
    items = db().execute(
        """SELECT i.*,
           (SELECT COUNT(*) FROM pluggy_investments v WHERE v.item_id=i.item_id) AS investments_count,
           (SELECT COUNT(*) FROM pluggy_investment_transactions vtx WHERE vtx.item_id=i.item_id) AS investment_transactions_count
           FROM pluggy_items i WHERE i.active=1 ORDER BY i.created_at"""
    ).fetchall()
    investments = db().execute(
        """SELECT investment_id,item_id,code,name,investment_type,subtype,quantity,
                  amount_original,balance,unit_value,status,updated_at
           FROM pluggy_investments ORDER BY name LIMIT 250"""
    ).fetchall()
    return jsonify(items=[dict(row) for row in items], investments=[dict(row) for row in investments])


@app.route('/api/pluggy/items', methods=['POST'])
def pluggy_add_item():
    payload = request.get_json(silent=True) or {}
    item_id = str(payload.get('item_id') or '').strip()
    label = str(payload.get('label') or '').strip()[:100]
    if not re.fullmatch(r'[0-9a-fA-F-]{20,50}', item_id):
        return json_error('Informe um itemId válido copiado do Dashboard da Pluggy.')
    try:
        item = _pluggy_request('GET', '/items/' + item_id)
        connector = item.get('connector') or {}
        connector_name = str(connector.get('name') or item.get('connectorName') or 'MeuPluggy')
        status = str(item.get('status') or 'UNKNOWN')
        consent_expires = str(item.get('consentExpiresAt') or '')
        now_text = datetime.now(TZ).isoformat(timespec='seconds')
        db().execute(
            """INSERT INTO pluggy_items(item_id,label,connector_name,status,consent_expires_at,active,created_at,updated_at)
               VALUES(?,?,?,?,?,1,?,?)
               ON CONFLICT(item_id) DO UPDATE SET label=excluded.label,
               connector_name=excluded.connector_name,status=excluded.status,
               consent_expires_at=excluded.consent_expires_at,active=1,updated_at=excluded.updated_at""",
            (item_id, label or connector_name, connector_name, status, consent_expires, now_text, now_text)
        )
        db().commit()
        return jsonify(ok=True, item={'item_id':item_id,'label':label or connector_name,'connector_name':connector_name,'status':status})
    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else 502
        detail = ''
        try: detail = (exc.response.json() or {}).get('message') or (exc.response.json() or {}).get('code') or ''
        except Exception: pass
        return json_error('Não foi possível validar o itemId na Pluggy. HTTP %s %s' % (code, safe_error_text(detail, 100)), 400)
    except Exception as exc:
        return json_error('Falha ao validar conexão Pluggy: ' + safe_error_text(exc, 120), 400)


@app.route('/api/pluggy/items/<item_id>/clear', methods=['POST'])
def pluggy_clear_item_data(item_id):
    """Apaga os dados importados de uma conexão, preservando a conexão e credenciais."""
    con = db()
    item = con.execute('SELECT item_id,label FROM pluggy_items WHERE item_id=? AND active=1', (item_id,)).fetchone()
    if not item:
        return json_error('Conexão Pluggy não encontrada ou inativa.', 404)
    try:
        # IDs das movimentações pertencentes exclusivamente a esta conexão.
        external_ids = [r[0] for r in con.execute(
            'SELECT transaction_id FROM pluggy_investment_transactions WHERE item_id=?', (item_id,)
        ).fetchall()]
        event_ids = set(external_ids)
        event_rows = []
        if external_ids:
            for start in range(0, len(external_ids), 400):
                chunk = external_ids[start:start+400]
                marks = ','.join('?' for _ in chunk)
                event_rows.extend(con.execute(
                    f"SELECT external_id,event_type,target_id FROM pluggy_imported_events WHERE external_id IN ({marks})", chunk
                ).fetchall())
        provento_ids = [int(r['target_id']) for r in event_rows
                        if r['event_type'] == 'provento' and r['target_id'] is not None]

        # Remove operações efetivamente criadas pela conexão e snapshots dela.
        # Operações apenas conciliadas com lançamentos B3/manuais são preservadas.
        operation_ids = []
        rows = con.execute('SELECT id,detalhes FROM operacoes').fetchall()
        for row in rows:
            try:
                meta = json.loads(row['detalhes'] or '{}')
            except (TypeError, ValueError):
                meta = {}
            if meta.get('origem') == 'Pluggy' and meta.get('item_id') == item_id:
                operation_ids.append(int(row['id']))
            elif meta.get('pluggy_snapshot') == 'PluggySnapshot:' + item_id:
                operation_ids.append(int(row['id']))
        for start in range(0, len(operation_ids), 500):
            chunk = operation_ids[start:start+500]
            marks = ','.join('?' for _ in chunk)
            con.execute(f'DELETE FROM operacoes WHERE id IN ({marks})', chunk)

        deleted_proventos = 0
        for start in range(0, len(provento_ids), 500):
            chunk = provento_ids[start:start+500]
            marks = ','.join('?' for _ in chunk)
            deleted_proventos += con.execute(f'DELETE FROM proventos WHERE id IN ({marks})', chunk).rowcount
        for start in range(0, len(external_ids), 400):
            chunk = external_ids[start:start+400]
            marks = ','.join('?' for _ in chunk)
            con.execute(f'DELETE FROM pluggy_imported_events WHERE external_id IN ({marks})', chunk)

        deleted = {}
        for table in ('pluggy_position_snapshots','pluggy_investment_transactions','pluggy_investments',
                      'pluggy_transactions','pluggy_accounts'):
            deleted[table] = con.execute(f'DELETE FROM {table} WHERE item_id=?', (item_id,)).rowcount
        con.execute('UPDATE pluggy_items SET last_synced_at=?,updated_at=? WHERE item_id=?',
                    ('', datetime.now(TZ).isoformat(timespec='seconds'), item_id))
        con.commit()
        CACHE.clear(); PORTFOLIO_HISTORY_CACHE.clear()
        PROVENTOS_SYNC_CACHE.update(time=0, result=None, signature=None)
        return jsonify(ok=True, connection=item['label'], deleted={**deleted,
                       'operacoes':len(operation_ids),'proventos':deleted_proventos,
                       'eventos':len(event_rows)})
    except Exception:
        con.rollback()
        raise


@app.route('/api/pluggy/items/<item_id>', methods=['DELETE'])
def pluggy_remove_item(item_id):
    db().execute('UPDATE pluggy_items SET active=0,updated_at=? WHERE item_id=?',
                 (datetime.now(TZ).isoformat(timespec='seconds'), item_id))
    db().commit()
    return jsonify(ok=True)


PLUGGY_SYNC_LOCK = threading.Lock()


@app.route('/api/pluggy/sync', methods=['POST'])
def pluggy_sync():
    if not PLUGGY_SYNC_LOCK.acquire(blocking=False):
        return json_error('Já existe uma sincronização Pluggy em andamento.', 409)
    try:
        return _pluggy_sync_impl()
    finally:
        PLUGGY_SYNC_LOCK.release()


def _pluggy_sync_impl():
    payload = request.get_json(silent=True) or {}
    requested_item_id = str(payload.get('item_id') or '').strip()
    if requested_item_id:
        all_items = db().execute(
            'SELECT * FROM pluggy_items WHERE active=1 AND item_id=? ORDER BY created_at',
            (requested_item_id,)
        ).fetchall()
        if not all_items:
            return json_error('Conexão Pluggy não encontrada ou inativa.', 404)
    else:
        all_items = db().execute('SELECT * FROM pluggy_items WHERE active=1 ORDER BY created_at').fetchall()
    if not all_items:
        return json_error('Adicione ao menos um itemId do MeuPluggy antes de sincronizar.')
    force_sync = bool(payload.get('force'))
    sync_interval_minutes = _pluggy_sync_interval()
    now = datetime.now(TZ)
    items = []
    skipped_recent = 0
    for local_item in all_items:
        last_text = str(local_item['last_synced_at'] or '').strip()
        try:
            last_sync = datetime.fromisoformat(last_text) if last_text else None
            if last_sync and last_sync.tzinfo is None:
                last_sync = last_sync.replace(tzinfo=TZ)
        except (TypeError, ValueError):
            last_sync = None
        if not force_sync and last_sync and (now - last_sync).total_seconds() < sync_interval_minutes * 60:
            skipped_recent += 1
            continue
        items.append(local_item)
    if not items:
        return jsonify(ok=True, skipped_recent=skipped_recent, interval_minutes=sync_interval_minutes,
                       message='Sincronização recente. Nenhuma consulta à Pluggy foi necessária.')
    summary = {'items':0,'investments':0,
               'investment_transactions':0,'operations_imported':0,'operations_matched':0,
               'dividends_imported':0,'duplicates_ignored':0,'b3_duplicates_reconciled':0,
               'skipped_recent':skipped_recent,'interval_minutes':sync_interval_minutes,'warnings':[]}
    now_text = datetime.now(TZ).isoformat(timespec='seconds')
    sync_run_id = uuid.uuid4().hex
    try:
        for local_item in items:
            db().execute(
                "INSERT INTO pluggy_sync_runs(run_id,item_id,started_at,status) VALUES(?,?,?,'running')",
                (sync_run_id + ':' + str(local_item['item_id']), str(local_item['item_id']), now_text)
            )
        db().commit()
        _pluggy_api_key()
        summary['b3_duplicates_reconciled'] = _reconcile_existing_pluggy_b3_duplicates()
        for local_item in items:
            item_id = local_item['item_id']
            item = _pluggy_request('GET', '/items/' + item_id)
            investments = _pluggy_results('/investments', {'itemId':item_id})
            summary['items'] += 1
            connector = item.get('connector') or {}
            db().execute(
                'UPDATE pluggy_items SET connector_name=?,status=?,consent_expires_at=?,updated_at=? WHERE item_id=?',
                (str(connector.get('name') or local_item['connector_name'] or 'MeuPluggy'),
                 str(item.get('status') or ''), str(item.get('consentExpiresAt') or ''),
                 now_text, item_id)
            )
            for investment in investments:
                investment_id = str(investment.get('id') or '')
                if not investment_id: continue
                db().execute(
                    """INSERT INTO pluggy_investments(investment_id,item_id,code,name,investment_type,subtype,quantity,
                       amount_original,balance,unit_value,status,raw_json,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(investment_id) DO UPDATE SET item_id=excluded.item_id,code=excluded.code,name=excluded.name,
                       investment_type=excluded.investment_type,subtype=excluded.subtype,quantity=excluded.quantity,
                       amount_original=excluded.amount_original,balance=excluded.balance,unit_value=excluded.unit_value,
                       status=excluded.status,raw_json=excluded.raw_json,updated_at=excluded.updated_at""",
                    (investment_id,item_id,str(investment.get('code') or ''),str(investment.get('name') or ''),
                     str(investment.get('type') or ''),str(investment.get('subtype') or ''),
                     float(investment.get('quantity') or 0),float(investment.get('amountOriginal') or 0),
                     float(investment.get('balance') or 0),float(investment.get('value') or 0),
                     str(investment.get('status') or ''),json.dumps(investment,ensure_ascii=False),now_text)
                )
                summary['investments'] += 1
                if _refine_pluggy_asset_type(investment) == 'outros':
                    summary['warnings'].append(
                        'Tipo de investimento não reconhecido pela classificação automática: ' +
                        str(investment.get('name') or investment.get('type') or investment_id)[:90]
                    )
                inv_transactions = _pluggy_results('/investments/' + investment_id + '/transactions')
                for movement in inv_transactions:
                    summary['investment_transactions'] += 1
                    external_id = str(movement.get('id') or '').strip()
                    prior = db().execute(
                        'SELECT import_state,imported_operation_id,raw_json FROM pluggy_investment_transactions WHERE transaction_id=?',
                        (external_id,)
                    ).fetchone() if external_id else None
                    if prior and prior['import_state'] in ('imported_operation','imported_provento','matched_existing','provento_duplicado_ignorado'):
                        if prior['imported_operation_id']:
                            if prior['import_state'] == 'imported_operation':
                                try: old_movement = json.loads(prior['raw_json'] or '{}')
                                except (TypeError, ValueError): old_movement = {}
                                old_qty = abs(float(old_movement.get('quantity') or 0))
                                old_price = abs(float(old_movement.get('value') or 0))
                                if old_price <= 0 and old_qty > 0:
                                    old_price = abs(float(old_movement.get('amount') or 0)) / old_qty
                                db().execute('UPDATE operacoes SET tipo=?,preco=?,custos=? WHERE id=?',
                                    (_refine_pluggy_asset_type(investment), old_price,
                                     _pluggy_expenses_total(old_movement), int(prior['imported_operation_id'])))
                            else:
                                db().execute('UPDATE operacoes SET tipo=? WHERE id=?',
                                    (_refine_pluggy_asset_type(investment), int(prior['imported_operation_id'])))
                        summary['duplicates_ignored'] += 1
                        continue
                    state = _pluggy_import_investment_transaction(item_id, investment, movement, now_text)
                    if state == 'imported_operation': summary['operations_imported'] += 1
                    elif state == 'matched_existing': summary['operations_matched'] += 1
                    elif state == 'imported_provento': summary['dividends_imported'] += 1
                    elif state == 'provento_duplicado_ignorado': summary['duplicates_ignored'] += 1
                db().execute(
                    "UPDATE pluggy_sync_runs SET last_investment_id=? WHERE run_id=? AND status='running'",
                    (investment_id, sync_run_id + ':' + str(item_id))
                )
            pluggy_snapshot_date = snapshot_date_for() or datetime.now(TZ).date().isoformat()
            snapshot_fixed, snapshot_market, skipped_fixed = _pluggy_fixed_income_snapshot_rows(
                item_id, investments, pluggy_snapshot_date, now_text
            )
            summary['fixed_income_positions'] = summary.get('fixed_income_positions', 0) + snapshot_fixed
            summary['market_positions'] = summary.get('market_positions', 0) + snapshot_market
            summary['fixed_income_skipped_missing_data'] = summary.get('fixed_income_skipped_missing_data', 0) + skipped_fixed
            if skipped_fixed:
                summary['warnings'].append(f'{skipped_fixed} posição(ões) de renda fixa não importada(s): faltam indexador ou taxa contratual.')
            db().execute('UPDATE pluggy_items SET last_synced_at=?,updated_at=? WHERE item_id=?',
                         (now_text,now_text,item_id))
            db().execute(
                "UPDATE pluggy_sync_runs SET status='completed',finished_at=?,last_investment_id=?,error='' WHERE run_id=?",
                (datetime.now(TZ).isoformat(timespec='seconds'), investment_id if investments else '', sync_run_id + ':' + str(item_id))
            )
            db().commit()
    except requests.HTTPError as exc:
        db().rollback()
        try:
            db().execute("UPDATE pluggy_sync_runs SET status='failed',finished_at=?,error=? WHERE run_id LIKE ? AND status='running'",
                         (datetime.now(TZ).isoformat(timespec='seconds'), safe_error_text(exc, 180), sync_run_id + ':%'))
            db().commit()
        except sqlite3.Error:
            try: db().rollback()
            except sqlite3.Error: pass
        code = exc.response.status_code if exc.response is not None else 502
        return json_error('Falha ao sincronizar Pluggy (HTTP %s). Confira itemId, autorização MeuPluggy e credenciais.' % code, 400)
    except Exception as exc:
        db().rollback()
        try:
            db().execute("UPDATE pluggy_sync_runs SET status='failed',finished_at=?,error=? WHERE run_id LIKE ? AND status='running'",
                         (datetime.now(TZ).isoformat(timespec='seconds'), safe_error_text(exc, 180), sync_run_id + ':%'))
            db().commit()
        except sqlite3.Error:
            try: db().rollback()
            except sqlite3.Error: pass
        app.logger.exception('Falha durante sincronização Pluggy; os checkpoints concluídos foram preservados e a próxima execução poderá retomar pelos IDs externos já importados.')
        return json_error('Falha na sincronização Pluggy: ' + safe_error_text(exc, 140), 400)
    # Após atualizar as posições/movimentações, reconcilia proventos dos ativos atuais.
    try:
        current_items = (carteira().json or {}).get('itens', [])
        summary['proventos_sync'] = sync_brapi_proventos(force=False, quote_items=current_items, active_items=current_items)
    except Exception as dividend_exc:
        summary['warnings'].append('Não foi possível atualizar proventos após a sincronização: ' + safe_error_text(dividend_exc, 120))
    return jsonify(ok=True, summary=summary, synced_at=now_text)


PLUGGY_SCHEDULER_STOP = threading.Event()


def pluggy_server_scheduler_loop():
    """Run automatic Pluggy sync on the server; browser state is irrelevant."""
    # Avoid racing module initialization, DB migrations, and Gunicorn startup.
    if PLUGGY_SCHEDULER_STOP.wait(60):
        return
    while not PLUGGY_SCHEDULER_STOP.is_set():
        try:
            with app.app_context():
                active_count = db().execute('SELECT COUNT(*) FROM pluggy_items WHERE active=1').fetchone()[0]
                interval = _pluggy_sync_interval()
                retry_row = db().execute("SELECT value FROM app_settings WHERE key='pluggy_scheduler_retry_at'").fetchone()
                retry_at = None
                if retry_row and retry_row['value']:
                    try:
                        retry_at = datetime.fromisoformat(str(retry_row['value']))
                        if retry_at.tzinfo is None:
                            retry_at = retry_at.replace(tzinfo=TZ)
                    except (TypeError, ValueError):
                        retry_at = None
                retry_blocked = bool(retry_at and (datetime.now(TZ) - retry_at).total_seconds() < 300)
            if active_count and interval > 0 and not retry_blocked:
                with app.test_request_context('/api/pluggy/sync', method='POST', json={}):
                    result = pluggy_sync()
                    status = result[1] if isinstance(result, tuple) and len(result) > 1 else getattr(result, 'status_code', 200)
                with app.app_context():
                    if status >= 400:
                        db().execute("INSERT INTO app_settings(key,value) VALUES('pluggy_scheduler_retry_at',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (datetime.now(TZ).isoformat(timespec='seconds'),))
                        db().commit()
                        app.logger.warning('Agendador Pluggy não concluiu a execução (HTTP %s); nova tentativa em 5 minutos.', status)
                    else:
                        db().execute("DELETE FROM app_settings WHERE key='pluggy_scheduler_retry_at'")
                        db().commit()
        except Exception:
            app.logger.exception('Falha no agendador Pluggy')
        PLUGGY_SCHEDULER_STOP.wait(60)


# Gunicorn uses one worker in the supported compose file. The lock protects
# manual and scheduled requests within that process.
if not any(t.name == 'pluggy-server-scheduler' and t.is_alive() for t in threading.enumerate()):
    threading.Thread(target=pluggy_server_scheduler_loop, daemon=True, name='pluggy-server-scheduler').start()


@app.route('/api/integracoes', methods=['GET'])
def api_integrations_list():
    rows = {row['provider']: row for row in token_db().execute('SELECT provider,token_encrypted,enabled,updated_at FROM api_credentials').fetchall()}
    providers = []
    for key, meta in API_PROVIDERS.items():
        row = rows.get(key)
        token = _get_api_token(key) if meta['auth'] and key != 'pluggy' else ''
        pluggy_credentials = _get_pluggy_credentials() if key == 'pluggy' else {}
        configured = bool(row) if key == 'pluggy' else bool(row)
        if row and meta['auth'] and key != 'pluggy' and not token:
            try: token = _api_fernet().decrypt(row['token_encrypted'].encode()).decode()
            except Exception: token = ''
        if row and key == 'pluggy' and not pluggy_credentials:
            try:
                packed = _api_fernet().decrypt(row['token_encrypted'].encode()).decode()
                pluggy_credentials = json.loads(packed)
            except Exception: pluggy_credentials = {}
        providers.append({'id':key, **meta, 'connected':key in ('brapi','hgbrasil','bcb','dadosb3'), 'configured':configured, 'enabled':bool(row['enabled']) if row else bool(token or pluggy_credentials), 'masked':('••••••••' + token[-4:]) if token else '', 'client_id_masked':(('••••••••' + pluggy_credentials.get('client_id','')[-4:]) if pluggy_credentials.get('client_id') else ''), 'updated_at':row['updated_at'] if row else None})
    return jsonify({'providers':providers})


@app.route('/api/integracoes/<provider>', methods=['POST','DELETE'])
def api_integrations_save(provider):
    if provider not in API_PROVIDERS or not API_PROVIDERS[provider]['auth']:
        return jsonify({'error':'Este provedor não aceita token nesta tela.'}), 400
    if request.method == 'DELETE':
        token_db().execute('DELETE FROM api_credentials WHERE provider=?', (provider,)); token_db().commit()
        if provider == 'pluggy': PLUGGY_AUTH_CACHE.update(api_key='', expires_at=0)
        if provider in ('brapi','hgbrasil','dadosb3','fred'):
            CACHE.clear(); PORTFOLIO_HISTORY_CACHE.clear(); BENCHMARK_CACHE.clear(); PROVENTOS_SYNC_CACHE.update(time=0, result=None, signature=None)
        return jsonify({'ok':True})
    payload = request.get_json(silent=True) or {}
    if 'enabled' in payload and not any(k in payload for k in ('token','client_id','client_secret')):
        enabled = 1 if bool(payload.get('enabled')) else 0
        token_db().execute('UPDATE api_credentials SET enabled=?,updated_at=? WHERE provider=?',
                     (enabled, datetime.now(TZ).isoformat(timespec='seconds'), provider))
        token_db().commit()
        if provider == 'pluggy' and not enabled:
            PLUGGY_AUTH_CACHE.update(api_key='', expires_at=0)
        if provider in ('brapi','hgbrasil','dadosb3','fred'):
            CACHE.clear(); PORTFOLIO_HISTORY_CACHE.clear(); BENCHMARK_CACHE.clear(); PROVENTOS_SYNC_CACHE.update(time=0, result=None, signature=None)
        return jsonify({'ok':True,'enabled':bool(enabled),'configured':bool(token_db().execute('SELECT 1 FROM api_credentials WHERE provider=?',(provider,)).fetchone())})
    if provider == 'pluggy':
        client_id = str(payload.get('client_id') or '').strip()
        client_secret = str(payload.get('client_secret') or '').strip()
        if not client_id or not client_secret:
            return jsonify({'error':'Informe Client ID e Client Secret.'}), 400
        packed = json.dumps({'client_id':client_id,'client_secret':client_secret}, separators=(',',':'))
        encrypted = _api_fernet().encrypt(packed.encode()).decode()
        token_db().execute('INSERT INTO api_credentials(provider,token_encrypted,enabled,updated_at) VALUES(?,?,?,?) ON CONFLICT(provider) DO UPDATE SET token_encrypted=excluded.token_encrypted,enabled=excluded.enabled,updated_at=excluded.updated_at', ('pluggy',encrypted,1,datetime.now(TZ).isoformat(timespec='seconds')))
        token_db().commit()
        PLUGGY_AUTH_CACHE.update(api_key='', expires_at=0)
        return jsonify({'ok':True,'configured':True})
    token = str(payload.get('token') or '').strip()
    enabled = bool(payload.get('enabled', True))
    if token:
        encrypted = _api_fernet().encrypt(token.encode()).decode()
        token_db().execute('INSERT INTO api_credentials(provider,token_encrypted,enabled,updated_at) VALUES(?,?,?,?) ON CONFLICT(provider) DO UPDATE SET token_encrypted=excluded.token_encrypted,enabled=excluded.enabled,updated_at=excluded.updated_at', (provider,encrypted,1 if enabled else 0,datetime.now(TZ).isoformat(timespec='seconds')))
    else:
        token_db().execute('UPDATE api_credentials SET enabled=?,updated_at=? WHERE provider=?', (1 if enabled else 0,datetime.now(TZ).isoformat(timespec='seconds'),provider))
    token_db().commit()
    if provider in ('brapi','hgbrasil','dadosb3','fred'):
        CACHE.clear(); PORTFOLIO_HISTORY_CACHE.clear(); BENCHMARK_CACHE.clear(); PROVENTOS_SYNC_CACHE.update(time=0, result=None, signature=None)
    return jsonify({'ok':True,'configured':bool(_get_api_token(provider))})


@app.route('/api/integracoes/consumo', methods=['GET'])
def api_integrations_usage():
    """Retorna estatísticas locais sem consumir uma requisição de nenhum provedor."""
    _api_cache_cleanup()
    return jsonify({'brapi': _api_usage_snapshot()})


@app.route('/api/integracoes/<provider>/test', methods=['POST'])
def api_integrations_test(provider):
    if provider not in API_PROVIDERS:
        return jsonify({'ok':False,'message':'Provedor desconhecido.'}), 404
    if provider in ('bcb','ibge','tesouro','cvm','b3','yahoo'):
        return jsonify({'ok':False,'message':'Esta fonte não usa token cadastrado. A disponibilidade e os requisitos estão descritos no catálogo.'}), 400
    if provider == 'pluggy':
        credentials = _get_pluggy_credentials()
        if not credentials.get('client_id') or not credentials.get('client_secret'):
            return jsonify({'ok':False,'message':'Cadastre Client ID e Client Secret antes de testar.'}), 400
        try:
            api_key = _pluggy_api_key(force=False)
            if not api_key:
                raise ValueError('A Pluggy não retornou uma API Key.')
            return jsonify({'ok':True,'message':'Credenciais autenticadas. API Key válida; o fluxo pessoal MeuPluggy usa itemIds cadastrados manualmente.'})
        except (requests.RequestException, ValueError) as exc:
            return jsonify({'ok':False,'message':'Não foi possível autenticar na Pluggy. Confira as credenciais e tente novamente.'}), 400
    token = _get_api_token(provider)
    if not token:
        return jsonify({'ok':False,'message':'Cadastre um token antes de testar.'}), 400
    try:
        if provider == 'brapi':
            response = _brapi_get('https://brapi.dev/api/v2/user/usage', headers={'Authorization':f'Bearer {token}'}, timeout=15, cache_ttl=15 * 60, stale_ttl=6 * 3600)
        elif provider == 'hgbrasil':
            response = _provider_get('hgbrasil', 'https://api.hgbrasil.com/finance', params={'key':token}, timeout=15, cache_ttl=15 * 60, stale_ttl=15 * 60)
        elif provider == 'fred':
            response = _provider_get('fred', 'https://api.stlouisfed.org/fred/series', params={'series_id':'GDP','api_key':token,'file_type':'json'}, timeout=15, cache_ttl=15 * 60, stale_ttl=15 * 60)
        elif provider == 'dadosb3':
            response = _provider_get('dadosb3', 'https://dadosb3.com/indices/IFIX/precos', headers={'X-API-Key':token}, timeout=15, cache_ttl=15 * 60, stale_ttl=15 * 60)
        response.raise_for_status()
        data = response.json()
        if provider == 'hgbrasil' and data.get('valid_key') is False: raise ValueError('A chave foi recusada pela HG Brasil.')
        if provider == 'dadosb3' and (data.get('amostra') or not isinstance(data.get('serie'), list) or len(data.get('serie') or []) < 2):
            raise ValueError('A chave ainda não liberou o histórico completo. Verifique a chave e tente novamente.')
        return jsonify({'ok':True,'message':'Conexão autenticada. A cobertura de cada ticker e endpoint ainda depende do plano e do provedor.'})
    except (requests.RequestException, ValueError) as exc:
        message = 'Não foi possível validar a integração. Confira as credenciais, a disponibilidade do provedor e tente novamente.'
        return jsonify({'ok':False,'message':message}), 400


def _fundamental_rating(quote, asset_type=''):
    """Nota automática conservadora: pontua somente indicadores realmente recebidos da API."""
    if not isinstance(quote, dict) or asset_type in ('renda_fixa', 'tesouro', 'cripto', 'outros'):
        return {'score': None, 'coverage': 0, 'label': 'Dados fundamentalistas indisponíveis', 'details': []}
    def number(*keys):
        for key in keys:
            value = quote.get(key)
            try:
                if value is not None and value != '':
                    n = float(value)
                    if n == n and abs(n) != float('inf'):
                        return n
            except (TypeError, ValueError):
                continue
        return None
    metrics = []
    # Cada métrica contribui apenas quando o provedor devolve um valor válido.
    pe = number('priceEarnings', 'trailingPE', 'peRatio')
    if pe is not None and pe > 0:
        metrics.append(('Valuation P/L', 8 if pe <= 10 else 7 if pe <= 15 else 5 if pe <= 25 else 3, 'P/L %.2f' % pe))
    pb = number('priceToBook', 'priceToBookRatio', 'priceToBookValue')
    if pb is not None and pb > 0:
        metrics.append(('Valuation P/VP', 8 if pb <= 1.2 else 7 if pb <= 2 else 5 if pb <= 4 else 3, 'P/VP %.2f' % pb))
    roe = number('returnOnEquity', 'roe')
    if roe is not None:
        roe = roe * 100 if abs(roe) <= 1 else roe
        metrics.append(('Rentabilidade ROE', 9 if roe >= 20 else 8 if roe >= 15 else 6 if roe >= 10 else 4 if roe >= 0 else 1, 'ROE %.1f%%' % roe))
    margin = number('profitMargins', 'netProfitMargin', 'margemLiquida')
    if margin is not None:
        margin = margin * 100 if abs(margin) <= 1 else margin
        metrics.append(('Margem líquida', 9 if margin >= 20 else 8 if margin >= 12 else 6 if margin >= 6 else 4 if margin >= 0 else 1, 'Margem %.1f%%' % margin))
    op_margin = number('operatingMargins', 'operatingMargin')
    if op_margin is not None:
        op_margin = op_margin * 100 if abs(op_margin) <= 1 else op_margin
        metrics.append(('Eficiência operacional', 9 if op_margin >= 20 else 8 if op_margin >= 12 else 6 if op_margin >= 6 else 4 if op_margin >= 0 else 1, 'Margem operacional %.1f%%' % op_margin))
    debt = number('debtToEquity', 'debtToEquityRatio')
    if debt is not None:
        metrics.append(('Endividamento', 9 if debt <= 30 else 8 if debt <= 60 else 6 if debt <= 100 else 3 if debt <= 200 else 1, 'Dívida/PL %.1f' % debt))
    current = number('currentRatio', 'current_ratio')
    if current is not None and current > 0:
        metrics.append(('Liquidez corrente', 9 if current >= 2 else 8 if current >= 1.5 else 6 if current >= 1 else 3, 'Liquidez corrente %.2f' % current))
    quick = number('quickRatio', 'quick_ratio')
    if quick is not None and quick > 0:
        metrics.append(('Liquidez imediata', 9 if quick >= 1.5 else 8 if quick >= 1 else 6 if quick >= .7 else 3, 'Liquidez rápida %.2f' % quick))
    market_price = number('regularMarketPrice', 'close', 'price')
    high_52 = number('fiftyTwoWeekHigh', '52WeekHigh', 'yearHigh')
    if market_price and high_52 and high_52 > 0:
        drawdown = (market_price / high_52 - 1) * 100
        metrics.append(('Queda desde máxima de 52 semanas', 9 if drawdown >= -10 else 7 if drawdown >= -20 else 5 if drawdown >= -35 else 2, 'Distância da máxima de 52 semanas %.1f%%' % drawdown))
    revenue_growth = number('revenueGrowth', 'revenueQuarterlyGrowth')
    if revenue_growth is not None:
        revenue_growth = revenue_growth * 100 if abs(revenue_growth) <= 1 else revenue_growth
        metrics.append(('Crescimento de receita', 9 if revenue_growth >= 20 else 8 if revenue_growth >= 10 else 6 if revenue_growth >= 0 else 2, 'Crescimento de receita %.1f%%' % revenue_growth))
    operating_cash = number('operatingCashflow', 'operatingCashFlow')
    free_cash = number('freeCashflow', 'freeCashFlow')
    if operating_cash is not None and free_cash is not None:
        metrics.append(('Geração de caixa', 9 if operating_cash > 0 and free_cash > 0 else 5 if operating_cash > 0 else 1, 'Fluxo operacional e fluxo de caixa livre disponíveis'))
    earnings = number('earningsGrowth', 'earningsQuarterlyGrowth')
    if earnings is not None:
        earnings = earnings * 100 if abs(earnings) <= 1 else earnings
        metrics.append(('Geração de lucro', 9 if earnings >= 20 else 8 if earnings >= 10 else 6 if earnings >= 0 else 2, 'Crescimento de lucro %.1f%%' % earnings))
    dividend_yield = number('dividendYield', 'yield')
    payout = number('payoutRatio', 'payout')
    if payout is not None:
        payout = payout * 100 if abs(payout) <= 1 else payout
        metrics.append(('Sustentabilidade dos proventos', 9 if 0 <= payout <= 60 else 7 if payout <= 80 else 4 if payout <= 100 else 1, 'Payout %.1f%%' % payout))
    if dividend_yield is not None and dividend_yield > 0:
        dividend_yield = dividend_yield * 100 if dividend_yield <= 1 else dividend_yield
        metrics.append(('Histórico de proventos', 8 if dividend_yield <= 12 else 6 if dividend_yield <= 18 else 3, 'Dividend yield %.1f%%' % dividend_yield))
    coverage = len(metrics)
    if coverage < 3:
        return {'score': None, 'coverage': coverage, 'label': 'Análise incompleta · aguardando fundamentos', 'providers': quote.get('fundamentalProviders') or ([quote.get('quoteProvider')] if quote.get('quoteProvider') else []), 'details': [m[2] for m in metrics]}
    score = round(sum(m[1] for m in metrics) / coverage, 1)
    label = 'Excelente' if score >= 8.5 else 'Boa' if score >= 7 else 'Atenção' if score >= 5 else 'Risco elevado'
    return {'score': score, 'coverage': coverage, 'label': label, 'providers': quote.get('fundamentalProviders') or ([quote.get('quoteProvider')] if quote.get('quoteProvider') else []), 'details': [m[2] for m in metrics]}


def _enrich_quote_fundamentals(ticker, quote):
    """Preserva os fundamentos já retornados pelas fontes ainda ativas."""
    enriched = dict(quote or {})
    if enriched.get('priceEarnings') is not None or enriched.get('returnOnEquity') is not None:
        providers = list(enriched.get('fundamentalProviders') or [])
        if enriched.get('quoteProvider') == 'brapi' and 'brapi' not in providers:
            providers.append('brapi')
        enriched['fundamentalProviders'] = providers
    return enriched


def _first_positive_price(*values):
    """Return the first finite positive price, ignoring zero/invalid provider placeholders."""
    for value in values:
        try:
            parsed = float(value)
            if math.isfinite(parsed) and parsed > 0:
                return parsed
        except (TypeError, ValueError):
            continue
    return 0.0


def _quote_timestamp(value):
    """Normalize provider quote timestamps (epoch or ISO) to São Paulo ISO format."""
    if value in (None, ''):
        return None
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
            stamp = float(value)
            if stamp > 10_000_000_000:
                stamp /= 1000.0
            if stamp > 0:
                return datetime.fromtimestamp(stamp, TZ).isoformat(timespec='seconds')
    except (TypeError, ValueError, OSError, OverflowError):
        pass
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=TZ)
        return parsed.astimezone(TZ).isoformat(timespec='seconds')
    except (TypeError, ValueError, OverflowError):
        return None


def _quote_updated_at(quote):
    """Get the actual quote/close date supplied by a provider when possible."""
    if not isinstance(quote, dict):
        return None
    for key in ('quoteLastUpdated', 'regularMarketTime', 'updatedAt', 'updated_at', 'lastUpdated', 'last_updated', 'date'):
        normalized = _quote_timestamp(quote.get(key))
        if normalized:
            return normalized
    return None


def _quote_candle_date(value):
    """Retorna o dia de negociação do candle diário Yahoo sem deslocá-lo de fuso."""
    if value in (None, ''):
        return None
    try:
        stamp = float(value)
        if stamp > 10_000_000_000:
            stamp /= 1000.0
        if stamp > 0:
            return datetime.fromtimestamp(stamp, timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        pass
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except (TypeError, ValueError):
        return None


def _load_last_valid_quote(ticker):
    """Retorna a última cotação válida persistida, identificada como desatualizada."""
    try:
        with QUOTE_HISTORY_LOCK:
            with open(QUOTE_HISTORY_PATH, 'r', encoding='utf-8') as handle:
                records = json.load(handle)
        record = records.get(str(ticker or '').upper()) if isinstance(records, dict) else None
        if not isinstance(record, dict):
            return {}
        price = float(record.get('regularMarketPrice') or 0)
        if not math.isfinite(price) or price <= 0:
            return {}
        record = dict(record)
        record['regularMarketPrice'] = price
        record['quoteIsStale'] = True
        record['quoteLastUpdated'] = record.get('quoteLastUpdated')
        record['quoteProvider'] = record.get('quoteProvider') or 'Última cotação salva'
        return record
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}


def cotacao_cacheada(ticker):
    """Retorna cotação já disponível localmente, sem iniciar chamadas externas."""
    symbol = str(ticker or '').strip().upper()
    cached = CACHE.get(symbol)
    if cached:
        cached_at, payload = cached
        if isinstance(payload, dict) and payload:
            result = dict(payload)
            if time.time() - float(cached_at or 0) >= CACHE_TTL:
                result['quoteIsStale'] = True
            return result
    return _load_last_valid_quote(symbol)


def _save_last_valid_quote(ticker, quote):
    """Persiste somente preços positivos e finitos, sem sobrescrever cache válido por falha."""
    try:
        price = _first_positive_price(quote.get('regularMarketPrice'), quote.get('close'), quote.get('price'))
        if price <= 0:
            return
        record = {
            'regularMarketPrice': price,
            'quoteProvider': quote.get('quoteProvider') or 'Provedor de cotação',
            'quoteLastUpdated': _quote_updated_at(quote) or (None if quote.get('quoteIsStale') else datetime.now(TZ).isoformat(timespec='seconds')),
            'longName': quote.get('longName') or quote.get('shortName') or str(ticker).upper(),
        }
        with QUOTE_HISTORY_LOCK:
            os.makedirs(os.path.dirname(QUOTE_HISTORY_PATH) or '.', exist_ok=True)
            try:
                with open(QUOTE_HISTORY_PATH, 'r', encoding='utf-8') as handle:
                    records = json.load(handle)
                if not isinstance(records, dict):
                    records = {}
            except (OSError, ValueError, json.JSONDecodeError):
                records = {}
            records[str(ticker).upper()] = record
            temporary = QUOTE_HISTORY_PATH + '.tmp'
            with open(temporary, 'w', encoding='utf-8') as handle:
                json.dump(records, handle, ensure_ascii=False, separators=(',', ':'))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, QUOTE_HISTORY_PATH)
    except (OSError, ValueError, TypeError):
        # Falha no cache persistente não deve interromper a consulta da carteira.
        pass


def cotacao(ticker, force_refresh=False):
    now = time.time()
    ticker = str(ticker or '').strip().upper()
    if not force_refresh and ticker in CACHE and now - CACHE[ticker][0] < CACHE_TTL:
        return CACHE[ticker][1]

    data = {}
    brapi_token = _brapi_token()
    headers = {'Authorization': f'Bearer {brapi_token}'} if brapi_token else {}
    params = {'fundamental': 'true', 'dividends': 'true'}
    if brapi_token:
        params['token'] = brapi_token

    # Primeira tentativa: endpoint de cotação individual.
    try:
        response = _brapi_get(
            f'https://brapi.dev/api/quote/{ticker}',
            params=params, headers=headers, timeout=8
        )
        response.raise_for_status()
        results = response.json().get('results') or []
        if results and isinstance(results[0], dict):
            data = {**results[0], 'quoteProvider': 'brapi'}
            if response.headers.get('X-LonelyWallet-Cache') == 'stale':
                data['quoteIsStale'] = True
    except Exception:
        data = {}

    # Alguns tickers (principalmente FIIs/BDRs) podem não retornar preço
    # no endpoint individual. Tenta localizar a cotação pela lista da Brapi.
    has_price = _first_positive_price(data.get('regularMarketPrice'), data.get('close'), data.get('price')) > 0
    if not has_price and ticker:
        try:
            list_url = 'https://brapi.dev/api/quote/list'
            list_params = {'search': ticker, 'limit': 20, 'sortBy': 'name', 'sortOrder': 'asc'}
            response = _brapi_get(list_url, params=list_params, headers=headers, timeout=8)
            if response.status_code in (401, 403) and headers:
                # An expired optional token must not block the public latest-close list.
                response = _brapi_get(list_url, params=list_params,
                    headers={'User-Agent': USER_AGENT}, timeout=8, cache_ttl=6 * 3600, stale_ttl=7 * 86400)
            response.raise_for_status()
            stocks = response.json().get('stocks') or []
            def matches_symbol(item):
                candidate = str(item.get('stock') or item.get('symbol') or '').upper()
                return candidate == ticker or candidate.removesuffix('.SA') == ticker
            match = next((item for item in stocks if matches_symbol(item)), None)
            if match:
                data = {**data, **match}
                if response.headers.get('X-LonelyWallet-Cache') == 'stale':
                    data['quoteIsStale'] = True
                list_close = _first_positive_price(match.get('close'), match.get('price'))
                if list_close > 0 and _first_positive_price(match.get('regularMarketPrice')) <= 0:
                    # /quote/list returns the latest B3 close for listed FIIs/ETFs too.
                    data['regularMarketPrice'] = list_close
                    data['quoteProvider'] = 'brapi'
                    data['quoteIsStale'] = True
                    data['quoteLastUpdated'] = _quote_updated_at(match)
        except Exception:
            pass

    # Fontes alternativas configuradas: preço e variação vêm de uma única fonte.
    has_price = _first_positive_price(data.get('regularMarketPrice'), data.get('close'), data.get('price')) > 0
    if not has_price and ticker:
        fallback = _fallback_market_quote(ticker)
        if fallback:
            data = {**data, **fallback}
    elif has_price and data.get('quoteIsStale') and not _quote_updated_at(data) and ticker:
        # Alguns resultados de lista B3 devolvem um fechamento sem a data do candle.
        # Consulta o Yahoo para obter o fechamento e a data da sessão, sem misturar
        # preço/variação de fontes diferentes. Fundamentos Brapi são preservados.
        dated_fallback = _fallback_market_quote(ticker)
        if dated_fallback and dated_fallback.get('quoteLastUpdated'):
            quote_fields = {'regularMarketPrice', 'close', 'price', 'regularMarketChangePercent',
                            'changePercent', 'quoteLastUpdated', 'quoteIsStale', 'quoteProvider',
                            'regularMarketTime'}
            for key, value in data.items():
                if key not in quote_fields and key not in dated_fallback:
                    dated_fallback[key] = value
            data = dated_fallback

    has_valid_price = _first_positive_price(data.get('regularMarketPrice'), data.get('close'), data.get('price')) > 0
    if has_valid_price:
        data['quoteLastUpdated'] = _quote_updated_at(data) or data.get('quoteLastUpdated')
        if data.get('quoteLastUpdated'):
            try:
                updated = datetime.fromisoformat(str(data['quoteLastUpdated']))
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=TZ)
                # Quotes older than five calendar days are last-known closes, not current market quotes.
                age_days = (datetime.now(TZ) - updated.astimezone(TZ)).total_seconds() / 86400
                if age_days > 5:
                    data['quoteIsStale'] = True
            except (TypeError, ValueError, OverflowError):
                pass
        data['quoteIsStale'] = bool(data.get('quoteIsStale'))
        _save_last_valid_quote(ticker, data)
    else:
        # Mantém a última cotação realmente observada, mas sempre a identifica como antiga.
        data = _load_last_valid_quote(ticker)
    data = _enrich_quote_fundamentals(ticker, data)
    CACHE[ticker] = (now, data)
    return data


def _fallback_market_quote(ticker):
    """Busca cotação/variação em provedores alternativos configurados, sem combinar séries."""
    headers = {'User-Agent': USER_AGENT}
    # HG Brasil: cobertura de ativos B3 conforme o plano.
    token = _get_api_token('hgbrasil')
    if token:
        try:
            response = _provider_get('hgbrasil', 'https://api.hgbrasil.com/finance',
                params={'key': token, 'symbol': ticker}, headers=headers, timeout=10,
                cache_ttl=_brapi_market_cache_ttl(), stale_ttl=7 * 86400)
            response.raise_for_status()
            payload = response.json()
            results = payload.get('results') or {}
            stocks = results.get('stocks') or results
            item = stocks.get(ticker) or stocks.get(ticker.upper()) or {}
            price = item.get('price') or item.get('close')
            change = item.get('change_percent') or item.get('changePercent')
            if price and float(price) > 0:
                return {'regularMarketPrice': float(price),
                        'regularMarketChangePercent': float(change) if change is not None else None,
                        'longName': item.get('name') or ticker,
                        'quoteProvider': 'HG Brasil'}
        except (requests.RequestException, ValueError, TypeError, AttributeError):
            pass

    # Yahoo Finance usa sufixo .SA para a maioria dos ativos listados na B3.
    # Tentamos primeiro o ticker informado quando já tem sufixo; caso contrário,
    # o símbolo brasileiro é consultado com .SA para não retornar mercado errado.
    yahoo_symbols = [ticker] if '.' in ticker else [ticker + '.SA', ticker]
    for yahoo_symbol in yahoo_symbols:
        try:
            response = _provider_get('yahoo',
                f'https://query1.finance.yahoo.com/v8/finance/chart/{requests.utils.quote(yahoo_symbol, safe="")}',
                params={'range': '6mo', 'interval': '1d'},
                headers=headers, timeout=10, cache_ttl=_brapi_market_cache_ttl(), stale_ttl=7 * 86400)
            response.raise_for_status()
            result = next(iter(response.json().get('chart', {}).get('result') or []), None)
            if result:
                meta = result.get('meta') or {}
                returned_symbol = str(meta.get('symbol') or yahoo_symbol).upper()
                if returned_symbol != yahoo_symbol.upper():
                    continue
                price = _first_positive_price(meta.get('regularMarketPrice'))
                previous = _first_positive_price(meta.get('chartPreviousClose'), meta.get('previousClose'))
                quote_time = meta.get('regularMarketTime')
                candle_date = None
                stale_close = False
                candles = ((result.get('indicators') or {}).get('quote') or [{}])[0]
                closes = candles.get('close') or []
                timestamps = result.get('timestamp') or []
                last_index = next((idx for idx in range(len(closes) - 1, -1, -1)
                                   if _first_positive_price(closes[idx]) > 0), None)
                if price <= 0 and last_index is not None:
                    # Suspended or non-traded tickers often return zero in meta but retain historical candles.
                    price = _first_positive_price(closes[last_index])
                    quote_time = timestamps[last_index] if last_index < len(timestamps) else quote_time
                    candle_date = _quote_candle_date(quote_time)
                    stale_close = True
                elif quote_time is None and last_index is not None:
                    quote_time = timestamps[last_index] if last_index < len(timestamps) else None
                    candle_date = _quote_candle_date(quote_time)
                updated_at = candle_date or _quote_timestamp(quote_time)
                if updated_at:
                    try:
                        quote_dt = datetime.fromisoformat(updated_at)
                        stale_close = stale_close or (datetime.now(TZ) - quote_dt).total_seconds() > 5 * 86400
                    except (TypeError, ValueError, OverflowError):
                        pass
                if price > 0:
                    change = ((price / previous) - 1) * 100 if previous > 0 else None
                    return {'regularMarketPrice': price,
                            'regularMarketChangePercent': None if stale_close else change,
                            'longName': meta.get('longName') or meta.get('shortName') or ticker,
                            'quoteProvider': 'Yahoo Finance · último fechamento' if stale_close else 'Yahoo Finance',
                            'quoteIsStale': stale_close,
                            'quoteLastUpdated': updated_at}
        except (requests.RequestException, ValueError, TypeError, AttributeError):
            continue
    return {}


def _sgs_series(series_id, start_date, end_date):
    """Busca séries oficiais SGS com retry e fallback de cache recente."""
    start = date.fromisoformat(str(start_date)[:10])
    end = date.fromisoformat(str(end_date)[:10])
    if end < start:
        return []
    key = (int(series_id), start.isoformat(), end.isoformat())
    cached = SGS_CACHE.get(key)
    cached_age = (time.time() - cached[0]) if cached else None
    if cached and cached_age < SGS_CACHE_TTL:
        return cached[1]

    stale_points = cached[1] if cached and cached_age is not None and cached_age <= 7 * 86400 else None
    points = []
    cursor = start
    while cursor <= end:
        chunk_end = min(end, cursor + timedelta(days=8 * 365))
        params = {
            'formato': 'json',
            'dataInicial': cursor.strftime('%d/%m/%Y'),
            'dataFinal': chunk_end.strftime('%d/%m/%Y')
        }
        url = f'https://api.bcb.gov.br/dados/serie/bcdata.sgs.{int(series_id)}/dados'
        payload = None
        last_error = None
        for attempt, delay in enumerate((0.0, 0.35, 0.85)):
            if delay:
                time.sleep(delay)
            try:
                response = requests.get(url, params=params, timeout=18,
                                        headers={'Accept': 'application/json'})
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, list):
                    raise ValueError('BCB SGS retornou JSON em formato diferente de lista')
                last_error = None
                break
            except (requests.RequestException, ValueError, TypeError) as exc:
                last_error = exc
                payload = None
                # Retry a resposta vazia/não JSON e indisponibilidades transitórias.
                if attempt == 2:
                    break
        if last_error is not None or payload is None:
            if stale_points is not None:
                app.logger.warning(
                    'BCB SGS indisponível para série %s; utilizando cache local recente (%s)',
                    int(series_id), type(last_error).__name__ if last_error else 'resposta vazia'
                )
                return stale_points
            reason = 'resposta vazia ou não JSON' if isinstance(last_error, ValueError) else type(last_error).__name__ if last_error else 'resposta vazia'
            raise ValueError(f'BCB SGS indisponível para série {int(series_id)}: {reason}') from last_error

        for row in payload:
            try:
                d = datetime.strptime(row['data'], '%d/%m/%Y').date()
                value = float(str(row['valor']).replace(',', '.'))
                if math.isfinite(value):
                    points.append((d, value))
            except (KeyError, TypeError, ValueError):
                continue
        cursor = chunk_end + timedelta(days=1)
    points.sort(key=lambda item: item[0])
    SGS_CACHE[key] = (time.time(), points)
    return points


def _rate_number(value):
    """Extrai a primeira taxa numérica de campos como '120% do CDI' ou '6,5% a.a.'."""
    raw = str(value or '').strip().replace(',', '.')
    match = re.search(r'(-?\d+(?:\.\d+)?)', raw)
    return float(match.group(1)) if match else 0.0


def _fixed_income_factor(details, start_date, end_date):
    """Fator bruto de acumulação; não desconta IR/IOF nem taxas do emissor."""
    details = details if isinstance(details, dict) else {}
    indexer = str(details.get('indexador') or '').strip().upper()
    form = str(details.get('forma') or '').strip().lower()
    rate_text = str(details.get('taxa') or '').strip()
    rate = _rate_number(rate_text)
    if not indexer or not rate_text or rate <= 0:
        return None, 'Indexador ou taxa não informado'
    try:
        start = date.fromisoformat(str(start_date)[:10])
        end = date.fromisoformat(str(end_date)[:10])
    except (TypeError, ValueError):
        return None, 'Data da aplicação inválida'
    if end <= start:
        return 1.0, None
    hybrid = form == 'híbrido' or '+' in rate_text
    factor = 1.0
    try:
        if indexer == 'CDI':
            for _, daily_pct in _sgs_series(12, start + timedelta(days=1), end):
                daily = max(0.0, daily_pct) / 100.0
                if hybrid:
                    spread_daily = (1 + rate / 100.0) ** (1 / 252) - 1
                    factor *= (1 + daily) * (1 + spread_daily)
                else:
                    factor *= 1 + daily * (rate / 100.0)
        elif indexer in ('SELIC', 'SELIC OVER'):
            for _, daily_pct in _sgs_series(11, start + timedelta(days=1), end):
                daily = max(0.0, daily_pct) / 100.0
                factor *= (1 + daily * (rate / 100.0))
        elif indexer in ('IPCA', 'IGP-M'):
            series_id = 433 if indexer == 'IPCA' else 189
            # Índices de inflação são mensais: só entram competências completas
            # posteriores ao mês da aplicação, evitando aplicar mês cheio em data parcial.
            first_month = date(start.year + (start.month == 12), 1 if start.month == 12 else start.month + 1, 1)
            last_complete_month = date(end.year, end.month, 1) - timedelta(days=1)
            if first_month <= last_complete_month:
                for point_date, monthly_pct in _sgs_series(series_id, first_month, last_complete_month):
                    monthly = max(-99.0, monthly_pct) / 100.0
                    if hybrid:
                        spread_month = (1 + rate / 100.0) ** (1 / 12) - 1
                        factor *= (1 + monthly) * (1 + spread_month)
                    else:
                        factor *= 1 + monthly * (rate / 100.0)
        elif indexer in ('PREFIXADO', 'PRÉ-FIXADO', 'PRE-FIXADO'):
            # Taxa anual efetiva composta em dias úteis (base 252).
            business_days = 0
            current = start + timedelta(days=1)
            holidays = set()
            for year in range(start.year, end.year + 1):
                try:
                    holidays.update(b3_holidays(year))
                except Exception:
                    pass
            while current <= end:
                if current.weekday() < 5 and current not in holidays:
                    business_days += 1
                current += timedelta(days=1)
            factor = (1 + rate / 100.0) ** (business_days / 252)
        else:
            return None, f'Indexador {indexer} ainda não suportado'
    except Exception as exc:
        app.logger.warning('Falha ao calcular renda fixa (%s): %s', indexer, exc)
        return None, 'Não foi possível consultar o histórico do indexador'
    return max(0.01, factor), None


def _fixed_income_position_value(ticker, invested_cost, fallback_value, as_of=None):
    """Recalcula renda fixa por lote e não deixa snapshot de outro ticker ocultar operações."""
    cutoff = as_of or datetime.now(TZ).date().isoformat()
    base_date = snapshot_date_for(cutoff)
    imported = imported_balance(ticker, cutoff) if base_date else None

    def rows(where_sql='', params=()):
        query = "SELECT * FROM operacoes WHERE UPPER(ticker)=? AND tipo='renda_fixa' AND status='executada' "
        query += where_sql + ' ORDER BY data,id'
        return db().execute(query, (str(ticker).upper(), *params)).fetchall()

    def apply_rows(operations, lots):
        for op in operations:
            quantity = max(0.0, float(op['quantidade'] or 0))
            if quantity <= 1e-8:
                continue
            amount = quantity * max(0.0, float(op['preco'] or 0))
            if op['lado'] == 'compra':
                try:
                    details = json.loads(op['detalhes'] or '{}')
                    if not isinstance(details, dict):
                        details = {}
                except Exception:
                    details = {}
                application_date = _fixed_income_application_date(details, op['data'])
                is_pluggy_snapshot_without_start = bool(details.get('pluggy_snapshot')) and application_date is None
                try:
                    institutional_balance = max(0.0, float(details.get('saldo_atual') or 0)) if is_pluggy_snapshot_without_start else 0.0
                except (TypeError, ValueError):
                    institutional_balance = 0.0
                valuation_date = application_date.isoformat() if application_date else (None if is_pluggy_snapshot_without_start else op['data'])
                lots.append({'quantity': quantity, 'principal': amount + max(0.0, float(op['custos'] or 0)),
                             'date': valuation_date, 'details': details, 'institutional_balance': institutional_balance,
                             'legacy': False})
            else:
                remaining_quantity = quantity
                for lot in lots:
                    if remaining_quantity <= 1e-8:
                        break
                    lot_quantity = max(0.0, float(lot.get('quantity') or 0))
                    if lot_quantity <= 1e-8:
                        continue
                    consumed = min(lot_quantity, remaining_quantity)
                    fraction = max(0.0, (lot_quantity - consumed) / lot_quantity)
                    lot['principal'] = max(0.0, float(lot['principal']) * fraction)
                    if lot.get('institutional_balance', 0) > 0:
                        lot['institutional_balance'] = max(0.0, float(lot['institutional_balance']) * fraction)
                    lot['quantity'] = lot_quantity - consumed
                    remaining_quantity -= consumed

    lots = []
    if imported:
        imported_qty = max(0.0, float(imported['quantidade'] or 0))
        pre_ops = rows('AND data<=?', (base_date,))
        post_ops = rows('AND data>? AND data<=?', (base_date, cutoff))
        reconstructed_qty = sum(
            max(0.0, float(op['quantidade'] or 0)) * (1 if op['lado'] == 'compra' else -1)
            for op in pre_ops
        )
        tolerance = max(0.0001, imported_qty * 0.0001)
        if pre_ops and abs(reconstructed_qty - imported_qty) <= tolerance:
            apply_rows(pre_ops, lots)
        else:
            base_cost = imported_qty * max(0.0, float(imported['preco_medio'] or 0))
            if base_cost > 0:
                lots.append({'quantity': imported_qty, 'principal': base_cost, 'date': base_date,
                             'details': {}, 'legacy': True})
        apply_rows(post_ops, lots)
    else:
        apply_rows(rows('AND data<=?', (cutoff,)), lots)

    total_cost = sum(max(0.0, lot['principal']) for lot in lots)
    total_value = 0.0
    has_calculated = False
    warnings = []
    for lot in lots:
        principal = max(0.0, lot['principal'])
        if principal <= 0:
            continue
        if lot.get('institutional_balance', 0) > 0 and not lot.get('date'):
            total_value += float(lot['institutional_balance'])
            has_calculated = True
            warnings.append('Saldo atual informado pela instituição; data original da aplicação indisponível')
            continue
        factor, error = _fixed_income_factor(lot['details'], lot['date'], cutoff)
        if factor is None:
            total_value += principal
            warnings.append(error)
        else:
            total_value += principal * factor
            has_calculated = True
    if not lots:
        return invested_cost, fallback_value, False, 'Sem histórico de aplicação'
    if not has_calculated and warnings:
        return total_cost, fallback_value if fallback_value > 0 else total_cost, False, warnings[0]
    return total_cost, total_value, True, (warnings[0] if warnings else None)


def _rolling_monthly_arithmetic_average(monthly_returns, window=12):
    """Média aritmética dos retornos mensais de ganho de capital, sem proventos.

    Usa até os últimos 12 meses calculáveis; lacunas históricas não transformam
    meses sem dados em retorno zero nem impedem a média dos meses disponíveis.
    """
    rolling = []
    series = []
    for row in monthly_returns or []:
        try:
            value = float(row.get('retorno'))
            if not math.isfinite(value):
                continue
        except (TypeError, ValueError, AttributeError):
            continue
        rolling.append(value)
        if len(rolling) > max(1, int(window or 12)):
            rolling.pop(0)
        average = sum(rolling) / len(rolling)
        series.append({
            'mes': str(row.get('mes') or ''),
            'valor': round(average * 100, 4),
            'ganho_prejuizo_mes': round(float(row.get('resultado') or 0), 2),
            'meses_base': len(rolling)
        })
    last_count = len(rolling)
    average_percent = round((sum(rolling) / last_count) * 100, 2) if last_count else None
    return series, average_percent, last_count


_FIXED_INCOME_IOF_PERCENT = (
    96, 93, 90, 86, 83, 80, 76, 73, 70, 66,
    63, 60, 56, 53, 50, 46, 43, 40, 36, 33,
    30, 26, 23, 20, 16, 13, 10, 6, 3
)


def _fixed_income_iof_percent(days):
    """Percentual do rendimento retido como IOF nos primeiros 29 dias."""
    try:
        day_count = max(0, int(days))
    except (TypeError, ValueError):
        return None
    if day_count <= 0 or day_count >= 30:
        return 0.0
    return float(_FIXED_INCOME_IOF_PERCENT[day_count - 1])


def _fixed_income_ir_percent(days):
    """Alíquota regressiva de IR para pessoa física em renda fixa tributável."""
    try:
        day_count = max(0, int(days))
    except (TypeError, ValueError):
        return None
    if day_count <= 180:
        return 22.5
    if day_count <= 360:
        return 20.0
    if day_count <= 720:
        return 17.5
    return 15.0


def _fixed_income_tax_profile(details, ticker=''):
    """Identifica a tributação de pessoa física pelo tipo legal do título.

    O emissor é usado como identificação contratual, não para inferir sozinho o
    tipo fiscal. Quando o título é genérico/desconhecido, não presume tributação.
    """
    details = details if isinstance(details, dict) else {}
    explicit_title = ' '.join(str(details.get(key) or '') for key in
                              ('titulo', 'title', 'subtipo', 'subtype'))
    normalized_explicit = unicodedata.normalize('NFKD', explicit_title).encode('ascii', 'ignore').decode('ascii').upper()
    normalized_explicit = re.sub(r'[^A-Z0-9]+', ' ', normalized_explicit).strip()
    # O nome do produto/ticker serve apenas como fallback quando o campo de título
    # não foi preenchido. O nome do emissor/banco nunca determina a classificação.
    if normalized_explicit and re.search(r'\bOUTRO\b', normalized_explicit):
        return None
    raw = explicit_title if normalized_explicit else ' '.join(
        str(details.get(key) or '') for key in ('nome', 'name', 'ticker')
    )
    normalized = unicodedata.normalize('NFKD', raw).encode('ascii', 'ignore').decode('ascii').upper()
    normalized = re.sub(r'[^A-Z0-9]+', ' ', normalized)

    profile = None
    if re.search(r'\b(LCA|CRA|CRI|CDCA)\b', normalized):
        title = next(token for token in ('LCA', 'CRA', 'CRI', 'CDCA') if re.search(rf'\b{token}\b', normalized))
        profile = {'titulo': title, 'ir': False, 'iof': False}
    elif re.search(r'\b(LCI|LH)\b', normalized):
        title = next(token for token in ('LCI', 'LH') if re.search(rf'\b{token}\b', normalized))
        profile = {'titulo': title, 'ir': False, 'iof': True}
    elif 'DEBENTURE' in normalized:
        incentivada = any(token in normalized for token in ('INCENTIVADA', 'INFRAESTRUTURA', 'LEI 12431'))
        profile = {'titulo': 'Debênture', 'ir': not incentivada, 'iof': False}
    elif re.search(r'\bLF\b', normalized):
        profile = {'titulo': 'LF', 'ir': True, 'iof': False}
    elif re.search(r'\b(CDB|RDB|LC|CCB)\b', normalized):
        title = next((token for token in ('CDB', 'RDB', 'LC', 'CCB') if re.search(rf'\b{token}\b', normalized)), 'Renda fixa')
        profile = {'titulo': title, 'ir': True, 'iof': True}
    elif any(token in normalized for token in ('TESOURO', 'LFT', 'LTN', 'NTN')):
        profile = {'titulo': 'Tesouro', 'ir': True, 'iof': True}
    elif 'POUPANCA' in normalized:
        profile = {'titulo': 'Poupança', 'ir': False, 'iof': False}

    if not profile:
        return None

    explicit_ir_exempt = details.get('ir_isento', details.get('isento_ir'))
    explicit_iof_exempt = details.get('iof_isento', details.get('isento_iof'))
    if explicit_ir_exempt is True or str(explicit_ir_exempt or '').strip().lower() in ('sim', 'true', '1', 'isento'):
        profile['ir'] = False
    if explicit_iof_exempt is True or str(explicit_iof_exempt or '').strip().lower() in ('sim', 'true', '1', 'isento'):
        profile['iof'] = False
    return profile


def _fixed_income_application_date(details, operation_date=''):
    """Retorna a data real da aplicação; não confunde snapshot Pluggy com aplicação."""
    details = details if isinstance(details, dict) else {}
    sources = [details]
    for nested_key in ('contract', 'contrato', 'investment', 'aplicacao'):
        nested = details.get(nested_key)
        if isinstance(nested, dict):
            sources.append(nested)
    keys = (
        'data_aplicacao', 'application_date', 'applicationDate',
        'investment_date', 'investmentDate', 'start_date', 'startDate',
        'applied_at', 'appliedAt', 'data_inicio', 'dataInicio', 'openedAt'
    )
    for source in sources:
        for key in keys:
            value = source.get(key)
            if not value:
                continue
            try:
                if isinstance(value, date):
                    return value
                raw = str(value).strip()
                try:
                    return date.fromisoformat(raw[:10])
                except ValueError:
                    parsed = datetime.fromisoformat(raw.replace('Z', '+00:00'))
                    if parsed.tzinfo:
                        parsed = parsed.astimezone(TZ)
                    return parsed.date()
            except (TypeError, ValueError):
                continue
    # Uma linha sintética da Pluggy é criada na data do snapshot. Essa data não
    # representa necessariamente a contratação e não pode definir faixa de IR.
    if details.get('pluggy_snapshot'):
        return None
    try:
        return date.fromisoformat(str(operation_date or '')[:10])
    except (TypeError, ValueError):
        return None


def _fixed_income_tax_amounts_for_gain(gain, days, profile):
    """Calcula IOF primeiro e IR sobre o rendimento líquido de IOF."""
    gain = max(0.0, float(gain or 0))
    if not profile:
        return None
    iof = 0.0
    if profile.get('iof'):
        iof_percent = _fixed_income_iof_percent(days)
        if iof_percent is None:
            return None
        iof = min(gain, gain * iof_percent / 100.0)
    ir = 0.0
    if profile.get('ir'):
        ir_percent = _fixed_income_ir_percent(days)
        if ir_percent is None:
            return None
        ir = max(0.0, gain - iof) * ir_percent / 100.0
    return {'iof': round(iof, 2), 'ir': round(ir, 2)}


def _fixed_income_net_balance_estimate(ticker, gross_balance, as_of=None):
    """Estima saldo líquido de renda fixa após IR/IOF, por título e lote.

    O cálculo utiliza os dados de contratação e o indexador registrado. Se faltar
    data, título, taxa ou histórico que permita identificar a base tributável,
    retorna saldo None em vez de inventar alíquota. Não estima deságio/spread ou
    penalidade contratual de resgate antecipado.
    """
    try:
        cutoff = date.fromisoformat(str(as_of or datetime.now(TZ).date().isoformat())[:10])
        gross_balance = max(0.0, float(gross_balance or 0))
        if not math.isfinite(gross_balance):
            raise ValueError('Saldo inválido')
    except (TypeError, ValueError):
        return {'saldo': None, 'iof': None, 'ir': None, 'mensagem': 'Dados insuficientes para estimar os descontos.'}
    if gross_balance <= 0:
        return {'saldo': 0.0, 'iof': 0.0, 'ir': 0.0, 'mensagem': 'Sem saldo positivo.'}

    symbol = str(ticker or '').strip().upper()
    base_date = snapshot_date_for(cutoff.isoformat())
    imported = imported_balance(symbol, cutoff.isoformat()) if base_date else None
    lots = []

    def operation_rows(where_sql='', params=()):
        query = "SELECT * FROM operacoes WHERE UPPER(ticker)=? AND tipo='renda_fixa' AND status='executada' "
        query += where_sql + ' ORDER BY data,id'
        return [dict(row) for row in db().execute(query, (symbol, *params)).fetchall()]

    def apply_operations(rows, destination):
        for operation in rows:
            try:
                quantity = max(0.0, float(operation.get('quantidade') or 0))
                unit_price = max(0.0, float(operation.get('preco') or 0))
                costs = max(0.0, float(operation.get('custos') or 0))
            except (TypeError, ValueError):
                continue
            if quantity <= 0:
                continue
            if operation.get('lado') == 'compra':
                try:
                    details = json.loads(operation.get('detalhes') or '{}')
                    if not isinstance(details, dict):
                        details = {}
                except (TypeError, ValueError):
                    details = {}
                destination.append({
                    'quantity': quantity,
                    'principal': quantity * unit_price + costs,
                    'details': details,
                    'date': _fixed_income_application_date(details, operation.get('data'))
                })
            elif operation.get('lado') == 'venda':
                # Reduz a base de cada lote pela fração da quantidade resgatada,
                # não pelo valor bruto recebido na venda (que inclui rendimento).
                remaining_quantity = quantity
                for lot in destination:
                    if remaining_quantity <= 1e-8:
                        break
                    lot_quantity = max(0.0, float(lot.get('quantity') or 0))
                    if lot_quantity <= 1e-8:
                        continue
                    consumed = min(lot_quantity, remaining_quantity)
                    remaining_fraction = max(0.0, (lot_quantity - consumed) / lot_quantity)
                    lot['principal'] = max(0.0, float(lot.get('principal') or 0) * remaining_fraction)
                    lot['quantity'] = lot_quantity - consumed
                    remaining_quantity -= consumed

    if imported:
        imported_qty = max(0.0, float(imported.get('quantidade') or 0))
        pre_rows = operation_rows('AND data<=?', (base_date,))
        post_rows = operation_rows('AND data>? AND data<=?', (base_date, cutoff.isoformat()))
        reconstructed_qty = sum(
            (1.0 if row.get('lado') == 'compra' else -1.0) * max(0.0, float(row.get('quantidade') or 0))
            for row in pre_rows
        )
        tolerance = max(0.0001, imported_qty * 0.0001)
        if pre_rows and abs(reconstructed_qty - imported_qty) <= tolerance:
            apply_operations(pre_rows, lots)
        else:
            imported_principal = imported_qty * max(0.0, float(imported.get('preco_medio') or 0))
            if imported_principal > 0:
                legacy_details = {
                    'nome': imported.get('nome') or '',
                    'titulo': imported.get('titulo') or '',
                    'emissor': imported.get('emissor') or '',
                    'origem': imported.get('origem') or ''
                }
                lots.append({'quantity': imported_qty, 'principal': imported_principal,
                             'details': legacy_details, 'date': None})
        apply_operations(post_rows, lots)
    else:
        rows = operation_rows('AND data<=?', (cutoff.isoformat(),))
        apply_operations(rows, lots)

    lots = [lot for lot in lots if float(lot.get('quantity') or 0) > 1e-8 and float(lot.get('principal') or 0) > 0.01]
    if not lots:
        return {'saldo': None, 'iof': None, 'ir': None, 'mensagem': 'Histórico dos aportes insuficiente para estimar os descontos.'}

    profiles = [_fixed_income_tax_profile(lot.get('details') or {}, symbol) for lot in lots]
    if any(profile is None for profile in profiles):
        return {'saldo': None, 'iof': None, 'ir': None, 'mensagem': 'Tipo de título não identificado; confira o título e o emissor.'}
    profile_signatures = {(bool(profile.get('ir')), bool(profile.get('iof'))) for profile in profiles}
    if len(profile_signatures) > 1:
        return {'saldo': None, 'iof': None, 'ir': None, 'mensagem': 'A posição reúne títulos com tratamentos tributários diferentes; separe os investimentos.'}

    # Títulos integralmente isentos não exigem reconstruir o rendimento para o
    # cálculo tributário: o saldo bruto conhecido também é o saldo líquido.
    if all(not profile.get('ir') and not profile.get('iof') for profile in profiles):
        return {'saldo': round(gross_balance, 2), 'iof': 0.0, 'ir': 0.0,
                'mensagem': 'Título classificado como isento de IR e IOF para pessoa física.'}

    # Para produtos sujeitos apenas a IOF (ex.: LCI conforme contrato), após 30
    # dias não há desconto. O prazo precisa estar disponível para confirmar isso.
    lot_dates = []
    for lot in lots:
        start_date = lot.get('date')
        if not start_date or start_date > cutoff:
            return {'saldo': None, 'iof': None, 'ir': None,
                    'mensagem': 'Data original da aplicação não disponível; não é seguro estimar a tributação.'}
        lot_dates.append((start_date, (cutoff - start_date).days))

    active_iof = any(profile.get('iof') and days < 30 for profile, (_, days) in zip(profiles, lot_dates))
    active_ir = any(profile.get('ir') for profile in profiles)
    if not active_iof and not active_ir:
        return {'saldo': round(gross_balance, 2), 'iof': 0.0, 'ir': 0.0,
                'mensagem': 'Sem IR ou IOF estimado para os prazos atuais.'}

    total_iof = total_ir = estimated_gross = 0.0
    for lot, profile, (start_date, days) in zip(lots, profiles, lot_dates):
        principal = max(0.0, float(lot['principal']))
        if profile.get('ir') or (profile.get('iof') and days < 30):
            factor, error = _fixed_income_factor(lot.get('details') or {}, start_date.isoformat(), cutoff.isoformat())
            if factor is None:
                return {'saldo': None, 'iof': None, 'ir': None,
                        'mensagem': error or 'Indexador ou taxa indisponível para estimar os descontos.'}
            lot_gross = principal * factor
            gain = max(0.0, lot_gross - principal)
            taxes = _fixed_income_tax_amounts_for_gain(gain, days, profile)
            if taxes is None:
                return {'saldo': None, 'iof': None, 'ir': None, 'mensagem': 'Não foi possível determinar as alíquotas deste título.'}
            total_iof += taxes['iof']
            total_ir += taxes['ir']
            estimated_gross += lot_gross
        else:
            # Lote sem tributo aplicável no prazo atual: assume apenas sua
            # parcela do saldo bruto proporcional ao principal, sem inventar ganho.
            estimated_gross += principal

    tolerance = max(2.0, gross_balance * 0.015)
    if abs(estimated_gross - gross_balance) > tolerance:
        return {'saldo': None, 'iof': None, 'ir': None,
                'mensagem': 'O histórico dos aportes não reconcilia com o saldo atual; confira os lançamentos.'}
    net = max(0.0, gross_balance - total_iof - total_ir)
    return {
        'saldo': round(net, 2), 'iof': round(total_iof, 2), 'ir': round(total_ir, 2),
        'mensagem': 'Estimativa de resgate hoje. Não inclui deságio, spread ou penalidade contratual de resgate antecipado.'
    }


def snapshot_date_for(reference_date=None):
    # Última data-base global válida para a data consultada.
    cutoff = reference_date or datetime.now(TZ).date().isoformat()
    row = db().execute(
        'SELECT MAX(data_base) AS data_base FROM saldos_importados WHERE data_base<=?',
        (cutoff,)
    ).fetchone()
    return row['data_base'] if row and row['data_base'] else None


def imported_balance(ticker, reference_date=None):
    base_date = snapshot_date_for(reference_date)
    if not base_date:
        return None
    row = db().execute(
        'SELECT * FROM saldos_importados WHERE ticker=? AND data_base=?',
        (str(ticker).upper(), base_date)
    ).fetchone()
    return dict(row) if row else None


def asset_type_for_ticker(ticker, reference_date=None):
    key = str(ticker or '').strip().upper()
    row = db().execute('SELECT tipo FROM operacoes WHERE upper(ticker)=? ORDER BY data,id LIMIT 1', (key,)).fetchone()
    if row:
        return str(row['tipo'] or '').lower()
    balance = imported_balance(key, reference_date)
    if balance and balance.get('tipo'):
        return str(balance['tipo']).lower()
    row = db().execute('SELECT asset_type FROM pluggy_position_snapshots WHERE upper(ticker)=? ORDER BY snapshot_at DESC LIMIT 1', (key,)).fetchone()
    if row and row['asset_type']:
        return str(row['asset_type']).lower()
    row = db().execute('SELECT investment_type,subtype FROM pluggy_investments WHERE upper(code)=? OR upper(name)=? ORDER BY updated_at DESC LIMIT 1', (key, key)).fetchone()
    if row:
        raw = str(row['subtype'] or row['investment_type'] or '').lower()
        mapping = {'stock':'acao','equity':'acao','fii':'fii','reit':'reit','etf':'etf','fixed_income':'renda_fixa','bond':'renda_fixa','treasury':'tesouro','crypto':'cripto'}
        return mapping.get(raw, raw if raw in ('acao','fii','etf','bdr','internacional','renda_fixa','cripto','stocks','reit','tesouro','fundo','outros') else 'outros')
    return None


def posicao(ticker, as_of=None):
    """Calcula posição usando o último saldo-base e apenas operações posteriores.

    A fotografia é global e completa: ativos ausentes nela começam com saldo zero.
    Isso evita somar novamente todo o histórico anterior à importação.
    """
    cutoff = as_of or datetime.now(TZ).date().isoformat()
    base_date = snapshot_date_for(cutoff)
    balance = imported_balance(ticker, cutoff) if base_date else None
    if base_date:
        qtd = float(balance['quantidade']) if balance else 0.0
        custo = qtd * float(balance['preco_medio'] or 0) if balance else 0.0
        ops = db().execute(
            "SELECT * FROM operacoes WHERE ticker=? AND status='executada' AND data>? AND data<=? ORDER BY data,id",
            (str(ticker).upper(), base_date, cutoff)
        ).fetchall()
    else:
        qtd = custo = 0.0
        ops = db().execute(
            "SELECT * FROM operacoes WHERE ticker=? AND status='executada' AND data<=? ORDER BY data,id",
            (str(ticker).upper(), cutoff)
        ).fetchall()
    for op in ops:
        q = float(op['quantidade']); valor = q * float(op['preco'])
        # Preço médio = soma dos valores efetivos de compra / quantidade adquirida.
        # Taxas/custos operacionais não alteram o preço médio unitário solicitado.
        if op['lado'] == 'compra':
            qtd += q; custo += valor
        elif qtd > 0:
            pm = custo / qtd
            vendido = min(q, qtd)
            qtd -= vendido; custo = max(0.0, custo - vendido * pm)
    return max(qtd, 0), (custo / qtd if qtd > 0 else 0)


def json_error(message, status=400):
    return jsonify(ok=False, erro=message), status


@app.errorhandler(RequestEntityTooLarge)
def handle_request_too_large(_error):
    return json_error('Arquivo grande demais. O limite de envio é 16 MB.', 413)


def safe_error_text(error, limit=180):
    """Redact credentials, query strings and bearer values before returning errors."""
    text = str(error or 'Erro desconhecido')
    text = re.sub(r'(?i)(authorization\s*[:=]\s*bearer\s+)\S+', r'\1[REDACTED]', text)
    text = re.sub(r'(?i)(access[_-]?token|refresh[_-]?token|client[_-]?secret|clientsecret|api[_-]?key|apikey|token|secret|password|senha)(\s*[=:]\s*)[^&\s,;]+', r'\1\2[REDACTED]', text)
    text = re.sub(r"""https?://[^\s"'<>]+""", lambda m: _redact_url(m.group(0)), text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:max(20, int(limit))]


def _redact_url(value):
    from urllib.parse import urlsplit
    try:
        parsed = urlsplit(value.rstrip('.,);]'))
        host = parsed.hostname or 'host'
        port = f':{parsed.port}' if parsed.port else ''
        suffix = '…' if parsed.path and parsed.path != '/' else ''
        return f'{parsed.scheme}://{host}{port}/{suffix}'
    except Exception:
        return '[URL removida]'


def easter_sunday(year):
    a=year%19; b=year//100; c=year%100; d=b//4; e=b%4; f=(b+8)//25; g=(b-f+1)//3
    h=(19*a+b-d-g+15)%30; i=c//4; k=c%4; l=(32+2*e+2*i-h-k)%7; m=(a+11*h+22*l)//451
    month=(h+l-7*m+114)//31; day=((h+l-7*m+114)%31)+1
    return date(year,month,day)


def b3_holidays(year):
    easter=easter_sunday(year)
    dates={date(year,1,1),date(year,4,21),date(year,5,1),date(year,9,7),date(year,10,12),date(year,11,2),date(year,11,15),date(year,11,20),date(year,12,25),date(year,12,24),date(year,12,31)}
    dates.update({easter-timedelta(days=48),easter-timedelta(days=47),easter-timedelta(days=2),easter+timedelta(days=60)})
    return dates


def session_open(day):
    easter=easter_sunday(day.year)
    return clock_time(13,0) if day==easter-timedelta(days=46) else MARKET_OPEN


def next_b3_session(requested):
    candidate=requested.astimezone(TZ).replace(second=0,microsecond=0)
    while candidate.weekday()>=5 or candidate.date() in b3_holidays(candidate.year):
        candidate=(candidate+timedelta(days=1)).replace(hour=10,minute=0)
    opening=session_open(candidate.date())
    if candidate.time()<opening:
        candidate=candidate.replace(hour=opening.hour,minute=opening.minute)
    elif candidate.time()>=MARKET_CLOSE:
        candidate=(candidate+timedelta(days=1)).replace(hour=10,minute=0)
        while candidate.weekday()>=5 or candidate.date() in b3_holidays(candidate.year):
            candidate=(candidate+timedelta(days=1)).replace(hour=10,minute=0)
    return candidate


_DUE_OPERATIONS_LOCK = threading.Lock()

def _promote_due_operations(con, now=None):
    """Promote due scheduled operations without taking needless write locks."""
    now = now or datetime.now(TZ).isoformat(timespec='minutes')
    with _DUE_OPERATIONS_LOCK:
        due = con.execute(
            "SELECT 1 FROM operacoes WHERE status='agendada' AND data_execucao<=? LIMIT 1", (now,)
        ).fetchone()
        if not due:
            return 0
        cursor = con.execute(
            "UPDATE operacoes SET status='executada', data=substr(data_execucao,1,10) WHERE status='agendada' AND data_execucao<=?",
            (now,)
        )
        changed = max(0, int(cursor.rowcount or 0))
        con.commit()
        return changed


def process_due_operations():
    con = db()
    try:
        return _promote_due_operations(con)
    except sqlite3.OperationalError as exc:
        message = str(exc).lower()
        if 'locked' in message or 'busy' in message:
            # This is housekeeping; leave due rows pending for the next request/tick
            # instead of making the entire dashboard return HTTP 500.
            app.logger.warning('Operações agendadas aguardando liberação do SQLite: %s', exc)
            try: con.rollback()
            except sqlite3.Error: pass
            return 0
        raise


def scheduler_loop():
    # Promove operações agendadas automaticamente quando chega o horário previsto.
    while True:
        try:
            with app.app_context():
                _promote_due_operations(db())
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if 'locked' in message or 'busy' in message:
                app.logger.warning('Agendador de operações aguardando liberação do SQLite: %s', exc)
            else:
                app.logger.exception('Falha no agendador de operações')
        except Exception:
            app.logger.exception('Falha no agendador de operações')
        time.sleep(30)


threading.Thread(target=scheduler_loop,daemon=True,name='b3-operation-scheduler').start()


def _operation_effective_date(row):
    if str(row.get('status') or '').lower() == 'agendada':
        return str(row.get('data_execucao') or row.get('data') or '')[:10]
    return str(row.get('data') or '')[:10]


def _quantity_as_of_excluding(ticker, cutoff, exclude_ids=None):
    """Rebuild a raw quantity (without clamping) at cutoff, excluding edited/deleted rows."""
    symbol = str(ticker or '').upper()
    excluded = {int(value) for value in (exclude_ids or set())}
    base_date = snapshot_date_for(cutoff)
    balance = imported_balance(symbol, cutoff) if base_date else None
    quantity = float(balance['quantidade'] or 0) if balance else 0.0
    clauses = ['UPPER(ticker)=?', "status='executada'", 'data<=?']
    params = [symbol, cutoff]
    if base_date:
        clauses.append('data>?')
        params.append(base_date)
    if excluded:
        clauses.append('id NOT IN (' + ','.join('?' for _ in excluded) + ')')
        params.extend(sorted(excluded))
    rows = db().execute('SELECT id,lado,quantidade,data FROM operacoes WHERE ' + ' AND '.join(clauses) + ' ORDER BY data,id', params).fetchall()
    for row in rows:
        quantity += float(row['quantidade'] or 0) * (1 if row['lado'] == 'compra' else -1)

    # Pluggy's latest current position is a checkpoint for transactions after its
    # own snapshot time, when no newer imported B3/opening snapshot exists.
    pluggy = db().execute(
        "SELECT MAX(s.snapshot_at) AS snapshot_at, SUM(s.quantity) AS quantity "
        "FROM pluggy_position_snapshots s JOIN pluggy_items i ON i.item_id=s.item_id "
        "WHERE i.active=1 AND UPPER(s.ticker)=?", (symbol,)
    ).fetchone()
    pluggy_date = str(pluggy['snapshot_at'] or '')[:10] if pluggy else ''
    if pluggy_date and pluggy_date <= cutoff and (not base_date or pluggy_date > base_date):
        quantity = float(pluggy['quantity'] or 0)
        clauses = ['UPPER(ticker)=?', "status='executada'", 'data>?', 'data<=?']
        params = [symbol, pluggy_date, cutoff]
        if excluded:
            clauses.append('id NOT IN (' + ','.join('?' for _ in excluded) + ')')
            params.extend(sorted(excluded))
        rows = db().execute('SELECT id,lado,quantidade FROM operacoes WHERE ' + ' AND '.join(clauses) + ' ORDER BY data,id', params).fetchall()
        for row in rows:
            quantity += float(row['quantidade'] or 0) * (1 if row['lado'] == 'compra' else -1)
    return quantity


def sale_breaks_future_position(ticker, operation_date, quantity_delta=0.0, exclude_ids=None):
    """Return True when a proposed ledger change makes any later position negative."""
    symbol = str(ticker or '').upper()
    day = str(operation_date or '')[:10]
    try:
        date.fromisoformat(day)
        delta = float(quantity_delta)
        if not math.isfinite(delta):
            return True
    except (TypeError, ValueError):
        return True
    excluded = {int(value) for value in (exclude_ids or set())}
    quantity = _quantity_as_of_excluding(symbol, day, excluded) + delta
    if quantity < -1e-8:
        return True

    clauses = ['UPPER(ticker)=?', "((status='executada' AND data>?) OR (status='agendada' AND substr(COALESCE(data_execucao,data),1,10)>=?))"]
    params = [symbol, day, day]
    if excluded:
        clauses.append('id NOT IN (' + ','.join('?' for _ in excluded) + ')')
        params.extend(sorted(excluded))
    rows = db().execute(
        'SELECT id,lado,quantidade,status,data,data_execucao FROM operacoes WHERE ' + ' AND '.join(clauses) +
        " ORDER BY CASE WHEN status='agendada' THEN substr(COALESCE(data_execucao,data),1,10) ELSE data END, "
        'COALESCE(data_execucao,data),id', params
    ).fetchall()
    checkpoints = db().execute(
        'SELECT data_base,quantidade FROM saldos_importados WHERE UPPER(ticker)=? AND data_base>? ORDER BY data_base',
        (symbol, day)
    ).fetchall()
    checkpoints_by_date = {str(row['data_base']): float(row['quantidade'] or 0) for row in checkpoints}
    events_by_date = {}
    for row in rows:
        event_day = _operation_effective_date(dict(row))
        if not event_day or event_day < day:
            continue
        events_by_date.setdefault(event_day, []).append(row)
    for event_day in sorted(set(events_by_date) | set(checkpoints_by_date)):
        for row in events_by_date.get(event_day, []):
            quantity += float(row['quantidade'] or 0) * (1 if row['lado'] == 'compra' else -1)
            if quantity < -1e-8:
                return True
        if event_day in checkpoints_by_date:
            # An imported position snapshot is an authoritative end-of-day checkpoint.
            quantity = checkpoints_by_date[event_day]
    return False


def valid_operation(data):
    try:
        ticker=str(data.get('ticker','')).strip().upper(); tipo=str(data.get('tipo','acao')).lower(); lado=str(data.get('lado','compra')).lower()
        qtd=float(data.get('quantidade',0)); preco=float(data.get('preco',0)); custos=float(data.get('custos',0) or 0)
        dt=str(data.get('data','')); date.fromisoformat(dt)
        if not all(math.isfinite(v) for v in (qtd, preco, custos)):
            raise ValueError('Valores financeiros precisam ser finitos.')
        allowed_types=('acao','fii','etf','bdr','internacional','renda_fixa','cripto','stocks','reit','tesouro','fundo','outros')
        if not ticker or len(ticker)>80 or tipo not in allowed_types: raise ValueError()
        if lado not in ('compra','venda') or qtd<=0 or preco<0 or custos<0: raise ValueError()
        if lado=='venda':
            existing_type=asset_type_for_ticker(ticker, dt)
            if not existing_type or existing_type!=tipo:
                return None,'O ativo selecionado não pertence à categoria informada. Escolha o ativo dentro da categoria correta.'
        raw=str(data.get('data_hora') or (dt+'T10:00'))
        requested=datetime.fromisoformat(raw)
        if requested.tzinfo is None: requested=requested.replace(tzinfo=TZ)
        now=datetime.now(TZ); now_minute=now.replace(second=0,microsecond=0)
        # Datas anteriores são lançamentos históricos; não devem ser empurradas para o próximo pregão.
        if requested.date()<now.date():
            execution=requested; status='executada'
        # Renda fixa (CDB, LCI, LCA etc.) não depende do pregão da bolsa:
        # registra imediatamente, sem criar operação agendada.
        elif tipo=='renda_fixa':
            execution=now; status='executada'
        elif requested.date()==now.date() and now.weekday()<5 and now.date() not in b3_holidays(now.year) and MARKET_OPEN<=requested.time()<MARKET_CLOSE and requested<=now_minute:
            execution=requested; status='executada'
        else:
            execution=next_b3_session(requested)
            status='executada' if execution<=now_minute else 'agendada'
        effective_day = execution.date().isoformat()
        if lado == 'venda' and sale_breaks_future_position(ticker, effective_day, -qtd):
            return None,'A venda deixaria a posição negativa em uma data posterior, considerando compras, vendas agendadas e saldos importados.'
        details=data.get('detalhes') or {}
        if isinstance(details,str): details=json.loads(details)
        return (ticker,tipo,lado,qtd,preco,execution.date().isoformat(),custos,status,now.isoformat(timespec='minutes'),execution.isoformat(timespec='minutes'),json.dumps(details,ensure_ascii=False)),None
    except Exception:
        return None,'Confira ativo, tipo, quantidade, preço, custos e data da operação.'


@app.route('/api/health', methods=['GET'])
def health():
    try:
        db().execute('SELECT 1').fetchone()
        token_db().execute('SELECT 1').fetchone()
        charts = _chart_assets_status()
        if charts['status'] == 'unhealthy':
            return jsonify(
                ok=False,
                status='unhealthy',
                version=APP_VERSION,
                charts=charts,
            ), 503
        overall_status = 'degraded' if charts['status'] == 'degraded' else 'healthy'
        return jsonify(
            ok=True,
            status=overall_status,
            version=APP_VERSION,
            charts=charts,
        ), 200
    except Exception:
        app.logger.exception('Healthcheck failed')
        return jsonify(ok=False, status='unhealthy', version=APP_VERSION), 503


@app.route('/')
def index(): return render_template('index.html')


@app.route('/api/operacoes/exportar', methods=['GET'])
def exportar_operacoes_b3():
    con = db()
    wb = Workbook()
    ws = wb.active
    ws.title = 'Movimentações'
    ws.append(['Entrada/Saída','Data','Movimentação','Produto','Instituição','Código de Negociação','Quantidade','Preço unitário','Valor da Operação'])
    operations = con.execute("SELECT * FROM operacoes ORDER BY date(data),id").fetchall()
    for op in operations:
        side = str(op['lado'] or '').lower()
        product = f"{op['ticker']} - {op['tipo']}"
        qty = float(op['quantidade'] or 0)
        price = float(op['preco'] or 0)
        ws.append(['Débito' if side == 'venda' else 'Crédito', date.fromisoformat(str(op['data'])[:10]),
                   'Compra / Venda', product, 'Lonely Wallet', op['ticker'], qty, price, qty * price])
    dividends = con.execute("SELECT ticker,valor_total,data FROM proventos ORDER BY date(data)").fetchall()
    for item in dividends:
        ws.append(['Crédito', date.fromisoformat(str(item['data'])[:10]), 'Rendimento',
                   f"{item['ticker']} - provento", 'Lonely Wallet', item['ticker'], 0, 0,
                   float(item['valor_total'] or 0)])
    for col,width in {'A':16,'B':14,'C':22,'D':38,'E':22,'F':22,'G':16,'H':18,'I':20}.items():
        ws.column_dimensions[col].width=width
    ws.freeze_panes='A2'
    output=io.BytesIO()
    wb.save(output); output.seek(0)
    return send_file(output, as_attachment=True, download_name='Lonely-Wallet-Lancamentos-B3.xlsx',
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


def is_pluggy_operation(row):
    """Return True for Pluggy-imported operations, which are read-only."""
    try:
        details = row['detalhes'] if hasattr(row, 'keys') else row.get('detalhes')
        meta = json.loads(details or '{}') if isinstance(details, (str, bytes)) else (details or {})
        return isinstance(meta, dict) and str(meta.get('origem') or '').strip().lower() == 'pluggy'
    except Exception:
        return False


@app.route('/api/operacoes', methods=['GET','POST'])
def operacoes():
    if request.method == 'GET':
        process_due_operations()
        rows = db().execute('SELECT * FROM operacoes ORDER BY COALESCE(data_execucao,data) DESC,id DESC').fetchall()
        return jsonify(itens=[dict(r) for r in rows])
    process_due_operations()
    payload, error = valid_operation(request.get_json(silent=True) or {})
    if error: return json_error(error)
    db().execute('INSERT INTO operacoes(ticker,tipo,lado,quantidade,preco,data,custos,status,data_registro,data_execucao,detalhes) VALUES(?,?,?,?,?,?,?,?,?,?,?)', payload)
    db().commit(); CACHE.pop(payload[0], None)
    return jsonify(ok=True,status=payload[7],data_execucao=payload[9])


def clear_seed_snapshot_when_empty():
    """Remove a fotografia fixa quando o usuário exclui o último lançamento.

    Proventos pagos/recebidos permanecem como histórico financeiro independente;
    apenas as posições importadas que sustentavam o patrimônio atual são limpas.
    """
    remaining = int(db().execute('SELECT COUNT(*) FROM operacoes').fetchone()[0])
    if remaining == 0:
        db().execute(
            "DELETE FROM saldos_importados WHERE data_base=? AND origem=?",
            ('2026-10-31', 'Posição final informada pelo usuário (captura de 31/10/2026)')
        )
        db().execute(
            "INSERT INTO integracao_estado(chave,valor) VALUES(?,?) "
            "ON CONFLICT(chave) DO UPDATE SET valor=excluded.valor",
            ('snapshot_20261031_lifecycle_v45', 'reset_empty')
        )


def cleanup_b3_operation_refs(operation_ids):
    ids=[int(x) for x in operation_ids if x is not None]
    if not ids:
        return
    con=db()
    batch_ids=set()
    for start in range(0,len(ids),500):
        chunk=ids[start:start+500]
        marks=','.join('?' for _ in chunk)
        batch_ids.update(r[0] for r in con.execute(
            f"SELECT DISTINCT lote_id FROM b3_importados WHERE categoria='operacao' AND registro_id IN ({marks}) AND lote_id IS NOT NULL", chunk
        ).fetchall())
        con.execute(f"DELETE FROM b3_importados WHERE categoria='operacao' AND registro_id IN ({marks})", chunk)
    for batch_id in batch_ids:
        counts=con.execute("SELECT COUNT(*) FROM b3_importados WHERE lote_id=? AND categoria='operacao'",(batch_id,)).fetchone()[0]
        con.execute('UPDATE b3_import_lotes SET operacoes=? WHERE id=?',(int(counts),batch_id))



def cleanup_pluggy_asset_refs(scope, tipo='', ticker='', operation_ids=None, investment_ids=None):
    """Remove Pluggy investment cache/transactions linked to assets explicitly removed in the asset manager.
    Keeps Pluggy connections and credentials intact; only investment data is handled by the integration.
    """
    con = db()
    operation_ids = [int(x) for x in (operation_ids or []) if x is not None]
    investment_ids = list(dict.fromkeys(str(x) for x in (investment_ids or []) if x))
    if scope == 'todas':
        inv_ids = [r[0] for r in con.execute('SELECT investment_id FROM pluggy_investments').fetchall()]
        deleted_tx = con.execute('SELECT COUNT(*) FROM pluggy_investment_transactions').fetchone()[0]
        con.execute('DELETE FROM pluggy_investment_transactions')
        con.execute('DELETE FROM pluggy_investments')
        # Pluggy event ledger is investment/dividend import provenance, not the bank account feed.
        deleted_events = con.execute("SELECT COUNT(*) FROM pluggy_imported_events").fetchone()[0]
        con.execute('DELETE FROM pluggy_imported_events')
        return {'pluggy_investments': len(inv_ids), 'pluggy_transactions': int(deleted_tx), 'pluggy_events': int(deleted_events)}
    ticker = str(ticker or '').strip().upper()
    if not ticker:
        return {'pluggy_investments': 0, 'pluggy_transactions': 0, 'pluggy_events': 0}
    inv_ids = [r[0] for r in con.execute(
        "SELECT investment_id FROM pluggy_investments WHERE upper(code)=? OR upper(name)=?",
        (ticker, ticker)
    ).fetchall()]
    # Include investment ids found through their cached transaction ticker.
    extra_ids = [r[0] for r in con.execute(
        "SELECT DISTINCT investment_id FROM pluggy_investment_transactions WHERE upper(ticker)=?",
        (ticker,)
    ).fetchall()]
    inv_ids = list(dict.fromkeys(inv_ids + extra_ids + investment_ids))
    deleted_tx = 0
    if inv_ids:
        for start in range(0, len(inv_ids), 400):
            chunk = inv_ids[start:start+400]
            marks = ','.join('?' for _ in chunk)
            deleted_tx += con.execute(
                f'DELETE FROM pluggy_investment_transactions WHERE investment_id IN ({marks})', chunk
            ).rowcount
            con.execute(f'DELETE FROM pluggy_investments WHERE investment_id IN ({marks})', chunk)
    deleted_tx += con.execute(
        'DELETE FROM pluggy_investment_transactions WHERE upper(ticker)=?', (ticker,)
    ).rowcount
    deleted_events = con.execute(
        'DELETE FROM pluggy_imported_events WHERE upper(ticker)=?', (ticker,)
    ).rowcount
    # Remove any remaining Pluggy transaction-to-operation links for deleted operations.
    if operation_ids:
        for start in range(0, len(operation_ids), 500):
            chunk = operation_ids[start:start+500]
            marks = ','.join('?' for _ in chunk)
            con.execute(
                f"UPDATE pluggy_investment_transactions SET imported_operation_id=NULL, import_state='imported_only' WHERE imported_operation_id IN ({marks})",
                chunk
            )
    return {'pluggy_investments': len(inv_ids), 'pluggy_transactions': int(deleted_tx), 'pluggy_events': int(deleted_events)}


def rotate_predelete_backups(backup_root=None, keep=10):
    """Keep the newest complete backups; tolerate concurrent removal of old folders."""
    root = Path(backup_root or os.environ.get('BACKUP_DIR', '/data/backups'))
    keep = max(1, int(keep))
    candidates = []
    try:
        paths = list(root.glob('pre-exclusao-*'))
    except OSError as exc:
        app.logger.warning('Não foi possível listar backups preventivos: %s', exc)
        return []
    for path in paths:
        try:
            if path.is_symlink() or not path.is_dir():
                continue
            stat = path.stat()
            candidates.append((stat.st_mtime_ns, path.name, path))
        except FileNotFoundError:
            # Another cleanup process may have removed this directory after glob().
            continue
        except OSError as exc:
            app.logger.warning('Não foi possível inspecionar backup preventivo %s: %s', path.name, exc)
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    removed = []
    for _mtime, _name, old in candidates[keep:]:
        try:
            # Recheck type/existence immediately before deletion to avoid following symlinks.
            if old.is_symlink() or not old.is_dir():
                continue
            shutil.rmtree(old)
            removed.append(old.name)
        except FileNotFoundError:
            continue
        except OSError as exc:
            app.logger.warning('Não foi possível remover backup preventivo antigo %s: %s', old.name, exc)
    return removed


def create_predelete_backup():
    """Cria cópias SQLite consistentes antes de exclusões em massa."""
    backup_root = Path(os.environ.get('BACKUP_DIR', '/data/backups'))
    backup_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(TZ).strftime('%Y%m%d-%H%M%S-%f')
    folder = backup_root / f'pre-exclusao-{stamp}'
    folder.mkdir(mode=0o700)
    try:
        for source, connection in ((Path(DB_PATH), db()), (Path(TOKEN_DB_PATH), token_db())):
            target = folder / source.name
            dest = sqlite3.connect(str(target), timeout=30)
            try:
                connection.backup(dest)
                if dest.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise RuntimeError('Falha ao validar backup preventivo.')
            finally:
                dest.close()
        rotate_predelete_backups(backup_root, keep=10)
        return str(folder)
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise


@app.route('/api/operacoes/gerenciar', methods=['DELETE'])
def excluir_operacoes_em_massa():
    con=db()
    payload=request.get_json(silent=True) or {}
    scope=str(payload.get('escopo') or '').lower()
    if scope=='todas':
        all_rows=con.execute('SELECT id,detalhes FROM operacoes').fetchall()
        ids=[int(r['id']) for r in all_rows if not is_pluggy_operation(r)]
        ticker=''
        tipo=''
    elif scope=='ativo':
        tipo=str(payload.get('tipo') or '').strip().lower()
        ticker=str(payload.get('ticker') or '').strip().upper()
        if not tipo or not ticker:
            return json_error('Selecione a classe e o ativo para excluir os lançamentos.')
        asset_rows=con.execute('SELECT id,detalhes FROM operacoes WHERE lower(tipo)=? AND upper(ticker)=?',(tipo,ticker)).fetchall()
        ids=[int(r['id']) for r in asset_rows if not is_pluggy_operation(r)]
    else:
        return json_error('Escopo de exclusão inválido.')
    if payload.get('confirmar') is not True:
        return json_error('Confirme explicitamente a exclusão para continuar.', 409)
    try:
        if ids:
            selected = db().execute(
                'SELECT id,ticker,data,lado,quantidade,status,data_execucao FROM operacoes WHERE id IN (' + ','.join('?' for _ in ids) + ')', ids
            ).fetchall()
            for ticker in {str(row['ticker']).upper() for row in selected}:
                ticker_rows = [row for row in selected if str(row['ticker']).upper() == ticker]
                first_day = min(_operation_effective_date(dict(row)) for row in ticker_rows)
                if sale_breaks_future_position(ticker, first_day, 0.0, set(ids)):
                    return json_error(f'A exclusão deixaria uma venda posterior de {ticker} sem quantidade suficiente. Ajuste os lançamentos antes de excluir.', 409)
        backup_path = create_predelete_backup()
        pluggy_investment_ids = []
        if ids:
            for start in range(0, len(ids), 500):
                chunk = ids[start:start+500]
                marks = ','.join('?' for _ in chunk)
                for row in con.execute(f'SELECT detalhes FROM operacoes WHERE id IN ({marks})', chunk).fetchall():
                    try:
                        meta = json.loads(row[0] or '{}')
                        if meta.get('origem') == 'Pluggy' and meta.get('investment_id'):
                            pluggy_investment_ids.append(str(meta['investment_id']))
                    except Exception:
                        pass
        cleanup_b3_operation_refs(ids)
        for start in range(0,len(ids),500):
            chunk=ids[start:start+500]
            marks=','.join('?' for _ in chunk)
            con.execute(f'DELETE FROM operacoes WHERE id IN ({marks})',chunk)
        # Pluggy-imported operations and their source cache are immutable and excluded above.
        pluggy_removed = {'pluggy_investments': 0, 'pluggy_transactions': 0, 'pluggy_events': 0}
        if scope == 'todas':
            # Limpeza integral da carteira: lançamentos, snapshots/importações e cache de posições.
            # Mantém tokens, conexões Pluggy e transações bancárias para permitir nova sincronização.
            con.execute('DELETE FROM saldos_importados')
            con.execute('DELETE FROM proventos_api')
            con.execute('DELETE FROM proventos')
            con.execute('DELETE FROM b3_importados')
            con.execute('DELETE FROM b3_import_lotes')
            # Preserve Pluggy source records and imported-event ledger.
            con.execute('DELETE FROM integracao_estado WHERE chave LIKE ?', ('snapshot_%',))
            pluggy_removed = {'pluggy_investments': pluggy_removed.get('pluggy_investments',0),
                              'pluggy_transactions': pluggy_removed.get('pluggy_transactions',0),
                              'pluggy_events': pluggy_removed.get('pluggy_events',0)}
            CACHE.clear(); PORTFOLIO_HISTORY_CACHE.clear()
            PROVENTOS_SYNC_CACHE.update(time=0, result=None, signature=None)
        clear_seed_snapshot_when_empty()
        con.commit()
    except Exception:
        con.rollback()
        raise
    return jsonify(ok=True,excluidos=len(ids),pluggy_removidos=pluggy_removed,backup=backup_path)


@app.route('/api/operacoes/<int:op_id>', methods=['DELETE','PUT'])
def delete_operacao(op_id):
    current=db().execute('SELECT * FROM operacoes WHERE id=?',(op_id,)).fetchone()
    if not current: return json_error('Lançamento não encontrado.',404)
    if is_pluggy_operation(current):
        return json_error('Lançamentos importados pela Pluggy são somente leitura e não podem ser editados ou excluídos.', 403)
    if request.method == 'DELETE':
        effective_day = _operation_effective_date(dict(current))
        if sale_breaks_future_position(current['ticker'], effective_day, 0.0, {op_id}):
            return json_error('Não é possível excluir este lançamento: a alteração deixaria uma venda posterior sem quantidade suficiente.', 409)
        cleanup_b3_operation_refs([op_id])
        db().execute('DELETE FROM operacoes WHERE id=?', (op_id,))
        clear_seed_snapshot_when_empty()
        db().commit()
        return jsonify(ok=True)
    j=request.get_json(silent=True) or {}
    try:
        ticker=str(j.get('ticker',current['ticker'])).strip().upper()
        tipo=str(j.get('tipo',current['tipo'])).lower()
        lado=str(j.get('lado',current['lado'])).lower()
        qtd=float(j.get('quantidade',current['quantidade']))
        preco=float(j.get('preco',current['preco']))
        custos=float(j.get('custos',current['custos'] or 0))
        dt=str(j.get('data') or current['data'])
        allowed_types=('acao','fii','etf','bdr','internacional','renda_fixa','cripto','stocks','reit','tesouro','fundo','outros')
        date.fromisoformat(dt)
        if not all(math.isfinite(v) for v in (qtd, preco, custos)):
            raise ValueError('Valores financeiros precisam ser finitos.')
        if not ticker or len(ticker)>80 or tipo not in allowed_types or lado not in ('compra','venda') or qtd<=0 or preco<0 or custos<0: raise ValueError()
        if lado=='venda':
            existing_type=asset_type_for_ticker(ticker, dt)
            if not existing_type or existing_type!=tipo:
                return json_error('O ativo selecionado não pertence à categoria informada. Escolha o ativo dentro da categoria correta.')
        raw=str(j.get('data_hora') or (dt+'T10:00'))
        requested=datetime.fromisoformat(raw)
        if requested.tzinfo is None: requested=requested.replace(tzinfo=TZ)
        now=datetime.now(TZ); now_minute=now.replace(second=0,microsecond=0)
        if requested.date()<now.date():
            execution=requested; status='executada'
        elif tipo=='renda_fixa':
            execution=now; status='executada'
        elif requested.date()==now.date() and now.weekday()<5 and now.date() not in b3_holidays(now.year) and MARKET_OPEN<=requested.time()<MARKET_CLOSE and requested<=now_minute:
            execution=requested; status='executada'
        else:
            execution=next_b3_session(requested); status='executada' if execution<=now_minute else 'agendada'
        effective_day = execution.date().isoformat()
        old_effective_day = _operation_effective_date(dict(current))
        if sale_breaks_future_position(current['ticker'], old_effective_day, 0.0, {op_id}):
            return json_error('A edição deixaria uma venda posterior do ativo original sem quantidade suficiente.', 409)
        delta = qtd if lado == 'compra' else -qtd
        if sale_breaks_future_position(ticker, effective_day, delta, {op_id}):
            return json_error('A edição deixaria a posição negativa em uma data posterior, considerando compras, vendas agendadas e saldos importados.', 409)
        details=j.get('detalhes',current['detalhes'] or '{}')
        if isinstance(details,dict): details=json.dumps(details,ensure_ascii=False)
        else:
            parsed=json.loads(details or '{}'); details=json.dumps(parsed,ensure_ascii=False)
        db().execute('UPDATE operacoes SET ticker=?,tipo=?,lado=?,quantidade=?,preco=?,custos=?,data=?,status=?,data_execucao=?,detalhes=? WHERE id=?',
            (ticker,tipo,lado,qtd,preco,custos,execution.date().isoformat(),status,execution.isoformat(timespec='minutes'),details,op_id))
        db().commit(); CACHE.pop(current['ticker'],None); CACHE.pop(ticker,None)
        return jsonify(ok=True,status=status,data_execucao=execution.isoformat(timespec='minutes'))
    except Exception:
        return json_error('Confira ativo, tipo, operação, quantidade, preço, custos e data do lançamento.')


@app.route('/api/proventos', methods=['GET','POST'])
def proventos():
    if request.method == 'GET':
        rows = db().execute('SELECT * FROM proventos ORDER BY data DESC,id DESC').fetchall()
        return jsonify(itens=[dict(r) for r in rows])
    j = request.get_json(silent=True) or {}
    try:
        ticker = str(j['ticker']).strip().upper(); valor = float(j['valor_total']); dt = str(j['data']); date.fromisoformat(dt)
        if not ticker or not math.isfinite(valor) or valor <= 0: raise ValueError()
    except Exception: return json_error('Confira ticker, valor e data.')
    db().execute('INSERT INTO proventos(ticker,valor_total,data) VALUES(?,?,?)', (ticker,valor,dt)); db().commit()
    return jsonify(ok=True)


@app.route('/api/proventos/sincronizar', methods=['POST'])
def sincronizar_proventos():
    result = sync_brapi_proventos(force=True, quote_items=carteira().json.get('itens', []))
    return jsonify(result)


def _b3_normalize(value):
    raw = unicodedata.normalize('NFKD', str(value or '').strip().lower())
    return ''.join(ch for ch in raw if not unicodedata.combining(ch))


def _b3_number(value):
    if value is None or value == '-':
        return 0.0
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        raw = str(value).strip().replace('R$', '').replace(' ', '')
        if not raw or raw == '-':
            return 0.0
        if ',' in raw:
            raw = raw.replace('.', '').replace(',', '.')
        number = float(raw)
    if not math.isfinite(number):
        raise ValueError('A planilha B3 contém um valor numérico não finito.')
    return number


def _b3_date(value):
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raw = str(value or '').strip()
    for fmt in ('%d/%m/%Y', '%Y-%m-%d'):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            pass
    raise ValueError('Data inválida na planilha B3: ' + raw)


def _b3_asset(product):
    name = str(product or '').strip()
    parts = name.rsplit(' - ', 1)
    ticker = parts[0].strip().upper()
    suffix = _b3_normalize(parts[1]) if len(parts) == 2 else ''
    explicit_types = {
        'acao':'acao','acoes':'acao','fii':'fii','etf':'etf','bdr':'bdr',
        'internacional':'internacional','stocks':'stocks','reit':'reit',
        'cripto':'cripto','criptomoeda':'cripto','renda_fixa':'renda_fixa',
        'tesouro':'tesouro','fundo':'fundo','outros':'outros'
    }
    if suffix in explicit_types:
        return ticker[:80], explicit_types[suffix]
    if not ticker:
        return '', 'outros'
    if _b3_normalize(name).startswith(('lca ', 'lci ', 'cdb ', 'rdb ', 'tesouro ')) or ticker.startswith(('LCA', 'LCI', 'CDB', 'RDB')):
        return ticker[:80], 'renda_fixa'
    if re.fullmatch(r'[A-Z]{4}11', ticker):
        return ticker, 'fii' if ('fii' in _b3_normalize(name) or 'imob' in _b3_normalize(name)) else 'etf'
    if re.fullmatch(r'[A-Z]{4}(3[0-9]|[4-9][0-9])', ticker):
        return ticker, 'bdr'
    if re.fullmatch(r'[A-Z]{4}[0-9]{1,2}', ticker):
        return ticker, 'acao'
    return ticker[:80], 'outros'


MAX_B3_UPLOAD_BYTES = 16 * 1024 * 1024
MAX_B3_XLSX_UNCOMPRESSED_BYTES = 128 * 1024 * 1024
MAX_B3_XLSX_ENTRIES = 512
MAX_B3_XLSX_SINGLE_XML_BYTES = 64 * 1024 * 1024
MAX_B3_XLSX_ROWS = 200_000
MAX_B3_XLSX_COLUMNS = 64


def _validate_b3_xlsx_container(raw):
    """Reject malformed/oversized ZIP containers before OpenPyXL parses XML."""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            infos = archive.infolist()
            if not infos or len(infos) > MAX_B3_XLSX_ENTRIES:
                raise ValueError('A planilha contém uma quantidade inválida de arquivos internos.')
            names = {info.filename for info in infos}
            if '[Content_Types].xml' not in names or 'xl/workbook.xml' not in names:
                raise ValueError('O arquivo não possui a estrutura esperada de uma planilha XLSX.')
            if 'xl/vbaProject.bin' in names:
                raise ValueError('Planilhas com macros não são aceitas. Exporte novamente como .xlsx sem macros.')
            expanded_total = 0
            for info in infos:
                if info.is_dir():
                    continue
                if info.file_size < 0 or info.compress_size < 0:
                    raise ValueError('A planilha contém metadados ZIP inválidos.')
                expanded_total += info.file_size
                if expanded_total > MAX_B3_XLSX_UNCOMPRESSED_BYTES:
                    raise ValueError('A planilha descompactada ultrapassa o limite seguro de 128 MB.')
                if info.file_size > MAX_B3_XLSX_SINGLE_XML_BYTES:
                    raise ValueError('Um componente interno da planilha ultrapassa o limite seguro de 64 MB.')
                if info.file_size > 1_000_000 and info.compress_size > 0:
                    if info.file_size / info.compress_size > 200:
                        raise ValueError('A planilha apresenta uma taxa de compressão suspeita.')
            if not any(name.startswith('xl/worksheets/sheet') and name.endswith('.xml') for name in names):
                raise ValueError('A planilha não contém uma aba de dados válida.')
    except zipfile.BadZipFile as exc:
        raise ValueError('O arquivo XLSX está corrompido ou não é um ZIP válido.') from exc


def _parse_b3_workbook(file_storage):
    if not file_storage or not str(file_storage.filename or '').lower().endswith('.xlsx'):
        raise ValueError('Selecione uma planilha Excel da B3 no formato .xlsx.')
    raw = file_storage.read(MAX_B3_UPLOAD_BYTES + 1)
    if len(raw) > MAX_B3_UPLOAD_BYTES:
        raise RequestEntityTooLarge()
    if not raw:
        raise ValueError('O arquivo selecionado está vazio.')
    _validate_b3_xlsx_container(raw)
    # OpenPyXL uses defusedxml when installed. Fail closed rather than process
    # untrusted workbook XML with an unprotected parser.
    from openpyxl.xml import DEFUSEDXML
    if not DEFUSEDXML:
        raise RuntimeError('Proteção XML indisponível. Instale a dependência defusedxml.')
    wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    try:
        ws = wb.active
        if ws is None or (ws.max_column or 0) > MAX_B3_XLSX_COLUMNS:
            raise ValueError('A planilha excede o limite de 64 colunas suportadas.')
        rows = ws.iter_rows(values_only=True)
        header = next(rows, None)
        if not header:
            raise ValueError('A planilha não contém cabeçalho.')
        headers = {_b3_normalize(v): i for i, v in enumerate(header) if v is not None}
        required = ['entrada/saida', 'data', 'movimentacao', 'produto', 'quantidade', 'preco unitario', 'valor da operacao']
        missing = [h for h in required if h not in headers]
        if missing:
            raise ValueError('Não reconheci o formato da planilha. Confira se é a planilha de movimentações exportada pela B3.')
        parsed = []
        for row_number, row in enumerate(rows, start=2):
            if row_number > MAX_B3_XLSX_ROWS + 1:
                raise ValueError('A planilha ultrapassa o limite de 200.000 linhas.')
            if not row or not any(v is not None for v in row):
                continue
            get = lambda key: row[headers[key]] if headers[key] < len(row) else None
            movement = str(get('movimentacao') or '').strip()
            movement_norm = _b3_normalize(movement)
            entry = _b3_normalize(get('entrada/saida'))
            product = str(get('produto') or '').strip()
            ticker, asset_type = _b3_asset(product)
            if not ticker:
                continue
            dt = _b3_date(get('data'))
            qty = _b3_number(get('quantidade'))
            price = _b3_number(get('preco unitario'))
            amount = _b3_number(get('valor da operacao'))
            category = None
            record = None
            if movement_norm in ('rendimento', 'juros sobre capital proprio', 'dividendo'):
                if amount <= 0:
                    continue
                category = 'provento'
                record = {'ticker': ticker, 'valor_total': amount, 'data': dt, 'tipo_evento': movement}
            elif movement_norm in ('transferencia - liquidacao', 'compra / venda'):
                if qty <= 0 or amount <= 0:
                    continue
                side = 'venda' if entry in ('debito', 'saida', 'saída') else 'compra'
                if movement_norm == 'compra / venda' and asset_type == 'renda_fixa':
                    side = 'compra' if entry not in ('debito', 'saida', 'saída') else 'venda'
                category = 'operacao'
                record = {'ticker': ticker, 'tipo': asset_type, 'lado': side, 'quantidade': qty,
                          'preco': price if price > 0 else amount / qty, 'data': dt, 'custos': 0.0,
                          'status': 'executada', 'detalhes': {'origem': 'Importação B3',
                          'movimentacao': movement, 'produto': product}}
            else:
                # Eventos sem valor negociado não viram compra/venda ou provento.
                continue
            fingerprint = hashlib.sha256(json.dumps(
                [row_number, category, entry, dt, movement_norm, product, qty, price, amount],
                ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
            parsed.append({'fingerprint': fingerprint, 'categoria': category, 'registro': record})
        return parsed
    finally:
        wb.close()


@app.route('/api/importar-b3/lotes', methods=['GET'])
def listar_lotes_b3():
    con = db()
    batches = [dict(r) for r in con.execute("""
        SELECT l.id, l.arquivo, l.importado_em, l.operacoes, l.proventos,
               COUNT(i.fingerprint) AS registros
        FROM b3_import_lotes l LEFT JOIN b3_importados i ON i.lote_id=l.id
        GROUP BY l.id ORDER BY l.importado_em DESC
    """).fetchall()]
    legacy = [dict(r) for r in con.execute("""
        SELECT importado_em, SUM(CASE WHEN categoria='operacao' THEN 1 ELSE 0 END) AS operacoes,
               SUM(CASE WHEN categoria='provento' THEN 1 ELSE 0 END) AS proventos,
               COUNT(*) AS registros
        FROM b3_importados WHERE lote_id IS NULL GROUP BY importado_em ORDER BY importado_em DESC
    """).fetchall()]
    return jsonify(lotes=batches, legados=legacy)


@app.route('/api/importar-b3/lotes/<lote_id>', methods=['DELETE'])
def excluir_lote_b3(lote_id):
    con = db()
    lote = con.execute('SELECT * FROM b3_import_lotes WHERE id=?', (lote_id,)).fetchone()
    if not lote:
        return jsonify(error='Importação não encontrada ou sem vínculo seguro.'), 404
    rows = con.execute('SELECT categoria,registro_id FROM b3_importados WHERE lote_id=?', (lote_id,)).fetchall()
    deleted_ops = deleted_provs = 0
    try:
        for row in rows:
            if not row['registro_id']:
                continue
            if row['categoria'] == 'operacao':
                cur = con.execute("DELETE FROM operacoes WHERE id=? AND detalhes LIKE '%Importação B3%'", (row['registro_id'],))
                deleted_ops += cur.rowcount
            elif row['categoria'] == 'provento':
                cur = con.execute('DELETE FROM proventos WHERE id=?', (row['registro_id'],))
                deleted_provs += cur.rowcount
        con.execute('DELETE FROM b3_importados WHERE lote_id=?', (lote_id,))
        con.execute('DELETE FROM b3_import_lotes WHERE id=?', (lote_id,))
        con.commit()
    except Exception:
        con.rollback()
        raise
    return jsonify(ok=True, operacoes=deleted_ops, proventos=deleted_provs)


def _b3_semantic_duplicate(item):
    """Detecta duplicatas apenas contra registros já persistidos, na mesma data.

    Não aproxima datas em uma janela de dias: execuções parciais e aportes iguais
    em dias diferentes são operações legítimas. A deduplicação do mesmo arquivo
    continua sendo feita pelo fingerprint exato da linha B3.
    """
    rec=item['registro']
    if item['categoria']=='operacao':
        row=db().execute(
            """SELECT id FROM operacoes WHERE upper(ticker)=? AND lower(tipo)=? AND lower(lado)=?
               AND date(data)=date(?)
               AND ABS(quantidade-?)<=0.00000001
               AND ABS(preco-?)<=0.00000001 LIMIT 1""",
            (str(rec['ticker']).upper(),str(rec['tipo']).lower(),str(rec['lado']).lower(),
             rec['data'],float(rec['quantidade']),float(rec['preco']))
        ).fetchone()
        return row['id'] if row else None
    row=db().execute(
        """SELECT id FROM proventos WHERE upper(ticker)=? AND date(data)=date(?)
           AND ABS(valor_total-?)<=0.00000001 LIMIT 1""",
        (str(rec['ticker']).upper(),rec['data'],float(rec['valor_total']))
    ).fetchone()
    return row['id'] if row else None
@app.route('/api/importar-b3', methods=['POST'])
def importar_b3():
    try:
        upload = request.files.get('arquivo')
        parsed=_parse_b3_workbook(upload)
        filename = os.path.basename(upload.filename or 'Planilha B3.xlsx')
        mode=request.form.get('modo','preview')
        existing={r['fingerprint'] for r in db().execute('SELECT fingerprint FROM b3_importados').fetchall()}
        new_rows=[]; semantic_duplicates=0
        # A fotografia de duplicatas é tomada antes de inserir qualquer linha do lote.
        # Assim, duas execuções legítimas e iguais no mesmo arquivo não se eliminam.
        for item in parsed:
            if item['fingerprint'] in existing or _b3_semantic_duplicate(item):
                semantic_duplicates+=1
            else:
                new_rows.append(item)
        if mode != 'importar':
            return jsonify(ok=True,total=len(parsed),novos=len(new_rows),duplicados=semantic_duplicates,
                operacoes=sum(1 for r in new_rows if r['categoria']=='operacao'),
                proventos=sum(1 for r in new_rows if r['categoria']=='provento'))
        inserted_ops=inserted_provs=0
        duplicates=semantic_duplicates
        con=db()
        if not new_rows:
            return jsonify(ok=True,importadas=0,operacoes=0,proventos=0,duplicados=len(parsed),lote_id=None)
        lote_id=uuid.uuid4().hex
        imported_at=datetime.now(TZ).isoformat(timespec='seconds')
        con.execute('INSERT INTO b3_import_lotes(id,arquivo,importado_em,operacoes,proventos) VALUES(?,?,?,?,?)',
                    (lote_id,filename,imported_at,0,0))
        for item in new_rows:
            # Fingerprints já existentes foram filtrados antes da transação; a
            # restrição PRIMARY KEY permanece como última defesa contra reimportação.
            if con.execute('SELECT 1 FROM b3_importados WHERE fingerprint=?',(item['fingerprint'],)).fetchone():
                duplicates+=1; continue
            rec=item['registro']
            if item['categoria']=='operacao':
                cur=con.execute("INSERT INTO operacoes(ticker,tipo,lado,quantidade,preco,data,custos,status,data_registro,data_execucao,detalhes) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (rec['ticker'],rec['tipo'],rec['lado'],rec['quantidade'],rec['preco'],rec['data'],0.0,'executada',rec['data'],rec['data']+'T10:00:00-03:00',json.dumps(rec['detalhes'],ensure_ascii=False)))
                record_id=cur.lastrowid; inserted_ops+=1
            else:
                cur=con.execute('INSERT INTO proventos(ticker,valor_total,data) VALUES(?,?,?)',(rec['ticker'],rec['valor_total'],rec['data']))
                record_id=cur.lastrowid; inserted_provs+=1
            con.execute('INSERT INTO b3_importados(fingerprint,categoria,importado_em,lote_id,registro_id) VALUES(?,?,?,?,?)',
                        (item['fingerprint'],item['categoria'],imported_at,lote_id,record_id))
        con.execute('UPDATE b3_import_lotes SET operacoes=?,proventos=? WHERE id=?',(inserted_ops,inserted_provs,lote_id))
        con.commit()
        # A importação altera a carteira: verifica eventos de proventos dos ativos que permaneceram em posição.
        dividend_sync = None
        try:
            current_items = (carteira().json or {}).get('itens', [])
            dividend_sync = sync_brapi_proventos(force=False, quote_items=current_items, active_items=current_items)
        except Exception as dividend_exc:
            dividend_sync = {'ok': False, 'mensagem': safe_error_text(dividend_exc, 120)}
        return jsonify(ok=True,importadas=inserted_ops+inserted_provs,operacoes=inserted_ops,proventos=inserted_provs,duplicados=duplicates,lote_id=lote_id, sincronizacao_proventos=dividend_sync)
    except RequestEntityTooLarge:
        return json_error('Arquivo grande demais. O limite de envio é 16 MB.', 413)
    except ValueError as exc:
        return json_error(str(exc))
    except Exception as exc:
        app.logger.exception('Falha ao importar planilha B3: %s', exc)
        return json_error('Não foi possível ler a planilha B3. Confira o arquivo e tente novamente.')


@app.route('/api/metas', methods=['GET','POST'])
def metas():
    if request.method == 'GET':
        return jsonify(itens=[dict(r) for r in db().execute('SELECT * FROM metas ORDER BY id DESC').fetchall()])
    j = request.get_json(silent=True) or {}
    try:
        nome = str(j['nome']).strip(); objetivo = float(j['objetivo']); aporte = float(j.get('aporte',0)); tipo = str(j.get('tipo','patrimonio'))
        categoria = str(j.get('categoria','')); ticker = str(j.get('ticker','')).strip().upper(); variacao = float(j.get('variacao_anual',0)); classes = j.get('classes','[]')
        if not all(math.isfinite(v) for v in (objetivo, aporte, variacao)) or not nome or objetivo <= 0 or aporte < 0 or not -100 <= variacao <= 1000: raise ValueError()
        if isinstance(classes, list): classes = json.dumps(classes, ensure_ascii=False)
        else:
            parsed = json.loads(str(classes or '[]'))
            if not isinstance(parsed, list): raise ValueError()
            classes = json.dumps(parsed, ensure_ascii=False)
        if tipo == 'ativos' and not ticker: raise ValueError()
        if tipo == 'proventos' and not json.loads(classes): raise ValueError()
    except Exception: return json_error('Informe os dados da meta corretamente.')
    db().execute('INSERT INTO metas(nome,objetivo,aporte,tipo,categoria,ticker,variacao_anual,classes) VALUES(?,?,?,?,?,?,?,?)',(nome,objetivo,aporte,tipo,categoria,ticker,variacao,classes)); db().commit()
    return jsonify(ok=True)


@app.route('/api/metas/<int:meta_id>', methods=['DELETE'])
def delete_meta(meta_id):
    db().execute('DELETE FROM metas WHERE id=?',(meta_id,)); db().commit(); return jsonify(ok=True)


def _pluggy_total_return(ticker, current_value, dividends=0.0):
    """Retorno total desde a primeira compra usando o histórico importado da Pluggy."""
    rows = db().execute(
        "SELECT movement_type, transaction_date, quantity, unit_value, amount, raw_json "
        "FROM pluggy_investment_transactions WHERE UPPER(ticker)=? "
        "ORDER BY transaction_date, transaction_id",
        (str(ticker or '').upper(),)
    ).fetchall()
    if not rows:
        return None
    buys = sells = 0.0
    first_buy = None
    for row in rows:
        movement = str(row['movement_type'] or '').upper()
        amount = abs(float(row['amount'] or 0))
        qty = abs(float(row['quantity'] or 0))
        unit = abs(float(row['unit_value'] or 0))
        if amount <= 0 and qty > 0 and unit > 0:
            amount = qty * unit
        try:
            raw = json.loads(row['raw_json'] or '{}')
        except (TypeError, ValueError):
            raw = {}
        expenses = _pluggy_expenses_total(raw)
        if movement == 'BUY':
            buys += amount + expenses
            if first_buy is None:
                first_buy = str(row['transaction_date'] or '')[:10] or None
        elif movement == 'SELL':
            sells += max(0.0, amount - expenses)
    if buys <= 0:
        return None
    # Variação considera somente resultado de preço e operações realizadas.
    # Rentabilidade total acrescenta os proventos recebidos no período.
    variation_value = float(current_value or 0) + sells - buys
    total_return_value = variation_value + float(dividends or 0)
    return {
        'variation_pct': round(variation_value / buys * 100, 2),
        'variation_value': round(variation_value, 2),
        'total_pct': round(total_return_value / buys * 100, 2),
        'total_value': round(total_return_value, 2),
        'invested': round(buys, 2), 'first_buy': first_buy
    }


def _cotacao_worker(symbol, force_refresh=False):
    with app.app_context():
        return cotacao(symbol, force_refresh=force_refresh)


def _prefetch_quotes_parallel(tickers):
    """Consulta cotações em paralelo; o cliente brapi serializa o tráfego no limite gratuito."""
    symbols = sorted({str(t or '').strip().upper() for t in tickers if str(t or '').strip()})
    now = time.time()
    pending = [s for s in symbols if s not in CACHE or now - CACHE[s][0] >= CACHE_TTL]
    if not pending:
        return
    with ThreadPoolExecutor(max_workers=min(8, len(pending))) as pool:
        futures = [pool.submit(_cotacao_worker, symbol) for symbol in pending]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception:
                pass


@app.route('/api/carteira')
def carteira(use_cached_quotes=False, force_refresh_quotes=False):
    process_due_operations()
    base_date = snapshot_date_for()
    ticker_rows = db().execute('SELECT DISTINCT ticker FROM operacoes').fetchall()
    tickers = {str(row[0]).upper() for row in ticker_rows}
    tickers.update(str(row[0]).upper() for row in db().execute(
        "SELECT DISTINCT s.ticker FROM pluggy_position_snapshots s "
        "JOIN pluggy_items i ON i.item_id=s.item_id WHERE i.active=1"
    ).fetchall())
    if base_date:
        tickers.update(str(row[0]).upper() for row in db().execute(
            'SELECT ticker FROM saldos_importados WHERE data_base=?', (base_date,)
        ).fetchall())
    if force_refresh_quotes and tickers:
        quote_types = {}
        for row in db().execute('SELECT ticker,tipo FROM operacoes ORDER BY id').fetchall():
            quote_types[str(row['ticker']).upper()] = str(row['tipo'] or '').strip().lower()
        if base_date:
            for row in db().execute('SELECT ticker,tipo FROM saldos_importados WHERE data_base=?', (base_date,)).fetchall():
                quote_types[str(row['ticker']).upper()] = str(row['tipo'] or '').strip().lower()
        for row in db().execute(
            "SELECT s.ticker,s.asset_type FROM pluggy_position_snapshots s "
            "JOIN pluggy_items i ON i.item_id=s.item_id WHERE i.active=1"
        ).fetchall():
            quote_types[str(row['ticker']).upper()] = str(row['asset_type'] or '').strip().lower()
        market_tickers = [ticker for ticker in sorted(tickers) if quote_types.get(ticker) != 'renda_fixa']
        if market_tickers:
            with ThreadPoolExecutor(max_workers=4) as quote_pool:
                quote_jobs = [quote_pool.submit(_cotacao_worker, ticker, True) for ticker in market_tickers]
                for quote_job in as_completed(quote_jobs):
                    try:
                        quote_job.result()
                    except Exception:
                        # cotacao() preserva a última cotação válida quando uma fonte falha.
                        pass
    itens=[]; invested=actual=received=0.0
    receipt_totals = received_proventos_by_ticker(tickers)
    for ticker in sorted(tickers):
        qty, avg = posicao(ticker)
        pluggy_position = db().execute(
            "SELECT s.asset_type, SUM(s.quantity) AS quantity, "
            "SUM(s.amount_original) AS amount_original, SUM(s.balance) AS balance, "
            "MAX(s.snapshot_at) AS snapshot_at "
            "FROM pluggy_position_snapshots s JOIN pluggy_items i ON i.item_id=s.item_id "
            "WHERE i.active=1 AND UPPER(s.ticker)=? GROUP BY UPPER(s.ticker)",
            (ticker,)
        ).fetchone()
        if pluggy_position:
            # A fotografia é autoritativa inclusive quando a quantidade é zero.
            qty = max(0.0, float(pluggy_position['quantity'] or 0))
            original = float(pluggy_position['amount_original'] or 0)
            history_average = _pluggy_average_purchase_price(ticker)
            avg = history_average if history_average and history_average > 0 else (original / qty if original > 0 and qty > 0 else avg)
        if qty <= 1e-8: continue
        imported = imported_balance(ticker)
        op_kind = db().execute('SELECT tipo FROM operacoes WHERE ticker=? ORDER BY id LIMIT 1',(ticker,)).fetchone()
        kind = (pluggy_position['asset_type'] if pluggy_position else None) or (imported.get('tipo') if imported else None) or (op_kind['tipo'] if op_kind else 'outros')
        # Renda fixa usa o indexador e a data de cada aplicação, não cotação de bolsa.
        quote = {} if kind == 'renda_fixa' else (cotacao_cacheada(ticker) if use_cached_quotes else cotacao(ticker, force_refresh=force_refresh_quotes))
        observed_price = _first_positive_price(
            quote.get('regularMarketPrice'), quote.get('close'), quote.get('price')
        )
        quote_is_stale = bool(quote.get('quoteIsStale'))
        market_quote_available = observed_price > 0 and not quote_is_stale
        stale_quote_available = observed_price > 0 and quote_is_stale
        imported_price = float(imported.get('preco_base') or 0) if imported else 0.0
        snapshot_balance = float(pluggy_position['balance'] or 0) if pluggy_position else 0.0
        snapshot_balance_available = math.isfinite(snapshot_balance) and snapshot_balance > 0
        if market_quote_available:
            price = observed_price
            value = qty * price
            quote_source = quote.get('quoteProvider') or 'brapi'
        elif snapshot_balance_available:
            # A instituição fornece saldo atual, não cotação de mercado. Preserva
            # o patrimônio informado sem apresentar esse valor como uma cotação.
            value = snapshot_balance
            price = value / qty if qty > 0 else 0.0
            quote_source = 'Saldo atual da Pluggy (sem cotação de mercado)'
        elif stale_quote_available:
            price = observed_price
            value = qty * price
            quote_source = quote.get('quoteProvider') or 'Última cotação salva'
        elif imported_price > 0:
            price = imported_price
            value = qty * price
            quote_source = 'Preço-base importado (referência histórica)'
        else:
            price = avg
            value = qty * price
            quote_source = 'Custo médio (estimativa sem cotação atual)'
        quote_available = market_quote_available
        price_reference_available = market_quote_available or stale_quote_available
        dividends = float(receipt_totals.get(ticker, 0.0))
        divs = (quote.get('dividendsData') or {}).get('cashDividends',[])[:12]
        annual_div = sum(float(x.get('rate') or 0) for x in divs)
        ceiling = annual_div / .06 if annual_div else None
        cost = qty * avg
        fixed_calc_ok = False
        fixed_calc_warning = None
        fixed_tax_estimate = {'saldo': None, 'iof': None, 'ir': None, 'mensagem': 'Aguardando atualização do indexador.'}
        if kind == 'renda_fixa':
            if use_cached_quotes:
                # Não consulta BCB/SGS na primeira pintura; usa o saldo-base ou
                # custo médio como referência temporária até a atualização completa.
                fixed_calc_ok = False
                fixed_calc_warning = 'Atualização do indexador em segundo plano'
                quote_available = False
                quote_source = 'Atualização do indexador'
            else:
                cost, value, fixed_calc_ok, fixed_calc_warning = _fixed_income_position_value(ticker, cost, value)
                price = value / qty if qty > 0 else value
                quote_available = fixed_calc_ok
                price_reference_available = fixed_calc_ok
                quote_source = ('Saldo atual informado pela instituição'
                                if fixed_calc_warning and 'saldo atual informado pela instituição' in str(fixed_calc_warning).lower()
                                else 'Indexador' if fixed_calc_ok else 'Sem indexador')
                if fixed_calc_ok and not quote.get('quoteLastUpdated'):
                    quote['quoteLastUpdated'] = datetime.now(TZ).isoformat(timespec='seconds')
                fixed_tax_estimate = _fixed_income_net_balance_estimate(ticker, value)
        pluggy_return = _pluggy_total_return(ticker, value, dividends) if pluggy_position else None
        invested += cost; actual += value; received += (0.0 if kind == 'renda_fixa' else dividends)
        default_name = imported.get('nome') if imported else ticker
        daily_variation = quote.get('regularMarketChangePercent') if quote.get('regularMarketChangePercent') is not None else quote.get('changePercent')
        # Variação individual: diferença entre último preço conhecido e preço médio.
        # Rentabilidade individual: essa diferença somada apenas aos proventos pagos.
        # Não usamos retorno histórico da Pluggy aqui para manter percentual e reais na mesma base.
        has_usable_valuation = price_reference_available or snapshot_balance_available or (kind == 'renda_fixa' and fixed_calc_ok)
        if kind == 'renda_fixa' and fixed_calc_ok:
            variation_value = value - cost
            variation_pct = round(variation_value / cost * 100, 2) if cost else None
        elif price_reference_available and avg > 0:
            variation_value = (observed_price - avg) * qty
            variation_pct = round((observed_price / avg - 1) * 100, 2)
        else:
            variation_value = None
            variation_pct = None
        price_result = variation_value if variation_value is not None else 0.0
        total_return_value = price_result + (0.0 if kind == 'renda_fixa' else dividends)
        profitability_pct = round(total_return_value / cost * 100, 2) if cost else 0.0
        if kind == 'renda_fixa' and fixed_calc_ok:
            display_price = price
        elif price_reference_available:
            display_price = observed_price
        else:
            display_price = None
        itens.append({'ticker':ticker,'tipo':kind,'quantidade':round(qty,6),'preco_medio':round(avg,2),'preco_atual':round(display_price,2) if display_price is not None else None,'cotacao_disponivel':quote_available,'cotacao_desatualizada':stale_quote_available,'cotacao_referencia_disponivel':price_reference_available,'valor_atual_origem':quote_source,'rentabilidade_parcial':not has_usable_valuation,'cotacao_ultima_atualizacao':quote.get('quoteLastUpdated'),'cotacao_origem':quote_source,'investido':round(cost,2),'valor_atual':round(value,2),'lucro':round(variation_value,2) if variation_value is not None else None,'lucro_pct':profitability_pct,'variacao_valor':round(variation_value,2) if variation_value is not None else None,'variacao_pct':variation_pct,'pluggy_variacao_pct':pluggy_return['variation_pct'] if pluggy_return else None,'pluggy_variacao_valor':pluggy_return['variation_value'] if pluggy_return else None,'pluggy_retorno_pct':pluggy_return['total_pct'] if pluggy_return else None,'pluggy_retorno_valor':pluggy_return['total_value'] if pluggy_return else None,'pluggy_investido_historico':pluggy_return['invested'] if pluggy_return else None,'pluggy_primeira_compra':pluggy_return['first_buy'] if pluggy_return else None,'proventos':0.0 if kind=='renda_fixa' else round(dividends,2),'calculo_indexador':fixed_calc_ok if kind=='renda_fixa' else None,'aviso_calculo':fixed_calc_warning if kind=='renda_fixa' else None,'saldo_com_descontos':fixed_tax_estimate.get('saldo') if kind=='renda_fixa' else None,'desconto_iof_estimado':fixed_tax_estimate.get('iof') if kind=='renda_fixa' else None,'desconto_ir_estimado':fixed_tax_estimate.get('ir') if kind=='renda_fixa' else None,'aviso_saldo_com_descontos':fixed_tax_estimate.get('mensagem') if kind=='renda_fixa' else None,'pl':quote.get('priceEarnings'),'pvp':quote.get('priceToBook') or quote.get('priceToBookRatio') or quote.get('priceToBookValue'),'avaliacao':_fundamental_rating(quote, kind),'preco_teto_bazin':round(ceiling,2) if ceiling else None,'abaixo_do_teto':bool(ceiling and market_quote_available and observed_price and observed_price<=ceiling),'nome':quote.get('longName') or quote.get('shortName') or default_name or ticker,'logo':quote.get('logourl') or quote.get('logo') or ('/static/lonely-wallet-logo.png' if kind=='renda_fixa' else f'https://icons.brapi.dev/icons/{ticker}.svg'),'variacao_dia':variation_pct,'pluggy_snapshot_at':pluggy_position['snapshot_at'] if pluggy_position else None,'pluggy_snapshot':bool(pluggy_position)})
    allocation={}
    for item in itens: allocation[item['tipo']]=allocation.get(item['tipo'],0)+item['valor_atual']
    return jsonify(itens=itens,resumo={'investido':round(invested,2),'atual':round(actual,2),'lucro':round(actual-invested,2),'lucro_pct':round((actual/invested-1)*100,2) if invested else 0,'proventos':round(received,2),'ativos':len(itens)},alocacao=allocation)


def position_quantity_on_date_details(ticker, reference_date):
    """Retorna a quantidade estimada na data-com e informa a origem do cálculo.

    O saldo-base de 31/10/2026 é a fotografia mais confiável da carteira. Como o
    histórico importado pode não conter todas as operações anteriores, a quantidade
    retroativa é reconstruída revertendo as operações conhecidas. Se a reversão
    resultar em zero/negativo apesar de haver saldo-base, mantém-se uma estimativa
    pelo saldo-base, com aviso explícito, em vez de descartar silenciosamente o evento.
    """
    symbol = str(ticker or '').strip().upper()
    try:
        ref = date.fromisoformat(str(reference_date)[:10])
    except (TypeError, ValueError):
        return 0.0, 'data inválida'

    base_date = snapshot_date_for()
    if base_date:
        base_day = date.fromisoformat(base_date)
        if ref < base_day:
            base_row = db().execute(
                'SELECT quantidade FROM saldos_importados WHERE ticker=? AND data_base=?',
                (symbol, base_date)
            ).fetchone()
            if base_row:
                base_quantity = max(0.0, float(base_row['quantidade'] or 0))
                quantity = base_quantity
                later_ops = db().execute(
                    "SELECT lado,quantidade FROM operacoes WHERE ticker=? AND status='executada' AND data>? AND data<=? ORDER BY data DESC,id DESC",
                    (symbol, ref.isoformat(), base_date)
                ).fetchall()
                for operation in later_ops:
                    amount = float(operation['quantidade'] or 0)
                    if operation['lado'] == 'compra':
                        quantity -= amount
                    else:
                        quantity += amount
                if quantity > 1e-8:
                    return round(quantity, 8), f'estimada pela reversão do saldo-base de {base_date}; confirme o histórico de operações'
                if base_quantity > 1e-8:
                    return round(base_quantity, 8), f'estimada pelo saldo-base de {base_date}; histórico parcial não permite determinar a quantidade exata na data-com'
                return 0.0, 'sem saldo-base; quantidade depende do histórico de operações'

    quantity = posicao(symbol, as_of=ref.isoformat())[0]
    return round(max(0.0, quantity), 8), 'histórico de operações'


def position_quantity_on_date(ticker, reference_date):
    """Quantidade detida na data, respeitando saldo-base e operações posteriores."""
    return position_quantity_on_date_details(ticker, reference_date)[0]


def previous_b3_session(day):
    """Retorna o pregão anterior quando a fonte só informa a data ex."""
    candidate = day - timedelta(days=1)
    while candidate.weekday() >= 5 or candidate in b3_holidays(candidate.year):
        candidate -= timedelta(days=1)
    return candidate


def _api_date(value):
    """Normaliza datas ISO/date-time da brapi sem deslocar o dia por fuso horário."""
    if value in (None, ''):
        return ''
    raw = str(value).strip()
    if len(raw) >= 10 and raw[4:5] == '-' and raw[7:8] == '-':
        candidate = raw[:10]
        try:
            return date.fromisoformat(candidate).isoformat()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(raw.replace('Z', '+00:00')).date().isoformat()
    except ValueError:
        for fmt in ('%d/%m/%Y', '%Y/%m/%d'):
            try:
                return datetime.strptime(raw[:10], fmt).date().isoformat()
            except ValueError:
                continue
    return ''


def _asset_type_map():
    types = {}
    for row in db().execute('SELECT ticker,tipo FROM operacoes ORDER BY id').fetchall():
        types[str(row['ticker']).upper()] = str(row['tipo'] or 'outros').lower()
    base_date = snapshot_date_for()
    if base_date:
        for row in db().execute('SELECT ticker,tipo FROM saldos_importados WHERE data_base=?', (base_date,)).fetchall():
            types[str(row['ticker']).upper()] = str(row['tipo'] or 'outros').lower()
    return types


def _fetch_brapi_dividend_events(asset_type, symbols, start_date):
    """Busca eventos em dinheiro usando os endpoints v2 dedicados de ações e FIIs."""
    if not symbols:
        return [], []
    headers = {'Authorization': f'Bearer {_brapi_token()}'} if _brapi_token() else {}
    events, errors = [], []
    if asset_type == 'fii':
        url = 'https://brapi.dev/api/v2/fii/dividends'
        response_key = 'dividends'
    else:
        url = 'https://brapi.dev/api/v2/stocks/dividends'
        response_key = 'results'
    batch_size = BRAPI_MAX_TICKERS_PER_REQUEST
    for offset in range(0, len(symbols), batch_size):
        batch = symbols[offset:offset + batch_size]
        params = {'symbols': ','.join(batch), 'sortBy': 'paymentDate', 'sortOrder': 'asc'}
        if start_date:
            params['startDate'] = start_date
        # Algumas respostas limitam a janela padrão ao passado. Abrir endDate
        # permite receber pagamentos já anunciados para os próximos 12 meses.
        params['endDate'] = (datetime.now(TZ).date() + timedelta(days=365)).isoformat()
        if asset_type != 'fii' and _brapi_token():
            params['includeRaw'] = 'true'
        try:
            response = _brapi_get(url, params=params, headers=headers, timeout=18, cache_ttl=12 * 3600, stale_ttl=30 * 86400)
            if response.status_code in (400, 403) and params.get('includeRaw') == 'true':
                params.pop('includeRaw', None)
                response = _brapi_get(url, params=params, headers=headers, timeout=18, cache_ttl=12 * 3600, stale_ttl=30 * 86400)
            response.raise_for_status()
            payload = response.json()
            if asset_type == 'fii':
                events.extend(payload.get(response_key) or [])
            else:
                for result in payload.get(response_key) or []:
                    symbol = str(result.get('requestedSymbol') or result.get('symbol') or '').upper()
                    data = result.get('data') or {}
                    for event in data.get('cashDividends') or []:
                        events.append({**event, 'symbol': event.get('symbol') or symbol})
        except requests.RequestException as exc:
            code = getattr(getattr(exc, 'response', None), 'status_code', None)
            errors.append(f'{asset_type}: HTTP {code or "falha de conexão"} ao consultar {", ".join(batch)}')
        except (ValueError, TypeError):
            errors.append(f'{asset_type}: resposta inválida para {", ".join(batch)}')
    return events, errors




def sync_brapi_proventos(force=False, quote_items=None, active_items=None):
    """Sincroniza proventos apenas para posições atualmente mantidas. Idempotente e limitada por cache."""
    now_ts = time.time()
    if active_items is None:
        try:
            active_items = (carteira().json or {}).get('itens', [])
        except Exception:
            active_items = []
    types = {str(item.get('ticker') or '').strip().upper(): str(item.get('tipo') or 'outros').lower()
             for item in (active_items or []) if str(item.get('ticker') or '').strip()}
    signature = tuple(sorted(types.items()))
    if (not force and PROVENTOS_SYNC_CACHE.get('result')
            and PROVENTOS_SYNC_CACHE.get('signature') == signature
            and now_ts - PROVENTOS_SYNC_CACHE['time'] < PROVENTOS_SYNC_TTL):
        return PROVENTOS_SYNC_CACHE['result']

    if not types:
        result = {'ok': True, 'importados': 0, 'ignorados': 0, 'erros': [], 'mensagem': 'Carteira sem posições: consulta de proventos pausada; os registros históricos foram preservados.'}
        PROVENTOS_SYNC_CACHE.update(time=now_ts, result=result, signature=signature)
        return result

    # O intervalo começa no primeiro registro conhecido. Quando só existe saldo-base,
    # não presumimos que a posição já existia antes da fotografia.
    first_dates = [str(row['data'])[:10] for row in db().execute("SELECT data FROM operacoes WHERE status='executada'").fetchall() if row['data']]
    snapshot = snapshot_date_for()
    history_floor = (datetime.now(TZ).date() - timedelta(days=365)).isoformat()
    # Inclui pelo menos 12 meses de eventos, mesmo quando a primeira operação
    # cadastrada é recente ou o histórico foi importado como saldo-base.
    date_candidates = first_dates + ([snapshot] if snapshot else []) + [history_floor]
    start_date = min(date_candidates) if date_candidates else history_floor
    stock_types = {'acao', 'ações', 'bdr', 'etf', 'etfs', 'unit', 'indice', 'stocks', 'stock', 'internacional', 'etf_internacional'}
    stock_symbols = sorted(symbol for symbol, kind in types.items() if kind in stock_types)
    fii_symbols = sorted(symbol for symbol, kind in types.items() if kind in {'fii', 'fiis', 'fundo_imobiliario', 'fundos_imobiliarios'})

    # Sem token, evita enviar lotes com tickers protegidos. O sandbox da brapi documenta
    # apenas MXRF11/HGLG11 para FIIs; as ações são sincronizadas quando o endpoint autorizar.
    if not _brapi_token():
        fii_symbols = [symbol for symbol in fii_symbols if symbol in {'MXRF11', 'HGLG11'}]
        stock_symbols = [symbol for symbol in stock_symbols if symbol in {'PETR4', 'MGLU3', 'VALE3', 'ITUB4'}]

    fetched, errors = [], []
    stock_events, stock_errors = _fetch_brapi_dividend_events('acao', stock_symbols, start_date)
    fii_events, fii_errors = _fetch_brapi_dividend_events('fii', fii_symbols, start_date)
    fetched.extend((event, 'fii') for event in fii_events)
    fetched.extend((event, 'acao') for event in stock_events)
    errors.extend(stock_errors + fii_errors)

    # Fallback compatível com a cotação v1 usada pela versão estável: ela ainda
    # pode trazer eventos em dividendsData.cashDividends quando o endpoint v2
    # não está habilitado/coberto para determinado ticker. A data-com continua
    # obrigatória; não se presume elegibilidade usando exDate ou data atual.
    if quote_items is None:
        try:
            quote_items = carteira().json.get('itens', [])
        except Exception:
            quote_items = []
    # O fallback também consulta tickers que já foram vendidos: eles continuam
    # tendo histórico de proventos, mesmo sem posição atual na carteira.
    quote_symbols = set(types)
    quote_symbols.update(str(item.get('ticker') or '').upper() for item in (quote_items or []))
    for ticker in sorted(symbol for symbol in quote_symbols if symbol):
        kind = types.get(ticker, 'outros')
        if kind not in stock_types and kind not in {'fii', 'fiis', 'fundo_imobiliario', 'fundos_imobiliarios'}:
            continue
        quote = cotacao(ticker)
        for event in ((quote.get('dividendsData') or {}).get('cashDividends') or []):
            fetched.append(({**event, 'symbol': ticker}, 'fii' if kind in {'fii','fiis','fundo_imobiliario','fundos_imobiliarios'} else 'acao'))

    today = datetime.now(TZ).date()
    imported = ignored = 0
    conn = db()
    for event, source_type in fetched:
        ticker = str(event.get('symbol') or event.get('requestedSymbol') or '').strip().upper()
        if not ticker or ticker not in types:
            ignored += 1
            continue
        official_record_date = _api_date(event.get('lastDatePrior'))
        payment_date = _api_date(event.get('paymentDate'))
        ex_date = _api_date(event.get('exDate'))
        approved_date = _api_date(event.get('approvedOn'))
        # lastDatePrior é a data-com oficial. Se a fonte legada não a trouxer,
        # a véspera do exDate é usada apenas como estimativa e fica sinalizada.
        record_date = official_record_date
        estimated_date = False
        if not record_date and ex_date:
            try:
                record_date = previous_b3_session(date.fromisoformat(ex_date)).isoformat()
                estimated_date = True
            except ValueError:
                record_date = ''
        try:
            rate = float(event.get('rawRate') or event.get('rate') or event.get('value') or 0)
        except (TypeError, ValueError):
            rate = 0
        if rate <= 0:
            ignored += 1
            continue
        try:
            if record_date:
                qty, quantity_method = position_quantity_on_date_details(ticker, record_date)
            else:
                current_row = next((item for item in (quote_items or []) if str(item.get('ticker') or '').upper() == ticker), None)
                qty = float((current_row or {}).get('quantidade') or posicao(ticker)[0])
                quantity_method = 'posição atual (data-com ausente na fonte)'
        except Exception:
            qty, quantity_method = 0.0, 'quantidade indisponível'
        if qty <= 1e-8:
            ignored += 1
            continue
        total = round(qty * rate, 2)
        if total <= 0:
            ignored += 1
            continue
        raw_label = str(event.get('label') or ('RENDIMENTO' if source_type == 'fii' else 'PROVENTO')).strip().upper()
        label = 'JCP' if 'JCP' in raw_label or 'INTEREST' in raw_label else ('RENDIMENTO' if 'REND' in raw_label else 'DIVIDENDO')
        status = 'Pago' if payment_date and date.fromisoformat(payment_date) < today else 'A receber'
        # Data de pagamento pode ser corrigida pela fonte; não faz parte da chave.
        event_anchor = record_date or payment_date or approved_date or 'sem-data'
        event_key = '|'.join((ticker, label, event_anchor, f'{rate:.10f}'))
        remarks_parts = [str(event.get('remarks') or '').strip()]
        if estimated_date:
            remarks_parts.append('Data-com estimada pela véspera do exDate; confira o informe oficial.')
        if quantity_method.startswith('reconstruída'):
            remarks_parts.append('Quantidade reconstruída a partir do saldo-base; depende da completude das operações cadastradas.')
        elif quantity_method.startswith('posição atual'):
            remarks_parts.append('Quantidade estimada pela posição atual, pois a fonte não informou data-com.')
        remarks_text = ' '.join(part for part in remarks_parts if part)
        conn.execute('''
          INSERT INTO proventos_api (event_key,ticker,tipo_ativo,tipo_evento,data_aprovacao,data_com,data_ex,data_pagamento,valor_por_ativo,quantidade,valor_total,status,remarks,sincronizado_em)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(event_key) DO UPDATE SET
            tipo_ativo=excluded.tipo_ativo,tipo_evento=excluded.tipo_evento,data_aprovacao=excluded.data_aprovacao,
            data_ex=excluded.data_ex,data_pagamento=excluded.data_pagamento,valor_por_ativo=excluded.valor_por_ativo,quantidade=excluded.quantidade,
            valor_total=excluded.valor_total,status=excluded.status,remarks=excluded.remarks,sincronizado_em=excluded.sincronizado_em
        ''', (event_key,ticker,types[ticker],label,approved_date,record_date,ex_date,payment_date,rate,qty,total,status,remarks_text,datetime.now(TZ).isoformat(timespec='seconds')))
        imported += 1

    conn.execute('INSERT INTO integracao_estado(chave,valor) VALUES(?,?) ON CONFLICT(chave) DO UPDATE SET valor=excluded.valor',
                 ('brapi_proventos_ultima_sincronizacao', datetime.now(TZ).isoformat(timespec='seconds')))
    conn.commit()
    result = {
        'ok': not errors, 'importados': imported, 'ignorados': ignored, 'erros': [safe_error_text(error, 120) for error in errors],
        'mensagem': (f'{imported} evento(s) elegível(is) sincronizado(s).' if imported else 'Nenhum evento elegível retornado para os ativos cadastrados. Verifique token, plano e cobertura da brapi.'),
        'ultima_sincronizacao': datetime.now(TZ).isoformat(timespec='minutes')
    }
    PROVENTOS_SYNC_CACHE.update(time=now_ts, result=result, signature=signature)
    return result


def _is_income_event(event_type):
    return 'AMORTIZA' not in str(event_type or '').upper()


def _received_proventos_records(active_tickers=None):
    """Return paid receipts only, combining manual/B3/Pluggy records and paid API events."""
    active_set = None if active_tickers is None else {str(t).upper() for t in active_tickers}
    if active_set is not None and not active_set:
        return []
    today = datetime.now(TZ).date()
    pluggy_ids = {int(r['target_id']) for r in db().execute(
        "SELECT target_id FROM pluggy_imported_events WHERE event_type='provento' AND target_id IS NOT NULL"
    ).fetchall()}
    b3_ids = {int(r['registro_id']) for r in db().execute(
        "SELECT registro_id FROM b3_importados WHERE lower(categoria)='provento' AND registro_id IS NOT NULL"
    ).fetchall()}
    origin_by_id = {ident: 'Pluggy' for ident in pluggy_ids}
    for ident in b3_ids:
        origin_by_id.setdefault(ident, 'B3')

    records = []
    seen_fingerprints = set()
    rows = db().execute('SELECT id,ticker,valor_total,data FROM proventos ORDER BY data,id').fetchall()
    for raw in rows:
        row = dict(raw)
        payment_date = _api_date(row.get('data'))
        if not payment_date:
            continue
        try:
            if date.fromisoformat(payment_date[:10]) > today:
                continue
        except ValueError:
            continue
        ticker = str(row.get('ticker') or '').upper()
        if active_set is not None and ticker not in active_set:
            continue
        amount = round(float(row.get('valor_total') or 0), 2)
        if not math.isfinite(amount) or amount <= 0:
            continue
        key = (ticker, payment_date[:10], amount)
        origin = origin_by_id.get(int(row['id']), 'Manual')
        # Exact same payment imported from multiple origins is counted once.
        if key in seen_fingerprints:
            continue
        seen_fingerprints.add(key)
        records.append({
            'ticker': ticker, 'tipo': 'Provento', 'tipo_ativo': typesafe_asset_type(ticker),
            'status': 'Pago', 'data_com': None, 'data_pagamento': payment_date[:10],
            'quantidade': None, 'valor_por_ativo': None, 'valor_total': amount,
            'origem': origin, 'tipo_evento': 'PROVENTO'
        })

    api_rows = db().execute(
        "SELECT event_key,ticker,tipo_ativo,tipo_evento,data_com,data_pagamento,valor_por_ativo,quantidade,valor_total,status,remarks "
        "FROM proventos_api WHERE lower(trim(status))='pago' ORDER BY data_pagamento,event_key"
    ).fetchall()
    for raw in api_rows:
        row = dict(raw)
        payment_date = _api_date(row.get('data_pagamento'))
        if not payment_date:
            continue
        try:
            if date.fromisoformat(payment_date[:10]) > today:
                continue
        except ValueError:
            continue
        ticker = str(row.get('ticker') or '').upper()
        if not ticker or (active_set is not None and ticker not in active_set):
            continue
        amount = round(float(row.get('valor_total') or 0), 2)
        if not math.isfinite(amount) or amount <= 0 or not _is_income_event(row.get('tipo_evento')):
            continue
        fingerprint = (ticker, payment_date[:10], amount)
        if fingerprint in seen_fingerprints:
            continue
        seen_fingerprints.add(fingerprint)
        records.append({
            'ticker': ticker, 'tipo': 'Provento',
            'tipo_ativo': row.get('tipo_ativo') or typesafe_asset_type(ticker),
            'status': 'Pago', 'data_com': row.get('data_com'),
            'data_pagamento': payment_date[:10],
            'quantidade': float(row.get('quantidade') or 0) or None,
            'valor_por_ativo': float(row.get('valor_por_ativo') or 0) or None,
            'valor_total': amount, 'origem': 'BRAPI',
            'tipo_evento': row.get('tipo_evento') or 'PROVENTO',
            'remarks': row.get('remarks') or '', 'event_key': row.get('event_key')
        })
    records.sort(key=lambda item: (str(item.get('data_pagamento') or ''), str(item.get('ticker') or '')))
    return records


def _confirmed_received_proventos(records, as_of=None):
    """Keep only confirmed payments with payment dates through the local current date."""
    cutoff = as_of or datetime.now(TZ).date()
    confirmed = []
    for raw in records or []:
        row = dict(raw)
        if str(row.get('status') or '').strip().casefold() != 'pago':
            continue
        payment_date = _api_date(row.get('data_pagamento'))
        if not payment_date:
            continue
        try:
            paid_on = date.fromisoformat(payment_date[:10])
        except ValueError:
            continue
        if paid_on > cutoff or not _is_income_event(row.get('tipo_evento')):
            continue
        try:
            amount = float(row.get('valor_total') or 0)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(amount) or amount <= 0:
            continue
        row['data_pagamento'] = paid_on.isoformat()
        row['valor_total'] = round(amount, 2)
        confirmed.append(row)
    return confirmed


def _confirmed_received_proventos_by_month(records, month_keys, as_of=None):
    """Aggregate only confirmed payments inside the supplied rolling-month window."""
    allowed_months = {str(month) for month in month_keys}
    totals = {}
    for row in _confirmed_received_proventos(records, as_of=as_of):
        month = str(row['data_pagamento'])[:7]
        if month not in allowed_months:
            continue
        totals[month] = totals.get(month, 0.0) + float(row['valor_total'])
    return {month: round(value, 2) for month, value in totals.items()}


def _filter_upcoming_proventos_by_month(upcoming, as_of=None):
    """Keep announced payments in the current month or later, not overdue ones."""
    today = as_of or datetime.now(TZ).date()
    current_month = today.strftime('%Y-%m')
    filtered = []
    for raw in upcoming or []:
        row = dict(raw)
        if str(row.get('status') or '').strip().casefold() != 'a receber':
            continue
        payment_date = _api_date(row.get('data_pagamento'))
        if not payment_date or payment_date[:7] < current_month:
            continue
        try:
            amount = float(row.get('valor_total') or 0)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(amount) or amount <= 0:
            continue
        if not _is_income_event(row.get('tipo_evento') or row.get('tipo')):
            continue
        row['data_pagamento'] = payment_date[:10]
        row['valor_total'] = round(amount, 2)
        filtered.append(row)
    return filtered


def _confirmed_proventos_total_12m(received_total, upcoming, as_of=None):
    """Sum receipts from the last 12 calendar months and announced payments in the next 12 months.

    Upcoming entries must explicitly be marked ``A receber`` and have a valid
    future payment date and a positive, finite amount. Duplicate imported rows
    sharing ticker/date/amount are counted once, matching dashboard reconciliation.
    """
    today = as_of or datetime.now(TZ).date()
    try:
        received = float(received_total or 0)
    except (TypeError, ValueError):
        received = 0.0
    if not math.isfinite(received) or received < 0:
        received = 0.0

    try:
        horizon = date(today.year + 1, today.month, today.day)
    except ValueError:  # 29 de fevereiro em ano não bissexto.
        horizon = date(today.year + 1, today.month, 28)

    upcoming_total = 0.0
    seen = set()
    for raw in upcoming or []:
        row = dict(raw)
        if str(row.get('status') or '').strip().casefold() != 'a receber':
            continue
        if not _is_income_event(row.get('tipo_evento') or row.get('tipo')):
            continue
        payment_date = _api_date(row.get('data_pagamento'))
        if not payment_date:
            continue
        try:
            expected_on = date.fromisoformat(payment_date[:10])
        except ValueError:
            continue
        if not today <= expected_on <= horizon:
            continue
        try:
            amount = float(row.get('valor_total') or 0)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(amount) or amount <= 0:
            continue
        ticker = str(row.get('ticker') or '').strip().upper()
        event_type = _b3_normalize(row.get('tipo_evento') or row.get('tipo') or 'provento')
        fingerprint = (ticker, expected_on.isoformat(), event_type, round(amount, 2))
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        upcoming_total += amount

    return round(received + upcoming_total, 2)


def typesafe_asset_type(ticker):
    return _asset_type_map().get(str(ticker or '').upper(), 'outros')


def received_proventos_by_ticker(active_tickers=None):
    # O resumo considera somente ativos com posição atual; o histórico bruto permanece no banco.
    active = sorted({str(t).upper() for t in active_tickers}) if active_tickers is not None else []
    if active_tickers is None:
        candidates = {str(row['ticker']).upper() for row in db().execute('SELECT DISTINCT ticker FROM operacoes').fetchall()}
        base_date = snapshot_date_for()
        if base_date:
            candidates.update(str(row['ticker']).upper() for row in db().execute(
                'SELECT ticker FROM saldos_importados WHERE data_base=?', (base_date,)
            ).fetchall())
        candidates.update(str(row['ticker']).upper() for row in db().execute(
            "SELECT DISTINCT s.ticker FROM pluggy_position_snapshots s JOIN pluggy_items i ON i.item_id=s.item_id WHERE i.active=1"
        ).fetchall())
        active = sorted(ticker for ticker in candidates if posicao(ticker)[0] > 1e-8 or db().execute(
            "SELECT COALESCE(SUM(s.quantity),0) FROM pluggy_position_snapshots s JOIN pluggy_items i ON i.item_id=s.item_id WHERE i.active=1 AND UPPER(s.ticker)=?",
            (ticker,)
        ).fetchone()[0] > 1e-8)
    totals = {}
    for row in _received_proventos_records(active):
        if not _is_income_event(row.get('tipo_evento')):
            continue
        ticker = row['ticker']
        totals[ticker] = totals.get(ticker, 0.0) + float(row.get('valor_total') or 0)
    return totals


def _historical_points(raw_points):
    """Normaliza fechamentos de mercado sem adjustedClose, que pode incorporar proventos."""
    parsed = []
    for point in raw_points or []:
        try:
            raw_date = point.get('date')
            if isinstance(raw_date, (int, float)):
                price_date = datetime.fromtimestamp(raw_date, TZ).date()
            else:
                price_date = date.fromisoformat(str(raw_date)[:10])
            close = float(point.get('close') or 0)
            # adjustedClose pode descontar/reinvestir dividendos e JCP.
            # A evolução patrimonial solicitada mede somente variação de preço.
            if close > 0:
                parsed.append({'data': price_date, 'close': close})
        except (TypeError, ValueError, OSError):
            continue
    parsed.sort(key=lambda item: item['data'])
    if not parsed:
        return []

    output = []
    for point in parsed:
        if point['close'] > 0:
            output.append({'data': point['data'], 'preco': point['close'], 'fechamento': point['close'], 'ajustado': False})
    return output


def _parse_historical_payload(payload, response_key='results'):
    """Aceita o envelope v2 de ações, o envelope de FIIs e o endpoint legado v1."""
    output = {}
    rows = payload.get(response_key) or []
    for result in rows:
        symbol = str(result.get('requestedSymbol') or result.get('symbol') or '').strip().upper()
        if not symbol:
            continue
        data = result.get('data') or {}
        raw_points = (result.get('historicalDataPrice') or data.get('historicalDataPrice') or [])
        points = _historical_points(raw_points)
        if points:
            output[symbol] = points
            # Em caso de ticker renomeado, preserva a chave que foi pedida pelo usuário.
            returned = str(result.get('symbol') or '').strip().upper()
            if returned and returned not in output:
                output[returned] = points
    return output


def _request_historical_batch(url, symbols, params, response_key='results', headers=None, timeout=15):
    """Consulta lotes respeitando o máximo de tickers do plano e cacheia a série histórica."""
    if not symbols:
        return {}
    output = {}
    batch_size = BRAPI_MAX_TICKERS_PER_REQUEST
    for offset in range(0, len(symbols), batch_size):
        batch = symbols[offset:offset + batch_size]
        try:
            response = _brapi_get(
                url, params={**params, 'symbols': ','.join(batch)},
                headers=headers or {}, timeout=timeout, cache_ttl=24 * 3600, stale_ttl=30 * 86400
            )
            response.raise_for_status()
            output.update(_parse_historical_payload(response.json(), response_key=response_key))
        except (requests.RequestException, ValueError, TypeError):
            continue
    return output


def _request_legacy_history(symbol, headers):
    """Fallback compatível com o endpoint v1, consultado por ticker para isolar erros."""
    for history_range in ('max', '1y'):
        try:
            response = _brapi_get(
                f'https://brapi.dev/api/quote/{symbol}',
                params={'range': history_range, 'interval': '1d'},
                headers=headers, timeout=12, cache_ttl=24 * 3600, stale_ttl=30 * 86400
            )
            response.raise_for_status()
            payload = response.json()
            result = next(iter(payload.get('results') or []), None)
            if not result:
                continue
            raw_points = result.get('historicalDataPrice') or (result.get('data') or {}).get('historicalDataPrice') or []
            points = _historical_points(raw_points)
            if points:
                return points
        except (requests.RequestException, ValueError, TypeError):
            continue
    return []


def fetch_portfolio_price_history(operations, extra_positions=None):
    """Busca preços históricos por classe e mantém séries parciais quando algum ativo não tem cobertura."""
    asset_types = {}
    first_dates = []
    for operation in operations:
        symbol = str(operation.get('ticker') or '').strip().upper()
        if not symbol:
            continue
        asset_types[symbol] = str(operation.get('tipo') or '').strip().lower()
        try:
            first_dates.append(date.fromisoformat(str(operation.get('data') or '')[:10]))
        except (TypeError, ValueError):
            pass
    for position in extra_positions or []:
        symbol = str(position.get('ticker') or '').strip().upper()
        if symbol:
            asset_types.setdefault(symbol, str(position.get('tipo') or '').strip().lower())
            try:
                first_dates.append(date.fromisoformat(str(position.get('data_base') or position.get('pluggy_snapshot_at') or '')[:10]))
            except (TypeError, ValueError):
                pass

    symbols = sorted(asset_types)
    if not symbols:
        return {}
    coverage_start = min(first_dates).isoformat() if first_dates else ''
    # A carteira pode ganhar operações mais antigas após uma importação B3/Pluggy.
    # Incluir a menor data no cache evita reutilizar uma série FII que começou
    # depois da nova primeira operação e parecia completa por engano.
    cache_key = ('price-only-v3', coverage_start) + tuple(sorted((symbol, asset_types[symbol]) for symbol in symbols))
    now = time.time()
    cached = PORTFOLIO_HISTORY_CACHE.get(cache_key)
    if cached:
        ttl = PORTFOLIO_HISTORY_TTL if cached.get('data') else PORTFOLIO_HISTORY_EMPTY_TTL
        if now - cached['time'] < ttl:
            return cached['data']

    headers = {'Authorization': f'Bearer {_brapi_token()}'} if _brapi_token() else {}
    response_data = {}

    # Ações, BDRs e ETFs negociados em bolsa usam o histórico de ações da API v2.
    stock_types = {'acao', 'ações', 'stock', 'stocks', 'bdr', 'etf', 'etfs', 'etf_internacional', 'internacional', 'unit', 'indice'}
    stock_symbols = [symbol for symbol in symbols if asset_types[symbol] in stock_types]
    if stock_symbols:
        stock_url = 'https://brapi.dev/api/v2/stocks/historical'
        for range_value in ('max', '1y'):
            missing_stock_symbols = [symbol for symbol in stock_symbols if symbol not in response_data]
            for offset in range(0, len(missing_stock_symbols), 20):
                batch = missing_stock_symbols[offset:offset + 20]
                found = _request_historical_batch(
                    stock_url, batch,
                    {'range': range_value, 'interval': '1d', 'sortOrder': 'asc'},
                    headers=headers
                )
                response_data.update({symbol: [{**point, 'fonte': 'brapi · histórico de ações'} for point in points] for symbol, points in found.items()})
            # Não transformar uma resposta parcial em dezenas de chamadas
            # individuais no carregamento do dashboard. Os tickers sem série
            # permanecem sinalizados como histórico incompleto e podem ser
            # reconsultados em uma atualização posterior após expirar o cache.
            if all(symbol in response_data for symbol in stock_symbols):
                break

    # FIIs têm endpoint dedicado. A janela é explícita para cobrir desde a primeira compra.
    fii_types = {'fii', 'fiis', 'fundo_imobiliario', 'fundos_imobiliarios'}
    fii_symbols = [symbol for symbol in symbols if asset_types[symbol] in fii_types]
    if fii_symbols:
        start_date = min(first_dates).isoformat() if first_dates else (datetime.now(TZ).date() - timedelta(days=365)).isoformat()
        fii_url = 'https://brapi.dev/api/v2/fii/historical'
        fii_params = {'startDate': start_date, 'endDate': datetime.now(TZ).date().isoformat(), 'sortOrder': 'asc'}
        for offset in range(0, len(fii_symbols), 20):
            batch = fii_symbols[offset:offset + 20]
            fii_found = _request_historical_batch(
                fii_url, batch, fii_params, response_key='fiis', headers=headers
            )
            response_data.update({symbol: [{**point, 'fonte': 'brapi · histórico de FIIs'} for point in points] for symbol, points in fii_found.items()})
        # Tickers ausentes permanecem identificados como histórico incompleto.
        # Evita chamadas individuais em cascata quando a resposta em lote é parcial.

    # Classes sem endpoint histórico específico (ex.: renda fixa cadastrada manualmente)
    # não podem bloquear as séries de ativos negociados em bolsa. Para classes não reconhecidas,
    # tenta-se o endpoint legado individualmente.
    fallback_symbols = [
        symbol for symbol in symbols
        if symbol not in response_data and asset_types[symbol] not in {'renda_fixa', 'tesouro', 'tesouro_direto', 'fundos', 'fundos_de_investimento'}
    ]
    for symbol in fallback_symbols:
        points = _request_legacy_history(symbol, headers)
        if points:
            response_data[symbol] = [{**point, 'fonte':'brapi · histórico legado'} for point in points]

    # Respostas vazias ficam em cache por apenas 2 minutos; séries válidas, por 30 minutos.
    PORTFOLIO_HISTORY_CACHE[cache_key] = {'time': now, 'data': response_data}
    return response_data

def _market_cost_basis_at_date(executed, snapshot_map, base_day, cutoff, market_tickers, include_details=False):
    """Reconstrói o custo médio de ativos negociados em bolsa sem inventar custos.

    Quando o histórico anterior a uma venda está incompleto, o ticker é marcado
    como custo desconhecido. O preço de venda nunca é usado como substituto do
    preço de aquisição. include_details retorna o custo por ticker e a lista de
    posições cujo custo não pôde ser comprovado.
    """
    market_tickers = set(market_tickers or ())
    if not market_tickers:
        return {'basis': 0.0, 'basis_by_ticker': {}, 'unknown_tickers': []} if include_details else 0.0

    def average_cost_before(target):
        quantity = 0.0
        basis = 0.0
        incomplete = False
        target_key = (target['_date'], int(target.get('id') or 0))
        target_qty = max(0.0, float(target.get('_quantity') or 0))
        for previous in executed:
            if (previous['_date'], int(previous.get('id') or 0)) >= target_key:
                break
            if previous['_ticker'] != target['_ticker']:
                continue
            qty = max(0.0, float(previous.get('_quantity') or 0))
            if previous.get('lado') == 'compra':
                quantity += qty
                basis += qty * max(0.0, float(previous.get('_price') or 0)) + max(0.0, float(previous.get('_costs') or 0))
            elif quantity > 1e-8:
                sold = min(quantity, qty)
                average = basis / quantity if quantity > 1e-8 else 0.0
                quantity -= sold
                basis = max(0.0, basis - average * sold)
                if qty - sold > 1e-8:
                    incomplete = True
            else:
                incomplete = True
        if incomplete or quantity + 1e-8 < target_qty or quantity <= 1e-8 or basis <= 0:
            return None
        return basis / quantity

    positions = {}
    snapshot_map = snapshot_map or {}
    for ticker in market_tickers:
        base = snapshot_map.get(ticker)
        quantity = max(0.0, float(base.get('quantidade') or 0)) if base and base_day else 0.0
        basis = quantity * max(0.0, float(base.get('preco_medio') or 0)) if base and base_day else 0.0
        positions[ticker] = {'quantity': quantity, 'basis': basis, 'unknown_basis': bool(quantity > 1e-8 and basis <= 0)}

    if base_day and cutoff >= base_day:
        for op in executed:
            if not (base_day < op['_date'] <= cutoff) or op['_ticker'] not in market_tickers:
                continue
            position = positions[op['_ticker']]
            qty = max(0.0, float(op.get('_quantity') or 0))
            amount = qty * max(0.0, float(op.get('_price') or 0)) + max(0.0, float(op.get('_costs') or 0))
            if op.get('lado') == 'compra':
                position['quantity'] += qty
                position['basis'] += amount
            else:
                available = position['quantity']
                sold = min(available, qty)
                if sold > 1e-8:
                    average = position['basis'] / available if available > 1e-8 else 0.0
                    position['basis'] = max(0.0, position['basis'] - average * sold)
                    position['quantity'] = max(0.0, available - sold)
                if qty - sold > 1e-8:
                    position['unknown_basis'] = True
    elif base_day:
        for op in reversed(executed):
            if not (cutoff < op['_date'] <= base_day) or op['_ticker'] not in market_tickers:
                continue
            position = positions[op['_ticker']]
            qty = max(0.0, float(op.get('_quantity') or 0))
            amount = qty * max(0.0, float(op.get('_price') or 0)) + max(0.0, float(op.get('_costs') or 0))
            if op.get('lado') == 'compra':
                available = position['quantity']
                if available + 1e-8 < qty:
                    position['unknown_basis'] = True
                removable = min(available, qty)
                # Reverter uma compra significa retirar o custo real daquela
                # aquisição, não o custo médio posterior da carteira. Usar a
                # média atual aqui distorce o custo anterior quando o preço da
                # compra é diferente da média acumulada.
                purchase_unit_cost = (amount / qty) if qty > 1e-8 else 0.0
                removed_basis = purchase_unit_cost * removable
                position['basis'] = max(0.0, position['basis'] - removed_basis)
                position['quantity'] = max(0.0, available - removable)
            else:
                average = (position['basis'] / position['quantity']) if position['quantity'] > 1e-8 and position['basis'] > 0 else average_cost_before(op)
                if average is None:
                    position['unknown_basis'] = True
                    average = 0.0
                position['quantity'] += qty
                position['basis'] += qty * average
    else:
        for op in executed:
            if op['_date'] > cutoff:
                break
            if op['_ticker'] not in market_tickers:
                continue
            position = positions[op['_ticker']]
            qty = max(0.0, float(op.get('_quantity') or 0))
            amount = qty * max(0.0, float(op.get('_price') or 0)) + max(0.0, float(op.get('_costs') or 0))
            if op.get('lado') == 'compra':
                position['quantity'] += qty
                position['basis'] += amount
            else:
                available = position['quantity']
                sold = min(available, qty)
                if sold > 1e-8:
                    average = position['basis'] / available if available > 1e-8 else 0.0
                    position['basis'] = max(0.0, position['basis'] - average * sold)
                    position['quantity'] = max(0.0, available - sold)
                if qty - sold > 1e-8:
                    position['unknown_basis'] = True

    basis_by_ticker = {
        ticker: max(0.0, float(position['basis']))
        for ticker, position in positions.items()
    }
    unknown_tickers = sorted(
        ticker for ticker, position in positions.items()
        if position.get('unknown_basis')
    )
    total = sum(basis_by_ticker.values())
    if include_details:
        return {'basis': total, 'basis_by_ticker': basis_by_ticker, 'unknown_tickers': unknown_tickers}
    return total


def build_portfolio_performance(operations, dividends, history=None, imported_positions=None, snapshot_date=None, current_positions=None):
    """Reconstrói patrimônio e ganho/perda de capital por mês usando fechamentos não ajustados.

    Compras e vendas são fluxos externos (Modified Dietz). Dividendos, JCP e demais
    proventos não entram na rentabilidade nem no ganho de capital desta série.
    Ativos sem histórico são sinalizados e não devem receber retorno inventado.
    """
    imported_positions = [dict(row) for row in (imported_positions or [])]
    snapshot_date = _api_date(snapshot_date) if snapshot_date else ''
    all_types = {
        str(op.get('ticker') or '').upper(): str(op.get('tipo') or '').strip().lower()
        for op in operations if op.get('ticker')
    }
    all_types.update({
        str(row.get('ticker') or '').upper(): str(row.get('tipo') or '').strip().lower()
        for row in imported_positions if row.get('ticker')
    })
    current_positions = [dict(row) for row in (current_positions or [])]
    current_map = {str(row.get('ticker') or '').upper(): row for row in current_positions if row.get('ticker')}
    for row in current_positions:
        ticker = str(row.get('ticker') or '').strip().upper()
        kind = str(row.get('tipo') or '').strip().lower()
        if ticker and kind:
            all_types[ticker] = kind
    pluggy_current_map = {
        ticker: row for ticker, row in current_map.items()
        if row.get('pluggy_snapshot') and row.get('pluggy_snapshot_at')
    }
    non_market_types = set()
    fixed_income_symbols = {symbol for symbol, kind in all_types.items() if kind in {'renda_fixa', 'tesouro', 'tesouro_direto'}}
    all_tickers = sorted(symbol for symbol, kind in all_types.items() if kind not in non_market_types)
    history = history if history is not None else fetch_portfolio_price_history(operations, [*imported_positions, *current_positions])
    normalized = {}
    history_sources = {}
    for ticker, points in (history or {}).items():
        if str(ticker).upper() not in all_types:
            continue
        valid = [
            point for point in points
            if isinstance(point.get('data'), date) and float(point.get('preco') or 0) > 0
        ]
        if valid:
            normalized[str(ticker).upper()] = sorted(valid, key=lambda item: item['data'])
            source_counts = Counter(str(point.get('fonte') or 'Fonte não identificada') for point in valid)
            history_sources[str(ticker).upper()] = {
                'fonte': source_counts.most_common(1)[0][0],
                'pontos': len(valid),
                'inicio': min(point['data'] for point in valid).isoformat(),
                'fim': max(point['data'] for point in valid).isoformat(),
                'ajustado': all(bool(point.get('ajustado')) for point in valid)
            }

    # Ativos sem série histórica também entram na composição patrimonial.
    # Para eles, usamos custo médio como referência nos meses passados e a
    # cotação/valor atual do próprio portfólio no mês corrente.
    historical_tickers = set(normalized) | fixed_income_symbols
    available_tickers = set(all_tickers) | fixed_income_symbols
    # Sinaliza também séries parciais: ter alguma cotação não significa cobrir
    # todo o período desde a primeira compra/saldo-base até a data atual.
    first_date_by_ticker = {}
    for operation in operations:
        ticker = str(operation.get('ticker') or '').strip().upper()
        try:
            operation_date = date.fromisoformat(str(operation.get('data') or '')[:10])
            if ticker:
                first_date_by_ticker[ticker] = min(first_date_by_ticker.get(ticker, operation_date), operation_date)
        except (TypeError, ValueError):
            pass
    for position in [*imported_positions, *pluggy_current_map.values()]:
        ticker = str(position.get('ticker') or '').strip().upper()
        try:
            raw_position_date = position.get('data_base') or position.get('pluggy_snapshot_at') or snapshot_date or ''
            position_date = date.fromisoformat(str(raw_position_date)[:10])
            if ticker:
                first_date_by_ticker[ticker] = min(first_date_by_ticker.get(ticker, position_date), position_date)
        except (TypeError, ValueError):
            pass
    today_for_coverage = datetime.now(TZ).date()
    missing_set = set(all_tickers) - historical_tickers
    for ticker in set(all_tickers) & set(normalized):
        points = normalized[ticker]
        first_required = first_date_by_ticker.get(ticker)
        last_point = points[-1]['data'] if points else None
        if (first_required and points[0]['data'] > first_required) or (last_point and (today_for_coverage - last_point).days > 45):
            missing_set.add(ticker)
    missing_symbols = sorted(missing_set)
    empty = {
        'series_12m': [], 'series_total': [], 'rentabilidade_12m': None,
        'rentabilidade_total': None, 'meses_disponiveis': 0,
        'primeiro_mes': None, 'historico_completo': False,
        'ativos_sem_historico': missing_symbols, 'ativos_custo_desconhecido': [],
        'ativos_com_historico': len(historical_tickers), 'total_ativos': len(all_tickers),
        'fontes_historico': history_sources,
        'historico_parcial': bool(missing_symbols) or bool(snapshot_date)
    }
    snapshot_map = {str(row.get('ticker') or '').upper(): row for row in imported_positions if row.get('ticker')}
    def snapshot_only_result():
        snapshot_cost = sum(float(row.get('quantidade') or 0) * float(row.get('preco_medio') or 0) for row in imported_positions)
        snapshot_value = sum(float(row.get('quantidade') or 0) * float(row.get('preco_base') or 0) for row in imported_positions)
        snapshot_return = (snapshot_value / snapshot_cost - 1) * 100 if snapshot_cost > 0 else None
        if snapshot_return is None or not snapshot_date:
            return empty
        return {
            **empty, 'series_total': [{'mes': snapshot_date[:7], 'valor': round(snapshot_return, 4)}],
            'rentabilidade_total': round(snapshot_return, 2), 'meses_disponiveis': 0,
            'primeiro_mes': snapshot_date[:7], 'resultado_por_saldo_base': True,
            'historico_parcial': True, 'ativos_com_historico': 0, 'total_ativos': len(all_tickers)
        }

    if not normalized and not fixed_income_symbols and not all_tickers:
        return snapshot_only_result()
    if not operations and not snapshot_map and not pluggy_current_map:
        return empty

    def adjusted_operation_values(ticker, trade_date, quantity, price, costs):
        # Compras/vendas permanecem nos preços efetivamente registrados.
        return quantity * price, costs

    executed = []
    for op in operations:
        try:
            op_date = date.fromisoformat(str(op.get('data') or '')[:10])
            ticker = str(op.get('ticker') or '').upper()
            if ticker not in available_tickers:
                continue
            executed.append({
                **op, '_date': op_date, '_ticker': ticker,
                '_quantity': max(0.0, float(op.get('quantidade') or 0)),
                '_price': max(0.0, float(op.get('preco') or 0)),
                '_costs': max(0.0, float(op.get('custos') or 0))
            })
        except (TypeError, ValueError):
            continue
    executed.sort(key=lambda item: (item['_date'], int(item.get('id') or 0)))
    if not executed and not snapshot_map and not pluggy_current_map:
        return snapshot_only_result()
    base_day = date.fromisoformat(snapshot_date) if snapshot_date else None
    if not executed and not base_day and not pluggy_current_map:
        return snapshot_only_result()
    pluggy_anchor_days = []
    for row in pluggy_current_map.values():
        try:
            pluggy_anchor_days.append(date.fromisoformat(str(row.get('pluggy_snapshot_at') or '')[:10]))
        except (TypeError, ValueError):
            pass
    first_operation = min((item['_date'] for item in executed), default=base_day)
    today = datetime.now(TZ).date()
    anchors = [value for value in (first_operation, base_day, *pluggy_anchor_days) if value]
    history_start = min(anchors) if anchors else today
    month_cursor = date(history_start.year, history_start.month, 1)
    month_end = date(today.year, today.month, 1)
    month_keys = []
    while month_cursor <= month_end:
        month_keys.append(month_cursor.strftime('%Y-%m'))
        month_cursor = (month_cursor.replace(day=28) + timedelta(days=4)).replace(day=1)

    def held_quantities(cutoff):
        """Reconstrói posições em torno da fotografia, sem somar o histórico duas vezes."""
        holdings = {}
        if base_day and snapshot_map:
            holdings = {
                ticker: max(0.0, float(row.get('quantidade') or 0))
                for ticker, row in snapshot_map.items()
            }
            if cutoff < base_day:
                # Volta da fotografia para a data consultada, revertendo operações conhecidas.
                for op in executed:
                    if cutoff < op['_date'] <= base_day:
                        current = holdings.get(op['_ticker'], 0.0)
                        if op.get('lado') == 'compra':
                            current -= op['_quantity']
                        else:
                            current += op['_quantity']
                        holdings[op['_ticker']] = current
            else:
                # Avança a partir da fotografia, aplicando somente operações posteriores.
                for op in executed:
                    if base_day < op['_date'] <= cutoff:
                        current = holdings.get(op['_ticker'], 0.0)
                        if op.get('lado') == 'compra':
                            current += op['_quantity']
                        else:
                            current -= op['_quantity']
                        holdings[op['_ticker']] = current
        else:
            for op in executed:
                if op['_date'] > cutoff:
                    break
                current = holdings.get(op['_ticker'], 0.0)
                current += op['_quantity'] if op.get('lado') == 'compra' else -op['_quantity']
                holdings[op['_ticker']] = current
        for ticker, row in pluggy_current_map.items():
            try:
                anchor = date.fromisoformat(str(row.get('pluggy_snapshot_at') or '')[:10])
            except (TypeError, ValueError):
                continue
            if cutoff >= anchor:
                holdings[ticker] = max(0.0, float(row.get('quantidade') or 0))
        return {ticker: max(0.0, qty) for ticker, qty in holdings.items() if qty > 1e-8}

    def fixed_income_value(ticker, cutoff):
        """Reconstrói os lotes de renda fixa e aplica o indexador até o fechamento mensal."""
        pluggy_row = pluggy_current_map.get(ticker)
        if pluggy_row:
            try:
                pluggy_date = date.fromisoformat(str(pluggy_row.get('pluggy_snapshot_at') or '')[:10])
            except (TypeError, ValueError):
                pluggy_date = None
            if pluggy_date and cutoff >= pluggy_date:
                return max(0.0, float(pluggy_row.get('valor_atual') or 0))
        lots = []
        if base_day and snapshot_map and cutoff >= base_day:
            base = snapshot_map.get(ticker)
            if base:
                principal = float(base.get('quantidade') or 0) * float(base.get('preco_medio') or 0)
                if principal > 0:
                    lots.append({'principal': principal, 'date': base_day.isoformat(), 'details': {}, 'legacy_value': float(base.get('quantidade') or 0) * float(base.get('preco_base') or 0)})
            relevant = [op for op in executed if base_day < op['_date'] <= cutoff and op['_ticker'] == ticker]
        else:
            relevant = [op for op in executed if op['_date'] <= cutoff and op['_ticker'] == ticker]
        for op in relevant:
            amount = op['_quantity'] * op['_price']
            if op.get('lado') == 'compra':
                try:
                    details = json.loads(op.get('detalhes') or '{}') if isinstance(op.get('detalhes'), str) else (op.get('detalhes') or {})
                except (TypeError, ValueError):
                    details = {}
                lots.append({'principal': amount + op['_costs'], 'date': op['_date'].isoformat(), 'details': details})
            else:
                remaining = amount
                for lot in lots:
                    if remaining <= 0:
                        break
                    reduction = min(lot['principal'], remaining)
                    lot['principal'] -= reduction
                    remaining -= reduction
        total = 0.0
        for lot in lots:
            principal = max(0.0, float(lot['principal']))
            if principal <= 0:
                continue
            factor, _warning = _fixed_income_factor(lot['details'], lot['date'], cutoff.isoformat())
            total += principal * factor if factor is not None else float(lot.get('legacy_value') or principal)
        return total if lots else 0.0

    portfolio_value_cache = {}

    def fallback_cost_value(ticker, cutoff, quantity):
        """Estima apenas o valor patrimonial de ativos sem cotação histórica pelo custo médio.

        Não cria ganho fictício: o resultado desses ativos fica zerado até existir
        uma cotação histórica confiável. O mês corrente usa o valor atual do portfólio.
        """
        if cutoff >= today:
            current = current_map.get(ticker) or {}
            try:
                current_value = float(current.get('valor_atual') or 0)
                if current_value > 0:
                    return current_value
            except (TypeError, ValueError):
                pass

        qty = 0.0
        cost = 0.0
        base = snapshot_map.get(ticker)
        if base and base_day and cutoff >= base_day:
            qty = max(0.0, float(base.get('quantidade') or 0))
            cost = qty * max(0.0, float(base.get('preco_medio') or 0))
            relevant_ops = [op for op in executed if op['_ticker'] == ticker and base_day < op['_date'] <= cutoff]
        else:
            relevant_ops = [op for op in executed if op['_ticker'] == ticker and op['_date'] <= cutoff]

        for op in relevant_ops:
            amount = op['_quantity'] * op['_price']
            if op.get('lado') == 'compra':
                qty += op['_quantity']
                cost += amount + op['_costs']
            elif qty > 1e-8:
                sold = min(qty, op['_quantity'])
                average = cost / qty
                qty -= sold
                cost -= average * sold
        if qty <= 1e-8:
            # Para saldos-base anteriores à primeira operação conhecida, conserva
            # o custo médio do saldo-base em vez de apagar a posição do gráfico.
            if base and quantity > 0:
                return quantity * max(0.0, float(base.get('preco_medio') or 0))
            return 0.0
        return max(0.0, cost)

    def portfolio_value(cutoff):
        cache_key = cutoff.isoformat()
        if cache_key in portfolio_value_cache:
            return portfolio_value_cache[cache_key]
        holdings = held_quantities(cutoff)
        total = 0.0
        priced_positions = 0
        for ticker, qty in holdings.items():
            if ticker in fixed_income_symbols:
                fixed_value = fixed_income_value(ticker, cutoff)
                if fixed_value > 0:
                    total += fixed_value
                    priced_positions += 1
                continue
            points = normalized.get(ticker) or []
            previous = next((point for point in reversed(points) if point['data'] <= cutoff), None)
            price = float(previous['preco']) if previous and (cutoff - previous['data']).days <= 45 else 0.0
            if price > 0:
                total += qty * price
            else:
                # Não excluir a posição inteira por falta de histórico. O custo
                # médio mantém o patrimônio aplicado visível sem inventar retorno.
                total += fallback_cost_value(ticker, cutoff, qty)
            priced_positions += 1
        if holdings and not priced_positions:
            portfolio_value_cache[cache_key] = None
            return None
        portfolio_value_cache[cache_key] = total
        return total

    # O argumento dividends é mantido por compatibilidade da chamada, mas
    # proventos são deliberadamente excluídos da evolução e da rentabilidade.

    monthly_returns = []
    portfolio_series = []
    def net_applied_through(cutoff, after=None, ticker_filter=None):
        """Mantém o critério de fluxo líquido para renda fixa e compatibilidade legada."""
        allowed = set(ticker_filter) if ticker_filter is not None else None
        total = 0.0
        if base_day and after is not None and cutoff >= base_day:
            total = sum(
                float(row.get('quantidade') or 0) * float(row.get('preco_medio') or 0)
                for ticker, row in snapshot_map.items()
                if allowed is None or ticker in allowed
            )
        for op in executed:
            if op['_date'] > cutoff:
                break
            if after is not None and op['_date'] <= after:
                continue
            if allowed is not None and op['_ticker'] not in allowed:
                continue
            trade_notional, adjusted_costs = adjusted_operation_values(
                op['_ticker'], op['_date'], op['_quantity'], op['_price'], op['_costs']
            )
            total += (trade_notional + adjusted_costs) if op.get('lado') == 'compra' else -(trade_notional - adjusted_costs)
        return total

    def market_cost_basis_through(cutoff):
        return _market_cost_basis_at_date(
            executed, snapshot_map, base_day, cutoff,
            set(all_tickers) - fixed_income_symbols, include_details=True
        )

    def _neutral_value_for_unknown_cost(ticker, quantity, cutoff):
        """Use market value as a zero-return reference when acquisition cost is unknowable."""
        if quantity <= 1e-8:
            return 0.0
        points = normalized.get(ticker) or []
        previous = next((point for point in reversed(points) if point['data'] <= cutoff), None)
        if previous and (cutoff - previous['data']).days <= 45:
            return max(0.0, quantity * float(previous['preco']))
        if cutoff >= today:
            row = current_map.get(ticker) or {}
            try:
                current_value = float(row.get('valor_atual') or 0)
                if current_value > 0:
                    return current_value
            except (TypeError, ValueError):
                pass
        # Sem cotação histórica, usa exatamente o mesmo valor de referência
        # empregado em portfolio_value(), evitando ganho fictício por base zero.
        return max(0.0, fallback_cost_value(ticker, cutoff, quantity))

    def applied_capital_through(cutoff):
        """Combina custo médio, âncoras Pluggy e regra preservada de renda fixa."""
        fixed_income_applied = net_applied_through(
            cutoff,
            after=base_day if base_day and cutoff >= base_day else None,
            ticker_filter=fixed_income_symbols
        )
        details = market_cost_basis_through(cutoff)
        basis_by_ticker = dict(details['basis_by_ticker'])
        unknown = set(details['unknown_tickers'])
        holdings = held_quantities(cutoff)
        # A fotografia Pluggy é autoritativa a partir da própria data de captura.
        for ticker, row in pluggy_current_map.items():
            try:
                anchor = date.fromisoformat(str(row.get('pluggy_snapshot_at') or '')[:10])
            except (TypeError, ValueError):
                continue
            if cutoff >= anchor:
                basis_by_ticker[ticker] = max(0.0, float(row.get('investido') or 0))
                unknown.discard(ticker)
        # Sem custo de aquisição comprovável, não se usa preço de venda nem se
        # fabrica ganho: a referência fica neutra pelo valor de mercado e é sinalizada.
        for ticker in sorted(unknown):
            basis_by_ticker[ticker] = _neutral_value_for_unknown_cost(
                ticker, holdings.get(ticker, 0.0), cutoff
            )
        return sum(max(0.0, value) for value in basis_by_ticker.values()) + fixed_income_applied, sorted(unknown)

    first_year, first_month = int(month_keys[0][:4]), int(month_keys[0][5:7])
    previous_end = date(first_year, first_month, 1) - timedelta(days=1)
    for key in month_keys:
        year, month = int(key[:4]), int(key[5:7])
        next_month = (date(year, month, 28) + timedelta(days=4)).replace(day=1)
        last_day = next_month - timedelta(days=1)
        end_date = min(last_day, today)
        start_date = previous_end
        start_value = portfolio_value(start_date)
        end_value = portfolio_value(end_date)
        if end_value is not None:
            applied_value, unknown_cost_tickers = applied_capital_through(end_date)
            portfolio_series.append({
                'mes': key,
                'aplicado': round(applied_value, 2),
                'patrimonio': round(end_value, 2),
                'ganho': round(end_value - applied_value, 2),
                'capital_incompleto': bool(unknown_cost_tickers),
                'ativos_custo_desconhecido': unknown_cost_tickers
            })
        if end_value is None:
            previous_end = end_date
            continue
        if start_value is None:
            if not held_quantities(start_date):
                start_value = 0.0
            else:
                previous_end = end_date
                continue

        period_days = max(1, (end_date - start_date).days)
        signed_flows = 0.0
        weighted_flows = 0.0
        for op in executed:
            if not (start_date < op['_date'] <= end_date):
                continue
            trade_notional, adjusted_costs = adjusted_operation_values(
                op['_ticker'], op['_date'], op['_quantity'], op['_price'], op['_costs']
            )
            flow = (trade_notional + adjusted_costs) if op.get('lado') == 'compra' else -(trade_notional - adjusted_costs)
            weight = max(0.0, min(1.0, (end_date - op['_date']).days / period_days))
            signed_flows += flow
            weighted_flows += flow * weight

        denominator = start_value + weighted_flows
        if denominator > 1e-8:
            # Retorno exclusivamente de preço: proventos ficam fora deste cálculo.
            month_result = end_value - start_value - signed_flows
            month_return = month_result / denominator
            if -0.95 < month_return < 5:
                monthly_returns.append({'mes': key, 'retorno': month_return,
                                'resultado': month_result})
        previous_end = end_date

    roi_total_series = []
    for point in portfolio_series:
        invested = float(point.get('aplicado') or 0)
        gain = float(point.get('ganho') or 0)
        if invested > 1e-8:
            roi_total_series.append({
                'mes': point['mes'],
                'valor': round(gain / invested * 100, 4),
                'ganho_prejuizo_mes': round(gain, 2)
            })
    roi_total = roi_total_series[-1]['valor'] if roi_total_series else None

    if not monthly_returns:
        # Mesmo sem retorno mensal calculável, a série total usa exatamente
        # patrimônio menos capital líquido aplicado em cada fechamento.
        return {
            **snapshot_only_result(),
            'series_patrimonio': portfolio_series,
            'ativos_custo_desconhecido': sorted({ticker for point in portfolio_series for ticker in point.get('ativos_custo_desconhecido', [])}),
            'series_roi_total': roi_total_series,
            'rentabilidade_total': round(roi_total, 2) if roi_total is not None else None,
            'series_12m': [], 'rentabilidade_12m': None,
            'historico_parcial': True
        }

    total_factor = 1.0
    series_total = []
    for row in monthly_returns:
        factor = 1.0 + row['retorno']
        total_factor *= factor
        point = {'mes': row['mes'], 'valor': round((total_factor - 1.0) * 100, 4),
                 'ganho_prejuizo_mes': round(float(row.get('resultado') or 0), 2)}
        series_total.append(point)

    # Regra do card: média aritmética dos percentuais mensais de ganho de capital,
    # usando até 12 meses calculáveis. Meses sem histórico não entram como zero.
    series_12m, return_12m_average, months_in_average = _rolling_monthly_arithmetic_average(monthly_returns, 12)
    return {
        **empty,
        'series_12m': series_12m,
        'series_total': series_total,
        'series_roi_total': roi_total_series,
        'series_patrimonio': portfolio_series,
        'ativos_custo_desconhecido': sorted({ticker for point in portfolio_series for ticker in point.get('ativos_custo_desconhecido', [])}),
        'rentabilidade_12m': return_12m_average,
        'meses_base_12m': months_in_average,
        'rentabilidade_total': round(roi_total, 2) if roi_total is not None else None,
        'meses_disponiveis': len(monthly_returns),
        'primeiro_mes': monthly_returns[0]['mes'],
        'historico_completo': len(monthly_returns) >= 12 and not missing_symbols and not snapshot_date,
        'resultado_por_saldo_base': False,
        'historico_parcial': bool(missing_symbols) or bool(snapshot_date)
    }



def _benchmark_monthly_from_yahoo(symbol, start_date, end_date):
    """Obtém fechamentos mensais do Yahoo Finance; retorna None quando o símbolo não tem cobertura."""
    start = date.fromisoformat(str(start_date)[:10]) - timedelta(days=45)
    end = date.fromisoformat(str(end_date)[:10]) + timedelta(days=2)
    period1 = int(datetime.combine(start, clock_time.min, TZ).timestamp())
    period2 = int(datetime.combine(end, clock_time.min, TZ).timestamp())
    url = f'https://query1.finance.yahoo.com/v8/finance/chart/{requests.utils.quote(symbol, safe="")}'
    response = _provider_get(
        'yahoo', url,
        params={'period1': period1, 'period2': period2, 'interval': '1mo', 'events': 'div,splits'},
        headers={'User-Agent': USER_AGENT}, timeout=15, cache_ttl=24 * 3600, stale_ttl=30 * 86400
    )
    response.raise_for_status()
    payload = response.json()
    result = next(iter(payload.get('chart', {}).get('result') or []), None)
    if not result:
        return None
    timestamps = result.get('timestamp') or []
    indicators = result.get('indicators') or {}
    quote_rows = (indicators.get('quote') or [{}])[0]
    adjusted_rows = (indicators.get('adjclose') or [{}])[0]
    closes = adjusted_rows.get('adjclose') or quote_rows.get('close') or []
    points = []
    for stamp, close in zip(timestamps, closes):
        try:
            if close is None or float(close) <= 0:
                continue
            month_key = datetime.fromtimestamp(int(stamp), ZoneInfo('UTC')).strftime('%Y-%m')
            points.append((month_key, float(close)))
        except (TypeError, ValueError, OSError):
            continue
    # Yahoo retorna pontos de fechamento mensal. O primeiro ponto é a base 0%.
    by_month = {}
    for month_key, close in points:
        by_month[month_key] = close
    ordered = sorted(by_month.items())
    if len(ordered) < 2:
        return None
    base = ordered[0][1]
    return [{'mes': key, 'valor': round((value / base - 1) * 100, 4)} for key, value in ordered if base > 0]


def _benchmark_monthly_bcb(code, start_date, end_date):
    """Calcula CDI/IPCA acumulados a partir das séries oficiais SGS do Banco Central."""
    if code == 'CDI':
        points = _sgs_series(12, start_date, end_date)
        monthly_factors = {}
        for point_date, daily_pct in points:
            key = point_date.strftime('%Y-%m')
            monthly_factors[key] = monthly_factors.get(key, 1.0) * (1 + float(daily_pct) / 100.0)
        monthly_returns = {key: (factor - 1) * 100 for key, factor in monthly_factors.items()}
    elif code == 'IPCA':
        points = _sgs_series(433, start_date, end_date)
        monthly_returns = {point_date.strftime('%Y-%m'): float(value) for point_date, value in points}
    else:
        return None
    cumulative = 1.0
    output = []
    for key in sorted(monthly_returns):
        cumulative *= 1 + monthly_returns[key] / 100.0
        output.append({'mes': key, 'valor': round((cumulative - 1) * 100, 4)})
    return output if len(output) >= 1 else None


def _benchmark_monthly_hg(symbol, start_date, end_date, token):
    """Histórico mensal HG Brasil. A API documenta BVSP e IFIX no endpoint histórico."""
    response = _provider_get(
        'hgbrasil', 'https://api.hgbrasil.com/v2/finance/historical',
        params={'key':token,'symbols':symbol,'start_date':date.fromisoformat(str(start_date)[:10]).isoformat(),
                'end_date':date.fromisoformat(str(end_date)[:10]).isoformat(),'sample_by':'1M'},
        timeout=20, cache_ttl=24 * 3600, stale_ttl=30 * 86400
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get('valid_key') is False:
        raise ValueError('Chave HG Brasil inválida.')
    rows = (payload.get('results') or {}).get(symbol.upper()) or (payload.get('results') or {}).get(symbol.lower()) or {}
    by_month = {}
    for timestamp, values in rows.items():
        try:
            month_key = str(timestamp)[:7]
            close = float((values or {}).get('close') or 0)
            if close > 0:
                by_month[month_key] = close
        except (TypeError, ValueError, AttributeError):
            continue
    ordered = sorted(by_month.items())
    if len(ordered) < 2:
        return None
    base = ordered[0][1]
    return [{'mes':key,'valor':round((value/base-1)*100,4)} for key,value in ordered if base>0]


def _benchmark_monthly_brapi(symbol, start_date, end_date, token):
    """Histórico mensal brapi; aceita apenas uma resposta real do endpoint histórico."""
    response = _brapi_get(
        'https://brapi.dev/api/v2/stocks/historical',
        params={'symbols':symbol,'startDate':date.fromisoformat(str(start_date)[:10]).isoformat(),
                'endDate':date.fromisoformat(str(end_date)[:10]).isoformat(),'interval':'1mo','sortOrder':'asc'},
        headers={'Authorization':f'Bearer {token}'}, timeout=20, cache_ttl=24 * 3600, stale_ttl=30 * 86400
    )
    response.raise_for_status()
    found = _parse_historical_payload(response.json())
    points = found.get(symbol.upper()) or []
    by_month = {}
    for point in points:
        price = float(point.get('preco') or 0)
        if price > 0:
            by_month[point['data'].strftime('%Y-%m')] = price
    ordered = sorted(by_month.items())
    if len(ordered) < 2:
        return None
    base = ordered[0][1]
    return [{'mes':key,'valor':round((value/base-1)*100,4)} for key,value in ordered if base>0]



def _benchmark_monthly_ibge_ipca(start_date, end_date):
    """Consulta de conferência do IPCA no SIDRA/IBGE (tabela 1737, variável 63)."""
    start = date.fromisoformat(str(start_date)[:10])
    end = date.fromisoformat(str(end_date)[:10])
    first_period = start.year * 100 + start.month
    last_period = end.year * 100 + end.month
    response = requests.get(
        f'https://apisidra.ibge.gov.br/values/t/1737/n1/1/v/63/p/{first_period}-{last_period}',
        headers={'User-Agent': USER_AGENT},
        timeout=20
    )
    response.raise_for_status()
    rows = response.json()
    if not isinstance(rows, list) or len(rows) < 2:
        return None
    monthly = {}
    for row in rows[1:]:
        try:
            period = str(row.get('D3C') or row.get('D1C') or '')
            raw_value = str(row.get('V') or '').strip()
            if len(period) != 6 or not period.isdigit() or raw_value in ('', '-', '...', 'X'):
                continue
            monthly[f'{period[:4]}-{period[4:6]}'] = float(raw_value.replace(',', '.'))
        except (TypeError, ValueError, AttributeError):
            continue
    factor = 1.0
    output = []
    for key in sorted(monthly):
        factor *= 1 + monthly[key] / 100.0
        output.append({'mes': key, 'valor': round((factor - 1) * 100, 4)})
    return output or None


def _benchmark_monthly_prices(points):
    """Converte fechamentos diários em fechamento mensal e retorno acumulado."""
    by_month = {}
    for point_date, close in points:
        try:
            if not isinstance(point_date, date):
                point_date = date.fromisoformat(str(point_date)[:10])
            price = float(close)
            if price > 0:
                by_month[point_date.strftime('%Y-%m')] = price
        except (TypeError, ValueError):
            continue
    ordered = sorted(by_month.items())
    if len(ordered) < 2:
        return None
    base = ordered[0][1]
    if base <= 0:
        return None
    return [{'mes': key, 'valor': round((value / base - 1) * 100, 4)} for key, value in ordered]


def _benchmark_monthly_dadosb3(code, start_date, end_date, token=''):
    """Série de fechamento de índice da API Dados B3; usa a chave no cabeçalho X-API-Key."""
    params = {
        'de': date.fromisoformat(str(start_date)[:10]).isoformat(),
        'ate': date.fromisoformat(str(end_date)[:10]).isoformat()
    }
    headers = {'X-API-Key': token} if token else {}
    response = _provider_get(
        'dadosb3', f'https://dadosb3.com/indices/{code}/precos',
        params=params, headers=headers, timeout=20, cache_ttl=24 * 3600, stale_ttl=30 * 86400
    )
    response.raise_for_status()
    payload = response.json()
    rows = payload.get('serie') or []
    if not isinstance(rows, list) or not rows:
        return None
    points = []
    for row in rows:
        try:
            point_date = date.fromisoformat(str(row.get('data') or '')[:10])
            close = float(row.get('fechamento') or 0)
            if close > 0:
                points.append((point_date, close))
        except (TypeError, ValueError, AttributeError):
            continue
    data = _benchmark_monthly_prices(points)
    if not data:
        return None
    return {
        'data': data,
        'sample': bool(payload.get('amostra')),
        'as_of': payload.get('ate') or data[-1]['mes'],
        'note': payload.get('nota') or '',
        'citation': payload.get('fonte') or 'Dados B3'
    }


def _benchmark_latest_month(points):
    return points[-1].get('mes') if points else None



@app.route('/api/benchmarks')
def api_benchmarks():
    """Séries mensais com prioridade de fontes e procedência explícita; sem interpolação."""
    today = datetime.now(TZ).date()
    # A página de Rentabilidade precisa disponibilizar o histórico integral de cada
    # benchmark, independentemente da data da primeira operação da carteira.
    requested_start = date(1990, 1, 1)
    # Busca meses anteriores para calcular o retorno do primeiro mês selecionado.
    start = max(requested_start - timedelta(days=75), date(1990, 1, 1))
    end = today
    cache_key = requested_start.isoformat()
    cached = BENCHMARK_CACHE.get(cache_key)
    if cached and time.time() - cached['time'] < BENCHMARK_CACHE_TTL:
        return jsonify(cached['data'])

    series = {}
    source_map = {}
    metadata = {}
    validation = {}
    unavailable = []

    # CDI/IPCA oficiais do BCB. O IPCA é mensal e só possui competências já divulgadas.
    try:
        cdi_data = _benchmark_monthly_bcb('CDI', start, end)
    except (requests.RequestException, ValueError, TypeError):
        cdi_data = None
    if cdi_data:
        series['CDI'] = cdi_data
        source_map['CDI'] = 'Banco Central do Brasil · SGS 12'
        metadata['CDI'] = {'source': source_map['CDI'], 'last_available': _benchmark_latest_month(cdi_data), 'observations': len(cdi_data)}
    else:
        unavailable.append('CDI')

    try:
        ipca_bcb = _benchmark_monthly_bcb('IPCA', start, end)
    except (requests.RequestException, ValueError, TypeError):
        ipca_bcb = None
    try:
        ipca_ibge = _benchmark_monthly_ibge_ipca(start, end)
    except (requests.RequestException, ValueError, TypeError, KeyError):
        ipca_ibge = None
    if ipca_bcb:
        series['IPCA'] = ipca_bcb
        source_map['IPCA'] = 'Banco Central do Brasil · SGS 433'
    elif ipca_ibge:
        series['IPCA'] = ipca_ibge
        source_map['IPCA'] = 'IBGE · SIDRA tabela 1737, variável 63'
    else:
        unavailable.append('IPCA')
    if series.get('IPCA'):
        metadata['IPCA'] = {
            'source': source_map['IPCA'],
            'last_available': _benchmark_latest_month(series['IPCA']),
            'observations': len(series['IPCA']),
            'note': 'O mês corrente só aparece depois da divulgação oficial do IPCA.'
        }
    if ipca_bcb and ipca_ibge:
        bcb_map = {item['mes']: float(item['valor']) for item in ipca_bcb}
        ibge_map = {item['mes']: float(item['valor']) for item in ipca_ibge}
        overlap = sorted(set(bcb_map).intersection(ibge_map))
        if overlap:
            last_overlap = overlap[-1]
            difference = abs(bcb_map[last_overlap] - ibge_map[last_overlap])
            validation['IPCA'] = {
                'status': 'conferido' if difference <= 0.02 else 'divergencia',
                'mes': last_overlap,
                'diferenca_pontos_percentuais': round(difference, 4),
                'sources': ['BCB SGS 433', 'IBGE SIDRA 1737/63']
            }

    dadosb3_token = _get_api_token('dadosb3')
    hg_token = _get_api_token('hgbrasil')
    brapi_token = _brapi_token()

    yahoo_symbols = {
        'IBOV': ['^BVSP', 'IBOV.SA'],
        'IFIX': ['IFIX.SA', '^IFIX'],
        'SMLL': ['SMLL.SA', '^SMLL'],
        'IDIV': ['IDIV.SA', '^IDIV'],
        'IVVB11': ['IVVB11.SA']
    }
    brapi_symbols = {
        'IBOV': ['^BVSP', 'IBOV'],
        'IFIX': ['IFIX', '^IFIX'],
        'SMLL': ['SMLL', '^SMLL'],
        'IDIV': ['IDIV', '^IDIV']
    }
    hg_symbols = {'IBOV': 'BVSP', 'IFIX': 'IFIX'}

    for code in ('IBOV', 'IFIX', 'SMLL', 'IDIV', 'IVVB11'):
        candidates = []
        sample_candidate = None

        # Dados B3: índice de retorno total, fonte prioritária para os índices nacionais.
        if code in ('IBOV', 'IFIX', 'SMLL', 'IDIV'):
            try:
                result = _benchmark_monthly_dadosb3(code, start, end, dadosb3_token)
                if result:
                    candidate = {
                        'data': result['data'],
                        'source': 'Dados B3 · fechamento de índice de retorno total (dados públicos da B3)',
                        'sample': result['sample'],
                        'note': result.get('note') or '',
                        'citation': result.get('citation') or 'Dados B3'
                    }
                    if result['sample']:
                        sample_candidate = candidate
                    else:
                        candidates.append(candidate)
            except (requests.RequestException, ValueError, TypeError, KeyError):
                pass

        # HG Brasil oferece histórico para IBOV e IFIX; a cobertura depende do plano.
        if code in hg_symbols and hg_token:
            try:
                data = _benchmark_monthly_hg(hg_symbols[code], start, end, hg_token)
                if data:
                    candidates.append({'data': data, 'source': 'HG Brasil · histórico mensal', 'sample': False})
            except (requests.RequestException, ValueError, TypeError, KeyError):
                pass

        # brapi: tenta os códigos cobertos pelo plano; não se presume cobertura.
        if brapi_token and code in brapi_symbols:
            for symbol in brapi_symbols[code]:
                try:
                    data = _benchmark_monthly_brapi(symbol, start, end, brapi_token)
                    if data:
                        candidates.append({'data': data, 'source': f'brapi · histórico mensal ({symbol})', 'sample': False})
                        break
                except (requests.RequestException, ValueError, TypeError, KeyError):
                    continue

        # Yahoo Finance permanece como contingência não oficial para cobrir períodos maiores.
        for symbol in yahoo_symbols.get(code, []):
            try:
                data = _benchmark_monthly_from_yahoo(symbol, start, end)
                if data:
                    candidates.append({'data': data, 'source': f'Yahoo Finance · {symbol} (complementar)', 'sample': False})
                    break
            except (requests.RequestException, ValueError, TypeError, KeyError):
                continue

        # Amostra pública Dados B3 é último recurso; sua limitação será informada na interface.
        if sample_candidate:
            candidates.append(sample_candidate)

        if candidates:
            chosen = candidates[0]
            series[code] = chosen['data']
            source_map[code] = chosen['source']
            metadata[code] = {
                'source': chosen['source'],
                'last_available': _benchmark_latest_month(chosen['data']),
                'observations': len(chosen['data']),
                'sample': bool(chosen.get('sample')),
                'other_sources_available': [item['source'] for item in candidates[1:]],
                'note': chosen.get('note') or ''
            }
        else:
            unavailable.append(code)

    result = {
        'series': series,
        'unavailable': unavailable,
        'source': source_map,
        'meta': metadata,
        'validation': validation,
        'start': requested_start.isoformat(),
        'end': end.isoformat(),
        'updated_at': datetime.now(TZ).isoformat(timespec='seconds'),
        'notes': {
            'IPCA': 'O IPCA é mensal. A competência só aparece depois da divulgação oficial; não se estima o mês corrente.',
            'indices': 'Para IBOV, IFIX, SMLL e IDIV, Dados B3 é priorizado quando a série completa está disponível. Fontes alternativas são identificadas e não são misturadas por média.',
            'IVVB11': 'IVVB11 é um ETF, não um índice. Usa fechamento ajustado quando o provedor oferece esse campo.'
        }
    }
    BENCHMARK_CACHE[cache_key] = {'time': time.time(), 'data': result}
    return jsonify(result)

def _performance_history_signature(inputs):
    raw = json.dumps(inputs, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _compute_performance_history(inputs):
    operations = [dict(row) for row in inputs.get('operations') or []]
    dividends = [dict(row) for row in inputs.get('dividends') or []]
    imported_positions = [dict(row) for row in inputs.get('imported_positions') or []]
    current_positions = [dict(row) for row in inputs.get('current_positions') or []]
    snapshot_date = inputs.get('snapshot_date')
    history = fetch_portfolio_price_history(operations, [*imported_positions, *current_positions])
    performance = build_portfolio_performance(
        operations, dividends, history=history,
        imported_positions=imported_positions, snapshot_date=snapshot_date,
        current_positions=current_positions
    )
    return_types = sorted({
        str(row.get('tipo') or '').strip().lower()
        for row in [*operations, *imported_positions, *current_positions]
        if row.get('tipo')
    })
    performance_by_type = {}
    for asset_type in return_types:
        type_ops = [row for row in operations if str(row.get('tipo') or '').strip().lower() == asset_type]
        type_positions = [row for row in imported_positions if str(row.get('tipo') or '').strip().lower() == asset_type]
        type_current = [row for row in current_positions if str(row.get('tipo') or '').strip().lower() == asset_type]
        type_tickers = {str(row.get('ticker') or '').upper() for row in [*type_ops, *type_positions, *type_current]}
        type_dividends = [row for row in dividends if str(row.get('ticker') or '').upper() in type_tickers]
        performance_by_type[asset_type] = build_portfolio_performance(
            type_ops, type_dividends, history=history,
            imported_positions=type_positions, snapshot_date=snapshot_date,
            current_positions=type_current
        )
    return {
        'rentabilidade_historica': performance,
        'rentabilidade_por_tipo': performance_by_type,
    }


def _persist_performance_history_snapshot(signature, result):
    """Atualiza só os campos históricos se o snapshot ainda for da mesma carteira."""
    try:
        with DASHBOARD_CACHE_LOCK:
            with open(DASHBOARD_CACHE_PATH, 'r', encoding='utf-8') as handle:
                record = json.load(handle)
            payload = record.get('payload') if isinstance(record, dict) else None
            if not isinstance(payload, dict) or payload.get('performance_history_signature') != signature:
                return False
            if record.get('signature') != _dashboard_cache_signature():
                return False
            payload.update(result)
            payload['performance_history_pending'] = False
            integrity = payload.setdefault('integridade_dados', {})
            integrity['historico_precos'] = result['rentabilidade_historica'].get('fontes_historico', {})
            record['payload'] = payload
            tmp_path = DASHBOARD_CACHE_PATH + '.tmp'
            with open(tmp_path, 'w', encoding='utf-8') as handle:
                json.dump(record, handle, ensure_ascii=False, separators=(',', ':'))
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, DASHBOARD_CACHE_PATH)
            return True
    except (OSError, ValueError, TypeError, json.JSONDecodeError, sqlite3.Error) as exc:
        app.logger.warning('Não foi possível atualizar o cache com o histórico patrimonial: %s', exc)
        return False


def _performance_history_worker(initial_inputs, initial_signature):
    global PERFORMANCE_HISTORY_PENDING_INPUTS
    inputs, signature = initial_inputs, initial_signature
    while inputs is not None:
        result = None
        error = None
        try:
            # O contexto permite que os helpers de cache/log usem app.logger e g.
            with app.app_context():
                result = _compute_performance_history(inputs)
        except Exception as exc:
            error = exc
            app.logger.exception('Falha ao reconstruir histórico patrimonial em segundo plano')

        with PERFORMANCE_HISTORY_LOCK:
            if PERFORMANCE_HISTORY_PENDING_INPUTS is not None:
                inputs = PERFORMANCE_HISTORY_PENDING_INPUTS
                PERFORMANCE_HISTORY_PENDING_INPUTS = None
                signature = PERFORMANCE_HISTORY_STATE.get('pending_signature') or _performance_history_signature(inputs)
                PERFORMANCE_HISTORY_STATE.update({
                    'status': 'running', 'signature': signature,
                    'pending_signature': '', 'result': None, 'error': '', 'updated_at': ''
                })
                continue
            inputs = None
            if error is not None:
                PERFORMANCE_HISTORY_STATE.update({
                    'status': 'failed', 'result': None,
                    'error': str(error)[:500], 'updated_at': datetime.now(TZ).isoformat(timespec='seconds')
                })
            else:
                PERFORMANCE_HISTORY_STATE.update({
                    'status': 'ready', 'result': result,
                    'error': '', 'updated_at': datetime.now(TZ).isoformat(timespec='seconds')
                })
        if error is None and result is not None:
            _persist_performance_history_snapshot(signature, result)


def _queue_performance_history(inputs):
    global PERFORMANCE_HISTORY_PENDING_INPUTS
    signature = _performance_history_signature(inputs)
    with PERFORMANCE_HISTORY_LOCK:
        status = PERFORMANCE_HISTORY_STATE.get('status')
        current_signature = PERFORMANCE_HISTORY_STATE.get('signature')
        pending_signature = PERFORMANCE_HISTORY_STATE.get('pending_signature')
        if status == 'ready' and current_signature == signature:
            return signature
        if status == 'running':
            if current_signature == signature or pending_signature == signature:
                return signature
            PERFORMANCE_HISTORY_PENDING_INPUTS = inputs
            PERFORMANCE_HISTORY_STATE['pending_signature'] = signature
            return signature
        PERFORMANCE_HISTORY_STATE.update({
            'status': 'running', 'signature': signature,
            'pending_signature': '', 'result': None, 'error': '', 'updated_at': ''
        })
        PERFORMANCE_HISTORY_PENDING_INPUTS = None
        thread = threading.Thread(
            target=_performance_history_worker,
            args=(inputs, signature),
            name='portfolio-performance-history', daemon=True
        )
        thread.start()
    return signature


@app.route('/api/performance-history')
def performance_history_status():
    requested_signature = str(request.args.get('signature') or '')
    with PERFORMANCE_HISTORY_LOCK:
        state = dict(PERFORMANCE_HISTORY_STATE)
        pending_signature = state.get('pending_signature')
        if requested_signature and requested_signature not in (state.get('signature'), pending_signature):
            return jsonify(status='superseded')
        if requested_signature and requested_signature == pending_signature:
            return jsonify(status='running')
        response = {'status': state.get('status'), 'updated_at': state.get('updated_at')}
        if state.get('status') == 'ready' and isinstance(state.get('result'), dict):
            response.update(state['result'])
        if state.get('status') == 'failed':
            response['erro'] = state.get('error') or 'Não foi possível reconstruir o histórico patrimonial.'
        return jsonify(response)


@app.route('/api/dashboard')
def dashboard():
    # O último dashboard totalmente processado é reaproveitado no F5 quando
    # os bancos, credenciais e as últimas cotações persistidas continuam iguais.
    full_refresh = request.args.get('full') == '1'
    current_only = request.args.get('current_only') == '1'
    if not full_refresh:
        cached_snapshot = _read_dashboard_snapshot()
        if cached_snapshot:
            cached_payload = dict(cached_snapshot['payload'])
            cached_payload['cache_info'] = {
                'cached': True,
                'saved_at': cached_snapshot['saved_at'],
                'age_seconds': cached_snapshot['age_seconds']
            }
            return jsonify(cached_payload)
    if full_refresh:
        # A primeira etapa força cotações recentes e indexadores, mas não espera
        # sincronização de proventos nem histórico de preços para liberar os valores.
        force_quotes = request.args.get('refresh_quotes') != '0'
        portfolio_before_sync = carteira(force_refresh_quotes=force_quotes).json
        if current_only:
            sync_status = PROVENTOS_SYNC_CACHE.get('result') or {
                'ok': True,
                'mensagem': 'Valores atuais conferidos. Histórico e sincronização complementar seguem em segundo plano.'
            }
        else:
            sync_status = sync_brapi_proventos(
                force=False,
                quote_items=portfolio_before_sync.get('itens', []),
                active_items=portfolio_before_sync.get('itens', [])
            )
        portfolio = carteira().json
    else:
        portfolio = carteira(use_cached_quotes=True).json
        sync_status = PROVENTOS_SYNC_CACHE.get('result') or {
            'ok': True,
            'mensagem': 'Atualização de cotações, histórico e proventos será concluída em segundo plano.'
        }
    ops = [dict(r) for r in db().execute("SELECT * FROM operacoes WHERE status='executada' ORDER BY data,id").fetchall()]
    scheduled = [dict(r) for r in db().execute("SELECT * FROM operacoes WHERE status='agendada' ORDER BY data_execucao,id").fetchall()]
    active_tickers = {str(item.get('ticker') or '').upper() for item in portfolio.get('itens', []) if item.get('ticker')}
    today = datetime.now(TZ).date()
    received_records = _confirmed_received_proventos(_received_proventos_records(active_tickers), as_of=today)
    if active_tickers:
        marks = ','.join('?' for _ in active_tickers)
        pending_rows = [dict(r) for r in db().execute("SELECT * FROM proventos_api WHERE status='A receber' AND UPPER(ticker) IN (" + marks + ") ORDER BY data_pagamento,ticker", tuple(sorted(active_tickers))).fetchall()]
        manual_rows = [dict(r) for r in db().execute('SELECT * FROM proventos WHERE UPPER(ticker) IN (' + marks + ') ORDER BY data', tuple(sorted(active_tickers))).fetchall()]
    else:
        pending_rows = []
        manual_rows = []

    monthly = {}
    for op in ops:
        month = op['data'][:7]
        sign = 1 if op['lado'] == 'compra' else -1
        monthly[month] = monthly.get(month, 0) + sign * (op['quantidade'] * op['preco'] + float(op.get('custos') or 0))

    month_keys = []
    cursor = date(today.year, today.month, 1)
    for _ in range(11):
        cursor = (cursor.replace(day=1) - timedelta(days=1)).replace(day=1)
    for _ in range(12):
        month_keys.append(cursor.strftime('%Y-%m'))
        cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)

    detailed = [dict(item) for item in received_records]
    received_by_month = _confirmed_received_proventos_by_month(detailed, month_keys, as_of=today)

    # Pagamentos futuros já declarados. Prioriza lastDatePrior; quando a cotação
    # antiga só informa exDate, a data-com aproximada e a quantidade são sinalizadas.
    upcoming = []
    manual_keys = {(str(row['ticker']).upper(), str(row['data'])[:10], round(float(row['valor_total'] or 0), 2)) for row in manual_rows}
    # Lançamentos manuais com data futura representam proventos anunciados,
    # não recebidos. Preserve-os como A receber em vez de os misturar ao histórico pago.
    for row in manual_rows:
        payment_date = _api_date(row.get('data'))
        if payment_date and date.fromisoformat(payment_date) > today:
            upcoming.append({
                'ticker':str(row['ticker']).upper(),'tipo':'Provento manual','tipo_ativo':typesafe_asset_type(row['ticker']),
                'status':'A receber','data_com':None,'data_pagamento':payment_date,'quantidade':None,
                'valor_por_ativo':None,'valor_total':float(row['valor_total'] or 0),'origem':'Manual',
                'tipo_evento':'PROVENTO','remarks':'Data e valor informados manualmente.'
            })
    for row in pending_rows:
        key=(str(row['ticker']).upper(),str(row['data_pagamento'])[:10],round(float(row['valor_total'] or 0),2))
        if key in manual_keys:
            continue
        upcoming.append({
            'ticker':row['ticker'],'tipo':row['tipo_evento'],'tipo_ativo':row['tipo_ativo'],
            'status':'A receber','data_com':row['data_com'],'data_pagamento':row['data_pagamento'],
            'quantidade':float(row['quantidade'] or 0),'valor_por_ativo':float(row['valor_por_ativo'] or 0),
            'valor_total':float(row['valor_total'] or 0),'origem':('Fonte histórica' if 'Alpha Vantage' in str(row.get('remarks') or '') else 'brapi'),'tipo_evento':row['tipo_evento'],
            'remarks':row.get('remarks','')
        })

    # Fallback visual da versão estável: eventos anunciados vindos da cotação v1
    # aparecem mesmo se o módulo v2 de dividendos não estiver habilitado no plano.
    # Esses itens são deduplicados com o histórico sincronizado e não substituem
    # os pagamentos confirmados do banco.
    pending_keys = {
        (str(item.get('ticker') or '').upper(), str(item.get('data_pagamento') or '')[:10],
         str(item.get('tipo_evento') or item.get('tipo') or '').upper(), round(float(item.get('valor_por_ativo') or 0), 8))
        for item in upcoming
    }
    pending_amount_keys = {(str(item.get('ticker') or '').upper(), str(item.get('data_pagamento') or '')[:10], round(float(item.get('valor_total') or 0), 2)) for item in upcoming}
    manual_pending_keys = {
        (str(row['ticker']).upper(), str(row['data'])[:10], round(float(row['valor_total'] or 0), 2))
        for row in manual_rows if str(row.get('data') or '')[:10] > today.isoformat()
    }
    for item in portfolio.get('itens', []):
        ticker = str(item.get('ticker') or '').upper()
        kind = str(item.get('tipo') or '').lower()
        if not ticker or kind == 'renda_fixa':
            continue
        quote = cotacao(ticker) if full_refresh else cotacao_cacheada(ticker)
        events = ((quote.get('dividendsData') or {}).get('cashDividends') or [])
        for event in events:
            payment_date = _api_date(event.get('paymentDate') or event.get('payment_date'))
            if not payment_date or date.fromisoformat(payment_date) < today:
                continue
            try:
                rate = float(event.get('rawRate') or event.get('rate') or event.get('value') or 0)
            except (TypeError, ValueError):
                rate = 0.0
            if rate <= 0:
                continue
            official_date = _api_date(event.get('lastDatePrior'))
            ex_date = _api_date(event.get('exDate'))
            estimated = False
            record_date = official_date
            if not record_date and ex_date:
                try:
                    record_date = previous_b3_session(date.fromisoformat(ex_date)).isoformat()
                    estimated = True
                except ValueError:
                    record_date = ''
            try:
                if record_date:
                    quantity, quantity_method = position_quantity_on_date_details(ticker, record_date)
                else:
                    quantity = float(item.get('quantidade') or 0)
                    quantity_method = 'posição atual (data-com ausente na fonte)'
            except Exception:
                quantity, quantity_method = 0.0, 'quantidade indisponível'
            if quantity <= 1e-8:
                continue
            event_type = str(event.get('label') or event.get('type') or ('RENDIMENTO' if kind == 'fii' else 'PROVENTO')).upper()
            dedupe = (ticker, payment_date, event_type, round(rate, 8))
            if dedupe in pending_keys or (ticker, payment_date, round(quantity * rate, 2)) in manual_pending_keys or (ticker, payment_date, round(quantity * rate, 2)) in pending_amount_keys:
                continue
            remarks = str(event.get('remarks') or '')
            if estimated:
                remarks = (remarks + ' Data-com estimada pela véspera do exDate; confira o informe oficial.').strip()
            if quantity_method.startswith('reconstruída'):
                remarks = (remarks + ' Quantidade reconstruída a partir do saldo-base; depende da completude das operações cadastradas.').strip()
            elif quantity_method.startswith('posição atual'):
                remarks = (remarks + ' Quantidade estimada pela posição atual, pois a fonte não informou data-com.').strip()
            upcoming.append({
                'ticker':ticker,'tipo':event_type,'tipo_ativo':kind,'status':'A receber',
                'data_com':record_date or None,'data_pagamento':payment_date,
                'quantidade':round(quantity,8),'valor_por_ativo':round(rate,8),
                'valor_total':round(quantity*rate,2),'origem':'Cotação v1','tipo_evento':event_type,
                'remarks':remarks
            })
            pending_keys.add(dedupe)
            pending_amount_keys.add((ticker, payment_date, round(quantity * rate, 2)))

    # Pending forecasts are limited to the current month and future months.
    upcoming = _filter_upcoming_proventos_by_month(upcoming, as_of=today)

    monthly_values = [{'mes':m,'recebido':round(received_by_month.get(m,0),2)} for m in month_keys]
    total_12m = round(sum(received_by_month.get(month, 0.0) for month in month_keys), 2)
    total_confirmed_12m = _confirmed_proventos_total_12m(total_12m, upcoming, as_of=today)
    portfolio_total = round(sum(float(x.get('valor_atual') or 0) for x in portfolio.get('itens', [])), 2)
    # O card Maiores pagadores considera o histórico integral de recebimentos,
    # inclusive ativos que já não estejam na carteira atual. A fonte canônica
    # exige status Pago e data de pagamento não posterior à data local atual.
    all_confirmed_received = _confirmed_received_proventos(
        _received_proventos_records(), as_of=today
    )
    paid_by_ticker = {}
    for receipt in all_confirmed_received:
        if not _is_income_event(receipt.get('tipo_evento')):
            continue
        ticker = str(receipt.get('ticker') or '').strip().upper()
        amount = float(receipt.get('valor_total') or 0)
        if not ticker or not math.isfinite(amount) or amount <= 0:
            continue
        bucket = paid_by_ticker.setdefault(ticker, {
            'ticker': ticker,
            'tipo_ativo': receipt.get('tipo_ativo') or typesafe_asset_type(ticker),
            'status': 'Pago',
            'data_pagamento': receipt.get('data_pagamento'),
            'valor_total': 0.0,
        })
        bucket['valor_total'] += amount
        if str(receipt.get('data_pagamento') or '') > str(bucket.get('data_pagamento') or ''):
            bucket['data_pagamento'] = receipt.get('data_pagamento')
    top_paid_payers = sorted(
        paid_by_ticker.values(),
        key=lambda item: (-round(float(item['valor_total']), 2), item['ticker'])
    )[:2]
    for item in top_paid_payers:
        item['valor_total'] = round(float(item['valor_total']), 2)
    type_totals = {}
    for event in detailed:
        if _is_income_event(event.get('tipo_evento')):
            type_totals[event['ticker']] = type_totals.get(event['ticker'], 0.0) + float(event.get('valor_total') or 0)

    base_date = snapshot_date_for()
    imported_rows = [dict(row) for row in db().execute(
        'SELECT ticker,tipo,quantidade,preco_medio,preco_base,data_base,nome,origem FROM saldos_importados WHERE data_base=? ORDER BY tipo,nome,ticker',
        (base_date,)
    ).fetchall()] if base_date else []
    # Modified Dietz recebe pagamentos efetivos; adjustedClose evita contar o
    # mesmo provento duas vezes. A posição-base também informa a cobertura atual.
    performance_dividends = [
        {'ticker':item['ticker'],'data':item['data_pagamento'],'valor_total':item['valor_total'],'tipo_evento':item.get('tipo_evento')}
        for item in detailed if _is_income_event(item.get('tipo_evento'))
    ]
    performance_pending = False
    performance_signature = ''
    if full_refresh and not current_only:
        performance_inputs = {
            'operations': ops,
            'dividends': performance_dividends,
            'imported_positions': imported_rows,
            'snapshot_date': base_date,
            'current_positions': [dict(row) for row in portfolio.get('itens', [])]
        }
        performance_signature = _queue_performance_history(performance_inputs)
        performance_pending = True
        # Resposta imediata: a consulta histórica (que pode consumir uma chamada
        # por ativo no plano gratuito) é executada em worker e consultada por polling.
        performance = {
            'series_12m': [], 'series_total': [], 'series_roi_total': [],
            'series_patrimonio': [], 'rentabilidade_12m': None,
            'rentabilidade_total': None, 'meses_disponiveis': 0,
            'primeiro_mes': None, 'historico_completo': False,
            'historico_parcial': True, 'ativos_sem_historico': [],
            'ativos_com_historico': 0, 'total_ativos': len(portfolio.get('itens', [])),
            'fontes_historico': {}
        }
        performance_by_type = {}
    else:
        # Estrutura compatível com a interface enquanto o histórico é carregado.
        performance = {
            'series_12m': [], 'series_total': [], 'series_roi_total': [],
            'series_patrimonio': [], 'rentabilidade_12m': None,
            'rentabilidade_total': None, 'meses_disponiveis': 0,
            'primeiro_mes': None, 'historico_completo': False,
            'historico_parcial': True, 'ativos_sem_historico': [],
            'ativos_com_historico': 0, 'total_ativos': len(portfolio.get('itens', [])),
            'fontes_historico': {}
        }
        performance_by_type = {}
    dashboard_response = jsonify(**portfolio, operacoes=ops, agendadas=scheduled, proventos=manual_rows,
        saldos_importados=imported_rows, data_base_snapshot=base_date,
        aportes_mensais=monthly, proventos_mensais=received_by_month,
        metas=[dict(r) for r in db().execute('SELECT * FROM metas').fetchall()],
        rentabilidade_historica=performance, rentabilidade_por_tipo=performance_by_type,
        performance_history_pending=performance_pending, performance_history_signature=performance_signature,
        integridade_dados={
            'cotacoes': {item['ticker']: {'fonte': item.get('cotacao_origem'), 'disponivel': bool(item.get('cotacao_disponivel'))} for item in portfolio.get('itens', [])},
            'historico_precos': performance.get('fontes_historico', {}),
            'proventos': {'fonte_canonica': 'brapi + lançamentos manuais/B3', 'deduplicacao': 'chave de evento + ticker/data de pagamento/valor', 'sincronizacao': sync_status}
        },
        proventos_dashboard={
            'mensal':monthly_values,'total_12m':total_12m,'total_confirmado_12m':total_confirmed_12m,'media_mensal':round(total_12m/12,2),
            'total_carteira':portfolio_total,'recebidos':sorted(detailed,key=lambda x:x['data_pagamento'],reverse=True),
            'a_receber':sorted(upcoming,key=lambda x:x['data_pagamento']),
            'maiores_pagadores':top_paid_payers,
            'distribuicao':type_totals,'sincronizacao':sync_status
        })
    response_payload = dashboard_response.get_json()
    if full_refresh:
        try:
            saved_at = _write_dashboard_snapshot(response_payload)
            response_payload['cache_info'] = {'cached': False, 'saved_at': saved_at, 'age_seconds': 0}
            if performance_signature:
                with PERFORMANCE_HISTORY_LOCK:
                    ready_result = (PERFORMANCE_HISTORY_STATE.get('result')
                                    if PERFORMANCE_HISTORY_STATE.get('status') == 'ready'
                                    and PERFORMANCE_HISTORY_STATE.get('signature') == performance_signature else None)
                if ready_result:
                    _persist_performance_history_snapshot(performance_signature, ready_result)
        except (OSError, TypeError, ValueError) as cache_error:
            app.logger.warning('Não foi possível salvar o cache do dashboard: %s', cache_error)
            response_payload['cache_info'] = {'cached': False, 'saved_at': '', 'cache_error': True}
    else:
        response_payload['cache_info'] = {'cached': False, 'saved_at': ''}
    return jsonify(response_payload)


@app.route('/api/ativos/busca')
def busca_ativos():
    termo = str(request.args.get('q', '')).strip()
    tipo = str(request.args.get('tipo', 'acao')).lower()
    if len(termo) < 2:
        return jsonify(itens=[])

    # O endpoint de listagem da brapi permite filtrar por ticker/nome e subtipo.
    params = {'search': termo, 'limit': 10, 'sortBy': 'name', 'sortOrder': 'asc'}
    if tipo == 'acao':
        params['type'] = 'stock'
    elif tipo == 'fii':
        params.update({'type': 'fund', 'subType': 'fii'})
    elif tipo == 'etf':
        params.update({'type': 'fund', 'subType': 'etf'})
    elif tipo == 'bdr':
        params['type'] = 'bdr'
    elif tipo == 'stocks':
        params['type'] = 'stock'
    elif tipo in ('internacional', 'cripto', 'renda_fixa', 'reit', 'tesouro', 'fundo', 'outros'):
        return jsonify(itens=[], mensagem='Busca automática ainda não disponível para esta classe. Digite o ticker manualmente.')

    headers = {'Authorization': f'Bearer {_brapi_token()}'} if _brapi_token() else {}
    try:
        response = _brapi_get('https://brapi.dev/api/quote/list',
                                params=params, headers=headers, timeout=8, cache_ttl=6 * 3600, stale_ttl=7 * 86400)
        response.raise_for_status()
        payload = response.json()
        itens = []
        for item in payload.get('stocks', []):
            ticker = str(item.get('stock') or item.get('symbol') or '').upper()
            if not ticker:
                continue
            itens.append({
                'ticker': ticker,
                'nome': item.get('name') or ticker,
                'preco': item.get('close') or item.get('regularMarketPrice'),
                'tipo': tipo
            })
        return jsonify(itens=itens)
    except requests.RequestException:
        return jsonify(itens=[], mensagem='Não foi possível consultar a lista de ativos agora.'), 502


@app.route('/api/distribuicao', methods=['GET', 'PUT'])
def distribuicao():
    if request.method == 'GET':
        rows = db().execute('SELECT id,nome,percentual,ordem FROM distribuicao_alvo ORDER BY ordem,id').fetchall()
        return jsonify(itens=[dict(r) for r in rows])
    payload = request.get_json(silent=True) or {}
    itens = payload.get('itens', [])
    if not isinstance(itens, list) or not itens:
        return json_error('Adicione pelo menos uma classe de ativo.')
    cleaned = []
    names = set()
    total = 0.0
    try:
        for item in itens:
            nome = str(item.get('nome', '')).strip()
            percentual = float(item.get('percentual', 0))
            key = nome.casefold()
            if not nome or len(nome) > 50 or key in names or percentual < 0 or percentual > 100:
                raise ValueError()
            names.add(key)
            total += percentual
            cleaned.append((nome, percentual))
        if abs(total - 100.0) > 0.001:
            return json_error('A soma dos percentuais precisa ser exatamente 100%.')
    except (TypeError, ValueError):
        return json_error('Confira os nomes e percentuais informados.')
    con = db()
    con.execute('DELETE FROM distribuicao_alvo')
    con.executemany('INSERT INTO distribuicao_alvo(nome,percentual,ordem) VALUES(?,?,?)',
                    [(nome, percentual, idx) for idx, (nome, percentual) in enumerate(cleaned, 1)])
    con.commit()
    rows = con.execute('SELECT id,nome,percentual,ordem FROM distribuicao_alvo ORDER BY ordem,id').fetchall()
    return jsonify(ok=True, itens=[dict(r) for r in rows])


def _tax_year():
    try:
        year = int(request.args.get('ano', datetime.now(TZ).year))
        if year < 2000 or year > 2100:
            raise ValueError()
        return year
    except (TypeError, ValueError):
        raise ValueError('Informe um ano-base válido.')


def build_irpf_summary(year):
    """Resumo fiscal anual com saldos-base e operações posteriores.

    Saldos importados são fotografias completas: inicializam ou redefinem as
    posições na data-base, evitando zerar vendas de ativos sem compras locais.
    O custo fiscal inclui custos informados nas compras e é reduzido
    proporcionalmente nas vendas.
    """
    year_start = date(year, 1, 1).isoformat()
    year_end = date(year, 12, 31).isoformat()
    snapshots = db().execute(
        'SELECT DISTINCT data_base FROM saldos_importados WHERE data_base<=? ORDER BY data_base',
        (year_end,)
    ).fetchall()
    snapshot_dates = [str(r['data_base']) for r in snapshots]
    opening_snapshot = max((d for d in snapshot_dates if d < year_start), default=None)
    in_year_snapshots = [d for d in snapshot_dates if year_start <= d <= year_end]

    holdings = {}
    def reset_from_snapshot(snapshot_date):
        holdings.clear()
        rows = db().execute('SELECT * FROM saldos_importados WHERE data_base=?', (snapshot_date,)).fetchall()
        for row in rows:
            ticker = str(row['ticker']).upper()
            qty = max(0.0, float(row['quantidade'] or 0))
            avg = max(0.0, float(row['preco_medio'] or 0))
            holdings[ticker] = {'ticker': ticker, 'tipo': str(row['tipo'] or 'outros'),
                                'quantidade': qty, 'custo_total': qty * avg}

    if opening_snapshot:
        reset_from_snapshot(opening_snapshot)
        ops_start = opening_snapshot
    else:
        ops_start = '0001-01-01'

    ops = db().execute(
        "SELECT * FROM operacoes WHERE status='executada' AND data>? AND data<=? ORDER BY data,id",
        (ops_start, year_end)
    ).fetchall()
    sales = {}
    snapshot_set = set(in_year_snapshots)
    # Agrupa por data para aplicar a fotografia somente após todas as operações do dia.
    by_date = {}
    for op in ops:
        by_date.setdefault(str(op['data'])[:10], []).append(op)
    for op_date in sorted(set(by_date) | snapshot_set):
        for op in by_date.get(op_date, []):
            ticker = str(op['ticker']).upper()
            kind = str(op['tipo'] or 'outros')
            qty = max(0.0, float(op['quantidade'] or 0))
            price = max(0.0, float(op['preco'] or 0))
            costs = max(0.0, float(op['custos'] or 0))
            item = holdings.setdefault(ticker, {'ticker': ticker, 'tipo': kind, 'quantidade': 0.0, 'custo_total': 0.0})
            item['tipo'] = kind or item['tipo']
            if op['lado'] == 'compra':
                item['quantidade'] += qty
                item['custo_total'] += qty * price + costs
            else:
                available = item['quantidade']
                sold = min(qty, available)
                if sold > 0:
                    average = item['custo_total'] / available if available > 0 else 0.0
                    basis = average * sold
                    proceeds = max(0.0, sold * price - costs)
                    profit = proceeds - basis
                    if year_start <= op_date <= year_end:
                        month = op_date[:7]
                        group = 'fiis' if kind in ('fii', 'reit') else 'comuns'
                        bucket = sales.setdefault((group, month), {'grupo': group, 'mes': month, 'vendas': 0.0,
                            'custo': 0.0, 'resultado': 0.0, 'operacoes': 0})
                        bucket['vendas'] += proceeds
                        bucket['custo'] += basis
                        bucket['resultado'] += profit
                        bucket['operacoes'] += 1
                    item['quantidade'] -= sold
                    item['custo_total'] = max(0.0, item['custo_total'] - basis)
        if op_date in snapshot_set:
            reset_from_snapshot(op_date)
            snapshot_set.remove(op_date)

    positions = []
    # Usa a posição fiscal reconstruída acima (inclui custos de compra), em vez
    # de posicao(), cuja regra operacional de preço médio exclui custos.
    for item in holdings.values():
        if item['quantidade'] <= 1e-8:
            continue
        positions.append({'ticker':item['ticker'],'tipo':item['tipo'],
            'quantidade':round(item['quantidade'],8),
            'preco_medio':round(item['custo_total']/item['quantidade'],4),
            'custo_total':round(item['custo_total'],2)})

    positions.sort(key=lambda x: (x['tipo'], x['ticker']))

    incomes = [dict(row) for row in db().execute(
        "SELECT * FROM irpf_lancamentos WHERE substr(data,1,4)=? ORDER BY data DESC,id DESC", (str(year),)).fetchall()]
    exempt = sum(float(x['valor']) for x in incomes if x['grupo'] == 'isento')
    exclusive = sum(float(x['valor']) for x in incomes if x['grupo'] == 'exclusiva')
    monthly_sales = [dict(v, vendas=round(v['vendas'], 2), custo=round(v['custo'], 2), resultado=round(v['resultado'], 2))
                     for _, v in sorted(sales.items(), key=lambda pair: pair[0][1])]
    return {'ano': year, 'posicoes': positions, 'rendimentos': incomes, 'operacoes': monthly_sales,
        'resumo': {'bens': round(sum(x['custo_total'] for x in positions), 2), 'isentos': round(exempt, 2),
                   'exclusivos': round(exclusive, 2), 'resultado_vendas': round(sum(x['resultado'] for x in monthly_sales), 2)}}


@app.route('/api/irpf/resumo')
def irpf_resumo():
    try:
        return jsonify(build_irpf_summary(_tax_year()))
    except ValueError as exc:
        return json_error(str(exc))


@app.route('/api/irpf/lancamentos', methods=['GET', 'POST'])
def irpf_lancamentos():
    if request.method == 'GET':
        try:
            year = _tax_year()
        except ValueError as exc:
            return json_error(str(exc))
        rows = db().execute(
            "SELECT * FROM irpf_lancamentos WHERE substr(data,1,4)=? ORDER BY data DESC,id DESC",
            (str(year),)
        ).fetchall()
        return jsonify(itens=[dict(row) for row in rows])
    payload = request.get_json(silent=True) or {}
    try:
        grupo = str(payload.get('grupo', '')).strip().lower()
        natureza = str(payload.get('natureza', '')).strip()
        fonte = str(payload.get('fonte_pagadora', '')).strip()
        documento = str(payload.get('documento', '')).strip()
        valor = float(payload.get('valor', 0))
        data_registro = str(payload.get('data', ''))
        observacoes = str(payload.get('observacoes', '')).strip()
        date.fromisoformat(data_registro)
        if not math.isfinite(valor) or grupo not in ('isento', 'exclusiva') or not natureza or not fonte or valor <= 0:
            raise ValueError()
        if len(natureza) > 100 or len(fonte) > 160 or len(documento) > 40 or len(observacoes) > 1000:
            raise ValueError()
    except (TypeError, ValueError):
        return json_error('Confira o grupo, natureza, fonte pagadora, valor e data.')
    cur = db().execute(
        'INSERT INTO irpf_lancamentos(grupo,natureza,fonte_pagadora,documento,valor,data,observacoes) VALUES(?,?,?,?,?,?,?)',
        (grupo, natureza, fonte, documento, valor, data_registro, observacoes)
    )
    db().commit()
    return jsonify(ok=True, id=cur.lastrowid)


@app.route('/api/irpf/lancamentos/<int:item_id>', methods=['PUT', 'DELETE'])
def irpf_lancamento_item(item_id):
    current = db().execute('SELECT * FROM irpf_lancamentos WHERE id=?', (item_id,)).fetchone()
    if not current:
        return json_error('Rendimento não encontrado.', 404)
    if request.method == 'DELETE':
        db().execute('DELETE FROM irpf_lancamentos WHERE id=?', (item_id,))
        db().commit()
        return jsonify(ok=True)
    payload = request.get_json(silent=True) or {}
    try:
        grupo = str(payload.get('grupo', current['grupo'])).strip().lower()
        natureza = str(payload.get('natureza', current['natureza'])).strip()
        fonte = str(payload.get('fonte_pagadora', current['fonte_pagadora'])).strip()
        documento = str(payload.get('documento', current['documento'] or '')).strip()
        valor = float(payload.get('valor', current['valor']))
        data_registro = str(payload.get('data', current['data']))
        observacoes = str(payload.get('observacoes', current['observacoes'] or '')).strip()
        date.fromisoformat(data_registro)
        if not math.isfinite(valor) or grupo not in ('isento', 'exclusiva') or not natureza or not fonte or valor <= 0:
            raise ValueError()
        if len(natureza) > 100 or len(fonte) > 160 or len(documento) > 40 or len(observacoes) > 1000:
            raise ValueError()
    except (TypeError, ValueError):
        return json_error('Confira os dados do rendimento.')
    db().execute(
        'UPDATE irpf_lancamentos SET grupo=?,natureza=?,fonte_pagadora=?,documento=?,valor=?,data=?,observacoes=? WHERE id=?',
        (grupo, natureza, fonte, documento, valor, data_registro, observacoes, item_id)
    )
    db().commit()
    return jsonify(ok=True)


@app.route('/api/darfs', methods=['GET', 'POST'])
def darfs():
    if request.method == 'GET':
        year = str(request.args.get('ano', datetime.now(TZ).year))
        if not year.isdigit() or not 2000 <= int(year) <= 2100:
            return json_error('Informe um ano válido.')
        rows = db().execute(
            "SELECT * FROM darfs WHERE substr(competencia,1,4)=? ORDER BY competencia DESC,vencimento DESC,id DESC",
            (year,)
        ).fetchall()
        return jsonify(itens=[dict(row) for row in rows])
    payload = request.get_json(silent=True) or {}
    try:
        competencia = str(payload.get('competencia', '')).strip()
        if len(competencia) != 7 or competencia[4] != '-':
            raise ValueError()
        date.fromisoformat(competencia + '-01')
        codigo = str(payload.get('codigo', '')).strip()
        valor = float(payload.get('valor', 0))
        vencimento = str(payload.get('vencimento', '')).strip()
        pago_em = str(payload.get('pago_em', '')).strip()
        status = str(payload.get('status', 'pendente')).strip().lower()
        observacoes = str(payload.get('observacoes', '')).strip()
        date.fromisoformat(vencimento)
        if pago_em:
            date.fromisoformat(pago_em)
        if not math.isfinite(valor) or not codigo or len(codigo) > 20 or valor <= 0 or status not in ('pendente', 'pago'):
            raise ValueError()
        if status == 'pago' and not pago_em:
            raise ValueError()
        if len(observacoes) > 1000:
            raise ValueError()
    except (TypeError, ValueError):
        return json_error('Confira competência, código, valor, vencimento e situação. Para DARF pago, informe a data do pagamento.')
    cur = db().execute(
        'INSERT INTO darfs(competencia,codigo,valor,vencimento,pago_em,status,observacoes) VALUES(?,?,?,?,?,?,?)',
        (competencia, codigo, valor, vencimento, pago_em, status, observacoes)
    )
    db().commit()
    return jsonify(ok=True, id=cur.lastrowid)


@app.route('/api/darfs/<int:darf_id>', methods=['PUT', 'DELETE'])
def darf_item(darf_id):
    current = db().execute('SELECT * FROM darfs WHERE id=?', (darf_id,)).fetchone()
    if not current:
        return json_error('DARF não encontrado.', 404)
    if request.method == 'DELETE':
        db().execute('DELETE FROM darfs WHERE id=?', (darf_id,))
        db().commit()
        return jsonify(ok=True)
    payload = request.get_json(silent=True) or {}
    merged = {key: payload.get(key, current[key]) for key in ('competencia', 'codigo', 'valor', 'vencimento', 'pago_em', 'status', 'observacoes')}
    try:
        competencia = str(merged['competencia']).strip()
        if len(competencia) != 7 or competencia[4] != '-':
            raise ValueError()
        date.fromisoformat(competencia + '-01')
        codigo = str(merged['codigo']).strip()
        valor = float(merged['valor'])
        vencimento = str(merged['vencimento']).strip()
        pago_em = str(merged['pago_em'] or '').strip()
        status = str(merged['status']).strip().lower()
        observacoes = str(merged['observacoes'] or '').strip()
        date.fromisoformat(vencimento)
        if pago_em:
            date.fromisoformat(pago_em)
        if not math.isfinite(valor) or not codigo or len(codigo) > 20 or valor <= 0 or status not in ('pendente', 'pago'):
            raise ValueError()
        if status == 'pago' and not pago_em:
            raise ValueError()
        if len(observacoes) > 1000:
            raise ValueError()
    except (TypeError, ValueError):
        return json_error('Confira os dados do DARF. Para DARF pago, informe a data do pagamento.')
    db().execute(
        'UPDATE darfs SET competencia=?,codigo=?,valor=?,vencimento=?,pago_em=?,status=?,observacoes=? WHERE id=?',
        (competencia, codigo, valor, vencimento, pago_em, status, observacoes, darf_id)
    )
    db().commit()
    return jsonify(ok=True)


@app.route('/api/ativo/<ticker>')
def ativo(ticker): return jsonify(cotacao(ticker.upper()))


if __name__ == '__main__': app.run(host='0.0.0.0',port=8080)
