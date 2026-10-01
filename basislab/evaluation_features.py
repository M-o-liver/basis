"""Causal evaluation features. These do not change the research reducer."""
from datetime import datetime, timedelta
from functools import lru_cache
import math
from .equities import NY, calendar
from .evaluation import digest
from .tape import SEMANTICS


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def sign(value):
    return (value > 0) - (value < 0)


def semantic_version(row):
    return digest({key: row.get(key) for key in SEMANTICS})


def compact(row):
    """Only latest working state is mutable; no repeated candles/books in evaluation."""
    result = {k: v for k, v in row.items() if k not in ('pm_depth', 'underlying_context')}
    context = row.get('underlying_context') or {}
    if context:
        result['underlying_context'] = {k: context.get(k) for k in
            ('market_state', 'price_asof', 'quote_delay', 'regular_hours_only', 'session_returns',
             'catalyst', 'corporate_actions', 'corporate_action') if k in context}
    return result


@lru_cache(maxsize=512)
def session_times(day):
    cal = calendar(day)
    current = cal.date_to_session(day, direction='next')
    opening = int(cal.session_open(current).timestamp()*1000)
    closing = int(cal.session_close(current).timestamp()*1000)
    previous = cal.previous_session(current)
    previous_close = int(cal.session_close(previous).timestamp()*1000)
    return opening, closing, previous_close, str(current.date()) == day


def session_regime(row, now):
    if row['asset'] in ('BTC', 'ETH'):
        return dict(regime='BOTH_OPEN', target_open=True, next_open=None,
                    expiry_near=0 <= row['expiry']-now <= 7200000)
    dt = datetime.fromtimestamp(now/1000, NY)
    opening, closing, previous_close, is_session = session_times(dt.date().isoformat())
    is_open = is_session and opening <= now < closing
    if is_open:
        regime = 'OPEN_AUCTION / FIRST PRINT' if now-opening < 60000 else 'BOTH_OPEN'
        next_open = opening
        close_reference = previous_close
    elif is_session and now >= closing:
        next_day = (dt.date()+timedelta(days=1)).isoformat()
        next_open = session_times(next_day)[0]
        close_reference = closing
        regime = 'AFTER_HOURS' if dt.hour < 20 else 'POLY_OPEN_TARGET_CLOSED'
    else:
        next_open = opening
        close_reference = previous_close
        regime = 'WEEKEND' if dt.weekday() >= 5 else 'PREMARKET' if is_session and dt.hour >= 4 else 'POLY_OPEN_TARGET_CLOSED'
    return dict(regime=regime, target_open=is_open, next_open=next_open,
                target_close=close_reference, expiry_near=0 <= row['expiry']-now <= 7200000,
                execution_quality='YAHOO_DELAYED_PROXY; first available proxy is not an auction print')


def quality_reason(row, due, protocol, target_open=True):
    if row is None:
        return 'NO_FRAME'
    age = due-row['timestamp_wall']
    if age < 0:
        return 'FUTURE_FRAME'
    if age > protocol['frame_max_age_ms']:
        return 'FRAME_STALE'
    if not all(finite(row.get(k)) and 0 <= row[k] <= 1 for k in ('pm_yes', 'opt_yes')):
        return 'MISSING_OR_INVALID_PROBABILITY'
    if row.get('source_state') not in ('OK', 'PROXY'):
        return 'TARGET_CLOSED' if row.get('source_state') == 'CLOSED' else row.get('source_state', 'UNKNOWN_QUALITY')
    if 'WIDE_PM_BOOK' in row.get('quality_flags', []):
        return 'WIDE_PM_BOOK'
    if row.get('pm_source') == 'gamma_indicative':
        return 'INDICATIVE_PM_NOT_LIVE_BOOK'
    if any(ref > row['raw_id'] for ref in row.get('input_refs', {}).values()):
        return 'FUTURE_RAW_REFERENCE'
    if row['asset'] not in ('BTC', 'ETH') and target_open and not session_regime(row, due)['target_open']:
        return 'TARGET_CLOSED'
    return None


def bin_index(value, edges):
    if not finite(value):
        return 'MISSING'
    return next((i for i, (a, b) in enumerate(zip(edges, edges[1:])) if a <= value < b), 'OUTSIDE')


def local_iv(row):
    return (row.get('surface_features') or {}).get('local_iv', (row.get('model_inputs') or {}).get('iv'))


