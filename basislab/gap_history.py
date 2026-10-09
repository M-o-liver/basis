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
from .gap_math import HORIZONS, horizon_outcome, logit, formation
from .store import decode, encode

VERSION='gap-response-2.0'
from .gap_shape import response_curve, response_model, hazard_summary
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
        raw_table='records' if v2 else 'raw'
        raw_first=db.execute(f'SELECT id,received_ms FROM {raw_table} ORDER BY id LIMIT 1').fetchone()
        raw_last=db.execute(f'SELECT id,received_ms FROM {raw_table} ORDER BY id DESC LIMIT 1').fetchone()
        self.sources.append(dict(db=db,path=str(path),v2=v2,table=table,column=column,first_id=first[0],last_id=last[0],first_ms=first[1],last_ms=last[1],raw_table=raw_table,raw_first=raw_first[0],raw_last=raw_last[0],raw_first_ms=raw_first[1],raw_last_ms=raw_last[1]))

    def close(self):
        for s in self.sources:s['db'].close()

    def native(self,scope,start,end):
        for s in self.sources:
            if not s['v2'] or s['last_ms']<start or s['first_ms']>end:continue
            for r in s['db'].execute("SELECT a.raw_id,a.timestamp_ms,a.kind,a.before_value,a.after_value,st.source,st.kind trigger_kind,r.source_ms FROM salient a JOIN records r ON r.id=a.raw_id JOIN streams st ON st.id=r.stream_id WHERE a.scope=? AND a.timestamp_ms BETWEEN ? AND ? AND a.kind IN ('pm_jump','opt_jump') ORDER BY a.timestamp_ms,a.raw_id",(scope,start,end)):
                yield dict(raw_id=r['raw_id'],timestamp=r['timestamp_ms'],leg=r['kind'][:-5],before=decode(r['before_value']),after=decode(r['after_value']),trigger_source=r['source'],trigger_kind=r['trigger_kind'],source_ms=r['source_ms'])

    def raw_refs(self,kind,subject,start,end,first=False):
        candidates=[]
        for s in (self.sources if first else reversed(self.sources)):
            if s['raw_first_ms']>end or s['raw_last_ms']<start:continue
            db=s['db'];table=s['raw_table']
            lo=lower_id(db,table,'received_ms',start,s['raw_first'],s['raw_last']+1)
            hi=lower_id(db,table,'received_ms',end+1,lo,s['raw_last']+1)
            direction='ASC' if first else 'DESC'
            if s['v2']:
                streams=[r[0] for r in db.execute('SELECT id FROM streams WHERE kind=? AND subject=?',(kind,subject))]
                for stream in streams:
                    candidates.extend(dict(r) for r in db.execute(f'SELECT id,received_ms FROM records INDEXED BY record_stream WHERE stream_id=? AND id>=? AND id<? AND received_ms BETWEEN ? AND ? ORDER BY id {direction} LIMIT 64',(stream,lo,hi,start,end)))
            else:
                candidates.extend(dict(r) for r in db.execute(f'SELECT id,received_ms FROM raw INDEXED BY raw_kind WHERE kind=? AND subject=? AND id>=? AND id<? AND received_ms BETWEEN ? AND ? ORDER BY id {direction} LIMIT 64',(kind,subject,lo,hi,start,end)))
            if candidates and not first:break
        return sorted({r['id']:r for r in candidates}.values(),key=lambda r:r['id'],reverse=not first)

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
            offset=int.from_bytes(hashlib.sha256(f'gap-response-1.0:{day}:{i}'.encode()).digest()[:8],'big')%(23*3600000)
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
        mean_opt_close_fraction=float(np.mean([np.mean([r['opt_fraction'] for r in group if abs(r['gap_pp'])>=.1]) for group in clusters.values() if any(abs(r['gap_pp'])>=.1 for r in group)])) if fractions else None,
        mean_pm_close_fraction=float(np.mean([np.mean([r['pm_fraction'] for r in group if abs(r['gap_pp'])>=.1]) for group in clusters.values() if any(abs(r['gap_pp'])>=.1 for r in group)])) if fractions else None,
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


