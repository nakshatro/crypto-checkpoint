#!/usr/bin/env python3
"""Market-structure validation and cautious capitulation/distribution heuristics."""
from datetime import datetime, timezone
import json, math
from pathlib import Path

def _num(x, default=None):
    try:
        v=float(x)
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default

def validate_levels(rows, mtf, lookback=72):
    """Classify active S/R levels against recent completed hourly candles."""
    recent=rows[-lookback:]
    out={"lookback_hours":len(recent),"levels":[],"summary":{"active":0,"tested":0,"broken":0,"reclaimed":0}}
    if not recent:
        return out
    for side in ("support","resistance"):
        candidates=[]
        for tf,data in (mtf.get("timeframes") or {}).items():
            for level in data.get(side,[]):
                candidates.append({"timeframe":tf,"side":side,**level})
        for level in candidates:
            p=_num(level.get("price"))
            if not p: continue
            # ATR-scaled zone width, bounded to avoid treating broad areas as precise levels.
            atrs=[max(0.0,_num(x.get("h"),0)-_num(x.get("l"),0)) for x in recent[-24:]]
            atr=sum(atrs)/max(1,len(atrs))
            half=max(p*0.0015,min(p*0.008,atr*0.35))
            lo,hi=p-half,p+half
            touches=holds=breaks=reclaims=0
            breakout_retests=failed_retests=0
            retest_events=[]
            for i,c in enumerate(recent):
                h,l,cl=_num(c.get("h"),0),_num(c.get("l"),0),_num(c.get("c"),0)
                if h>=lo and l<=hi:
                    touches+=1
                    if side=="support" and cl>=lo: holds+=1
                    if side=="resistance" and cl<=hi: holds+=1
                if i>0:
                    prev=_num(recent[i-1].get("c"),cl)
                    if side=="support" and prev>=lo and cl<lo: breaks+=1
                    elif side=="resistance" and prev<=hi and cl>hi: breaks+=1
                    if side=="support" and prev<lo and cl>hi: reclaims+=1
                    elif side=="resistance" and prev>hi and cl<lo: reclaims+=1
                    # Check the next six completed candles after a close-break for a retest.
                    if side=="support" and prev>=lo and cl<lo:
                        future=recent[i+1:i+7]
                        for ret in future:
                            rh,rl,rc=_num(ret.get("h"),0),_num(ret.get("l"),0),_num(ret.get("c"),0)
                            if rh>=lo:
                                if rc<lo:
                                    breakout_retests+=1; retest_events.append("bearish_breakdown_retest")
                                elif rc>hi:
                                    failed_retests+=1; retest_events.append("failed_breakdown_reclaim")
                                break
                    elif side=="resistance" and prev<=hi and cl>hi:
                        future=recent[i+1:i+7]
                        for ret in future:
                            rh,rl,rc=_num(ret.get("h"),0),_num(ret.get("l"),0),_num(ret.get("c"),0)
                            if rl<=hi:
                                if rc>hi:
                                    breakout_retests+=1; retest_events.append("bullish_breakout_retest")
                                elif rc<lo:
                                    failed_retests+=1; retest_events.append("failed_breakout_rejection")
                                break
            status="broken" if breaks and breaks>=max(1,holds) else ("tested" if touches else "active")
            out["summary"][status]=out["summary"].get(status,0)+1
            if reclaims:
                out["summary"]["reclaimed"]+=1
            out["levels"].append({"price":p,"side":side,"timeframe":level.get("timeframe"),"zone_low":round(lo,8),"zone_high":round(hi,8),"touches_lookback":touches,"holds_lookback":holds,"close_breaks_lookback":breaks,"reclaims_lookback":reclaims,"breakout_retests_lookback":breakout_retests,"failed_retests_lookback":failed_retests,"retest_events":retest_events[-3:],"status":status})
    return out

