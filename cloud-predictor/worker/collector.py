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
 # OKX public account long/short ratio fallback.
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
 # Binance is blocked on some hosted runners. Calculate a clearly labelled recent-trades taker ratio from OKX public trades.
 # This is a recent sample, not a 1-hour aggregate.
 try:
  trades=None; used_inst=None; last_trade_error=None
  for inst in (base+"-USDT-SWAP",base+"-USDT"):
   try:
    j=get_json("https://www.okx.com/api/v5/market/trades",{"instId":inst,"limit":"500"})
    if str(j.get("code"))=="0" and j.get("data"):
     trades=j["data"]; used_inst=inst; break
    last_trade_error="OKX returned no trades for "+inst+": "+str(j.get("msg",""))
   except Exception as e:
    last_trade_error=str(e)[:120]
  if trades:
   buy=sum(float(t.get("sz",0)) for t in trades if str(t.get("side","")).lower()=="buy")
   sell=sum(float(t.get("sz",0)) for t in trades if str(t.get("side","")).lower()=="sell")
   if sell>0:
    out["taker_buy_sell_ratio"]={"buySellRatio":buy/sell,"buy_size":buy,"sell_size":sell,"trade_count":len(trades),"source":"OKX public recent trades (up to 500; not a 1h aggregate)","instrument":used_inst,"timestamp":trades[0].get("ts")}
   else:
    out["taker_buy_sell_ratio"]={"unavailable":"Recent OKX trades had zero sell-side size","trade_count":len(trades),"instrument":used_inst}
  else:
   out["taker_buy_sell_ratio"]={"unavailable":last_trade_error or "No recent OKX trades returned"}
 except Exception as e:
  out["taker_buy_sell_ratio"]={"unavailable":"OKX recent trades: "+str(e)[:120]}
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

# ---------- five-layer checkpoint model (adapted from BUILD PROMPT.docx) ----------
import xml.etree.ElementTree as ET
HORIZON_WEIGHTS={"24h":{"structure":.20,"derivs":.30,"liquidity":.30,"onchain":.05,"news":.15},
 "1w":{"structure":.30,"derivs":.25,"liquidity":.15,"onchain":.15,"news":.15},
 "1m":{"structure":.30,"derivs":.10,"liquidity":.05,"onchain":.35,"news":.20}}
REGIME_MULT={"UPTREND":{"structure":1.2},"DOWNTREND":{"structure":1.2},"RANGE":{"structure":.7,"derivs":1.2,"liquidity":1.2},"HIGH_VOL":{"structure":.8,"liquidity":1.3,"news":1.2}}
BULL_WORDS=("approve","approval","inflow","record high","surge","rally","adopt","partnership","upgrade","breakout","accumulat")
BEAR_WORDS=("hack","exploit","ban","lawsuit","sues","outflow","crash","plunge","liquidat","delist","investigation","fraud","bankrupt")
def daily_candles(symbol):
 base=symbol.replace("USDT","")
 for inst in (base+"-USDT-SWAP",base+"-USDT"):
  try:
   j=get_json("https://www.okx.com/api/v5/market/candles",{"instId":inst,"bar":"1Dutc","limit":"300"})
   if str(j.get("code"))=="0":
    rows=[{"t":int(x[0]),"o":float(x[1]),"h":float(x[2]),"l":float(x[3]),"c":float(x[4]),"v":float(x[7] or 0)} for x in reversed(j.get("data",[])) if len(x)>=9 and str(x[8])=="1"]
    if len(rows)>=200:return rows
  except Exception: pass
 return None
def ema_values(vals,n):
 if not vals:return []
 k=2/(n+1); e=vals[0]; out=[e]
 for x in vals[1:]: e=x*k+e*(1-k); out.append(e)
 return out
def ret_std(vals,n):
 rr=[math.log(vals[i]/vals[i-1]) for i in range(max(1,len(vals)-n),len(vals)) if vals[i]>0 and vals[i-1]>0]
 return statistics.pstdev(rr) if len(rr)>1 else None
