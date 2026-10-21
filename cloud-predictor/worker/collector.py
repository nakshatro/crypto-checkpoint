#!/usr/bin/env python3
"""Experimental Binance hourly nearest-analogue forecast worker."""
import json, math, os, statistics, time
from datetime import datetime, timezone
from pathlib import Path
import requests
ROOT=Path(__file__).resolve().parents[1]; DATA=ROOT/"docs"/"data"; DATA.mkdir(parents=True,exist_ok=True)
SYMBOLS={"BTC":"BTCUSDT","ETH":"ETHUSDT","XRP":"XRPUSDT","SOL":"SOLUSDT"}; HORIZONS={"4h":4,"24h":24,"72h":72}
S=requests.Session(); S.headers.update({"User-Agent":"CryptoCheckpointForecast/1.0"})
def get_json(url,params=None):
 r=S.get(url,params=params,timeout=(4,8)); r.raise_for_status(); return r.json()
def write_json(name,obj): (DATA/name).write_text(json.dumps(obj,indent=2,allow_nan=False),encoding="utf-8")
def pct(a,b): return (a/b-1)*100 if b else 0
def mean(xs): return statistics.fmean(xs) if xs else 0
def feature(rows,i):
 cs=[x["c"] for x in rows[:i+1]]; last=cs[-1]; vbase=mean([x["v"] for x in rows[max(0,i-167):i+1]])
 ret=lambda n:pct(last,cs[-n-1]) if len(cs)>n else 0
 rr=[pct(cs[j],cs[j-1]) for j in range(max(1,len(cs)-24),len(cs))]
 ranges=[(x["h"]-x["l"])/x["c"]*100 for x in rows[max(0,i-23):i+1] if x["c"]]
 return {"r1":ret(1),"r4":ret(4),"r12":ret(12),"r24":ret(24),"ma6gap":pct(last,mean(cs[-6:])),"ma24gap":pct(last,mean(cs[-24:])),"volratio":rows[i]["v"]/vbase if vbase else 1,"range24":mean(ranges),"volatility24":statistics.pstdev(rr) if len(rr)>1 else 0}
KEYS=["r1","r4","r12","r24","ma6gap","ma24gap","volratio","range24","volatility24"]
SCALE={"r1":1,"r4":2,"r12":3,"r24":5,"ma6gap":1.5,"ma24gap":3,"volratio":1.5,"range24":1,"volatility24":1}
def dist(a,b): return math.sqrt(mean([min(5,abs(a[k]-b[k])/SCALE[k])**2 for k in KEYS]))
def fetch_rows(symbol):
 # Binance can return HTTP 451 from hosted runners in some regions. Use OKX public candles.
 base=symbol.replace("USDT","")
 last_error=None
 for inst in (base+"-USDT-SWAP", base+"-USDT"):
  try:
   collected=[]; cursor=None
   for _ in range(3):
    params={"instId":inst,"bar":"1H","limit":"300"}
    if cursor is not None: params["after"]=str(cursor)
    j=get_json("https://www.okx.com/api/v5/market/candles",params)
    if str(j.get("code"))!="0": raise RuntimeError("OKX "+str(j.get("code"))+": "+str(j.get("msg")))
    batch=j.get("data",[])
    if not batch: break
    for r in batch:
     if len(r)>=9 and str(r[8])=="1":
      collected.append({"t":int(r[0]),"o":float(r[1]),"h":float(r[2]),"l":float(r[3]),"c":float(r[4]),"v":float(r[7] or r[5] or 0)})
    oldest=min(int(r[0]) for r in batch if r)
    if cursor==oldest: break
    cursor=oldest
    if len(collected)>=480: break
    time.sleep(0.35)
   unique={r["t"]:r for r in collected}
   rows=sorted(unique.values(),key=lambda r:r["t"])
   if len(rows)>=180: return rows[-600:]
   last_error=RuntimeError("OKX "+inst+": only "+str(len(rows))+" completed hourly candles")
  except Exception as e: last_error=e
 raise last_error or RuntimeError("No OKX hourly history for "+symbol)
