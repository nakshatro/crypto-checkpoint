#!/usr/bin/env python3
"""Second-generation, event-driven Crypto Checkpoint research backtest.

Requires archived 1H OHLCV files: data/historical/{coin}_spot_1h.csv.gz
Columns: open_time_ms, open, high, low, close, volume
Optional funding: data/historical/{coin}_funding.csv.gz with funding_time_ms,funding_rate.

This is a CMI-inspired OHLCV strategy benchmark, NOT a historical replay of the
five-layer production model: historical OI/CVD/liquidations/order-book/news/ETF
snapshots are not archived in the repository.
"""
import csv, gzip, json, math
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timezone
from statistics import mean, pstdev

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "historical"
OUT = DATA / "backtest"
COINS = ("BTC", "ETH", "XRP", "SOL")
START_CAPITAL = 1000.0
RISK_PER_TRADE = 0.005
MAX_NOTIONAL_PCT = 0.10
MAX_PORTFOLIO_RISK_PCT = 0.02
MAX_HOLD_HOURS = 72
STOP_ATR = 1.5
TARGET_R = 2.0
FEE_BPS = {"spot": 10.0, "futures": 5.0}
SLIPPAGE_BPS = 5.0
FUNDING_INTERVAL_HOURS = 8

def read_rows(path):
    if not path.exists():
        return []
    rows = {}
    with gzip.open(path, "rt", newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                ts = int(r.get("open_time_ms") or r.get("timestamp") or r.get("ts"))
                if ts > 100_000_000_000_000: ts //= 1000
                o,h,l,c,v = [float(r[k]) for k in ("open","high","low","close","volume")]
                if ts > 0 and min(o,h,l,c) > 0 and all(math.isfinite(x) for x in (o,h,l,c,v)):
                    rows[ts] = {"t":ts,"o":o,"h":h,"l":l,"c":c,"v":max(0,v)}
            except (KeyError, TypeError, ValueError):
                continue
    return [rows[k] for k in sorted(rows)]

def read_funding(coin):
    path = DATA / f"{coin.lower()}_funding.csv.gz"
    out = {}
    if not path.exists(): return out
    with gzip.open(path, "rt", newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                ts=int(r.get("funding_time_ms") or r.get("timestamp"))
                if ts > 100_000_000_000_000: ts //= 1000
                out[ts]=float(r["funding_rate"])
            except (KeyError, TypeError, ValueError): continue
    return out

def ema(values, n):
    k=2/(n+1); result=[]; cur=None
    for x in values:
        cur=x if cur is None else x*k+cur*(1-k)
        result.append(cur)
    return result

def rsi(values, n=14):
    if len(values) < n+1: return 50.0
    dif=[values[i]-values[i-1] for i in range(len(values)-n,len(values))]
    up=sum(max(x,0) for x in dif)/n; down=sum(max(-x,0) for x in dif)/n
    return 100.0 if down == 0 else 100-100/(1+up/down)

def atr(rows, n=14):
    if len(rows) < n+1: return None
    tr=[]
    for i in range(len(rows)-n,len(rows)):
        p=rows[i-1]["c"]; x=rows[i]
        tr.append(max(x["h"]-x["l"],abs(x["h"]-p),abs(x["l"]-p)))
    return mean(tr)

def signal(rows, strategy):
    """Signal is computed only from completed candles through rows[-1]."""
    if len(rows) < 60: return 0, None
    closes=[x["c"] for x in rows]; vols=[x["v"] for x in rows]
    e20=ema(closes,20)[-1]; e50=ema(closes,50)[-1]
    e50_prev=ema(closes[:-6],50)[-1]
    a=atr(rows)
    if not a or a <= 0: return 0, None
    last=rows[-1]; prior=rows[-25:-1]; volbase=mean(vols[-25:-1]) if len(vols)>=25 else 0
    vr=last["v"]/volbase if volbase else 0
    ret24=(closes[-1]/closes[-25]-1)*100
    r=rsi(closes)
    if strategy == "trend":
        long=last["c"]>e20>e50 and e50>e50_prev and last["c"]>max(x["h"] for x in prior[-12:]) and vr>=1.05
        short=last["c"]<e20<e50 and e50<e50_prev and last["c"]<min(x["l"] for x in prior[-12:]) and vr>=1.05
    else:
        # Capitulation/distribution WATCH becomes a trade only after a close reclaim/rejection.
        prior_low=min(x["l"] for x in prior[-12:]); prior_high=max(x["h"] for x in prior[-12:])
        long=ret24<=-4 and r<=32 and last["c"]>last["o"] and last["c"]>prior_low and vr>=1.35
        short=ret24>=4 and r>=68 and last["c"]<last["o"] and last["c"]<prior_high and vr>=1.35
    return (1 if long else -1 if short else 0), a

def run_portfolio(data, funding, mode, strategy):
    capital=START_CAPITAL; peak=capital; max_dd=0.0; positions={}; trades=[]; curve=[]
    timestamps=sorted(set(t for rows in data.values() for t in rows))
    indexes={c:{x["t"]:i for i,x in enumerate(rows)} for c,rows in data.items()}
    last_rows={c:[] for c in data}
    fee=FEE_BPS[mode]/10000; slip=SLIPPAGE_BPS/10000
    for ts in timestamps:
        # Process open positions at this candle, with conservative stop-first intrabar ordering.
        for coin,pos in list(positions.items()):
            i=indexes[coin].get(ts)
            if i is None or i==0: continue
            bar=data[coin][i]; side=pos["side"]; exit_px=None; reason=None
            # Stage 2: add the remaining half at this bar's open only if the
            # previous completed candle still confirms the original direction
            # and price has not already crossed the invalidation stop.
            if pos.get("stage2_pending"):
                confirmed,_=signal(data[coin][:i],strategy)
                valid_price=(bar["o"]>pos["stop"]) if side==1 else (bar["o"]<pos["stop"])
                if confirmed==side and valid_price:
                    add_notional=pos["target_notional"]-pos["notional"]
                    add_px=bar["o"]
                    old_value=pos["entry_avg"]*pos["notional"]
                    pos["entry_avg"]=(old_value+add_px*add_notional)/(pos["notional"]+add_notional)
                    pos["notional"]+=add_notional
                    pos["entry_cost"]+=add_notional*(fee+slip)
                pos["stage2_pending"]=False
            if side==1:
                if bar["l"]<=pos["stop"]: exit_px=pos["stop"]; reason="stop"
                elif bar["h"]>=pos["target"]: exit_px=pos["target"]; reason="target"
            else:
                if bar["h"]>=pos["stop"]: exit_px=pos["stop"]; reason="stop"
                elif bar["l"]<=pos["target"]: exit_px=pos["target"]; reason="target"
            if reason is None and (ts-pos["entry_ts"])/3600000>=MAX_HOLD_HOURS:
                exit_px=bar["c"]; reason="time"
            if exit_px is not None:
                gross=side*(exit_px-pos["entry_avg"])/pos["entry_avg"]*pos["notional"]
                exit_cost=pos["notional"]*(fee+slip)
                net=gross-pos["entry_cost"]-exit_cost+pos["funding_pnl"]
                capital+=net
                trades.append({"coin":coin,"mode":mode,"strategy":strategy,"side":"LONG" if side==1 else "SHORT",
                    "entry_time":datetime.fromtimestamp(pos["entry_ts"]/1000,timezone.utc).isoformat(),
                    "exit_time":datetime.fromtimestamp(ts/1000,timezone.utc).isoformat(),"exit_reason":reason,
                    "entry_price":round(pos["entry_avg"],8),"exit_price":round(exit_px,8),
                    "notional_usdt":round(pos["notional"],2),"gross_pnl_usdt":round(gross,4),
                    "cost_usdt":round(pos["entry_cost"]+exit_cost,4),"funding_pnl_usdt":round(pos["funding_pnl"],4),
                    "net_pnl_usdt":round(net,4)})
                del positions[coin]
                continue
            # Funding is charged/credited only on timestamps present in the historical funding file.
            rate=funding.get(coin,{}).get(ts)
            if mode=="futures" and rate is not None:
                pos["funding_pnl"] += -side*pos["notional"]*rate
        # Generate signals from prior completed candle; execute at current candle open.
        for coin,rows in data.items():
            i=indexes[coin].get(ts)
            if i is None or i < 60 or coin in positions or i==0: continue
            hist=rows[:i]
            side,a=signal(hist,strategy)
            if mode=="spot" and side<0: continue
            if not side or not a: continue
            entry=rows[i]["o"]
            stop=entry-side*STOP_ATR*a
            target=entry+side*TARGET_R*(STOP_ATR*a)
            risk_per_unit=abs(entry-stop)/entry
            risk_budget=capital*RISK_PER_TRADE
            notional=min(capital*MAX_NOTIONAL_PCT,risk_budget/max(risk_per_unit,1e-9))
            # Keep aggregate open stop-risk within the portfolio risk cap.
            open_risk=sum(p["risk_usdt"] for p in positions.values())
            notional=min(notional,max(0,capital*MAX_PORTFOLIO_RISK_PCT-open_risk)/max(risk_per_unit,1e-9))
            if notional < 5: continue
            entry_cost=notional*(fee+slip)
            first_tranche=notional*0.5
            entry_cost=first_tranche*(fee+slip)
            positions[coin]={"side":side,"entry_ts":ts,"entry_avg":entry,"notional":first_tranche,
                "target_notional":notional,"stage2_pending":True,
                "stop":stop,"target":target,"entry_cost":entry_cost,"funding_pnl":0.0,
                "risk_usdt":notional*risk_per_unit}
        unrealized=0.0
        for coin,pos in positions.items():
            i=indexes[coin].get(ts)
            if i is not None:
                px=data[coin][i]["c"]
                unrealized+=pos["side"]*(px-pos["entry_avg"])/pos["entry_avg"]*pos["notional"]
        equity=capital+unrealized; peak=max(peak,equity); max_dd=max(max_dd,(peak-equity)/peak if peak else 0)
        curve.append({"timestamp_ms":ts,"equity_usdt":round(equity,4)})
    # Force-close remaining positions at last available close.
    for coin,pos in list(positions.items()):
        bar=data[coin][-1]; px=bar["c"]; side=pos["side"]
        gross=side*(px-pos["entry_avg"])/pos["entry_avg"]*pos["notional"]
        exit_cost=pos["notional"]*(fee+slip); net=gross-pos["entry_cost"]-exit_cost+pos["funding_pnl"]
        capital+=net
        trades.append({"coin":coin,"mode":mode,"strategy":strategy,"side":"LONG" if side==1 else "SHORT",
            "entry_time":datetime.fromtimestamp(pos["entry_ts"]/1000,timezone.utc).isoformat(),
            "exit_time":datetime.fromtimestamp(bar["t"]/1000,timezone.utc).isoformat(),"exit_reason":"end_of_data",
            "entry_price":round(pos["entry_avg"],8),"exit_price":round(px,8),"notional_usdt":round(pos["notional"],2),
            "gross_pnl_usdt":round(gross,4),"cost_usdt":round(pos["entry_cost"]+exit_cost,4),
            "funding_pnl_usdt":round(pos["funding_pnl"],4),"net_pnl_usdt":round(net,4)})
    wins=[x["net_pnl_usdt"] for x in trades if x["net_pnl_usdt"]>0]
    losses=[x["net_pnl_usdt"] for x in trades if x["net_pnl_usdt"]<0]
    total=sum(x["net_pnl_usdt"] for x in trades)
    rets=[(curve[i]["equity_usdt"]/curve[i-1]["equity_usdt"]-1) for i in range(1,len(curve)) if curve[i-1]["equity_usdt"]>0]
    sharpe=(mean(rets)/pstdev(rets)*math.sqrt(24*365)) if len(rets)>1 and pstdev(rets)>0 else None
    return {"starting_capital_usdt":START_CAPITAL,"ending_capital_usdt":round(START_CAPITAL+total,2),
        "net_pnl_usdt":round(total,2),"return_pct":round(total/START_CAPITAL*100,2),
        "trades":len(trades),"win_rate_pct":round(100*len(wins)/len(trades),2) if trades else None,
        "profit_factor":round(sum(wins)/abs(sum(losses)),3) if losses else ("infinite" if wins else None),
        "max_drawdown_pct":round(max_dd*100,2),"sharpe_annualized_hourly":round(sharpe,3) if sharpe is not None else None,
        "open_positions_at_end":0,"assumptions":{"risk_per_trade_pct":RISK_PER_TRADE*100,
        "max_portfolio_stop_risk_pct":MAX_PORTFOLIO_RISK_PCT*100,"max_notional_per_position_pct":MAX_NOTIONAL_PCT*100,
        "stop_atr":STOP_ATR,"target_R":TARGET_R,"max_holding_hours":MAX_HOLD_HOURS,
        "fee_bps_per_side":FEE_BPS[mode],"slippage_bps_per_side":SLIPPAGE_BPS,
        "futures_leverage":1,"spot_shorting":False,"staged_entries":"50% at next-bar open; remaining 50% at the following bar open only if the prior completed candle still confirms the signal and price has not crossed the stop"},
        "trades_detail":trades,"equity_curve":curve}

def main():
    data={c:read_rows(DATA/f"{c.lower()}_spot_1h.csv.gz") for c in COINS}
    coverage={c:{"rows":len(v),"first":v[0]["t"] if v else None,"last":v[-1]["t"] if v else None} for c,v in data.items()}
    missing=[c for c,v in data.items() if len(v)<500]
    if missing:
        report={"status":"blocked_missing_hourly_history","required_file_pattern":"data/historical/{coin}_spot_1h.csv.gz",
            "minimum_rows_per_coin":500,"coverage":coverage,"missing_coins":missing,
            "existing_available_history":"Repository currently contains daily spot candles and funding history, but no archived 1H OHLCV files. This script intentionally refuses to report a strategy result without sufficient hourly data.",
            "production_model_replay":"Not possible from current archive: historical OI/CVD/liquidation/order-book/on-chain/news/ETF feature snapshots are not stored with forecast timestamps."}
    else:
        funding={c:read_funding(c) for c in COINS}
        report={"status":"completed_ohlcv_proxy_only","generated_at":datetime.now(timezone.utc).isoformat(),
            "scope":"CMI-inspired OHLCV reversal and trend strategies; not a replay of the live five-layer model.",
            "data_coverage":coverage,"modes":{}}
        for mode in ("spot","futures"):
            report["modes"][mode]={}
            for strategy in ("reversal","trend"):
                report["modes"][mode][strategy]=run_portfolio(data,funding,mode,strategy)
        report["limitations"]=["Spot is long-only; futures supports long and short at 1x notional, no leverage.",
            "Funding is applied only where archived funding timestamps exactly match hourly bars; missing funding rows are not interpolated.",
            "No historical OI, taker CVD, liquidations, order-book, ETF, on-chain, macro/news snapshots; production five-layer forecast cannot be replayed.",
            "No staged second tranche in this baseline implementation; next iteration should add a separately tested scale-in rule.",
            "Intrabar stop is assumed to execute before target if both are touched in one candle; gaps may still worsen fills."]
    OUT.mkdir(parents=True,exist_ok=True)
    (OUT/"backtest_v2_report.json").write_text(json.dumps(report,indent=2,allow_nan=False),encoding="utf-8")
    print(json.dumps({"status":report["status"],"report":"data/historical/backtest/backtest_v2_report.json",
        "coverage":coverage,"modes":list(report.get("modes",{}))},indent=2))
if __name__=="__main__": main()
