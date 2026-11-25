#!/usr/bin/env python3
"""MCX S3 research scanner and full-snapshot recorder. No order endpoints."""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, time as walltime
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import time
from zoneinfo import ZoneInfo
import requests
from mcx_recorder import Recorder

IST = ZoneInfo('Asia/Kolkata')
MASTER_URL = 'https://assets.upstox.com/market-quote/instruments/exchange/MCX.json.gz'
VERSION = 'MCX_S3_RESEARCH_V1'
FETCH_VERSION = 'ATM_FIRST_REFRESH_V5'
ALERT_POLICY = 'MCX_OPTIONS_BUY_GAP2_5_PEERMID_ATM4_V4'
ALERT_FILTERS = {'min_gap_pct':2., 'max_gap_pct':5., 'max_spread_pct':2., 'min_oi_raw':500.,
                 'min_volume_raw':100., 'max_trade_age_seconds':30., 'max_atm_steps':4}


def now(): return datetime.now(IST)
def iso(t): return t.isoformat()
def number(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError): return None

def timestamp(x):
    try:
        n = number(x)
        if n is not None: return datetime.fromtimestamp(n/1000 if n > 1e11 else n, IST)
        return datetime.fromisoformat(str(x).replace('Z', '+00:00')).astimezone(IST)
    except (ValueError, TypeError, OverflowError, OSError): return None

def price_text(value):
    return f'{value:.2f}'.rstrip('0').rstrip('.')

def normal_cdf(x): return (1 + math.erf(x / math.sqrt(2))) / 2

def black76(f, k, years, vol, side, rate=.06):
    if min(f, k, years, vol) <= 0: raise ValueError('Nonpositive model input')
    d1 = (math.log(f/k) + .5*vol*vol*years)/(vol*math.sqrt(years))
    d2 = d1-vol*math.sqrt(years); discount = math.exp(-rate*years)
    if side == 'CE': return discount*(f*normal_cdf(d1)-k*normal_cdf(d2))
    return discount*(k*normal_cdf(-d2)-f*normal_cdf(-d1))

def implied_vol(price, f, k, years, side, rate=.06):
    if min(price, f, k, years) <= 0: return None
    discount = math.exp(-rate*years)
    intrinsic = discount*max((f-k) if side == 'CE' else (k-f), 0)
    upper = discount*(f if side == 'CE' else k)
    if price <= intrinsic or price >= upper: return None
    lo, hi = .000001, 10.
    if black76(f, k, years, hi, side, rate) < price: return None
    for _ in range(65):
        mid = (lo+hi)/2
        if black76(f, k, years, mid, side, rate) < price: lo = mid
        else: hi = mid
    return (lo+hi)/2

def greeks(f, k, years, vol, side, rate=.06):
    discount = math.exp(-rate*years); root = math.sqrt(years)
    d1 = (math.log(f/k)+.5*vol*vol*years)/(vol*root)
    pdf = math.exp(-d1*d1/2)/math.sqrt(2*math.pi)
    value = black76(f, k, years, vol, side, rate)
    return {'iv_pct': vol*100,
            'delta': discount*(normal_cdf(d1) if side == 'CE' else normal_cdf(d1)-1),
            'gamma': discount*pdf/(f*vol*root),
            'vega_per_1pct': discount*f*pdf*root/100,
            'theta_per_day': (rate*value-discount*f*pdf*vol/(2*root))/365,
            'rho_per_1pct': -years*value/100}


def close_time(day):
    custom = os.getenv('MCX_CLOSE')
    if custom: return walltime.fromisoformat(custom)
    # Published non-agri schedule; use MCX_CLOSE for future revisions/special sessions.
    return walltime(23, 55) if '2026-11-02' <= day <= '2027-03-12' else walltime(23, 30)

def session_close(t):
    return datetime.combine(t.date(), close_time(t.date().isoformat()), IST)

def expiry_at(row):
    dt = timestamp(row.get('expiry'))
    if not dt: return None
    overrides = json.loads(os.getenv('EXPIRY_TIMES_JSON', '{}'))
    expiry_time = walltime.fromisoformat(overrides[row['underlying_symbol']]) if row['underlying_symbol'] in overrides else close_time(dt.date().isoformat())
    return datetime.combine(dt.date(), expiry_time, IST)


def discover(master, t, symbols):
    futures = {r['instrument_key']: r for r in master if r.get('instrument_type') == 'FUT'}
    groups = defaultdict(list); rejected = Counter()
    for r in master:
        if r.get('segment') != 'MCX_FO' or r.get('instrument_type') not in ('CE','PE'): continue
        if symbols and r.get('underlying_symbol') not in symbols: continue
        end = expiry_at(r)
        if not end or end <= t: continue
        if r.get('underlying_key') not in futures:
            rejected['unmapped_future'] += 1; continue
        if not number(r.get('strike_price')) or number(r['strike_price']) <= 0:
            rejected['invalid_strike'] += 1; continue
        groups[r['underlying_symbol']].append(r)
    selected = []
    for symbol, contracts in sorted(groups.items()):
        end = min(expiry_at(r) for r in contracts)
        selected.extend(r for r in contracts if expiry_at(r) == end)
    # Exact underlying_key is required; never substitute a similarly named/nearest future.
    used = {r['underlying_key']: futures[r['underlying_key']] for r in selected}
    return selected, used, dict(rejected)


