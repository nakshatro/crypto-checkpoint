#!/usr/bin/env python3
"""Multi-timeframe support/resistance zones from completed OKX candles."""
from datetime import datetime, timezone
import math
TIMEFRAMES = {"15m": ("15m", 1.0, 240), "1H": ("1H", 1.2, 300), "4H": ("4H", 1.5, 300), "1D": ("1D", 1.8, 300), "1W": ("1W", 2.0, 200)}
def _candles(symbol, bar, limit, get_json):
    base=symbol.replace("USDT",""); last_error=None
    for inst in (base+"-USDT-SWAP",base+"-USDT"):
        try:
            j=get_json("https://www.okx.com/api/v5/market/candles",{"instId":inst,"bar":bar,"limit":str(limit)})
            if str(j.get("code"))!="0": raise RuntimeError(str(j.get("msg","OKX candle error")))
            rows=[{"t":int(r[0]),"o":float(r[1]),"h":float(r[2]),"l":float(r[3]),"c":float(r[4]),"v":float(r[7] or r[5] or 0)} for r in j.get("data",[]) if len(r)>=9 and str(r[8])=="1"]
            rows.sort(key=lambda x:x["t"])
            if len(rows)>=30:return rows
            last_error=RuntimeError("Insufficient completed candles: "+str(len(rows)))
        except Exception as exc:last_error=exc
    raise last_error or RuntimeError("No candle data")
def _atr(rows,period=14):
    tr=[]
    for i,x in enumerate(rows):
        prev=rows[i-1]["c"] if i else x["c"]
        tr.append(max(x["h"]-x["l"],abs(x["h"]-prev),abs(x["l"]-prev)))
    return sum(tr[-period:])/max(1,len(tr[-period:]))
def _pivots(rows,side):
    out=[]
    for i in range(2,len(rows)-2):
        p=rows[i]["l"] if side=="support" else rows[i]["h"]
        w=[rows[j]["l"] if side=="support" else rows[j]["h"] for j in range(i-2,i+3)]
        if p==(min(w) if side=="support" else max(w)):
            out.append({"price":p,"time":rows[i]["t"],"volume":rows[i]["v"],"side":side})
    return out
def _merge_levels(levels,current,tolerance):
    clusters=[]
    for item in sorted(levels,key=lambda x:x["price"]):
        if not clusters or abs(item["price"]-clusters[-1]["price"])/max(item["price"],1e-12)>tolerance:clusters.append({"items":[item],"price":item["price"]})
        else:
            c=clusters[-1];c["items"].append(item);c["price"]=sum(x["price"] for x in c["items"])/len(c["items"])
    result=[];now_ms=int(datetime.now(timezone.utc).timestamp()*1000)
    for c in clusters:
        items=c["items"];touches=len(items);recency=max(0.0,1.0-(now_ms-max(x["time"] for x in items))/(365*86400000))
        volume_score=min(1.0,math.log1p(sum(max(0,x["volume"]) for x in items))/20);strength=min(100,25+min(touches,5)*9+recency*20+volume_score*10);p=c["price"]
        result.append({"price":round(p,8),"distance_pct":round((p/current-1)*100,3),"touches":touches,"strength":round(strength),"last_test_utc":datetime.fromtimestamp(max(x["time"] for x in items)/1000,timezone.utc).isoformat()})
    return sorted(result,key=lambda x:(abs(x["distance_pct"]),-x["strength"]))[:4]
def build_mtf_levels(symbol,current_price,get_json):
    result={"method":"confirmed 5-candle swing pivots clustered by ATR/price proximity; strength is confluence, not reversal probability","current_price":current_price,"timeframes":{},"updated_at":datetime.now(timezone.utc).isoformat()}
    for label,(bar,weight,limit) in TIMEFRAMES.items():
        try:
            rows=_candles(symbol,bar,limit,get_json);atr=_atr(rows);tol=max(.0025,min(.012,(atr/max(rows[-1]["c"],1e-12))*.45))
            pivots=_pivots(rows,"support")+_pivots(rows,"resistance")
            supports=[x for x in pivots if x["side"]=="support" and x["price"]<current_price];resistances=[x for x in pivots if x["side"]=="resistance" and x["price"]>current_price]
            result["timeframes"][label]={"source":"OKX completed "+bar+" candles","candle_time":datetime.fromtimestamp(rows[-1]["t"]/1000,timezone.utc).isoformat(),"atr":round(atr,8),"support":_merge_levels(supports,current_price,tol),"resistance":_merge_levels(resistances,current_price,tol)}
        except Exception as exc:result["timeframes"][label]={"unavailable":str(exc)[:160],"support":[],"resistance":[]}
    flat=[{"timeframe":tf,"side":side,**level} for tf,d in result["timeframes"].items() for side in ("support","resistance") for level in d.get(side,[])]
    for side in ("support","resistance"):
        groups=[]
        for item in sorted((x for x in flat if x["side"]==side),key=lambda x:x["price"]):
            if not groups or abs(item["price"]-groups[-1]["price"])/max(item["price"],1e-12)>.004:groups.append({"price":item["price"],"items":[item]})
            else:groups[-1]["items"].append(item);groups[-1]["price"]=sum(x["price"] for x in groups[-1]["items"])/len(groups[-1]["items"])
        combined=[]
        for g in groups:
            items=g["items"];tfs=sorted(set(x["timeframe"] for x in items),key=lambda x:["15m","1H","4H","1D","1W"].index(x))
            combined.append({"price":round(g["price"],8),"distance_pct":round((g["price"]/current_price-1)*100,3),"timeframes":tfs,"confirmations":len(tfs),"strength":round(min(100,sum(x["strength"] for x in items)/len(items)+12*(len(tfs)-1)))})
        result["confluence_"+side]=sorted(combined,key=lambda x:(abs(x["distance_pct"]),-x["strength"]))[:6]
    return result
