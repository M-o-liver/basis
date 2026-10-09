"""Small response curves and censored lifetimes, clustered on event/UTC-day."""
from collections import Counter
import math
import numpy as np

GAP_POINTS=(0,.25,.5,1,1.5,2,3,4,6,8,12,20)
ODDS_POINTS=(0,.1,.25,.5,1,2,3,5,8,12)


def response_curve(rows,axis='gap_pp',target='opt_toward'):
    if not rows:return []
    blocks=sorted({r['block'] for r in rows});index={b:i for i,b in enumerate(blocks)}
    counts=Counter(r['block'] for r in rows);ids=np.array([index[r['block']] for r in rows])
    x=np.log1p([abs(r[axis]) for r in rows]);y=np.array([r[target] for r in rows]);weight=np.array([1/counts[r['block']] for r in rows])
    points=GAP_POINTS if axis=='gap_pp' else ODDS_POINTS;out=[];rng=np.random.default_rng(7)
    draws=rng.integers(len(blocks),size=(256,len(blocks)))
    for point in points:
        kernel=np.maximum(0,1-abs(x-math.log1p(point))/.45);support=kernel>0
        n=len(set(ids[support]));item=dict(x=point,n_blocks=n,openings=int(support.sum()),unique_events=len({r['event_id'] for r,k in zip(rows,support) if k}),bandwidth_log1p=.45)
        if n<10 or not support.any():out.append(dict(item,mean_pp=None,ci95=None,status='SPARSE'));continue
        den=np.bincount(ids,weights=weight*kernel,minlength=len(blocks));num=np.bincount(ids,weights=weight*kernel*y,minlength=len(blocks))
        fit=float(num.sum()/den.sum());a=num[draws].sum(axis=1);b=den[draws].sum(axis=1);samples=a[b>0]/b[b>0]
        out.append(dict(item,mean_pp=fit,ci95=np.quantile(samples,[.025,.975]).tolist(),status='EXPLORATORY'))
    return out


def response_model(rows,axis='gap_pp',ex_post=False):
    complete=[r for r in rows if r.get('iv') and r.get('spot_recent') is not None and r.get('delta_pm_pp') is not None and (not ex_post or r.get('future_spot_return') is not None)]
    blocks=Counter(r['block'] for r in complete)
    if len(blocks)<20:return dict(status='INSUFFICIENT',n_blocks=len(blocks),openings=len(complete))
    knots=(1,2,4,8) if axis=='gap_pp' else (.25,.5,1,2)
    def basis(value):
        x=math.log1p(abs(value));return [x]+[max(0,x-math.log1p(k)) for k in knots]
    names=['intercept','log1p_magnitude']+[f'hinge_{k}' for k in knots]+['fresh_pm']+['fresh_pm_x_'+s for s in ['magnitude']+[str(k) for k in knots]]
    names+=['prior_60s_spot','IV','log_days_to_expiry','threshold_distance','event_up','gap_sign','recent_PM_pp','recent_OPT_pp']
    if ex_post:names+=['future_spot_EX_POST_ONLY']
    def vector(r,value=None,fresh=None):
        b=basis(r[axis] if value is None else value);f=int(r['fresh_pm_unfollowed']) if fresh is None else fresh
        return [1]+b+[f]+[f*v for v in b]+[r['spot_recent'],r['iv'],math.log(max(1e-6,r['tte_days'])),r['distance'],1 if r['direction']=='up' else -1,1 if r['gap_pp']>0 else -1,r['delta_pm_pp'],r['delta_opt_pp']]+([r['future_spot_return']] if ex_post else [])
    original=np.asarray([vector(r) for r in complete]);active=[0]+[i for i in range(1,len(names)) if np.std(original[:,i])>1e-12]
    centers=original[:,active].mean(axis=0);centers[0]=0;scale=original[:,active].std(axis=0);scale[0]=1
    X=(original[:,active]-centers)/scale;w=np.array([1/blocks[r['block']] for r in complete]);Y=np.array([r['opt_toward'] for r in complete])
    coef,_,rank,_=np.linalg.lstsq(X*np.sqrt(w[:,None]),Y*np.sqrt(w),rcond=None)
    residual=Y-X@coef;bread=np.linalg.pinv(X.T@(w[:,None]*X));scores={b:np.zeros(len(active)) for b in blocks}
    for r,x,e,v in zip(complete,X,residual,w):scores[r['block']]+=x*e*v
    meat=sum((np.outer(a,a) for a in scores.values()),start=np.zeros((len(active),len(active))))
    covariance=bread@meat@bread*len(blocks)/max(1,len(blocks)-1)
    points=GAP_POINTS if axis=='gap_pp' else ODDS_POINTS;profiles={}
    for fresh,label in ((0,'OTHER'),(1,'FRESH_PM_UNFOLLOWED')):
        subset=[r for r in complete if int(r['fresh_pm_unfollowed'])==fresh]
        if not subset:profiles[label]=[];continue
        representative=dict(subset[0])
        for field in ('spot_recent','iv','tte_days','distance','delta_pm_pp','delta_opt_pp','future_spot_return'):
            vals=[r[field] for r in subset if r.get(field) is not None]
            if vals:representative[field]=float(np.median(vals))
        out=[]
        for point in points:
            nearby=[r for r in subset if abs(math.log1p(abs(r[axis]))-math.log1p(point))<.45];n=len({r['block'] for r in nearby})
            if n<10:out.append(dict(x=point,n_blocks=n,mean_pp=None,ci95=None,status='SPARSE'));continue
            v=(np.asarray(vector(representative,point,fresh))[active]-centers)/scale;estimate=float(v@coef);se=math.sqrt(max(0,float(v@covariance@v)))
            out.append(dict(x=point,n_blocks=n,mean_pp=estimate,ci95=[estimate-1.96*se,estimate+1.96*se] if rank==len(active) else None,status='EXPLORATORY'))
        profiles[label]=out
    return dict(status='EXPLORATORY',n_blocks=len(blocks),openings=len(complete),axis=axis,knots=knots,rank=int(rank),
        controls=[names[i] for i in active],constant_columns_dropped=[names[i] for i in range(len(names)) if i not in active],
        profiles=profiles,interpretation='Ex-post attribution; future spot is unavailable to predictions' if ex_post else 'Prediction-time inputs only; descriptive spline, not causal proof')


