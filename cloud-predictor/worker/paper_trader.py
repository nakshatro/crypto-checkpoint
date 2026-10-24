#!/usr/bin/env python3
"""Paper-trade model setups against completed OKX hourly candles. No real orders."""
import json, math
from datetime import datetime, timezone
from pathlib import Path
import requests
ROOT=Path(__file__).resolve().parents[1]; DATA=ROOT/"docs"/"data"; DATA.mkdir(parents=True,exist_ok=True)
S=requests.Session(); S.headers.update({"User-Agent":"CryptoCheckpointPaperTrader/1.0"})
SYMS={"BTC":"BTC-USDT-SWAP","ETH":"ETH-USDT-SWAP","XRP":"XRP-USDT-SWAP","SOL":"SOL-USDT-SWAP"}
FEE_SIDE=.0005; SLIP_SIDE=.0002; MAX_HOURS=72; REENTRY_COOLDOWN_HOURS=72; MAX_OPEN_TRADES=2
def read(p,d):
 try:return json.loads(p.read_text(encoding="utf-8"))
 except Exception:return d
def candles(inst,after):
 r=S.get("https://www.okx.com/api/v5/market/candles",params={"instId":inst,"bar":"1H","limit":"100"},timeout=(5,12)); r.raise_for_status(); j=r.json()
 if str(j.get("code"))!="0": raise RuntimeError(j.get("msg","OKX candle error"))
 return sorted([{"t":int(x[0]),"o":float(x[1]),"h":float(x[2]),"l":float(x[3]),"c":float(x[4])} for x in j.get("data",[]) if len(x)>=9 and str(x[8])=="1" and int(x[0])>=after],key=lambda x:x["t"])
def candidate(coin,v,now):
 h=((v.get("model") or {}).get("horizons") or {}).get("24h") or {}; p=h.get("p_up"); conf=((v.get("model") or {}).get("confidence") or {}).get("value",0); cov=h.get("coverage",0); price=v.get("price"); band=h.get("range")
 if not isinstance(p,(int,float)) or not isinstance(price,(int,float)) or not isinstance(band,list) or len(band)<2:return None
 side="long" if p>=.60 else "short" if p<=.40 else None
 if not side or conf<.50 or cov<.65:return None
 edge=float(band[0] if side=="long" else band[1]); risk=price-edge if side=="long" else edge-price
 if risk<=0:return None
 risk=max(risk,price*.005); stop=price-risk if side=="long" else price+risk; tp=price+1.5*risk if side=="long" else price-1.5*risk
 stamp=v.get("price_time") or now
 try: dt=datetime.fromisoformat(stamp.replace("Z","+00:00"))
 except Exception: dt=datetime.fromisoformat(now.replace("Z","+00:00"))
 key=coin+"|"+side+"|"+dt.strftime("%Y-%m-%dT%H")
 fr=((v.get("derivatives") or {}).get("funding_rate") or {})
 if isinstance(fr,list): fr=fr[-1] if fr else {}
 try: funding_rate=float(fr.get("fundingRate")) if isinstance(fr,dict) and fr.get("fundingRate") is not None else None
 except Exception: funding_rate=None
 return {"trade_id":key,"coin":coin,"side":side,"funding_rate_at_entry":funding_rate,"funding_source_at_entry":fr.get("source") if isinstance(fr,dict) else None,"signal_time_utc":dt.isoformat(),"entry_price":float(price),"stop_loss":round(stop,10),"take_profit_1":round(tp,10),"p_up":float(p),"confidence":float(conf),"coverage":float(cov),"risk_pct":round(risk/price*100,4),"reward_risk":1.5,"status":"open","exit_price":None,"exit_time_utc":None,"exit_reason":None,"gross_return_pct":None,"net_return_pct":None,"round_trip_cost_pct":round(2*(FEE_SIDE+SLIP_SIDE)*100,4),"evaluation":"Completed 1h candles; if stop and target both hit in one candle, stop is assumed first."}
