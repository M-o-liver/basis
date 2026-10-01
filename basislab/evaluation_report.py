"""Transparent episode-level measurements, never a trading score or an edge declaration."""
from collections import Counter, defaultdict
from datetime import datetime, timezone
import itertools
from pathlib import Path
import sqlite3
import time
import numpy as np
from .algos import FAMILIES
from .evaluation import EvaluationDB, digest
from .evaluation_features import bin_index, finite, local_iv, sign
from .store import decode, encode

ALGOS=tuple(name for name in FAMILIES if name!='sequential_martingale')


def block(entry):
    return entry['asset']+':'+datetime.fromtimestamp(entry['opened_ms']/1000,timezone.utc).date().isoformat()


def interval(values, blocks, protocol):
    groups=defaultdict(list)
    for value,key in zip(values,blocks):groups[key].append(value)
    if len(groups)<protocol['minimum_dependence_blocks']:
        return dict(interval_95=None,blocks=len(groups),reason='Insufficient asset/UTC-day dependence blocks')
    ordered=[groups[k] for k in sorted(groups)]
    rng=np.random.default_rng(protocol['control_seed'])
    means=[float(np.mean([v for i in rng.integers(len(ordered),size=len(ordered)) for v in ordered[i]])) for _ in range(399)]
    return dict(interval_95=[float(x) for x in np.quantile(means,[.025,.975])],blocks=len(groups),
                method='Asset/UTC-day block bootstrap; descriptive, unadjusted; longer dependence may remain')


def summarize(pairs, protocol):
    values=[o['directional_opt_move_pp'] for e,o in pairs]
    movement=[o['delta_opt_pp'] for e,o in pairs]
    conv=[o['convergence_pp'] for e,o in pairs]
    hits=[int(v>protocol['movement_epsilon_pp']) for v in values]
    n=len(values)
    return dict(episodes=n,unique_events=len({e['event_id'] for e,o in pairs}),
        mean_poly_indicated_opt_move_pp=float(np.mean(values)) if n else None,
        median_poly_indicated_opt_move_pp=float(np.median(values)) if n else None,
        directional_hit_rate=sum(hits)/n if n else None,
        no_material_target_move=sum(abs(v)<=protocol['movement_epsilon_pp'] for v in movement),
        mean_convergence_pp=float(np.mean(conv)) if n else None,
        mean_opt_indicated_pm_move_pp=float(np.mean([o['directional_pm_move_pp'] for e,o in pairs])) if n else None,
        movement_classes=dict(Counter(o['movement_class'] for e,o in pairs)),
        target_move_uncertainty=interval(values,[block(e) for e,o in pairs],protocol),
        hit_rate_uncertainty=interval(hits,[block(e) for e,o in pairs],protocol))


def analyzer_overlap(entries, protocol):
    """Output order and missing data do not change the matrices."""
    entries=sorted(entries,key=lambda e:e['id'])
    result={name:{} for name in ALGOS}
    for a,b in itertools.product(ALGOS,repeat=2):
        common=[(e,e.get('analyzers',{}).get(a,{}),e.get('analyzers',{}).get(b,{})) for e in entries]
        scalar=lambda x:x.get('score') if finite(x.get('score')) else x.get('registered_predictor')
        numeric=[(scalar(x),scalar(y)) for e,x,y in common if finite(scalar(x)) and finite(scalar(y))]
        flags=[(e,x,y) for e,x,y in common if x.get('flagged') is not None and y.get('flagged') is not None]
        both=[(e,x,y) for e,x,y in flags if x['flagged'] and y['flagged']]
        either=sum(x['flagged'] or y['flagged'] for e,x,y in flags)
        correlation=None
        if len(numeric)>=20:
            x,y=np.array(numeric).T
            if np.std(x)>1e-12 and np.std(y)>1e-12:correlation=float(np.corrcoef(x,y)[0,1])
        result[a][b]=dict(score_pairs=len(numeric),score_correlation=correlation,
            flag_comparable_episodes=len(flags),same_episode_flagged=len(both),flag_union= either,
            flag_jaccard=len(both)/either if either else None,
            same_timestamp_within_60s=sum(abs(x['output']['timestamp']-y['output']['timestamp'])<=60000 for e,x,y in both),
            flagged_opening_gap_directions=dict(Counter('POSITIVE' if e['gap_pp']>0 else 'NEGATIVE' for e,x,y in both)),
            same_predicted_price_direction='NOT_DEFINED: anomaly scores and temporal leadership are not price-direction votes')
    return dict(matrix=result,flag_definition=protocol['analyzer_flag'],
                evidence_unit='Frozen episode entries, not frames; outputs share data and scheduling',
                temporal_score_status='Temporal regression predictors require their separate future-effective registration; no new alarm threshold. Other scores remain original analyzer raw_scores.')


