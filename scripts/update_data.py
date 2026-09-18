import json, math, statistics, time, re
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urlencode

ROOT=Path(__file__).resolve().parents[1]
DATA=ROOT/'data'; DATA.mkdir(exist_ok=True)
CMC='https://pro-api.coinmarketcap.com/public-api'
CG='https://api.coingecko.com/api/v3'
NOW=lambda: datetime.now(timezone.utc).isoformat()
HEADERS={'User-Agent':'Mozilla/5.0 CryptoCheckpoint/2.9','Accept':'application/json'}
OKX='https://www.okx.com'


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
            if i<retries:
                # Public CoinGecko is rate-limited. Respect Retry-After when exposed;
                # otherwise use a conservative backoff.
                wait=8*(i+1)
                if hasattr(e,'headers') and e.headers:
                    try: wait=max(wait,int(e.headers.get('Retry-After') or 0))
                    except Exception: pass
                time.sleep(wait)
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


HISTORY_CACHE=DATA/'history_cache.json'
HISTORY_TTL=3600

def load_history_cache():
    try: return json.loads(HISTORY_CACHE.read_text())
    except Exception: return {}

def save_history_cache(cache): HISTORY_CACHE.write_text(json.dumps(cache,separators=(',',':')))

history_cache=load_history_cache()

