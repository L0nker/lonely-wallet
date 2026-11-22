#!/usr/bin/env python3
"""Vendor the pinned Chart.js UMD build into the Flask static directory."""
from pathlib import Path
import requests

VERSION = '4.4.4'
URL = f'https://cdn.jsdelivr.net/npm/chart.js@{VERSION}/dist/chart.umd.min.js'
TARGET = Path(__file__).resolve().parents[1] / 'app' / 'static' / 'vendor' / 'chart.umd.min.js'
MINIMUM_SIZE = 100_000


def main() -> None:
    response = requests.get(URL, timeout=30, headers={'User-Agent': 'LonelyWallet-Build/1.0'})
    response.raise_for_status()
    content = response.content
    if len(content) < MINIMUM_SIZE or b'Chart' not in content[:100_000]:
        raise RuntimeError('O arquivo recebido do Chart.js não passou pela validação básica.')
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    temporary = TARGET.with_suffix('.js.tmp')
    temporary.write_bytes(content)
    temporary.replace(TARGET)
    (TARGET.parent / 'CHARTJS-LICENSE.txt').write_text(
        f'Chart.js {VERSION}\nDistribuição UMD: {URL}\nLicença: MIT (Chart.js project)\n',
        encoding='utf-8'
    )
    print(f'Chart.js {VERSION} instalado em {TARGET}')


if __name__ == '__main__':
    main()