def global_model_context():
 out={"stablecoin_30d":None,"fear_greed":None,"headlines":[],"events":[]}
 try:
  j=get_json("https://stablecoins.llama.fi/stablecoincharts/all")
  if len(j)>=31:
   a=j[-1]["totalCirculatingUSD"]["peggedUSD"]; b=j[-31]["totalCirculatingUSD"]["peggedUSD"]
   if b: out["stablecoin_30d"]=a/b-1
 except Exception: pass
 try:
  j=get_json("https://api.alternative.me/fng/",{"limit":30})
  if j.get("data"): out["fear_greed"]=[int(x["value"]) for x in reversed(j["data"])]
 except Exception: pass
 for url in ("https://www.coindesk.com/arc/outboundfeeds/rss/","https://cointelegraph.com/rss","https://decrypt.co/feed"):
  try:
   rr=S.get(url,timeout=(4,8)); rr.raise_for_status(); root=ET.fromstring(rr.content)
   for item in root.iter("item"):
    title=(item.findtext("title") or "").strip()
    if title: out["headlines"].append(title)
  except Exception: pass
 out["headlines"]=out["headlines"][:100]
 return out
def chain_tvl_change(chain):
 try:
  j=get_json("https://api.llama.fi/v2/historicalChainTvl/"+chain)
  if len(j)>=31 and j[-31].get("tvl"): return j[-1]["tvl"]/j[-31]["tvl"]-1
 except Exception: pass
 return None
def bounded(x,lo=-1,hi=1): return max(lo,min(hi,x))
def layer_mean(items):
 items=[x for x in items if x[1] is not None]
 total=sum(x[2] for x in items)
 if not items or not total:return None,[]
 return sum(x[1]*x[2] for x in items)/total,[{"name":n,"score":round(s,3),"weight":round(w/total,3),"note":note} for n,s,w,note in items]
