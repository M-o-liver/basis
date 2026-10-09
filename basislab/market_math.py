"""Current event -> actual option legs -> fully costed, inspectable payoff math."""
from collections import OrderedDict
import copy
import hashlib
import math
import threading
import time
from .gap_math import VERSION, conditional_ev, conditional_paths, execution_cost, gap_math, formation, reweight, vertical
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
        c.update(usd_conversion_spot=spot,quote_currency='BTC_OR_ETH',conversion='Native provider premium converted at observed spot; USD research payoff proxy')
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
        chain=sorted([c for c in subset if c['option_type']==kind and number(c.get('bid')) is not None and number(c.get('ask')) is not None and 0<=c['bid']<=c['ask'] and c['ask']>0],key=lambda c:c['strike'])
        singles=list({c['instrument']:c for anchor in (threshold,spot) for c in sorted(chain,key=lambda c:abs(c['strike']-anchor))[:2]}.values())
        for c in singles:
            legs=[dict(c,side='BUY',price=c['ask'])];cost=execution_cost(legs,multiplier)
            maximum=c['strike']*multiplier if kind=='put' else None
            out.append(dict(kind='long_'+kind,legs=legs,**cost,max_loss=cost['debit'],max_payout=maximum,
                            q_exec=cost['debit']/maximum if maximum else None))
        pairs=set()
        for width in (1,2,4):
            possible=[(i,i+width) for i in range(len(chain)-width)]
            for anchor in (threshold,spot):
                pairs.update(sorted(possible,key=lambda pair:abs((chain[pair[0]]['strike']+chain[pair[1]]['strike'])/2-anchor))[:2])
        for i,j in sorted(pairs):
            try:out.append(vertical(chain[i],chain[j],kind,multiplier))
            except ValueError:pass
    return out


