"""Exploratory gap response on causal tape windows; frame counts are not N."""
from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
import numpy as np
from .gap_math import HORIZONS, horizon_outcome, logit
from .store import decode, encode

VERSION='gap-response-1.0'
DAY=86400000


def lower_id(db, table, column, target, low, high):
    # Recorder availability time is monotone. Search PKs, never scan the monolith.
    while low<high:
        mid=(low+high)//2
        row=db.execute(f'SELECT id,{column} FROM {table} WHERE id>=? ORDER BY id LIMIT 1',(mid,)).fetchone()
        if row is None or row[1]>=target:high=mid
        else:low=row[0]+1
    return low


class HotTape:
    def __init__(self,tape):
        self.tape=str(Path(tape).resolve());self.sources=[]
        pointer=Path(self.tape+'.storage.json')
        if pointer.exists():
            manifest=Path(json.loads(pointer.read_text())['manifest']);m=json.loads(manifest.read_text())
            if m.get('legacy'):self.add(Path(m['legacy']['path']),False,True,m['legacy']['limits']['observations'])
            for s in m['segments']:self.add(manifest.parent/s['file'],True,s.get('closed',False),s.get('limits',{}).get('observations'))
        else:self.add(Path(self.tape),False,False,None)
        self.start=min(s['first_ms'] for s in self.sources);self.end=max(s['last_ms'] for s in self.sources)

    def add(self,path,v2,closed,limit):
        db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro'+('&immutable=1' if closed else ''),uri=True)
        db.row_factory=sqlite3.Row;table='frames' if v2 else 'observations';column='timestamp_wall' if v2 else 'timestamp_ms'
        first=db.execute(f'SELECT id,{column} FROM {table} ORDER BY id LIMIT 1').fetchone()
        last=db.execute(f'SELECT id,{column} FROM {table} '+('WHERE id<=? ' if limit else '')+'ORDER BY id DESC LIMIT 1',(limit,) if limit else ()).fetchone()
        if not first or not last:db.close();return
        self.sources.append(dict(db=db,path=str(path),v2=v2,table=table,column=column,first_id=first[0],last_id=last[0],first_ms=first[1],last_ms=last[1]))

    def close(self):
        for s in self.sources:s['db'].close()

    def native(self,scope,start,end):
        for s in self.sources:
            if not s['v2'] or s['last_ms']<start or s['first_ms']>end:continue
            for r in s['db'].execute("SELECT raw_id,timestamp_ms,kind,before_value,after_value FROM salient WHERE scope=? AND timestamp_ms BETWEEN ? AND ? AND kind IN ('pm_jump','opt_jump') ORDER BY timestamp_ms,raw_id",(scope,start,end)):
                yield dict(raw_id=r['raw_id'],timestamp=r['timestamp_ms'],leg=r['kind'][:-5],before=decode(r['before_value']),after=decode(r['after_value']))

    def rows(self,start,end):
        for s in self.sources:
            if s['last_ms']<start or s['first_ms']>end:continue
            db=s['db'];lo=lower_id(db,s['table'],s['column'],start,s['first_id'],s['last_id']+1)
            hi=lower_id(db,s['table'],s['column'],end+1,lo,s['last_id']+1)
            if s['v2']:
                events={r['id']:decode(r['data']) for r in db.execute('SELECT id,data FROM event_versions')}
                blobs={}
                def blob(ref):
                    if ref not in blobs:blobs[ref]=decode(db.execute('SELECT data FROM blobs WHERE id=?',(ref,)).fetchone()[0])
                    return blobs[ref]
                query='SELECT id,raw_id,event_version_id,meta_id,model_id,context_id,input_refs,timestamp_wall,pm_yes,opt_yes,spot,pm_timestamp,opt_timestamp,options_received_ms FROM frames WHERE id>=? AND id<? ORDER BY id'
                for f in db.execute(query,(lo,hi)):
                    if f['opt_yes'] is None or f['pm_yes'] is None:continue
                    meta=blob(f['meta_id']);model=blob(f['model_id']);e=events[f['event_version_id']]
                    context=blob(f['context_id']) if f['context_id'] else {}
                    yield dict(e,**{k:f[k] for k in ('id','raw_id','timestamp_wall','pm_yes','opt_yes','spot','pm_timestamp','opt_timestamp','options_received_ms')},
                        source_state=meta.get('source_state'),pm_source=meta.get('pm_source'),quality_flags=meta.get('quality_flags',[]),
                        local_iv=model.get('iv') or meta.get('surface_features',{}).get('local_iv'),
                        input_refs=dict(zip(('catalog','mapping','pm','options','spot','history'),json.loads(f['input_refs']))),
                        regime=context.get('market_state','BOTH_OPEN'),storage_version=2)
            else:
                # Discover active identities throughout the window without DISTINCT
                # over 67M rows. Each event then uses its covering event/time index.
                ids=set()
                for target in range(start,end+1,60000):
                    at=lower_id(db,s['table'],s['column'],target,lo,hi)
                    ids.update(r[0] for r in db.execute('SELECT event_id FROM observations WHERE id>=? AND id<?',(at,min(hi,at+256))))
                for event in sorted(ids):
                    query='SELECT id,data FROM observations WHERE id IN (SELECT MAX(id) FROM observations INDEXED BY obs_event WHERE event_id=? AND timestamp_ms BETWEEN ? AND ? AND id>=? AND id<? GROUP BY timestamp_ms/1000) ORDER BY id'
                    for r in db.execute(query,(event,start,end,lo,hi)):
                        row=decode(r['data'])
                        if row.get('opt_yes') is None or row.get('pm_yes') is None:continue
                        row.update(id=r['id'],local_iv=row.get('model_inputs',{}).get('iv') or row.get('surface_features',{}).get('local_iv'),
                            regime=row.get('underlying_context',{}).get('market_state','BOTH_OPEN'),storage_version=1)
                        yield row