def temporal_predictor(name, output):
    diagnostics=output.get('diagnostics',{})
    if name=='lead_lag_multiscale':
        values=[r['strength'] for r in diagnostics.get('scales',[]) if r.get('direction')!='INSUFFICIENT' and finite(r.get('strength'))]
    elif name=='event_sync':
        values=[]
        for r in diagnostics.get('windows',[]):
            a,b=r.get('pm_then_opt',{}),r.get('opt_then_pm',{})
            if a.get('triggers',0)>0 and b.get('triggers',0)>0 and finite(a.get('probability')) and finite(b.get('probability')):
                values.append(a['probability']-b['probability'])
    else:
        return None
    return sum(values)/len(values) if values else None


def matching_key(row, regime, protocol):
    distance = abs(math.log(row['strike_or_threshold']/row['spot'])) if row.get('spot', 0) and row.get('strike_or_threshold', 0) else None
    return [row['asset'], row['event_type'], row['direction'],
            bin_index(row.get('opt_yes'), protocol['probability_bins']),
            bin_index((row['expiry']-row['timestamp_wall'])/3600000, protocol['time_to_expiry_bins_hours']),
            regime, bin_index(local_iv(row), protocol['iv_bins']), bin_index(distance, protocol['distance_bins'])]


def neighborhood(row, panel, protocol):
    group = ('asset', 'expiry', 'event_type', 'direction', 'window_start', 'settlement_source', 'threshold_inclusive')
    nodes = []
    for peer in panel.values():
        if all(row.get(k) == peer.get(k) for k in group) and not quality_reason(peer, row['timestamp_wall'], protocol):
            nodes.append(dict(event_id=peer['event_id'], event_version=semantic_version(peer),
                strike=peer['strike_or_threshold'], pm=peer['pm_yes'], opt=peer['opt_yes'],
                gap_pp=peer['gap_pp'], raw_id=peer['raw_id'], frame_id=peer.get('observation_id')))
    nodes.sort(key=lambda n: (n['strike'], n['event_id']))
    other = [n for n in nodes if n['event_id'] != row['event_id']]
    agreement = sum(sign(n['gap_pp']) == sign(row['gap_pp']) for n in other)/len(other) if other and finite(row.get('gap_pp')) else None
    return dict(group={k: row.get(k) for k in group}, nodes=nodes, agreement=agreement,
                description='Comparable touch/terminal curves; neighboring events are dependent')


def curve_shape(nodes, direction):
    ordered = sorted(nodes, key=lambda x: x['strike'])
    monotonic = {leg: sum(sign(b[leg]-a[leg]) == (1 if direction == 'up' else -1)
                         for a, b in zip(ordered, ordered[1:])) for leg in ('pm', 'opt')}
    crossings = sum(sign(a['pm']-a['opt'])*sign(b['pm']-b['opt']) < 0 for a, b in zip(ordered, ordered[1:]))
    curvature = {leg: [((c[leg]-b[leg])/(c['strike']-b['strike'])-(b[leg]-a[leg])/(b['strike']-a['strike']))
                       for a,b,c in zip(ordered,ordered[1:],ordered[2:]) if a['strike'] < b['strike'] < c['strike']]
                 for leg in ('pm', 'opt')}
    return dict(monotonicity_violations=monotonic, crossings=crossings, curvature=curvature)


