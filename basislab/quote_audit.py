"""Equity signal sensitivity using captured books and actual monitoring hours.

This diagnostic does not reinterpret recorded Q, change the archived reducer,
or infer an execution edge. Flat IV, zero carry and American quotes remain
proxies; session monitoring isolates one specific semantic approximation.
"""
from datetime import datetime, timezone
import math

import numpy as np

from .equities import calendar
from .pricing import YEAR_MS, normal_cdf, touch_probability, terminal_probability
from .semantics import number

VERSION = 'quote-audit-1.0'


def option_value(spot, strike, sigma, years, kind):
    scale = sigma * math.sqrt(years)
    d1 = (math.log(spot / strike) + sigma * sigma * years / 2) / scale
    if kind == 'call':
        return spot * normal_cdf(d1) - strike * normal_cdf(d1 - scale)
    return strike * normal_cdf(scale - d1) - spot * normal_cdf(-d1)


def implied_iv(spot, strike, years, price, kind):
    if kind not in ('call', 'put') or any(number(x) is None or x <= 0 for x in (spot, strike, years)):
        return None
    intrinsic = max(spot - strike, 0) if kind == 'call' else max(strike - spot, 0)
    maximum = spot if kind == 'call' else strike
    if number(price) is None or not intrinsic < price < maximum:
        return None
    lo, hi = .0001, 5.0
    if not option_value(spot, strike, lo, years, kind) <= price <= option_value(spot, strike, hi, years, kind):
        return None
    for _ in range(60):
        mid = (lo + hi) / 2
        if option_value(spot, strike, mid, years, kind) < price:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def otm_iv(surface, spot, threshold, now):
    years = (surface['expiry'] - now) / YEAR_MS
    choices = []
    for kind in ('call', 'put'):
        for contract in surface.get(kind + 's', []):
            strike, bid, ask = (number(contract.get(k)) for k in ('strike', 'bid', 'ask'))
            if strike is None or not ((kind == 'put' and strike < spot) or (kind == 'call' and strike >= spot)):
                continue
            if bid is None or ask is None or not 0 < bid <= ask:
                continue
            if contract.get('contract_size', 'REGULAR') not in ('REGULAR', ''):
                continue
            values = [implied_iv(spot, strike, years, p, kind) for p in (bid, (bid + ask) / 2, ask)]
            if any(v is None for v in values):
                continue
            choices.append(dict(instrument=contract['instrument'], option_type=kind, strike=strike,
                                bid=bid, ask=ask, bid_iv=values[0], mid_iv=values[1], ask_iv=values[2]))
    choices.sort(key=lambda c: (abs(c['strike'] - threshold), c['instrument']))
    choices = choices[:3]
    if not choices:
        return None
    weights = [1 / max(1, abs(c['strike'] - threshold)) for c in choices]
    result = {field: sum(c[field] * w for c, w in zip(choices, weights)) / sum(weights)
              for field in ('bid_iv', 'mid_iv', 'ask_iv')}
    return dict(result, contracts=choices, method='OTM_BID_MID_ASK_ZERO_CARRY_IV_INVERSION')


def monitoring_intervals(now, cutoff):
    start = datetime.fromtimestamp(now / 1000, timezone.utc).date().isoformat()
    end = datetime.fromtimestamp(cutoff / 1000, timezone.utc).date().isoformat()
    cal = calendar(start)
    intervals = []
    for session in cal.sessions_in_range(start, end):
        opened, closed = (int(getattr(cal, 'session_' + field)(session).timestamp() * 1000) for field in ('open', 'close'))
        a, b = max(now, opened), min(cutoff, closed)
        if a < b:
            intervals.append((a, b))
    return intervals


