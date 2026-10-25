# Full-system refresh trigger requested 2026-10-23; no runtime behavior changed.
# Calendar and ETF automation verification refresh: 2026-10-25.
#!/usr/bin/env python3
"""Experimental Crypto Checkpoint: analogue scenarios plus a five-layer probabilistic model."""
import json, math, os, statistics, time, re, bisect
from datetime import datetime, timezone, timedelta
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
def quantile_linear(values,q):
 if not values:return 0.0
 xs=sorted(values); pos=(len(xs)-1)*q; lo=int(math.floor(pos)); hi=int(math.ceil(pos))
 if lo==hi:return xs[lo]
 return xs[lo]+(xs[hi]-xs[lo])*(pos-lo)
def make_forecasts(rows):
 n=len(rows)
 if n<180: raise RuntimeError("Not enough completed hourly candles")
 cur=feature(rows,n-1); out=[]
 # Blend a broad analogue pool with rolling volatility to reduce neighbour-selection jitter.
 analogue_window=120
 returns=[pct(rows[i]["c"],rows[i-1]["c"]) for i in range(max(1,n-169),n) if rows[i-1]["c"]]
 hourly_sigma=statistics.pstdev(returns) if len(returns)>1 else 0.0
 volatility_weight=0.35
 z=1.281551565545
 for label,h in HORIZONS.items():
  candidates=[(dist(cur,feature(rows,i)),pct(rows[i+h]["c"],rows[i]["c"])) for i in range(30,n-h-1)]
  candidates.sort(key=lambda x:x[0]); vals=[x[1] for x in candidates[:analogue_window]]
  count=len(vals); up=sum(x>0 for x in vals)
  analogue_lo=quantile_linear(vals,.10); analogue_hi=quantile_linear(vals,.90)
  vol_bound=z*hourly_sigma*math.sqrt(h)
  lo=(1-volatility_weight)*analogue_lo+volatility_weight*(-vol_bound)
  hi=(1-volatility_weight)*analogue_hi+volatility_weight*vol_bound
  lo,hi=min(lo,hi),max(lo,hi)
  out.append({"horizon":label,"hours":h,"analogue_count":count,"analogue_window":analogue_window,"up_frequency_pct":round(up/count*100,1) if count else None,"median_return_pct":round(statistics.median(vals),1) if vals else 0,"range_10_90_pct":[round(round(lo*10)/10,1),round(round(hi*10)/10,1)] if vals else [0,0],"range_method":"Blended interval: 65% empirical 10th/90th quantiles of 120 nearest historical analogues + 35% rolling 168h volatility envelope (normal 10th/90th proxy); rounded to 0.1 percentage point. Recompute on completed candles; genuine regime changes can move the range.","rolling_168h_hourly_sigma_pct":round(hourly_sigma,4),"volatility_blend_weight":volatility_weight,"interpretation":"historical analogue frequency, not calibrated probability"})
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
 # OKX historical derivatives enrich the model when public Rubik endpoints respond.
 base=symbol.replace("USDT","")
 try:
  j=get_json("https://www.okx.com/api/v5/public/funding-rate-history",{"instId":base+"-USDT-SWAP","limit":"100"})
  if str(j.get("code"))=="0" and j.get("data"):
   out["funding_history"]=[float(x["fundingRate"]) for x in reversed(j["data"]) if x.get("fundingRate") not in (None,"")]
 except Exception as e: out["funding_history_unavailable"]=str(e)[:120]
 try:
  j=get_json("https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-volume",{"ccy":base,"period":"1H"})
  rows=sorted(j.get("data",[]),key=lambda x:int(x[0])) if str(j.get("code"))=="0" else []
  if len(rows)>=25:
   vals=[float(x[1]) for x in rows if len(x)>1 and x[1] not in (None,"")]
   if len(vals)>=25 and vals[-25]: out["oi_change_24h"]=vals[-1]/vals[-25]-1
   out["oi_series"]=[[int(x[0]),float(x[1])] for x in rows if len(x)>1 and x[1] not in (None,"")]
   out["oi_history_count"]=len(rows)
 except Exception as e: out["oi_history_unavailable"]=str(e)[:120]
 try:
  j=get_json("https://www.okx.com/api/v5/rubik/stat/taker-volume",{"ccy":base,"instType":"CONTRACTS","period":"1H"})
  rows=sorted(j.get("data",[]),key=lambda x:int(x[0])) if str(j.get("code"))=="0" else []
  last=rows[-24:]
  sell=sum(float(x[1]) for x in last if len(x)>2); buy=sum(float(x[2]) for x in last if len(x)>2)
  if buy+sell>0: out["taker_cvd_24h"]=(buy-sell)/(buy+sell); out["taker_cvd_count"]=len(last)
 except Exception as e: out["taker_cvd_unavailable"]=str(e)[:120]
 try:
  sp=get_json("https://www.okx.com/api/v5/market/ticker",{"instId":base+"-USDT"})
  sw=get_json("https://www.okx.com/api/v5/market/ticker",{"instId":base+"-USDT-SWAP"})
  if sp.get("data") and sw.get("data"):
   spot=float(sp["data"][0]["last"]); swap=float(sw["data"][0]["last"])
   if spot: out["basis"]=(swap-spot)/spot
 except Exception as e: out["basis_unavailable"]=str(e)[:120]
 try:
  j=get_json("https://www.okx.com/api/v5/market/books",{"instId":base+"-USDT-SWAP","sz":"400"})
  if str(j.get("code"))=="0" and j.get("data"):
   bids=j["data"][0].get("bids",[]); asks=j["data"][0].get("asks",[])
   bv=sum(float(x[0])*float(x[1]) for x in bids); av=sum(float(x[0])*float(x[1]) for x in asks)
   if bv+av: out["book_imbalance"]=(bv-av)/(bv+av)
 except Exception as e: out["book_imbalance_unavailable"]=str(e)[:120]
 try:
  j=get_json("https://www.okx.com/api/v5/public/instruments",{"instType":"SWAP","instFamily":base+"-USDT"})
  ct=float(j["data"][0]["ctVal"]) if str(j.get("code"))=="0" and j.get("data") else None
  q=get_json("https://www.okx.com/api/v5/public/liquidation-orders",{"instType":"SWAP","instFamily":base+"-USDT","state":"filled"})
  if ct and str(q.get("code"))=="0":
   longs=shorts=0.0
   for group in q.get("data",[]):
    for x in group.get("details",[]):
     usd=float(x.get("sz",0))*ct*float(x.get("bkPx",0))
     if x.get("posSide")=="long" or (x.get("posSide")=="net" and x.get("side")=="sell"): longs+=usd
     else: shorts+=usd
   out["recent_liquidations_usd"]={"longs":longs,"shorts":shorts}
 except Exception as e: out["liquidations_unavailable"]=str(e)[:120]
 return out

