#!/usr/bin/env python3
"""Backfill 1h spot and linear USDT candles from Bybit V5.

Writes the exact spot paths consumed by scripts/backtest_v2.py:
  data/historical/{coin}_spot_1h.csv.gz
Also writes separate futures-market candles:
  data/historical/{coin}_linear_1h.csv.gz

No credentials are required. Public market data only.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE_URL = "https://api.bybit.com/v5/market/kline"
COINS = ("btc", "eth", "xrp", "sol")
SYMBOLS = {coin: coin.upper() + "USDT" for coin in COINS}
HOUR_MS = 60 * 60 * 1000
FIELDS = ("open_time_ms", "open", "high", "low", "close", "volume")


def request_page(category: str, symbol: str, start_ms: int, end_ms: int,
                 retries: int = 6) -> list[list[str]]:
    params = urllib.parse.urlencode({
        "category": category, "symbol": symbol, "interval": "60",
        "start": start_ms, "end": end_ms, "limit": 1000,
    })
    url = BASE_URL + "?" + params
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "crypto-checkpoint-backtest/2.0"}
            )
            with urllib.request.urlopen(req, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("retCode") != 0:
                raise RuntimeError(
                    f"Bybit retCode={payload.get('retCode')}: {payload.get('retMsg')}"
                )
            return payload.get("result", {}).get("list", [])
        except Exception:
            if attempt + 1 == retries:
                raise
            time.sleep(min(2 ** attempt, 30))
    return []


def fetch_market(category: str, symbol: str, start_ms: int, end_ms: int) -> list[dict]:
    rows: dict[int, dict] = {}
    # Use bounded windows to avoid ambiguous pagination and API max-page truncation.
    window_ms = 999 * HOUR_MS
    cursor = start_ms
    while cursor <= end_ms:
        window_end = min(cursor + window_ms, end_ms)
        page = request_page(category, symbol, cursor, window_end)
        for item in page:
            if len(item) < 6:
                continue
            ts = int(item[0])
            if start_ms <= ts <= end_ms and ts % HOUR_MS == 0:
                rows[ts] = {
                    "open_time_ms": ts,
                    "open": item[1], "high": item[2], "low": item[3],
                    "close": item[4], "volume": item[5],
                }
        cursor = window_end + HOUR_MS
        time.sleep(0.12)
    return [rows[k] for k in sorted(rows)]


def write_gzip_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=path.parent)
    os.close(fd)
    try:
        with gzip.open(temp_name, "wt", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def audit(rows: list[dict], start_ms: int, end_ms: int) -> dict:
    expected = ((end_ms - start_ms) // HOUR_MS) + 1
    times = [int(r["open_time_ms"]) for r in rows]
    missing = []
    have = set(times)
    # Store a bounded sample of missing timestamps; count remains exact.
    missing_count = 0
    for ts in range(start_ms, end_ms + 1, HOUR_MS):
        if ts not in have:
            missing_count += 1
            if len(missing) < 24:
                missing.append(datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat())
    coverage = (len(rows) / expected) if expected else 0
    return {
        "rows": len(rows), "expected_rows": expected,
        "coverage_pct": round(coverage * 100, 4),
        "missing_hours": missing_count, "missing_sample_utc": missing,
        "first_open_utc": datetime.fromtimestamp(times[0] / 1000, timezone.utc).isoformat() if times else None,
        "last_open_utc": datetime.fromtimestamp(times[-1] / 1000, timezone.utc).isoformat() if times else None,
        "complete": missing_count == 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=730,
                        help="History length; default 730 days (24 months)")
    parser.add_argument("--output-dir", default="data/historical")
    parser.add_argument("--minimum-coverage", type=float, default=99.5)
    args = parser.parse_args()
    if args.days < 30 or args.days > 2000:
        parser.error("--days must be between 30 and 2000")

    # Only closed hourly candles: latest candle open time is the previous full hour.
    now_ms = int(time.time() * 1000)
    end_ms = (now_ms // HOUR_MS) * HOUR_MS - HOUR_MS
    start_ms = end_ms - (args.days * 24 - 1) * HOUR_MS
    output = Path(args.output_dir)
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "Bybit V5 public market kline API",
        "interval": "1h", "requested_days": args.days,
        "start_utc": datetime.fromtimestamp(start_ms / 1000, timezone.utc).isoformat(),
        "end_utc": datetime.fromtimestamp(end_ms / 1000, timezone.utc).isoformat(),
        "markets": {}, "passed": True,
    }

    for coin, symbol in SYMBOLS.items():
        report["markets"][coin] = {}
        for category, suffix in (("spot", "spot"), ("linear", "linear")):
            print(f"Fetching {category} {symbol} ...", flush=True)
            rows = fetch_market(category, symbol, start_ms, end_ms)
            path = output / f"{coin}_{suffix}_1h.csv.gz"
            write_gzip_csv(path, rows)
            result = audit(rows, start_ms, end_ms)
            result["file"] = str(path)
            result["category"] = category
            report["markets"][coin][category] = result
            if result["coverage_pct"] < args.minimum_coverage:
                report["passed"] = False
            print(f"  {result['rows']}/{result['expected_rows']} candles "
                  f"({result['coverage_pct']}% coverage)", flush=True)

    report_path = output / "backtest" / "hourly_backfill_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    sys.exit(main())