def advanced_model(coin,symbol,rows,derivs,ctx,price):
 missing=[]; details={}; scores={}
 d1=daily_candles(symbol); regime="RANGE"; vol_ratio=None
 if d1 and len(d1)>=200:
  closes=[x["c"] for x in d1]; e50=ema_values(closes,50); e200=ema_values(closes,200)
  rv7=ret_std(closes,7); rv90=ret_std(closes,90)
  vol_ratio=(rv7/rv90) if rv7 is not None and rv90 else None
  slope=(e50[-1]/e50[-11]-1) if len(e50)>=11 and e50[-11] else 0
  if vol_ratio is not None and vol_ratio>1.6: regime="HIGH_VOL"
  elif closes[-1]>e50[-1]>e200[-1] and slope>0: regime="UPTREND"
  elif closes[-1]<e50[-1]<e200[-1] and slope<0: regime="DOWNTREND"
  c=closes[-1]; e20=ema_values(closes,20)[-1]
  trend=sum([1 if c>e20 else -1,1 if c>e50[-1] else -1,1 if c>e200[-1] else -1,1 if e20>e50[-1] else -1])/4
  dif=[closes[i]-closes[i-1] for i in range(1,len(closes))]
  gains=[max(x,0) for x in dif[-14:]]; losses=[max(-x,0) for x in dif[-14:]]
  ag=sum(gains)/max(1,len(gains)); al=sum(losses)/max(1,len(losses)); rsi=100 if al==0 else 100-100/(1+ag/al)
  structure=[("trend",trend,.5,f"daily close vs EMA20/50/200; EMA50 slope {slope*100:+.2f}% over 10 days"),("momentum",bounded((rsi-50)/25),.3,f"daily RSI14 {rsi:.1f}")]
  if coin!="BTC":
   btc=ctx.get("btc_daily")
   if btc and len(btc)>30 and len(d1)>30:
    rel=(closes[-1]/closes[-31]-1)-(btc[-1]["c"]/btc[-31]["c"]-1)
    structure.append(("relative_strength_vs_BTC_30d",bounded(rel/.15),.2,f"{rel*100:+.1f}% vs BTC over 30d"))
  s,comps=layer_mean(structure); scores["structure"]=s; details["structure"]={"score":s,"components":comps}
 else:
  scores["structure"]=None; details["structure"]={"score":None,"components":[]}; missing.append("daily_candles_structure")
 if scores.get("structure") is None: missing.append("structure")
 dc=[]
 fr=derivs.get("funding_rate"); funding=None
 if isinstance(fr,dict) and fr.get("fundingRate") is not None: funding=float(fr["fundingRate"])
 elif isinstance(fr,list) and fr and fr[-1].get("fundingRate") is not None: funding=float(fr[-1]["fundingRate"])
 if funding is not None: dc.append(("funding_crowding",bounded(-funding/.001)*(.4 if regime in ("UPTREND","DOWNTREND") else 1),.30,f"latest funding {funding*100:.4f}% (historical percentile unavailable)"))
 else: missing.append("funding_rate")
 ls=derivs.get("global_long_short_ratio")
 if isinstance(ls,dict) and ls.get("longShortRatio") is not None:
  ratio=float(ls["longShortRatio"]); dc.append(("long_short_ratio",bounded(-math.log(max(ratio,.01))*.5),.15,f"account long/short ratio {ratio:.3f}"))
 dc.append(("price_vs_oi",None,.30,"24h OI change unavailable from a single snapshot"))
 s,comps=layer_mean(dc); scores["derivs"]=s; details["derivs"]={"score":s,"components":comps}
 if s is None: missing.append("derivatives_layer")
 lc=[]; tr=derivs.get("taker_buy_sell_ratio")
 if isinstance(tr,dict) and tr.get("buySellRatio") is not None:
  ratio=float(tr["buySellRatio"]); lc.append(("recent_taker_flow",bounded((ratio-1)/.5),.4,f"buy/sell size ratio {ratio:.3f}; recent sample, not 24h CVD"))
 lc.extend([("liquidation_magnet",None,.30,"not computed: reliable 168h OI-change series unavailable"),("recent_liquidation_flow",None,.20,"liquidation-flow endpoint not verified")])
 s,comps=layer_mean(lc); scores["liquidity"]=s; details["liquidity"]={"score":s,"components":comps}
 if s is None: missing.append("liquidity_layer")
 oc=[]; stable=ctx.get("stablecoin_30d")
 if stable is not None: oc.append(("stablecoin_supply_30d",bounded(stable/.03),.4,f"global stablecoin supply {stable*100:+.2f}% over 30d"))
 else: missing.append("stablecoin_supply")
 chain={"ETH":"Ethereum","SOL":"Solana"}.get(coin)
 if chain:
  tvl=chain_tvl_change(chain)
  if tvl is not None: oc.append(("chain_tvl_30d",bounded(tvl/.15),.3,f"{chain} TVL {tvl*100:+.1f}% over 30d"))
  else: missing.append("chain_tvl_"+coin.lower())
 oc.append(("manual_etf_flow",None,.4,"no verified ETF-flow input configured"))
 s,comps=layer_mean(oc); scores["onchain"]=s; details["onchain"]={"score":s,"components":comps}
 if s is None: missing.append("onchain_layer")
 words=[]; terms={"BTC":("bitcoin","btc"),"ETH":("ethereum","ether","eth"),"SOL":("solana","sol"),"XRP":("xrp","ripple")}[coin]
 for title in ctx.get("headlines",[]):
  low=title.lower()
  if not any(t in low for t in terms): continue
  b=sum(w in low for w in BULL_WORDS); bear=sum(w in low for w in BEAR_WORDS)
  if b+bear: words.append((b-bear)/(b+bear))
 nc=[]
 if words: nc.append(("headline_sentiment",bounded(sum(words)/len(words))*min(1,len(words)/5),.6,f"{len(words)} matching keyword-scored headlines; not semantic sentiment"))
 fg=ctx.get("fear_greed")
 if fg:
  v=fg[-1]; fs=((50-v)/50*.8 if v<=25 or v>=75 else (v-50)/50*.4)
  nc.append(("fear_greed",bounded(fs),.4,f"Fear & Greed {v}; contrarian at extremes"))
 else: missing.append("fear_greed")
 s,comps=layer_mean(nc); scores["news"]=s; details["news"]={"score":s,"components":comps}
 if s is None: missing.append("news_layer")
 horizons={}
 daily_returns=[math.log(d1[i]["c"]/d1[i-1]["c"]) for i in range(1,len(d1)) if d1[i]["c"]>0 and d1[i-1]["c"]>0] if d1 else []
 sigma=statistics.pstdev(daily_returns[-30:]) if len(daily_returns)>=2 else None
 for hz,days in (("24h",1),("1w",7),("1m",30)):
  w={k:HORIZON_WEIGHTS[hz][k]*REGIME_MULT.get(regime,{}).get(k,1) for k,v in scores.items() if v is not None}
  tw=sum(w.values()); composite=sum(scores[k]*w[k] for k in w)/tw if tw else None
  coverage=sum(HORIZON_WEIGHTS[hz][k] for k in w)
  p_up=(.5+.2*math.tanh(2*composite)) if composite is not None else None
  band=price*math.exp(sigma*math.sqrt(days)) if sigma is not None else None
  horizons[hz]={"composite":round(composite,3) if composite is not None else None,"p_up":round(p_up,3) if p_up is not None else None,"coverage":round(coverage,3),"bias":"Bullish" if composite is not None and composite>.15 else "Bearish" if composite is not None and composite<-.15 else "Neutral" if composite is not None else "Unavailable","range":[round(price/math.exp(sigma*math.sqrt(days)),8),round(band,8)] if band else None}
 layer_vals=[x for x in scores.values() if x is not None]; dispersion=statistics.pstdev(layer_vals) if len(layer_vals)>1 else .5
 confidence=max(0,min(1,1-dispersion/.6))*(horizons["1w"]["coverage"] or 0)
 return {"regime":regime,"vol_ratio":round(vol_ratio,3) if vol_ratio is not None else None,"layers":details,"horizons":horizons,"confidence":{"value":round(confidence,2),"label":"High" if confidence>=.6 else "Medium" if confidence>=.35 else "Low","notes":["layers disagree"] if dispersion>.35 else []},"data_quality":{"missing":sorted(set(missing))},"method":"five-layer weighted model; missing components excluded and weights renormalized; not calibrated"}