def windows(start,end,per_day=1):
    result={(start,min(end,start+3600000)),(max(start,end-3600000),end)}
    for day in range(start//DAY,end//DAY+1):
        for i in range(per_day):
            offset=int.from_bytes(hashlib.sha256(f'{VERSION}:{day}:{i}'.encode()).digest()[:8],'big')%(23*3600000)
            a=max(start,day*DAY+offset);b=min(end,a+3600000)
            if b-a>=1800000:result.add((a,b))
    # Merge overlaps so the same opening never enters twice.
    merged=[]
    for a,b in sorted(result):
        if merged and a<=merged[-1][1]:merged[-1]=(merged[-1][0],max(b,merged[-1][1]))
        else:merged.append((a,b))
    return merged


def bin_gap(value):
    a=abs(value)
    return '0-1pp' if a<1 else '1-2pp' if a<2 else '2-4pp' if a<4 else '4-8pp' if a<8 else '8pp+'


def bin_odds(value):
    a=abs(value)
    return '0-.25' if a<.25 else '.25-.5' if a<.5 else '.5-1' if a<1 else '1-2' if a<2 else '2+'


def block_summary(rows):
    if not rows:return dict(n_blocks=0,observations=0,status='INSUFFICIENT')
    clusters=defaultdict(list)
    for r in rows:clusters[r['block']].append(r)
    values=[]
    for group in clusters.values():
        values.append([np.mean([r['opt_toward']>0 for r in group]),np.mean([r['pm_toward']>0 for r in group]),
            np.mean([r['opt_toward'] for r in group]),np.mean([r['pm_toward'] for r in group]),
            np.mean([r['convergence'] for r in group])])
    a=np.asarray(values);means=a.mean(axis=0)
    ci=None
    if len(a)>=10:
        rng=np.random.default_rng(7);draws=np.asarray([a[rng.integers(len(a),size=len(a))].mean(axis=0) for _ in range(256)])
        ci=np.quantile(draws,[.025,.975],axis=0).T.tolist()
    fractions=[r for r in rows if abs(r['gap_pp'])>=.1]
    return dict(n_blocks=len(clusters),observations=len(rows),unique_events=len({r['event_id'] for r in rows}),
        opt_toward_rate=float(means[0]),pm_toward_rate=float(means[1]),mean_opt_move_pp=float(means[2]),
        mean_pm_move_pp=float(means[3]),mean_convergence_pp=float(means[4]),
        median_opt_move_pp=float(np.median([r['opt_toward'] for r in rows])),
        mean_opt_close_fraction=float(np.mean([r['opt_fraction'] for r in fractions])) if fractions else None,
        mean_pm_close_fraction=float(np.mean([r['pm_fraction'] for r in fractions])) if fractions else None,
        fraction_min_opening_gap_pp=.1,ci95_event_day_bootstrap=ci,
        ci_columns=['opt_toward_rate','pm_toward_rate','mean_opt_move_pp','mean_pm_move_pp','mean_convergence_pp'],
        no_opt_movement_rate=sum(r['opt_toward']==0 for r in rows)/len(rows),
        status='EXPLORATORY' if len(clusters)>=10 else 'SMALL_SAMPLE')


def regression(rows,contemporaneous_spot=False):
    complete=[r for r in rows if r.get('iv') and r.get('spot_recent') is not None and (not contemporaneous_spot or r.get('future_spot_return') is not None)]
    if len(complete)<30:return dict(status='INSUFFICIENT',observations=len(complete))
    names=['intercept','opening_log_odds_gap','prior_60s_spot_return','IV','log_days_to_expiry','log_threshold_distance','up_direction']
    def vector(r):
        return [1,r['z'],r['spot_recent'],r['iv'],math.log(max(1e-6,r['tte_days'])),r['distance'],1 if r['direction']=='up' else -1]+([r['future_spot_return']] if contemporaneous_spot else [])
    if contemporaneous_spot:names.append('contemporaneous_spot_return_EX_POST')
    original=np.asarray([vector(r) for r in complete]);active=[0]+[i for i in range(1,original.shape[1]) if np.std(original[:,i])>1e-12]
    if 1 not in active:return dict(status='INSUFFICIENT_GAP_VARIATION',observations=len(complete))
    scales=np.std(original[:,active],axis=0);scales[0]=1
    centers=np.mean(original[:,active],axis=0);centers[0]=0
    X=(original[:,active]-centers)/scales;z_index=active.index(1);groups=defaultdict(list)
    for i,r in enumerate(complete):groups[r['block']].append(i)
    result=dict(observations=len(complete),n_blocks=len(groups),controls=[names[i] for i in active],constant_controls_dropped=[names[i] for i in range(len(names)) if i not in active],status='EXPLORATORY',
                interpretation='Ex-post spot attribution, not a predictive control' if contemporaneous_spot else 'Opening-only controls; exploratory association, not causality')
    for target,label,sign in [('dy','options_beta',1),('dx','poly_reversion_b',-1)]:
        y=np.array([r[target] for r in complete]);coef,_,rank,_=np.linalg.lstsq(X,y,rcond=None)
        residual=y-X@coef;bread=np.linalg.pinv(X.T@X);meat=np.zeros((X.shape[1],X.shape[1]))
        for indices in groups.values():
            score=X[indices].T@residual[indices];meat+=np.outer(score,score)
        se=np.sqrt(np.maximum(0,np.diag(bread@meat@bread)))
        estimate=float(sign*coef[z_index]/scales[z_index]);error=se[z_index]/scales[z_index]
        interval=[float(estimate-1.96*error),float(estimate+1.96*error)] if len(groups)>=20 and rank==X.shape[1] else None
        result[label]=dict(coefficient=estimate,ci95_cluster=interval,rank=int(rank),r2=float(1-np.sum(residual**2)/max(1e-20,np.sum((y-y.mean())**2))))
    return result


def lifecycle_summary(rows):
    clusters=defaultdict(list)
    for r in rows:clusters[r['block']].append(r)
    if not clusters:return dict(n_blocks=0,half_life_s=None,status='INSUFFICIENT_CONTINUOUS_HISTORY')
    weighted=[];reach=close=widen=double=0
    for group in clusters.values():
        weight=1/(len(group)*len(clusters))
        for r in group:
            if r['half_life_s'] is not None:weighted.append((r['half_life_s'],weight));reach+=weight
            close+=weight*r['close_before_double'];widen+=weight*r['close_before_widening'];double+=weight*r['doubled']
    cumulative=0;median=None
    for value,weight in sorted(weighted):
        cumulative+=weight
        if cumulative>=.5:median=value;break
    return dict(n_blocks=len(clusters),observations=len(rows),half_life_s=median,half_reached_rate=reach,
                p_close_before_double=close,p_close_before_widening=widen,p_gap_doubles=double,
                median_time_to_max_s=float(np.median([np.median([r['time_to_max_s'] for r in group]) for group in clusters.values()])),
                status='OBSERVED' if median is not None else 'MEDIAN_NOT_REACHED_WITHIN_30M')


def study(tape,per_day=1,stride_seconds=60,start=None,end=None,progress=True):
    started=time.monotonic();reader=HotTape(tape)
    start=max(reader.start,start or reader.start);end=min(reader.end,end or reader.end)
    plan=windows(start,end,per_day);outcomes=defaultdict(list);life=[];event_studies=defaultdict(list)
    scanned=0;excluded=Counter();trade_openings=[]
    try:
        for region,(a,b) in enumerate(plan):
            panel=defaultdict(list)
            for r in reader.rows(a,b):
                scanned+=1
                if r.get('source_state') not in ('OK','PROXY','CLOSED') or 'WIDE_PM_BOOK' in r.get('quality_flags',[]):
                    excluded[r.get('source_state','UNKNOWN')]+=1;continue
                if not all(v is not None and math.isfinite(v) for v in (r.get('pm_yes'),r.get('opt_yes'),r.get('spot'))):continue
                key=(r['event_id'],r.get('mapping_hash'),r['expiry'],r['event_type'],r['direction'])
                panel[key].append(r)
            for key,rows in panel.items():
                rows.sort(key=lambda r:(r['timestamp_wall'],r['raw_id'],r['id']));times=[r['timestamp_wall'] for r in rows]
                causal_keys=[(r['timestamp_wall'],r['raw_id']) for r in rows]
                for jump in reader.native(key[0],a,b):
                    i=bisect_right(causal_keys,(jump['timestamp'],jump['raw_id']))-1
                    if i<0 or jump['timestamp']-times[i]>2000:continue
                    opening=rows[i];leg=jump['leg'];other='opt' if leg=='pm' else 'pm';delta=jump['after']-jump['before']
                    if not delta:continue
                    for h in HORIZONS[:-1]:
                        deadline=jump['timestamp']+h*1000;j=bisect_right(times,deadline)-1
                        if deadline>b or j<i or deadline-times[j]>2000:continue
                        move=(1 if delta>0 else -1)*(rows[j][other+'_yes']-opening[other+'_yes'])*100
                        event_studies[(leg,h)].append(dict(block=key[0]+':'+str(jump['timestamp']//DAY),event_id=key[0],opt_toward=move,pm_toward=0,convergence=move,opt_fraction=0,pm_fraction=0,gap_pp=0))
                next_open=a;next_trade=a;previous=None
                for i,r in enumerate(rows):
                    t=r['timestamp_wall'];gap=100*(r['pm_yes']-r['opt_yes']);z=logit(r['pm_yes'])-logit(r['opt_yes'])
                    block=r['event_id']+':'+str(t//DAY)
                    previous=r
                    if t<next_open or gap==0:continue
                    next_open=t+stride_seconds*1000
                    prior=bisect_right(times,t-60000)-1
                    spot_recent=math.log(r['spot']/rows[prior]['spot']) if prior>=0 and t-60000-times[prior]<=2000 else None
                    meta=dict(event_id=r['event_id'],block=block,asset=r['asset'],event_type=r['event_type'],direction=r['direction'],
                        asset_class='crypto' if r['asset'] in ('BTC','ETH') else 'equity',regime=r.get('regime'),
                        probability_bucket='tail<5%' if min(r['pm_yes'],r['opt_yes'])<.05 else 'middle5-95%' if max(r['pm_yes'],r['opt_yes'])<.95 else 'tail>95%',
                        expiry_bucket='<1day' if r['expiry']-t<DAY else '1-7days' if r['expiry']-t<7*DAY else '7days+',
                        gap_pp=gap,z=z,spot_recent=spot_recent,iv=r.get('local_iv'),tte_days=(r['expiry']-t)/DAY,
                        distance=math.log(r['strike_or_threshold']/r['spot']))
                    for h in HORIZONS:
                        deadline=t+h*1000;j=bisect_right(times,deadline)-1
                        out=horizon_outcome(r,rows[j] if j>=i else None,deadline)
                        if out is not None and deadline<=b:outcomes[h].append(dict(out,**meta))
                        else:excluded['MISSING_HORIZON_'+str(h)]+=1
                    if t>=next_trade and r.get('input_refs',{}).get('options'):
                        opening=dict(r,gap_pp=gap,model_inputs=dict(iv=r.get('local_iv')))
                        opening['_future']={}
                        for h in HORIZONS:
                            j=bisect_right(times,t+h*1000)-1
                            if j>=i and t+h*1000<=b and t+h*1000-times[j]<=2000:
                                opening['_future'][str(h)]=dict(rows[j])
                        trade_openings.append(opening);next_trade=t+1800000
                    # Half-life and full-close/double are censored at 30m, never
                    # reported only among successes. Sub-.1pp gaps keep responses.
                    if abs(gap)>=.1:
                        stop=bisect_right(times,min(b,t+1800000));future=rows[i+1:stop]
                        continuous=bool(future) and all(right['timestamp_wall']-left['timestamp_wall']<=5000 for left,right in zip([r]+future,future))
                        half=close=double=widen=None;peak=abs(gap);peak_at=t
                        if continuous:
                            for f in future:
                                g=100*(f['pm_yes']-f['opt_yes']);dt=(f['timestamp_wall']-t)/1000
                                if abs(g)<=abs(gap)/2 and half is None:half=dt
                                if (g*gap<=0 or abs(g)<=.01) and close is None:close=dt
                                if abs(g)>=2*abs(gap) and double is None:double=dt
                                if abs(g)>abs(gap)+1e-9 and widen is None:widen=dt
                                if abs(g)>peak:peak=abs(g);peak_at=f['timestamp_wall']
                        full=continuous and b>=t+1800000 and future[-1]['timestamp_wall']>=t+1798000
                        life.append(dict(block=block,event_id=r['event_id'],half_life_s=half,close_before_double=close is not None and (double is None or close<double),
                            close_before_widening=close is not None and (widen is None or close<widen),
                            asset=r['asset'],event_type=r['event_type'],direction=r['direction'],
                            doubled=double is not None,time_to_max_s=(peak_at-t)/1000,full_followup=full,gap_bin=bin_gap(gap)))
            if progress:print(f'gap research: region {region+1}/{len(plan)}, {scanned:,} usable candidate frames read',flush=True)
    finally:reader.close()
    report=dict(version=VERSION,mode='EXPLORATORY_HISTORICAL',created_ms=time.time_ns()//1000000,start_ms=start,end_ms=end,
        selection=f'Deterministic {per_day} 1h window(s) per UTC day, earliest and latest; all nonzero usable gaps in each window; {stride_seconds}s openings; no episode threshold',
        windows=plan,frame_records_read=scanned,excluded=dict(excluded),sampling_stride_seconds=stride_seconds,
        unit='Event/UTC-day blocks; repeated openings are clustered, not independent discoveries',
        probability_resolution=.001,horizons={str(h):block_summary(outcomes[h]) for h in HORIZONS},
        regressions={str(h):regression(outcomes[h]) for h in HORIZONS},spot_conditioned={str(h):regression(outcomes[h],True) for h in HORIZONS},
        edge_surface={},strata={},stratified_horizons={},event_studies={},comparables={},seconds=time.monotonic()-started)
    thirty=outcomes[1800]
    for dimension,function in [('gap_pp',lambda r:bin_gap(r['gap_pp'])),('log_odds_gap',lambda r:bin_odds(r['z']))]:
        buckets=defaultdict(list)
        for r in thirty:buckets[function(r)].append(r)
        report['edge_surface'][dimension]={k:block_summary(v) for k,v in sorted(buckets.items())}
    for dimension in ('asset','asset_class','event_type','direction','probability_bucket','expiry_bucket','regime'):
        buckets=defaultdict(list)
        for r in thirty:buckets[str(r[dimension])].append(r)
        report['strata'][dimension]={k:block_summary(v) for k,v in sorted(buckets.items())}
    buckets=defaultdict(list)
    for r in thirty:buckets['|'.join((r['asset'],r['event_type'],r['direction'],bin_gap(r['gap_pp'])))].append(r)
    report['comparables']={key:block_summary(rows) for key,rows in buckets.items()}
    for dimension in ('asset','asset_class','event_type','direction','probability_bucket','expiry_bucket','regime','gap_bin','log_odds_bin'):
        dimension_results={}
        for horizon,rows in outcomes.items():
            buckets=defaultdict(list)
            for r in rows:
                key=bin_gap(r['gap_pp']) if dimension=='gap_bin' else bin_odds(r['z']) if dimension=='log_odds_bin' else str(r[dimension])
                buckets[key].append(r)
            dimension_results[str(horizon)]={}
            for key,group in buckets.items():
                count=len({r['block'] for r in group})
                dimension_results[str(horizon)][key]=dict(response=block_summary(group),error_correction=regression(group)) if count>=10 else dict(n_blocks=count,status='INSUFFICIENT_STRATUM')
        report['stratified_horizons'][dimension]=dimension_results
    report['quote_scale_sensitivity']={str(h):regression([r for r in rows if all(.001<=r[k]<=.999 for k in ('p0','q0','p1','q1'))],True) for h,rows in outcomes.items()}
    report['quote_scale_sensitivity_definition']='Separate sensitivity fit with all opening/horizon probabilities in [.001,.999]. Raw interior probabilities remain unchanged in the main fit; endpoints alone are clipped.'
    for (leg,h),rows in event_studies.items():report['event_studies'].setdefault(leg,{})[str(h)]=block_summary(rows)
    report['event_study_timing']='Exact v2 native salient jump timestamps/raw boundaries. Other leg uses causal preceding frame (<=2s); outcomes use as-of frames. Legacy has no salient stream and is excluded from native studies.'
    complete=[r for r in life if r['full_followup']];halves=[r['half_life_s'] for r in complete if r['half_life_s'] is not None]
    # Kaplan-Meier median: if fewer than half reached half-gap, median is not observed.
    report['lifecycle']=dict(openings=len(life),complete_30m_followup=len(complete),n_blocks=len({r['block'] for r in complete}),max_valid_frame_gap_ms=5000,
        half_reached_rate=len(halves)/len(complete) if complete else None,
        median_half_life_s=float(np.quantile(halves,.5*len(complete)/len(halves))) if halves and len(halves)>=len(complete)/2 else None,
        median_status='OBSERVED' if halves and len(halves)>=len(complete)/2 else 'NOT_REACHED_WITHIN_30M',
        p_close_before_double=sum(r['close_before_double'] for r in complete)/len(complete) if complete else None,
        p_gap_doubles=sum(r['doubled'] for r in complete)/len(complete) if complete else None,
        median_time_to_max_s=float(np.median([r['time_to_max_s'] for r in complete])) if complete else None)
    report['lifecycle'].update(lifecycle_summary(complete))
    for key,summary in report['comparables'].items():
        asset,kind,direction,gap_bin=key.split('|')
        subset=[r for r in complete if r['gap_bin']==gap_bin and r['asset']==asset and r['event_type']==kind and r['direction']==direction]
        summary['lifecycle']=lifecycle_summary(subset)
        summary['half_life_s']=summary['lifecycle']['half_life_s']
    report['_trade_openings']=trade_openings
    return report


def text_report(report):
    lines=['BASIS / EXPLORATORY GAP RESPONSE',f"{report['frame_records_read']:,} frame records, {len(report['windows'])} chronology windows. N = event/day blocks.",
        'HORIZON    N    OPT toward PM   PM toward OPT    OPT move pp   GAP close pp']
    for h,r in report['horizons'].items():
        if r.get('n_blocks'):lines.append(f"{h+'s':>7} {r['n_blocks']:5d} {100*r['opt_toward_rate']:14.1f}% {100*r['pm_toward_rate']:14.1f}% {r['mean_opt_move_pp']:14.4f} {r['mean_convergence_pp']:14.4f}")
    lines+=['GAP @30m    N     OPT toward PM   AVG close pp']
    for k,r in report['edge_surface']['gap_pp'].items():lines.append(f"{k:<10} {r['n_blocks']:5d} {100*r['opt_toward_rate']:14.1f}% {r['mean_convergence_pp']:14.4f}")
    lines.append('Half-life: '+str(report['lifecycle']))
    lines.append('Beta / Poly reversion b (opening controls; then ex-post spot controls):')
    for h in report['horizons']:
        a=report['regressions'][h];b=report['spot_conditioned'][h]
        lines.append(f"{h}s: beta {a.get('options_beta',{}).get('coefficient')} / b {a.get('poly_reversion_b',{}).get('coefficient')}; spot beta {b.get('options_beta',{}).get('coefficient')}")
    return '\n'.join(lines)
