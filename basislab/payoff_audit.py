"""Separate equity payoff sensitivity; never edits reducer Q or starred math.

The PM mass tilt preserves the model's conditional distributions. That is a
testable hypothesis, not identification of a physical distribution from one
binary quote. Scenario ranges and Monte Carlo SE are not confidence intervals
for model error. Quote fits are checked per leg, before any EV is admissible.
"""
import math
import copy

import numpy as np

from .gap_math import execution_cost, payoff
from .market_math import candidates
from .pricing import YEAR_MS, normal_cdf
from .quote_audit import monitoring_intervals, otm_iv
from .semantics import number, probability

VERSION = 'payoff-audit-1.0'
SCENARIOS = (
    ('QUOTE_BID', 'bid', None, 0., 0.),
    ('QUOTE_MID', 'mid', None, 0., 0.),
    ('QUOTE_ASK', 'ask', None, 0., 0.),
    ('HALF_SESSION_VARIANCE', 'mid', .5, 0., 0.),
    ('THREE_QUARTER_SESSION_VARIANCE', 'mid', .75, 0., 0.),
    ('CARRY_STRESS', 'mid', None, .05, .005),
)


def european_value(spot, strike, sigma, years, kind, rate=0., dividend=0.):
    """Discounted European proxy, with explicit annual continuous carry."""
    scale = sigma * math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate - dividend + sigma * sigma / 2) * years) / scale
    s, k = spot * math.exp(-dividend * years), strike * math.exp(-rate * years)
    if kind == 'call':
        return s * normal_cdf(d1) - k * normal_cdf(d1 - scale)
    return k * normal_cdf(scale - d1) - s * normal_cdf(-d1)


def scenario_iv(estimate, spot, years, field, rate, dividend):
    # Re-invert the same OTM quotes under each carry assumption. The provider's
    # published IV never supplies the alternative conditional distribution.
    values, weights = [], []
    for c in estimate['contracts']:
        price = c['bid'] if field == 'bid' else c['ask'] if field == 'ask' else (c['bid'] + c['ask']) / 2
        lo, hi = .0001, 5.
        value = lambda sigma: european_value(spot, c['strike'], sigma, years, c['option_type'], rate, dividend)
        if not value(lo) <= price <= value(hi):
            raise ValueError('OTM_QUOTE_OUTSIDE_CARRY_PRICE_RANGE')
        for _ in range(60):
            mid = (lo + hi) / 2
            if value(mid) < price:
                lo = mid
            else:
                hi = mid
        values.append((lo + hi) / 2)
        weights.append(c['weight'])
    return sum(v * w for v, w in zip(values, weights)) / sum(weights)


def session_paths(spot, threshold, sigma, now, cutoff, expiry, direction,
                  paths=32768, seed=29, session_share=None, rate=0., dividend=0.):
    """Joint regular-session hit mass and later option expiry endpoints.

    A fixed expiry total variance is distributed over regular and unmonitored
    time. None uses uniform calendar variance; other shares are unfitted stress
    assumptions. Every segment evolves prices, including nights after cutoff.
    Bridge survival integrates hits only inside qualifying regular sessions.
    Independent paths share random numbers across scenarios, not across time.
    """
    if min(spot, threshold, sigma) <= 0 or not now < cutoff <= expiry or direction not in ('up', 'down'):
        raise ValueError('INVALID_SESSION_PATH_INPUT')
    if paths < 2 or (session_share is not None and not 0 < session_share < 1):
        raise ValueError('INVALID_VARIANCE_ALLOCATION_OR_PATH_COUNT')
    monitored = monitoring_intervals(now, cutoff)
    regular = monitoring_intervals(now, expiry)
    if not monitored:
        raise ValueError('NO_REMAINING_MONITORED_SESSION')
    boundaries = sorted({now, cutoff, expiry, *(t for pair in regular for t in pair)})
    total = (expiry - now) / YEAR_MS
    regular_years = sum(b - a for a, b in regular) / YEAR_MS
    if session_share is not None and not 0 < regular_years < total:
        raise ValueError('VARIANCE_ALLOCATION_NEEDS_BOTH_REGIMES')
    regular_scale = 1 if session_share is None else total * session_share / regular_years
    outside_scale = 1 if session_share is None else total * (1 - session_share) / (total - regular_years)
    active, regular_set = set(monitored), set(regular)
    opens = {a for a, _ in monitored}
    signed = 1 if direction == 'up' else -1
    barrier = signed * math.log(threshold / spot)
    level, survival = np.zeros(paths), np.ones(paths)
    continuous = np.ones(paths)
    if monitored[0][0] == now and barrier <= 0:
        survival[:] = 0
    if barrier <= 0:
        continuous[:] = 0
    rng = np.random.default_rng(seed)
    for a, b in zip(boundaries, boundaries[1:]):
        years = (b - a) / YEAR_MS
        # Cutoff can split a session; use containment, not equality, for its
        # variance regime and for event monitoring of the segment before it.
        in_regular = any(x <= a and b <= y for x, y in regular_set)
        variance = sigma * sigma * years * (regular_scale if in_regular else outside_scale)
        next_level = level + (rate - dividend) * years - variance / 2 + math.sqrt(variance) * rng.standard_normal(paths)
        if b <= cutoff:
            da, db = barrier - signed * level, barrier - signed * next_level
            safe = np.where((da <= 0) | (db <= 0), 0,
                            -np.expm1(-2 * np.maximum(da, 0) * np.maximum(db, 0) / variance))
            continuous *= safe
            if any(x <= a and b <= y for x, y in active):
                survival *= safe
            elif b in opens:
                survival *= (db > 0)
        level = next_level
    hit = 1 - survival
    ess = lambda w: float(w.sum() ** 2 / np.sum(w * w)) if np.any(w) else 0.
    return dict(terminal=spot * np.exp(level), hit=hit, q=float(hit.mean()),
                q_se=float(hit.std(ddof=1) / math.sqrt(paths)), continuous_control=float((1 - continuous).mean()),
                hit_effective_paths=ess(hit), no_hit_effective_paths=ess(survival),
                discount=math.exp(-rate * total), sessions=len(monitored), paths=paths, seed=seed,
                session_variance_share=regular_years / total if session_share is None else session_share,
                total_variance=sigma * sigma * total, rate=rate, dividend=dividend)


