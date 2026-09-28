"""Explainable underlying/instrument analysis. No claim of an LLM or predictive EV."""
import math
import statistics

VERSION='underlying-thesis-1.0.0'
YEAR=365.25*86400


def underlying_thesis(row,history,now):
    facts={k:row.get(k) for k in ('asset','spot','event_type','direction','strike_or_threshold','expiry','pm_yes','opt_yes','gap_pp','model_confidence','mapping_confidence')}
    context=row.get('underlying_context',{})
    facts['stock_context']=context
    spot=row.get('spot');strike=row.get('strike_or_threshold');gap=row.get('gap_pp')
    reasons=[]
    usable=sorted((r for r in history if r['timestamp_wall']<=now and r.get('spot') and r.get('source_state') in ('OK','PROXY')),key=lambda r:r['timestamp_wall'])
    returns={}
    for seconds in (300,900,3600):
        past=next((r for r in reversed(usable) if r['timestamp_wall']<=now-seconds*1000),None)
        if past and now-seconds*1000-past['timestamp_wall']<=60000 and spot:
            returns[str(seconds)]=spot/past['spot']-1
    # One-minute as-of points; do not annualize sub-second duplicated observations.
    samples=[]
    for r in usable:
        if r['timestamp_wall']<now-3600000:continue
        if samples and r['timestamp_wall']-samples[-1]['timestamp_wall']>120000:samples=[]
        if not samples or r['timestamp_wall']-samples[-1]['timestamp_wall']>=60000:samples.append(r)
    rv=None
    if len(samples)>=11:
        changes=[math.log(b['spot']/a['spot']) for a,b in zip(samples,samples[1:])]
        dt=(samples[-1]['timestamp_wall']-samples[0]['timestamp_wall'])/1000/len(changes)
        rv=statistics.stdev(changes)*math.sqrt(YEAR/dt)
    iv=row.get('surface_features',{}).get('local_iv') or row.get('model_inputs',{}).get('iv')
    facts.update(spot_returns=returns,realized_vol_1h=rv,local_iv=iv,
        local_skew=row.get('surface_features',{}).get('local_iv_skew'),
        threshold_distance_pct=100*(strike/spot-1) if strike and spot else None,
        hours_to_event=(row.get('expiry',now)-now)/3600000)
    direction=1 if gap is not None and gap*(1 if row.get('direction')=='up' else -1)>0 else -1
    if row.get('source_state') not in ('OK','PROXY'):reasons.append('Event comparison is not currently usable: '+str(row.get('source_state')))
    if not spot or not iv or not math.isfinite(iv) or iv<=0:reasons.append('Underlying spot or usable implied volatility missing')
    if len(returns)<2:reasons.append('Need fresh 5m and 15m underlying price history')
    elif all(returns.get(str(s),0)*direction<-.001 for s in (300,900)):
        reasons.append('Proposed direction opposes both 5m and 15m underlying moves')
    if rv is not None and rv>0 and iv and iv>2*rv:reasons.append('Long premium is expensive versus observed realized volatility (IV/RV > 2)')
    if facts['hours_to_event']<2:reasons.append('Event cutoff is less than two hours away')
    if context:
        if context.get('market_state')!='OPEN':reasons.append('Stock regular session is closed')
        catalyst=context.get('catalyst',{})
        if catalyst.get('status')!='AVAILABLE':reasons.append('Earnings calendar is unavailable; stock catalyst risk unreviewed')
        if any(now-86400000<=t<=row.get('expiry',now)+86400000 for t in catalyst.get('earnings_dates',[])):
            reasons.append('Earnings overlaps the event; a jump model is required before directional automation')
        if context.get('corporate_action'):reasons.append('Stock split in reference window requires mapping review')
    proposition=f"{row.get('asset')} {row.get('event_type')} {row.get('direction')} {strike}"
    interpretation=('Touch odds encode path and volatility; a vanilla option does not replicate the proposition. '
                    if row.get('event_type')=='touch' else 'A terminal probability is not an expected stock return. ')
    interpretation+='A directional expression is considered only with fresh PM movement, neighboring agreement, underlying context and acceptable cost.'
    return dict(version=VERSION,event_id=row['event_id'],timestamp=now,input_raw_id=row.get('raw_id'),
        input_refs=row.get('input_refs',{}),eligible=not reasons,status='CONTEXT_READY' if not reasons else 'WATCH',
        proposed_direction='bullish' if direction>0 else 'bearish',proposition=proposition,
        interpretation=interpretation,facts=facts,rejection_reasons=reasons,
        limitations=['Descriptive model checks, not a forecast or expected-value estimate','No news/fundamental agent is running','Flat-volatility and zero-carry approximations'])


def option_value(spot,strike,iv,years,put=False):
    if years<=0:return max(0,strike-spot if put else spot-strike)
    n=lambda x:(1+math.erf(x/math.sqrt(2)))/2
    scale=iv*math.sqrt(years);d1=(math.log(spot/strike)+.5*iv*iv*years)/scale;d2=d1-scale
    return strike*n(-d2)-spot*n(-d1) if put else spot*n(d1)-strike*n(d2)


def expression_thesis(thesis,quote,quantity,desk,now):
    facts=thesis['facts'];spot=facts['spot'];iv=quote.get('iv') or facts['local_iv']
    horizon=min(3600,max(0,(quote['expiry']-now)/1000));years=(quote['expiry']-now)/1000/YEAR
    result=dict(thesis,expression=dict(instrument=quote['instrument'],kind=quote.get('option_type'),strike=quote['strike'],
        expiry=quote['expiry'],quantity=quantity,multiplier=quote['multiplier'],quote_raw_id=quote['raw_id'],
        local_iv=iv,holding_seconds=horizon,estimated_quote=quote.get('estimated'),scenarios=[]))
    if not spot or not iv or years<=0:
        result.update(eligible=False,rejection_reasons=thesis['rejection_reasons']+['Cannot price an underlying stress scenario']);return result
    put=quote.get('option_type')=='put';base=option_value(spot,quote['strike'],iv,years,put)
    mid=(quote['bid']+quote['ask'])/2;half=(quote['ask']-quote['bid'])/2
    buy=desk.costs(quote,'BUY',quantity)
    scale=min(iv,facts.get('realized_vol_1h') or iv)*math.sqrt(horizon/YEAR)
    scenarios=[]
    for name,move in (('down_one_sigma',-scale),('flat_spot',0),('up_one_sigma',scale)):
        value=option_value(spot*math.exp(move),quote['strike'],iv,max(0,years-horizon/YEAR),put)
        mark=max(0,mid+value-base)
        sell_quote=dict(quote,bid=max(0,mark-half),ask=mark+half)
        sell=desk.costs(sell_quote,'SELL',quantity)
        scenarios.append(dict(name=name,spot=spot*math.exp(move),net_pnl=sell['net_credit']-buy['total_debit']))
    result['expression'].update(scenarios=scenarios,entry_debit=buy['total_debit'],
        assumption='One-hour spot shocks; constant IV and quoted spread; model changes anchored to quote midpoint. These are scenarios, not outcome probabilities.')
    supportive=scenarios[0] if put else scenarios[2]
    if supportive['net_pnl']<=0:
        result.update(eligible=False,rejection_reasons=thesis['rejection_reasons']+['Even a favorable one-sigma underlying move fails to cover costs and decay'])
    return result
