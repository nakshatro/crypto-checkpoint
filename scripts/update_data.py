import json, math, statistics, time, re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urlencode

ROOT=Path(__file__).resolve().parents[1]
DATA=ROOT/'data'; DATA.mkdir(exist_ok=True)
CMC='https://pro-api.coinmarketcap.com/public-api'
CG='https://api.coingecko.com/api/v3'
NOW=lambda: datetime.now(timezone.utc).isoformat()
HEADERS={'User-Agent':'Mozilla/5.0 CryptoCheckpoint/2.4','Accept':'application/json'}


def get_json(url, timeout=20, retries=2):
    last=None
    for i in range(retries+1):
        try:
            req=Request(url,headers=HEADERS)
            with urlopen(req,timeout=timeout) as r:
                raw=r.read().decode('utf-8')
                return json.loads(raw)
        except Exception as e:
            last=e
            if i<retries: time.sleep(1.5*(i+1))
    raise last


def api(base,path,params=None,timeout=20):
    q=('?'+urlencode(params)) if params else ''
    return get_json(base+path+q,timeout)


def cmc(path,params=None):
    j=api(CMC,path,params)
    st=j.get('status',{})
    if str(st.get('error_code','0'))!='0': raise RuntimeError(f"CMC {st.get('error_code')}: {st.get('error_message')}")
    return j.get('data')


def cg(path,params=None):
    j=api(CG,path,params)
    if isinstance(j,dict) and j.get('error'): raise RuntimeError(f"CoinGecko: {j['error']}")
    return j


def ema(vals,p):
    if len(vals)<p:return None
    k=2/(p+1); e=sum(vals[:p])/p
    for v in vals[p:]: e=v*k+e*(1-k)
    return e


def sma(vals,p): return sum(vals[-p:])/p if len(vals)>=p else None


def rsi(vals,p=14):
    if len(vals)<p+1:return None
    gains=[];losses=[]
    for i in range(len(vals)-p,len(vals)):
        d=vals[i]-vals[i-1];gains.append(max(d,0));losses.append(max(-d,0))
    ag=sum(gains)/p;al=sum(losses)/p
    if al==0:return 100.0
    return 100-100/(1+ag/al)


def atr(rows,p=14):
    if len(rows)<p+1:return None
    tr=[]
    for i in range(1,len(rows)):
        tr.append(max(rows[i][2]-rows[i][3],abs(rows[i][2]-rows[i-1][4]),abs(rows[i][3]-rows[i-1][4])))
    return sma(tr,p)