class API:
    def __init__(self):
        token = os.getenv('UPSTOX_ACCESS_TOKEN', '')
        if not token: raise RuntimeError('UPSTOX_ACCESS_TOKEN is missing')
        self.session = requests.Session()
        self.session.headers.update({'Authorization': 'Bearer '+token, 'Accept':'application/json'})
        self.last = 0.

    def get(self, path, keys=None):
        params = {'instrument_key': ','.join(keys)} if keys else None
        for attempt in range(4):
            time.sleep(max(0, .4-(time.monotonic()-self.last)))
            try:
                response = self.session.get('https://api.upstox.com'+path, params=params, timeout=(10,30))
            except requests.RequestException:
                if attempt == 3: raise RuntimeError('Market API network failure') from None
                time.sleep(2**attempt); continue
            finally: self.last = time.monotonic()
            if response.status_code == 401: raise PermissionError('Upstox token expired/unauthorized; renew repository secret')
            if response.status_code == 429 or response.status_code >= 500:
                time.sleep(min(20, 2**attempt)); continue
            if not response.ok: raise RuntimeError(f'Market API HTTP {response.status_code} ({path})')
            payload = response.json()
            if payload.get('status') != 'success': raise RuntimeError('Market API unsuccessful response')
            return payload.get('data', {})
        raise RuntimeError('Market API retries exhausted')

    def quotes(self, keys, greek=False):
        path = '/v3/market-quote/option-greek' if greek else '/v2/market-quote/quotes'
        result = {}
        batch_size=50 if greek else 100
        for i in range(0, len(keys), batch_size):
            data = self.get(path, keys[i:i+batch_size]); received = now()
            for key, value in data.items():
                token = value.get('instrument_token')
                if token: result[token] = (value, received)
                elif key in keys: result[key] = (value, received)
        return result

    def master(self):
        try:
            response = requests.get(MASTER_URL, timeout=40)
            response.raise_for_status()
            return json.loads(gzip.decompress(response.content))
        except (requests.RequestException, ValueError, OSError):
            raise RuntimeError('Cannot download MCX instrument master') from None


def snapshot(row, quote, observed, future=None, broker=None):
    q = quote or {}; depth = q.get('depth') or {}
    bids, asks = depth.get('buy') or [], depth.get('sell') or []
    bid = number(bids[0].get('price')) if bids else None
    ask = number(asks[0].get('price')) if asks else None
    traded = timestamp(q.get('last_trade_time')); updated = timestamp(q.get('timestamp'))
    ltp = number(q.get('last_price')); oi = number(q.get('oi')); volume = number(q.get('volume'))
    age = (observed-traded).total_seconds() if traded else None
    flags = []
    if not q: flags.append('missing_quote')
    if not traded: flags.append('missing_trade_timestamp')
    elif traded.date() != observed.date(): flags.append('previous_day_trade')
    elif age < 0: flags.append('future_trade_timestamp')
    if age is not None and age > 180: flags.append('old_last_trade')
    if bid is None or ask is None or min(bid,ask) <= 0: flags.append('missing_book')
    elif bid > ask: flags.append('crossed_book')
    broker_raw, broker_at = broker if broker else ({}, None)
    return {'schema':1, 'version':VERSION, 'kind':'option', 'observed_at':iso(observed),
            'instrument_key':row['instrument_key'], 'symbol':row['underlying_symbol'],
            'trading_symbol':row.get('trading_symbol'), 'expiry_at':iso(expiry_at(row)),
            'underlying_key':row['underlying_key'], 'strike':number(row['strike_price']),
            'side':row['instrument_type'], 'lot_size':number(row.get('lot_size')),
            'qty_multiplier':number(row.get('qty_multiplier')), 'tick_size_raw':row.get('tick_size'),
            'ltp':ltp, 'oi_raw':oi, 'volume_raw':volume,
            'quantity_unit':'broker_raw_unverified_MCX_units',
            'volume_oi': volume/oi if volume is not None and oi and oi > 0 else None,
            'bid':bid, 'ask':ask,
            'bid_qty_raw':number(bids[0].get('quantity')) if bids else None,
            'ask_qty_raw':number(asks[0].get('quantity')) if asks else None,
            'spread':ask-bid if bid is not None and ask is not None else None,
            'spread_pct_mid':100*(ask-bid)/((ask+bid)/2) if bid and ask and ask+bid>0 else None,
            'last_trade_at':iso(traded) if traded else None, 'trade_age_seconds':age,
            'quote_updated_at':iso(updated) if updated else None,
            'quote_age_seconds':(observed-updated).total_seconds() if updated else None,
            'future_quote_raw':future[0] if future else None,
            'future_observed_at':iso(future[1]) if future else None,
            'broker_greeks':broker_raw or None,
            'broker_greeks_observed_at':iso(broker_at) if broker_at else None,
            'raw_quote':q, 'quality_flags':flags, 'model_target':None,
            'gap_pct':None, 'ask_gap_pct':None, 'peer_iv_pct':None,
            'model_greeks':None, 'model_status':'not_computed'}


