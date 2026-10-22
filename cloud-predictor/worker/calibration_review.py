#!/usr/bin/env python3
"""Per-coin, per-horizon forecast evaluation with leakage-safe expanding base-rate baseline."""
import json,bisect
from datetime import datetime,timezone,timedelta
from pathlib import Path
DATA=Path(__file__).resolve().parents[1]/"docs"/"data"
def main():
 try: hist=json.loads((DATA/"history.json").read_text(encoding="utf-8"))
 except Exception: hist=[]
 series={}
 for row in hist:
  try:t=datetime.fromisoformat(row["updated_at"].replace("Z","+00:00"))
  except Exception:continue
  for c,v in (row.get("coins") or {}).items():
   if v.get("price") is not None and "error" not in v:series.setdefault(c,[]).append((t,v))
 out={"updated_at":datetime.now(timezone.utc).isoformat(),"method":"Nearest archived snapshot within 90m of target. Historical base-rate baseline is expanding and uses only earlier matured outcomes for that coin/horizon (starts at 50% until prior outcomes exist), avoiding look-ahead. Separate coin/horizon samples; overlapping forecast windows are not independent.","horizons":{}}
 for hz,hours in (("4h",4),("24h",24),("72h",72)):
  result={}; pooled=[]
  for coin,ss in sorted(series.items()):
   ss.sort(key=lambda z:z[0]); ts=[z[0] for z in ss]; raw=[]
   for t,v in ss:
    p=((v.get("model",{}).get("horizons") or {}).get(hz) or {}).get("p_up"); price=v.get("price")
    if not isinstance(p,(int,float)) or not isinstance(price,(int,float)):continue
    target=t+timedelta(hours=hours); pos=bisect.bisect_left(ts,target); cand=[k for k in (pos-1,pos) if 0<=k<len(ss) and abs((ts[k]-target).total_seconds())<=5400]
    if not cand:continue
    k=min(cand,key=lambda z:abs((ts[z]-target).total_seconds())); fp=ss[k][1].get("price")
    if isinstance(fp,(int,float)):raw.append({"forecast_time_utc":t.isoformat(),"outcome_time_utc":ts[k].isoformat(),"price_at_forecast":price,"price_at_outcome":fp,"p_up":round(float(p),4),"actual_up":bool(fp>price),"return_pct":round((fp/price-1)*100,4)})
   # Calculate baseline probability using only outcomes that matured before this forecast was made.
   matured=[]
   for r in raw:
    ft=datetime.fromisoformat(r["forecast_time_utc"]); prior=[x for x in matured if x["outcome_dt"]<=ft]
    r["base_rate_p_up"]=round((sum(x["outcome"] for x in prior)+0.5)/(len(prior)+1),4)
    matured.append({"outcome_dt":datetime.fromisoformat(r["outcome_time_utc"]),"outcome":int(r["actual_up"])})
   result[coin]=score(raw);pooled.extend(raw)
  result["ALL_COINS_POOLED"]=score(pooled);out["horizons"][hz]=result
 (DATA/"calibration_detailed.json").write_text(json.dumps(out,indent=2,allow_nan=False),encoding="utf-8")
 print(json.dumps({h:{c:{k:v for k,v in s.items() if k!="rows"} for c,s in r.items()} for h,r in out["horizons"].items()},indent=2))
def score(rows):
 n=len(rows)
 if not n:return {"n":0,"rows":[]}
 y=[int(r["actual_up"]) for r in rows];p=[r["p_up"] for r in rows];basep=[r["base_rate_p_up"] for r in rows]
 b=sum((a-b)**2 for a,b in zip(p,y))/n;cb=sum((a-b)**2 for a,b in zip(basep,y))/n
 return {"n":n,"brier":round(b,5),"coinflip_brier":.25,"skill_vs_coinflip":round(1-b/.25,5),"expanding_historical_base_rate_brier":round(cb,5),"skill_vs_expanding_historical_rate":round(1-b/cb,5) if cb else None,"directional_hit_rate":round(sum((a>.5)==bool(yv) for a,yv in zip(p,y))/n,4),"rows":rows}
if __name__=="__main__":main()
