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
        if macro_status and macro_status.get("status") not in ("ok", "degraded"):
            errors.append("Macro calendar status is " + str(macro_status.get("status")))
        etf = context.get("etf_flows") or {}
        for coin in ("BTC", "ETH", "SOL", "XRP"):
            meta = etf.get(coin) or {}
            if meta.get("error"):
                warnings.append(f"{coin} ETF flow source failed: {meta['error']}")
            elif not meta.get("updated_at"):
                warnings.append(f"{coin} ETF flow has no source timestamp")
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
