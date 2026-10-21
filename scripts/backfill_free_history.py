#!/usr/bin/env python3
"""Backfill free historical datasets for Crypto Checkpoint.

Downloads public daily Binance spot candles, Binance USD-M funding history,
public BTC ETF flow history, and timestamped crypto news archive samples.
Writes compressed CSV/JSONL files plus a coverage manifest. Missing/limited
sources are recorded as such; no synthetic values are generated.
"""
import csv, gzip, io, json, time, zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "historical"
OUT.mkdir(parents=True, exist_ok=True)
COINS = {"BTC":"BTCUSDT", "ETH":"ETHUSDT", "XRP":"XRPUSDT", "SOL":"SOLUSDT"}
START_YEAR = 2021
NOW = datetime.now(timezone.utc)

def get(url, timeout=30):
    req = Request(url, headers={"User-Agent":"CryptoCheckpoint-Free-Historical-Backfill/1.0"})
    with urlopen(req, timeout=timeout) as r:
        return r.read()

def save_gzip_csv(path, header, rows):
    with gzip.open(path, "wt", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(header); w.writerows(rows)

manifest = {
    "generated_at": NOW.isoformat(),
    "policy": "free/public sources only; no fabricated data; preserve source timestamps",
    "coverage": {},
    "limitations": [
        "Binance public archives may not contain every symbol for the full period.",
        "Public Binance open-interest statistics are limited to recent history (documented latest 30 days); no multi-year OI is inferred.",
        "ETF products only exist from their respective launch dates; flow history is not applicable before launch.",
        "News archive availability, pagination, rate limits and historical completeness depend on the public provider."
    ]
}

# Public Binance Vision monthly daily candles: no API key required.
for coin, symbol in COINS.items():
    rows = []
    months_ok = []
    for year in range(START_YEAR, NOW.year + 1):
        for month in range(1, 13):
            if (year, month) > (NOW.year, NOW.month):
                break
            ym = f"{year}-{month:02d}"
            url = f"https://data.binance.vision/data/spot/monthly/klines/{symbol}/1d/{symbol}-1d-{ym}.zip"
            try:
                raw = get(url, timeout=40)
                with zipfile.ZipFile(io.BytesIO(raw)) as z:
                    names = [n for n in z.namelist() if n.endswith(".csv")]
                    if not names: continue
                    with z.open(names[0]) as bf:
                        for line in io.TextIOWrapper(bf, encoding="utf-8"):
                            parts = next(csv.reader([line]))
                            if len(parts) >= 6 and parts[0].isdigit():
                                rows.append(parts[:12])
                months_ok.append(ym)
            except (HTTPError, URLError, TimeoutError, zipfile.BadZipFile, OSError):
                continue
            time.sleep(0.08)
    # Binance archive timestamps may be milliseconds or microseconds depending on
    # the data vintage. Normalize both open and close times to milliseconds.
    for row in rows:
        for idx in (0, 6):
            if len(row) > idx and row[idx].isdigit():
                value = int(row[idx])
                if value > 100_000_000_000_000:
                    row[idx] = str(value // 1000)
    rows.sort(key=lambda x: int(x[0]))
    path = OUT / f"{coin.lower()}_spot_1d.csv.gz"
    save_gzip_csv(path, ["open_time_ms","open","high","low","close","volume","close_time_ms","quote_volume","trades","taker_buy_base","taker_buy_quote","ignore"], rows)
    manifest["coverage"][f"{coin}_spot_1d"] = {
        "file": str(path.relative_to(ROOT)), "rows": len(rows),
        "first_timestamp_ms": rows[0][0] if rows else None,
        "last_timestamp_ms": rows[-1][0] if rows else None,
        "months_found": len(months_ok), "source": "Binance public data archive"
    }

# Prefer Binance's public monthly fundingRate archive; fall back to the REST endpoint.
# Archive coverage can be broader and avoids long pagination loops.
for coin, symbol in COINS.items():
    rows, months_ok = [], []
    for year in range(START_YEAR, NOW.year + 1):
        for month in range(1, 13):
            if (year, month) > (NOW.year, NOW.month): break
            ym = f"{year}-{month:02d}"
            url = f"https://data.binance.vision/data/futures/um/monthly/fundingRate/{symbol}/{symbol}-fundingRate-{ym}.zip"
            try:
                raw = get(url, timeout=40)
                with zipfile.ZipFile(io.BytesIO(raw)) as z:
                    for name in z.namelist():
                        if not name.endswith(".csv"): continue
                        with z.open(name) as bf:
                            for line in io.TextIOWrapper(bf, encoding="utf-8"):
                                parts = next(csv.reader([line]))
                                if len(parts) >= 2 and parts[0].isdigit():
                                    rows.append([parts[0], parts[1], parts[2] if len(parts)>2 else ""])
                months_ok.append(ym)
            except Exception:
                continue
            time.sleep(0.05)
    if not rows:
        start_ms, end_ms = int(datetime(START_YEAR,1,1,tzinfo=timezone.utc).timestamp()*1000), int(NOW.timestamp()*1000)
        cursor = start_ms
        while cursor < end_ms:
        url = f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={symbol}&startTime={cursor}&endTime={end_ms}&limit=1000"
        try:
            batch = json.loads(get(url).decode("utf-8"))
            if not isinstance(batch, list) or not batch: break
            rows.extend([[x.get("fundingTime"), x.get("fundingRate"), x.get("markPrice")] for x in batch])
            nxt = int(batch[-1]["fundingTime"]) + 1
            if nxt <= cursor: break
            cursor = nxt
            if len(batch) < 1000: break
            time.sleep(0.15)
        except Exception as e:
            manifest["coverage"][f"{coin}_funding"] = {"status":"partial_or_unavailable","error":str(e)[:180]}
            break
    uniq = {str(r[0]):r for r in rows}
    rows = sorted(uniq.values(), key=lambda x:int(x[0]))
    path = OUT / f"{coin.lower()}_funding.csv.gz"
    save_gzip_csv(path, ["funding_time_ms","funding_rate","mark_price"], rows)
    manifest["coverage"][f"{coin}_funding"] = {
        "file": str(path.relative_to(ROOT)), "rows": len(rows),
        "first_timestamp_ms": rows[0][0] if rows else None,
        "last_timestamp_ms": rows[-1][0] if rows else None,
        "source": "Binance public USD-M funding-rate archive/API",
        "archive_months_found": len(months_ok),
        "status": "available" if rows else "unavailable"
    }

# Public BTC ETF historical JSON; do not assume this source covers other assets.
try:
    raw = get("https://www.tftc.io/bitcoin-etf-flows/data.json")
    data = json.loads(raw.decode("utf-8"))
    (OUT / "btc_etf_flows.json").write_text(json.dumps({"source":"TFTC Bitcoin ETF tracker","retrieved_at":NOW.isoformat(),"data":data}, indent=2), encoding="utf-8")
    manifest["coverage"]["BTC_ETF_flows"] = {"file":"data/historical/btc_etf_flows.json","source":"TFTC public historical JSON","status":"available","note":"Coverage begins with spot ETF launch; verify schema before modelling."}
except Exception as e:
    manifest["coverage"]["BTC_ETF_flows"] = {"status":"unavailable","error":str(e)[:180]}

# News provider archive is queried in modest monthly windows to reduce load.
news_path = OUT / "crypto_news_archive.jsonl.gz"
count, dates = 0, []
try:
    with gzip.open(news_path, "wt", encoding="utf-8") as f:
        for year in range(max(START_YEAR, NOW.year-5), NOW.year+1):
            for month in range(1, 13):
                if (year, month) > (NOW.year, NOW.month): break
                start = f"{year}-{month:02d}-01"
                end = f"{year+1}-01-01" if month == 12 else f"{year}-{month+1:02d}-01"
                url = "https://cryptocurrency.cv/api/archive?start_date=" + start + "&end_date=" + end + "&limit=1000"
                try:
                    payload = json.loads(get(url, timeout=40).decode("utf-8"))
                    articles = payload.get("articles", payload.get("data", [])) if isinstance(payload, dict) else []
                    if isinstance(articles, list):
                        for article in articles:
                            if isinstance(article, dict):
                                f.write(json.dumps(article, ensure_ascii=False) + "\n"); count += 1
                                ts = article.get("pubDate") or article.get("published_at") or article.get("date")
                                if ts: dates.append(str(ts))
                except Exception:
                    continue
                time.sleep(0.12)
    manifest["coverage"]["news_archive"] = {
        "file":"data/historical/crypto_news_archive.jsonl.gz","rows":count,
        "first_timestamp":min(dates) if dates else None,"last_timestamp":max(dates) if dates else None,
        "source":"cryptocurrency.cv public archive","status":"available" if count else "unavailable",
        "note":"Provider pagination/schema may cap returned articles; row count is not proof of complete coverage."
    }
except Exception as e:
    manifest["coverage"]["news_archive"] = {"status":"unavailable","error":str(e)[:180]}

(OUT / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
print(json.dumps(manifest, indent=2))
