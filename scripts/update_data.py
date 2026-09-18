import json, math, statistics, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError

ROOT=Path(__file__).resolve().parents[1]
DATA=ROOT/'data'; DATA.mkdir(exist_ok=True)
BINANCE='https://fapi.binance.com'
BYBIT='https://api.bybit.com'
CMC='https://pro-api.coinmarketcap.com/public-api'
NOW=datetime.now(timezone.utc).isoformat()

HEADERS={'User-Agent':'Mozilla/5.0 CryptoCheckpoint/2.1','Accept':'application/json','Accept-Language':'en-US,en;q=0.8'}

def get_json(url, timeout=15, retries=2):
    last=None
    for i in range(retries+1):
        try:
            req=Request(url, headers=HEADERS)
            with urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode('utf-8'))
        except Exception as e:
            last=e
            if i<retries: time.sleep(1.2*(i+1))
    raise last

def api(base,path,params=None,timeout=15):
    q=('?'+urlencode(params)) if params else ''
    return get_json(base+path+q,timeout=timeout)

def binance(path,params=None): return api(BINANCE,path,params)
def bybit(path,params=None): return api(BYBIT,path,params)

def ema(vals,p):
    if len(vals)<p:return None
    k=2/(p+1); e=sum(vals[:p])/p
    for v in vals[p:]: e=v*k+e*(1-k)
    return e

def sma(vals,p): return sum(vals[-p:])/p if len(vals)>=p else None

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

def kline_binance(symbol,interval='4h',limit=220):
    arr=binance('/fapi/v1/klines',{'symbol':symbol,'interval':interval,'limit':limit})
    return [[int(x[0]),float(x[1]),float(x[2]),float(x[3]),float(x[4]),float(x[5])] for x in arr]

def kline_bybit(symbol,interval='240',limit=220):
    arr=bybit('/v5/market/kline',{'category':'linear','symbol':symbol,'interval':interval,'limit':limit}).get('result',{}).get('list',[])
    arr=list(reversed(arr))
    return [[int(x[0]),float(x[1]),float(x[2]),float(x[3]),float(x[4]),float(x[5])] for x in arr]

def kline(symbol,interval='4h',limit=220):
    try:return kline_binance(symbol,interval,limit)
    except Exception:
        bi={'4h':'240','1d':'D','15m':'15'}[interval]
        return kline_bybit(symbol,bi,limit)

def structure(rows,look=30):
    r=rows[-look:]; c=[x[4] for x in rows]; e20,e55=ema(c,20),ema(c,55)
    return {'high':max(x[2] for x in r),'low':min(x[3] for x in r),'trend':'Rising' if e20 and e55 and e20>e55 else 'Falling' if e20 and e55 and e20<e55 else 'Range'}

def oi_current(symbol):
    try:
        j=binance('/fapi/v1/openInterest',{'symbol':symbol})
        return float(j.get('openInterest') or 0)
    except Exception:
        try:
            j=bybit('/v5/market/tickers',{'category':'linear','symbol':symbol})
            x=(j.get('result',{}).get('list') or [{}])[0]
            return float(x.get('openInterest') or 0)
        except Exception:return None

def calc_analysis(symbol,m):
    try:
        r4=kline(symbol,'4h',220); r1=kline(symbol,'1d',120); c=[x[4] for x in r4]
        e10,e20,e55=ema(c,10),ema(c,20),ema(c,55); ma50,ma100=sma(c,50),sma(c,100); rr=rsi(c); aa=atr(r4); s4=structure(r4); s1=structure(r1)
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
        return {'symbol':symbol,'score':round(score,2),'signal':signal,'rsi':rr,'ema10':e10,'ema20':e20,'ema55':e55,'ma50':ma50,'ma100':ma100,'atr':aa,'structure4h':s4,'structure1d':s1,'trend':s4['trend'],'reasons':reasons}
    except Exception as e:return {'symbol':symbol,'error':str(e)}

def spike_for(symbol):
    try:
        rows=kline(symbol,'15m',25)
        if len(rows)<22:return None
        now=int(time.time()*1000); completed=rows[:-1] if rows[-1][0]+900000>now else rows
        if len(completed)<21:return None
        cur=completed[-1][5]; base=statistics.median([x[5] for x in completed[-21:-1]])
        ratio=cur/base if base else None
        if ratio is None:return None
        label='EXTREME' if ratio>=4 else 'MAJOR' if ratio>=2.5 else 'SPIKE' if ratio>=1.75 else 'ELEVATED' if ratio>=1.25 else 'NORMAL'
        return {'vol15m':cur,'volBaseline':base,'volRatio':ratio,'spike':label,'spikeSource':'Binance Futures 15m'}
    except Exception:return None

# Previous snapshot for 15m OI change.
prev={}
try:
    old=json.loads((DATA/'market.json').read_text()); prev={x['symbol']:x for x in old.get('symbols',[])}
except Exception:pass

# Binance Futures is primary because the GitHub Actions runner receives HTTP 403 from Bybit.
try:
    info=binance('/fapi/v1/exchangeInfo')
    eligible={x['symbol'] for x in info.get('symbols',[]) if x.get('quoteAsset')=='USDT' and x.get('contractType')=='PERPETUAL' and x.get('status')=='TRADING'}
    tickers=binance('/fapi/v1/ticker/24hr')
    premium=binance('/fapi/v1/premiumIndex')
    prem={x.get('symbol'):x for x in premium}
    rows=[]
    for x in tickers:
        s=x.get('symbol','')
        if s not in eligible:continue
        price=float(x.get('lastPrice') or 0)
        if price<=0:continue
        p=prem.get(s,{})
        old=prev.get(s,{})
        rows.append({'symbol':s,'price':price,'change':float(x.get('priceChangePercent') or 0),'high':float(x.get('highPrice') or 0),'low':float(x.get('lowPrice') or 0),'volume':float(x.get('quoteVolume') or 0),'oi':old.get('oi'),'funding':float(p.get('lastFundingRate') or 0)*100,'nextFunding':int(p.get('nextFundingTime') or 0),'oiDelta':None,'ts':int(time.time()*1000),'source':'Binance USD-M Futures'})
