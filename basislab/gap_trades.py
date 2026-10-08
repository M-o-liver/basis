"""Historical ask/bid trade math, using only quotes available by each deadline."""
from collections import Counter,defaultdict
import copy
import math
from pathlib import Path
import numpy as np
from .config import Config
from .engine import Engine
from .gap_math import execution_cost
from .gap_history import bin_gap,bin_odds
from .market_math import MarketMath,execution_quote
from .tape import open_store


def historical_trades(tape,openings,progress=True):
    store=open_store(tape,read_only=True);engine=Engine(store,persist=False);math_engine=MarketMath(engine)
    seen=set();rows=[];excluded=Counter();surfaces={}
    def surface_for(row):
        raw_id=row.get('input_refs',{}).get('options')
        if not raw_id or raw_id>row['raw_id']:return None
        if raw_id not in surfaces:
            raw=store.raw_record(raw_id)
            if not raw or raw['received_ms']>row['timestamp_wall']:return None
            engine.surfaces={};engine.spots={};engine.update_options(raw)
            surfaces[raw_id]=copy.deepcopy(engine.surfaces)
            if len(surfaces)>128:surfaces.pop(next(iter(surfaces)))
        options=[s for (asset,expiry),s in surfaces[raw_id].items() if asset==row['asset'] and expiry>=row['expiry']]
        return min(options,key=lambda s:s['expiry']) if options else None
    try:
        for index,opening in enumerate(openings):
            key=(opening['event_id'],opening['timestamp_wall']//86400000)
            if key in seen:continue
            surface=surface_for(opening)
            if not surface:excluded['NO_CAUSAL_SURFACE']+=1;continue
            result=math_engine.calculate(opening,surface,opening['timestamp_wall']);trade=result['trade']
            if not trade:excluded[result['trade_reason']]+=1;continue
            if result['execution_state']=='MARKET_CLOSED':excluded['MARKET_CLOSED']+=1;continue
            seen.add(key)
            for h,future in opening.get('_future',{}).items():
                horizon=int(h);deadline=opening['timestamp_wall']+horizon*1000
                if future['timestamp_wall']>deadline or deadline-future['timestamp_wall']>2000:excluded['MISSING_HORIZON']+=1;continue
                exit_surface=surface_for(future)
                if not exit_surface:excluded['MISSING_EXIT_SURFACE']+=1;continue
                if exit_surface['expiry']!=trade['expiry']:excluded['EXACT_EXPIRY_NOT_RETAINED']+=1;continue
                exit_legs=[]
                for leg in trade['legs']:
                    contract=next((c for c in exit_surface.get(leg['option_type']+'s',[]) if c['instrument']==leg['instrument']),None)
                    if not contract or contract.get('bid') is None or contract.get('ask') is None or not 0<=contract['bid']<=contract['ask']:break
                    contract=execution_quote(contract,exit_surface,future['spot'])
                    side='SELL' if leg['side']=='BUY' else 'BUY'
                    exit_legs.append(dict(leg,bid=contract['bid'],ask=contract['ask'],side=side,price=contract['bid'] if side=='SELL' else contract['ask']))
                if len(exit_legs)!=len(trade['legs']):excluded['MISSING_EXACT_EXIT_QUOTES']+=1;continue
                age_limit=120000 if surface['venue']=='yahoo' else 45000
                if not 0<=deadline-exit_surface['received_ms']<=age_limit:excluded['EXIT_QUOTE_STALE']+=1;continue
                close=execution_cost(exit_legs,trade['multiplier']);credit=-close['debit'];pnl=credit-trade['debit']
                entry_mid=sum((1 if l['side']=='BUY' else -1)*(l['bid']+l['ask'])/2*trade['multiplier'] for l in trade['legs'])
                exit_mid=sum((1 if l['side']=='SELL' else -1)*(l['bid']+l['ask'])/2*trade['multiplier'] for l in exit_legs)
                rows.append(dict(event_id=opening['event_id'],block=str(key),asset=opening['asset'],event_type=opening['event_type'],
                    gap_bin=bin_gap(opening['gap_pp']),log_odds_bin=bin_odds(result['math']['log_odds_gap']),expression=trade['kind'],horizon=horizon,
                    cost=trade['spread_cost']+trade['fees']+trade['slippage']+close['spread_cost']+close['fees']+close['slippage'],entry_debit=trade['debit'],pnl=pnl,roi=pnl/trade['debit'],gross_movement=exit_mid-entry_mid,
                    opening_raw_id=opening['raw_id'],exit_raw_id=future['raw_id'],entry_surface_raw_id=surface['raw_id'],exit_surface_raw_id=exit_surface['raw_id']))
            if progress and index%100==0:print(f'trade math: {index+1}/{len(openings)} openings, {len(rows)} causal exits',flush=True)
    finally:store.close()
    def summary(group):
        return dict(n_blocks=len({r['block'] for r in group}),trades=len(group),win_rate=sum(r['pnl']>0 for r in group)/len(group),
            mean_pnl=float(np.mean([r['pnl'] for r in group])),median_pnl=float(np.median([r['pnl'] for r in group])),
            total_cost=sum(r['cost'] for r in group),mean_gross_movement=float(np.mean([r['gross_movement'] for r in group])),
            mean_net_movement=float(np.mean([r['pnl'] for r in group])),mean_roi=float(np.mean([r['roi'] for r in group])))
    report=dict(mode='EXPLORATORY_HISTORICAL',selection='First eligible sampled opening per event/UTC-day; select highest modeled positive ROI; no threshold optimization',
        cost_model='Actual entry ask/bid and exit bid/ask, plus 10bps slippage, size impact, 10bps fees and $0.65/leg each side',
        missing=dict(excluded),opening_blocks=len(seen),horizons={},strata={},rows=rows)
    for h in sorted({r['horizon'] for r in rows}):report['horizons'][str(h)]=summary([r for r in rows if r['horizon']==h])
    for dimension in ('asset','event_type','gap_bin','log_odds_bin','expression'):
        groups=defaultdict(list)
        for r in rows:
            if r['horizon']==1800:groups[r[dimension]].append(r)
        report['strata'][dimension]={k:summary(v) for k,v in groups.items()}
    return report
