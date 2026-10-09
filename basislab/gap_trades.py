"""Fixed-horizon exploratory structure returns; frozen legs and causal source quotes."""
from collections import Counter,defaultdict
import copy
import math
import numpy as np
from .engine import Engine
from .gap_math import execution_cost, gap_math, payoff
from .gap_history import HotTape,bin_gap,bin_odds
from .market_math import MarketMath,execution_quote
from .tape import open_store
from .semantics import timestamp,number
from .equities import calendar,regular_session
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo


def next_open(now):
    day=datetime.fromtimestamp(now/1000,ZoneInfo('America/New_York')).date();cal=calendar(day)
    for session in cal.sessions_in_range(day.isoformat(),(day+timedelta(days=20)).isoformat()):
        opened=int(cal.session_open(session).timestamp()*1000)
        if opened>now:return opened
    raise ValueError('No XNYS open in calendar coverage')


def exact_quotes(trade,surface,spot,at):
    if surface is None or surface['expiry']!=trade['expiry']:raise ValueError('EXACT_EXPIRY_UNAVAILABLE')
    equity=surface['venue']=='yahoo';limit=120000 if equity else 45000
    if not 0<=at-surface['received_ms']<=limit:raise ValueError('QUOTE_STALE')
    if equity and (regular_session(at)['market_state']!='OPEN' or surface.get('context',{}).get('market_state')!='OPEN'):raise ValueError('MARKET_CLOSED')
    if not spot or not spot.get('price') or spot.get('received_ms',at)>at:raise ValueError('NO_CAUSAL_SPOT')
    if not equity and not 0<=at-spot.get('source_ms',0)<=30000:raise ValueError('SPOT_STALE')
    out=[]
    for leg in trade['legs']:
        c=next((c for c in surface.get(leg['option_type']+'s',[]) if c['instrument']==leg['instrument']),None)
        if c is None:raise ValueError('EXACT_CONTRACT_UNAVAILABLE')
        if c.get('bid') is None or c.get('ask') is None:raise ValueError('MISSING_BID_ASK')
        if not 0<=c['bid']<=c['ask']:raise ValueError('CROSSED_QUOTE')
        source=c.get('source_ms',surface.get('source_ms'))
        if source is not None and source>at:raise ValueError('FUTURE_SOURCE_TIMESTAMP')
        if not equity and (source is None or at-source>limit):raise ValueError('STALE_SOURCE')
        c=execution_quote(c,surface,spot['price'])
        out.append(dict(leg,bid=c['bid'],ask=c['ask'],price=c['ask'] if leg['side']=='BUY' else c['bid'],
            source_ms=source,received_ms=surface['received_ms']))
    return out


def liquidation(legs,multiplier):
    reverse=[dict(l,side='SELL' if l['side']=='BUY' else 'BUY',price=l['bid'] if l['side']=='BUY' else l['ask']) for l in legs]
    costs=execution_cost(reverse,multiplier)
    return dict(liquidation_credit=-costs['debit'],mid_value=sum((1 if l['side']=='BUY' else -1)*(l['bid']+l['ask'])/2*multiplier for l in legs),**costs)


