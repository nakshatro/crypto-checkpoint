#!/usr/bin/env python3
"""Walk-forward benchmarks, calibration, selective trading, and funding ablation.
All forecasts are historical experiments, not the production five-layer model.
"""
import csv,gzip,json,math,random,bisect
from datetime import datetime,timezone
from pathlib import Path
from statistics import mean
ROOT=Path(__file__).resolve().parents[1]; DATA=ROOT/"data"/"historical"; OUT=DATA/"backtest"
COINS=("BTC","ETH","XRP","SOL"); HORIZONS={"1d":1,"3d":3,"7d":7}; MIN_TRAIN=120; SHRINK=20.0
TRADE_THRESHOLDS=(0.52,0.55,0.60); COSTS_BPS=(5,10,20); BOOTSTRAP_REPS=300
OUT.mkdir(parents=True,exist_ok=True)

def load_prices(coin):
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

def load_funding(coin):
 p=DATA/(coin.lower()+"_funding.csv.gz")
 if not p.exists(): return []
 out=[]
 try:
  with gzip.open(p,"rt",newline="",encoding="utf-8") as f:
   for r in csv.DictReader(f):
    try:
     t=int(r["funding_time_ms"]); rate=float(r["funding_rate"])
     if t>100_000_000_000_000:t//=1000
     if math.isfinite(rate):out.append((t,rate))
    except (ValueError,KeyError,TypeError):pass
 except OSError: return []
 return sorted(out)

def features(rows,funding):
 cs=[x[1] for x in rows]; times=[x[0] for x in rows]; fts=[x[0] for x in funding]
 fs=[]
 for i,(ts,c) in enumerate(rows):
  mom7=c/cs[i-7]-1 if i>=7 else 0.0
  ma20=mean(cs[max(0,i-19):i+1]); ma50=mean(cs[max(0,i-49):i+1])
  g20=c/ma20-1; g50=c/ma50-1
  mb=-1 if mom7<-.02 else 1 if mom7>.02 else 0
  t20=int(g20>=0); t50=int(g50>=0)
  regime="bull" if g50>.01 else "bear" if g50<-.01 else "sideways"
  # Use only the latest funding rate published at or before the candle timestamp.
  k=bisect.bisect_right(fts,ts)-1
  fr=funding[k][1] if k>=0 else None
  fb=None if fr is None else (-1 if fr < -0.0001 else 1 if fr > 0.0001 else 0)
  fs.append({"momentum":(mb,),"ma20":(t20,),"ma50":(t50,),"combined":(mb,t20),"funding":(mb,t20,fb) if fb is not None else None,
             "regime":regime,"mom7":mom7,"g20":g20,"g50":g50,"funding_rate":fr})
 return fs

def brier(ps): return mean([(p-y)**2 for p,y in ps]) if ps else None
def metric_pack(ps):
 if not ps:return {"n":0,"accuracy_pct":None,"brier_score":None,"constant_train_rate_brier":None,"mean_predicted_up_pct":None,"actual_up_pct":None,"accuracy_ci95_pct":None,"calibration_bins":[]}
 ys=[int(p["actual_up"]) for p in ps]; probs=[float(p["p_up"]) for p in ps]
 acc=mean([int((p>=.5)==bool(y)) for p,y in zip(probs,ys)]); rate=mean(ys)
 # Block bootstrap by horizon-sized contiguous blocks; use horizon_days attached to records.
 n=len(ps); block=max(1,int(ps[0].get("horizon_days",1))); rng=random.Random(20261022); boots=[]
 starts=list(range(0,n,block))
 for _ in range(BOOTSTRAP_REPS):
  ix=[]
  while len(ix)<n:
   st=rng.choice(starts); ix.extend(range(st,min(st+block,n)))
  ix=ix[:n]; boots.append(mean([int((probs[j]>=.5)==bool(ys[j])) for j in ix]))
 boots.sort(); lo=boots[int(.025*(len(boots)-1))]; hi=boots[int(.975*(len(boots)-1))]
 bins=[]
 for a,z in [(0,.4),(.4,.45),(.45,.5),(.5,.55),(.55,.6),(.6,.7),(.7,1.000001)]:
  subset=[p for p in ps if a<=p["p_up"]<z]
  if subset:bins.append({"range":f"{a:.2f}-{min(z,1):.2f}","n":len(subset),"mean_predicted_up_pct":round(100*mean([p["p_up"] for p in subset]),2),"actual_up_pct":round(100*mean([p["actual_up"] for p in subset]),2)})
 return {"n":n,"accuracy_pct":round(100*acc,2),"brier_score":round(brier(list(zip(probs,ys))),5),"constant_train_rate_brier":round(mean([(rate-y)**2 for y in ys]),5),"mean_predicted_up_pct":round(100*mean(probs),2),"actual_up_pct":round(100*rate,2),"accuracy_ci95_pct":[round(100*lo,2),round(100*hi,2)],"calibration_bins":bins}

