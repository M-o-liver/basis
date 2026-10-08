"""Small, inspectable gap and payoff calculations. No trading policy."""
import hashlib
import math
import numpy as np
from .semantics import number, probability
from .pricing import YEAR_MS, terminal_probability, touch_probability

VERSION = 'gap-math-1.0'
HORIZONS = (1, 5, 15, 30, 60, 300, 1800)


def logit(p, resolution=.001):
    p = probability(p)
    if p is None:
        return None
    epsilon = max(1e-9, min(.01, resolution / 2))
    p = epsilon if p==0 else 1-epsilon if p==1 else p
    return math.log(p)-math.log1p(-p)


def gap_math(p, q, resolution=.001):
    p, q = probability(p), probability(q)
    if p is None or q is None:
        return dict(gap_pp=None, relative_gap=None, log_odds_gap=None, odds_ratio=None, binary_kelly=None)
    z = logit(p, resolution)-logit(q, resolution)
    relative=abs(p-q)/q if q else None
    return dict(gap_pp=100*(p-q), relative_gap=relative if relative is not None and math.isfinite(relative) else None,
                log_odds_gap=z, odds_ratio=math.exp(z) if z<709 else None,
                binary_kelly=(p-q)/(1-q) if p>q and q<1 else 0,
                endpoint_clipped=p in (0,1) or q in (0,1), probability_resolution=resolution)


def execution_cost(legs, multiplier=100, quantity=1):
    """Buy ask, sell bid; same conservative fees/slippage as the retired desk."""
    gross = sum((1 if l['side']=='BUY' else -1)*l['price']*multiplier for l in legs)*quantity
    turnover = sum(l['price']*multiplier for l in legs)*quantity
    # Ten bps slippage plus five bps/$10k turnover impact. Fee: 10 bps + .65/leg.
    slippage = turnover*(.001 + turnover/10000*.0005)
    fees = turnover*.001 + .65*len(legs)*quantity
    spread = sum(abs(l['price']-(l['bid']+l['ask'])/2)*multiplier for l in legs)*quantity
    return dict(quoted_debit=gross, spread_cost=spread, slippage=slippage, fees=fees,
                debit=gross+slippage+fees, multiplier=multiplier, quantity=quantity,
                execution_model='ASK_BUY_BID_SELL + 10bps slip + size impact + 10bps fee + $0.65/leg')


def vertical(lower, upper, kind, multiplier=100):
    if kind not in ('call','put') or not lower['strike']<upper['strike']:
        raise ValueError('Vertical needs ordered distinct strikes and call/put type')
    buy, sell = (lower,upper) if kind=='call' else (upper,lower)
    legs=[]
    for side, c in (('BUY',buy),('SELL',sell)):
        bid, ask = number(c.get('bid')), number(c.get('ask'))
        if bid is None or ask is None or not 0<=bid<=ask or ask<=0:
            raise ValueError('Missing/crossed actual bid/ask')
        legs.append(dict(c, side=side, price=ask if side=='BUY' else bid))
    cost=execution_cost(legs,multiplier)
    maximum=(upper['strike']-lower['strike'])*multiplier
    if not 0<cost['debit']<maximum:
        raise ValueError('Nonpositive debit or costs exceed defined payout')
    return dict(kind=kind+'_vertical', legs=legs, **cost, max_payout=maximum,
                max_loss=cost['debit'], q_exec=cost['debit']/maximum)


def binary_ev(p, trade):
    q=trade['debit']/trade['max_payout'];ev=p*trade['max_payout']-trade['debit']
    return dict(expected_payoff=p*trade['max_payout'],ev=ev,roi=ev/trade['debit'],
                q_exec=q,pm_edge_pp=100*(p-q),binary_kelly=(p-q)/(1-q) if p>q and q<1 else 0,
                payoff_method='BINARY_VERTICAL_APPROXIMATION',
                approximation='Vertical has a ramp between strikes, not a binary payoff')


def reweight(p, hit_mean, no_hit_mean):
    if probability(p) is None:
        raise ValueError('Invalid PM probability')
    return p*hit_mean+(1-p)*no_hit_mean


