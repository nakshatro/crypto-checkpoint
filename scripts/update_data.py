import json, math, os, statistics, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urlencode

ROOT=Path(__file__).resolve().parents[1]
DATA=ROOT/'data'
DATA.mkdir(exist_ok=True)
BYBIT='https://api.bybit.com'
CMC='https://pro-api.coinmarketcap.com/public-api'
NOW=datetime.now(timezone.utc).isoformat()

def get_json(url, timeout=20):
    req=Request(url,headers={'User-Agent':'CryptoCheckpoint/2.0','Accept':'application/json'})
    with urlopen(req,timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))

def bybit(path, params):
    return get_json(BYBIT+path+'?'+urlencode(params))

def ema(vals,p):
    if len(vals)<p:return None
    k=2/(p+1); e=sum(vals[:p])/p
    for v in vals[p:]:e=v*k+e*(1-k)
    return e

def sma(vals,p):return sum(vals[-p:])/p if len(vals)>=p else None

def rsi(vals,p=14):
    if len(vals)<p+1:return None
    g=l=0
    for i in range(len(vals)-p,len(vals)):
        d=vals[i]-vals[i-1]
        if d>=0:g+=d
        else:l-=d
    if l==0:return 100
    return 100-100/(1+g/l)

def atr(rows,p=14):
    if len(rows)<p+1:return None
    tr=[]
    for i in range(1,len(rows)):
        tr.append(max(rows[i][2]-rows[i][3],abs(rows[i][2]-rows[i-1][4]),abs(rows[i][3]-rows[i-1][4])))
    return sma(tr,p)

def kline(symbol, interval='240', limit=220):
    j=bybit('/v5/market/kline',{'category':'linear','symbol':symbol,'interval':interval,'limit':limit})
    arr=j.get('result',{}).get('list',[])
    arr=list(reversed(arr))
    return [[int(x[0]),float(x[1]),float(x[2]),float(x[3]),float(x[4]),float(x[5])] for x in arr]

def structure(rows, look=30):
    r=rows[-look:]
    closes_all=[x[4] for x in rows]
    e20,e55=ema(closes_all,20),ema(closes_all,55)
    return {'high':max(x[2] for x in r),'low':min(x[3] for x in r),'trend':'Rising' if e20 and e55 and e20>e55 else 'Falling' if e20 and e55 and e20<e55 else 'Range'}

def calc_analysis(symbol, m):
    try:
        r4=kline(symbol,'240',220); r1=kline(symbol,'D',120)
        c=[x[4] for x in r4]
        e10,e20,e55=ema(c,10),ema(c,20),ema(c,55)
        ma50,ma100=sma(c,50),sma(c,100); rr=rsi(c); aa=atr(r4); s4=structure(r4); s1=structure(r1)
        score=5; reasons=[]
        if e10 and e20 and e55:
            if e10>e20>e55:score+=1.2;reasons.append('EMA bullish')
            elif e10<e20<e55:score-=1.2;reasons.append('EMA bearish')
        if rr is not None:
            if 55<=rr<=70:score+=.7;reasons.append('RSI bullish zone')
            elif rr<=45:score-=.7;reasons.append('RSI weak')
        ch=m.get('change',0)
        if ch>3:score+=.4;reasons.append('momentum')
        elif ch<-3:score-=.4;reasons.append('negative momentum')
        oi=m.get('oiDelta')
        if oi is not None:
            if oi>5:score+=.5;reasons.append('OI expansion')
            elif oi<-5:score-=.2;reasons.append('OI contraction')
        vr=m.get('volRatio') or 0
        if vr>=1.75:score+=.8;reasons.append(f'{vr:.1f}x volume spike')
        elif vr>=1.25:score+=.3;reasons.append('elevated volume')
        score=max(0,min(10,score)); signal='LONG' if score>=7 else 'SHORT' if score<=4 else 'NEUTRAL'
        entryLo=entryHi=sl=tp1=tp2=m['price']
        if aa and m['price']:
            if signal=='LONG':entryLo=max(s4['low'],m['price']-aa*.5);entryHi=m['price'];sl=min(s4['low'],m['price']-aa*1.25);tp1=max(s4['high'],m['price']+aa);tp2=m['price']+aa*2
            elif signal=='SHORT':entryLo=m['price'];entryHi=min(s4['high'],m['price']+aa*.5);sl=max(s4['high'],m['price']+aa*1.25);tp1=min(s4['low'],m['price']-aa);tp2=m['price']-aa*2
        return {'symbol':symbol,'score':round(score,2),'signal':signal,'rsi':rr,'ema10':e10,'ema20':e20,'ema55':e55,'ma50':ma50,'ma100':ma100,'atr':aa,'structure4h':s4,'structure1d':s1,'trend':s4['trend'],'entryLo':entryLo,'entryHi':entryHi,'sl':sl,'tp1':tp1,'tp2':tp2,'reasons':reasons}
    except Exception as e:
        return {'symbol':symbol,'error':str(e)}

def median(a):return statistics.median(a) if a else None

def spike_for(symbol):
    try:
        rows=kline(symbol,'15',25)
        if len(rows)<22:return None
        # Last candle may still be open. Use the latest completed 15m candle.
        now_ms=int(time.time()*1000)
        completed=rows[:-1] if rows[-1][0]+15*60*1000>now_ms else rows
        if len(completed)<21:return None
        cur=completed[-1][5]; base=median([x[5] for x in completed[-21:-1]])
        ratio=cur/base if base else None
        if ratio is None:return None
        label='EXTREME' if ratio>=4 else 'MAJOR' if ratio>=2.5 else 'SPIKE' if ratio>=1.75 else 'ELEVATED' if ratio>=1.25 else 'NORMAL'
        return {'vol15m':cur,'volBaseline':base,'volRatio':ratio,'spike':label,'spikeSource':'GitHub snapshot'}
    except Exception:
        return None