def telegram(message):
 token=os.getenv("TELEGRAM_BOT_TOKEN","").strip(); chat=os.getenv("TELEGRAM_CHAT_ID","").strip()
 if not token or not chat:return "not_configured"
 try:
  r=S.post("https://api.telegram.org/bot"+token+"/sendMessage",json={"chat_id":chat,"text":message,"disable_web_page_preview":True},timeout=15);r.raise_for_status();return "sent"
 except Exception as e:return "failed: "+str(e)[:160]
def main():
 now=datetime.now(timezone.utc).isoformat(); model_context=global_model_context(); model_context["btc_daily"]=daily_candles("BTCUSDT"); snap={"updated_at":now,"source":"OKX public hourly candles plus five-layer model; missing components disclosed","model_context":{"fear_greed":model_context.get("fear_greed",[])[-1] if model_context.get("fear_greed") else None,"stablecoin_30d":model_context.get("stablecoin_30d"),"headline_count":len(model_context.get("headlines",[]))},"coins":{}}; issues=[]
 for coin,symbol in SYMBOLS.items():
  try:
   rows=fetch_rows(symbol); f,forecasts=make_forecasts(rows); last=rows[-1]
   # Use the live OKX ticker for the displayed price; keep completed candles for indicators/forecasts.
   live_price=None; price_source=None; price_time=None; base=symbol.replace("USDT","")
   for inst in (base+"-USDT-SWAP",base+"-USDT"):
    try:
     tj=get_json("https://www.okx.com/api/v5/market/ticker",{"instId":inst})
     td=tj.get("data",[])
     if str(tj.get("code"))=="0" and td and td[0].get("last"):
      live_price=float(td[0]["last"]); price_source="OKX live ticker ("+inst+")"; price_time=datetime.fromtimestamp(int(td[0]["ts"])/1000,timezone.utc).isoformat(); break
    except Exception:
     pass
   display_price=live_price if live_price is not None else last["c"]
   deriv_data=derivatives(symbol)
   model=advanced_model(coin,symbol,rows,deriv_data,model_context,display_price)
   snap["coins"][coin]={"coin":coin,"symbol":symbol,"price":display_price,"price_source":price_source or "last completed OKX hourly candle (ticker unavailable)","price_time":price_time or datetime.fromtimestamp(last["t"]/1000,timezone.utc).isoformat(),"candle_time":datetime.fromtimestamp(last["t"]/1000,timezone.utc).isoformat(),"change_1h_pct":round(f["r1"],3),"change_4h_pct":round(f["r4"],3),"change_12h_pct":round(f["r12"],3),"change_24h_pct":round(f["r24"],3),"volume_ratio_24h":round(f["volratio"],3),"ma6_gap_pct":round(f["ma6gap"],3),"ma24_gap_pct":round(f["ma24gap"],3),"volatility_24h_pct":round(f["volatility24"],3),"derivatives":deriv_data,"forecasts":forecasts,"model":model}
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