def study(tape,per_day=1,stride_seconds=60,start=None,end=None,progress=True,cache_path=None):
    started=time.monotonic();reader=HotTape(tape)
    start=max(reader.start,start or reader.start);end=min(reader.end,end or reader.end)
    plan=windows(start,end,per_day);outcomes=defaultdict(list);life=[];impulses=defaultdict(list)
    scanned=0;excluded=Counter();trade_openings=[];seen_open=set();seen_life=set()
    for region,(a,b) in enumerate(plan):
        try:
            panel=defaultdict(list)
            follow_end=min(end,b+7200000)
            for r in reader.rows(max(reader.start,a-60000),follow_end):
                scanned+=1
                if r.get('source_state') not in ('OK','PROXY','CLOSED') or 'WIDE_PM_BOOK' in r.get('quality_flags',[]):
                    excluded[r.get('source_state','UNKNOWN')]+=1;continue
                if not all(v is not None and math.isfinite(v) for v in (r.get('pm_yes'),r.get('opt_yes'),r.get('spot'))):continue
                key=(r['event_id'],r.get('mapping_hash'),r['expiry'],r['event_type'],r['direction'])
                panel[key].append(r)
            for key,rows in panel.items():
                rows.sort(key=lambda r:(r['timestamp_wall'],r['raw_id'],r['id']));times=[r['timestamp_wall'] for r in rows]
                causal_keys=[(r['timestamp_wall'],r['raw_id']) for r in rows];gap_values=np.array([100*(r['pm_yes']-r['opt_yes']) for r in rows])
                breaks=np.flatnonzero(np.diff(times)>5000)+1
                def metadata(r,i):
                    t=r['timestamp_wall'];gap=100*(r['pm_yes']-r['opt_yes']);prior=bisect_right(times,t-30000)-1
                    recent=formation(r,rows[prior] if prior>=0 else None)
                    before=bisect_right(times,t-60000)-1
                    spot_recent=math.log(r['spot']/rows[before]['spot']) if before>=0 and t-60000-times[before]<=2000 else None
                    return dict(event_id=r['event_id'],block=r['event_id']+':'+str(t//DAY),asset=r['asset'],event_type=r['event_type'],direction=r['direction'],
                        asset_class='crypto' if r['asset'] in ('BTC','ETH') else 'equity',regime=r.get('regime'),
                        probability_bucket='tail<5%' if min(r['pm_yes'],r['opt_yes'])<.05 else 'middle5-95%' if max(r['pm_yes'],r['opt_yes'])<.95 else 'tail>95%',
                        expiry_bucket='<1day' if r['expiry']-t<DAY else '1-7days' if r['expiry']-t<7*DAY else '7days+',
                        gap_pp=gap,z=logit(r['pm_yes'])-logit(r['opt_yes']),spot_recent=spot_recent,iv=r.get('local_iv'),tte_days=(r['expiry']-t)/DAY,
                        distance=math.log(r['strike_or_threshold']/r['spot']),**recent)
                for jump in reader.native(key[0],a,b):
                    i=bisect_right(causal_keys,(jump['timestamp'],jump['raw_id']-1))-1
                    if i<0 or jump['timestamp']-times[i]>2000 or abs(jump['after']-jump['before'])*100<.25:continue
                    base=rows[i];leg=jump['leg'];other='opt' if leg=='pm' else 'pm';delta=jump['after']-jump['before']
                    opening=dict(base,timestamp_wall=jump['timestamp'],raw_id=jump['raw_id'],**{leg+'_yes':jump['after']})
                    meta=metadata(opening,i);meta.update(jump_pp=100*delta,jump_bin='.25-.5pp' if abs(delta)<.005 else '.5-1pp' if abs(delta)<.01 else '1-2pp' if abs(delta)<.02 else '2pp+',
                        gap_bin=bin_gap(meta['gap_pp']),trigger_source=jump['trigger_source'],trigger_kind=jump['trigger_kind'])
                    for h in HORIZONS[:-1]:
                        deadline=jump['timestamp']+h*1000;j=bisect_right(times,deadline)-1
                        if j<i:continue
                        out=horizon_outcome(opening,rows[j],deadline)
                        if out is None or deadline>follow_end:continue
                        move=(1 if delta>0 else -1)*(rows[j][other+'_yes']-base[other+'_yes'])*100
                        impulses[(leg,h)].append(dict(meta,opt_toward=move,pm_toward=0,convergence=move,opt_fraction=0,pm_fraction=0,
                            opening_raw_id=jump['raw_id'],preceding_raw_id=base['raw_id'],pm_before=jump['before'] if leg=='pm' else base['pm_yes'],
                            pm_after=opening['pm_yes'],opt_before=jump['before'] if leg=='opt' else base['opt_yes'],opt_after=opening['opt_yes'],spot_star=base['spot']))
                next_open=a;next_trade=a;next_life=a
                for i,r in enumerate(rows):
                    t=r['timestamp_wall']
                    if not a<=t<=b or t<next_open or r['pm_yes']==r['opt_yes']:continue
                    identity=(key,r['raw_id'])
                    if identity in seen_open:continue
                    seen_open.add(identity);next_open=t+stride_seconds*1000;meta=metadata(r,i)
                    for h in HORIZONS:
                        deadline=t+h*1000;j=bisect_right(times,deadline)-1
                        out=horizon_outcome(r,rows[j] if j>=i else None,deadline)
                        if out is not None and deadline<=follow_end:outcomes[h].append(dict(out,**meta))
                        else:excluded['MISSING_HORIZON_'+str(h)]+=1
                    if t>=next_trade and r.get('input_refs',{}).get('options'):
                        opening=dict(r,gap_pp=meta['gap_pp'],model_inputs=dict(iv=r.get('local_iv')),formation={k:meta[k] for k in ('provenance','fresh_pm_unfollowed','delta_pm_pp','delta_opt_pp','spot_return')})
                        trade_openings.append(opening);next_trade=t+300000
                    if t<next_life or abs(meta['gap_pp'])<.1 or identity in seen_life:continue
                    next_life=t+300000;seen_life.add(identity)
                    stop=bisect_right(times,min(t+7200000,r['expiry']));bad=breaks[breaks>i]
                    reason='HORIZON_COMPLETE'
                    if len(bad) and bad[0]<stop:stop=int(bad[0]);reason='SOURCE_OR_RECORDING_GAP'
                    elif stop and times[stop-1]<t+7198000:reason='EXPIRY' if r['expiry']<=t+7200000 else 'END_OF_AVAILABLE_HISTORY'
                    g=gap_values[i:stop];elapsed=(np.asarray(times[i:stop])-t)/1000;absolute=abs(g);size=abs(meta['gap_pp'])
                    first=lambda mask:float(elapsed[np.flatnonzero(mask)[0]]) if mask.any() else None
                    peak=int(np.argmax(absolute)) if len(g) else 0
                    life.append(dict(meta,gap_bin=bin_gap(size),log_odds_bin=bin_odds(meta['z']),close25_s=first(absolute<=.75*size),
                        half_s=first(absolute<=.5*size),full_s=first((g*meta['gap_pp']<=0)|(absolute<=.01)),double_s=first(absolute>=2*size),
                        censor_s=max(0,min(7200,(follow_end-t)/1000,(r['expiry']-t)/1000,float(elapsed[-1])+2)) if len(elapsed) else 0,censor_reason=reason,time_to_max_s=float(elapsed[peak]) if len(elapsed) else None))
            if progress:print(f'gap shape: region {region+1}/{len(plan)}, {scanned:,} frames including lookback/follow-up',flush=True)
        except Exception:
            reader.close();raise
    reader.close()
    if cache_path:
        import zlib
        payload=dict(start=start,end=end,per_day=per_day,stride_seconds=stride_seconds,plan=plan,
            outcomes={str(k):v for k,v in outcomes.items()},life=life,impulses={f'{leg}:{h}':v for (leg,h),v in impulses.items()},
            scanned=scanned,excluded=dict(excluded),trade_openings=trade_openings,opening_count=len(seen_open),data_read_seconds=time.monotonic()-started)
        Path(cache_path).write_bytes(zlib.compress(encode(payload).encode(),3))
    return summarize(dict(start=start,end=end,per_day=per_day,stride_seconds=stride_seconds,plan=plan,outcomes=outcomes,life=life,
        impulses=impulses,scanned=scanned,excluded=excluded,trade_openings=trade_openings,opening_count=len(seen_open),data_read_seconds=time.monotonic()-started))


def summarize(data):
    started=time.monotonic()
    start,end,per_day,stride_seconds,plan,life,scanned,excluded,trade_openings=[data[k] for k in ('start','end','per_day','stride_seconds','plan','life','scanned','excluded','trade_openings')]
    outcomes=defaultdict(list,{int(h):rows for h,rows in data['outcomes'].items()})
    impulses={(k if isinstance(k,tuple) else (k.split(':')[0],int(k.split(':')[1]))):rows for k,rows in data['impulses'].items()}
    report=dict(version=VERSION,mode='EXPLORATORY_HISTORICAL',created_ms=time.time_ns()//1000000,start_ms=start,end_ms=end,
        selection=f'Deterministic {per_day} 1h opening window(s)/UTC day + earliest/latest; 60s lookback and up to 2h follow-up; all nonzero usable gaps; {stride_seconds}s opening stride; 300s hazard stride',
        windows=plan,frame_records_read=scanned,opening_count=data['opening_count'],unique_events=len({r['event_id'] for rows in outcomes.values() for r in rows}),
        event_day_blocks=len({r['block'] for rows in outcomes.values() for r in rows}),excluded=dict(excluded),sampling_stride_seconds=stride_seconds,
        unit='Event/UTC-day blocks; overlapping windows deduplicated; repeated frames are not independent discoveries',
        formation_rules=dict(window_seconds=30,widening_pp=.10,dominant_travel=.75,fresh_pm_pp=.25,opt_followed_fraction_max=.25,spot_common_log_return=.001,both_common_min_pp=.02,causal_claim=False),
        probability_resolution=.001,horizons={str(h):block_summary(outcomes[h]) for h in HORIZONS},response_curves={},controlled_curves={},edge_surface={},strata={},
        gap_provenance={},event_studies={},event_study_strata={},conditional_hazards={},comparables={})
    for h,rows in outcomes.items():
        report['response_curves'][str(h)]={axis:response_curve(rows,axis) for axis in ('gap_pp','z')}
        report['controlled_curves'][str(h)]={axis:dict(prediction_time=response_model(rows,axis),ex_post_spot=response_model(rows,axis,True)) for axis in ('gap_pp','z')}
        buckets=defaultdict(list)
        for r in rows:buckets[r['provenance']].append(r)
        report['gap_provenance'][str(h)]={k:block_summary(v) for k,v in buckets.items()}
        report['gap_provenance'][str(h)]['LARGE_FRESH_PM_UNFOLLOWED']=block_summary([r for r in rows if abs(r['gap_pp'])>=4 and r['fresh_pm_unfollowed']])
    thirty=outcomes[1800]
    for dimension in ('gap_pp','z'):
        func=bin_gap if dimension=='gap_pp' else bin_odds;buckets=defaultdict(list)
        for r in thirty:buckets[func(r[dimension])].append(r)
        report['edge_surface'][dimension]={k:block_summary(v) for k,v in buckets.items()}
    for dimension in ('asset','asset_class','event_type','direction','probability_bucket','expiry_bucket','regime','provenance'):
        buckets=defaultdict(list)
        for r in thirty:buckets[str(r[dimension])].append(r)
        report['strata'][dimension]={k:block_summary(v) for k,v in buckets.items()}
    buckets=defaultdict(list)
    for r in thirty:buckets['|'.join((r['asset'],r['event_type'],r['direction'],bin_gap(r['gap_pp'])))].append(r)
    report['comparables']={k:block_summary(v) for k,v in buckets.items()}
    report['lifecycle']=hazard_summary(life)
    for dimension in ('gap_bin','log_odds_bin','provenance'):
        buckets=defaultdict(list)
        for r in life:buckets[r[dimension]].append(r)
        report['conditional_hazards'][dimension]={k:hazard_summary(v) for k,v in buckets.items()}
    for key,summary in report['comparables'].items():
        asset,kind,direction,gap_bin=key.split('|');subset=[r for r in life if r['asset']==asset and r['event_type']==kind and r['direction']==direction and r['gap_bin']==gap_bin]
        summary['lifecycle']=hazard_summary(subset);summary['half_life_s']=summary['lifecycle'].get('median_half_life_s')
    for (leg,h),rows in impulses.items():
        report['event_studies'].setdefault(leg,{})[str(h)]=block_summary(rows)
        target=report['event_study_strata'].setdefault(leg,{}).setdefault(str(h),{})
        for dimension in ('gap_bin','jump_bin','event_type','probability_bucket','expiry_bucket','trigger_source'):
            buckets=defaultdict(list)
            for r in rows:buckets[str(r[dimension])].append(r)
            target[dimension]={k:block_summary(v) for k,v in buckets.items()}
    report['event_study_timing']='Exact native receipt timestamp/raw ID; other leg/spot from strictly preceding causal frame <=2s old; post-jump PM/OPT from native values, not a later frame; OPT jumps can be spot/theta-created model moves. Receipt ordering is not causality.'
    report['impulse_audit']=[r for rows in impulses.values() for r in rows][:128]
    report['_trade_openings']=sorted(trade_openings,key=lambda r:(r['timestamp_wall'],r['raw_id']))
    report['seconds']=data['data_read_seconds']+time.monotonic()-started
    return report


def text_report(report):
    lines=['BASIS / EXPLORATORY GAP SHAPE',f"{report['frame_records_read']:,} frames; {report['opening_count']:,} openings; {report['unique_events']} events; {report['event_day_blocks']} event/day blocks.",
        'HORIZON    N    OPT toward PM   PM toward OPT    OPT move pp   GAP close pp']
    for h,r in report['horizons'].items():
        if r.get('n_blocks'):lines.append(f"{h+'s':>7} {r['n_blocks']:5d} {100*r['opt_toward_rate']:14.1f}% {100*r['pm_toward_rate']:14.1f}% {r['mean_opt_move_pp']:14.4f} {r['mean_convergence_pp']:14.4f}")
    lines+=['GAP @30m    N     OPT toward PM   AVG close pp']
    for k,r in sorted(report['edge_surface']['gap_pp'].items()):lines.append(f"{k:<10} {r['n_blocks']:5d} {100*r['opt_toward_rate']:14.1f}% {r['mean_convergence_pp']:14.4f}")
    for kind,r in report['gap_provenance']['1800'].items():
        if r.get('n_blocks'):lines.append(f"{kind}: N {r['n_blocks']}, OPT {100*r['opt_toward_rate']:.1f}%, mean move {r['mean_opt_move_pp']:.4f}pp")
    if report.get('trade_math'):
        lines.append('STRUCTURE HORIZON N WIN% MEAN PNL MEDIAN PNL COSTS')
        for h,r in report['trade_math']['horizons'].items():lines.append(f"{h} {r['n_blocks']} {r['win_rate']*100:.1f}% ${r['mean_pnl']:.2f} ${r['median_pnl']:.2f} ${r['total_cost']:.2f}")
    return '\n'.join(lines)