# Existing snapshots let us calculate OI change between 15-minute runs.
prev={}
try:
    old=json.loads((DATA/'market.json').read_text())
    prev={x['symbol']:x for x in old.get('symbols',[])}
except Exception:pass

# Market-wide Stage A data: all eligible Bybit linear USDT contracts.
tickers=bybit('/v5/market/tickers',{'category':'linear'}).get('result',{}).get('list',[])
rows=[]
for x in tickers:
    s=x.get('symbol','')
    if not s.endswith('USDT'):continue
    price=float(x.get('lastPrice') or 0)
    if price<=0:continue
    oi=float(x.get('openInterestValue') or 0)
    old=prev.get(s,{})
    oldoi=float(old.get('oi') or 0)
    item={'symbol':s,'price':price,'change':float(x.get('price24hPcnt') or 0)*100,'high':float(x.get('highPrice24h') or 0),'low':float(x.get('lowPrice24h') or 0),'volume':float(x.get('turnover24h') or 0),'oi':oi,'funding':float(x.get('fundingRate') or 0)*100,'nextFunding':int(x.get('nextFundingTime') or 0),'oiDelta':((oi-oldoi)/oldoi*100 if oldoi else None),'ts':int(time.time()*1000),'source':'Bybit Linear'}
    rows.append(item)
rows.sort(key=lambda x:x['volume'],reverse=True)

# True volume spike calculation for the active market set. 150 is enough to catch unusual activity without hammering the API.
active=rows[:150]
with ThreadPoolExecutor(max_workers=20) as ex:
    fut={ex.submit(spike_for,x['symbol']):x for x in active}
    for f in as_completed(fut):
        x=fut[f]
        try:
            sp=f.result()
            if sp:x.update(sp)
        except Exception:pass

# Stage A rank after volume spike enrichment.
for x in rows:
    spike=x.get('volRatio') or 0
    x['stageAScore']=round(min(3,abs(x['change'])/2)+min(2,abs(x['oiDelta'] or 0)/5)+(3 if spike>=1.75 else 1.5 if spike>=1.25 else 0),3)
ranked=sorted(rows,key=lambda x:x['stageAScore'],reverse=True)

# Stage B technical analysis on top 40 plus core instruments.
by_symbol={x['symbol']:x for x in rows}
selected=[]
for x in ranked[:40]:selected.append(x['symbol'])
for s in ['BTCUSDT','ETHUSDT','XRPUSDT','SOLUSDT']:
    if s in by_symbol and s not in selected:selected.append(s)
analysis=[]
with ThreadPoolExecutor(max_workers=8) as ex:
    fut={ex.submit(calc_analysis,s,by_symbol[s]):s for s in selected}
    for f in as_completed(fut):
        a=f.result()
        if 'error' not in a:analysis.append(a)
analysis.sort(key=lambda x:x.get('score',0),reverse=True)

# CMC context: keyless public endpoints, fetched server-side by GitHub Actions to avoid browser CORS.
ctx={'source':'CoinMarketCap keyless snapshot','generated_at':NOW}
try:
    fg= get_json(CMC+'/v3/fear-and-greed/latest').get('data',{})
    ctx.update(fng=int(fg.get('value')),fngLabel=fg.get('value_classification'),fngUpdated=fg.get('update_time'))
except Exception as e:ctx['fngError']=str(e)
try:
    a=get_json(CMC+'/v1/altcoin-season-index/latest').get('data',{})
    ctx.update(altseason=int(a.get('altcoin_index')),altseasonUpdated=a.get('snapshot_time') or a.get('update_time'))
except Exception as e:ctx['altseasonError']=str(e)
try:
    g=get_json(CMC+'/v1/global-metrics/quotes/latest?convert=USD').get('data',{});q=g.get('quote',{}).get('USD',{})
    ctx.update(btcDom=float(g.get('btc_dominance')),totalMarketCap=float(q.get('total_market_cap')),totalMarketCapChange=float(q.get('total_market_cap_yesterday_percentage_change')))
except Exception as e:ctx['globalError']=str(e)
try:
    q=get_json(CMC+'/v3/cryptocurrency/quotes/latest?id=1,1027&convert=USD').get('data',{})
    ctx.update(btcCap=float(q['1']['quote']['USD']['market_cap']),ethCap=float(q['1027']['quote']['USD']['market_cap']))
    ctx['total3']=ctx['totalMarketCap']-ctx['btcCap']-ctx['ethCap']
    ctx['total3Btc']=ctx['total3']/ctx['btcCap'] if ctx['btcCap'] else None
except Exception as e:ctx['assetError']=str(e)

(DATA/'market.json').write_text(json.dumps({'generated_at':NOW,'source':'Bybit Linear','count':len(rows),'symbols':rows},separators=(',',':')))
(DATA/'analysis.json').write_text(json.dumps({'generated_at':NOW,'source':'Bybit 4H/1D','analysis':analysis},separators=(',',':')))
(DATA/'context.json').write_text(json.dumps(ctx,separators=(',',':')))
print(f'Generated {len(rows)} market rows, {len(analysis)} analysis rows')
print('Context:',{k:ctx.get(k) for k in ('fng','fngLabel','altseason','btcDom','totalMarketCap','total3','total3Btc')})