def conditional_statistics(trade, sample, pm_interval, model_value):
    """Analytic vanilla baseline plus a joint-sample conditional mass tilt."""
    values = payoff(trade, sample['terminal']) * sample['discount']
    hit, q = sample['hit'], sample['q']
    if not 0 < q < 1:
        raise ValueError('CONDITIONAL_CLASS_UNAVAILABLE')
    h = float(np.mean(hit * values) / q)
    n = float(np.mean((1 - hit) * values) / (1 - q))
    spread = h - n
    influence = hit * (values - h) / q - (1 - hit) * (values - n) / (1 - q)
    information = [(p - q) * spread for p in pm_interval]
    errors = [float(((p - q) * influence - spread * (hit - q)).std(ddof=1) / math.sqrt(len(hit))) for p in pm_interval]
    exit_legs = [dict(l, side='SELL' if l['side'] == 'BUY' else 'BUY',
                      price=l['bid'] if l['side'] == 'BUY' else l['ask']) for l in trade['legs']]
    exit_cost = execution_cost(exit_legs, trade['multiplier'])
    exit_drag = exit_cost['spread_cost'] + exit_cost['fees'] + exit_cost['slippage']
    entry_drag = trade['spread_cost'] + trade['fees'] + trade['slippage']
    return dict(payoff_if_hit=h, payoff_if_no_hit=n, information_range=sorted(information),
                expiry_ev_range=sorted(model_value + i - trade['debit'] for i in information),
                net_ev_range=sorted(model_value + i - trade['debit'] - exit_drag for i in information),
                mc_standard_error=max(errors), model_value=model_value,
                baseline_ev=model_value - trade['debit'], execution_drag=entry_drag + exit_drag,
                net_information_lower=min(information) - entry_drag - exit_drag,
                expiry_sample_value=float(values.mean()), estimated_exit_drag=exit_drag)