# ---------- five-layer checkpoint model (adapted from BUILD PROMPT.docx) ----------
import xml.etree.ElementTree as ET
HORIZON_WEIGHTS={"4h":{"structure":.15,"derivs":.35,"liquidity":.30,"onchain":.05,"news":.15},
 "24h":{"structure":.20,"derivs":.30,"liquidity":.30,"onchain":.05,"news":.15},
 "72h":{"structure":.25,"derivs":.28,"liquidity":.20,"onchain":.12,"news":.15},
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
def automatic_etf_flows():
 """Fetch daily net flows for BTC/ETH/SOL ETF tables and XRP holdings-derived ETF flows."""
 from html.parser import HTMLParser
 import csv, io
 class Parser(HTMLParser):
  def __init__(self):
   super().__init__(convert_charrefs=True); self.rows=[]; self.row=None; self.cell=None
  def handle_starttag(self,tag,attrs):
   if tag=="tr": self.row=[]
   elif tag in ("td","th") and self.row is not None:self.cell=[]
  def handle_data(self,data):
   if self.cell is not None:self.cell.append(data)
  def handle_endtag(self,tag):
   if tag in ("td","th") and self.cell is not None:self.row.append(" ".join("".join(self.cell).split()));self.cell=None
   elif tag=="tr" and self.row is not None:
    if self.row:self.rows.append(self.row)
    self.row=None;self.cell=None
 def dateval(s):
  for fmt in ("%d %b %Y","%d %B %Y","%b %d %Y","%B %d %Y","%Y-%m-%d","%d/%m/%Y"):
   try:return datetime.strptime(s.strip().replace(",",""),fmt).date()
   except ValueError:pass
  return None
 def numval(s):
  x=s.strip().replace("$","").replace(",","").replace("−","-")
  if x in ("","-","—","N/A"):return None
  neg=x.startswith("(") and x.endswith(")")
  if neg:x=x[1:-1]
  try:
   v=float(x);return -abs(v) if neg else v
  except ValueError:return None
 result={"flows_usd":{},"metadata":{}}
 for coin,url in (("BTC","https://farside.co.uk/btc/"),("ETH","https://farside.co.uk/ETH/"),("SOL","https://farside.co.uk/SOL/")):
  try:
   resp=S.get(url,timeout=(5,12));resp.raise_for_status();parser=Parser();parser.feed(resp.text);rows=[]
   # Read the explicit Total column only. Never substitute the last fund value
   # when a pending/missing Total is shown as a dash.
   total_idx=None
   for header in parser.rows:
    if header and any(cell.strip().lower()=="total" for cell in header):
     total_idx=max(i for i,cell in enumerate(header) if cell.strip().lower()=="total")
     break
   if total_idx is None:raise RuntimeError("Farside Total column header not found")
   for row in parser.rows:
    if len(row)<=total_idx:continue
    d=dateval(row[0])
    if not d:continue
    total_value=numval(row[total_idx])
    if total_value is not None:rows.append((d,total_value))
   bydate={d:v for d,v in rows};recent=sorted(bydate.items())[-5:]
   if len(recent)<3:raise RuntimeError("Fewer than 3 dated rows with a reported Total parsed")
   total=sum(v for _,v in recent)*1_000_000
   result["flows_usd"][coin]=round(total,2)
   result["metadata"][coin]={"source":"Farside Investors public daily ETF flow table","url":url,"period_start":recent[0][0].isoformat(),"period_end":recent[-1][0].isoformat(),"sessions":len(recent),"five_session_net_flow_usd":round(total,2),"unit_conversion":"US$ millions converted to USD","updated_at":datetime.now(timezone.utc).isoformat()}
  except Exception as e:result["metadata"][coin]={"source":"Farside Investors public daily ETF flow table","url":url,"error":str(e)[:160]}
 # XRP source aggregates issuer-reported holdings changes by trading date.
 xrp_url="https://xrp-insights.com/data/xrp-etf-flows.csv"
 try:
  resp=S.get(xrp_url,timeout=(5,12));resp.raise_for_status()
  daily={}
  for row in csv.DictReader(io.StringIO(resp.text)):
   d=dateval(row.get("date",""));status=(row.get("flow_status") or "").strip().lower();row_status=(row.get("row_status") or "").strip().lower()
   usd=numval(row.get("flow_usd") or "")
   if d and status=="computed" and usd is not None and row_status in ("settled","provisional"):
    daily.setdefault(d,0.0);daily[d]+=usd
  recent=sorted(daily.items())[-5:]
  if len(recent)<3:raise RuntimeError("Fewer than 3 valid XRP ETF flow dates parsed")
  total=sum(v for _,v in recent)
  result["flows_usd"]["XRP"]=round(total,2)
  result["metadata"]["XRP"]={"source":"XRP Insights issuer-holdings-derived ETF flows","url":xrp_url,"period_start":recent[0][0].isoformat(),"period_end":recent[-1][0].isoformat(),"sessions":len(recent),"five_session_net_flow_usd":round(total,2),"unit_conversion":"USD; daily flows aggregated across funds","updated_at":datetime.now(timezone.utc).isoformat(),"caveat":"Derived from reported holdings changes; latest provisional session may be revised."}
 except Exception as e:result["metadata"]["XRP"]={"source":"XRP Insights issuer-holdings-derived ETF flows","url":xrp_url,"error":str(e)[:160]}
 return result

def load_manual_inputs():
 try:
  p=ROOT/"manual_inputs.json"
  return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
 except Exception: return {}
def automatic_macro_events():
 """Fetch upcoming BLS release dates/times from official monthly pages plus FOMC meeting dates."""
 from html import unescape
 now=datetime.now(timezone.utc);out=[]
 wanted=("consumer price index","producer price index","employment situation","job openings and labor turnover","productivity and costs","employment cost index","import and export price","retail sales","personal income and outlays","gross domestic product")
 eastern=__import__("zoneinfo").ZoneInfo("America/New_York")
 weekday=r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
 month=r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
 date_time_re=re.compile(r"(?:"+weekday+r"),?\s+("+month+r")\s+(\d{1,2})(?:,?\s+(20\d{2}))?\s+(\d{1,2}:\d{2}\s+[AP]M)",re.I)
 for offset in range(3):
  year=now.year;month_num=now.month+offset
  while month_num>12:year+=1;month_num-=12
  url="https://www.bls.gov/schedule/%d/%02d_sched_list.htm"%(year,month_num)
  try:
   resp=S.get(url,headers={"User-Agent":"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/129 Safari/537.36"},timeout=(5,12));resp.raise_for_status()
   raw=unescape(re.sub(r"<[^>]+>"," ",resp.text));raw=re.sub(r"\s+"," ",raw)
   matches=list(date_time_re.finditer(raw))
   for i,m in enumerate(matches):
    end=matches[i+1].start() if i+1<len(matches) else len(raw)
    title=raw[m.end():end].strip(" |:-\t")
    if not any(key in title.lower() for key in wanted):continue
    try:
     release_year=m.group(3) or str(year);dt=datetime.strptime("%s %s %s %s"%(m.group(1),m.group(2),release_year,m.group(4)),"%B %d %Y %I:%M %p")
     t=dt.replace(tzinfo=eastern).astimezone(timezone.utc);hours=(t-now).total_seconds()/3600
     if -1<=hours<=24*75:out.append({"name":title[:180],"time_utc":t.isoformat(),"date_utc":t.strftime("%Y-%m-%d"),"time_exact":True,"in_hours":round(hours,1),"source":"US BLS official monthly release schedule","url":url})
    except Exception:continue
  except Exception:continue
 # Official BLS schedule fallback for the currently published 2026 dates.
 # Live monthly pages are attempted above on every run; this list prevents an empty
 # countdown if GitHub Actions cannot retrieve or parse the official BLS HTML.
 if not any(str(e.get("source","")).startswith("US BLS official") for e in out) and now.year==2026:
  fallback_releases=[
   ("Job Openings and Labor Turnover Survey","November",3,"10:00 AM","https://www.bls.gov/schedule/2026/11_sched_list.htm"),
   ("Productivity and Costs","November",5,"08:30 AM","https://www.bls.gov/schedule/2026/11_sched_list.htm"),
   ("Employment Situation","November",6,"08:30 AM","https://www.bls.gov/schedule/2026/11_sched_list.htm"),
   ("Consumer Price Index","November",10,"08:30 AM","https://www.bls.gov/schedule/2026/11_sched_list.htm"),
   ("Producer Price Index","November",13,"08:30 AM","https://www.bls.gov/schedule/2026/11_sched_list.htm"),
   ("U.S. Import and Export Price Indexes","November",17,"08:30 AM","https://www.bls.gov/schedule/2026/11_sched_list.htm"),
   ("Job Openings and Labor Turnover Survey","December",1,"10:00 AM","https://www.bls.gov/schedule/2026/12_sched_list.htm"),
   ("Employment Situation","December",4,"08:30 AM","https://www.bls.gov/schedule/2026/12_sched_list.htm"),
   ("Productivity and Costs","December",8,"08:30 AM","https://www.bls.gov/schedule/2026/12_sched_list.htm"),
   ("Consumer Price Index","December",10,"08:30 AM","https://www.bls.gov/schedule/2026/12_sched_list.htm"),
   ("Producer Price Index","December",15,"08:30 AM","https://www.bls.gov/schedule/2026/12_sched_list.htm"),
   ("U.S. Import and Export Price Indexes","December",17,"08:30 AM","https://www.bls.gov/schedule/2026/12_sched_list.htm"),
  ]
  from zoneinfo import ZoneInfo
  eastern=ZoneInfo("America/New_York")
  for name,mon,day,clock,url in fallback_releases:
   try:
    dt=datetime.strptime(f"{mon} {day} 2026 {clock}","%B %d %Y %I:%M %p").replace(tzinfo=eastern).astimezone(timezone.utc)
    hours=(dt-now).total_seconds()/3600
    if -1<=hours<=24*75:out.append({"name":name,"time_utc":dt.isoformat(),"date_utc":dt.strftime("%Y-%m-%d"),"time_exact":True,"in_hours":round(hours,1),"source":"Official BLS 2026 schedule fallback (live calendar unavailable)","url":url})
   except Exception:pass
 try:
  resp=S.get("https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",timeout=(5,12));resp.raise_for_status()
  html=unescape(re.sub(r"<[^>]+>"," ",resp.text));html=re.sub(r"\s+"," ",html)
  sections=list(re.finditer(r"(20\d{2})\s+FOMC Meetings",html,re.I))
  for i,m in enumerate(sections):
   year=int(m.group(1))
   if year<now.year or year>now.year+1:continue
   stop=sections[i+1].start() if i+1<len(sections) else len(html);section=html[m.end():stop]
   for item in re.finditer(r"("+month+r")\s+(\d{1,2})\s*[–-]\s*(\d{1,2})",section,re.I):
    month_name,day,_=item.groups()
    try:
     t=datetime.strptime(month_name+" "+day+" "+str(year),"%B %d %Y").replace(tzinfo=timezone.utc);hours=(t-now).total_seconds()/3600
     if -24<=hours<=24*90:out.append({"name":"FOMC meeting (date only; announcement time not inferred)","time_utc":None,"date_utc":t.strftime("%Y-%m-%d"),"time_exact":False,"in_hours":None,"days_away":round(hours/24,1),"source":"Federal Reserve official calendar","url":"https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"})
    except Exception:continue
 except Exception:pass
 unique={}
 for event in out:
  key=(event["name"],event["date_utc"])
  if key not in unique:unique[key]=event
 return sorted(unique.values(),key=lambda e:(e["date_utc"],0 if e.get("time_exact") else 1,e["name"]))[:20]

def fetch_prediction_market_outlooks(events):
 """Fetch public Polymarket event probabilities relevant to macro releases."""
 now=datetime.now(timezone.utc); found=[]
 try:
  resp=S.get("https://gamma-api.polymarket.com/events",params={"active":"true","closed":"false","limit":250,"order":"volume24hr","ascending":"false"},timeout=(5,12))
  resp.raise_for_status(); payload=resp.json()
  if not isinstance(payload,list): raise RuntimeError("Unexpected Polymarket Gamma response")
  macro_terms=("federal reserve","fed rate","interest rate","fomc","consumer price index","cpi","inflation","producer price index","ppi","nonfarm payroll","unemployment","jobs report","treasury yield")
  for event in payload:
   title=str(event.get("title") or event.get("question") or "")
   outcomes=[]
   for market in (event.get("markets") or [])[:12]:
    question=str(market.get("question") or market.get("groupItemTitle") or title)
    if not any(term in (title+" "+question).lower() for term in macro_terms): continue
    try:
     import json as _json
     names=market.get("outcomes",[]); prices=market.get("outcomePrices",[])
     if isinstance(names,str): names=_json.loads(names)
     if isinstance(prices,str): prices=_json.loads(prices)
     odds=[]
     if isinstance(names,list) and isinstance(prices,list):
      for name,p in zip(names,prices):
       try: odds.append({"outcome":str(name),"probability_pct":round(float(p)*100,1)})
       except Exception: pass
      if odds: outcomes.append({"question":question,"outcomes":odds})
    except Exception: pass
   if outcomes:
    found.append({"source":"Polymarket public event market","title":title,"url":"https://polymarket.com/event/"+str(event.get("slug") or ""),"volume_24h_usd":event.get("volume24hr"),"outcomes":outcomes[:4],"updated_at":now.isoformat(),"note":"Market-implied odds, not guaranteed probabilities or price targets."})
   if len(found)>=6: break
 except Exception as e:
  return {"status":"unavailable","source":"Polymarket Gamma public API","checked_at":now.isoformat(),"error":str(e)[:160],"markets":[]}
 return {"status":"ok" if found else "no_matching_markets","source":"Polymarket Gamma public API","checked_at":now.isoformat(),"markets":found}

def upcoming_manual_events(manual):
 # Retained for compatibility; all active events come from automatic feeds.
 return []
def global_model_context():
 manual=load_manual_inputs(); etf_auto=automatic_etf_flows(); merged_flows=dict(manual.get("etf_flow_5d_usd") or {}); merged_flows.update(etf_auto.get("flows_usd") or {}); manual["etf_flow_5d_usd"]=merged_flows; manual["etf_flow_metadata"]=etf_auto.get("metadata",{}); events=automatic_macro_events(); prediction_markets=fetch_prediction_market_outlooks(events); live_bls=sum(1 for e in events if e.get("source")=="US BLS official monthly release schedule"); fallback_bls=sum(1 for e in events if "fallback" in str(e.get("source","")).lower()); fomc=sum(1 for e in events if e.get("source")=="Federal Reserve official calendar"); macro_status={"status":"ok" if live_bls>0 else ("degraded" if events else "unavailable"),"live_bls_events":live_bls,"fallback_bls_events":fallback_bls,"fomc_events":fomc,"checked_at":datetime.now(timezone.utc).isoformat(),"note":"Fallback events are estimates from a stored calendar and must not be treated as live-verified." if fallback_bls else ("Official BLS schedule parsed." if live_bls else "No verified official BLS events parsed.")}; out={"stablecoin_30d":None,"fear_greed":None,"headlines":[],"events":events,"macro_status":macro_status,"prediction_markets":prediction_markets,"manual":manual,"etf_flow_metadata":manual["etf_flow_metadata"]}
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
def estimated_liq_clusters(h1,oi_series,price):
 lev=[(5,.10),(10,.25),(20,.30),(50,.20),(100,.15)]; mmr=.005; candles=h1[-168:]
 oi_map=dict(oi_series or []); use_oi=bool(oi_series) and len(oi_series)>=100; width=price*.0025; bins={}
 def add(side,p,amount):
  k=round(p/width); slot=bins.setdefault(k,{"long":0.0,"short":0.0}); slot[side]+=amount
 for i,x in enumerate(candles):
  typical=(x["h"]+x["l"]+x["c"])/3
  w=max(oi_map.get(x["t"],0)-oi_map.get(x["t"]-3600000,0),0) if use_oi else x["v"]
  if w<=0:continue
  long_share=.6 if x["c"]>=x["o"] else .4; later=candles[i+1:]
  lo=min((y["l"] for y in later),default=x["l"]); hi=max((y["h"] for y in later),default=x["h"])
  for levg,weight in lev:
   lp=typical*(1-1/levg+mmr); sp=typical*(1+1/levg-mmr)
   if lp<lo and lp<price:add("long",lp,w*weight*long_share)
   if sp>hi and sp>price:add("short",sp,w*weight*(1-long_share))
 maxv=max([v["long"] for v in bins.values()]+[v["short"] for v in bins.values()]+[1e-9])
 below=[{"price":k*width,"strength":v["long"]/maxv,"dist_pct":(k*width/price-1)*100} for k,v in bins.items() if v["long"]>0 and abs((k*width/price-1)*100)<=12]
 above=[{"price":k*width,"strength":v["short"]/maxv,"dist_pct":(k*width/price-1)*100} for k,v in bins.items() if v["short"]>0 and abs((k*width/price-1)*100)<=12]
 def mass(arr):return sum(x["strength"]/max(abs(x["dist_pct"]),.5) for x in arr if abs(x["dist_pct"])<=6)
 ma,mb=mass(above),mass(below); magnet=(ma-mb)/(ma+mb) if ma+mb else None
 above=sorted(above,key=lambda x:-x["strength"])[:3]; below=sorted(below,key=lambda x:-x["strength"])[:3]
 return above,below,magnet,("OI-delta weighted" if use_oi else "volume-weighted estimate; fewer than 100 hourly OI points")
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
 fund_hist=derivs.get("funding_history") or []
 fund_pr=(sum(1 for x in fund_hist if x<=funding)/len(fund_hist)) if funding is not None and fund_hist else None
 if fund_pr is not None:
  dc.append(("funding_crowding",bounded(-(fund_pr-.5)*2*(.4 if regime in ("UPTREND","DOWNTREND") else 1)),.30,f"funding {funding*100:.4f}% ({fund_pr*100:.0f}th percentile of available history)"))
 elif funding is not None:
  dc.append(("funding_crowding",bounded(-funding/.001)*(.4 if regime in ("UPTREND","DOWNTREND") else 1),.30,f"latest funding {funding*100:.4f}%; historical percentile unavailable"))
  missing.append("historical_funding_percentile")
 else: missing.append("funding_rate")
 ls=derivs.get("global_long_short_ratio")
 if isinstance(ls,dict) and ls.get("longShortRatio") is not None:
  ratio=float(ls["longShortRatio"]); dc.append(("long_short_ratio",bounded(-math.log(max(ratio,.01))*.5),.15,f"account long/short ratio {ratio:.3f}; historical percentile unavailable"))
 else: missing.append("long_short_ratio_history")
 oi_chg=derivs.get("oi_change_24h")
 px_chg=(rows[-1]["c"]/rows[-25]["c"]-1) if len(rows)>=25 and rows[-25]["c"] else 0
 if oi_chg is not None:
  base=.6 if oi_chg>0 else .2
  oscore=(1 if px_chg>0 else -1)*base*min(1,abs(oi_chg)/.05)
  dc.append(("price_vs_oi",oscore,.30,f"price {px_chg*100:+.1f}%, OI {oi_chg*100:+.1f}% over 24h"))
 else:
  dc.append(("price_vs_oi",None,.30,"24h OI change unavailable")); missing.append("open_interest_change_24h")
 if isinstance(derivs.get("basis"),(int,float)):
  dc.append(("perp_basis",bounded(-derivs["basis"]/.001)*.3,.05,f"perpetual basis {derivs['basis']*10000:+.1f} bps"))
 s,comps=layer_mean(dc); scores["derivs"]=s; details["derivs"]={"score":s,"components":comps}
 if s is None: missing.append("derivatives_layer")
 lc=[]; cvd=derivs.get("taker_cvd_24h")
 if cvd is not None:
  lc.append(("taker_cvd_24h",bounded(float(cvd)/.10),.40,f"24h taker CVD {float(cvd)*100:+.1f}% of volume"))
 else:
  tr=derivs.get("taker_buy_sell_ratio")
  if isinstance(tr,dict) and tr.get("buySellRatio") is not None:
   ratio=float(tr["buySellRatio"]); lc.append(("recent_taker_flow",bounded((ratio-1)/.5),.40,f"buy/sell size ratio {ratio:.3f}; recent sample, not 24h CVD"))
   missing.append("taker_cvd_24h")
  else: missing.append("taker_cvd_24h")
 liq_above,liq_below,magnet,liq_method=estimated_liq_clusters(rows,derivs.get("oi_series"),price)
 if magnet is not None: lc.append(("liquidation_magnet",magnet,.30,"estimated liquidation fuel above vs below; "+liq_method))
 else: lc.append(("liquidation_magnet",None,.30,"no liquidation clusters in range; "+liq_method)); missing.append("liquidation_magnet")
 liq=derivs.get("recent_liquidations_usd")
 if isinstance(liq,dict) and (float(liq.get("longs",0))+float(liq.get("shorts",0)))>0:
  longs=float(liq.get("longs",0)); shorts=float(liq.get("shorts",0)); lc.append(("recent_liquidation_flow",.3*(longs-shorts)/(longs+shorts),.20,f"liquidated longs ${longs/1e6:.1f}M vs shorts ${shorts/1e6:.1f}M"))
 else:
  lc.append(("recent_liquidation_flow",None,.20,"liquidation-flow endpoint unavailable")); missing.append("liquidation_flow")
 book=derivs.get("book_imbalance")
 if book is not None: lc.append(("book_imbalance",bounded(float(book)*2),.10,f"top-400-level notional imbalance {float(book):+.2f}"))
 else: lc.append(("book_imbalance",None,.10,"order-book imbalance unavailable")); missing.append("book_imbalance")
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
 etf=(ctx.get("manual",{}).get("etf_flow_5d_usd",{}) or {}).get(coin)
 if etf is not None:
  oc.append(("spot_etf_flow_5d",bounded(float(etf)/({"BTC":1.5e9,"ETH":5e8,"SOL":1e8,"XRP":1e8}[coin])),.4,f"automatically sourced 5-session spot ETF flow ${float(etf)/1e6:+.0f}M"))
 else:
  oc.append(("spot_etf_flow_5d",None,.4,"spot ETF flow unavailable from configured public source"))
  missing.append("spot_etf_flow_5d")
 s,comps=layer_mean(oc); scores["onchain"]=s; details["onchain"]={"score":s,"components":comps}
 if s is None: missing.append("onchain_layer")
 words=[]; terms={"BTC":("bitcoin","btc"),"ETH":("ethereum","ether","eth"),"SOL":("solana","sol"),"XRP":("xrp","ripple")}[coin]
 for title in ctx.get("headlines",[]):
  low=title.lower()
  if not any(re.search(r"\b"+re.escape(t)+r"\b",low) for t in terms): continue
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
 patterns=[]
 cvd=derivs.get("taker_cvd_24h")
 if fund_pr is not None and oi_chg is not None and magnet is not None and px_chg<.005 and fund_pr<.15 and oi_chg>.02 and magnet>0:
  patterns.append({"name":"SHORT_SQUEEZE_SETUP","bias":1,"why":"low funding percentile, rising OI, flat/down price and estimated liquidation fuel above"})
 if fund_pr is not None and oi_chg is not None and magnet is not None and fund_pr>.85 and oi_chg>.03 and magnet<0:
  patterns.append({"name":"LONG_FLUSH_RISK","bias":-1,"why":"high funding percentile, rapidly rising OI and estimated liquidation fuel below"})
 near_high=bool(d1) and price>=.99*max(x["c"] for x in d1[-20:])
 if regime=="UPTREND" and fund_pr is not None and oi_chg is not None and cvd is not None and .30<=fund_pr<=.80 and 0<oi_chg<.08 and cvd>0:
  patterns.append({"name":"HEALTHY_TREND","bias":1,"why":"uptrend, moderate funding percentile, OI growth below 8%, net taker buying"})
 if near_high and cvd is not None and cvd<0 and oi_chg is not None and oi_chg<0:
  patterns.append({"name":"EXHAUSTION","bias":-1,"why":"near 20-day high with negative taker flow and falling OI"})
 adj=max(-.20,min(.20,sum(.10*p["bias"] for p in patterns)))
 if oi_chg is None or cvd is None: missing.append("interaction_patterns_partially_unavailable")
 horizons={}
 daily_returns=[math.log(d1[i]["c"]/d1[i-1]["c"]) for i in range(1,len(d1)) if d1[i]["c"]>0 and d1[i-1]["c"]>0] if d1 else []
 sigma=statistics.pstdev(daily_returns[-30:]) if len(daily_returns)>=2 else None
 for hz,days in (("4h",4/24),("24h",1),("72h",3),("1w",7),("1m",30)):
  w={k:HORIZON_WEIGHTS[hz][k]*REGIME_MULT.get(regime,{}).get(k,1) for k,v in scores.items() if v is not None}
  tw=sum(w.values()); composite=sum(scores[k]*w[k] for k in w)/tw if tw else None
  if composite is not None and hz in ("4h","24h","72h","1w"): composite=bounded(composite+adj)
  coverage=sum(HORIZON_WEIGHTS[hz][k] for k in w)
  p_up=(.5+.2*math.tanh(2*composite)) if composite is not None else None
  band=price*math.exp(sigma*math.sqrt(days)) if sigma is not None else None
  horizons[hz]={"composite":round(composite,3) if composite is not None else None,"p_up":round(p_up,3) if p_up is not None else None,"coverage":round(coverage,3),"bias":"Bullish" if composite is not None and composite>.15 else "Bearish" if composite is not None and composite<-.15 else "Neutral" if composite is not None else "Unavailable","range":[round(price/math.exp(sigma*math.sqrt(days)),8),round(band,8)] if band else None}
 layer_vals=[x for x in scores.values() if x is not None]; dispersion=statistics.pstdev(layer_vals) if len(layer_vals)>1 else .5
 confidence=max(0,min(1,1-dispersion/.6))*(horizons["72h"]["coverage"] or 0)
 if ctx.get("events"): confidence*=.8
 if not ctx.get("events"): missing.append("scheduled_events_not_configured")
 return {"regime":regime,"vol_ratio":round(vol_ratio,3) if vol_ratio is not None else None,"layers":details,"horizons":horizons,"patterns":patterns,"levels":{"liq_above":liq_above,"liq_below":liq_below,"liq_method":liq_method},"confidence":{"value":round(confidence,2),"label":"High" if confidence>=.6 else "Medium" if confidence>=.35 else "Low","notes":((["layers disagree"] if dispersion>.35 else [])+([ "scheduled event within 24h: "+", ".join(e["name"] for e in ctx.get("events",[])) ] if ctx.get("events") else []))},"data_quality":{"missing":sorted(set(missing))},"method":"five-layer weighted model; missing components excluded and weights renormalized; not calibrated"}


def should_log_history(hist,now):
 if not hist:return True
 try:
  last=datetime.fromisoformat(hist[-1]["updated_at"].replace("Z","+00:00"))
  return (now-last).total_seconds()>=3300
 except Exception:return True
def evaluate_model_calibration(hist):
 out={}
 horizons={"4h":4,"24h":24,"72h":72,"1w":168,"1m":720}
 parsed=[]
 for snap in hist:
  try: parsed.append((datetime.fromisoformat(snap["updated_at"].replace("Z","+00:00")),snap))
  except Exception: pass
 parsed.sort(key=lambda x:x[0])
 by_coin={}
 for t,snap in parsed:
  for coin,v in (snap.get("coins",{}) or {}).items():
   if v.get("price") is not None: by_coin.setdefault(coin,[]).append((t,v))
 for hz,hours in horizons.items():
  rows=[]
  for coin,series in by_coin.items():
   ts=[x[0] for x in series]
   for idx,(t,v) in enumerate(series):
    p=((v.get("model",{}).get("horizons",{}) or {}).get(hz,{}) or {}).get("p_up")
    price=v.get("price")
    if p is None or price is None: continue
    target=t+timedelta(hours=hours)
    pos=bisect.bisect_left(ts,target)
    candidates=[k for k in (pos-1,pos) if 0<=k<len(series) and abs((ts[k]-target).total_seconds())<=5400]
    if not candidates: continue
    future=series[min(candidates,key=lambda k:abs((ts[k]-target).total_seconds()))][1]
    rows.append((float(p),1 if future["price"]>price else 0))
  n=len(rows)
  if not n: out[hz]={"n":0}; continue
  brier=sum((p-o)**2 for p,o in rows)/n; bins=[]; edges=[0,.4,.45,.5,.55,.6,1.01]
  for lo,hi in zip(edges[:-1],edges[1:]):
   bucket=[(p,o) for p,o in rows if lo<=p<hi]
   if bucket: bins.append({"range":f"{lo:.2f}-{min(hi,1):.2f}","n":len(bucket),"mean_p":round(sum(p for p,_ in bucket)/len(bucket),3),"freq_up":round(sum(o for _,o in bucket)/len(bucket),3)})
  out[hz]={"n":n,"brier":round(brier,4),"skill_vs_coinflip":round(1-brier/.25,4),"hit_rate":round(sum(1 for p,o in rows if (p>.5)==bool(o))/n,3),"base_rate_up":round(sum(o for _,o in rows)/n,3),"bins":bins}
 return out

def telegram(message):
 token=os.getenv("TELEGRAM_BOT_TOKEN","").strip(); chat=os.getenv("TELEGRAM_CHAT_ID","").strip()
 if not token or not chat:return "not_configured: missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID"
 last_error=None
 for attempt in range(3):
  try:
   r=S.post("https://api.telegram.org/bot"+token+"/sendMessage",json={"chat_id":chat,"text":message,"disable_web_page_preview":True},timeout=20)
   r.raise_for_status()
   try: payload=r.json()
   except Exception: return "failed: Telegram returned non-JSON response"
   # Telegram may return HTTP 200 with ok=false; do not mistake that for delivery.
   if not payload.get("ok"):
    return "failed: Telegram API "+str(payload.get("error_code","unknown"))+": "+str(payload.get("description","unspecified error"))[:180]
   return "sent: message_id="+str((payload.get("result") or {}).get("message_id","unknown"))
  except Exception as e:
   last_error=e
   if attempt<2: time.sleep(2*(attempt+1))
 return "failed after 3 attempts: "+str(last_error)[:180]
def update_event_tracker(events,snap,hist,now_dt):
 """Persist pre-event crypto forecasts and evaluate post-event market moves when timestamps are precise."""
 path=DATA/"event_tracker.json"
 try: store=json.loads(path.read_text(encoding="utf-8"))
 except Exception: store={"events":[]}
 records={x.get("key"):x for x in store.get("events",[]) if x.get("key")}
 history=[]
 for row in hist:
  try: history.append((datetime.fromisoformat(row["updated_at"].replace("Z","+00:00")),row))
  except Exception: pass
 history.sort(key=lambda x:x[0])
 def snapshot_at(ts,max_gap_hours=2):
  if not history:return None
  candidates=[(abs((t-ts).total_seconds()),row) for t,row in history if abs((t-ts).total_seconds())<=max_gap_hours*3600]
  return min(candidates,key=lambda x:x[0])[1] if candidates else None
 def prices(row):
  return {c:{"price":v.get("price"),"model":{hz:((v.get("model",{}).get("horizons",{}) or {}).get(hz,{}) or {}).get("p_up") for hz in ("4h","24h","72h")},"analogue":{hz:next((f.get("median_return_pct") for f in v.get("forecasts",[]) if f.get("horizon")==hz),None) for hz in ("4h","24h","72h")}} for c,v in (row.get("coins",{}) or {}).items() if v.get("price") is not None and "error" not in v}
 # Revisit already-tracked events even after they disappear from the upcoming-calendar window,
 # so 24h/72h outcomes can mature after the event date.
 event_list={x.get("key"):x.get("event",{}) for x in records.values() if x.get("key")}
 for e in events or []:
  k="|".join((str(e.get("name","Event")),str(e.get("date_utc","")),str(e.get("source",""))))
  event_list[k]=e
 for e in event_list.values():
  key="|".join((str(e.get("name","Event")),str(e.get("date_utc","")),str(e.get("source",""))))
  rec=records.get(key,{"key":key,"event":e,"first_seen_utc":now_dt.isoformat(),"status":"scheduled","pre_event_history":[],"outcomes":{}})
  rec["event"]=e
  exact=bool(e.get("time_exact") and e.get("time_utc"))
  try: event_dt=datetime.fromisoformat(e["time_utc"].replace("Z","+00:00")) if exact else None
  except Exception: event_dt=None
  if event_dt and now_dt < event_dt:
   rec["status"]="scheduled"
   current={"captured_at":now_dt.isoformat(),"coins":prices({"coins":snap.get("coins",{})})}
   last=rec.get("pre_event_history",[])
   if not last or (now_dt-datetime.fromisoformat(last[-1]["captured_at"])).total_seconds()>=3300:
    last.append(current)
   rec["pre_event_history"]=last[-48:]
   rec["latest_pre_event"]=current
  elif not exact:
   rec["status"]="date_only_event_time_unknown"
   rec["note"]="Calendar provides a date but not an exact release time; intraday event attribution is not calculated."
  else:
   rec["status"]="event_time_passed"
   base_row=snapshot_at(event_dt,2)
   base_prices=prices(base_row) if base_row else {}
   rec["event_baseline"]={"target_time_utc":event_dt.isoformat(),"snapshot_time_utc":base_row.get("updated_at") if base_row else None,"coins":base_prices}
   for label,hours in (("4h",4),("24h",24),("72h",72)):
    target=event_dt+timedelta(hours=hours)
    if now_dt < target: continue
    future=snapshot_at(target,1.5)
    if not future: continue
    out={}
    for coin,b in base_prices.items():
     f=(future.get("coins",{}) or {}).get(coin,{})
     if b.get("price") and f.get("price"):
      out[coin]={"baseline_price":b["price"],"observed_price":f["price"],"return_pct":round((f["price"]/b["price"]-1)*100,3),"observation_time_utc":future.get("updated_at")}
    rec.setdefault("outcomes",{})[label]={"target_time_utc":target.isoformat(),"coins":out}
   if all(k in rec.get("outcomes",{}) for k in ("4h","24h","72h")): rec["status"]="outcomes_complete"
   elif rec.get("outcomes"): rec["status"]="outcomes_partial"
  records[key]=rec
 # Retain latest 100 calendar events and their pre-event snapshots/outcomes.
 ordered=sorted(records.values(),key=lambda x:(x.get("event",{}).get("date_utc",""),x.get("event",{}).get("name","")),reverse=True)[:100]
 store={"updated_at":now_dt.isoformat(),"method":"Official calendar date/time plus archived model outlooks and observed crypto returns; no causal claim that the event caused the move.","events":ordered}
 write_json("event_tracker.json",store)
 return store

def main():
 now=datetime.now(timezone.utc).isoformat(); model_context=global_model_context(); model_context["btc_daily"]=daily_candles("BTCUSDT"); snap={"updated_at":now,"source":"OKX public hourly candles plus five-layer model; missing components disclosed","model_context":{"fear_greed":model_context.get("fear_greed",[])[-1] if model_context.get("fear_greed") else None,"stablecoin_30d":model_context.get("stablecoin_30d"),"headline_count":len(model_context.get("headlines",[])),"events":model_context.get("events",[]),"macro_status":model_context.get("macro_status",{}),"prediction_markets":model_context.get("prediction_markets",{}),"etf_flows":model_context.get("etf_flow_metadata",{})},"coins":{}}; issues=[]
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
  except Exception as e:
   issues.append(coin+": "+str(e)[:200]); snap["coins"][coin]={"coin":coin,"symbol":symbol,"error":str(e)[:200]}
 successful=sum(1 for v in snap["coins"].values() if "error" not in v)
 if successful==0:
  write_json("status.json",{"updated_at":now,"ok":False,"coins_updated":[],"issues":issues,"message":"All assets failed; previous latest.json preserved."})
  raise RuntimeError("All four assets failed; keeping previous latest.json")
 write_json("latest.json",snap); write_json("forecasts.json",{"updated_at":now,"items":[{"coin":c,"price":v["price"],**f} for c,v in snap["coins"].items() if "error" not in v for f in v["forecasts"]]})
 status={"updated_at":now,"ok":bool(snap["coins"]),"coins_updated":[c for c,v in snap["coins"].items() if "error" not in v],"issues":issues}; histpath=DATA/"history.json"
 try: hist=json.loads(histpath.read_text(encoding="utf-8"))
 except Exception: hist=[]
 now_dt=datetime.fromisoformat(now.replace("Z","+00:00"))
 if should_log_history(hist,now_dt):
  hist.append({"updated_at":now,"coins":{c:{"price":v["price"],"forecasts":v["forecasts"],"model":v.get("model")} for c,v in snap["coins"].items() if "error" not in v}})
 hist=hist[-3000:]
 write_json("history.json",hist)
 event_tracker=update_event_tracker(snap.get("model_context",{}).get("events",[]),snap,hist,now_dt)
 write_json("calibration.json",{"updated_at":now,"note":"Calibration requires future hourly observations; treat early samples cautiously. n>=100 per horizon is preferred.","horizons":evaluate_model_calibration(hist)})
 state_path=DATA/"notification_state.json"
 try: nstate=json.loads(state_path.read_text(encoding="utf-8"))
 except Exception: nstate={}
 hour_key=now_dt.strftime("%Y-%m-%dT%H"); notify_lines=[]; event_keys=[]
 def add_event(key,line):
  previous=(nstate.get("last_event_sent",{}) or {}).get(key)
  try: recent=previous and (now_dt-datetime.fromisoformat(previous.replace("Z","+00:00"))).total_seconds()<4*3600
  except Exception: recent=False
  if not recent: notify_lines.append(line); event_keys.append(key)
 for c,v in snap["coins"].items():
  if "error" in v: continue
  ch1=float(v.get("change_1h_pct") or 0); ch4=float(v.get("change_4h_pct") or 0); vr=float(v.get("volume_ratio_24h") or 0)
  if abs(ch1)>=1.5: add_event(c+":price-impulse",f"PRICE MOVE — {c}: {ch1:+.2f}% over 1h; 4h {ch4:+.2f}%. Confirm candle close.")
  if vr>=2.0: add_event(c+":volume-spike",f"VOLUME SPIKE — {c}: {vr:.2f}x baseline; volume alone is not a signal.")
  if ch1*ch4<0 and abs(ch1)>=.8 and abs(ch4)>=1: add_event(c+":possible-reversal",f"POSSIBLE REVERSAL — {c}: 1h {ch1:+.2f}% opposes 4h {ch4:+.2f}%.")
  d=v.get("derivatives",{}) or {}; oi=d.get("oi_change_24h")
  if isinstance(oi,(int,float)) and abs(oi)>=.08: add_event(c+":oi-shift",f"OPEN INTEREST — {c}: 24h change {oi*100:+.1f}%; direction is not implied.")
  fr=d.get("funding_rate")
  if isinstance(fr,list) and fr: fr=fr[-1]
  try: funding=float(fr.get("fundingRate")) if isinstance(fr,dict) and fr.get("fundingRate") is not None else None
  except Exception: funding=None
  if funding is not None and abs(funding)>=.0005: add_event(c+":funding-extreme",f"FUNDING — {c}: {funding*100:.4f}% per interval; verify source and interval.")
  liq=d.get("recent_liquidations_usd")
  if isinstance(liq,dict):
   longs=float(liq.get("longs") or 0); shorts=float(liq.get("shorts") or 0)
   if max(longs,shorts)>=1000000: add_event(c+":liquidation",f"LIQUIDATIONS — {c}: recent sample longs {longs:,.0f} USD; shorts {shorts:,.0f} USD.")
  m=v.get("model",{}) or {}; h=(m.get("horizons",{}) or {}).get("24h",{}) or {}; p=h.get("p_up"); conf=(m.get("confidence",{}) or {}).get("value",0)
  prior=(nstate.get("last_model",{}) or {}).get(c,{})
  if isinstance(p,(int,float)) and isinstance(prior.get("p_up"),(int,float)):
   op=prior["p_up"]
   if (op-.5)*(p-.5)<0 or abs(p-op)>=.12: add_event(c+":model-shift",f"MODEL OUTLOOK CHANGE — {c}: P(up) {op*100:.1f}% → {p*100:.1f}%; confidence {conf:.2f}. Uncalibrated.")
 if issues: add_event("data:collection-issues","DATA QUALITY — "+"; ".join(issues[:4]))
 missing=[]
 for c,v in snap["coins"].items():
  if "error" in v: missing.append(c+": model unavailable"); continue
  names=((v.get("model",{}) or {}).get("data_quality",{}) or {}).get("missing",[]) or []
  if names: missing.append(c+": "+", ".join(str(x) for x in names[:3]))
 if missing: add_event("data:missing-inputs","DATA QUALITY — missing inputs: "+"; ".join(missing[:4]))
 for c,v in snap["coins"].items():
  if "error" in v: continue
  p=((v.get("model",{}) or {}).get("horizons",{}) or {}).get("24h",{}).get("p_up")
  f24=next((x for x in v.get("forecasts",[]) if x.get("horizon")=="24h"),{}); med=f24.get("median_return_pct")
  if isinstance(p,(int,float)) and isinstance(med,(int,float)) and ((p>.55 and med<-.2) or (p<.45 and med>.2)):
   add_event(c+":forecast-conflict",f"CONFLICTING FORECASTS — {c}: model P(up) {p*100:.1f}% vs analogue median {med:+.3f}%.")
 # Derive provisional trade levels only when model direction, confidence and data coverage pass thresholds.
 generated_setups={}
 for c,v in snap["coins"].items():
  if "error" in v: continue
  m=v.get("model",{}) or {}; h=(m.get("horizons",{}) or {}).get("24h",{}) or {}
  p=h.get("p_up"); coverage=h.get("coverage",0); conf=(m.get("confidence",{}) or {}).get("value",0)
  price=v.get("price"); band=h.get("range")
  if not isinstance(p,(int,float)) or not isinstance(price,(int,float)) or not isinstance(band,list) or len(band)<2: continue
  side="long" if p>=.60 else "short" if p<=.40 else None
  if side is None or conf<.50 or coverage<.65: continue
  edge=float(band[0] if side=="long" else band[1]); risk=price-edge if side=="long" else edge-price
  if risk<=0: continue
  risk=max(risk,price*.005); stop=price-risk if side=="long" else price+risk
  target=price+1.5*risk if side=="long" else price-1.5*risk
  setup={"coin":c,"side":side,"reference_price":price,"stop_loss":round(stop,8),"take_profit_1":round(target,8),"risk_pct":round(risk/price*100,2),"reward_risk":1.5,"p_up":p,"confidence":conf,"coverage":coverage}
  generated_setups[c]=setup; prev=(nstate.get("last_setup",{}) or {}).get(c,{})
  if prev.get("side")!=side or abs(float(prev.get("reference_price",price))-price)/price>=.015:
   add_event("auto-setup:"+c+":"+side,f"PROVISIONAL SETUP — {c} {side.upper()} | reference ${price:,.6g} | SL ${stop:,.6g} | TP1 ${target:,.6g} | model P(up) {p*100:.1f}% | confidence {conf:.2f}. Experimental and uncalibrated; confirm structure before trading.")
 nstate["last_setup"]=generated_setups
 if nstate.get("last_hourly_utc_hour")!=hour_key or os.getenv("TELEGRAM_FORCE_SEND","").lower() in ("1","true","yes"):
  lines=["📊 CRYPTO CHECKPOINT","🕒 "+now_dt.strftime("%d %b %Y · %H:%M UTC"),"━━━━━━━━━━━━━━━━━━","⚠️ Experimental model; probabilities are not calibrated.",""]
  for c in ("BTC","ETH","XRP","SOL"):
   v=snap["coins"].get(c,{})
   if "error" in v:
    lines.extend([f"🔴 {c}  |  DATA UNAVAILABLE",f"   {str(v.get('error',''))[:90]}",""])
    continue
   m=v.get("model",{}) or {}; conf=(m.get("confidence",{}) or {}).get("label","unknown")
   ch=float(v.get("change_1h_pct") or 0)
   arrow="🟢" if ch>0 else "🔴" if ch<0 else "⚪"
   price=float(v.get("price") or 0)
   icon={"BTC":"🟠","ETH":"🔵","XRP":"🟣","SOL":"🟢"}.get(c,"⚪")
   lines.append(f"{icon} {c}  ${price:,.6g}  |  1h {arrow}{ch:+.2f}%")
   lines.append(f"   Confidence: {conf}")
   mh=(m.get("horizons",{}) or {})
   for horizon,label in (("4h","4H"),("24h","24H"),("72h","72H")):
    hh=mh.get(horizon,{}) or {}; bias=hh.get("bias","Unavailable")
    analogue=next((x for x in v.get("forecasts",[]) if x.get("horizon")==horizon),{})
    medh=analogue.get("median_return_pct"); band=analogue.get("range_10_90_pct") or []
    pp=hh.get("p_up"); freq=analogue.get("up_frequency_pct")
    if isinstance(medh,(int,float)) and price>0:
     target=price*(1+medh/100)
     row=f"   {label} {bias} · target ${target:,.6g} ({medh:+.2f}%)"
    elif isinstance(pp,(int,float)):
     row=f"   {label} {bias} · P(up) {pp*100:.0f}%"
    else:
     row=f"   {label} outlook unavailable"
    if price>0 and len(band)>=2:
     low=price*(1+float(band[0])/100); high=price*(1+float(band[1])/100)
     row+=f" · range ${low:,.6g}–${high:,.6g}"
    if isinstance(freq,(int,float)): row+=f" · analogue up {freq:.0f}%"
    lines.append(row)
   lines.append("")
  # ETF flows: display the reported rolling five-session net total and source window.
  etfs=(snap.get("model_context",{}).get("etf_flows",{}) or {})
  lines.extend(["━━━━━━━━━━━━━━━━━━","💸 ETF INFLOWS / OUTFLOWS (5 SESSIONS)"])
  etf_rows=[]
  for coin in ("BTC","ETH","SOL","XRP"):
   meta=etfs.get(coin,{}) or {}
   value=meta.get("five_session_net_flow_usd")
   if value is None:
    lines.append(f"• {coin}: unavailable"+(f" — {str(meta.get('error'))[:85]}" if meta.get("error") else ""))
    continue
   sign="🟢" if value>0 else "🔴" if value<0 else "⚪"
   direction="net inflow" if value>0 else "net outflow" if value<0 else "flat"
   start=meta.get("period_start","?"); end=meta.get("period_end","?")
   lines.append(f"{sign} {coin}: {value/1_000_000:+,.1f}M USD · {direction}")
   lines.append(f"   Window: {start} to {end} · {meta.get('sessions','?')} sessions")
   if meta.get("caveat"): lines.append(f"   Note: {str(meta.get('caveat'))[:130]}")
  lines.append("Source window is shown per asset; XRP flows are holdings-derived estimates, not an identical ETF-table methodology.")
  evs=snap.get("model_context",{}).get("events",[]) or []
  lines.extend(["","━━━━━━━━━━━━━━━━━━","🗓 MACRO EVENTS & PRE-EVENT OUTLOOK"])
  if evs:
   for e in evs[:3]:
    event_name=str(e.get("name") or "Macro event")
    exact=bool(e.get("time_exact") and e.get("time_utc"))
    when=e.get("time_utc") if exact else (e.get("date_utc") or "date not confirmed")
    if exact:
     try: when=datetime.fromisoformat(str(when).replace("Z","+00:00")).strftime("%d %b · %H:%M UTC")
     except Exception: pass
    else: when=str(when)[:10]+" · time unconfirmed"
    countdown=(f" · T−{e.get('in_hours'):.0f}h" if isinstance(e.get('in_hours'),(int,float)) else f" · T−{e.get('days_away'):.1f}d" if isinstance(e.get('days_away'),(int,float)) else "")
    key="|".join((str(e.get("name","Event")),str(e.get("date_utc","")),str(e.get("source",""))))
    tracked=next((x for x in event_tracker.get("events",[]) if x.get("key")==key),{})
    tracking=str(tracked.get("status","tracking pending")).replace("_"," ")
    lines.append(f"• {event_name} — {when}{countdown}")
    lines.append(f"   Tracker: {tracking}")
    # Show the current model outlook as a pre-event baseline, not a claimed causal impact forecast.
    for coin in ("BTC","ETH"):
     cv=snap.get("coins",{}).get(coin,{})
     if "error" in cv: continue
     mh=(cv.get("model",{}) or {}).get("horizons",{}) or {}
     hh=mh.get("24h",{}) or {}; pp=hh.get("p_up"); bias=hh.get("bias","unavailable")
     fc=next((x for x in cv.get("forecasts",[]) if x.get("horizon")=="24h"),{})
     med=fc.get("median_return_pct")
     if isinstance(pp,(int,float)):
      outlook=f"{coin} 24h baseline: {bias}, P(up) {pp*100:.0f}%"
      if isinstance(med,(int,float)): outlook+=f", analogue median {med:+.2f}%"
      lines.append("   "+outlook)
    lines.append("   Scenario note: this is the current market baseline, not a prediction of the event result or causal price impact.")
  else:
   lines.append("Calendar unavailable this run; retrying next refresh.")
  # Market-implied macro scenarios from Polymarket; separate odds from crypto price impact.
  pm=(snap.get("model_context",{}).get("prediction_markets",{}) or {})
  lines.extend(["","━━━━━━━━━━━━━━━━━━","🔮 MACRO CONSENSUS & CRYPTO SCENARIOS"])
  if pm.get("markets"):
   for market in pm.get("markets",[])[:3]:
    lines.append(f"• {str(market.get('title','Macro market'))[:115]}")
    lines.append(f"   Source: Polymarket · 24h volume {float(market.get('volume_24h_usd') or 0):,.0f} USD")
    for outcome in market.get("outcomes",[])[:2]:
     odds=", ".join(f"{x.get('outcome')}: {x.get('probability_pct')}%" for x in outcome.get("outcomes",[])[:4])
     lines.append(f"   {str(outcome.get('question',''))[:75]} — {odds}")
   lines.append("Scenario guide (directional, not a guaranteed price target):")
   lines.append("• Softer inflation / less-hawkish Fed: usually supportive if yields and USD fall.")
   lines.append("• Hotter inflation / more-hawkish Fed: usually a headwind if yields and USD rise.")
   lines.append("• Mixed result or already-priced news: whipsaw risk; watch US 2Y yield, DXY and BTC reaction.")
   lines.append("Polymarket outcome odds are not coin-price targets.")
  else:
   lines.append("No relevant live Polymarket macro market was parsed this run.")
   lines.append("Softer inflation / less-hawkish Fed may support crypto if yields and USD fall; hotter inflation / hawkish guidance may pressure crypto if yields and USD rise.")
   if pm.get("error"): lines.append("Feed status: "+str(pm.get("error"))[:130])
  lines.append("Odds are market-implied estimates, may be thinly traded, and are not guarantees.")
  lines.extend(["","━━━━━━━━━━━━━━━━━━","🎯 TRADE PLAN"])
  if generated_setups:
   for c,su in generated_setups.items():
    lines.append(f"• {c} {su['side'].upper()}  |  Ref ${su['reference_price']:,.6g}")
    lines.append(f"   SL ${su['stop_loss']:,.6g}  ·  TP1 ${su['take_profit_1']:,.6g}  ·  R:R 1:{su['reward_risk']:.1f}")
   lines.append("⚠️ Verify chart structure and position risk before entry.")
  else:
   lines.append("⏸ NO TRADE")
   lines.append("No setup passed direction, confidence and coverage filters.")
  lines.extend(["","━━━━━━━━━━━━━━━━━━","ℹ️ Alerts for OI, funding, liquidations and data issues are sent separately."])
  notify_lines.insert(0,"\n".join(lines)); nstate["last_hourly_utc_hour"]=hour_key
 if notify_lines:
  message="\n\n".join(notify_lines)
  if len(message)>3800: message=message[:3750]+"\n… truncated; see Forecast Lab."
  status["telegram"]=telegram(message)
  if str(status["telegram"]).startswith("sent"):
   for key in event_keys: nstate.setdefault("last_event_sent",{})[key]=now
   nstate["last_model"]={c:{"p_up":((v.get("model",{}).get("horizons",{}) or {}).get("24h",{}) or {}).get("p_up")} for c,v in snap["coins"].items() if "error" not in v}
   write_json("notification_state.json",nstate)
 elif not os.getenv("TELEGRAM_BOT_TOKEN","").strip() or not os.getenv("TELEGRAM_CHAT_ID","").strip():
  status["telegram"]="not_configured"
 write_json("status.json",status); print(json.dumps(status,indent=2))
 if not snap["coins"]: raise RuntimeError("No coin data updated; inspect status.json")
if __name__=="__main__":main()
