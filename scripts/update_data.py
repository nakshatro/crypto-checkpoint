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
HEADERS={'User-Agent':'Mozilla/5.0 CryptoCheckpoint/2.12','Accept':'application/json'}
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
    # OKX market/candles returns at most 300 bars per request, so paginate backwards
    # to obtain enough 1H candles for the 4H MA100 calculation.
    candidates=[f'{coin_base}-USDT-SWAP', f'{coin_base}-USDT']
    last=None
    for inst in candidates:
        try:
            collected=[]
            cursor=None
            for _ in range(3):
                params={'instId':inst,'bar':'1H','limit':'300'}
                if cursor is not None: params['after']=str(cursor)
                q=urlencode(params)
                j=get_json(f'{OKX}/api/v5/market/candles?{q}',timeout=20,retries=1)
                if str(j.get('code'))!='0':
                    raise RuntimeError(f"OKX {j.get('code')}: {j.get('msg')}")
                batch=j.get('data',[])
                if not batch: break
                for r in batch:
                    if len(r)<9 or str(r[8])!='1': continue
                    collected.append([int(r[0])//1000,float(r[1]),float(r[2]),float(r[3]),float(r[4]),float(r[7] or r[5] or 0)])
                oldest=min(int(r[0]) for r in batch if len(r)>=1)
                if cursor==oldest: break
                cursor=oldest
                if len(collected)>=480: break
                time.sleep(0.4)
            # De-duplicate and sort oldest -> newest.
            uniq={r[0]:r for r in collected}
            rows=sorted(uniq.values(),key=lambda x:x[0])
            if len(rows)>=120:
                return rows[-600:],('OKX '+('SWAP' if inst.endswith('-SWAP') else 'SPOT'),inst)
            if rows: last=RuntimeError(f'OKX {inst}: only {len(rows)} completed 1H candles')
        except Exception as e:
            last=e
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



def confirmation_from_history(history, direction, entry_low, entry_high, atrv):
    # Completed 1H candle confirmation. A zone touch alone is never confirmation.
    if not history or len(history) < 4:
        return {'status':'NO_DATA','reason':'Insufficient completed candles'}
    last=history[-2] if len(history)>=2 else history[-1]
    prev=history[-3] if len(history)>=3 else history[-2]
    o,h,l,c=map(float,last[1:5])
    po,ph,pl,pc=map(float,prev[1:5])
    body=abs(c-o)
    atrv=max(float(atrv or 0), 1e-12)
    touched=l <= entry_high and h >= entry_low
    body_ratio=body/atrv
    close_buffer=0.05*atrv
    if direction=='LONG':
        bullish=c>o
        close_above=c > entry_high + close_buffer
        higher_close=c > pc
        # Require a meaningful bullish body and a close clearly outside the zone.
        confirmed=touched and bullish and close_above and higher_close and body_ratio>=0.25
        if confirmed:
            return {'status':'CONFIRMED','reason':'Zone touch + meaningful bullish 1H reaction + close above zone',
                    'candleOpen':o,'candleClose':c,'candleHigh':h,'candleLow':l,
                    'bodyATR':round(body_ratio,3),'closeBufferATR':0.05}
        if touched:
            reasons=[]
            if not bullish: reasons.append('1H candle not bullish')
            if not close_above: reasons.append('close not sufficiently above entry zone')
            if not higher_close: reasons.append('close did not exceed previous 1H close')
            if body_ratio<0.25: reasons.append('1H body too weak')
            return {'status':'TOUCHED_WAIT_CONFIRMATION','reason':'Entry zone touched; '+'; '.join(reasons),
                    'candleOpen':o,'candleClose':c,'candleHigh':h,'candleLow':l,
                    'bodyATR':round(body_ratio,3),'closeBufferATR':0.05}
        return {'status':'NOT_TRIGGERED','reason':'No completed 1H reaction from entry zone',
                'candleOpen':o,'candleClose':c,'candleHigh':h,'candleLow':l,
                'bodyATR':round(body_ratio,3),'closeBufferATR':0.05}
    bearish=c<o
    close_below=c < entry_low - close_buffer
    lower_close=c < pc
    confirmed=touched and bearish and close_below and lower_close and body_ratio>=0.25
    if confirmed:
        return {'status':'CONFIRMED','reason':'Zone touch + meaningful bearish 1H reaction + close below zone',
                'candleOpen':o,'candleClose':c,'candleHigh':h,'candleLow':l,
                'bodyATR':round(body_ratio,3),'closeBufferATR':0.05}
    if touched:
        reasons=[]
        if not bearish: reasons.append('1H candle not bearish')
        if not close_below: reasons.append('close not sufficiently below entry zone')
        if not lower_close: reasons.append('close did not fall below previous 1H close')
        if body_ratio<0.25: reasons.append('1H body too weak')
        return {'status':'TOUCHED_WAIT_CONFIRMATION','reason':'Entry zone touched; '+'; '.join(reasons),
                'candleOpen':o,'candleClose':c,'candleHigh':h,'candleLow':l,
                'bodyATR':round(body_ratio,3),'closeBufferATR':0.05}
    return {'status':'NOT_TRIGGERED','reason':'No completed 1H reaction from entry zone',
            'candleOpen':o,'candleClose':c,'candleHigh':h,'candleLow':l,
            'bodyATR':round(body_ratio,3),'closeBufferATR':0.05}


def build_trade_setup(m, tech, history=None):
    """Construct a conditional setup with confirmation and risk-quality filters."""
    price=float(m.get('price') or 0)
    atrv=float(tech.get('atr') or 0)
    e10=tech.get('ema10'); e20=tech.get('ema20')
    s4=tech.get('structure4h') or {}; s1=tech.get('structure1d') or {}
    signal=tech.get('signal'); score=float(tech.get('score') or 0)
    extension=tech.get('extension','NORMAL')
    hsrc=str(tech.get('historySource') or '')
    futures='SWAP' in hsrc.upper()

    # Guardrails: reject setups with an impractically wide structural stop.
    MAX_STOP_ATR=3.5
    MAX_RISK_PCT=8.0

    setup=None
    if signal=='LONG' and score>=7 and s4.get('trend')=='Rising' and s1.get('trend')!='Falling' and extension not in ('EXTREME_HIGH',):
        if e10 and e20 and atrv and price:
            lo=min(e10,e20); hi=max(e10,e20)
            entry_low=max(0.0,lo-0.15*atrv); entry_high=hi+0.15*atrv
            swing_low=float(s4.get('low') or 0)
            sl=max(0.0,swing_low-0.10*atrv)
            if sl>=entry_low: sl=max(0.0,entry_low-0.75*atrv)
            risk=max(entry_high-sl,0.0)
            risk_pct=(risk/entry_high*100) if entry_high else 999
            stop_atr=risk/atrv if atrv else 999
            if risk_pct > MAX_RISK_PCT or stop_atr > MAX_STOP_ATR:
                return None
            tp1=entry_high+1.5*risk; tp2=entry_high+2.5*risk
            if price < entry_low: status='BELOW_ENTRY_ZONE'
            elif price <= entry_high: status='INSIDE_ENTRY_ZONE'
            else: status='ABOVE_ENTRY_ZONE'
            confirmation=confirmation_from_history(history,'LONG',entry_low,entry_high,atrv) if history else {'status':'NO_DATA','reason':'History unavailable'}
            if confirmation.get('status')=='CONFIRMED': entry_trigger='CONFIRMED'
            elif status=='INSIDE_ENTRY_ZONE': entry_trigger='IN_ZONE_WAIT_CONFIRMATION'
            else: entry_trigger='WAIT_PULLBACK'
            setup={'direction':'LONG','entryLow':entry_low,'entryHigh':entry_high,'entryStatus':status,'entryTrigger':entry_trigger,
                   'confirmation':confirmation,'stopLoss':sl,'tp1':tp1,'tp2':tp2,'riskPerUnit':risk,
                   'riskPct':risk_pct,'stopDistanceATR':stop_atr,'riskQuality':'PASS',
                   'rrTp1':1.5,'rrTp2':2.5,'setupType':'EMA pullback + structure continuation',
                   'historyMarketType':'FUTURES' if futures else 'SPOT_FALLBACK'}
    elif signal=='SHORT' and score<=4 and s4.get('trend')=='Falling' and s1.get('trend')!='Rising' and extension not in ('EXTREME_LOW',):
        if e10 and e20 and atrv and price:
            lo=min(e10,e20); hi=max(e10,e20)
            entry_low=max(0.0,lo-0.15*atrv); entry_high=hi+0.15*atrv
            swing_high=float(s4.get('high') or 0)
            sl=swing_high+0.10*atrv
            if sl<=entry_high: sl=entry_high+0.75*atrv
            risk=max(sl-entry_low,0.0)
            risk_pct=(risk/entry_low*100) if entry_low else 999
            stop_atr=risk/atrv if atrv else 999
            if risk_pct > MAX_RISK_PCT or stop_atr > MAX_STOP_ATR:
                return None
            tp1=max(0.0,entry_low-1.5*risk); tp2=max(0.0,entry_low-2.5*risk)
            if price > entry_high: status='ABOVE_ENTRY_ZONE'
            elif price >= entry_low: status='INSIDE_ENTRY_ZONE'
            else: status='BELOW_ENTRY_ZONE'
            confirmation=confirmation_from_history(history,'SHORT',entry_low,entry_high,atrv) if history else {'status':'NO_DATA','reason':'History unavailable'}
            if confirmation.get('status')=='CONFIRMED': entry_trigger='CONFIRMED'
            elif status=='INSIDE_ENTRY_ZONE': entry_trigger='IN_ZONE_WAIT_CONFIRMATION'
            else: entry_trigger='WAIT_RETEST'
            setup={'direction':'SHORT','entryLow':entry_low,'entryHigh':entry_high,'entryStatus':status,'entryTrigger':entry_trigger,
                   'confirmation':confirmation,'stopLoss':sl,'tp1':tp1,'tp2':tp2,'riskPerUnit':risk,
                   'riskPct':risk_pct,'stopDistanceATR':stop_atr,'riskQuality':'PASS',
                   'rrTp1':1.5,'rrTp2':2.5,'setupType':'EMA retest + structure continuation',
                   'historyMarketType':'FUTURES' if futures else 'SPOT_FALLBACK'}
    return setup


def build_trade_setup(m, tech, history=None):
    """Construct a conditional setup from already-calculated technicals.
    This is a setup/entry engine, not proof of an exchange fill.
    """
    price=float(m.get('price') or 0)
    atrv=float(tech.get('atr') or 0)
    e10=tech.get('ema10'); e20=tech.get('ema20')
    s4=tech.get('structure4h') or {}; s1=tech.get('structure1d') or {}
    signal=tech.get('signal'); score=float(tech.get('score') or 0)
    extension=tech.get('extension','NORMAL')
    hsrc=str(tech.get('historySource') or '')
    futures='SWAP' in hsrc.upper()

    setup=None
    if signal=='LONG' and score>=7 and s4.get('trend')=='Rising' and s1.get('trend')!='Falling' and extension not in ('EXTREME_HIGH',):
        if e10 and e20 and atrv and price:
            lo=min(e10,e20); hi=max(e10,e20)
            # Entry zone is the EMA10-EMA20 pullback band, widened by 0.15 ATR.
            entry_low=max(0.0,lo-0.15*atrv); entry_high=hi+0.15*atrv
            # Invalidation uses the latest 4H structural low with a small ATR buffer.
            swing_low=float(s4.get('low') or 0)
            sl=max(0.0,swing_low-0.10*atrv)
            if sl>=entry_low: sl=max(0.0,entry_low-0.75*atrv)
            risk=max(entry_high-sl,0.0)
            tp1=entry_high+1.5*risk; tp2=entry_high+2.5*risk
            if price < entry_low: status='BELOW_ENTRY_ZONE'
            elif price <= entry_high: status='INSIDE_ENTRY_ZONE'
            else: status='ABOVE_ENTRY_ZONE'
            confirmation=confirmation_from_history(history,'LONG',entry_low,entry_high) if history else {'status':'NO_DATA','reason':'History unavailable'}
            if confirmation.get('status')=='CONFIRMED': entry_trigger='CONFIRMED'
            elif status=='INSIDE_ENTRY_ZONE': entry_trigger='IN_ZONE_WAIT_CONFIRMATION'
            else: entry_trigger='WAIT_PULLBACK'
            setup={'direction':'LONG','entryLow':entry_low,'entryHigh':entry_high,'entryStatus':status,'entryTrigger':entry_trigger,
                   'confirmation':confirmation, 'stopLoss':sl,'tp1':tp1,'tp2':tp2,'riskPerUnit':risk,
                   'rrTp1':1.5,'rrTp2':2.5,'setupType':'EMA pullback + structure continuation',
                   'historyMarketType':'FUTURES' if futures else 'SPOT_FALLBACK'}
    elif signal=='SHORT' and score<=4 and s4.get('trend')=='Falling' and s1.get('trend')!='Rising' and extension not in ('EXTREME_LOW',):
        if e10 and e20 and atrv and price:
            lo=min(e10,e20); hi=max(e10,e20)
            entry_low=max(0.0,lo-0.15*atrv); entry_high=hi+0.15*atrv
            swing_high=float(s4.get('high') or 0)
            sl=swing_high+0.10*atrv
            if sl<=entry_high: sl=entry_high+0.75*atrv
            risk=max(sl-entry_low,0.0)
            tp1=max(0.0,entry_low-1.5*risk); tp2=max(0.0,entry_low-2.5*risk)
            if price > entry_high: status='ABOVE_ENTRY_ZONE'
            elif price >= entry_low: status='INSIDE_ENTRY_ZONE'
            else: status='BELOW_ENTRY_ZONE'
            confirmation=confirmation_from_history(history,'SHORT',entry_low,entry_high) if history else {'status':'NO_DATA','reason':'History unavailable'}
            if confirmation.get('status')=='CONFIRMED': entry_trigger='CONFIRMED'
            elif status=='INSIDE_ENTRY_ZONE': entry_trigger='IN_ZONE_WAIT_CONFIRMATION'
            else: entry_trigger='WAIT_RETEST'
            setup={'direction':'SHORT','entryLow':entry_low,'entryHigh':entry_high,'entryStatus':status,'entryTrigger':entry_trigger,
                   'confirmation':confirmation, 'stopLoss':sl,'tp1':tp1,'tp2':tp2,'riskPerUnit':risk,
                   'rrTp1':1.5,'rrTp2':2.5,'setupType':'EMA retest + structure continuation',
                   'historyMarketType':'FUTURES' if futures else 'SPOT_FALLBACK'}
    return setup

def technical_from_history(symbol,m,history):
    try:
        h4=aggregate_hourly(history,4); d1=aggregate_hourly(history,24)
        c=[x[4] for x in h4]
        e10,e20,e55=ema(c,10),ema(c,20),ema(c,55)
        ma50,ma100=sma(c,50),sma(c,100)
        rr=rsi(c); aa=atr(h4)
        s4=structure(h4); s1=structure(d1)
        score=5.0; reasons=[]
        # Directional trend alignment.
        if e10 and e20 and e55:
            if e10>e20>e55: score+=1.2; reasons.append('EMA bullish')
            elif e10<e20<e55: score-=1.2; reasons.append('EMA bearish')
        if ma50 and ma100:
            if ma50>ma100: score+=0.6; reasons.append('MA50 > MA100')
            elif ma50<ma100: score-=0.6; reasons.append('MA50 < MA100')

        # 4H/1D structure adds directional confirmation instead of being display-only.
        if s4.get('trend')=='Rising': score+=0.6; reasons.append('4H structure rising')
        elif s4.get('trend')=='Falling': score-=0.6; reasons.append('4H structure falling')
        if s1.get('trend')=='Rising': score+=0.4; reasons.append('1D structure rising')
        elif s1.get('trend')=='Falling': score-=0.4; reasons.append('1D structure falling')

        # Momentum / extension: strong momentum can help, but extreme RSI is penalized
        # so the engine does not equate overextension with a fresh trade entry.
        ch=m.get('change',0)
        if ch>3: score+=0.4; reasons.append('positive 24h momentum')
        elif ch<-3: score-=0.4; reasons.append('negative 24h momentum')
        extension='NORMAL'
        if rr is not None:
            if rr>90:
                score-=0.8; extension='EXTREME_HIGH'; reasons.append('RSI extreme high')
            elif rr>80:
                score-=0.5; extension='HIGH'; reasons.append('RSI extended high')
            elif rr>70:
                score+=0.1; extension='ELEVATED_HIGH'; reasons.append('RSI elevated')
            elif 55<=rr<=70:
                score+=0.5; reasons.append('RSI bullish zone')
            elif rr<20:
                score+=0.8; extension='EXTREME_LOW'; reasons.append('RSI extreme low')
            elif rr<30:
                score+=0.5; extension='LOW'; reasons.append('RSI oversold')
            elif rr<=45:
                score-=0.5; reasons.append('RSI weak')

        oi=m.get('oiDelta')
        if oi is not None:
            if oi>5 and ch>0: score+=0.5; reasons.append('OI expansion with price')
            elif oi>5 and ch<0: score-=0.5; reasons.append('OI expansion against price')
            elif oi<-5 and ch<0: score+=0.2; reasons.append('OI contraction')
            elif oi<-5 and ch>0: score-=0.1; reasons.append('OI contraction')
        vr=m.get('volRatio') or 0
        if vr>=1.75: score+=0.6; reasons.append(f'{vr:.1f}x 1H volume spike')
        elif vr>=1.25: score+=0.25; reasons.append('elevated 1H volume')

        score=max(0,min(10,score))
        signal='LONG' if score>=7 else 'SHORT' if score<=4 else 'NEUTRAL'
        # Directional signal and trade setup are separate. A LONG/SHORT bias
        # does not mean an entry is available at the current price.
        base_result={
            'symbol':symbol,'score':round(score,2),'signal':signal,
            'rsi':rr,'ema10':e10,'ema20':e20,'ema55':e55,
            'ma50':ma50,'ma100':ma100,'atr':aa,
            'structure4h':s4,'structure1d':s1,'trend':s4['trend'],
            'reasons':reasons,'historySource':m.get('historySource','OKX'),'extension':extension,
            'watchTier':m.get('watchTier','OPPORTUNITY_SCAN')
        }
        setup=build_trade_setup(m,base_result,history)
        if setup:
            base_result['setup']=setup
            # A setup can be a valid potential trade while still waiting for its entry zone.
            if setup['entryTrigger']=='CONFIRMED':
                base_result['tradeState']='READY_'+setup['direction']
                base_result['setupStatus']='READY_CONFIRMED'
            elif setup['entryStatus']=='INSIDE_ENTRY_ZONE':
                base_result['tradeState']='POTENTIAL_'+setup['direction']
                base_result['setupStatus']='IN_ZONE_WAIT_CONFIRMATION'
            else:
                base_result['tradeState']='POTENTIAL_'+setup['direction']
                base_result['setupStatus']='WAITING_ENTRY'
        else:
            base_result['tradeState']='WATCH' if signal!='NEUTRAL' else 'NEUTRAL'
            base_result['setupStatus']='NO_VALID_SETUP'
        return base_result
    except Exception as e:
        return {'symbol':symbol,'error':str(e)}


def volume_spike(history):
    if len(history)<22: return None
    vols=[float(x[5]) for x in history]
    cur=vols[-2]
    base=statistics.median(vols[-22:-2])
    if base<=0: return None
    ratio=cur/base
    label=('EXTREME' if ratio>=4 else 'MAJOR' if ratio>=2.5 else
           'SPIKE' if ratio>=1.75 else 'ELEVATED' if ratio>=1.25 else 'NORMAL')
    return {'vol1h':cur,'volBaseline':base,'volRatio':ratio,'spike':label}

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
by_core_id={str(x.get('coinId')):x for x in rows}
core_ids={'BTC':'1','ETH':'1027','XRP':'52','SOL':'5426'}
# Candidate pool is broad; core assets are monitored separately and do not consume
# opportunity-selection logic. Duplicate tickers are handled by coinId.
vol_rank=sorted(rows,key=lambda x:x.get('volume') or 0,reverse=True)[:60]
deriv_rank=sorted([x for x in rows if x.get('derivVolume')],key=lambda x:x.get('derivVolume') or 0,reverse=True)[:60]
candidates={x['assetKey'] for x in vol_rank+deriv_rank}
for cid in core_ids.values():
    if cid in by_core_id: candidates.add(by_core_id[cid]['assetKey'])

# Stage B has two separate purposes:
# 1) Core Watch: always analyze BTC/ETH/XRP/SOL when history is available.
# 2) Opportunity Scan: analyze the top 8 non-core Stage-A candidates.
# A core asset therefore gets analysis coverage even when it is not tradeable,
# but it is never forced into Potential Trades.
ranked_all=sorted([by_asset[s] for s in candidates],key=lambda x:(x.get('derivVolume') or 0,x.get('volume') or 0),reverse=True)
core_ranked=[]
for cid in core_ids.values():
    if cid in by_core_id: core_ranked.append(by_core_id[cid])
core_keys={x['assetKey'] for x in core_ranked}
opportunity_ranked=[x for x in ranked_all if x['assetKey'] not in core_keys]
ranked=core_ranked[:4] + opportunity_ranked[:8]
for x in core_ranked[:4]: x['watchTier']='CORE_WATCH'
for x in opportunity_ranked[:8]: x['watchTier']='OPPORTUNITY_SCAN'

histories={}
history_stats={'requested':0,'cacheHits':0,'freshFetches':0,'errors':0}
# Fetch sequentially. OKX public market endpoints are IP-rate-limited; pacing and caching
# keep the GitHub Action well below the documented request limits.
for x in ranked:
    if not x.get('coinId'): continue
    history_stats['requested']+=1
    try:
        h,cache_hit,hsrc=fetch_coin_history(x['coinId'],x.get('baseSymbol'))
        histories[x['assetKey']]=h
        x['historySource']=hsrc
        x['historyMarketType']='FUTURES' if 'SWAP' in str(hsrc).upper() else 'SPOT_FALLBACK'
        history_stats['cacheHits']+=1 if cache_hit else 0
        history_stats['freshFetches']+=0 if cache_hit else 1
        sp=volume_spike(h)
        if sp:
            sp['spikeSource']=hsrc+' 1H market volume'
            x.update(sp)
        if not cache_hit: time.sleep(0.5)
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

# Persistent setup lifecycle. This preserves state across 15-minute GitHub Action runs.
STATE_FILE=DATA/'trade_state.json'
try:
    prior_state=json.loads(STATE_FILE.read_text()).get('setups',{})
except Exception:
    prior_state={}
now_iso=NOW()
current_state={}
for a in analysis:
    key=a.get('assetKey')
    setup=a.get('setup')
    if not key or not setup: continue
    old=prior_state.get(key,{})
    state=old.get('lifecycle','WAITING_ENTRY')
    setup_status=a.get('setupStatus')
    if setup_status=='READY_CONFIRMED':
        state='CONFIRMED'
    elif setup_status=='IN_ZONE_WAIT_CONFIRMATION':
        state='ZONE_TOUCHED'
    elif setup_status=='WAITING_ENTRY':
        state='WAITING_ENTRY'
    current_state[key]={
        'assetKey':key,'symbol':a.get('symbol'),'direction':setup.get('direction'),
        'lifecycle':state,'createdAt':old.get('createdAt',now_iso),
        'lastSeenAt':now_iso,'entryLow':setup.get('entryLow'),'entryHigh':setup.get('entryHigh'),
        'stopLoss':setup.get('stopLoss'),'tp1':setup.get('tp1'),'tp2':setup.get('tp2'),
        'riskPct':setup.get('riskPct'),'stopDistanceATR':setup.get('stopDistanceATR'),
        'setupStatus':setup_status,'confirmationStatus':(setup.get('confirmation') or {}).get('status')
    }
( DATA/'trade_state.json').write_text(json.dumps({'generated_at':now_iso,'setups':current_state},separators=(',',':')))


# Make sure every core asset has an analysis if its history succeeded.
ctx['engine_status']='OK' if rows else 'NO_MARKET_DATA'
ctx['context_status']='COMPLETE' if all(k in ctx for k in ('fng','altseason','btcDom','totalMarketCap','btcCap','ethCap','total3','total3Btc')) else 'PARTIAL'
ctx['marketSource']='CoinMarketCap listings'
ctx['derivativesSource']='CoinGecko aggregated derivatives'
ctx['volumeSource']='OKX 1H candles (futures-first, spot fallback)'
ctx['volumeSpikeTimeframe']='1H'
ctx['assetUniverse']='CMC listings excluding obvious tokenized-stock assets; unique identity = coinId'
ctx['stageBHistoryLimit']=12
ctx['potentialTradeDefinition']='Directional signal + aligned 4H/1D structure + non-extreme extension + defined entry zone/SL/TP; setup may remain WAITING_ENTRY'
ctx['coreWatchAssets']=['BTC','ETH','XRP','SOL']
ctx['stageBOpportunitySlots']=8
ctx['tradeSelectionRule']='Core assets are always monitored; potential trades require confluence plus a defined EMA pullback/retest setup'
ctx['setupEngine']='EMA10-EMA20 pullback/retest with 4H structural invalidation; TP1=1.5R, TP2=2.5R; zone touch is insufficient; READY requires meaningful completed 1H reaction; risk guardrails reject >8% stop distance or >3.5 ATR'
ctx['setupRiskGuardrails']={'maxRiskPct':8.0,'maxStopDistanceATR':3.5,'tp1R':1.5,'tp2R':2.5}
ctx['setupLifecycle']='WAITING_ENTRY → ZONE_TOUCHED → CONFIRMED → ACTIVE → TP1_HIT → TP2_HIT/SL_HIT → CLOSED; alternative exits EXPIRED/INVALIDATED/CANCELLED'
ctx['oiDeltaDefinition']='snapshot-to-snapshot change versus previous market.json run, not 24h'
ctx['derivativesMapping']='Only unique CMC ticker symbols are auto-enriched to avoid same-symbol collisions'
ctx['historyProvider']='OKX 1H candles (futures-first, spot fallback; serialized + 1h cache)'
ctx['historyCacheTTLSeconds']=HISTORY_TTL
ctx['historyFetchPacingSeconds']=0.5
ctx['historyStats']=history_stats
ctx['generated_at']=NOW()

(DATA/'market.json').write_text(json.dumps({'generated_at':NOW(),'source':'CMC + CoinGecko','count':len(rows),'symbols':rows,'engine_status':'OK' if rows else 'NO_MARKET_DATA'},separators=(',',':')))
(DATA/'analysis.json').write_text(json.dumps({'generated_at':NOW(),'source':'OKX hourly → 4H/1D + setup engine','count':len(analysis),'analysis':analysis},separators=(',',':')))
(DATA/'context.json').write_text(json.dumps(ctx,separators=(',',':')))

print(f'Generated {len(rows)} broad market rows, {len(analysis)} Stage-B analyses')
print('Setup confirmation: meaningful completed 1H reaction required before READY')
print(f'Persistent setup states: {len(current_state)}')
print('Market source: CoinMarketCap listings')
print('Derivatives source: CoinGecko aggregated derivatives' if derivs else 'Derivatives source: unavailable')
print('Volume spike source: OKX 1H candles; timeframe=1H')
print('Context:',{k:ctx.get(k) for k in ('fng','fngLabel','altseason','btcDom','totalMarketCap','total3','total3Btc')})