def horizon_outcome(entry, row, panel, due, protocol):
    """As-of selection is done by the recorder; reject future/stale or different semantics."""
    reason = quality_reason(row, due, protocol)
    if not entry['eligible']:
        reason = entry.get('ineligible_reason') or 'INELIGIBLE_ENTRY'
    if row and semantic_version(row) != entry['event_version']:
        reason = 'EVENT_VERSION_CHANGED'
    if row and entry.get('timestamp_monotonic') is not None and row.get('timestamp_monotonic') is not None:
        elapsed_ns=row['timestamp_monotonic']-entry['timestamp_monotonic']
        if elapsed_ns<0:
            reason='MONOTONIC_EPOCH_CHANGED; horizon unavailable across an unverified reboot'
        elif elapsed_ns>(due-entry['opened_ms'])*1000000+1000000:
            reason='CLOCK_DISCONTINUITY; frame arrived after monotonic horizon'
    session = entry['session']
    deferred=entry['kind']=='DEFERRED_RESPONSE' or bool(entry.get('deferred_context')) and not session['target_open']
    if deferred and entry['eligible'] and due<session['next_open']:
        reason='DEFERRED_UNTIL_TARGET_OPEN'
    if row and deferred and due >= session['next_open']:
        if (row.get('options_received_ms') or 0) < session['next_open'] or (row.get('spot_timestamp') or 0) < session['next_open']:
            reason = 'NO_POST_OPEN_TARGET_PROXY'
    data = dict(due_ms=due, asof_frame_id=row.get('observation_id') if row else None,
                asof_raw_id=row.get('raw_id') if row else None,
                asof_timestamp_ms=row.get('timestamp_wall') if row else None,
                quality={k: row.get(k) for k in ('source_state','quality_flags','model_confidence','pm_source',
                    'pm_timestamp','opt_timestamp','options_received_ms','spot_timestamp')} if row else {},
                reason=reason, status='MISSING' if reason else 'OBSERVED')
    data.update(pm_h=None,opt_h=None,spot_h=None,gap_h_pp=None,
        last_known_state={k:row.get(k) for k in ('pm_yes','opt_yes','spot','gap_pp','timestamp_wall')}
            if row and row['timestamp_wall']<=due and not (reason or '').startswith(('CLOCK_DISCONTINUITY','MONOTONIC_EPOCH_CHANGED')) else None)
    if reason:
        return data
    p, q = entry['pm_yes'], entry['opt_yes']
    d = sign(p-q)
    dp, dq = 100*(row['pm_yes']-p), 100*(row['opt_yes']-q)
    g0, gh = 100*(p-q), 100*(row['pm_yes']-row['opt_yes'])
    eps = protocol['movement_epsilon_pp']
    toward_opt, toward_pm = d*dq, -d*dp
    if toward_opt > eps and abs(dp) <= eps:
        who = 'OPT_TOWARD_OPENING_PM; PM_APPROXIMATELY_STAYED'
    elif toward_pm > eps and abs(dq) <= eps:
        who = 'PM_TOWARD_OPENING_OPT'
    elif toward_opt > eps and toward_pm > eps:
        who = 'BOTH_TOWARD_EACH_OTHER'
    elif dp*dq > 0 and abs(dp) > eps and abs(dq) > eps:
        who = 'BOTH_SAME_DIRECTION; COMMON_INFORMATION_POSSIBLE'
    elif abs(gh)-abs(g0) > eps:
        who = 'GAP_WIDENED'
    else:
        who = 'STABLE_OR_MIXED'
    travel = abs(dp)+abs(dq)
    data.update(pm_h=row['pm_yes'], opt_h=row['opt_yes'], spot_h=row.get('spot'), gap_h_pp=gh,
        delta_pm_pp=dp, delta_opt_pp=dq, delta_spot=(row['spot']-entry['spot']) if finite(row.get('spot')) and finite(entry.get('spot')) else None,
        spot_return=(row['spot']/entry['spot']-1) if row.get('spot') and entry.get('spot') else None,
        directional_opt_move_pp=toward_opt, directional_pm_move_pp=toward_pm,
        convergence_pp=abs(g0)-abs(gh), opt_moved_in_poly_direction=toward_opt > eps,
        opt_distance_to_opening_pm_change_pp=abs(g0)-100*abs(p-row['opt_yes']),
        pm_moved_toward_opening_opt=toward_pm > eps, movement_class=who,
        pm_travel=abs(dp)/travel if travel else None, opt_travel=abs(dq)/travel if travel else None)
    if finite(data.get('spot_return')):
        spot_direction=sign(data['spot_return'])*(1 if entry['direction']=='up' else -1)
        data['both_moves_spot_direction_consistent']=bool(spot_direction and sign(dp)==spot_direction and sign(dq)==spot_direction)
    opening = entry.get('neighbors', {}).get('nodes', [])
    later = []
    pairs = []
    for node in opening:
        peer = panel.get(node['event_id'])
        if peer and not quality_reason(peer,due,protocol) and semantic_version(peer) == node['event_version']:
            h = dict(node, pm=peer['pm_yes'], opt=peer['opt_yes'])
            later.append(h); pairs.append((node,h))
    if len(pairs) >= 2:
        mean = lambda xs: sum(xs)/len(xs)
        initial = mean([abs(a['pm']-a['opt']) for a,b in pairs])
        data['curve'] = dict(nodes=len(pairs), missing_nodes=len(opening)-len(pairs),
            opt_toward_opening_pm_pp=100*(initial-mean([abs(a['pm']-b['opt']) for a,b in pairs])),
            pm_toward_opening_opt_pp=100*(initial-mean([abs(a['opt']-b['pm']) for a,b in pairs])),
            convergence_pp=100*(initial-mean([abs(b['pm']-b['opt']) for a,b in pairs])),
            opening=curve_shape([a for a,b in pairs],entry['direction']), later=curve_shape(later,entry['direction']))
    else:
        data['curve'] = dict(status='INSUFFICIENT_COMPARABLE_NODES', nodes=len(pairs), missing_nodes=len(opening)-len(pairs))
    return data
