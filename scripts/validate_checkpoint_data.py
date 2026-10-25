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
        if macro_status and macro_status.get("status") == "degraded":
            warnings.append("Macro calendar is degraded: live official schedule parsing is not fully verified")
        if macro_status and macro_status.get("live_bls_events", 0) == 0:
            warnings.append("No live BLS events were parsed; fallback dates must be treated as unverified")
        fallback_events = [e for e in events if "fallback" in str(e.get("source", "")).lower()]
        live_bls_events = [e for e in events if "US BLS official monthly release schedule" == e.get("source")]
        if fallback_events and not live_bls_events:
            errors.append(f"Macro calendar relies on {len(fallback_events)} fallback events; live BLS schedule retrieval/parsing is unverified")
        etf = context.get("etf_flows") or {}
        for coin in ("BTC", "ETH", "SOL", "XRP"):
            meta = etf.get(coin) or {}
            if meta.get("error"):
                warnings.append(f"{coin} ETF flow source failed: {meta['error']}")
            elif not meta.get("updated_at"):
                warnings.append(f"{coin} ETF flow has no source timestamp")
            elif not meta.get("period_start") or not meta.get("period_end") or not isinstance(meta.get("sessions"), int):
                warnings.append(f"{coin} ETF flow metadata is missing its reporting period/session count")
            elif meta.get("sessions", 0) < 3:
                warnings.append(f"{coin} ETF flow uses only {meta.get('sessions')} sessions")
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
            handoff = paper.get("signal_handoff") or {}
            handoff_time = parse_time(handoff.get("updated_at"))
            if not handoff:
                errors.append("Paper-trade signal_handoff is missing; generated-signal reconciliation has not been recorded")
            elif not handoff_time:
                errors.append("Paper-trade signal_handoff has no valid timestamp")
            elif parse_time(paper.get("updated_at")) and abs((parse_time(paper.get("updated_at")) - handoff_time).total_seconds()) > 60:
                errors.append("Paper-trade signal_handoff timestamp does not match the latest ledger refresh")
            handoff_items = handoff.get("items") or []
            accepted_ids = {x.get("trade_id") for x in handoff_items if x.get("status") == "accepted_into_open_ledger"}
            open_ids = {x.get("trade_id") for x in opened}
            if handoff and accepted_ids != open_ids:
                errors.append("Signal handoff accepted trades do not match open paper-trade ledger entries")
            if len(opened) > 1:
                errors.append(f"Paper-trade ledger exceeds the configured one-open-trade cap: {len(opened)} open")
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
