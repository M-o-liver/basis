"""Versioned experimental trading policies over research diagnostics, not analyzer votes."""
import hashlib
import math
from datetime import datetime, timezone
from .store import encode
from .performance import VERSION as FEEDBACK_VERSION, feedback
from .thesis import VERSION as THESIS_VERSION, underlying_thesis, expression_thesis

VERSION='diagnostic-directional-2.0.0'
ALGOS={'SW':'sliced_wasserstein','EVENT_SYNC':'event_sync','LEAD_LAG':'lead_lag_multiscale',
       'COVARIANCE':'covariance_manifold','TOPOLOGY':'topology','ORDINAL_MMD':'ordinal_mmd','MARTINGALE':'sequential_martingale'}
PARAMETERS=dict(min_gap_pp=.75,tail_floor=.02,budget_pct=.005,max_positions=2,cooldown_ms=1800000,
    max_hold_ms=3600000,exit_gap_pp=.2,stop_loss_pct=.20,take_profit_pct=.30,max_option_spread=.06,
    max_roundtrip_drag=.05,min_pm_move_pp=.10,min_pm_travel=.60,neighbor_count=2,
    min_expiry_ms=7200000,max_signal_age_ms=180000,
    sw_threshold=.25,covariance_threshold=.5,topology_threshold=.10,ordinal_threshold=.05,
    event_sync_min_triggers=3,event_sync_probability=.4,lead_lag_strength=.15,martingale_e_threshold=10)


def definition(wallet):
    return dict(strategy_id=wallet+'_directional',strategy_version=VERSION,analyzer=ALGOS[wallet],parameters=PARAMETERS,
        parameter_hash=hashlib.sha256(encode(dict(PARAMETERS,feedback_version=FEEDBACK_VERSION,thesis_version=THESIS_VERSION)).encode()).hexdigest()[:16],feedback_version=FEEDBACK_VERSION,thesis_version=THESIS_VERSION,
        expression='Near-ATM long call/put; directional proxy, not exact replication of the event')


def entry_evidence(row, rows, now):
    """An anomaly alone is not a directional signal; require new PM movement and a curve."""
    change=next((row.get('features',{}).get('changes',{}).get(str(h)) for h in (60,30)
                 if row.get('features',{}).get('changes',{}).get(str(h))),None)
    if not change:return False,'Need 30–60s PM movement history'
    pm,opt=change.get('pm_pp'),change.get('opt_pp')
    if pm is None or opt is None or not all(math.isfinite(v) for v in (pm,opt)):
        return False,'Invalid movement history'
    if abs(pm)<PARAMETERS['min_pm_move_pp'] or pm*row['gap_pp']<=0:
        return False,'Need fresh PM movement in the gap direction'
    if abs(pm)/(abs(pm)+abs(opt))<PARAMETERS['min_pm_travel']:
        return False,'Gap movement dominated by conventional repricing'
    bid,ask=row.get('pm_bid'),row.get('pm_ask')
    if bid is None or ask is None or bid>ask or abs(row['gap_pp'])<=100*(ask-bid):
        return False,'Gap does not clear the PM bid/ask uncertainty'
    fields=('asset','expiry','event_type','direction','window_start','settlement_source','threshold_inclusive')
    peers=[p for p in rows.values() if p['event_id']!=row['event_id'] and all(p.get(k)==row.get(k) for k in fields)
           and p.get('strike_or_threshold') is not None and p['strike_or_threshold']!=row['strike_or_threshold']
           and p.get('source_state') in ('OK','PROXY') and p.get('gap_pp') is not None
           and 0<=now-p['timestamp_wall']<60000 and 'WIDE_PM_BOOK' not in p.get('quality_flags',[])
           and p.get('pm_source')!='gamma_indicative']
    nearest=[];strikes=set()
    for p in sorted(peers,key=lambda p:abs(p['strike_or_threshold']-row['strike_or_threshold'])):
        if p['strike_or_threshold'] not in strikes:nearest.append(p);strikes.add(p['strike_or_threshold'])
        if len(nearest)==PARAMETERS['neighbor_count']:break
    if len(nearest)<PARAMETERS['neighbor_count'] or not all(p['gap_pp']*row['gap_pp']>0 for p in nearest):
        return False,'Need agreement from two neighboring strikes'
    return True,'Fresh PM movement with two neighboring strikes; descriptive evidence'