def evaluate(rows, t, rate=.06):
    """Record own LTP IV; price targets only from liquid, fresh adjacent peer midpoints."""
    groups = defaultdict(list)
    for r in rows:
        r.update(model_target=None, peer_iv_pct=None, gap_pct=None, ask_gap_pct=None, peer_keys=[], peer_input_checks=[])
        fq = r.get('future_quote_raw') or {}; f = number(fq.get('last_price'))
        ft = timestamp(fq.get('last_trade_time')); ot = timestamp(r.get('last_trade_at'))
        end = timestamp(r['expiry_at']); years = (end-t).total_seconds()/(365*86400)
        r.update(futures_price=f, years=years, risk_free_rate=rate)
        # Model cannot use a different-day/nonexistent future or option trade.
        if not ft or not ot or ft.date()!=t.date() or ot.date()!=t.date() or ft>t or ot>t:
            r['model_status']='no_same_day_trade_inputs'; continue
        if not f or f<=0 or not r.get('ltp') or r['ltp']<=0 or years<=0:
            r['model_status']='invalid_model_inputs'; continue
        own = implied_vol(r['ltp'], f, r['strike'], years, r['side'], rate)
        r['own_iv_pct'] = own*100 if own else None
        if own:
            r['model_greeks'] = greeks(f,r['strike'],years,own,r['side'],rate)
            groups[(r['underlying_key'],r['expiry_at'],r['side'])].append(r)
        else: r['model_status']='iv_not_solvable'
    for group in groups.values():
        group.sort(key=lambda r:r['strike'])
        for index, r in enumerate(group):
            if index==0 or index==len(group)-1:
                r['model_status']='missing_bracketing_peer_iv'; continue
            left, right = group[index-1], group[index+1]
            checks=[]; peer_vols=[]
            for peer in (left,right):
                reasons=liquidity_reasons(peer)
                bid=number(peer.get('bid')); ask=number(peer.get('ask'))
                mid=(bid+ask)/2 if bid and ask and bid<=ask else None
                iv=implied_vol(mid,r['futures_price'],peer['strike'],r['years'],r['side'],rate) if mid else None
                if iv is None: reasons.append('midpoint_iv_not_solvable')
                checks.append({'instrument_key':peer['instrument_key'],'reasons':reasons,'midpoint':mid,'midpoint_iv_pct':iv*100 if iv else None})
                peer_vols.append(iv)
            r['peer_input_checks']=checks
            r['peer_keys']=[left['instrument_key'],right['instrument_key']]
            if any(c['reasons'] for c in checks):
                r['model_status']='peer_liquidity_or_freshness_failed'; continue
            weight = (r['strike']-left['strike'])/(right['strike']-left['strike'])
            vol = peer_vols[0]*(1-weight)+peer_vols[1]*weight
            fair = black76(r['futures_price'],r['strike'],r['years'],vol,r['side'],rate)
            r.update(model_target=fair, peer_iv_pct=vol*100, model_status='computed',
                     peer_keys=[left['instrument_key'],right['instrument_key']],
                     gap_pct=100*(fair-r['ltp'])/fair if fair>0 else None,
                     ask_gap_pct=100*(fair-r['ask'])/fair if fair>0 and r.get('ask') else None)
    return rows


def annotate_atm(rows):
    """Use all listed strikes, including unquoted strikes; same future and option expiry."""
    groups=defaultdict(list)
    for row in rows: groups[(row['underlying_key'],row['expiry_at'])].append(row)
    for group in groups.values():
        strikes=sorted({r['strike'] for r in group if number(r.get('strike')) is not None})
        futures_price=next((r['futures_price'] for r in group if number(r.get('futures_price')) is not None and r['futures_price']>0),None)
        atm=min(strikes,key=lambda strike:(abs(strike-futures_price),-strike)) if strikes and futures_price else None
        indices={strike:i for i,strike in enumerate(strikes)}
        for row in group:
            row['atm_strike']=atm
            row['strike_steps_from_atm']=indices[row['strike']]-indices[atm] if atm is not None else None
    return rows


def liquidity_reasons(r):
    reasons=[]
    ask=number(r.get('ask'));bid=number(r.get('bid'))
    if bid is None or ask is None or min(bid,ask)<=0:
        reasons.append('missing_positive_book')
    elif bid>ask:reasons.append('crossed_book')
    else:
        spread=100*(ask-bid)/((ask+bid)/2)
        if spread>ALERT_FILTERS['max_spread_pct']+1e-12:reasons.append('spread_above_2pct')
    for field,minimum,label in [('oi_raw',ALERT_FILTERS['min_oi_raw'],'oi_below_500'),
                                ('volume_raw',ALERT_FILTERS['min_volume_raw'],'volume_below_100')]:
        value=number(r.get(field))
        if value is None or value<minimum:reasons.append(label)
    age=number(r.get('trade_age_seconds'))
    if age is None or not 0<=age<=ALERT_FILTERS['max_trade_age_seconds']:
        reasons.append('trade_age_above_30s_or_unknown')
    for field in ('bid_qty_raw','ask_qty_raw'):
        quantity=number(r.get(field))
        if quantity is None or quantity<=0:reasons.append('missing_'+field)
    return reasons