def calibration(forecasts, resolutions, protocol):
    official=defaultdict(list)
    for r in resolutions:
        if r['kind']=='OFFICIAL_RESOLUTION':official[r['event_id']].append(r)
    eligible=[];excluded=Counter()
    # One first forecast per proposition, including if this function receives repeated historical rows.
    unique={}
    for f in sorted(forecasts,key=lambda x:(x['timestamp_ms'],x['raw_id'])):
        unique.setdefault(f['event_id'],f)
    for f in unique.values():
        rs=official.get(f['event_id'],[])
        if not rs:
            excluded['NOT_YET_OFFICIALLY_RESOLVED']+=1;continue
        if len({r.get('yes') for r in rs})!=1:
            excluded['CONFLICTING_OFFICIAL_RESOLUTIONS']+=1;continue
        valid=[r for r in rs if r.get('review')=='OFFICIAL_BINARY_API' and r.get('event_version')==f['event_version'] and r['available_ms']>=f['timestamp_ms']]
        if not valid:
            excluded['WORDING_VERSION_OR_REVIEW_UNAVAILABLE']+=1;continue
        eligible.append((f,valid[0]['yes']))
    curves={}
    for field in ('pm_yes','opt_yes'):
        buckets=[[] for _ in range(len(protocol['probability_bins'])-1)]
        for f,y in eligible:
            ix=bin_index(f.get(field),protocol['probability_bins'])
            if isinstance(ix,int):buckets[ix].append((f,y))
        merged=[];pending=[];start=protocol['probability_bins'][0]
        for i,items in enumerate(buckets):
            pending+=items
            if len(pending)>=protocol['calibration_min_bin_events']:
                merged.append((start,protocol['probability_bins'][i+1],pending));pending=[];start=protocol['probability_bins'][i+1]
        if pending:
            if merged:
                low,_,prior=merged.pop();merged.append((low,1,prior+pending))
            else:merged.append((start,1,pending))
        curves[field]=[dict(low=low,high=min(high,1),unique_resolved_events=len(items),
            predicted_mean=float(np.mean([f[field] for f,y in items])),realized_frequency=float(np.mean([y for f,y in items])),
            brier_mean=float(np.mean([(f[field]-y)**2 for f,y in items])),
            status='SUFFICIENT_BIN_COUNT' if len(items)>=protocol['calibration_min_bin_events'] else 'CALIBRATION INSUFFICIENT',
            uncertainty=interval([y for f,y in items],[f['asset']+':'+datetime.fromtimestamp(f['timestamp_ms']/1000,timezone.utc).date().isoformat() for f,y in items],protocol))
            for low,high,items in merged]
    equity=[(f,y) for f,y in eligible if f['asset'] not in ('BTC','ETH')]
    groups=defaultdict(list)
    for f,y in equity:
        tte=(f['expiry']-f['timestamp_ms'])/3600000
        distance=abs(np.log(f['strike_or_threshold']/f['spot'])) if f.get('spot') and f.get('strike_or_threshold') else None
        key=encode([f['event_type'],f['direction'],bin_index(distance,protocol['distance_bins']),
            bin_index(tte,protocol['time_to_expiry_bins_hours']),bin_index(local_iv(f),protocol['iv_bins'])])
        groups[key].append((f,y))
    proxies=[dict(stratum=json_decode(key),unique_resolved_events=len(items),
        status='DESCRIPTIVE_EMPIRICAL_RESIDUALS' if len(items)>=protocol['equity_proxy_min_events'] else 'CALIBRATION INSUFFICIENT',
        residual_p10_p90=[float(x) for x in np.quantile([f['opt_yes']-y for f,y in items],[.10,.90])] if len(items)>=protocol['equity_proxy_min_events'] else None)
        for key,items in sorted(groups.items())]
    return dict(unique_forecast_events=len(unique),unique_resolved_events=len(eligible),exclusions=dict(excluded),
        curves=curves,equity_proxy=dict(status='CALIBRATION INSUFFICIENT' if len(equity)<protocol['equity_proxy_min_events'] else 'DESCRIPTIVE; proxy error is not a trading edge',
            unique_resolved_events=len(equity),strata=proxies),
        caveat='Options estimates are risk-neutral/model proxies, not necessarily physical beliefs. Calibration, repricing and trading P&L are separate.')