def close(t,price,ts,reason):
 e=t["entry_price"]; gross=(price/e-1)*100 if t["side"]=="long" else (e/price-1)*100; cost=2*(FEE_SIDE+SLIP_SIDE)*100
 start=datetime.fromisoformat(t["signal_time_utc"].replace("Z","+00:00")); end=datetime.fromtimestamp(ts/1000,timezone.utc)
 intervals=max(0,int((end-start).total_seconds()//(8*3600)))
 rate=t.get("funding_rate_at_entry"); funding_cost=(float(rate)*intervals*100*(1 if t["side"]=="long" else -1)) if isinstance(rate,(int,float)) else None
 net=gross-cost-(funding_cost or 0)
 t.update(status="closed",exit_price=round(price,10),exit_time_utc=end.isoformat(),exit_reason=reason,gross_return_pct=round(gross,4),fees_slippage_cost_pct=round(cost,4),funding_intervals_estimate=intervals,funding_cost_pct_estimate=round(funding_cost,4) if funding_cost is not None else None,funding_note="Estimate using entry funding rate held constant; not historical realized funding." if funding_cost is not None else "Funding unavailable; net return excludes funding.",net_return_pct=round(net,4))
def main():
 now=datetime.now(timezone.utc).isoformat(); snap=read(DATA/"latest.json",{}); path=DATA/"paper_trades.json"
 store=read(path,{"created_at":now,"method":"Paper simulation only. No real orders. One open position per coin; 72h re-entry cooldown; top-ranked candidates only; max two simultaneous trades.","assumptions":{"fee_per_side_pct":FEE_SIDE*100,"slippage_per_side_pct":SLIP_SIDE*100,"max_hold_hours":MAX_HOURS,"reentry_cooldown_hours":REENTRY_COOLDOWN_HOURS,"max_simultaneous_open_trades":MAX_OPEN_TRADES,"same_candle_stop_target":"stop first","funding":"estimated from entry funding rate and 8h intervals; not realized historical funding"},"trades":[]})
 trades=store.get("trades",[]); errors=[]
 store["method"]="Paper simulation only. One open position per coin; 72h re-entry cooldown; strongest eligible candidates selected; max two simultaneous trades."
 store["assumptions"]={"fee_per_side_pct":FEE_SIDE*100,"slippage_per_side_pct":SLIP_SIDE*100,"max_hold_hours":MAX_HOURS,"reentry_cooldown_hours":REENTRY_COOLDOWN_HOURS,"max_simultaneous_open_trades":MAX_OPEN_TRADES,"same_candle_stop_target":"stop first","funding":"estimated from entry funding rate and 8h intervals; not realized historical funding"}
 for t in trades:
  if t.get("status")!="open":continue
  try:
   start=int(datetime.fromisoformat(t["signal_time_utc"].replace("Z","+00:00")).timestamp()*1000)
   for c in candles(SYMS[t["coin"]],start+1):
    sl=(c["l"]<=t["stop_loss"]) if t["side"]=="long" else (c["h"]>=t["stop_loss"])
    tp=(c["h"]>=t["take_profit_1"]) if t["side"]=="long" else (c["l"]<=t["take_profit_1"])
    if sl:close(t,t["stop_loss"],c["t"]+3600000,"stop_loss");break
    if tp:close(t,t["take_profit_1"],c["t"]+3600000,"take_profit_1");break
    if c["t"]+3600000>=start+MAX_HOURS*3600000:close(t,c["c"],c["t"]+3600000,"time_exit_72h");break
  except Exception as e:errors.append(t["coin"]+": "+str(e)[:150])
 # Enforce one open position per coin, a 72h cooldown after the latest entry,
 # and a portfolio cap. Rank all qualifying candidates, then take only the strongest.
 now_dt=datetime.now(timezone.utc)
 active={t["coin"] for t in trades if t.get("status")=="open"}
 last_entry={}
 for old in trades:
  try:
   dt=datetime.fromisoformat(old["signal_time_utc"].replace("Z","+00:00"))
   if old["coin"] not in last_entry or dt>last_entry[old["coin"]]:last_entry[old["coin"]]=dt
  except Exception:pass
 open_count=sum(t.get("status")=="open" for t in trades)
 candidates=[]
 for coin,v in (snap.get("coins") or {}).items():
  if coin not in SYMS or "error" in v or coin in active:continue
  last=last_entry.get(coin)
  if last and (now_dt-last).total_seconds()<REENTRY_COOLDOWN_HOURS*3600:continue
  t=candidate(coin,v,now)
  if not t or any(x.get("trade_id")==t["trade_id"] for x in trades):continue
  # Rank signal strength using probability distance from 50%, confidence, coverage,
  # with a mild penalty for larger stop distance. Ranking selects, it does not promise edge.
  t["selection_score"]=round(abs(t["p_up"]-.5)*2*0.50+t["confidence"]*0.30+t["coverage"]*0.20-min(t["risk_pct"]/100,0.10)*0.10,6)
  candidates.append(t)
 candidates.sort(key=lambda t:(t["selection_score"],t["confidence"],t["coverage"]),reverse=True)
 slots=max(0,MAX_OPEN_TRADES-open_count)
 for t in candidates[:slots]:
  t["selection_rule"]="Top-ranked eligible signal; one open trade per coin; 72h re-entry cooldown; max two simultaneous portfolio trades."
  trades.append(t);active.add(t["coin"])
 closed=[t for t in trades if t.get("status")=="closed"]; wins=sum((t.get("net_return_pct") or 0)>0 for t in closed)
 summary={"closed_trades":len(closed),"open_trades":sum(t.get("status")=="open" for t in trades),"wins_net":wins,"losses_or_flat_net":len(closed)-wins,"win_rate_net_pct":round(100*wins/len(closed),2) if closed else None,"mean_net_return_pct":round(sum(t.get("net_return_pct",0) or 0 for t in closed)/len(closed),4) if closed else None,"compounded_net_return_pct":round((math.prod(1+(t.get("net_return_pct",0) or 0)/100 for t in closed)-1)*100,4) if closed else None}
 store.update(updated_at=now,trades=trades[-2000:],summary=summary,errors=errors[-20:]);path.write_text(json.dumps(store,indent=2,allow_nan=False),encoding="utf-8")
 print(json.dumps({"updated_at":now,"summary":summary,"errors":errors},indent=2))
if __name__=="__main__":main()