def probs_from(train, key, bucket):
 vals=[y for x,y in train if x.get(key) is not None]
 if not vals:return .5,0
 base=(sum(y for _,y in vals)+1)/(len(vals)+2)
 same=[y for x,y in train if x.get(key)==bucket]
 local=(sum(same)+1)/(len(same)+2)
 return (len(same)*local+SHRINK*base)/(len(same)+SHRINK),len(same)

def strategy_metrics(ps,threshold,cost_bps):
 trades=[]
 for p in ps:
  prob=p["p_up"]
  side=1 if prob>=threshold else (-1 if prob<=1-threshold else 0)
  if side:
   gross=side*p["actual_return_pct"]
   net=gross-cost_bps/100
   trades.append({"side":side,"gross_pct":gross,"net_pct":net,"date":p["date"],"regime":p["regime"]})
 if not trades:return {"trades":0,"coverage_pct":0,"win_rate_pct":None,"gross_return_sum_pct":0,"net_return_sum_pct":0,"mean_net_per_trade_pct":None,"profit_factor":None}
 wins=[x["net_pct"] for x in trades if x["net_pct"]>0]; losses=[x["net_pct"] for x in trades if x["net_pct"]<0]
 return {"trades":len(trades),"coverage_pct":round(100*len(trades)/len(ps),2),"win_rate_pct":round(100*mean([int(x["net_pct"]>0) for x in trades]),2),"gross_return_sum_pct":round(sum(x["gross_pct"] for x in trades),3),"net_return_sum_pct":round(sum(x["net_pct"] for x in trades),3),"mean_net_per_trade_pct":round(mean([x["net_pct"] for x in trades]),4),"profit_factor":round(sum(wins)/abs(sum(losses)),3) if losses else (None if not wins else "infinite")}

def run_one(coin,rows,fs,label,h):
 if len(rows)<MIN_TRAIN+h+60:return {"status":"insufficient_data","metrics":metric_pack([])},[]
 cs=[x[1] for x in rows]; ps=[]
 for i in range(60,len(rows)-h):
  # Training labels must have completed before forecast date i.
  train=[]
  for j in range(60,i-h):
   y=int(cs[j+h]>cs[j]); train.append((fs[j],y))
  if len(train)<MIN_TRAIN:continue
  actual=int(cs[i+h]>cs[i]); ret=(cs[i+h]/cs[i]-1)*100
  pbase=(sum(y for _,y in train)+1)/(len(train)+2)
  values={}
  for key in ("momentum","ma20","ma50","combined"):
   values[key],_=probs_from(train,key,fs[i][key])
  if fs[i]["funding"] is not None:
   values["funding_augmented"],_=probs_from(train,"funding",fs[i]["funding"])
  else: values["funding_augmented"]=None
  date=datetime.fromtimestamp(rows[i][0]/1000,timezone.utc).date().isoformat()
  target=datetime.fromtimestamp(rows[i+h][0]/1000,timezone.utc).date().isoformat()
  p={"coin":coin,"horizon":label,"horizon_days":h,"date":date,"target_date":target,"actual_up":actual,"actual_return_pct":round(ret,5),"regime":fs[i]["regime"],"train_n":len(train),"p_base":pbase,"p_momentum":values["momentum"],"p_ma20":values["ma20"],"p_ma50":values["ma50"],"p_model":values["combined"],"p_funding":values["funding_augmented"],"momentum_7d_pct":round(fs[i]["mom7"]*100,4),"ma20_gap_pct":round(fs[i]["g20"]*100,4),"ma50_gap_pct":round(fs[i]["g50"]*100,4),"funding_rate":fs[i]["funding_rate"]}
  ps.append(p)
 names={"base":"p_base","momentum":"p_momentum","ma20":"p_ma20","ma50":"p_ma50","combined":"p_model","funding_augmented":"p_funding"}
 metrics={}
 for name,field in names.items():
  sub=[dict(x,p_up=x[field]) for x in ps if x[field] is not None]
  metrics[name]=metric_pack(sub)
 regimes={r:{name:metric_pack([dict(x,p_up=x[field]) for x in ps if x["regime"]==r and x[field] is not None]) for name,field in names.items()} for r in ("bull","bear","sideways")}
 strategies={}
 for name,field in (("combined","p_model"),("funding_augmented","p_funding")):
  strategies[name]={str(th):{str(cost):strategy_metrics([x for x in ps if x[field] is not None],th,cost) for cost in COSTS_BPS} for th in TRADE_THRESHOLDS}
 return {"status":"available","rows":len(rows),"metrics_by_model":metrics,"by_regime":regimes,"selective_trade_simulation":strategies},ps