class MarketMath:
    def __init__(self,engine):
        self.engine=engine;self.lock=threading.RLock();self.path_cache=OrderedDict();self.conditional_cache=OrderedDict();self.displayed=OrderedDict()
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
            semantics=copy.deepcopy(self.engine.events)
            priors={}
            for row in rows:
                history=self.engine.dynamics.history.get((row['event_id'],row.get('mapping_hash')),[])
                priors[row['event_id']]=next((copy.deepcopy(r) for r in reversed(history) if r['timestamp_wall']<=row['timestamp_wall']-30000),None)
        computed={}
        for row in rows:
            surface=next((s for (a,e),s in surfaces.items() if a==row['asset'] and e==row.get('model_inputs',{}).get('expiry')),None)
            if not surface:
                relevant=[s for (a,e),s in surfaces.items() if a==row['asset'] and e>=row['expiry']]
                surface=min(relevant,key=lambda s:s['expiry']) if relevant else None
            row['formation']=formation(row,priors[row['event_id']])
            result=self.calculate(row,surface,now)
            result['event_semantics']=semantics.get(row['event_id'])
            computed[row['event_id']]=result
        with self.lock:
            for row in computed.values():
                row['snapshot_id']=hashlib.sha256(encode([row['event_id'],row['raw_id'],row['calculated_at'],row.get('trade',{}).get('structure_id') if row.get('trade') else None]).encode()).hexdigest()[:24]
                self.displayed[row['snapshot_id']]=copy.deepcopy(row)
            while len(self.displayed)>512:self.displayed.popitem(last=False)
            self.rows=computed

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
        if not trade_candidates:result['trade_reason']='MISSING_ACTUAL_BID_ASK';return result
        sigma=row.get('model_inputs',{}).get('iv') or row.get('surface_features',{}).get('local_iv')
        if not sigma:result['trade_reason']='NO_IV_FOR_CONDITIONAL_PAYOFF_MODEL';return result
        cache_key=encode([surface['raw_id'],row.get('mapping_hash'),row['event_id'],row['spot'],sigma,now//15000])
        if cache_key not in self.path_cache:
            try:self.path_cache[cache_key]=conditional_paths(row['spot'],row['strike_or_threshold'],sigma,
                (row['expiry']-now)/YEAR_MS,(surface['expiry']-now)/YEAR_MS,row['direction'],row['event_type'])
            except ValueError as error:result['trade_reason']=str(error);return result
            if len(self.path_cache)>80:self.path_cache.popitem(last=False)
        paths=self.path_cache[cache_key]
        if min(paths['hit_effective_paths'],paths['no_hit_effective_paths'])<32:
            result['trade_reason']='INSUFFICIENT_CONDITIONAL_PATH_EFFECTIVE_SAMPLE';return result
        for trade in trade_candidates:
            condition_key=(cache_key,tuple(l['instrument'] for l in trade['legs']))
            if condition_key not in self.conditional_cache:
                self.conditional_cache[condition_key]=conditional_ev(row['pm_yes'],trade,paths,row['opt_yes'])
                if len(self.conditional_cache)>2000:self.conditional_cache.popitem(last=False)
            stats=dict(self.conditional_cache[condition_key]);p=row['pm_yes'];q=row['opt_yes']
            expected=reweight(p,stats['payoff_if_hit'],stats['payoff_if_no_hit'])
            info=(p-q)*(stats['payoff_if_hit']-stats['payoff_if_no_hit']);ev=expected-trade['debit']
            stats.update(expected_payoff=expected,ev=ev,roi=ev/trade['debit'],q_event=q,
                net_ev=ev-stats['estimated_exit_drag'],net_roi=(ev-stats['estimated_exit_drag'])/trade['debit'],
                pm_increment=info,information_value=info,net_information_value=info-stats['execution_drag'],
                information_drag_ratio=info/stats['execution_drag'] if stats['execution_drag']>0 else None,
                mc_standard_error=math.hypot(p*stats['mc_hit_se'],(1-p)*stats['mc_no_hit_se']))
            model_payoff=reweight(q,stats['payoff_if_hit'],stats['payoff_if_no_hit'])
            bid_value=sum((l['bid'] if l['side']=='BUY' else -l['ask'])*trade['multiplier'] for l in trade['legs'])
            ask_value=sum((l['ask'] if l['side']=='BUY' else -l['bid'])*trade['multiplier'] for l in trade['legs'])
            q_error=math.hypot(q*stats['mc_hit_se'],(1-q)*stats['mc_no_hit_se'])
            stats.update(q_payoff=model_payoff,baseline_ev=model_payoff-trade['debit'],quoted_payoff_band=[bid_value,ask_value],
                calibration='WITHIN_BOOK_MC_UNCERTAINTY' if bid_value-2*q_error<=model_payoff<=ask_value+2*q_error else 'MODEL_VALUE_OUTSIDE_ACTUAL_BOOK')
            trade.update(stats,event_exposure='PM_REWEIGHTED',pm_exposure_probability=p)
            result['candidates'].append(trade)
        result['model_limits']=['Flat-IV / zero carry conditional shape; event mass calibrated to displayed Q',
            'Actual ramp/payoff at option expiry; event cutoff may precede it',
            'Yahoo American/dividend/overnight proxy' if equity else 'Crypto USD premium / cash-payoff proxy']
        for trade in result['candidates']:
            trade.update(expiry=surface['expiry'],event_id=row['event_id'],asset=row['asset'],surface_raw_id=surface['raw_id'],
                         event_version=row.get('mapping_hash'),event_cutoff=row['expiry'],event_text=row['event_text'],
                         input_refs=copy.deepcopy(row.get('input_refs',{})),pm_yes=row['pm_yes'],opt_yes=row['opt_yes'],
                         quote_received_ms=surface['received_ms'],math_calculated_ms=now,execution_state=result['execution_state'],math_version=VERSION)
            if not equity and any(l.get('source_ms') is None or not 0<=now-l['source_ms']<=45000 for l in trade['legs']):
                trade['execution_state']='QUOTE_STALE'
            trade['structure_id']=hashlib.sha256(encode(dict(trade,pm=row['pm_yes'],q=row['opt_yes'],calculated_at=now)).encode()).hexdigest()[:24]
        eligible=[t for t in result['candidates'] if t['information_value']>0 and t['execution_state']!='QUOTE_STALE' and t['calibration']!='MODEL_VALUE_OUTSIDE_ACTUAL_BOOK']
        eligible.sort(key=lambda t:(t['net_ev']>0 and t['net_information_value']>0,t['information_drag_ratio'] or 0,t['net_ev']),reverse=True)
        result['candidates'].sort(key=lambda t:(t['calibration']=='WITHIN_BOOK_MC_UNCERTAINTY',t['information_value']>0,t['information_drag_ratio'] or 0),reverse=True)
        if eligible:
            result['trade']=eligible[0]
            result['trade_reason']='POSITIVE_INFORMATION_AFTER_DRAG' if eligible[0]['net_ev']>0 and eligible[0]['net_information_value']>0 else 'INFORMATION_BELOW_EXECUTION_DRAG'
        else:result['trade_reason']='NO_CALIBRATED_POSITIVE_PM_INFORMATION'
        return result

    def displayed_snapshot(self,event_id,snapshot_id):
        with self.lock:
            row=self.displayed.get(str(snapshot_id))
            if not row or row['event_id']!=event_id:raise ValueError('Displayed signal snapshot unavailable; refresh this market')
            return copy.deepcopy(row)

    def snapshot(self):
        with self.lock:return dict(rows=copy.deepcopy(list(self.rows.values())),error=self.error)