def make_forecasts(rows):
 n=len(rows)
 if n<180: raise RuntimeError("Not enough completed hourly candles")
 cur=feature(rows,n-1); out=[]
 for label,h in HORIZONS.items():
  candidates=[(dist(cur,feature(rows,i)),pct(rows[i+h]["c"],rows[i]["c"])) for i in range(30,n-h-1)]
  candidates.sort(key=lambda x:x[0]); vals=[x[1] for x in candidates[:60]]
  vals_sorted=sorted(vals); count=len(vals); up=sum(x>0 for x in vals)
  out.append({"horizon":label,"hours":h,"analogue_count":count,"up_frequency_pct":round(up/count*100,1) if count else None,"median_return_pct":round(statistics.median(vals),3) if vals else 0,"range_10_90_pct":[round(vals_sorted[max(0,int((count-1)*.1))],3),round(vals_sorted[max(0,int((count-1)*.9))],3)] if vals else [0,0],"interpretation":"historical analogue frequency, not calibrated probability"})
 return cur,out
def derivatives(symbol):
 out={}
 for key,path,params in [
  ("open_interest","/fapi/v1/openInterest",{"symbol":symbol}),
  ("funding_rate","/fapi/v1/fundingRate",{"symbol":symbol,"limit":1}),
  ("global_long_short_ratio","/futures/data/globalLongShortAccountRatio",{"symbol":symbol,"period":"1h","limit":1}),
  ("taker_buy_sell_ratio","/futures/data/takerlongshortRatio",{"symbol":symbol,"period":"1h","limit":1})]:
  try:
   v=get_json("https://fapi.binance.com"+path,params); out[key]=v[-1] if isinstance(v,list) and v else v
  except Exception as e: out[key]={"unavailable":str(e)[:160]}
 # OKX public derivatives statistics fallback for account long/short and taker buy/sell.
 # Rubik endpoints return recent aggregate rows; ratios are normalized to the dashboard schema.
 base=symbol.replace("USDT","")
 try:
  j=get_json("https://www.okx.com/api/v5/rubik/stat/contracts/long-short-account-ratio",{"ccy":base,"period":"1H"})
  data=j.get("data",[])
  if str(j.get("code"))=="0" and data:
   row=data[0]
   ratio=float(row[1]) if len(row)>1 and row[1] not in (None,"") else None
   if ratio is not None: out["global_long_short_ratio"]={"longShortRatio":ratio,"source":"OKX public account long/short ratio","timestamp":row[0] if row else None}
 except Exception as e:
  out.setdefault("global_long_short_ratio",{"unavailable":"OKX: "+str(e)[:120]})
 try:
  j=get_json("https://www.okx.com/api/v5/rubik/stat/contracts/taker-volume",{"ccy":base,"instType":"SWAP","period":"1H"})
  data=j.get("data",[])
  if str(j.get("code"))=="0" and data:
   row=data[0]
   # OKX row convention: timestamp, sell volume, buy volume.
   sell=float(row[1]) if len(row)>1 and row[1] not in (None,"") else 0
   buy=float(row[2]) if len(row)>2 and row[2] not in (None,"") else 0
   if sell>0: out["taker_buy_sell_ratio"]={"buySellRatio":buy/sell,"source":"OKX public taker volume","timestamp":row[0] if row else None}
   else: out["taker_buy_sell_ratio"]={"unavailable":"OKX returned zero sell volume in latest row"}
  else: out["taker_buy_sell_ratio"]={"unavailable":"OKX returned no taker-volume rows; code="+str(j.get("code"))+" msg="+str(j.get("msg"))[:100]}
 except Exception as e:
  out["taker_buy_sell_ratio"]={"unavailable":"OKX: "+str(e)[:120]}
 # Fallback to the main checkpoint snapshot for OI/funding when Binance futures is blocked.
 try:
  market=json.loads((ROOT.parent/"data"/"market.json").read_text(encoding="utf-8"))
  base=symbol.replace("USDT","")
  row=next((x for x in market.get("symbols",[]) if str(x.get("baseSymbol","")).upper()==base),None)
  if row:
   if not isinstance(out.get("open_interest"),dict) or "unavailable" in out.get("open_interest",{}):
    if row.get("oi") is not None: out["open_interest"]={"openInterest":row.get("oi"),"source":"main checkpoint snapshot (USD value)"}
   if not isinstance(out.get("funding_rate"),list) and (not isinstance(out.get("funding_rate"),dict) or "unavailable" in out.get("funding_rate",{})):
    if row.get("funding") is not None: out["funding_rate"]={"fundingRate":float(row.get("funding"))/100,"source":"main checkpoint snapshot; converted from percent to decimal"}
 except Exception:
  pass
 return out
