#!/usr/bin/env python3
"""Vendor the pinned Chart.js UMD build into Flask static assets during image build."""
from pathlib import Path
import os
import tempfile
import urllib.request

VERSION = "4.4.4"
URL = f"https://cdn.jsdelivr.net/npm/chart.js@{VERSION}/dist/chart.umd.min.js"
DESTINATION = Path(__file__).resolve().parents[1] / "app" / "static" / "vendor" / "chart.umd.min.js"


def main():
    DESTINATION.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(URL, headers={"User-Agent": "LonelyWallet-build/1.0"})
    with urllib.request.urlopen(request, timeout=45) as response:
        final_url = response.geturl()
        payload = response.read(600_001)
    if not final_url.startswith("https://cdn.jsdelivr.net/"):
        raise RuntimeError("Chart.js download redirected to an unexpected host.")
    if not (100_000 <= len(payload) <= 600_000):
        raise RuntimeError("Downloaded Chart.js file has an unexpected size.")
    header = payload[:512].decode("utf-8", errors="ignore")
    if "Chart.js" not in header or VERSION not in header:
        raise RuntimeError("Downloaded asset does not identify the pinned Chart.js version.")
    fd, temp_name = tempfile.mkstemp(prefix=".chartjs-", suffix=".tmp", dir=DESTINATION.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o644)
        os.replace(temp_name, DESTINATION)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
    print(f"Vendored Chart.js {VERSION}: {DESTINATION}")


if __name__ == "__main__":
    main()