def conditional_paths(spot, threshold, sigma, event_years, option_years, direction, event_type='touch', paths=16384, seed=7):
    """GBM zero carry, continuous barrier via Brownian bridge, importance sampled.

    Sample cutoff and expiry endpoints. The bridge integrates every intermediate
    touch, avoiding discrete monitoring bias. A normal mixture covers rare tails.
    No future data enters the calibration. This is a flat-IV research model.
    """
    if not all(number(x) is not None and x>0 for x in (spot,threshold,sigma,event_years,option_years)):
        raise ValueError('Invalid path inputs')
    if option_years<event_years or direction not in ('up','down'):
        raise ValueError('Option must cover the event cutoff')
    rng=np.random.default_rng(seed)
    scale=sigma*math.sqrt(event_years);mu=-.5*sigma*sigma*event_years
    barrier=math.log(threshold/spot)
    shift=max(-9,min(9,(barrier-mu)/scale))
    z=rng.standard_normal(paths)+np.where(np.arange(paths)%2,shift,0)
    # p(z) / [.5 p(z) + .5 p(z-shift)], stable even in far tails.
    weights=2/(1+np.exp(np.clip(shift*z-.5*shift*shift,-700,700)))
    endpoint=mu+scale*z
    terminal=spot*np.exp(endpoint-.5*sigma*sigma*max(0,option_years-event_years)+sigma*math.sqrt(max(0,option_years-event_years))*rng.standard_normal(paths))
    if event_type=='terminal':
        hit=(endpoint>=barrier if direction=='up' else endpoint<=barrier).astype(float)
    elif (direction=='up' and spot>=threshold) or (direction=='down' and spot<=threshold):
        hit=np.ones(paths)
    else:
        a=abs(barrier);signed=endpoint if direction=='up' else -endpoint
        hit=np.where(signed>=a,1,np.exp(np.minimum(0,-2*a*(a-signed)/(sigma*sigma*event_years))))
    hit_w=weights*hit;miss_w=weights*(1-hit)
    if hit_w.sum()<1e-250 or miss_w.sum()<1e-250:
        raise ValueError('Conditional path class unavailable; tail/crossed barrier')
    q=float(hit_w.sum()/weights.sum())
    ess=lambda w:float(w.sum()**2/np.sum(w*w))
    return dict(terminal=terminal,hit_weights=hit_w,no_hit_weights=miss_w,q_sample=q,
                q_model=touch_probability(spot,threshold,sigma,event_years,direction) if event_type=='touch' else terminal_probability(spot,threshold,sigma,event_years,direction),
                hit_effective_paths=ess(hit_w),no_hit_effective_paths=ess(miss_w),paths=paths,
                model='GBM_ZERO_CARRY_FLAT_IV_CONTINUOUS_BRIDGE',seed=seed)


def payoff(trade, terminal):
    result=np.zeros_like(terminal,dtype=float)
    for leg in trade['legs']:
        intrinsic=np.maximum(terminal-leg['strike'],0) if leg['option_type']=='call' else np.maximum(leg['strike']-terminal,0)
        result+=(1 if leg['side']=='BUY' else -1)*intrinsic*trade['multiplier']
    return result


def conditional_ev(p, trade, paths):
    values=payoff(trade,paths['terminal']);hw,nw=paths['hit_weights'],paths['no_hit_weights']
    h=float(np.dot(hw,values)/hw.sum());n=float(np.dot(nw,values)/nw.sum())
    expected=reweight(p,h,n);q=paths['q_model'];ev=expected-trade['debit']
    def se(w,mean):
        return math.sqrt(float(np.dot(w*w,(values-mean)**2)))/float(w.sum())
    error=math.sqrt((p*se(hw,h))**2+((1-p)*se(nw,n))**2)
    return dict(expected_payoff=expected,payoff_if_hit=h,payoff_if_no_hit=n,ev=ev,roi=ev/trade['debit'],
                baseline_ev=reweight(q,h,n)-trade['debit'],pm_increment=(p-q)*(h-n),
                mc_standard_error=error,mc_hit_se=se(hw,h),mc_no_hit_se=se(nw,n),q_path=q,q_sample=paths['q_sample'],
                hit_effective_paths=paths['hit_effective_paths'],no_hit_effective_paths=paths['no_hit_effective_paths'],
                paths=paths['paths'],payoff_method=paths['model'])


def horizon_outcome(opening, future, deadline, max_age_ms=2000):
    """An as-of observation, never a first observation after the deadline."""
    if future is None or future['timestamp_wall']>deadline or deadline-future['timestamp_wall']>max_age_ms:
        return None
    if any(future.get(k) is not None and future[k]>deadline for k in ('pm_timestamp','opt_timestamp','options_received_ms','spot_timestamp')):
        return None
    if any(opening.get(k) is not None and future.get(k) is not None and opening[k]!=future[k] for k in ('event_id','mapping_hash','expiry')):
        return None
    if future.get('raw_id') is not None and any(v is not None and v>future['raw_id'] for v in future.get('input_refs',{}).values()):
        return None
    p,q,p1,q1=[probability(v) for v in (opening.get('pm_yes'),opening.get('opt_yes'),future.get('pm_yes'),future.get('opt_yes'))]
    if any(v is None for v in (p,q,p1,q1)) or not p-q:
        return None
    gap=p-q;sign=1 if gap>0 else -1;opt=sign*(q1-q);pm=-sign*(p1-p)
    return dict(opt_toward=opt*100,pm_toward=pm*100,convergence=(abs(gap)-abs(p1-q1))*100,
                opt_fraction=opt/abs(gap),pm_fraction=pm/abs(gap),
                p0=p,q0=q,p1=p1,q1=q1,
                dy=logit(q1)-logit(q),dx=logit(p1)-logit(p),
                future_spot_return=math.log(future['spot']/opening['spot']) if future.get('spot') and opening.get('spot') else None)