def historical_trades(tape,openings,progress=True):
    store=open_store(tape,read_only=True);reader=HotTape(tape);engine=Engine(store,persist=False);math_engine=MarketMath(engine)
    seen=set();rows=[];excluded=Counter();surfaces={};raw_cache={}
    def raw(identity):
        if identity not in raw_cache:
            raw_cache[identity]=store.raw_record(identity)
            if len(raw_cache)>512:raw_cache.pop(next(iter(raw_cache)))
        return raw_cache[identity]
    def surface_at(record,expiry=None):
        if not record:return None
        identity=record['id']
        if identity not in surfaces:
            engine.surfaces={};engine.spots={};engine.update_options(record)
            surfaces[identity]=copy.deepcopy(engine.surfaces)
            if len(surfaces)>128:surfaces.pop(next(iter(surfaces)))
        choices=[s for (a,e),s in surfaces[identity].items() if a==record['subject'] and (expiry is None or e==expiry)]
        return min(choices,key=lambda s:s['expiry']) if choices else None
    def spot_at(asset,at,option_record):
        if asset not in ('BTC','ETH'):
            p=option_record['payload'];return dict(price=p.get('spot'),source_ms=p.get('spot_source_ms'),received_ms=option_record['received_ms'],raw_id=option_record['id'])
        refs=reader.raw_refs('spot',asset,max(reader.start,at-60000),at)
        if not refs:return None
        r=raw(refs[0]['id']);p=r['payload'];return dict(price=number(p.get('price')),source_ms=timestamp(p.get('timestamp')) or r.get('source_ms'),received_ms=r['received_ms'],raw_id=r['id'])
    def quote_at(trade,at,first=False):
        refs=reader.raw_refs('options',trade['asset'],at if first else max(reader.start,at-120000),min(reader.end,at+600000) if first else at,first=first)
        for ref in refs:
            record=raw(ref['id']);surface=surface_at(record,trade['expiry']);timestamp=record['received_ms'] if first else at
            spot=spot_at(trade['asset'],timestamp,record)
            try:return exact_quotes(trade,surface,spot,timestamp),surface,spot,timestamp
            except ValueError as error:excluded[str(error)]+=1
        return None
    try:
        for index,opening in enumerate(openings):
            key=(opening['event_id'],opening['timestamp_wall']//86400000)
            if key in seen:continue
            ref=opening.get('input_refs',{}).get('options')
            if not ref or ref>opening['raw_id']:excluded['NO_CAUSAL_SURFACE']+=1;continue
            record=raw(ref)
            if not record or record['received_ms']>opening['timestamp_wall']:excluded['FUTURE_OPENING_QUOTE']+=1;continue
            surface=surface_at(record)
            choices=[s for (a,e),s in surfaces.get(ref,{}).items() if a==opening['asset'] and e>=opening['expiry']]
            surface=min(choices,key=lambda s:s['expiry']) if choices else None
            result=math_engine.calculate(opening,surface,opening['timestamp_wall']);trade=result['trade']
            if not trade:excluded[result['trade_reason']]+=1;continue
            # First calibrated PM-helped expression per event/day, even if drag overwhelms it.
            seen.add(key)
            target=opening['timestamp_wall']+5000
            if trade['multiplier']==100 and regular_session(opening['timestamp_wall'])['market_state']!='OPEN':target=next_open(opening['timestamp_wall'])+10000
            entry=quote_at(trade,target,first=True)
            if entry is None:excluded['NO_VALID_POST_LATENCY_ENTRY']+=1;continue
            legs,entry_surface,entry_spot,entry_ms=entry
            if entry_ms>=trade['expiry']:excluded['EXPIRY_BEFORE_ENTRY']+=1;continue
            costs=execution_cost(legs,trade['multiplier'])
            if costs['debit']<=0:excluded['NONPOSITIVE_ENTRY_DEBIT']+=1;continue
            deadlines={'1800':entry_ms+1800000,'7200':entry_ms+7200000,'expiry':trade['expiry']}
            if trade['multiplier']==100:
                day=datetime.fromtimestamp(entry_ms/1000,ZoneInfo('America/New_York')).date();cal=calendar(day)
                deadlines['session_close']=int(cal.session_close(day.isoformat()).timestamp()*1000)-1
                deadlines['next_open']=next_open(entry_ms)+10000
            for horizon,deadline in deadlines.items():
                if deadline>reader.end:excluded['MISSING_'+horizon.upper()]+=1;continue
                exit_surface=None;exit_raw=None
                if horizon=='expiry':
                    # An as-of recorded price is an explicitly labeled payoff proxy, not official settlement.
                    refs=reader.raw_refs('spot' if trade['asset'] in ('BTC','ETH') else 'options',trade['asset'],deadline-120000,deadline)
                    if not refs:excluded['SETTLEMENT_MISSING']+=1;continue
                    settle_record=raw(refs[0]['id']);spot=spot_at(trade['asset'],deadline,settle_record)
                    age=45000 if trade['asset'] in ('BTC','ETH') else 120000
                    if not spot or spot.get('source_ms') is None or not 0<=deadline-spot['source_ms']<=age:excluded['SETTLEMENT_MISSING']+=1;continue
                    credit=float(payoff(trade,np.array([spot['price']]))[0]);exit_fees=exit_slip=exit_spread=0;gross_exit=credit
                    exit_raw=spot['raw_id'];actual_exit=deadline;quality='RECORDED_ASOF_SPOT_PAYOFF_PROXY'
                else:
                    if deadline>=trade['expiry']:excluded['OPTION_EXPIRED_BEFORE_'+horizon.upper()]+=1;continue
                    out=quote_at(trade,deadline,first=horizon=='next_open')
                    if out is None:excluded['MISSING_EXIT_'+horizon.upper()]+=1;continue
                    exit_legs,exit_surface,spot,actual_exit=out;mark=liquidation(exit_legs,trade['multiplier']);credit=mark['liquidation_credit']
                    exit_fees=mark['fees'];exit_slip=mark['slippage'];exit_spread=mark['spread_cost'];gross_exit=mark['mid_value'];exit_raw=exit_surface['raw_id'];quality='ACTUAL_BID_ASK_PROXY_QUOTES'
                pnl=credit-costs['debit'];entry_mid=sum((1 if l['side']=='BUY' else -1)*(l['bid']+l['ask'])/2*trade['multiplier'] for l in legs)
                rows.append(dict(event_id=opening['event_id'],block=str(key),asset=opening['asset'],event_type=opening['event_type'],provenance=opening['formation']['provenance'],
                    gap_bin=bin_gap(opening['gap_pp']),log_odds_bin=bin_odds(result['math']['log_odds_gap']),expression=trade['kind'],horizon=horizon,
                    cost=costs['spread_cost']+costs['fees']+costs['slippage']+exit_spread+exit_fees+exit_slip,entry_debit=costs['debit'],pnl=pnl,roi=pnl/costs['debit'],gross_movement=gross_exit-entry_mid,
                    target_entry=target,entry_ms=entry_ms,entry_delay_ms=entry_ms-target,target_exit=deadline,actual_exit=actual_exit,quote_quality=quality,
                    information_value=trade['information_value'],information_drag_ratio=trade['information_drag_ratio'],star_gap_pp=opening['gap_pp'],
                    opening_raw_id=opening['raw_id'],entry_surface_raw_id=entry_surface['raw_id'],exit_raw_id=exit_raw,exact_legs=[(l['instrument'],l['side']) for l in trade['legs']]))
            if progress and index%100==0:print(f'structure math: {index+1}/{len(openings)} openings, {len(rows)} fixed-horizon exits',flush=True)
    finally:store.close();reader.close()
    def summary(group):
        if not group:return dict(n_blocks=0,status='MISSING')
        return dict(n_blocks=len({r['block'] for r in group}),unique_events=len({r['event_id'] for r in group}),trades=len(group),win_rate=sum(r['pnl']>0 for r in group)/len(group),
            mean_pnl=float(np.mean([r['pnl'] for r in group])),median_pnl=float(np.median([r['pnl'] for r in group])),total_cost=sum(r['cost'] for r in group),
            mean_gross_movement=float(np.mean([r['gross_movement'] for r in group])),mean_roi=float(np.mean([r['roi'] for r in group])),
            median_entry_delay_s=float(np.median([r['entry_delay_ms']/1000 for r in group])),status='EXPLORATORY' if len(group)>=10 else 'SMALL_SAMPLE')
    report=dict(mode='EXPLORATORY_HISTORICAL',selection='First calibrated PM-helped candidate/event-day; rank information/drag with positive net-EV priority; freeze legs, then first valid options receipt after 5s / next XNYS open+10s',
        holding_policy='30m and 2h as-of exits; session close-1ms; next-open first valid receipt after open+10s; recorded expiry payoff where available. No optimized exits.',
        cost_model='Actual ask/bid; long-bid/short-ask exits + unchanged explicit fees/slippage/impact',missing=dict(excluded),opening_blocks=len(seen),horizons={},strata={},rows=rows)
    for h in ('1800','7200','session_close','next_open','expiry'):report['horizons'][h]=summary([r for r in rows if r['horizon']==h])
    for dimension in ('asset','event_type','gap_bin','log_odds_bin','provenance','expression'):
        report['strata'][dimension]={}
        for h in report['horizons']:
            groups=defaultdict(list)
            for r in rows:
                if r['horizon']==h:groups[r[dimension]].append(r)
            report['strata'][dimension][h]={k:summary(v) for k,v in groups.items()}
    return report