def alert_reasons(r):
    reasons=liquidity_reasons(r)+r.get('input_freshness_reasons',[])
    target=number(r.get('model_target'));ltp=number(r.get('ltp'));ask=number(r.get('ask'))
    if r.get('model_status')!='computed' or not target or target<=0:
        reasons.append('model_unavailable')
    if ltp is None or ltp<=0:reasons.append('invalid_ltp')
    if target and target>0:
        for label,value in [('ltp',ltp),('ask',ask)]:
            if value is None:continue
            gap=100*(target-value)/target
            if gap<ALERT_FILTERS['min_gap_pct']-1e-12:reasons.append(label+'_gap_below_2pct')
            if gap>ALERT_FILTERS['max_gap_pct']+1e-12:reasons.append(label+'_gap_above_5pct')
    steps=number(r.get('strike_steps_from_atm'))
    if steps is None or abs(steps)>ALERT_FILTERS['max_atm_steps']:
        reasons.append('outside_atm_plus_minus_4_or_unknown')
    return reasons


def alert_eligible(r):
    return not alert_reasons(r)


def annotate_from_master(rows, contracts):
    """ATM distances always use the entire listed strike ladder, not the fetched subset."""
    ladders=defaultdict(set)
    for c in contracts:
        ladders[(c['underlying_key'],iso(expiry_at(c)))].add(number(c['strike_price']))
    for r in rows:
        strikes=sorted(k for k in ladders[(r['underlying_key'],r['expiry_at'])] if k is not None)
        f=number(r.get('futures_price'))
        atm=min(strikes,key=lambda k:(abs(k-f),-k)) if strikes and f and f>0 else None
        r['atm_strike']=atm
        r['strike_steps_from_atm']=strikes.index(r['strike'])-strikes.index(atm) if atm is not None else None
    return rows


def priority_contracts(contracts, futures):
    """Return ATM +/-4 and one listed neighbour beyond each edge, grouped by future/expiry."""
    groups=defaultdict(list)
    for c in contracts:groups[(c['underlying_key'],iso(expiry_at(c)))].append(c)
    result=[]
    for (key,_),group in sorted(groups.items()):
        f=number((futures.get(key) or ({},None))[0].get('last_price'))
        if not f or f<=0:continue
        strikes=sorted({number(c['strike_price']) for c in group})
        atm=min(range(len(strikes)),key=lambda i:(abs(strikes[i]-f),-strikes[i]))
        chosen=set(strikes[max(0,atm-5):atm+6])
        result.append([c for c in group if number(c['strike_price']) in chosen])
    return result


def freshness_reasons(r, t):
    """Independent freshness diagnostics; a new HTTP response does not make an old trade fresh."""
    reasons=[]
    fields=[('candidate_trade',r.get('last_trade_at')),('candidate_quote',r.get('quote_updated_at')),
            ('candidate_received',r.get('quote_received_at',r.get('observed_at'))),
            ('future_trade',(r.get('future_quote_raw') or {}).get('last_trade_time')),
            ('future_quote',(r.get('future_quote_raw') or {}).get('timestamp')),
            ('future_received',r.get('future_observed_at'))]
    for label,value in fields:
        at=timestamp(value)
        if not at or not 0<=(t-at).total_seconds()<=30:reasons.append(label+'_stale_or_unknown')
    for check in r.get('peer_input_checks',[]):
        if 'trade_age_above_30s_or_unknown' in check.get('reasons',[]):reasons.append('peer_trade_stale_or_unknown')
    return sorted(set(reasons))


def diagnostic_bucket(r):
    if r.get('input_freshness_reasons'):return 'blocked_stale_inputs'
    if r.get('model_status')=='computed' and not liquidity_reasons(r):
        gaps=[number(r.get(k)) for k in ('gap_pct','ask_gap_pct')]
        if all(g is not None for g in gaps) and min(gaps)<ALERT_FILTERS['min_gap_pct']-1e-12:
            return 'valid_pricing_gap_too_small'
        if all(g is not None for g in gaps) and max(gaps)>ALERT_FILTERS['max_gap_pct']+1e-12:
            return 'valid_pricing_gap_too_large'
        if alert_eligible(r):return 'eligible_before_refresh'
    return 'other_input_or_liquidity_block'


