#!/usr/bin/env python3
"""Docker healthcheck for service availability, while reporting chart degradation."""
from __future__ import annotations

import json
import sys
from urllib.error import HTTPError
from urllib.request import urlopen

# The regular health endpoint deliberately reports HTTP 200 for a usable
# fallback renderer. The JSON body still says `degraded`, so operators can
# distinguish service availability from full Chart.js availability.
URL = 'http://127.0.0.1:8080/api/health'


def main() -> int:
    status_code = 0
    try:
        response = urlopen(URL, timeout=4)
        try:
            status_code = int(getattr(response, 'status', 200))
            body = response.read()
        finally:
            response.close()
    except HTTPError as exc:
        status_code = int(exc.code)
        body = exc.read()
        try:
            exc.close()
        except Exception:
            pass
    except Exception as exc:
        print(f'Lonely Wallet healthcheck: indisponível ({type(exc).__name__}).', file=sys.stderr)
        return 1

    try:
        payload = json.loads(body.decode('utf-8'))
    except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
        print(f'Lonely Wallet healthcheck: resposta inválida (HTTP {status_code}).', file=sys.stderr)
        return 1

    state = str(payload.get('status') or 'unhealthy')
    charts = payload.get('charts') if isinstance(payload.get('charts'), dict) else {}
    engine = str(charts.get('engine') or 'unknown')
    fallback_available = bool(charts.get('fallback_available'))
    warning = str(charts.get('warning') or '').strip()
    detail = f'; gráficos={engine}' + (f'; {warning}' if warning else '')
    print(f'Lonely Wallet healthcheck: {state} (HTTP {status_code}){detail}')

    if status_code != 200:
        return 1
    if state == 'healthy':
        return 0
    # The app is operational with a usable fallback; retain `degraded` in the
    # API response/log, but do not mark the whole container unhealthy.
    if state == 'degraded' and engine == 'fallback' and fallback_available:
        return 0
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
