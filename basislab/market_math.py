"""Current event -> actual option legs -> fully costed, inspectable payoff math."""
from collections import OrderedDict
import copy
import hashlib
import math
import threading
import time
from .gap_math import VERSION, binary_ev, conditional_ev, conditional_paths, execution_cost, gap_math, reweight, vertical
from .pricing import YEAR_MS
from .semantics import number
from .store import encode


def execution_quote(contract,surface,spot):
    c=dict(contract)
    if surface['venue']=='deribit' and c.get('forward'):
        c['native_bid']=c['bid']/c['forward'] if c.get('bid') is not None else None
        c['native_ask']=c['ask']/c['forward'] if c.get('ask') is not None else None
        c['native_mark']=c['mark']/c['forward'] if c.get('mark') is not None else None
        for field in ('bid','ask','mark'):
            if c.get('native_'+field) is not None:c[field]=c['native_'+field]*spot
        c.update(usd_conversion_spot=spot,quote_currency='BTC_OR_ETH',conversion='Native provider premium converted at observed spot; USD paper cash proxy')
    return c


def chain_subset(surface,threshold,spot,per_kind=8):
    rows=[]
    for kind in ('call','put'):
        chain=surface.get(kind+'s',[])
        choices=sorted(chain,key=lambda c:abs(c['strike']-threshold))[:per_kind//2]+sorted(chain,key=lambda c:abs(c['strike']-spot))[:per_kind//2]
        near=list({c['instrument']:c for c in choices}.values())
        rows.extend(dict(execution_quote(c,surface,spot),option_type=kind,expiry=surface['expiry'],venue=surface['venue'],
            received_ms=surface['received_ms'],source_ms=c.get('source_ms',surface.get('source_ms')),
            underlying=spot,multiplier=100 if surface['venue']=='yahoo' else 1,
            quote_quality='YAHOO_DELAY_UNKNOWN_RESEARCH_PROXY' if surface['venue']=='yahoo' else 'DERIBIT_PUBLIC_SNAPSHOT_USD_CONVERSION') for c in sorted(near,key=lambda c:c['strike']))
    return rows


def candidates(surface,threshold,spot):
    subset=chain_subset(surface,threshold,spot);out=[];multiplier=100 if surface['venue']=='yahoo' else 1
    for kind in ('call','put'):
        chain=[c for c in subset if c['option_type']==kind and number(c.get('bid')) is not None and number(c.get('ask')) is not None and 0<=c['bid']<=c['ask'] and c['ask']>0]
        for c in sorted(chain,key=lambda c:abs(c['strike']-threshold))[:2]:
            legs=[dict(c,side='BUY',price=c['ask'])];cost=execution_cost(legs,multiplier)
            maximum=c['strike']*multiplier if kind=='put' else None
            out.append(dict(kind='long_'+kind,legs=legs,**cost,max_loss=cost['debit'],max_payout=maximum,
                            q_exec=cost['debit']/maximum if maximum else None))
        below=[c for c in chain if c['strike']<threshold];above=[c for c in chain if c['strike']>threshold]
        pairs=[]
        if below and above:pairs.append((max(below,key=lambda c:c['strike']),min(above,key=lambda c:c['strike'])))
        # Narrow neighboring spreads around the threshold and current spot.
        pairs+=sorted(zip(chain,chain[1:]),key=lambda pair:min(abs((pair[0]['strike']+pair[1]['strike'])/2-threshold),abs((pair[0]['strike']+pair[1]['strike'])/2-spot)))[:3]
        seen=set()
        for lo,hi in pairs:
            key=(lo['instrument'],hi['instrument'])
            if key in seen:continue
            seen.add(key)
            try:out.append(vertical(lo,hi,kind,multiplier))
            except ValueError:pass
    return out


class MarketMath:
    def __init__(self,engine):
        self.engine=engine;self.lock=threading.RLock();self.path_cache=OrderedDict();self.conditional_cache=OrderedDict();self.tickets=OrderedDict()
        self.rows={};self.stop=threading.Event();self.thread=None;self.error=None

    def start(self):
        self.thread=threading.Thread(target=self.run,name='basis-market-math',daemon=True);self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:self.thread.join(timeout=10)

    def run(self):
        while not self.stop.is_set():
            try:self.refresh();self.error=None
            except Exception as error:self.error=str(error)
            self.stop.wait(2)

    def refresh(self):
        with self.engine.lock:
            now=time.time_ns()//1000000;rows=copy.deepcopy(list(self.engine.latest.values()))
            surfaces=copy.deepcopy(self.engine.surfaces)
        computed={}
        for row in rows:
            surface=next((s for (a,e),s in surfaces.items() if a==row['asset'] and e==row.get('model_inputs',{}).get('expiry')),None)
            if not surface:
                relevant=[s for (a,e),s in surfaces.items() if a==row['asset'] and e>=row['expiry']]
                surface=min(relevant,key=lambda s:s['expiry']) if relevant else None
            result=self.calculate(row,surface,now)
            computed[row['event_id']]=result
        with self.lock:self.rows=computed

    def calculate(self,row,surface,now=None):
        now=now if now is not None else time.time_ns()//1000000
        result=dict(row,math=gap_math(row.get('pm_yes'),row.get('opt_yes')),trade=None,candidates=[],chain=[],
                    trade_reason='NO_SURFACE',math_version=VERSION,calculated_at=now)
        if not surface or not row.get('spot'):return result
        result['chain']=chain_subset(surface,row['strike_or_threshold'],row['spot'])
        result['option_expiry']=surface['expiry'];result['surface_raw_id']=surface['raw_id']
        result['quote_age_ms']=max(0,now-surface['received_ms'])
        equity=surface['venue']=='yahoo';closed=equity and surface.get('context',row.get('underlying_context',{})).get('market_state')!='OPEN'
        if equity:
            from .equities import regular_session
            closed=closed or regular_session(now)['market_state']!='OPEN'
        fresh=0<=now-surface['received_ms']<=(max(90000,self.engine.config.yahoo_seconds*2000) if equity else 45000)
        result['execution_state']='MARKET_CLOSED' if closed else 'QUOTE_STALE' if not fresh else 'PROXY_EXECUTABLE' if equity else 'EXECUTABLE_SNAPSHOT'
        if row.get('gap_pp') is None:
            result['trade_reason']=row.get('source_state','NO_MODEL')+': '+', '.join(row.get('quality_flags',[]));return result
        trade_candidates=candidates(surface,row['strike_or_threshold'],row['spot'])
        if row['event_type']=='terminal':
            direction=row['direction'] if row['gap_pp']>0 else ('down' if row['direction']=='up' else 'up')
            kind='call_vertical' if direction=='up' else 'put_vertical'
            trade_candidates=[t for t in trade_candidates if t['kind']==kind and min(l['strike'] for l in t['legs'])<row['strike_or_threshold']<max(l['strike'] for l in t['legs'])]
        if not trade_candidates:result['trade_reason']='MISSING_ACTUAL_BID_ASK_OR_STRIKE_BRACKET';return result
        terminal=row['event_type']=='terminal' and surface['expiry']==row['expiry']
        if terminal:
            exposure_direction=row['direction'] if row['gap_pp']>0 else ('down' if row['direction']=='up' else 'up')
            kind='call_vertical' if exposure_direction=='up' else 'put_vertical'
            for trade in trade_candidates:
                strikes=[l['strike'] for l in trade['legs']]
                if trade['kind']==kind and min(strikes)<row['strike_or_threshold']<max(strikes):
                    p=row['pm_yes'] if row['gap_pp']>0 else 1-row['pm_yes']
                    trade.update(binary_ev(p,trade),event_exposure='YES' if row['gap_pp']>0 else 'NO',pm_exposure_probability=p)
                    result['candidates'].append(trade)
        else:
            sigma=row.get('model_inputs',{}).get('iv') or row.get('surface_features',{}).get('local_iv')
            if not sigma:result['trade_reason']='NO_IV_FOR_CONDITIONAL_PAYOFF_MODEL';return result
            cache_key=encode([surface['raw_id'],row.get('mapping_hash'),row['event_id'],row['spot'],sigma,now//15000])
            if cache_key not in self.path_cache:
                try:
                    self.path_cache[cache_key]=conditional_paths(row['spot'],row['strike_or_threshold'],sigma,
                        (row['expiry']-now)/YEAR_MS,(surface['expiry']-now)/YEAR_MS,row['direction'],row['event_type'])
                except ValueError as error:result['trade_reason']=str(error);return result
                if len(self.path_cache)>80:self.path_cache.popitem(last=False)
            paths=self.path_cache[cache_key]
            if min(paths['hit_effective_paths'],paths['no_hit_effective_paths'])<32:
                result['trade_reason']='INSUFFICIENT_CONDITIONAL_PATH_EFFECTIVE_SAMPLE';return result
            for trade in trade_candidates:
                condition_key=(cache_key,tuple(l['instrument'] for l in trade['legs']))
                if condition_key not in self.conditional_cache:
                    self.conditional_cache[condition_key]=conditional_ev(row['pm_yes'],trade,paths)
                    if len(self.conditional_cache)>1000:self.conditional_cache.popitem(last=False)
                stats=dict(self.conditional_cache[condition_key]);p=row['pm_yes']
                expected=reweight(p,stats['payoff_if_hit'],stats['payoff_if_no_hit'])
                stats.update(expected_payoff=expected,ev=expected-trade['debit'],roi=(expected-trade['debit'])/trade['debit'],
                    pm_increment=(p-stats['q_path'])*(stats['payoff_if_hit']-stats['payoff_if_no_hit']),
                    mc_standard_error=math.hypot(p*stats['mc_hit_se'],(1-p)*stats['mc_no_hit_se']))
                model_payoff=reweight(stats['q_path'],stats['payoff_if_hit'],stats['payoff_if_no_hit'])
                bid_value=sum((l['bid'] if l['side']=='BUY' else -l['ask'])*trade['multiplier'] for l in trade['legs'])
                ask_value=sum((l['ask'] if l['side']=='BUY' else -l['bid'])*trade['multiplier'] for l in trade['legs'])
                q_error=math.hypot(stats['q_path']*stats['mc_hit_se'],(1-stats['q_path'])*stats['mc_no_hit_se'])
                stats.update(q_payoff=model_payoff,quoted_payoff_band=[bid_value,ask_value],
                    calibration='WITHIN_BOOK_MC_UNCERTAINTY' if bid_value-2*q_error<=model_payoff<=ask_value+2*q_error else 'MODEL_VALUE_OUTSIDE_ACTUAL_BOOK')
                trade.update(stats,event_exposure='PM_REWEIGHTED',pm_exposure_probability=row['pm_yes'])
                result['candidates'].append(trade)
            result['model_limits']=['Flat IV / zero carry','Conditional shape remains options-model shape','Yahoo American/overnight proxy' if equity else 'Crypto USD premium conversion / cash payoff proxy']
        for trade in result['candidates']:
            trade.update(expiry=surface['expiry'],event_id=row['event_id'],asset=row['asset'],surface_raw_id=surface['raw_id'],
                         event_version=row.get('mapping_hash'),event_cutoff=row['expiry'],event_text=row['event_text'],
                         input_refs=copy.deepcopy(row.get('input_refs',{})),pm_yes=row['pm_yes'],opt_yes=row['opt_yes'],
                         quote_received_ms=surface['received_ms'],math_calculated_ms=now,execution_state=result['execution_state'],math_version=VERSION)
            if not equity and any(l.get('source_ms') is None or not 0<=now-l['source_ms']<=45000 for l in trade['legs']):
                trade['execution_state']='QUOTE_STALE'
            trade['structure_id']=hashlib.sha256(encode(dict(trade,pm=row['pm_yes'],q=row['opt_yes'],calculated_at=now)).encode()).hexdigest()[:24]
        # A Q-only calibration residual is not an expression of the PM gap.
        # Keep it inspectable in candidates, but suggest only payoffs helped by PM.
        positive=[t for t in result['candidates'] if t['ev']>0 and t.get('pm_increment',1)>0 and t['execution_state']!='QUOTE_STALE' and t.get('calibration')!='MODEL_VALUE_OUTSIDE_ACTUAL_BOOK']
        positive.sort(key=lambda t:t['roi'],reverse=True)
        if positive:
            result['trade']=positive[0];result['trade_reason']='POSITIVE_MODEL_EV' if fresh else 'QUOTE_STALE'
            with self.lock:
                self.tickets[positive[0]['structure_id']]=copy.deepcopy(positive[0])
                while len(self.tickets)>256:self.tickets.popitem(last=False)
        else:result['trade_reason']='MODEL_VALUE_OUTSIDE_ACTUAL_BOOK' if any(t['ev']>0 and t.get('calibration')=='MODEL_VALUE_OUTSIDE_ACTUAL_BOOK' for t in result['candidates']) else 'QUOTE_STALE' if any(t['ev']>0 and t['execution_state']=='QUOTE_STALE' for t in result['candidates']) else 'NO_POSITIVE_EV_AFTER_COSTS'
        return result

    def ticket(self,structure_id):
        with self.lock:
            result=self.tickets.get(str(structure_id))
            if not result:raise ValueError('Displayed structure expired; refresh this event')
            return copy.deepcopy(result)

    def snapshot(self):
        with self.lock:return dict(rows=copy.deepcopy(list(self.rows.values())),error=self.error)

    def quote_legs(self,trade,now):
        with self.engine.lock:
            event=self.engine.events.get(trade['event_id'])
            if not event or event.get('mapping_hash')!=trade.get('event_version') or event['expiry']!=trade.get('event_cutoff',event['expiry']):
                raise ValueError('EVENT_CONTEXT_CHANGED: select and refresh the current event')
            surface=self.engine.surfaces.get((trade['asset'],trade['expiry']))
            if not surface:raise ValueError('NO_SURFACE')
            equity=surface['venue']=='yahoo'
            if equity:
                from .equities import regular_session
                if surface.get('context',{}).get('market_state')!='OPEN' or regular_session(now)['market_state']!='OPEN':raise ValueError('MARKET_CLOSED: Yahoo target market is closed')
            limit=max(90000,self.engine.config.yahoo_seconds*2000) if equity else 45000
            if not 0<=now-surface['received_ms']<=limit:raise ValueError('QUOTE_STALE: option retrieval exceeded freshness limit')
            legs=[]
            spot=self.engine.spots.get(trade['asset'])
            if not equity and (not spot or not 0<=now-spot.get('source_ms',0)<=30000):raise ValueError('QUOTE_STALE: crypto conversion spot unavailable/stale')
            for old in trade['legs']:
                c=next((c for c in surface.get(old['option_type']+'s',[]) if c['instrument']==old['instrument']),None)
                if not c or number(c.get('bid')) is None or number(c.get('ask')) is None or not 0<=c['bid']<=c['ask'] or c['ask']<=0:
                    raise ValueError('MISSING_BID_ASK: exact displayed leg unavailable')
                source=c.get('source_ms',surface.get('source_ms'))
                if not equity and (source is None or not 0<=now-source<=limit):raise ValueError('QUOTE_STALE: Deribit source timestamp stale')
                if not equity:c=execution_quote(c,surface,spot['price'])
                legs.append(dict(old,bid=c['bid'],ask=c['ask'],price=c['ask'] if old['side']=='BUY' else c['bid'],
                    received_ms=surface['received_ms'],source_ms=source))
            return legs,surface['raw_id']