class Scanner:
    def __init__(self, api, root, telegram=True):
        self.api = api; self.root = Path(root); self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root/'state.json'; self.recorder = Recorder(self.root/'snapshots')
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {'version':VERSION,'days':{},'watches':[],'outbox':[]}
        if self.state.get('version') != VERSION: raise RuntimeError('Wrong state directory; keep NSE and MCX separate')
        if self.state.get('alert_policy')!=ALERT_POLICY:
            for message in self.state['outbox']:
                if not message.get('sent') and message['id'].startswith(('entry-','hit-')):
                    message['suppressed']='superseded_alert_policy'
            self.state['alert_policy']=ALERT_POLICY
        self.state['alert_filters']=ALERT_FILTERS
        self.telegram = telegram; self.master_day = None; self.options = []; self.futures = {}
        self.counts = Counter(); self.broker_greeks_available = True; self.last_send = 0.

    def save(self):
        temp=self.path.with_suffix('.tmp'); temp.write_text(json.dumps(self.state,allow_nan=False)); temp.replace(self.path)

    def emit(self, event, message):
        if not self.telegram: return
        for index in range(0,len(message),3500):
            id_ = event+':'+str(index//3500)
            if not any(x['id']==id_ for x in self.state['outbox']):
                self.state['outbox'].append({'id':id_,'text':message[index:index+3500],'sent':False})
        self.save()

    def flush(self):
        if not self.telegram: return
        token=os.getenv('TELEGRAM_BOT_TOKEN'); chat=os.getenv('TELEGRAM_CHAT_ID')
        if not token or not chat: raise RuntimeError('Telegram secrets missing; use record mode for no Telegram')
        sent=0
        pending=sorted((m for m in self.state['outbox'] if not m['sent'] and not m.get('suppressed')),
                       key=lambda m: 0 if m['id'].startswith(('report-','test-','online-')) else 1 if m['id'].startswith('hit-') else 2)
        for m in pending:
            if m['id'].startswith('entry-') and not m.get('refresh_seed'):
                m['suppressed']='missing_refresh_context_after_upgrade'
                self.save();continue
            time.sleep(max(0,3.2-(time.monotonic()-self.last_send)))
            try:
                if m['id'].startswith('entry-') and m.get('watch_id'):
                    w=next((w for w in self.state['watches'] if w['id']==m['watch_id']),None)
                    seed=m.get('refresh_seed')
                    checked=self.refresh_candidate(seed) if seed else None
                    if not checked or not alert_eligible(checked) or now()>session_close(now()):
                        m['suppressed']='final_refresh_rejected'
                        if w:w['status']='ENTRY_RECHECK_REJECTED'
                        self.counts['final_refresh_rejected']+=1
                        if checked:
                            self.counts['diagnostic_'+diagnostic_bucket(checked)]+=1
                        day=self.state['days'].get(now().date().isoformat())
                        if day is not None:day['counts']=dict(self.counts)
                        self.save();continue
                    w.update(entered=checked['observed_at'],entry=checked['ltp'],buy_reference=checked['ask'],
                             target=checked['model_target'],gap_pct=checked['gap_pct'],ask_gap_pct=checked['ask_gap_pct'])
                    m['text']=self.entry_message(checked)
                    self.counts['final_refresh_passed']+=1
                response=requests.post('https://api.telegram.org/bot'+token+'/sendMessage',
                                       json={'chat_id':chat,'text':m['text']},timeout=20)
                self.last_send=time.monotonic()
                if response.status_code==429: print('Telegram rate limit; message remains queued',flush=True); return
                if not response.ok or not response.json().get('ok'): raise RuntimeError('Telegram delivery failed; queue retained')
            except requests.RequestException: raise RuntimeError('Telegram network failure; queue retained') from None
            m['sent']=True
            if m.get('watch_id'):
                for w in self.state['watches']:
                    if w['id']==m['watch_id']:w['entry_confirmed']=True
            self.state['days'].get(now().date().isoformat(),{}).update(counts=dict(self.counts))
            self.save(); sent+=1
            if sent>=5: break

    def prepare(self,t):
        day=t.date().isoformat()
        if self.master_day!=day:
            master=self.api.master(); symbols={x.strip().upper() for x in os.getenv('MCX_SYMBOLS','CRUDEOIL,NATURALGAS,GOLD,SILVER').split(',') if x.strip()}
            self.options,self.futures,skipped=discover(master,t,symbols)
            if not self.options: raise RuntimeError('No mapped active MCX options discovered')
            with gzip.open(self.root/('master-'+day+'.json.gz'),'wt') as h:json.dump(master,h)
            self.master_day=day
            d=self.state['days'].setdefault(day,{'date':day,'sweeps':0,'counts':{},'reported':False})
            self.counts=Counter(d.get('counts',{}))
            d.update(alert_policy=ALERT_POLICY,alert_filters=ALERT_FILTERS,option_count=len(self.options),future_count=len(self.futures),discovery_skips=skipped)
            self.emit('online-'+ALERT_POLICY+'-'+day,f'MCX S3 ONLINE | {day}\nOption buying only; 2–5% gap at both LTP and ask; targets use fresh, liquid neighbour bid–ask midpoint IV; ATM ±4 only. Spread <=2%; OI >=500; volume >=100 (broker raw units); last trade <=30s. Recording all strikes, nearest expiry, '+', '.join(sorted({r['underlying_symbol'] for r in self.options}))+f'.\n{len(self.options)} options; raw snapshot recording ON.\n2–5% option-gap watches once per contract per day; all snapshots retained. Close {close_time(day):%H:%M} IST.')
        return self.state['days'][day]

    def report(self,day):
        d=self.state['days'][day]
        if d.get('reported'): return
        watches=[w for w in self.state['watches'] if w['day']==day]
        for w in watches:
            if w['status']=='OPEN':w['status']='NO_TOUCH_OBSERVED'
        cohort=[w for w in watches if w.get('policy')==ALERT_POLICY and w.get('entry_confirmed',True)]
        counts=Counter(w['status'] for w in cohort)
        report={'day':d,'watches':watches,'outcomes':dict(counts),'alert_policy':ALERT_POLICY,'new_policy_watches':len(cohort),'legacy_watches':len(watches)-len(cohort),
                'evidence':'Same-day post-entry polled LTP only. No full-tick or complete candle coverage claimed.'}
        folder=self.root/'reports';folder.mkdir(exist_ok=True)
        (folder/(day+'.json')).write_text(json.dumps(report,indent=2))
        text=f'MCX S3 DAILY | {day}\nSweeps {d["sweeps"]}; options {d["option_count"]}\n2–5% option watches {len(cohort)}; target observed {counts["TARGET_OBSERVED"]}; no touch observed {counts["NO_TOUCH_OBSERVED"]}.\nAll raw snapshots saved, including contracts without a model/watch.\nPolled observations; use recorded data for filter comparison.\nValid pricing but gap too small: {d.get("counts",{}).get("diagnostic_valid_pricing_gap_too_small",0)}\nBlocked by stale inputs: {d.get("counts",{}).get("diagnostic_blocked_stale_inputs",0)}'
        (folder/(day+'.txt')).write_text(text);self.emit('report-'+day,text)
        d['reported']=True; self.save()

    def process(self,rows,t,day):
        # Finish watches only from actual later trades, never stale pre-entry trades.
        latest={r['instrument_key']:r for r in rows}
        for w in self.state['watches']:
            if w['day']!=day or w['status']!='OPEN' or not w.get('entry_confirmed',True): continue
            r=latest.get(w['key']); trade=timestamp(r.get('last_trade_at')) if r else None
            if not r or not trade or not timestamp(w['entered'])<trade<=t or trade.date()!=t.date():continue
            if not r.get('ltp') or r['ltp']<=0:continue
            w.update(last_price=r['ltp'],last_observed_at=r['observed_at'])
            if r['ltp']>=w['target']:
                w.update(status='TARGET_OBSERVED',hit_at=r['last_trade_at'],hit_price=r['ltp'])
                if w.get('policy')==ALERT_POLICY:
                    self.emit('hit-'+w['id'],f'🎯 TARGET REACHED | {w["symbol"]} {w["strike"]:g} {w["side"]}\nEntry ₹{price_text(w.get("buy_reference",w["entry"]))} | Target ₹{price_text(w["target"])}\nObserved price ₹{price_text(r["ltp"])}\nGap at entry {w.get("ask_gap_pct",w["gap_pct"]):.2f}%\n{r["last_trade_at"]}')
        existing={(w['day'],w['key']) for w in self.state['watches'] if w.get('policy')==ALERT_POLICY and w.get('status')!='ENTRY_RECHECK_REJECTED'}
        for r in rows:
            if not alert_eligible(r):continue
            if (day,r['instrument_key']) in existing:continue
            id_=hashlib.sha256((ALERT_POLICY+':'+day+':'+r['instrument_key']).encode()).hexdigest()[:16]
            w={'id':id_,'policy':ALERT_POLICY,'entry_confirmed':not self.telegram,'buy_reference':r['ask'],'ask_gap_pct':r['ask_gap_pct'],'key':r['instrument_key'],'symbol':r['symbol'],'strike':r['strike'],'side':r['side'],
               'day':day,'entered':r['observed_at'],'entry':r['ltp'],'target':r['model_target'],'status':'OPEN',
               'gap_pct':r['gap_pct'],'atm_strike':r.get('atm_strike'),'strike_steps_from_atm':r.get('strike_steps_from_atm'),'quality_flags':r['quality_flags'],'snapshot_file':str(self.recorder.path.relative_to(self.root)),
               'last_price':r['ltp'],'last_observed_at':r['observed_at']}
            previous=next((old for old in self.state['watches'] if old['id']==id_),None)
            if previous is None:self.state['watches'].append(w)
            else:previous.clear();previous.update(w)
            existing.add((day,r['instrument_key']))
            self.emit('entry-'+id_,self.entry_message(r))
            for m in self.state['outbox']:
                if m['id']=='entry-'+id_+':0':
                    if m.get('suppressed')=='final_refresh_rejected':
                        m.pop('suppressed',None);m['sent']=False
                    m.update(watch_id=id_,refresh_seed={'instrument_key':r['instrument_key'],
                             'underlying_key':r['underlying_key'],'peer_keys':r['peer_keys']})

    @staticmethod
    def entry_message(r):
        option_name='CALL' if r['side']=='CE' else 'PUT'
        return (f'🟢 BUY MCX OPTION | {r["symbol"]} {r["strike"]:g} {r["side"]} ({option_name})\n'
                f'Expiry: {r["expiry_at"][:10]}\nEntry ₹{price_text(r["ask"])} | Target ₹{price_text(r["model_target"])}\n'
                f'Gap below model value at ask: {r["ask_gap_pct"]:.2f}%\nStop-loss: not set\n'
                f'{r["observed_at"][11:19]} IST | Option premium; not a futures trade')

    def build_rows(self, contracts, quotes, futures, evaluated_at, phase, broker=None):
        rows=[]
        for c in contracts:
            q,at=quotes.get(c['instrument_key'],({},evaluated_at))
            r=snapshot(c,q,at,futures.get(c['underlying_key']),(broker or {}).get(c['instrument_key']))
            r.update(quote_received_at=iso(at),observed_at=iso(evaluated_at),evaluated_at=iso(evaluated_at),
                     fetch_phase=phase,fetch_version=FETCH_VERSION)
            if r.get('last_trade_at'):r['trade_age_seconds']=(evaluated_at-timestamp(r['last_trade_at'])).total_seconds()
            rows.append(r)
        evaluate(rows,evaluated_at,float(os.getenv('RISK_FREE_RATE','.06')))
        annotate_from_master(rows,self.options)
        by_key={r['instrument_key']:r for r in rows}
        for r in rows:
            stale=freshness_reasons(r,evaluated_at)
            for key in r.get('peer_keys',[]):
                peer=by_key.get(key)
                if peer and freshness_reasons(peer,evaluated_at):stale.append('peer_quote_or_trade_stale_or_unknown')
            r['input_freshness_reasons']=sorted(set(stale))
            r.update(alert_policy=ALERT_POLICY)
            r['alert_rejection_reasons']=alert_reasons(r)
            r['alert_eligible']=not r['alert_rejection_reasons']
        return rows

    def refresh_candidate(self, seed):
        """One REST quote request for exactly candidate + both chosen peers + exact future."""
        contracts={c['instrument_key']:c for c in self.options}
        peers=seed.get('peer_keys',[])
        keys=[seed['instrument_key']]+peers+[seed['underlying_key']]
        if len(peers)!=2 or len(set(keys))!=4 or any(k not in contracts for k in keys[:3]):return None
        try:quotes=self.api.quotes(keys)
        except PermissionError:raise
        except RuntimeError as exc:
            self.counts['refresh_request_error']+=1
            self.recorder.write({'kind':'error','observed_at':iso(now()),'operation':'alert_refresh','keys':keys,'error':str(exc)})
            return None
        evaluated_at=now()
        if any(k not in quotes for k in keys):
            self.counts['refresh_missing_inputs']+=1
            self.counts['diagnostic_blocked_stale_inputs']+=1
            self.recorder.write({'kind':'error','observed_at':iso(evaluated_at),'operation':'alert_refresh',
                                 'missing_keys':[k for k in keys if k not in quotes]})
            return None
        future={keys[-1]:quotes[keys[-1]]}
        self.record_futures(future,'alert_refresh')
        rows=self.build_rows([contracts[k] for k in keys[:3]],quotes,future,evaluated_at,'alert_refresh')
        for r in rows:self.recorder.write(r)
        return next(r for r in rows if r['instrument_key']==seed['instrument_key'])

    def record_futures(self, futures, phase):
        for key,(q,at) in futures.items():
            self.recorder.write({'kind':'future','instrument_key':key,'observed_at':iso(at),
                                 'raw_quote':q,'fetch_phase':phase,'fetch_version':FETCH_VERSION})

    def sweep(self,t,create_watches=True):
        d=self.prepare(t); start=time.monotonic()
        for old in list(self.state['days']):
            if old<t.date().isoformat() and not self.state['days'][old].get('reported'):self.report(old)
        futures=self.api.quotes(list(self.futures));self.record_futures(futures,'atm_discovery')
        priority=priority_contracts(self.options,futures)
        priority_keys=set();all_quotes={};broker={};priority_rows=[]
        for group in priority:
            # <=22 option quotes + future normally fit one request. No Greeks on this path.
            keys=[c['instrument_key'] for c in group]
            priority_keys.update(keys)
            try:q=self.api.quotes(keys+list({c['underlying_key'] for c in group}))
            except PermissionError:raise
            except RuntimeError as exc:
                self.counts['quote_batch_error']+=1
                self.recorder.write({'kind':'error','observed_at':iso(now()),'operation':'priority_quotes','keys':keys,'error':str(exc)})
                continue
            fresh_futures={k:q[k] for k in self.futures if k in q}
            self.record_futures(fresh_futures,'atm_priority')
            all_quotes.update({k:q[k] for k in keys if k in q})
            at=now();rows=self.build_rows(group,q,fresh_futures,at,'atm_priority')
            priority_rows.extend(rows)
            for r in rows:
                self.recorder.write(r)
                self.counts[r['model_status']]+=1
                steps=r.get('strike_steps_from_atm')
                if steps is not None and abs(steps)<=4:
                    self.counts['diagnostic_'+diagnostic_bucket(r)]+=1
                    for reason in r['alert_rejection_reasons']:self.counts['alert_reject_'+reason]+=1
            if create_watches and at<=session_close(at):
                # Verify all four instruments, then process and deliver before background work.
                verified=[]
                existing={(w['day'],w['key']) for w in self.state['watches'] if w.get('policy')==ALERT_POLICY and w.get('status')!='ENTRY_RECHECK_REJECTED'}
                for r in rows:
                    if alert_eligible(r) and (d['date'],r['instrument_key']) not in existing:
                        checked=self.refresh_candidate(r)
                        if checked:
                            verified.append(checked)
                            if not alert_eligible(checked):self.counts['diagnostic_'+diagnostic_bucket(checked)]+=1
                # Ordinary observations may finish watches; only verified rows can start one.
                observations=[dict(r,alert_eligible=False) for r in rows]
                for r in observations:r['input_freshness_reasons']=r.get('input_freshness_reasons',[])+['entry_requires_refresh']
                self.process(observations+verified,now(),d['date'])
                if self.telegram:self.flush()
        # Preserve all-strike research coverage after the time-critical alert path.
        background=[c for c in self.options if c['instrument_key'] not in priority_keys]
        for i in range(0,len(background),100):
            group=background[i:i+100];keys=[c['instrument_key'] for c in group]
            try:q=self.api.quotes(keys)
            except PermissionError:raise
            except RuntimeError as exc:
                self.counts['quote_batch_error']+=1;q={}
                self.recorder.write({'kind':'error','observed_at':iso(now()),'operation':'background_quotes','keys':keys,'error':str(exc)})
            all_quotes.update(q)
            rows=self.build_rows(group,q,futures,now(),'background_recording')
            for r in rows:
                self.recorder.write(r);self.counts[r['model_status']]+=1
            if create_watches:
                for r in rows:r['input_freshness_reasons'].append('entry_requires_refresh')
                self.process(rows,now(),d['date']) if now()<=session_close(now()) else None
        # Broker Greeks are supplementary; do not delay premium quotes or alerts for them.
        if self.broker_greeks_available:
            keys=list(all_quotes)
            try:
                broker=self.api.quotes(keys,greek=True)
                for key,(value,at) in broker.items():
                    self.recorder.write({'kind':'broker_greeks','instrument_key':key,'observed_at':iso(at),'broker_greeks':value,
                                         'fetch_phase':'supplementary_greeks','fetch_version':FETCH_VERSION})
            except PermissionError:raise
            except RuntimeError:
                self.broker_greeks_available=False
                self.counts['broker_greeks_unavailable']+=1
        at=now()
        d.update(sweeps=d['sweeps']+1,last_sweep_at=iso(at),last_sweep_seconds=round(time.monotonic()-start,2),
                 counts=dict(self.counts),fetch_version=FETCH_VERSION,last_quote_count=len(all_quotes),
                 last_broker_greeks_count=len(broker),last_missing_quote_count=len(self.options)-len(all_quotes),
                 priority_option_count=len(priority_keys))
        self.save()
        print(f'MCX {at:%H:%M:%S} sweep={d["sweeps"]} quotes={len(all_quotes)} '
              f'valid_pricing_gap_too_small={self.counts["diagnostic_valid_pricing_gap_too_small"]} '
              f'blocked_stale_inputs={self.counts["diagnostic_blocked_stale_inputs"]}',flush=True)

    def checkpoint(self):
        self.recorder.close();self.save()
        if os.getenv('GIT_STATE')!='true':return
        def git(*args):return subprocess.run(['git',*args],cwd=self.root,check=True,capture_output=True,text=True)
        git('add','.')
        if subprocess.run(['git','diff','--cached','--quiet'],cwd=self.root).returncode:
            git('commit','-m','MCX snapshot checkpoint [skip ci]')
        git('push','origin','HEAD:mcx-research-state')


def run(mode):
    root=os.getenv('MCX_STATE_DIR','mcx-state');scanner=Scanner(API(),root,telegram=mode!='record')
    stop=[False]
    def halt(*_):stop[0]=True
    signal.signal(signal.SIGTERM,halt);signal.signal(signal.SIGINT,halt)
    started=time.monotonic();checkpoint=started;errors=0
    duration=float(os.getenv('MAX_RUN_MINUTES','300'))*60
    interval=max(5,float(os.getenv('SNAPSHOT_INTERVAL_SECONDS','60')))
    try:
        if mode=='test':
            scanner.prepare(now());scanner.sweep(now(),create_watches=False)
            if scanner.state['days'][now().date().isoformat()].get('last_quote_count',0)==0:
                raise RuntimeError('Connection test failed: zero MCX option quotes; snapshots saved')
            scanner.emit('test-'+iso(now()),'MCX connection test completed. Check snapshot model_status, quote availability and broker Greeks in recorded files.')
            scanner.flush();return
        while not stop[0] and time.monotonic()-started<duration:
            t=now()
            if t.weekday()>=5:break
            if t>=session_close(t)+timedelta(minutes=2):
                scanner.prepare(t);scanner.report(t.date().isoformat());scanner.flush();break
            if t.time()>=walltime(9) and t<=session_close(t):
                try:
                    scanner.sweep(t); errors=0
                except PermissionError:raise
                except (RuntimeError,requests.RequestException,ValueError) as exc:
                    errors+=1
                    scanner.recorder.write({'kind':'error','observed_at':iso(now()),'operation':'sweep','error':str(exc)})
                    print(f'MCX sweep error: {type(exc).__name__}: {exc}',flush=True)
                    if errors>=10:raise RuntimeError('Ten consecutive sweep failures; inspect errors in snapshots') from None
            try:scanner.flush()
            except RuntimeError as exc: print(str(exc),flush=True)
            if mode=='once':break
            if time.monotonic()-checkpoint>=300:scanner.checkpoint();checkpoint=time.monotonic()
            deadline=min(time.monotonic()+interval,started+duration)
            while not stop[0] and time.monotonic()<deadline:time.sleep(min(2,deadline-time.monotonic()))
    finally:scanner.checkpoint()

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode',choices=['run','once','test','record'],default='run')
    run(parser.parse_args().mode)

