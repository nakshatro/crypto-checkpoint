#!/usr/bin/env python3
"""Validate generated Crypto Checkpoint data files without changing trading state.

Exit non-zero on missing/stale snapshots or absent macro/ETF source metadata.
This is a diagnostic guard; it never invents replacement market data.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / "cloud-predictor" / "docs" / "data"
MAX_AGE_HOURS = 8

def parse_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None

def load_json(name):
    path = DATA / name
    if not path.exists():
        raise ValueError(f"Missing required file: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read {path.name}: {exc}") from exc

def main():
    errors = []
    warnings = []
    now = datetime.now(timezone.utc)
    try:
        latest = load_json("latest.json")
        updated = parse_time(latest.get("updated_at"))
        if not updated:
            errors.append("latest.json has no valid updated_at timestamp")
        elif (now - updated).total_seconds() > MAX_AGE_HOURS * 3600:
            errors.append(f"latest.json is stale: {(now-updated).total_seconds()/3600:.1f} hours old")
        coins = latest.get("coins") or {}
        available = [c for c in ("BTC", "ETH", "XRP", "SOL") if isinstance(coins.get(c), dict) and not coins[c].get("error")]
        if len(available) < 4:
            warnings.append("Only these assets have valid latest data: " + (", ".join(available) or "none"))
        context = latest.get("model_context") or {}
        events = context.get("events") or []
        macro_status = context.get("macro_status") or {}
        if not events:
            errors.append("No macro events in latest snapshot; this is not proof that no events are scheduled")
        if not macro_status:
            errors.append("Macro calendar status is missing from latest.json; feed health cannot be verified")
        elif macro_status.get("status") not in ("ok", "degraded"):
            errors.append("Macro calendar status is " + str(macro_status.get("status")))
        if events and all("fallback" in str(e.get("source", "")).lower() for e in events):
            errors.append("All macro events come from fallback data; live calendar retrieval/parsing is unverified")
        etf = context.get("etf_flows") or {}
        for coin in ("BTC", "ETH", "SOL", "XRP"):
            meta = etf.get(coin) or {}
            if meta.get("error"):
                warnings.append(f"{coin} ETF flow source failed: {meta['error']}")
            elif not meta.get("updated_at"):
                warnings.append(f"{coin} ETF flow has no source timestamp")
        try:
            paper = load_json("paper_trades.json")
            trades = paper.get("trades") or []
            closed = [t for t in trades if t.get("status") == "closed"]
            opened = [t for t in trades if t.get("status") == "open"]
            summary = paper.get("summary") or {}
            if summary.get("closed_trades") != len(closed):
                errors.append(f"Paper-trade closed count mismatch: summary={summary.get('closed_trades')} ledger={len(closed)}")
            if summary.get("open_trades") != len(opened):
                errors.append(f"Paper-trade open count mismatch: summary={summary.get('open_trades')} ledger={len(opened)}")
            wins = sum(1 for t in closed if isinstance(t.get("net_return_pct"), (int, float)) and t["net_return_pct"] > 0)
            if summary.get("wins_net") != wins:
                errors.append(f"Paper-trade win count mismatch: summary={summary.get('wins_net')} ledger={wins}")
            for t in trades:
                if t.get("status") == "closed" and not t.get("exit_time_utc"):
                    warnings.append(f"Closed trade missing exit time: {t.get('trade_id', 'unknown trade')}")
                if t.get("status") == "open" and t.get("exit_time_utc"):
                    errors.append(f"Open trade has exit time: {t.get('trade_id', 'unknown trade')}")
            paper_updated = parse_time(paper.get("updated_at"))
            if not paper_updated:
                errors.append("paper_trades.json has no valid updated_at timestamp")
            elif (now - paper_updated).total_seconds() > MAX_AGE_HOURS * 3600:
                errors.append(f"paper_trades.json is stale: {(now-paper_updated).total_seconds()/3600:.1f} hours old")
        except ValueError as exc:
            errors.append(str(exc))
    except ValueError as exc:
        errors.append(str(exc))

    for item in warnings:
        print("WARNING:", item)
    for item in errors:
        print("ERROR:", item)
    if errors:
        print(f"Checkpoint data validation FAILED ({len(errors)} errors, {len(warnings)} warnings)")
        return 1
    print(f"Checkpoint data validation passed ({len(warnings)} warnings)")
    return 0

if __name__ == "__main__":
    sys.exit(main())