def json_decode(value):
    import json
    return json.loads(value)


def vector(entry):
    features=entry.get('features',{})
    values=[entry.get('gap_pp'),entry.get('relative_gap'),features.get('spot_return'),
        np.log(max(entry['time_to_expiry_ms']/3600000,1e-6)),local_iv(entry),
        1 if entry['direction']=='up' else -1,entry.get('neighbors',{}).get('agreement')]
    return [float(v) for v in values] if all(finite(v) for v in values) else None


def prequential(pairs, protocol):
    ordered=sorted(pairs,key=lambda x:(x[0]['opened_ms'],x[0]['opening_raw_id'],x[0]['id']))
    output={}
    for extra in ('PM_SPECIFIC_MOVEMENT',)+ALGOS:
        rows=[];excluded=Counter()
        for e,o in ordered:
            x=vector(e)
            analyzer=e.get('analyzers',{}).get(extra,{})
            v=(e.get('features',{}).get('pm_change_pp') if extra=='PM_SPECIFIC_MOVEMENT' else
               analyzer.get('score') if finite(analyzer.get('score')) else analyzer.get('registered_predictor'))
            if x is None or not finite(v):excluded['MISSING_FROZEN_FEATURE']+=1;continue
            rows.append((e,o,x,float(v)))
        predictions=[]
        for e,o,x,v in rows:
            train=[(te,to,tx,tv) for te,to,tx,tv in rows if to['due_ms']<e['opened_ms'] and
                   to.get('asof_raw_id',2**63-1)<e['opening_raw_id']]
            if len(train)<protocol['regression_min_training'] or len({te['event_id'] for te,to,tx,tv in train})<protocol['regression_min_events']:
                continue
            target=np.array([to['delta_opt_pp'] for te,to,tx,tv in train])
            pred=[];ranks=[]
            for extended in (False,True):
                matrix=np.array([tx+([tv] if extended else []) for te,to,tx,tv in train])
                test=np.array(x+([v] if extended else []))
                center=matrix.mean(axis=0);scale=matrix.std(axis=0);scale[scale<1e-12]=1
                design=np.column_stack([np.ones(len(matrix)),(matrix-center)/scale])
                beta,_,rank,_=np.linalg.lstsq(design,target,rcond=None)
                pred.append(float(np.r_[1,(test-center)/scale]@beta));ranks.append(int(rank))
            y=o['delta_opt_pp']
            predictions.append(dict(entry_id=e['id'],block=block(e),base_error=abs(pred[0]-y),extended_error=abs(pred[1]-y),
                base_direction_correct=(sign(pred[0])==sign(y)) if abs(y)>protocol['movement_epsilon_pp'] else None,
                extended_direction_correct=(sign(pred[1])==sign(y)) if abs(y)>protocol['movement_epsilon_pp'] else None,
                base_prediction_pp=pred[0],extended_prediction_pp=pred[1],target_pp=y,rank=ranks))
        improvements=[p['base_error']-p['extended_error'] for p in predictions]
        available=[p for p in predictions if p['base_direction_correct'] is not None]
        output[extra]=dict(status='INSUFFICIENT_PREQUENTIAL_SAMPLE' if len(predictions)<protocol['minimum_episodes'] else 'DESCRIPTIVE_OUT_OF_SAMPLE',
            complete_feature_episodes=len(rows),test_episodes=len(predictions),excluded=dict(excluded),
            mean_mae_improvement_pp=float(np.mean(improvements)) if improvements else None,
            base_mae_pp=float(np.mean([p['base_error'] for p in predictions])) if predictions else None,
            extended_mae_pp=float(np.mean([p['extended_error'] for p in predictions])) if predictions else None,
            nonflat_direction_tests=len(available),base_direction_hit_rate=float(np.mean([p['base_direction_correct'] for p in available])) if available else None,
            extended_direction_hit_rate=float(np.mean([p['extended_direction_correct'] for p in available])) if available else None,
            uncertainty=interval(improvements,[p['block'] for p in predictions],protocol),
            note='Expanding causal replay prediction; training labels mature before test entry. Same complete cases for BASE and BASE+feature. No probability output, AUC or log-loss fabricated.')
    return output