def audit(row, surface, max_loss_limit=None, paths=32768):
    now = row['calculated_at']
    result = dict(version=VERSION, status='UNAVAILABLE', calculated_at=now,
                  event_id=row['event_id'], signal_raw_id=row['raw_id'], input_refs=row.get('input_refs'),
                  surface_raw_id=surface.get('raw_id'), surface_received_ms=surface.get('received_ms'),
                  quote_origin=surface.get('venue'), option_expiry=surface.get('expiry'),
                  signal_timestamp=row.get('timestamp_wall'), pm_timestamp=row.get('pm_timestamp'),
                  max_loss_limit=max_loss_limit, candidates=[], scenarios=[], passing_candidates=0,
                  interpretation='Unfitted sensitivity of discounted expiry payoffs. PM mass tilt is a hypothesis; no predicted repricing or trading edge is established.',
                  limits='Flat IV and European carry proxies for American quotes; unfitted session variance shares; unknown source-clock alignment and Yahoo/Pyth basis. Rates are stress assumptions, not current estimates. Round-trip exit drag uses the current book.',
                  quote_latency_verified=False, captured_execution_state=row.get('execution_state'))
    p_bid, p_ask = probability(row.get('pm_bid')), probability(row.get('pm_ask'))
    if surface.get('venue') not in ('yahoo', 'paperMoney_GUI') or row.get('event_type') != 'touch':
        result['reason'] = 'EQUITY_TOUCH_ONLY'
        return result
    if probability(row.get('opt_yes')) is None or row.get('source_state') in ('VERIFY_HIT', 'HISTORY', 'EXPIRED'):
        result['reason'] = 'NO_VALID_CURRENT_SIGNAL_OR_HISTORY_UNVERIFIED'
        return result
    if row.get('window_start') is not None and row['window_start'] > now:
        result['reason'] = 'FUTURE_MONITORING_WINDOW_UNSUPPORTED'
        return result
    if p_bid is None or p_ask is None or p_bid > p_ask:
        result['reason'] = 'NO_VALID_PM_BID_ASK_INTERVAL'
        return result
    result['pm_interval'] = [p_bid, p_ask]
    if not now < row['expiry'] <= surface['expiry']:
        result['reason'] = 'OPTION_DOES_NOT_COVER_REMAINING_EVENT'
        return result
    estimate = otm_iv(surface, row['spot'], row['strike_or_threshold'], now)
    if not estimate:
        result['reason'] = 'NO_USABLE_OTM_QUOTE_BRACKET'
        return result
    for c in estimate['contracts']:
        c['weight'] = 1 / max(1, abs(c['strike'] - row['strike_or_threshold']))
    result['iv_inputs'] = estimate['contracts']
    years = (surface['expiry'] - now) / YEAR_MS
    # Reuse USD equity debit construction without importing an execution API.
    # Preserve the actual source label on every returned leg.
    regular_surface = dict(surface, venue='yahoo', **{k:[c for c in surface.get(k,[]) if c.get('contract_size','REGULAR') in ('REGULAR','')] for k in ('calls','puts')})
    trades = candidates(regular_surface, row['strike_or_threshold'], row['spot'])
    for trade in trades:
        for leg in trade['legs']:
            leg.update(venue=surface['venue'], quote_quality=surface.get('quote_status', leg['quote_quality']))
    rows = [dict(kind=t['kind'], legs=t['legs'], debit=t['debit'], max_loss=t['max_loss'],
                 execution_drag=None, within_risk_limit=max_loss_limit is not None and t['max_loss'] <= max_loss_limit,
                 scenario_results=[], failures=[]) for t in trades]
    for name, field, share, rate, dividend in SCENARIOS:
        try:
            sigma = scenario_iv(estimate, row['spot'], years, field, rate, dividend)
            sample = session_paths(row['spot'], row['strike_or_threshold'], sigma, now, row['expiry'],
                                   surface['expiry'], row['direction'], paths=paths,
                                   session_share=share, rate=rate, dividend=dividend)
        except ValueError as error:
            result['scenarios'].append(dict(name=name, status='UNAVAILABLE', reason=str(error)))
            continue
        result['scenarios'].append(dict(name=name, status='OK', sigma=sigma,
                                       **{k:v for k,v in sample.items() if k not in ('terminal', 'hit', 'discount')}))
        for trade, summary in zip(trades, rows):
            leg_values = [european_value(row['spot'], l['strike'], sigma, years, l['option_type'], rate, dividend) for l in trade['legs']]
            fits = all(l['bid'] - .005 <= v <= l['ask'] + .005 for l, v in zip(trade['legs'], leg_values))
            baseline = sum((1 if l['side'] == 'BUY' else -1) * v * trade['multiplier'] for l,v in zip(trade['legs'], leg_values))
            detail = dict(name=name, q=sample['q'], leg_values=leg_values,
                          calibration='WITHIN_EACH_LEG_BOOK' if fits else 'MODEL_LEG_OUTSIDE_BOOK')
            try:
                detail.update(conditional_statistics(trade, sample, result['pm_interval'], baseline))
                if min(sample['hit_effective_paths'], sample['no_hit_effective_paths']) < 128:
                    detail['reason'] = 'LOW_CONDITIONAL_EFFECTIVE_SAMPLE'
            except ValueError as error:
                detail['reason'] = str(error)
            summary['scenario_results'].append(detail)
    for summary in rows:
        details = summary['scenario_results']
        if len(details) != len(SCENARIOS) or any(d.get('reason') for d in details):
            summary['failures'].append('CONDITIONAL_SCENARIO_UNAVAILABLE')
        if any(d['calibration'] != 'WITHIN_EACH_LEG_BOOK' for d in details):
            summary['failures'].append('MODEL_LEG_MISMATCH')
        usable = [d for d in details if d.get('net_ev_range') is not None]
        if usable:
            summary.update(worst_net_ev=min(d['net_ev_range'][0] for d in usable),
                           best_net_ev=max(d['net_ev_range'][1] for d in usable),
                           conservative_net_ev=min(d['net_ev_range'][0] - 2*d['mc_standard_error'] for d in usable),
                           conservative_information=min(d['net_information_lower'] - 2*d['mc_standard_error'] for d in usable),
                           calibrated_scenarios=sum(d['calibration'] == 'WITHIN_EACH_LEG_BOOK' for d in details),
                           execution_drag=max(d['execution_drag'] for d in usable))
            if min(summary['conservative_net_ev'], summary['conservative_information']) <= 0:
                summary['failures'].append('EDGE_NOT_ROBUST_TO_QUOTES_AND_SCENARIOS')
        else:
            summary['failures'].append('NO_CONDITIONAL_PAYOFF')
        if not summary['within_risk_limit']:
            summary['failures'].append('RISK_LIMIT_UNCONFIGURED' if max_loss_limit is None else 'ABOVE_RISK_LIMIT')
        if row.get('execution_state') in ('MARKET_CLOSED', 'QUOTE_STALE'):
            summary['failures'].append(row['execution_state'])
        summary['sensitivity_pass'] = not summary['failures']
    result.update(status='RESEARCH_ONLY', candidates=sorted(rows, key=lambda c:(c['sensitivity_pass'], c['within_risk_limit'], c.get('conservative_net_ev', -math.inf)), reverse=True),
                  passing_candidates=sum(c['sensitivity_pass'] for c in rows))
    result['decision'] = 'UNVALIDATED_CANDIDATE_REQUIRES_VENUE_AND_FORWARD_TEST' if result['passing_candidates'] else 'NO_ROBUST_CANDIDATE'
    return result