def session_probability(spot, threshold, sigma, now, cutoff, direction, paths=32768, seed=19):
    """GBM evolves through nights; only session endpoints/bridges count as hits.

    Conditional Brownian-bridge survival integrates within each regular session.
    No bridge through an unmonitored night/weekend can resolve the event. The
    same simulated endpoints give a continuous-monitoring control. This does
    not estimate a session/overnight variance split or a Pyth reference basis.
    """
    intervals = monitoring_intervals(now, cutoff)
    if not intervals or direction not in ('up', 'down') or min(spot, threshold, sigma) <= 0:
        return None
    boundaries = sorted({now, cutoff, *(t for pair in intervals for t in pair)})
    active = set(intervals)
    rng = np.random.default_rng(seed)
    signed = 1 if direction == 'up' else -1
    barrier = signed * math.log(threshold / spot)
    level = np.zeros(paths)
    survival = np.ones(paths)
    continuous_survival = np.ones(paths)
    # If now is inside a monitored session, current spot is a qualifying endpoint.
    if intervals[0][0] == now and barrier <= 0:
        survival[:] = 0
    if barrier <= 0:
        continuous_survival[:] = 0
    for a, b in zip(boundaries, boundaries[1:]):
        years = (b - a) / YEAR_MS
        next_level = level - sigma * sigma * years / 2 + sigma * math.sqrt(years) * rng.standard_normal(paths)
        da, db = barrier - signed * level, barrier - signed * next_level
        bridge_survival = np.where((da <= 0) | (db <= 0), 0,
                                  -np.expm1(np.minimum(0, -2 * np.maximum(da, 0) * np.maximum(db, 0) / (sigma * sigma * years))))
        continuous_survival *= bridge_survival
        if (a, b) in active:
            survival *= bridge_survival
        elif any(b == opened for opened, _ in intervals):
            survival *= (db > 0)
        level = next_level
    hits = 1 - survival
    return dict(probability=float(hits.mean()), standard_error=float(hits.std(ddof=1) / math.sqrt(paths)),
                continuous_control=float((1 - continuous_survival).mean()), paths=paths, seed=seed,
                sessions=len(intervals), monitoring='XNYS_REGULAR_SESSION_CONTINUOUS_BRIDGES',
                variance_clock='CALENDAR_TIME_INCLUDING_UNMONITORED_NIGHTS')


def audit(row, surface, now):
    result = dict(version=VERSION, status='UNAVAILABLE', recorded_q=row.get('opt_yes'),
                  recorded_gap_pp=row.get('gap_pp'), calculated_at=now,
                  limitation='Sensitivity diagnostic, not a fitted strategy or proven edge. OTM American quotes, zero carry, flat IV, uniform calendar variance and Yahoo/Pyth reference mismatch remain proxies.')
    if surface.get('venue') != 'yahoo' or not row.get('spot') or row.get('opt_yes') is None:
        return result
    estimate = otm_iv(surface, row['spot'], row['strike_or_threshold'], now)
    if not estimate:
        result['reason'] = 'NO_USABLE_OTM_QUOTE_BRACKET'
        return result
    years = (row['expiry'] - now) / YEAR_MS
    if years <= 0:
        return result
    transform = touch_probability if row['event_type'] == 'touch' else terminal_probability
    bracket = [transform(row['spot'], row['strike_or_threshold'], estimate[k], years, row['direction'])
               for k in ('bid_iv', 'mid_iv', 'ask_iv')]
    result.update(status='SENSITIVITY_ONLY', otm=estimate, otm_continuous_q=bracket,
                  otm_continuous_gap_pp=[100 * (row['pm_yes'] - q) for q in bracket],
                  surface_raw_id=surface['raw_id'], surface_received_ms=surface['received_ms'])
    if row['event_type'] == 'touch':
        session = session_probability(row['spot'], row['strike_or_threshold'], estimate['mid_iv'], now, row['expiry'], row['direction'])
        result.update(session=session, session_gap_pp=None if session is None else 100 * (row['pm_yes'] - session['probability']))
    alternatives = bracket + ([result['session']['probability']] if result.get('session') else [])
    base_sign = row['pm_yes'] - row['opt_yes']
    result['direction_survives'] = all(base_sign * (row['pm_yes'] - q) > 0 for q in alternatives)
    result['gap_retained_fraction'] = min(abs(row['pm_yes'] - q) for q in alternatives) / abs(base_sign) if base_sign else None
    return result