except Exception as e:
    # Last-resort Bybit if Binance is unavailable.
    tickers=bybit('/v5/market/tickers',{'category':'linear'}).get('result',{}).get('list',[])
    rows=[]
    for x in tickers:
        s=x.get('symbol','')
        if not s.endswith('USDT'):continue
        price=float(x.get('lastPrice') or 0)
        if price<=0:continue
        old=prev.get(s,{})
        oi=float(x.get('openInterestValue') or 0); oldoi=float(old.get('oi') or 0)
        rows.append({'symbol':s,'price':price,'change':float(x.get('price24hPcnt') or 0)*100,'high':float(x.get('highPrice24h') or 0),'low':float(x.get('lowPrice24h') or 0),'volume':float(x.get('turnover24h') or 0),'oi':oi,'funding':float(x.get('fundingRate') or 0)*100,'nextFunding':int(x.get('nextFundingTime') or 0),'oiDelta':((oi-oldoi)/oldoi*100 if oldoi else None),'ts':int(time.time()*1000),'source':'Bybit Linear'})

rows.sort(key=lambda x:x['volume'],reverse=True)

# Current OI for top 100 liquid contracts. This keeps the scanner broad while controlling API weight.
with ThreadPoolExecutor(max_workers=20) as ex:
    fut={ex.submit(oi_current,x['symbol']):x for x in rows[:100]}
    for f in as_completed(fut):
        x=fut[f]
        oi=f.result()
        if oi is not None:
            oldoi=float(prev.get(x['symbol'],{}).get('oi') or 0)
            x['oi']=oi*x['price']
            x['oiDelta']=((x['oi']-oldoi)/oldoi*100 if oldoi else None)

# True 15m volume anomaly on top 120 liquid contracts.
with ThreadPoolExecutor(max_workers=20) as ex:
    fut={ex.submit(spike_for,x['symbol']):x for x in rows[:120]}
    for f in as_completed(fut):
        sp=f.result()
        if sp:fut[f].update(sp)

for x in rows:
    spike=x.get('volRatio') or 0
    x['stageAScore']=round(min(3,abs(x['change'])/2)+min(2,abs(x.get('oiDelta') or 0)/5)+(3 if spike>=1.75 else 1.5 if spike>=1.25 else 0),3)
ranked=sorted(rows,key=lambda x:x['stageAScore'],reverse=True)
by_symbol={x['symbol']:x for x in rows}
selected=[x['symbol'] for x in ranked[:40]]
for s in ['BTCUSDT','ETHUSDT','XRPUSDT','SOLUSDT']:
    if s in by_symbol and s not in selected:selected.append(s)
analysis=[]
with ThreadPoolExecutor(max_workers=8) as ex:
    fut={ex.submit(calc_analysis,s,by_symbol[s]):s for s in selected}
    for f in as_completed(fut):
        a=f.result()
        if 'error' not in a:analysis.append(a)
analysis.sort(key=lambda x:x.get('score',0),reverse=True)

ctx={'source':'CoinMarketCap keyless snapshot','generated_at':NOW}
for name,path in [('fng','/v3/fear-and-greed/latest'),('altseason','/v1/altcoin-season-index/latest')]:
    try:
        d=get_json(CMC+path).get('data',{})
        if name=='fng':ctx.update(fng=int(d.get('value')),fngLabel=d.get('value_classification'),fngUpdated=d.get('update_time'))
        else:ctx.update(altseason=int(d.get('altcoin_index')),altseasonUpdated=d.get('snapshot_time') or d.get('update_time'))
    except Exception as e:ctx[name+'Error']=str(e)
try:
    g=get_json(CMC+'/v1/global-metrics/quotes/latest?convert=USD').get('data',{});q=g.get('quote',{}).get('USD',{})
    ctx.update(btcDom=float(g.get('btc_dominance')),totalMarketCap=float(q.get('total_market_cap')),totalMarketCapChange=float(q.get('total_market_cap_yesterday_percentage_change')))
except Exception as e:ctx['globalError']=str(e)
try:
    q=get_json(CMC+'/v3/cryptocurrency/quotes/latest?id=1,1027&convert=USD').get('data',{})
    ctx.update(btcCap=float(q['1']['quote']['USD']['market_cap']),ethCap=float(q['1027']['quote']['USD']['market_cap']))
    ctx['total3']=ctx.get('totalMarketCap',0)-ctx['btcCap']-ctx['ethCap'];ctx['total3Btc']=ctx['total3']/ctx['btcCap'] if ctx['btcCap'] else None
except Exception as e:ctx['assetError']=str(e)

(DATA/'market.json').write_text(json.dumps({'generated_at':NOW,'source':'Binance USD-M Futures','count':len(rows),'symbols':rows},separators=(',',':')))
(DATA/'analysis.json').write_text(json.dumps({'generated_at':NOW,'source':'Binance 4H/1D','analysis':analysis},separators=(',',':')))
(DATA/'context.json').write_text(json.dumps(ctx,separators=(',',':')))
print(f'Generated {len(rows)} market rows, {len(analysis)} analysis rows')
print('Context:',{k:ctx.get(k) for k in ('fng','fngLabel','altseason','btcDom','totalMarketCap','total3','total3Btc')})