def telegram(message):
 token=os.getenv("TELEGRAM_BOT_TOKEN","").strip(); chat=os.getenv("TELEGRAM_CHAT_ID","").strip()
 if not token or not chat:return "not_configured"
 try:
  r=S.post("https://api.telegram.org/bot"+token+"/sendMessage",json={"chat_id":chat,"text":message,"disable_web_page_preview":True},timeout=15);r.raise_for_status();return "sent"
 except Exception as e:return "failed: "+str(e)[:160]
def main():
 now=datetime.now(timezone.utc).isoformat(); snap={"updated_at":now,"source":"OKX public hourly candles; Binance futures derivatives where accessible","coins":{}}; issues=[]
 for coin,symbol in SYMBOLS.items():
  try:
   rows=fetch_rows(symbol); f,forecasts=make_forecasts(rows); last=rows[-1]
   snap["coins"][coin]={"coin":coin,"symbol":symbol,"price":last["c"],"candle_time":datetime.fromtimestamp(last["t"]/1000,timezone.utc).isoformat(),"change_1h_pct":round(f["r1"],3),"change_4h_pct":round(f["r4"],3),"change_12h_pct":round(f["r12"],3),"change_24h_pct":round(f["r24"],3),"volume_ratio_24h":round(f["volratio"],3),"ma6_gap_pct":round(f["ma6gap"],3),"ma24_gap_pct":round(f["ma24gap"],3),"volatility_24h_pct":round(f["volatility24"],3),"derivatives":derivatives(symbol),"forecasts":forecasts}
  except Exception as e:issues.append(coin+": "+str(e)[:200])
 write_json("latest.json",snap); write_json("forecasts.json",{"updated_at":now,"items":[{"coin":c,"price":v["price"],**f} for c,v in snap["coins"].items() for f in v["forecasts"]]})
 status={"updated_at":now,"ok":bool(snap["coins"]),"coins_updated":list(snap["coins"]),"issues":issues}; histpath=DATA/"history.json"
 try: hist=json.loads(histpath.read_text(encoding="utf-8"))
 except Exception: hist=[]
 hist.append({"updated_at":now,"coins":{c:{"price":v["price"],"forecasts":v["forecasts"]} for c,v in snap["coins"].items()}}); write_json("history.json",hist[-500:])
 alerts=[]
 for c,v in snap["coins"].items():
  f=next((x for x in v["forecasts"] if x["horizon"]=="24h"),None)
  if f and f["analogue_count"]>=40 and f["up_frequency_pct"] is not None and abs(f["up_frequency_pct"]-50)>=15:
   alerts.append(f'{c} {"BULLISH" if f["up_frequency_pct"]>50 else "BEARISH"} analogue lean | price {v["price"]:.6g} | 24h up-frequency {f["up_frequency_pct"]}% | median return {f["median_return_pct"]}%')
 if alerts: status["telegram"]=telegram("Crypto Checkpoint cloud alert\n"+now+"\n\n"+"\n".join(alerts))
 write_json("status.json",status); print(json.dumps(status,indent=2))
 if not snap["coins"]: raise RuntimeError("No coin data updated; inspect status.json")
if __name__=="__main__":main()