def main():
 report={"generated_at":datetime.now(timezone.utc).isoformat(),"method":"Chronological walk-forward benchmark comparison. Historical bucket frequencies are Laplace-smoothed and shrunk toward training base rate.","scope":"Daily spot price direction baseline plus funding-rate feature ablation. Not the production five-layer model.","leakage_controls":["Each training label is used only after its target horizon has completed.","Features only use candle/funding observations timestamped no later than forecast time.","No random split; chronological walk-forward evaluation."],"settings":{"minimum_training_observations":MIN_TRAIN,"shrinkage_strength":SHRINK,"horizons_days":HORIZONS,"trade_thresholds":TRADE_THRESHOLDS,"round_trip_cost_scenarios_bps":COSTS_BPS,"bootstrap_replicates":BOOTSTRAP_REPS},"benchmark_definitions":{"base":"Training-period up-frequency, smoothed; Brier benchmark correctly labelled.","momentum":"Historical up-frequency conditional on 7-day momentum bucket.","ma20":"Historical up-frequency conditional on price relative to 20-day moving average.","ma50":"Historical up-frequency conditional on price relative to 50-day moving average.","combined":"Historical up-frequency conditional on momentum bucket and 20-day moving-average side.","funding_augmented":"Combined features plus last funding rate known by forecast time; only scored when funding feature exists."},"limitations":["Confidence intervals use horizon-length block resampling but remain approximate; overlapping targets can still make inference optimistic.","Trading simulation assumes a single entry at forecast close and exit at horizon close, charges stated round-trip cost, and allows overlapping trades; summed returns are not a portfolio equity curve.","Funding feature is a simple historical ablation, not a full perpetual-futures PnL model; funding cash flows, leverage, liquidation, and margin are not simulated.","No strategy should be considered production-ready without further validation and realistic execution assumptions."],"results":{}}
 allp=[]
 for coin in COINS:
  rows=load_prices(coin);funding=load_funding(coin);fs=features(rows,funding);report["results"][coin]={"price_rows":len(rows),"funding_rows":len(funding),"horizons":{}}
  if not rows:report["results"][coin]["status"]="missing_price_file";continue
  for label,h in HORIZONS.items():
   res,ps=run_one(coin,rows,fs,label,h);report["results"][coin]["horizons"][label]=res;allp.extend(ps)
 fields=["coin","horizon","horizon_days","date","target_date","actual_up","actual_return_pct","regime","train_n","p_base","p_momentum","p_ma20","p_ma50","p_model","p_funding","momentum_7d_pct","ma20_gap_pct","ma50_gap_pct","funding_rate"]
 with gzip.open(OUT/"walk_forward_predictions.csv.gz","wt",newline="",encoding="utf-8") as f:
  w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(allp)
 report["prediction_file"]="data/historical/backtest/walk_forward_predictions.csv.gz";report["total_predictions"]=len(allp)
 (OUT/"walk_forward_report.json").write_text(json.dumps(report,indent=2,allow_nan=False),encoding="utf-8")
 print(json.dumps({"report":"data/historical/backtest/walk_forward_report.json","total_predictions":len(allp),"metrics":{c:{h:{m:v.get("metrics_by_model",{}).get(m,{}).get("n",0) for m in ("base","momentum","ma20","ma50","combined","funding_augmented")} for h,v in report["results"].get(c,{}).get("horizons",{}).items()} for c in COINS}},indent=2))
if __name__=="__main__":main()