def spot_conditioned(pairs, protocol):
    strata=defaultdict(list)
    for e,o in pairs:
        r=o.get('spot_return')
        key='MISSING' if not finite(r) else 'DOWN' if r<-.002 else 'UP' if r>.002 else 'FLAT_0.2_PERCENT'
        strata[key].append((e,o))
    complete=[(e,o,vector(e)) for e,o in pairs if vector(e) is not None and finite(o.get('spot_return'))]
    regression=dict(status='INSUFFICIENT_SAMPLE',complete_episodes=len(complete),
        note='Future spot is retrospective conditioning, never a forecast input or causal identification')
    if len(complete)>=protocol['regression_min_training'] and len({e['event_id'] for e,o,x in complete})>=protocol['regression_min_events']:
        # Prior spot, expiry, IV, direction and neighbors are controls. Compare without/with opening GAP.
        x=np.array([[v[2],v[3],v[4],v[5],v[6],o['spot_return'],v[0]] for e,o,v in complete])
        y=np.array([o['delta_opt_pp'] for e,o,v in complete]);center=x.mean(axis=0);scale=x.std(axis=0);scale[scale<1e-12]=1
        z=(x-center)/scale;design=np.column_stack([np.ones(len(x)),z])
        beta,_,rank,_=np.linalg.lstsq(design,y,rcond=None)
        base=design[:,:-1];bb=np.linalg.lstsq(base,y,rcond=None)[0]
        regression.update(status='DESCRIPTIVE_ASSOCIATION',gap_coefficient_pp_per_gap_pp=float(beta[-1]/scale[-1]),
            residual_mse_without_gap=float(np.mean((y-base@bb)**2)),residual_mse_with_gap=float(np.mean((y-design@beta)**2)),
            design_rank=int(rank),columns=['intercept','prior_spot_return','log_tte_hours','IV','event_direction','neighbor_agreement','future_spot_return','opening_gap_pp'],
            confidence_interval=None,uncertainty_reason='No iid standard errors; use dependence-aware prospective blocks before inference')
    return dict(strata={k:summarize(v,protocol) for k,v in sorted(strata.items())},regression=regression)


def paper_overlap(tape_path, started):
    path=Path(tape_path).with_suffix('.paper.sqlite3')
    if not path.exists():return dict(status='NO_PAPER_LEDGER',matrix={})
    db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
    try:
        rows=db.execute("SELECT timestamp_ms,data FROM ledger WHERE kind='ORDER' AND timestamp_ms>=? ORDER BY id DESC LIMIT 5000",(started,)).fetchall()
    finally:db.close()
    orders=[]
    for t,body in rows:
        data=decode(body);order=data.get('order',{})
        name=(order.get('algo_signal') or {}).get('algo_name')
        if name in ALGOS:
            orders.append((t,name,order.get('instrument'),order.get('side'),order.get('basis_event_id')))
    matrix={a:{b:0 for b in ALGOS} for a in ALGOS}
    for a,b in itertools.combinations(ALGOS,2):
        count=sum(any(abs(t-u)<=60000 and instrument==other and side==other_side and event==other_event
            for u,name,other,other_side,other_event in orders if name==b)
            for t,name,instrument,side,event in orders if name==a)
        matrix[a][b]=matrix[b][a]=count
    return dict(status='DESCRIPTIVE_PAPER_ORDER_OVERLAP',order_rows=len(orders),cap=5000,matrix=matrix,
        note='Actual candidate orders sharing instrument/side/event within 60s; counts are asymmetric-match groups symmetrized for display, not independent experiments. Policies unchanged.')


