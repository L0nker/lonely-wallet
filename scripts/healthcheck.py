#!/usr/local/bin/python
"""Docker healthcheck that reports the explicit application/chart state."""
from __future__ import annotations

import json
import sys
from urllib.error import HTTPError
from urllib.request import urlopen

URL = 'http://127.0.0.1:8080/api/health?strict=1'


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
    warning = str(charts.get('warning') or '').strip()
    detail = f'; gráficos={engine}' + (f'; {warning}' if warning else '')
    print(f'Lonely Wallet healthcheck: {state} (HTTP {status_code}){detail}')
    return 0 if status_code == 200 and state == 'healthy' else 1


if __name__ == '__main__':
    raise SystemExit(main())