def qualifies(signal,wallet):
    if signal.get('status')!='ok':
        reason='No salient source events yet' if wallet=='EVENT_SYNC' else signal.get('explanation','Analyzer not ready')
        return False,reason
    score=signal.get('raw_score');diag=signal.get('diagnostics',{})
    thresholds={'SW':'sw_threshold','COVARIANCE':'covariance_threshold','TOPOLOGY':'topology_threshold','ORDINAL_MMD':'ordinal_threshold'}
    if wallet in thresholds:
        limit=PARAMETERS[thresholds[wallet]]
        return score is not None and score>=limit,f"{signal['algo_name']} score {score}, threshold {limit}"
    if wallet=='LEAD_LAG':
        accepted=[s for s in diag.get('scales',[]) if s.get('direction')=='PM_LEADS_OPT' and s.get('strength',0)>=PARAMETERS['lead_lag_strength']]
        return bool(accepted),'PM leads at scales '+','.join(str(s['scale']) for s in accepted) if accepted else 'No qualifying PM leadership'
    if wallet=='EVENT_SYNC':
        for row in diag.get('windows',[]):
            pm=row['pm_then_opt'];reverse=row['opt_then_pm']
            if pm['triggers']>=PARAMETERS['event_sync_min_triggers'] and (pm['probability'] or 0)>=PARAMETERS['event_sync_probability'] and (pm['probability'] or 0)>(reverse['probability'] or 0):
                return True,f"PM→OPT event frequency {pm['probability']:.2f} at {row['window_seconds']}s; descriptive association"
        return False,'Too few or weak directed event coincidences'
    # Never manufacture sequential evidence from uncalibrated correlated alarms.
    valid=diag.get('valid_e_process') is True and score is not None and score>=PARAMETERS['martingale_e_threshold']
    return valid,'Calibrated e-process required; disabled diagnostics cannot trigger trades'