def detect_market_phase(rows, derivatives, sentiment=None):
    """Rule-based warning signals. Not on-chain whale-flow attribution or a trade signal."""
    sentiment=sentiment or {}
    if len(rows)<30:
        return {"phase":"INSUFFICIENT_DATA","confidence":"low","score":0,"evidence":[],"missing":["at least 30 completed hourly candles"]}
    closes=[_num(x.get("c"),0) for x in rows]
    vols=[_num(x.get("v"),0) for x in rows]
    last=rows[-1]; price=closes[-1]
    ret24=(price/closes[-25]-1)*100 if closes[-25] else 0
    ret72=(price/closes[-73]-1)*100 if len(closes)>=73 and closes[-73] else None
    base=sorted(vols[-169:-25]) if len(vols)>=169 else sorted(vols[:-24])
    med=base[len(base)//2] if base else 0
    volratio=(sum(vols[-24:])/24/med) if med>0 else None
    hi7=max(_num(x.get("h"),0) for x in rows[-168:])
    lo7=min(_num(x.get("l"),price) for x in rows[-168:])
    pos7=(price-lo7)/(hi7-lo7) if hi7>lo7 else .5
    highs=[_num(x.get("h"),0) for x in rows[-24:]]
    lower_highs=sum(1 for a,b in zip(highs,highs[1:]) if b<a)
    oi=_num(derivatives.get("oi_change_24h"))
    cvd=_num(derivatives.get("taker_cvd_24h"))
    liq=derivatives.get("recent_liquidations_usd") or {}
    longliq=_num(liq.get("longs")); shortliq=_num(liq.get("shorts"))
    fear=_num(sentiment.get("value") if isinstance(sentiment,dict) else sentiment)
    evidence_cap=[]; evidence_dist=[]; missing=[]
    if ret24<=-4: evidence_cap.append("24h price decline >=4%")
    if ret24<=-7: evidence_cap.append("24h price decline >=7% (strong)")
    if volratio is not None and volratio>=1.8: evidence_cap.append("24h average volume >=1.8x prior baseline")
    elif volratio is None: missing.append("volume baseline")
    if oi is not None and oi<=-0.03: evidence_cap.append("open interest down >=3% in 24h")
    if longliq is not None and shortliq is not None and longliq>=max(shortliq*2,1): evidence_cap.append("long liquidations materially exceed short liquidations")
    if cvd is not None and cvd<-.12: evidence_cap.append("negative 24h taker CVD")
    if fear is not None and fear<=20: evidence_cap.append("extreme-fear sentiment")
    if pos7<=.18: evidence_cap.append("price in bottom 18% of 7-day range")
    if pos7>=.78 and volratio is not None and volratio>=1.5: evidence_dist.append("price near 7-day high with elevated volume")
    if ret24<=1.0 and volratio is not None and volratio>=1.5 and pos7>=.60: evidence_dist.append("elevated turnover with limited upside progress near range highs")
    if lower_highs>=15 and pos7>=.55: evidence_dist.append("lower highs in recent 24-hour candles while price remains in upper range")
    if cvd is not None and cvd<-.10 and pos7>=.60: evidence_dist.append("negative taker CVD near upper range")
    if oi is not None and oi>=.02 and ret24<=1.0 and pos7>=.60: evidence_dist.append("open interest rising without corresponding price advance")
    # Classify only when multiple independent clues align; otherwise remain neutral.
    cap_score=min(100,round(len(evidence_cap)/6*100))
    dist_score=min(100,round(len(evidence_dist)/4*100))
    if len(evidence_cap)>=3 and cap_score>=50 and len(evidence_cap)>len(evidence_dist):
        phase="CAPITULATION_WATCH"
    elif len(evidence_dist)>=3 and dist_score>=60 and len(evidence_dist)>len(evidence_cap):
        phase="DISTRIBUTION_RISK"
    elif len(evidence_cap)>=2 and len(evidence_dist)>=2:
        phase="MIXED_STRESS"
    else:
        phase="NO_CONFIRMED_PATTERN"
    return {"phase":phase,"capitulation_score":cap_score,"distribution_score":dist_score,"confidence":"low" if len(missing)>=2 else ("medium" if max(len(evidence_cap),len(evidence_dist))>=4 else "low"),"evidence":{"capitulation":evidence_cap,"distribution":evidence_dist},"metrics":{"return_24h_pct":round(ret24,3),"return_72h_pct":round(ret72,3) if ret72 is not None else None,"volume_ratio_vs_baseline":round(volratio,3) if volratio is not None else None,"position_in_7d_range_pct":round(pos7*100,1),"oi_change_24h_pct":round(oi*100,2) if oi is not None else None,"taker_cvd_24h":round(cvd,3) if cvd is not None else None,"long_liquidations_usd":longliq,"short_liquidations_usd":shortliq,"fear_greed":fear},"missing":missing,"disclaimer":"Heuristic market-stress/distribution proxy, not proof of whale selling, a bottom/top, or a trade signal."}

def update_level_history(path, now, coins, max_snapshots=500):
    """Append compact level snapshots; compare repeated zones without hindsight leakage."""
    path=Path(path)
    try: store=json.loads(path.read_text(encoding="utf-8"))
    except Exception: store={"snapshots":[]}
    snapshots=store.get("snapshots",[])
    snap={"updated_at":now,"coins":{}}
    for coin,payload in coins.items():
        sr=payload.get("support_resistance") or {}
        val=payload.get("level_validation") or {}
        snap["coins"][coin]={"price":payload.get("price"),"levels":[{"side":x.get("side"),"timeframe":x.get("timeframe"),"price":x.get("price"),"status":x.get("status"),"touches":x.get("touches_lookback"),"breaks":x.get("close_breaks_lookback"),"retests":x.get("breakout_retests_lookback"),"failed_retests":x.get("failed_retests_lookback")} for x in val.get("levels",[])]}
    snapshots.append(snap)
    store={"updated_at":now,"method":"Append-only snapshots of detected levels and rolling 72-hour OHLC validation. Historical snapshots are descriptive, not a fully independent backtest.","snapshots":snapshots[-max_snapshots:]}
    path.write_text(json.dumps(store,indent=2,allow_nan=False),encoding="utf-8")
    return store