def report(tape, evaluation_id=None, mode='PROSPECTIVE', episode_id=None):
    path=Path(tape).with_suffix('.evaluation.sqlite3')
    if not path.exists():return dict(status='NOT_REGISTERED',reason='Use basis evaluate --start while the recorder is live.')
    db=EvaluationDB(tape,read_only=True)
    try:
        db.db.execute('BEGIN')
        campaign=db.campaign(evaluation_id,mode)
        if not campaign:return dict(status='NOT_REGISTERED',mode=mode)
        cid=campaign['evaluation_id'];protocol=campaign['protocol']
        campaign={k:v for k,v in campaign.items() if k!='initial_panel'}
        entries=[decode(r[0]) for r in db.db.execute('SELECT data FROM entries WHERE campaign_id=? ORDER BY opened_ms,id',(cid,))]
        outcomes={}
        for r in db.db.execute('SELECT o.* FROM outcomes o JOIN entries e ON e.id=o.entry_id WHERE e.campaign_id=?',(cid,)):
            outcomes[(r['entry_id'],r['name'])]=dict(decode(r['data']),due_ms=r['due_ms'],status=r['status'])
        if episode_id:
            entry=next((e for e in entries if e['id']==episode_id),None)
            return dict(campaign=campaign,entry=entry,outcomes={h:o for (i,h),o in outcomes.items() if i==episode_id},
                error=None if entry else 'Unknown evaluation episode')
        matches={r['entry_id']:dict(decode(r['data']),control_id=r['control_id']) for r in db.db.execute('SELECT m.* FROM matches m JOIN entries e ON e.id=m.entry_id WHERE e.campaign_id=?',(cid,))}
        scheduled={(r['entry_id'],r['name']) for r in db.db.execute('SELECT h.* FROM horizons h JOIN entries e ON e.id=h.entry_id WHERE e.campaign_id=?',(cid,))}
        forecasts=[decode(r[0]) for r in db.db.execute('SELECT data FROM forecasts WHERE campaign_id=?',(cid,))]
        hypotheses=[decode(r[0]) for r in db.db.execute('SELECT data FROM hypotheses WHERE campaign_id=? ORDER BY version',(cid,))] if db.db.execute("SELECT 1 FROM sqlite_master WHERE name='hypotheses'").fetchone() else []
        tracked={e['event_id'] for e in entries}|{f['event_id'] for f in forecasts}
        resolutions=[decode(r[0]) for r in db.db.execute('SELECT data FROM resolutions') if decode(r[0])['event_id'] in tracked]
        saved=db.db.execute('SELECT * FROM progress WHERE campaign_id=?',(cid,)).fetchone()
        progress=decode(saved['data']) if saved else {}
        evidence=[dict(kind=r['kind'],timestamp_ms=r['timestamp_ms'],**decode(r['data'])) for r in db.db.execute('SELECT * FROM evidence WHERE campaign_id=? ORDER BY id',(cid,))]
        db.db.execute('COMMIT')
    finally:db.close()
    episodes=[e for e in entries if e['kind']!='CONTROL'];eligible=[e for e in episodes if e['eligible']]
    primary=[(e,outcomes[(e['id'],protocol['primary_horizon'])]) for e in eligible
             if outcomes.get((e['id'],protocol['primary_horizon']),{}).get('status')=='OBSERVED']
    blocks={block(e) for e in eligible};unique={e['event_id'] for e in eligible}
    status='INSUFFICIENT_PROSPECTIVE_SAMPLE' if len(primary)<protocol['minimum_episodes'] or len(unique)<protocol['minimum_unique_events'] or len(blocks)<protocol['minimum_dependence_blocks'] else 'DESCRIPTIVE_PROSPECTIVE_RESULTS; EDGE_NOT_AUTOMATICALLY_PROVEN'
    if progress.get('stopped_versions'):status='ELIGIBILITY_STOPPED; SCIENTIFIC_VERSION_OR_ARCHIVE_CONFLICT'
    if mode!='PROSPECTIVE':status='EXPLORATORY / HISTORICAL; NOT CONFIRMATORY'
    by_id={e['id']:e for e in entries};horizons={}
    names=list(protocol['horizons'])+list(protocol['deferred_horizons'])+['FIRST_AVAILABLE_PROXY','expiry','resolution']
    for name in names:
        pairs=[(e,outcomes[(e['id'],name)]) for e in eligible if outcomes.get((e['id'],name),{}).get('status')=='OBSERVED' and 'directional_opt_move_pp' in outcomes[(e['id'],name)]]
        sampled=[outcomes[(e['id'],name)] for e in episodes if (e['id'],name) in outcomes]
        paired=[];missing_controls=Counter()
        for e,o in pairs:
            match=matches.get(e['id'],{});control_id=match.get('control_id');control_outcome=outcomes.get((control_id,name))
            if not control_id:missing_controls['NO_MATCH']+=1;continue
            if not control_outcome or control_outcome['status']!='OBSERVED':missing_controls['CONTROL_HORIZON_MISSING_OR_PENDING']+=1;continue
            c=by_id[control_id];cy=sign(e['gap_pp'])*control_outcome['delta_opt_pp']
            paired.append((e,o,c,cy))
        differences=[o['directional_opt_move_pp']-cy for e,o,c,cy in paired]
        use=Counter(c['id'] for e,o,c,cy in paired)
        matched=dict(treatment_episodes=len(paired),unique_control_moments=len(use),maximum_control_reuse=max(use.values(),default=0),
            treatment_hit_rate=float(np.mean([o['directional_opt_move_pp']>protocol['movement_epsilon_pp'] for e,o,c,cy in paired])) if paired else None,
            control_hit_rate=float(np.mean([cy>protocol['movement_epsilon_pp'] for e,o,c,cy in paired])) if paired else None,
            control_mean_target_move_pp=float(np.mean([cy for e,o,c,cy in paired])) if paired else None,
            mean_matched_excess_move_pp=float(np.mean(differences)) if differences else None,
            uncertainty=interval(differences,[block(e) for e,o,c,cy in paired],protocol),missing=dict(missing_controls),
            note='Same treatment GAP direction applied to prior matched random controls. Simultaneous market shocks and control reuse remain dependent.')
        horizon=dict(summarize(pairs,protocol),missing=len([o for o in sampled if o['status']=='MISSING']),
            missing_reasons=dict(Counter(o.get('reason','UNKNOWN') for o in sampled if o['status']=='MISSING')),
            pending=sum((e['id'],name) not in outcomes for e in episodes if (e['id'],name) in scheduled or name=='resolution' or name=='FIRST_AVAILABLE_PROXY' and e['kind']=='DEFERRED_RESPONSE'),
            matched_controls=matched)
        if name in protocol['horizons']:
            strat=defaultdict(list)
            for e,o in pairs:
                strat[e['session']['regime']].append((e,o))
                if e['session']['expiry_near']:strat['EXPIRY_NEAR'].append((e,o))
            horizon['session_regimes']={k:summarize(v,protocol) for k,v in sorted(strat.items())}
        horizons[name]=horizon
    baselines={}
    selections={
        'A_SIGNED_GAP':lambda e:True,
        'B_ABSOLUTE_GAP_4_TO_8_PP':lambda e:4<=abs(e['gap_pp'])<8,
        'B_ABSOLUTE_GAP_8_PLUS_PP':lambda e:abs(e['gap_pp'])>=8,
        'C_REL_30_PERCENT':lambda e:finite(e.get('relative_gap')) and e['relative_gap']>=protocol['rel_threshold'],
        'D_FRESH_PM_IN_GAP_DIRECTION':lambda e:finite(e.get('features',{}).get('pm_change_pp')) and sign(e['gap_pp'])*e['features']['pm_change_pp']>=protocol['fresh_pm_movement_pp'],
        'E_NEIGHBOR_MAJORITY_AGREEMENT':lambda e:finite(e.get('neighbors',{}).get('agreement')) and e['neighbors']['agreement']>.5,
        'F_PM_TRAVEL_DOMINANCE':lambda e:finite(e.get('features',{}).get('pm_travel')) and e['features']['pm_travel']>=protocol['pm_travel_threshold']}
    for name,predicate in selections.items():baselines[name]=summarize([(e,o) for e,o in primary if predicate(e)],protocol)
    baselines['G_MATCHED_RANDOM']=horizons[protocol['primary_horizon']]['matched_controls']
    baselines['H_NO_PREDICTION']=dict(predicted_target_move_pp=0,active_predictions=0,note='No prediction benchmark; cash return/P&L is not evaluated')
    curve_units={}
    for e,o in primary:
        if o.get('curve',{}).get('nodes',0)<2:continue
        key=encode([e['neighbors']['group'],e['opened_ms']//protocol['control_period_ms']])
        curve_units.setdefault(key,(e,o['curve']))
    curves=list(curve_units.values())
    return dict(status=status,campaign=campaign,elapsed_seconds=(time.time_ns()//1000000-campaign['started_ms'])/1000,
        evidence_units=dict(raw_id_last_processed=progress.get('raw_id'),compact_frames_processed=progress.get('frames_processed',0),
            raw_records_in_processed_prefix=max(0,progress.get('raw_id',0)-campaign['first_eligible_global_raw_id']+1),
            observed_episode_horizon_outcomes=sum(o['status']=='OBSERVED' for (key,name),o in outcomes.items() if by_id[key]['kind']!='CONTROL'),
            episode_entries=len(episodes),eligible_episode_entries=len(eligible),unique_events=len(unique),asset_day_blocks=len(blocks),
            control_moments=sum(e['kind']=='CONTROL' for e in entries),unique_forecast_events=len(forecasts),
            ineligible_entry_reasons=dict(Counter(e.get('ineligible_reason') for e in episodes if not e['eligible'])),
            entry_origins=dict(Counter(e.get('entry_origin','DEFERRED_RESPONSE') for e in episodes)),
            preboundary_open_gaps=sum(bool(e.get('left_censored')) for e in progress.get('episodes',{}).values()),
            warning='Frames and neighboring strikes are not independent trials. Episode reopens may also share a regime.'),
        worker=dict(updated_ms=saved['updated_ms'] if saved else None,frame_id=saved['frame_id'] if saved else None,
            last_frame_ms=progress.get('last_timestamp_ms'),stopped_versions=progress.get('stopped_versions',{})),
        outcomes_by_horizon=horizons,baselines=baselines,spot_conditioned=spot_conditioned(primary,protocol),
        analyzer_overlap=analyzer_overlap(eligible,protocol),paper_candidate_overlap=paper_overlap(tape,campaign['started_ms']),
        incremental_analyzer_value=prequential(primary,protocol),calibration=calibration(forecasts,resolutions,protocol),
        curve_outcomes=dict(family_half_hour_units=len(curves),
            mean_opt_toward_opening_pm_shape_pp=float(np.mean([c['opt_toward_opening_pm_pp'] for e,c in curves])) if curves else None,
            mean_pm_toward_opening_opt_shape_pp=float(np.mean([c['pm_toward_opening_opt_pp'] for e,c in curves])) if curves else None,
            mean_curve_convergence_pp=float(np.mean([c['convergence_pp'] for e,c in curves])) if curves else None,
            note='Common comparable nodes only; one opening per family/half-hour, never a claim that strikes are independent'),
        resolution_states=dict(Counter(r['kind'] for r in resolutions)),registered_hypotheses=hypotheses,operational_evidence=evidence[-30:],
        limitations=['Prospective sample and independent dependence blocks must mature before inference.',
            'Convergence is not causal leadership; shared outside information can move both markets.',
            'Controls are exact-bin, prior-available matches; sparse coverage is explicit, not silently relaxed.',
            'Temporal regression predictors are separately future-registered; pre-registration entries remain without them. No scalar alarm or trading direction is invented.',
            'Quote/model quality exclusions and missing outcomes are retained; equities remain delayed proxies.',
            'Fixed horizons are causal one-second-frame tests. Frozen native salient references preserve exact timing context; no subsecond edge is inferred from a frame.',
            'All secondary horizons, bins and analyzer comparisons are descriptive and unadjusted for multiple comparisons.',
            'Backfilled entries are causal reconstructions after protocol registration, not claims of live model decisions.'])


def text_report(data):
    if 'campaign' not in data:return encode(data)
    c=data['campaign'];units=data.get('evidence_units',{})
    lines=[f"BASIS / EVALUATE  {data['status']}",f"{c['evaluation_id']} v{c['evaluation_version']}  {c['mode']}",
        f"Frozen {c['evaluation_started_at']} | raw >= {c['first_eligible_global_raw_id']} frame >= {c['first_eligible_frame_id']}",
        f"Elapsed {data.get('elapsed_seconds',0)/3600:.2f}h | {units.get('eligible_episode_entries',0)} eligible entries / {units.get('unique_events',0)} events / {units.get('asset_day_blocks',0)} asset-day blocks",
        f"Processed {units.get('compact_frames_processed',0)} frames; frames are NOT the statistical sample size.",
        'HORIZON       N   EVENTS    POLY->OPT pp     HIT %   CONVERGENCE pp   MISSING/PENDING']
    fmt=lambda x: '--' if x is None else f'{x:.3f}'
    for name,h in data.get('outcomes_by_horizon',{}).items():
        if name=='resolution':continue
        lines.append(f"{name:<12} {h['episodes']:>5} {h['unique_events']:>8} {fmt(h['mean_poly_indicated_opt_move_pp']):>15} {fmt(h['directional_hit_rate']*100 if h['directional_hit_rate'] is not None else None):>9} {fmt(h['mean_convergence_pp']):>16} {h['missing']}/{h['pending']}")
    primary=data.get('outcomes_by_horizon',{}).get(c['protocol']['primary_horizon'],{}).get('matched_controls',{})
    lines.extend([f"Primary {c['protocol']['primary_horizon']} matched pairs: {primary.get('treatment_episodes',0)} | unique controls {primary.get('unique_control_moments',0)} | excess movement {fmt(primary.get('mean_matched_excess_move_pp'))}pp",
        'PRIMARY BASELINES (30m):'])
    for name,b in data.get('baselines',{}).items():
        if 'episodes' in b:lines.append(f"  {name}: N={b['episodes']} mean={fmt(b['mean_poly_indicated_opt_move_pp'])}pp hit={fmt(b['directional_hit_rate'])}")
    lines.append('SPOT / SESSION: '+encode(data.get('spot_conditioned',{}).get('regression',{})))
    lines.append('INCREMENTAL: '+', '.join(f'{k}={v["status"]} (N={v["test_episodes"]})' for k,v in data.get('incremental_analyzer_value',{}).items()))
    cal=data.get('calibration',{});lines.append(f"Calibration: {cal.get('unique_resolved_events',0)} unique resolved events | equity {cal.get('equity_proxy',{}).get('status','--')}")
    lines.append('Missing reasons: '+encode({k:v['missing_reasons'] for k,v in data.get('outcomes_by_horizon',{}).items() if v['missing_reasons']}))
    lines.append('No informational edge is declared from these descriptive measurements. --json exposes controls, overlap, curves, quality and frozen protocol; --episode inspects provenance.')
    return '\n'.join(lines)