def hazard_summary(rows,horizons=(30,300,1800,7200)):
    if not rows:return dict(n_blocks=0,status='INSUFFICIENT',horizons={})
    blocks=sorted({r['block'] for r in rows});block_index={b:i for i,b in enumerate(blocks)};counts=Counter(r['block'] for r in rows)
    ids=np.array([block_index[r['block']] for r in rows]);weights=np.array([1/counts[r['block']] for r in rows]);censor=np.array([r['censor_s'] for r in rows])
    def prepare(event,types=None):
        stop=np.minimum(event,censor);order=np.argsort(stop);observed=np.isfinite(event)&(event<=censor)
        times,inv=np.unique(event[observed],return_inverse=True)
        return dict(order=order,risk_index=np.searchsorted(stop[order],times,side='left'),observed=observed,inv=inv,times=times,types=types)
    values={k:np.array([r[k] if r[k] is not None else math.inf for r in rows]) for k in ('close25_s','half_s','full_s','double_s')}
    prepared={k:prepare(values[k]) for k in ('close25_s','half_s','full_s')}
    prepared['double']=prepare(np.minimum(values['half_s'],values['double_s']),values['double_s']<values['half_s'])
    def estimate(w):
        out={};median=None
        for key,p in prepared.items():
            suffix=np.r_[np.cumsum(w[p['order']][::-1])[::-1],0];risk=suffix[p['risk_index']]
            hits=np.bincount(p['inv'],weights=w[p['observed']],minlength=len(p['times']))
            hazards=np.divide(hits,risk,out=np.zeros_like(hits,dtype=float),where=risk>0);survival=np.cumprod(np.maximum(0,1-hazards))
            if key=='double':
                doubles=np.bincount(p['inv'],weights=w[p['observed']]*p['types'][p['observed']],minlength=len(p['times']))
                probability=np.cumsum(np.r_[1,survival[:-1]]*np.divide(doubles,risk,out=np.zeros_like(doubles,dtype=float),where=risk>0)) if len(survival) else np.array([])
            else:probability=1-survival
            if key=='half_s':
                crosses=np.flatnonzero(probability>=.5);median=float(p['times'][crosses[0]]) if len(crosses) else None
            out[key]=[float(probability[i]) if i>=0 else 0. for h in horizons for i in [int(np.searchsorted(p['times'],h,side='right'))-1]]
        return out,median
    estimates,median=estimate(weights);names={'close25_s':'p_25_closed','half_s':'p_50_closed','full_s':'p_full_closed','double':'p_double_before_half'};draws=[]
    if len(blocks)>=20:
        rng=np.random.default_rng(7)
        for _ in range(64):
            multiple=np.bincount(rng.integers(len(blocks),size=len(blocks)),minlength=len(blocks));draws.append(estimate(weights*multiple[ids])[0])
    out={}
    for index,h in enumerate(horizons):
        follow=int(np.sum(censor>=h));n_risk=len({r['block'] for r in rows if r['censor_s']>=h});item={names[k]:estimates[k][index] if n_risk else None for k in names}
        item.update(complete_openings=follow,censored_openings=len(rows)-follow,blocks_with_full_coverage=n_risk,
                    status='RIGHT_CENSORED_ESTIMATE' if n_risk>=10 else 'SPARSE_FOLLOWUP' if n_risk else 'MISSING_NO_FOLLOWUP')
        item['ci95_event_day_bootstrap']={names[k]:np.quantile([d[k][index] for d in draws],[.025,.975]).tolist() if draws and n_risk>=10 else None for k in names};out[str(h)]=item
    complete=[r for r in rows if r['censor_s']>=7198];groups={}
    for r in complete:groups.setdefault(r['block'],[]).append(r['time_to_max_s'])
    reached=[r['half_s'] for r in rows if r['half_s'] is not None]
    return dict(n_blocks=len(blocks),openings=len(rows),unique_events=len({r['event_id'] for r in rows}),horizons=out,
        median_half_life_s=median,median_half_life_reached_only_s=float(np.median(reached)) if reached else None,
        median_time_to_max_s=float(np.median([np.median(v) for v in groups.values()])) if groups else None,
        max_divergence_complete_openings=len(complete),method='Event/day weighted Kaplan-Meier; doubling-before-half uses competing-risk cumulative incidence; 64 event/day bootstrap resamples',
        censor_reasons=dict(Counter(r['censor_reason'] for r in rows)),status='EXPLORATORY' if len(blocks)>=10 else 'SMALL_SAMPLE')
