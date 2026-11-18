#!/usr/bin/env python3
"""Forward outcome review for archived pre-move LONG/SHORT signals."""
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
DATA=ROOT/"docs"/"data"

def dt(value):
    try:
        x=datetime.fromisoformat(str(value).replace("Z","+00:00"))
        return x if x.tzinfo else x.replace(tzinfo=timezone.utc)
    except Exception:
        return None

def evaluate(history, horizons=(4,24,72), max_lag_hours=2):
    parsed=[]
    for item in history or []:
        stamp=dt(item.get("updated_at"))
        if stamp: parsed.append((stamp,item))
    parsed.sort(key=lambda x:x[0])
    result={}
    for hours in horizons:
        rows=[]
        for i,(stamp,item) in enumerate(parsed):
            coins=item.get("coins",{}) or {}
            for coin,payload in coins.items():
                sig=payload.get("pre_move") or {}
                side=sig.get("action")
                stage=sig.get("stage")
                if side not in ("LONG","SHORT") or stage not in ("EARLY_WATCH","CONFIRMED_TRIGGER"):
                    continue
                entry=payload.get("price")
                try: entry=float(entry)
                except (TypeError,ValueError): continue
                if entry<=0: continue
                target=stamp+timedelta(hours=hours)
                future=next(((t,p) for t,p in parsed[i+1:] if t>=target),None)
                if not future: continue
                ft,fi=future
                if (ft-target).total_seconds()>max_lag_hours*3600: continue
                fp=(fi.get("coins",{}) or {}).get(coin,{})
                try: exit_price=float(fp.get("price"))
                except (TypeError,ValueError): continue
                if exit_price<=0: continue
                ret=(exit_price/entry-1)*100
                directional=ret if side=="LONG" else -ret
                rows.append({"coin":coin,"side":side,"stage":stage,"signal_time":stamp.isoformat(),"observed_time":ft.isoformat(),"return_pct":round(ret,4),"directional_return_pct":round(directional,4),"correct":directional>0})
        def stats(items):
            n=len(items)
            return {"n":n,"hit_rate_pct":round(sum(1 for x in items if x["correct"])/n*100,1) if n else None,"mean_directional_return_pct":round(sum(x["directional_return_pct"] for x in items)/n,4) if n else None,"mean_underlying_return_pct":round(sum(x["return_pct"] for x in items)/n,4) if n else None}
        result[str(hours)+"h"]={"all":stats(rows),"early_watch":stats([x for x in rows if x["stage"]=="EARLY_WATCH"]),"confirmed_trigger":stats([x for x in rows if x["stage"]=="CONFIRMED_TRIGGER"]),"samples":rows[-100:]}
    return {"updated_at":datetime.now(timezone.utc).isoformat(),"method":"Strict forward-only evaluation: each archived LONG/SHORT signal is compared with the first later snapshot at or after 4h/24h/72h (maximum 2h observation lag). Directional hit means return was positive for LONG or negative for SHORT. No signal is evaluated until its future window matures. Small samples are descriptive, not predictive accuracy.","horizons":result}

def main():
    path=DATA/"history.json"
    try: history=json.loads(path.read_text(encoding="utf-8"))
    except Exception: history=[]
    out=evaluate(history)
    (DATA/"pre_move_review.json").write_text(json.dumps(out,indent=2,allow_nan=False),encoding="utf-8")
    print("Pre-move forward review: "+", ".join(f"{h} n={v['all']['n']} hit={v['all']['hit_rate_pct']}" for h,v in out["horizons"].items()))

if __name__=="__main__":
    main()
