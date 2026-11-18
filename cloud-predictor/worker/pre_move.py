#!/usr/bin/env python3
"""Pre-move decision engine: early watchlists plus price-confirmed triggers.

This is a transparent rules/ensemble layer over the existing quantitative model.
It does not claim to predict a pump or dump with certainty.
"""
import math

def _n(x):
    try:
        v=float(x)
        return v if math.isfinite(v) else None
    except (TypeError,ValueError):
        return None

def _nearest(levels, side, price):
    items=[]
    for tf,data in (levels.get("timeframes") or {}).items():
        for row in data.get(side,[]) or []:
            p=_n(row.get("price"))
            if p is not None and ((side=="resistance" and p>price) or (side=="support" and p<price)):
                items.append((abs(p-price),p,tf,row.get("strength")))
    return min(items,key=lambda x:x[0]) if items else None

def evaluate_pre_move(coin, rows, derivatives, model, forecasts, mtf, market_phase):
    """Return LONG/SHORT watch or confirmed trigger, with evidence and invalidation."""
    if len(rows)<30:
        return {"action":"WAIT","stage":"INSUFFICIENT_DATA","confidence":"low","score_long":None,"score_short":None,"reasons":[],"missing":["30 completed hourly candles required"],"disclaimer":"Not a guaranteed prediction or financial advice."}
    closes=[_n(x.get("c")) for x in rows]
    highs=[_n(x.get("h")) for x in rows]
    lows=[_n(x.get("l")) for x in rows]
    vols=[_n(x.get("v")) or 0 for x in rows]
    price=closes[-1]
    if not price or any(x is None for x in closes[-25:]):
        return {"action":"WAIT","stage":"BAD_PRICE_DATA","confidence":"low","score_long":None,"score_short":None,"reasons":[],"missing":["valid recent candle closes"],"disclaimer":"Not a guaranteed prediction or financial advice."}
    # Use fixed, explicit evidence points. Missing inputs do not count either way.
    long_pts=short_pts=0.0; evidence=[]; missing=[]
    def add(name,bias,weight,detail):
        nonlocal long_pts,short_pts
        if bias>0: long_pts+=weight
        elif bias<0: short_pts+=weight
        if bias: evidence.append({"name":name,"bias":"bullish" if bias>0 else "bearish","weight":weight,"detail":detail})
    h24=(model.get("horizons") or {}).get("24h") or {}
    h4=(model.get("horizons") or {}).get("4h") or {}
    p=_n(h24.get("p_up"))
    if p is not None: add("five_layer_model",1 if p>=.55 else -1 if p<=.45 else 0,22,f"24h model P(up)={p:.3f}")
    else: missing.append("24h model probability")
    f24=next((x for x in forecasts or [] if str(x.get("horizon","")).lower()=="24h"),{})
    u=_n(f24.get("up_frequency_pct"))
    if u is not None: add("historical_analogue",1 if u>=56 else -1 if u<=44 else 0,12,f"24h analogue up-frequency={u:.1f}%")
    else: missing.append("24h analogue frequency")
    # Trend/momentum on completed hourly candles.
    ma6=sum(closes[-6:])/6; ma24=sum(closes[-24:])/24
    r4=(price/closes[-5]-1)*100; r12=(price/closes[-13]-1)*100
    trend_bias=1 if price>ma6>ma24 and r4>0 else -1 if price<ma6<ma24 and r4<0 else 0
    add("hourly_trend",trend_bias,16,f"4h return {r4:+.2f}%; 12h {r12:+.2f}%; price vs MA6/MA24")
    d=derivatives or {}
    cvd=_n(d.get("taker_cvd_24h"))
    if cvd is not None: add("taker_flow",1 if cvd>=.08 else -1 if cvd<=-.08 else 0,12,f"24h taker CVD {cvd:+.3f}")
    else:
        tr=d.get("taker_buy_sell_ratio") or {}
        ratio=_n(tr.get("buySellRatio")) if isinstance(tr,dict) else None
        if ratio is not None: add("recent_taker_flow",1 if ratio>=1.15 else -1 if ratio<=.87 else 0,7,f"recent buy/sell ratio {ratio:.3f}; sample is not a full 24h aggregate")
        else: missing.append("taker flow")
    oi=_n(d.get("oi_change_24h"))
    if oi is not None:
        # Rising OI confirms participation only when price direction agrees.
        add("open_interest",1 if oi>=.02 and r4>0 else -1 if oi>=.02 and r4<0 else 0,8,f"24h OI change {oi*100:+.2f}%")
    else: missing.append("24h open-interest change")
    book=_n(d.get("book_imbalance"))
    if book is not None: add("order_book",1 if book>=.12 else -1 if book<=-.12 else 0,6,f"bid/ask notional imbalance {book:+.2f}")
    else: missing.append("order-book imbalance")
    phase=(market_phase or {}).get("phase")
    if phase=="DISTRIBUTION_RISK": add("distribution_warning",-1,10,"distribution-risk heuristic active; wait for price confirmation")
    elif phase=="CAPITULATION_WATCH": add("capitulation_warning",0,0,"capitulation watch is not an automatic long; require reclaim/confirmation")
    # Recent completed candle volume relative to preceding 24h.
    prev=vols[-48:-24]; recent=vols[-24:]
    vr=(sum(recent)/24)/(sum(prev)/len(prev)) if prev and sum(prev)>0 else None
    if vr is not None:
        if vr>=1.35:
            # Volume validates participation but does not decide direction.
            evidence.append({"name":"volume_expansion","bias":"confirming","weight":5,"detail":f"24h average volume {vr:.2f}x previous 24h"})
    else: missing.append("comparable volume baseline")
    total=22+12+16+12+8+6+10
    coverage=100*(sum(x["weight"] for x in evidence if x["bias"] in ("bullish","bearish"))+sum(x["weight"] for x in evidence if x["name"]=="volume_expansion"))/total
    # Directional score normalised over the maximum directional weight.
    directional=86.0
    long_score=round(100*long_pts/directional,1); short_score=round(100*short_pts/directional,1)
    edge=long_pts-short_pts
    # Breakout/breakdown trigger is separate from the early warning.
    prev_high=max(highs[-7:-1]); prev_low=min(lows[-7:-1])
    last_high=highs[-1]; last_low=lows[-1]
    vol_now=vols[-1]; vol_base=sum(vols[-25:-1])/24 if sum(vols[-25:-1]) else 0
    vol_confirm=bool(vol_base and vol_now>=1.15*vol_base)
    long_trigger=closes[-1]>prev_high and vol_confirm
    short_trigger=closes[-1]<prev_low and vol_confirm
    action="WAIT"; stage="NO_EDGE"
    if long_pts>=43 and edge>=18:
        action="LONG"; stage="CONFIRMED_TRIGGER" if long_trigger else "EARLY_WATCH"
    elif short_pts>=43 and edge<=-18:
        action="SHORT"; stage="CONFIRMED_TRIGGER" if short_trigger else "EARLY_WATCH"
    # Never label a setup confirmed without a candle close + volume confirmation.
    if action=="LONG" and stage=="CONFIRMED_TRIGGER" and not long_trigger: stage="EARLY_WATCH"
    if action=="SHORT" and stage=="CONFIRMED_TRIGGER" and not short_trigger: stage="EARLY_WATCH"
    if action=="WAIT" and (long_pts>=30 or short_pts>=30): stage="CONFLICTING_EVIDENCE"
    confidence="high" if coverage>=75 and abs(edge)>=35 else "medium" if coverage>=55 and abs(edge)>=22 else "low"
    atr=sum((highs[i]-lows[i]) for i in range(len(rows)-14,len(rows)))/14
    stop=target=None; entry=None; invalidation=None
    if action=="LONG":
        entry=round(prev_high,8) if stage=="EARLY_WATCH" else round(price,8)
        stop=round(min(price-1.2*atr,entry-1.2*atr),8)
        risk=entry-stop
        near=_nearest(mtf,"resistance",price)
        target=round(near[1],8) if near and near[1]-entry>=1.5*risk else round(entry+2*risk,8)
        invalidation=f"Hourly close below {stop}"
    elif action=="SHORT":
        entry=round(prev_low,8) if stage=="EARLY_WATCH" else round(price,8)
        stop=round(max(price+1.2*atr,entry+1.2*atr),8)
        risk=stop-entry
        near=_nearest(mtf,"support",price)
        target=round(near[1],8) if near and entry-near[1]>=1.5*risk else round(entry-2*risk,8)
        invalidation=f"Hourly close above {stop}"
    return {"action":action,"stage":stage,"confidence":confidence,"coverage_pct":round(coverage,1),"score_long":long_score,"score_short":short_score,"edge":round(edge,1),"entry_reference":entry,"stop_reference":stop,"target_reference":target,"invalidation":invalidation,"trigger":{"long_close_above":round(prev_high,8),"short_close_below":round(prev_low,8),"volume_confirmed":vol_confirm,"volume_ratio_last_hour":round(vol_now/vol_base,2) if vol_base else None},"evidence":evidence,"missing":missing,"market_phase":phase,"metrics":{"return_4h_pct":round(r4,3),"return_12h_pct":round(r12,3),"oi_change_24h_pct":round(oi*100,2) if oi is not None else None,"taker_cvd_24h":cvd,"volume_ratio_24h":round(vr,2) if vr is not None else None},"disclaimer":"Early watch is not an entry. Confirmed trigger requires a completed hourly close beyond the recent 6-hour range and at least 1.15x hourly volume. Experimental, uncalibrated decision support; false signals and gaps are possible."}