class PolicyRunner:
    def __init__(self,desk):
        self.desk=desk;self.next_scan=0;self.next_thesis=0
        desk.db.execute('CREATE TABLE IF NOT EXISTS paper_theses(id INTEGER PRIMARY KEY,timestamp_ms INTEGER NOT NULL,event_id TEXT NOT NULL,version TEXT NOT NULL,data TEXT NOT NULL)')
        for op in ('UPDATE','DELETE'):
            desk.db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_paper_theses_{op} BEFORE {op} ON paper_theses BEGIN SELECT RAISE(ABORT,'immutable thesis'); END")

    def run(self,now):
        if now<self.next_scan:return
        self.next_scan=now+15000
        desk=self.desk
        with desk.engine.lock:
            rows=dict(desk.engine.latest);signals=dict(desk.engine.analyzer_latest)
            histories={key:list(value) for key,value in getattr(getattr(desk.engine,'dynamics',None),'history',{}).items()}
        if now>=self.next_thesis:
            desk.theses={event:underlying_thesis(row,histories.get((event,row.get('mapping_hash')),[]),now) for event,row in rows.items()}
            for event,thesis in desk.theses.items():
                desk.db.execute('INSERT INTO paper_theses(timestamp_ms,event_id,version,data) VALUES(?,?,?,?)',(now,event,THESIS_VERSION,encode(thesis)))
            self.next_thesis=now+60000
        for wallet,algo in ALGOS.items():
            state=desk.states[wallet]
            if not state.get('automation_enabled',True):continue
            if any(o['wallet_id']==wallet for o in desk.pending.values()):continue
            status='Watching: no fresh qualifying diagnostic'
            try:
                upgrading=desk.policy_upgrade_pending(wallet)
                if upgrading and not state['positions']:
                    desk.reset(wallet,carry=True);state=desk.states[wallet];upgrading=False
                risk_feedback=feedback(desk.performance.report,wallet,desk.runs[wallet]['id'],now)
                # Existing positions have independent exits; they do not wait for a new alarm.
                closing=False
                for instrument,position in list(state['positions'].items()):
                    context=position.get('policy_context',{});row=rows.get(context.get('event_id'))
                    try:quote=desk.quote(instrument,now)
                    except ValueError as error:
                        status='Exit monitoring: '+str(error);continue
                    pnl=position['quantity']*position['multiplier']*quote['bid']/position['cost_basis']-1
                    held=now-context.get('opened_at',now)
                    reason=None
                    if upgrading:reason='Policy upgrade: close prior-version exposure'
                    elif held>=PARAMETERS['max_hold_ms']:reason='Maximum holding time'
                    elif quote['expiry']-now<1800000:reason='Exit before expiry'
                    elif pnl<=-PARAMETERS['stop_loss_pct']:reason='Premium stop loss'
                    elif pnl>=PARAMETERS['take_profit_pct']:reason='Premium take profit'
                    elif row and row.get('gap_pp') is not None and now-row['timestamp_wall']<60000:
                        direction=row['gap_pp']*(1 if row['direction']=='up' else -1)
                        if abs(row['gap_pp'])<=PARAMETERS['exit_gap_pp']:reason='Gap converged'
                        elif direction*context.get('direction',0)<0:reason='Directional disagreement reversed'
                    if reason:
                        desk.submit(dict(wallet=wallet,instrument=instrument,quantity=position['quantity'],side='SELL',reason=reason,
                            basis_event_id=context.get('event_id'),algo_signal=context.get('signal'),policy_context=context,
                            client_order_id=f"{wallet}-{desk.runs[wallet]['id']}-{instrument}-exit-{now//15000}"),policy=True)
                        status='Exit queued: '+reason;closing=True;break
                if closing:pass
                elif upgrading:status='Draining prior-policy exposure before version change'
                elif now<state.get('entry_hold_until',0):status='Recovery: waiting two minutes after the execution gap'
                elif not risk_feedback['entry_allowed']:status='Loss brake until '+datetime.fromtimestamp(risk_feedback['cooldown_until']/1000,timezone.utc).strftime('%H:%M UTC')
                elif len(state['positions'])>=PARAMETERS['max_positions']:status='Holding: position limit'
                elif now-state.get('last_policy_order',0)<PARAMETERS['cooldown_ms']:status='Cooldown: managing existing positions'
                else:
                    candidates=[];best_window=-1
                    for event_id,row in rows.items():
                        signal=signals.get((event_id,algo))
                        if not signal or not 0<=now-signal['timestamp']<=PARAMETERS['max_signal_age_ms']:continue
                        if row.get('input_refs',{}).get('mapping',0)>signal['raw_id']:continue
                        p,q=row.get('pm_yes'),row.get('opt_yes');gap=row.get('gap_pp')
                        if row.get('source_state') not in ('OK','PROXY') or not 0<=now-row['timestamp_wall']<60000:continue
                        if p is None or q is None or not PARAMETERS['tail_floor']<=p<=1-PARAMETERS['tail_floor'] or not PARAMETERS['tail_floor']<=q<=1-PARAMETERS['tail_floor']:continue
                        if gap is None or abs(gap)<PARAMETERS['min_gap_pp'] or row.get('pm_source')=='gamma_indicative':continue
                        if 'WIDE_PM_BOOK' in row.get('quality_flags',[]):continue
                        if any(pos['asset']==row['asset'] for pos in state['positions'].values()):continue
                        good,why=qualifies(signal,wallet)
                        if not good:
                            samples=signal.get('input_window',{}).get('observations',0)
                            if samples>best_window:status='Watching: '+why;best_window=samples
                            continue
                        thesis=underlying_thesis(row,histories.get((event_id,row.get('mapping_hash')),[]),now)
                        desk.theses[event_id]=thesis
                        if not thesis['eligible']:status='Thesis: '+thesis['rejection_reasons'][0];continue
                        good,evidence=entry_evidence(row,rows,now)
                        if not good:status='Watching: '+evidence;continue
                        why+='; '+evidence
                        candidates.append((abs(gap),row,signal,why,thesis))
                    for _,row,signal,why,thesis in sorted(candidates,key=lambda item:-item[0]):
                        direction=1 if row['gap_pp']*(1 if row['direction']=='up' else -1)>0 else -1
                        budget=min(desk.metrics(wallet)['equity']*PARAMETERS['budget_pct']*risk_feedback['size_multiplier'],state['cash'])
                        quote=desk.policy_instrument(row,direction,PARAMETERS,budget)
                        if not quote:status='Cost gate: no affordable option below 5% round-trip drag';continue
                        quantity=quote['policy_quantity']
                        thesis=expression_thesis(thesis,quote,quantity,desk,now)
                        desk.theses[row['event_id']]=thesis
                        if not thesis['eligible']:status='Thesis: '+thesis['rejection_reasons'][0];continue
                        signal_ref=dict(algo_name=algo,algo_version=signal['algo_version'],timestamp=signal['timestamp'],raw_id=signal['raw_id'],market_scope=signal['market_scope'],raw_score=signal.get('raw_score'),parameter_hash=signal.get('parameters',{}).get('parameter_hash'))
                        context=dict(event_id=row['event_id'],direction=direction,opened_at=now,signal=signal_ref,opening_gap_pp=row['gap_pp'],
                            feedback=risk_feedback,estimated_roundtrip_drag=quote['roundtrip_drag'],max_roundtrip_drag=PARAMETERS['max_roundtrip_drag'],
                            observation=row,strategy_version=VERSION,thesis=thesis)
                        desk.submit(dict(wallet=wallet,instrument=quote['instrument'],quantity=quantity,side='BUY',basis_event_id=row['event_id'],algo_signal=signal_ref,policy_context=context,
                            reason=f"{why}; GAP {row['gap_pp']:+.2f}pp; {'call' if direction>0 else 'put'} directional proxy, not event replication",
                            client_order_id=f"{wallet}-{desk.runs[wallet]['id']}-{signal['raw_id']}-{row['event_id']}-entry"),policy=True)
                        status='Entry queued: '+quote['instrument'];break
                updated=dict(desk.states[wallet],policy_status=status,risk_feedback=risk_feedback)
                if status!=desk.states[wallet].get('policy_status'):
                    desk.append(wallet,'POLICY',dict(status=status,strategy=definition(wallet)),updated)
            except (ValueError,KeyError) as error:
                status='Blocked: '+str(error)
                if status!=desk.states[wallet].get('policy_status'):
                    desk.append(wallet,'POLICY',dict(status=status),dict(desk.states[wallet],policy_status=status))