def aggregate_hourly(hourly, hours):
    # hourly = [(ts,open,high,low,close,volume)]
    if not hourly:return []
    out=[]; bucket=None; cur=None
    for row in hourly:
        ts=row[0]; b=(ts//(hours*3600))*hours*3600
        if bucket!=b:
            if cur: out.append(cur)
            bucket=b;cur=[b,row[1],row[2],row[3],row[4],row[5],1]
        else:
            cur[2]=max(cur[2],row[2]);cur[3]=min(cur[3],row[3]);cur[4]=row[4];cur[5]+=row[5];cur[6]+=1
    if cur:out.append(cur)
    return [x for x in out if x[6]>=max(1,hours//2)]


def structure(rows,look=30):
    if not rows:return {'high':None,'low':None,'trend':'Range'}
    r=rows[-look:];c=[x[4] for x in rows];e20,e55=ema(c,20),ema(c,55)
    trend='Rising' if e20 and e55 and e20>e55 else 'Falling' if e20 and e55 and e20<e55 else 'Range'
    return {'high':max(x[2] for x in r),'low':min(x[3] for x in r),'trend':trend}


def parse_symbol(s):
    s=(s or '').upper().replace('-','/').replace('_','/')
    if '/' in s:
        parts=s.split('/')
        return parts[0]
    for q in ('USDT','USD','USDC','BTC','ETH'):
        if s.endswith(q) and len(s)>len(q):return s[:-len(q)]
    return s


def derivative_base(d):
    # CoinGecko derivative ticker shapes have changed over time; handle common forms.
    for k in ('base','base_symbol','coin_id'):
        if d.get(k): return str(d[k]).upper().replace('-USDT','').replace('_USDT','')
    return parse_symbol(d.get('symbol'))


def fetch_coin_history(coin_id):
    # 30d hourly is enough for 4H MA100 and 1D structure; one call per selected asset.
    j=cg(f'/coins/{coin_id}/market_chart',{'vs_currency':'usd','days':'30','interval':'hourly'})
    prices=j.get('prices',[]); vols=j.get('total_volumes',[])
    vm={int(t/1000):float(v) for t,v in vols}
    rows=[]
    for p in prices:
        ts=int(p[0]/1000); rows.append([ts,float(p[1]),float(p[1]),float(p[1]),float(p[1]),vm.get(ts,0)])
    return rows


def technical_from_history(symbol,m,history):
    try:
        h4=aggregate_hourly(history,4); d1=aggregate_hourly(history,24)
        c=[x[4] for x in h4]
        e10,e20,e55=ema(c,10),ema(c,20),ema(c,55);ma50,ma100=sma(c,50),sma(c,100);rr=rsi(c);aa=atr(h4)
        s4=structure(h4);s1=structure(d1)
        score=5.0; reasons=[]
        if e10 and e20 and e55:
            if e10>e20>e55:score+=1.2;reasons.append('EMA bullish')
            elif e10<e20<e55:score-=1.2;reasons.append('EMA bearish')
        if ma50 and ma100:
            if ma50>ma100:score+=.5;reasons.append('MA50 > MA100')
            elif ma50<ma100:score-=.5;reasons.append('MA50 < MA100')
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
        if vr>=1.75:score+=.8;reasons.append(f'{vr:.1f}x 1H volume spike')
        elif vr>=1.25:score+=.3;reasons.append('elevated 1H volume')
        score=max(0,min(10,score));signal='LONG' if score>=7 else 'SHORT' if score<=4 else 'NEUTRAL'
        return {'symbol':symbol,'score':round(score,2),'signal':signal,'rsi':rr,'ema10':e10,'ema20':e20,'ema55':e55,'ma50':ma50,'ma100':ma100,'atr':aa,'structure4h':s4,'structure1d':s1,'trend':s4['trend'],'reasons':reasons,'historySource':'CoinGecko market_chart'}
    except Exception as e:
        return {'symbol':symbol,'error':str(e)}


def volume_spike(history):
    if len(history)<22:return None
    # Use completed hourly bars; latest may be incomplete, so use previous bar as current completed bar.
    vols=[float(x[5]) for x in history]
    cur=vols[-2];base=statistics.median(vols[-22:-2])
    if base<=0:return None
    ratio=cur/base
    label='EXTREME' if ratio>=4 else 'MAJOR' if ratio>=2.5 else 'SPIKE' if ratio>=1.75 else 'ELEVATED' if ratio>=1.25 else 'NORMAL'
    return {'vol1h':cur,'volBaseline':base,'volRatio':ratio,'spike':label,'spikeSource':'CoinGecko 1H market volume'}

# Normalize CMC v3 quote shapes. Listings/Quotes v3 return quote as a LIST.
def usd_quote(asset):
    q=asset.get('quote') if isinstance(asset,dict) else None
    if isinstance(q,list):
        for item in q:
            if isinstance(item,dict) and str(item.get('symbol','')).upper()=='USD':
                return item
        return q[0] if q and isinstance(q[0],dict) else {}
    if isinstance(q,dict):
        if isinstance(q.get('USD'),dict): return q['USD']
        return q
    return {}

# ---------------- CMC broad market ----------------
ctx={'source':'CoinMarketCap Keyless Public API','generated_at':NOW()}
listings=[]
try:
    listings=cmc('/v3/cryptocurrency/listings/latest',{'start':'1','limit':'1000','convert':'USD','sort':'volume_24h','sort_dir':'desc'}) or []
except Exception as e:
    ctx['listingsError']=str(e)
ctx['listingCount']=len(listings) if isinstance(listings,list) else 0

# CMC context: these are authoritative CMC-native values.
try:
    d=cmc('/v3/fear-and-greed/latest') or {};ctx.update(fng=int(d.get('value')),fngLabel=d.get('value_classification'),fngUpdated=d.get('update_time'))
except Exception as e:ctx['fngError']=str(e)
try:
    d=cmc('/v1/altcoin-season-index/latest') or {};ctx.update(altseason=int(d.get('altcoin_index')),altseasonUpdated=d.get('snapshot_time') or d.get('update_time'))
except Exception as e:ctx['altseasonError']=str(e)
try:
    g=cmc('/v1/global-metrics/quotes/latest',{'convert':'USD'}) or {}
    q=g.get('quote',{}).get('USD',{}) if isinstance(g.get('quote'),dict) else {}
    if g.get('btc_dominance') is not None: ctx['btcDom']=float(g['btc_dominance'])
    if q.get('total_market_cap') is not None: ctx['totalMarketCap']=float(q['total_market_cap'])
    if q.get('total_market_cap_yesterday_percentage_change') is not None: ctx['totalMarketCapChange']=float(q['total_market_cap_yesterday_percentage_change'])
except Exception as e:ctx['globalError']=str(e)
# Derive BTC/ETH market caps directly from the already-fetched CMC listings.
# CMC v3 returns each asset's `quote` as a LIST containing the USD quote.
# Using the listings also avoids a second request and prevents quote-shape mismatches.
def listing_by_id(asset_id):
    for a in listings if isinstance(listings,list) else []:
        if str(a.get('id')) == str(asset_id):
            return a
    return None

btc_asset=listing_by_id(1)
eth_asset=listing_by_id(1027)
if btc_asset:
    bq=usd_quote(btc_asset) if 'usd_quote' in globals() else {}
    if bq.get('market_cap') is not None: ctx['btcCap']=float(bq['market_cap'])
    if bq.get('price') is not None: ctx['btcPrice']=float(bq['price'])
if eth_asset:
    eq=usd_quote(eth_asset) if 'usd_quote' in globals() else {}
    if eq.get('market_cap') is not None: ctx['ethCap']=float(eq['market_cap'])
    if eq.get('price') is not None: ctx['ethPrice']=float(eq['price'])

if 'btcCap' in ctx and 'ethCap' in ctx and 'totalMarketCap' in ctx:
    ctx['total3']=ctx['totalMarketCap']-ctx['btcCap']-ctx['ethCap']
    ctx['total3Btc']=ctx['total3']/ctx['btcCap'] if ctx['btcCap'] else None
    ctx['total3Method']='Total market cap minus BTC and ETH market caps'
else:
    ctx['total3Error']='BTC/ETH market cap unavailable from CMC listings'

# ---------------- CoinGecko derivatives ----------------
derivs=[]; deriv_error=None
try: derivs=cg('/derivatives') or []
except Exception as e: deriv_error=str(e)
if deriv_error:ctx['derivativesError']=deriv_error

# Build derivative lookup. Prefer USDT-like contracts and liquid markets.
deriv_by_base={}
for d in derivs if isinstance(derivs,list) else []:
    base=derivative_base(d)
    target=str(d.get('target') or '').upper()
    symbol=str(d.get('symbol') or '').upper()
    market_name=str(d.get('market') or d.get('market_name') or '')
    # Keep USD/USDT perpetual-like contracts; avoid dated futures when contract_type explicitly says futures.
    if target and target not in ('USDT','USD','USDC'): continue
    if not target and not any(q in symbol for q in ('USDT','USD')): continue
    if not base: continue
    try: vol=float(d.get('volume_24h') or 0); oi=float(d.get('open_interest') or 0)
    except: vol=0;oi=0
    item={'derivMarket':market_name,'derivSymbol':symbol,'oi':oi,'funding':float(d.get('funding_rate') or 0)*100 if d.get('funding_rate') is not None else None,'derivVolume':vol,'basis':float(d.get('basis') or 0) if d.get('basis') is not None else None,'derivPrice':float(d.get('price') or 0) if d.get('price') else None}
    # Choose the highest-volume derivative for each base asset.
    if base not in deriv_by_base or vol>deriv_by_base[base]['derivVolume']: deriv_by_base[base]=item

# ---------------- Build broad market rows ----------------
rows=[]
for x in listings if isinstance(listings,list) else []:
    if not isinstance(x,dict): continue
    sym=str(x.get('symbol') or '').upper()+'USDT'
    if sym=='USDTUSDT':continue
    q=usd_quote(x)
    price=q.get('price')
    if price is None:continue
    base=str(x.get('symbol') or '').upper()
    d=deriv_by_base.get(base,{})
    rows.append({'symbol':sym,'coinId':x.get('id'),'name':x.get('name'),'price':float(price),'change':float(q.get('percent_change_24h') or 0),'high':None,'low':None,'volume':float(q.get('volume_24h') or 0),'marketCap':float(q.get('market_cap') or 0),'oi':d.get('oi'),'funding':d.get('funding'),'oiDelta':None,'derivVolume':d.get('derivVolume'),'derivMarket':d.get('derivMarket'),'derivSymbol':d.get('derivSymbol'),'source':'CMC + CoinGecko derivatives','ts':int(time.time()*1000)})

# Load previous snapshot for OI deltas.
prev={}
try: prev={x['symbol']:x for x in json.loads((DATA/'market.json').read_text()).get('symbols',[])}
except Exception: pass
for x in rows:
    old=prev.get(x['symbol'],{});oldoi=old.get('oi');
    if x.get('oi') is not None and oldoi not in (None,0): x['oiDelta']=(x['oi']-float(oldoi))/float(oldoi)*100

# Stage A: prioritize liquid/active names, while retaining core.
rows.sort(key=lambda x:(x.get('derivVolume') or 0,x.get('volume') or 0),reverse=True)
by_symbol={x['symbol']:x for x in rows}
# Candidate pool: top 40 by 24h volume, plus top derivative volume names, plus core.
vol_rank=sorted(rows,key=lambda x:x.get('volume') or 0,reverse=True)[:60]
deriv_rank=sorted([x for x in rows if x.get('derivVolume')],key=lambda x:x.get('derivVolume') or 0,reverse=True)[:60]
candidates={x['symbol'] for x in vol_rank+deriv_rank}
for s in ['BTCUSDT','ETHUSDT','XRPUSDT','SOLUSDT']: 
    if s in by_symbol:candidates.add(s)

# Fetch CoinGecko hourly history for up to 28 assets (20 broad + 4 core + derivative leaders).
ranked=sorted([by_symbol[s] for s in candidates],key=lambda x:(x.get('derivVolume') or 0,x.get('volume') or 0),reverse=True)[:28]
for s in ['BTCUSDT','ETHUSDT','XRPUSDT','SOLUSDT']:
    if s in by_symbol and all(x['symbol']!=s for x in ranked):ranked.append(by_symbol[s])

histories={}
with ThreadPoolExecutor(max_workers=5) as ex:
    fut={ex.submit(fetch_coin_history,x['coinId']):x for x in ranked if x.get('coinId')}
    for f in as_completed(fut):
        x=fut[f]
        try:
            h=f.result();histories[x['symbol']]=h
            sp=volume_spike(h)
            if sp:x.update(sp)
        except Exception as e:x['historyError']=str(e)

for x in rows:
    vr=x.get('volRatio') or 0
    x['stageAScore']=round(min(3,abs(x.get('change',0))/2)+min(2,abs(x.get('oiDelta') or 0)/5)+(3 if vr>=1.75 else 1.5 if vr>=1.25 else 0),3)

# Stage B targeted technicals.
analysis=[]
with ThreadPoolExecutor(max_workers=5) as ex:
    fut={ex.submit(technical_from_history,x['symbol'],x,histories[x['symbol']]):x for x in ranked if x['symbol'] in histories}
    for f in as_completed(fut):
        a=f.result()
        if 'error' not in a:analysis.append(a)
analysis.sort(key=lambda x:x.get('score',0),reverse=True)

# Make sure every core asset has an analysis if its history succeeded.
ctx['engine_status']='OK' if rows else 'NO_MARKET_DATA'
ctx['context_status']='COMPLETE' if all(k in ctx for k in ('fng','altseason','btcDom','totalMarketCap','btcCap','ethCap','total3','total3Btc')) else 'PARTIAL'
ctx['marketSource']='CoinMarketCap listings'
ctx['derivativesSource']='CoinGecko aggregated derivatives'
ctx['volumeSource']='CoinGecko hourly market_chart'
ctx['volumeSpikeTimeframe']='1H'
ctx['generated_at']=NOW()

(DATA/'market.json').write_text(json.dumps({'generated_at':NOW(),'source':'CMC + CoinGecko','count':len(rows),'symbols':rows,'engine_status':'OK' if rows else 'NO_MARKET_DATA'},separators=(',',':')))
(DATA/'analysis.json').write_text(json.dumps({'generated_at':NOW(),'source':'CoinGecko hourly → 4H/1D','count':len(analysis),'analysis':analysis},separators=(',',':')))
(DATA/'context.json').write_text(json.dumps(ctx,separators=(',',':')))

print(f'Generated {len(rows)} broad market rows, {len(analysis)} Stage-B analyses')
print('Market source: CoinMarketCap listings')
print('Derivatives source: CoinGecko aggregated derivatives' if derivs else 'Derivatives source: unavailable')
print('Volume spike source: CoinGecko hourly market history; timeframe=1H')
print('Context:',{k:ctx.get(k) for k in ('fng','fngLabel','altseason','btcDom','totalMarketCap','total3','total3Btc')})