def from_snapshot(row, book=None, max_loss_limit=None, paths=32768):
    """Offline reproduction from a captured BASIS detail and optional GUI book.

    A normalized GUI book has asset, expiry, spot, received_ms, source_ms,
    quote_status and contracts (instrument, strike, option_type, bid, ask).
    Contracts must match the captured BASIS chain exactly. Receipt time is not
    exchange time; this function cannot validate quote synchrony or place orders.
    """
    row = copy.deepcopy(row)
    original_calculated = row['calculated_at']
    chain = row.get('chain', [])
    surface = dict(venue='yahoo', expiry=row.get('option_expiry'), raw_id=row.get('surface_raw_id'),
                   received_ms=chain[0]['received_ms'] if chain else None,
                   calls=[c for c in chain if c['option_type'] == 'call'],
                   puts=[c for c in chain if c['option_type'] == 'put'])
    if book is not None:
        if book.get('asset') != row['asset'] or book.get('expiry') != surface['expiry']:
            raise ValueError('GUI_BOOK_UNDERLYING_OR_EXPIRY_MISMATCH')
        received, spot = number(book.get('received_ms')), number(book.get('spot'))
        if received is None or received < original_calculated or spot is None or spot <= 0:
            raise ValueError('GUI_BOOK_PRECEDES_SIGNAL_OR_HAS_INVALID_SPOT')
        source = number(book.get('source_ms'))
        if book.get('source_ms') is not None and (source is None or source > received):
            raise ValueError('INVALID_GUI_SOURCE_CLOCK')
        originals = {c['instrument']: c for c in chain}
        contracts, seen = [], set()
        for c in book.get('contracts', []):
            old = originals.get(c.get('instrument'))
            bid, ask = number(c.get('bid')), number(c.get('ask'))
            if not old or c['instrument'] in seen or any(c.get(k) != old.get(k) for k in ('strike', 'option_type')):
                raise ValueError('GUI_CONTRACT_DOES_NOT_MATCH_CAPTURED_BASIS_CHAIN')
            if bid is None or ask is None or not 0 <= bid <= ask or ask <= 0:
                raise ValueError('INVALID_GUI_OPTION_BOOK')
            seen.add(c['instrument'])
            contracts.append(dict(old, bid=bid, ask=ask, received_ms=received,
                                  source_ms=source, mark=None, venue='paperMoney_GUI'))
        if not contracts or not book.get('quote_status'):
            raise ValueError('GUI_BOOK_REQUIRES_CONTRACTS_AND_DELAY_STATUS')
        row.update(spot=spot, calculated_at=received)
        surface.update(venue='paperMoney_GUI', raw_id=None, received_ms=received,
                       quote_status=book['quote_status'],
                       calls=[c for c in contracts if c['option_type'] == 'call'],
                       puts=[c for c in contracts if c['option_type'] == 'put'])
    result = audit(row, surface, max_loss_limit, paths)
    result['original_calculated_at'] = original_calculated
    result['signal_to_quote_receipt_ms'] = None if book is None else book['received_ms'] - original_calculated
    return result
