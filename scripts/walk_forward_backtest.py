#!/usr/bin/env python3
"""Walk-forward price-only baseline backtest; not validation of the five-layer model."""
import csv,gzip,json,math
from datetime import datetime,timezone
from pathlib import Path
from statistics import mean
ROOT=Path(__file__).resolve().parents[1]; DATA=ROOT/"data"/"historical"; OUT=DATA/"backtest"
COINS=("BTC","ETH","XRP","SOL"); HORIZONS={"1d":1,"3d":3,"7d":7}; MIN_TRAIN=120; SHRINK=20.0
OUT.mkdir(parents=True,exist_ok=True)

def load(coin):
 p=DATA/(coin.lower()+"_spot_1d.csv.gz")
 if not p.exists(): return []
 with gzip.open(p,"rt",newline="",encoding="utf-8") as f:
  d={}
  for r in csv.DictReader(f):
   try:
    t=int(r["open_time_ms"]); c=float(r["close"])
    if t>100_000_000_000_000:t//=1000
    if c>0 and math.isfinite(c): d[t]=c
   except (ValueError,KeyError,TypeError): pass
 return sorted(d.items())

def feat(rows,i):
 cs=[x[1] for x in rows]; c=cs[i]; m7=c/cs[i-7]-1 if i>=7 else 0.0
 m20=mean(cs[max(0,i-19):i+1]); m50=mean(cs[max(0,i-49):i+1]); g20=c/m20-1; g50=c/m50-1
 bucket=(-1 if m7<-.02 else 1 if m7>.02 else 0, int(g20>=0))
 regime="bull" if g50>.01 else "bear" if g50<-.01 else "sideways"
 return bucket,regime,m7,g20,g50

def metrics(ps):
 if not ps:return {"n":0,"directional_accuracy_pct":None,"brier_score":None,"baseline_always_up_accuracy_pct":None,"baseline_always_up_brier":None,"mean_predicted_up_pct":None,"actual_up_pct":None,"calibration_bins":[]}
 ys=[p["actual_up"] for p in ps]; probs=[p["p_up"] for p in ps]; base=sum(ys)/len(ys)
 bins=[]
 for lo,hi in [(0,.4),(.4,.5),(.5,.6),(.6,.7),(.7,1.000001)]:
  s=[p for p in ps if lo<=p["p_up"]<hi]
  if s: bins.append({"range":f"{lo:.1f}-{min(hi,1):.1f}","n":len(s),"mean_predicted_up_pct":round(100*mean([p["p_up"] for p in s]),2),"actual_up_pct":round(100*mean([p["actual_up"] for p in s]),2)})
 return {"n":len(ps),"directional_accuracy_pct":round(100*mean([int((p>=.5)==bool(y)) for p,y in zip(probs,ys)]),2),"brier_score":round(mean([(p-y)**2 for p,y in zip(probs,ys)]),5),"baseline_always_up_accuracy_pct":round(100*base,2),"baseline_always_up_brier":round(mean([(base-y)**2 for y in ys]),5),"mean_predicted_up_pct":round(100*mean(probs),2),"actual_up_pct":round(100*base,2),"calibration_bins":bins}

def run(coin,rows,label,h):
 if len(rows)<MIN_TRAIN+h+60:return {"status":"insufficient_data","rows":len(rows),"metrics":metrics([])},[]
 fs=[feat(rows,i) for i in range(len(rows))]; cs=[x[1] for x in rows]; ps=[]
 for i in range(60,len(rows)-h):
  train=[(fs[j][0],cs[j+h]/cs[j]>1) for j in range(60,i-h)]
  if len(train)<MIN_TRAIN:continue
  base=(sum(y for _,y in train)+1)/(len(train)+2); bucket,reg,m7,g20,g50=fs[i]
  same=[y for b,y in train if b==bucket]; local=(sum(same)+1)/(len(same)+2)
  prob=(len(same)*local+SHRINK*base)/(len(same)+SHRINK); ret=cs[i+h]/cs[i]-1
  ps.append({"coin":coin,"horizon":label,"date":datetime.fromtimestamp(rows[i][0]/1000,timezone.utc).date().isoformat(),"target_date":datetime.fromtimestamp(rows[i+h][0]/1000,timezone.utc).date().isoformat(),"p_up":round(prob,6),"actual_up":int(ret>0),"actual_return_pct":round(ret*100,5),"regime":reg,"train_n":len(train),"bucket_n":len(same),"momentum_7d_pct":round(m7*100,4),"ma20_gap_pct":round(g20*100,4),"ma50_gap_pct":round(g50*100,4)})
 return {"status":"available","rows":len(rows),"test_predictions":len(ps),"metrics":metrics(ps),"by_regime":{r:metrics([p for p in ps if p["regime"]==r]) for r in ("bull","bear","sideways")}},ps

def main():
 report={"generated_at":datetime.now(timezone.utc).isoformat(),"method":"Chronological walk-forward; bucketed historical up-frequency with Laplace smoothing and shrinkage toward the training base rate.","probability_note":"Price-only baseline estimates; not the production five-layer model, not financial advice.","leakage_controls":["Only training labels whose target horizon has completed before the forecast date are used.","Features use only data available at the forecast date.","No random split; forecasts are chronological."],"settings":{"minimum_training_observations":MIN_TRAIN,"shrinkage_strength":SHRINK,"horizons_days":HORIZONS},"results":{}}
 allp=[]
 for coin in COINS:
  rows=load(coin); report["results"][coin]={}
  if not rows:report["results"][coin]["status"]="missing_price_file";continue
  for label,h in HORIZONS.items():
   res,ps=run(coin,rows,label,h);report["results"][coin][label]=res;allp.extend(ps)
 fields=["coin","horizon","date","target_date","p_up","actual_up","actual_return_pct","regime","train_n","bucket_n","momentum_7d_pct","ma20_gap_pct","ma50_gap_pct"]
 with gzip.open(OUT/"walk_forward_predictions.csv.gz","wt",newline="",encoding="utf-8") as f:
  w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(allp)
 report["prediction_file"]="data/historical/backtest/walk_forward_predictions.csv.gz";report["total_predictions"]=len(allp)
 (OUT/"walk_forward_report.json").write_text(json.dumps(report,indent=2,allow_nan=False),encoding="utf-8")
 print(json.dumps({"report":"data/historical/backtest/walk_forward_report.json","total_predictions":len(allp),"metrics":{c:{h:report["results"].get(c,{}).get(h,{}).get("metrics",{}).get("n",0) for h in HORIZONS} for c in COINS}},indent=2))
if __name__=="__main__":main()