def okx_history(coin_base):
    # Prefer the USDT perpetual so technical/volume data reflects futures activity.
    # OKX public market endpoints are unauthenticated and support recent candles.
    candidates=[f'{coin_base}-USDT-SWAP', f'{coin_base}-USDT']
    last=None
    for inst in candidates:
        try:
            q=urlencode({'instId':inst,'bar':'1H','limit':'600'})
            j=get_json(f'{OKX}/api/v5/market/candles?{q}',timeout=20,retries=1)
            if str(j.get('code'))!='0': raise RuntimeError(f"OKX {j.get('code')}: {j.get('msg')}" )
            rows=[]
            for r in j.get('data',[]):
                if len(r)<9: continue
                # OKX returns newest first. Use only completed candles.
                if str(r[8])!='1': continue
                rows.append([int(r[0])//1000,float(r[1]),float(r[2]),float(r[3]),float(r[4]),float(r[7] or r[5] or 0)])
            rows.sort(key=lambda x:x[0])
            if len(rows)>=120: return rows, ('OKX '+('SWAP' if inst.endswith('-SWAP') else 'SPOT'),inst)
            if rows: last=RuntimeError(f'OKX {inst}: only {len(rows)} completed 1H candles')
        except Exception as e: last=e
    raise last or RuntimeError(f'OKX: no history for {coin_base}')

def fetch_coin_history(coin_id, base_symbol=None):
    key=str(coin_id)
    now=time.time()
    cached=history_cache.get(key)
    if isinstance(cached,dict) and cached.get('fetched_at') and now-float(cached['fetched_at']) < HISTORY_TTL and cached.get('rows'):
        return cached['rows'], True, cached.get('source','OKX cached')
    rows,source=okx_history(str(base_symbol or '').upper())
    history_cache[key]={'fetched_at':now,'rows':rows,'source':source[0],'instrument':source[1]}
    return rows,False,source[0]


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

def is_non_crypto_asset(x):
    """Exclude obvious tokenized equities/stocks from the crypto trading universe."""
    tags=x.get('tags') or []
    tag_text=' '.join(str(t.get('slug') if isinstance(t,dict) else t) for t in tags).lower()
    name=str(x.get('name') or '').lower()
    bad_terms=('tokenized-stock','tokenized-stock-representation','tokenized-stocks','equity-token')
    if any(t in tag_text for t in bad_terms): return True
    if 'tokenized stock' in name or 'tokenized stocks' in name: return True
    return False

# ---------------- CoinGecko derivatives ----------------
derivs=[]; deriv_error=None
deriv_by_base={}
try: derivs=cg('/derivatives') or []
except Exception as e: deriv_error=str(e)
if deriv_error:ctx['derivativesError']=deriv_error

# Build derivative lookup. Prefer USDT-like contracts and liquid markets.
# CMC can contain multiple assets with the same ticker symbol. To avoid attaching
# one asset's derivative contract to another same-symbol asset, only auto-map
# derivatives for CMC symbols that are unique in the filtered asset universe.
filtered_listings=[x for x in listings if isinstance(x,dict) and not is_non_crypto_asset(x)]
symbol_counts={}
for a in filtered_listings:
    b=str(a.get('symbol') or '').upper()
    if b: symbol_counts[b]=symbol_counts.get(b,0)+1
unique_symbols={b for b,n in symbol_counts.items() if n==1}
for d in derivs if isinstance(derivs,list) else []:
    base=derivative_base(d)
    target=str(d.get('target') or '').upper()
    symbol=str(d.get('symbol') or '').upper()
    market_name=str(d.get('market') or d.get('market_name') or '')
    # Keep USD/USDT perpetual-like contracts; avoid dated futures when contract_type explicitly says futures.
    if target and target not in ('USDT','USD','USDC'): continue
    if not target and not any(q in symbol for q in ('USDT','USD')): continue
    if not base or base not in unique_symbols: continue
    try: vol=float(d.get('volume_24h') or 0); oi=float(d.get('open_interest') or 0)
    except: vol=0;oi=0
    item={'derivMarket':market_name,'derivSymbol':symbol,'oi':oi,'funding':float(d.get('funding_rate') or 0)*100 if d.get('funding_rate') is not None else None,'derivVolume':vol,'basis':float(d.get('basis') or 0) if d.get('basis') is not None else None,'derivPrice':float(d.get('price') or 0) if d.get('price') else None}
    # Choose the highest-volume derivative for each base asset.
    if base not in deriv_by_base or vol>deriv_by_base[base]['derivVolume']: deriv_by_base[base]=item

# ---------------- Build broad market rows ----------------
rows=[]
for x in filtered_listings:
    if not isinstance(x,dict): continue
    sym=str(x.get('symbol') or '').upper()+'USDT'
    if sym=='USDTUSDT':continue
    q=usd_quote(x)
    price=q.get('price')
    if price is None:continue
    base=str(x.get('symbol') or '').upper()
    d=deriv_by_base.get(base,{})
    rows.append({'assetKey':f"CMC:{x.get('id')}",'baseSymbol':base,'symbol':sym,'coinId':x.get('id'),'name':x.get('name'),'price':float(price),'change':float(q.get('percent_change_24h') or 0),'high':None,'low':None,'volume':float(q.get('volume_24h') or 0),'marketCap':float(q.get('market_cap') or 0),'oi':d.get('oi'),'funding':d.get('funding'),'oiDelta':None,'derivVolume':d.get('derivVolume'),'derivMarket':d.get('derivMarket'),'derivSymbol':d.get('derivSymbol'),'source':'CMC + CoinGecko derivatives','ts':int(time.time()*1000)})

# Load previous snapshot for OI deltas.
prev={}
try: prev={x.get('assetKey') or f"CMC:{x.get('coinId')}":x for x in json.loads((DATA/'market.json').read_text()).get('symbols',[])}
except Exception: pass
for x in rows:
    old=prev.get(x.get('assetKey'),{});oldoi=old.get('oi');
    if x.get('oi') is not None and oldoi not in (None,0): x['oiDelta']=(x['oi']-float(oldoi))/float(oldoi)*100

# Stage A: prioritize liquid/active names, while retaining core.
rows.sort(key=lambda x:(x.get('derivVolume') or 0,x.get('volume') or 0),reverse=True)
by_asset={x['assetKey']:x for x in rows}
by_symbol={x['symbol']:x for x in rows if symbol_counts.get(x.get('baseSymbol'))==1}
# Candidate pool: top 40 by 24h volume, plus top derivative volume names, plus core.
vol_rank=sorted(rows,key=lambda x:x.get('volume') or 0,reverse=True)[:60]
deriv_rank=sorted([x for x in rows if x.get('derivVolume')],key=lambda x:x.get('derivVolume') or 0,reverse=True)[:60]
candidates={x['assetKey'] for x in vol_rank+deriv_rank}
for s in ['BTCUSDT','ETHUSDT','XRPUSDT','SOLUSDT']: 
    if s in by_symbol:candidates.add(by_symbol[s]['assetKey'])

# Fetch history for a small Stage-B shortlist to stay within CoinGecko free-tier limits.
# Four core assets are always retained when available.
ranked=sorted([by_asset[s] for s in candidates],key=lambda x:(x.get('derivVolume') or 0,x.get('volume') or 0),reverse=True)[:12]
for s in ['BTCUSDT','ETHUSDT','XRPUSDT','SOLUSDT']:
    if s in by_symbol and all(x['assetKey']!=by_symbol[s]['assetKey'] for x in ranked):ranked.append(by_symbol[s])

histories={}
history_stats={'requested':0,'cacheHits':0,'freshFetches':0,'errors':0}
# Fetch sequentially. CoinGecko's public API is only about 5–15 calls/minute, so
# parallel requests were causing the 429s seen in previous runs. Cached histories
# are reused for 1 hour, while the dashboard can still refresh every 15 minutes.
for x in ranked:
    if not x.get('coinId'): continue
    history_stats['requested']+=1
    try:
        h,cache_hit,hsrc=fetch_coin_history(x['coinId'],x.get('baseSymbol'))
        histories[x['assetKey']]=h
        x['historySource']=hsrc
        history_stats['cacheHits']+=1 if cache_hit else 0
        history_stats['freshFetches']+=0 if cache_hit else 1
        sp=volume_spike(h)
        if sp:
            sp['spikeSource']=hsrc+' 1H market volume'
            x.update(sp)
        if not cache_hit: time.sleep(1)
    except Exception as e:
        history_stats['errors']+=1
        x['historyError']=str(e)

# Keep only the current Stage-B cache universe so the committed cache stays small.
keep_ids={str(x.get('coinId')) for x in ranked if x.get('coinId')}
history_cache={k:v for k,v in history_cache.items() if k in keep_ids}
save_history_cache(history_cache)

for x in rows:
    vr=x.get('volRatio') or 0
    x['stageAScore']=round(min(3,abs(x.get('change',0))/2)+min(2,abs(x.get('oiDelta') or 0)/5)+(3 if vr>=1.75 else 1.5 if vr>=1.25 else 0),3)

# Stage B targeted technicals.
analysis=[]
for x in ranked:
    if x['assetKey'] not in histories: continue
    a=technical_from_history(x['symbol'],x,histories[x['assetKey']])
    if 'error' not in a:
        a['assetKey']=x['assetKey']
        a['coinId']=x['coinId']
        a['baseSymbol']=x['baseSymbol']
        analysis.append(a)
analysis.sort(key=lambda x:x.get('score',0),reverse=True)

# Make sure every core asset has an analysis if its history succeeded.
ctx['engine_status']='OK' if rows else 'NO_MARKET_DATA'
ctx['context_status']='COMPLETE' if all(k in ctx for k in ('fng','altseason','btcDom','totalMarketCap','btcCap','ethCap','total3','total3Btc')) else 'PARTIAL'
ctx['marketSource']='CoinMarketCap listings'
ctx['derivativesSource']='CoinGecko aggregated derivatives'
ctx['volumeSource']='OKX 1H candles (futures-first, spot fallback)'
ctx['volumeSpikeTimeframe']='1H'
ctx['assetUniverse']='CMC listings excluding obvious tokenized-stock assets; unique identity = coinId'
ctx['stageBHistoryLimit']=12
ctx['oiDeltaDefinition']='snapshot-to-snapshot change versus previous market.json run, not 24h'
ctx['derivativesMapping']='Only unique CMC ticker symbols are auto-enriched to avoid same-symbol collisions'
ctx['historyProvider']='OKX 1H candles (futures-first, spot fallback; serialized + 1h cache)'
ctx['historyCacheTTLSeconds']=HISTORY_TTL
ctx['historyFetchPacingSeconds']=1
ctx['historyStats']=history_stats
ctx['generated_at']=NOW()

(DATA/'market.json').write_text(json.dumps({'generated_at':NOW(),'source':'CMC + CoinGecko','count':len(rows),'symbols':rows,'engine_status':'OK' if rows else 'NO_MARKET_DATA'},separators=(',',':')))
(DATA/'analysis.json').write_text(json.dumps({'generated_at':NOW(),'source':'OKX hourly → 4H/1D','count':len(analysis),'analysis':analysis},separators=(',',':')))
(DATA/'context.json').write_text(json.dumps(ctx,separators=(',',':')))

print(f'Generated {len(rows)} broad market rows, {len(analysis)} Stage-B analyses')
print('Market source: CoinMarketCap listings')
print('Derivatives source: CoinGecko aggregated derivatives' if derivs else 'Derivatives source: unavailable')
print('Volume spike source: CoinGecko hourly market history; timeframe=1H')
print('Context:',{k:ctx.get(k) for k in ('fng','fngLabel','altseason','btcDom','totalMarketCap','total3','total3Btc')})
