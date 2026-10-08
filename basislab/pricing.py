"""Deterministic probability transforms. All clocks and source inputs are explicit."""
import math
from . import CALCULATION_VERSION
from .semantics import number, probability
YEAR_MS = 365 * 86400000


def normal_cdf(x):
    return 0.5 * math.erfc(-x / math.sqrt(2))


def terminal_probability(spot, strike, sigma, years, direction):
    if any(number(x) is None or x <= 0 for x in (spot, strike, sigma, years)) or direction not in ('up', 'down'):
        return None
    d2 = (math.log(spot / strike) - sigma * sigma * years / 2) / (sigma * math.sqrt(years))
    return normal_cdf(d2 if direction == 'up' else -d2)


def touch_probability(spot, strike, sigma, years, direction):
    if any(number(x) is None or x <= 0 for x in (spot, strike, sigma, years)) or direction not in ('up', 'down'):
        return None
    if (direction == 'up' and spot >= strike) or (direction == 'down' and spot <= strike):
        return 1.0
    distance = math.log(strike / spot) if direction == 'up' else math.log(spot / strike)
    drift = sigma * sigma / 2 * (1 if direction == 'up' else -1)
    scale = sigma * math.sqrt(years)
    # Reflection-principle first passage for GBM with zero carry.
    exponent = -2 * drift * distance / (sigma * sigma)
    if exponent > 700:
        return None
    result = normal_cdf((-distance - drift * years) / scale) + math.exp(exponent) * normal_cdf((-distance + drift * years) / scale)
    return min(1.0, max(0.0, result))


def discrepancy(pm, opt):
    p, q = probability(pm), probability(opt)
    if p is None or q is None:
        return dict(gap_pp=None, relative_gap=None, side=None)
    gap = p - q
    relative = abs(gap) / q if q > 0 else None
    # Subnormal tail probabilities can overflow the ratio even with valid inputs.
    # Keep the absolute gap; an unrepresentable REL is unavailable, never infinity.
    return dict(gap_pp=gap * 100, relative_gap=relative if relative is not None and math.isfinite(relative) else None,
                side='CHEAP' if gap > 0 else 'RICH' if gap < 0 else 'EVEN')


def finite_spread(calls, strike):
    below = [c for c in calls if c.get('strike', 0) < strike]
    above = [c for c in calls if c.get('strike', 0) > strike]
    if not below or not above:
        return None
    lo, hi = max(below, key=lambda c: c['strike']), min(above, key=lambda c: c['strike'])
    def mid(c):
        b, a, m = number(c.get('bid')), number(c.get('ask')), number(c.get('mark'))
        if b is not None and a is not None:
            return (a + b) / 2 if 0 <= b <= a else None
        return m if m is not None and m >= 0 else None
    l, h = mid(lo), mid(hi)
    if l is None or h is None:
        return dict(error='MISSING_OR_CROSSED_OPTION_QUOTES', strikes_used=[lo, hi])
    width = hi['strike'] - lo['strike']
    q = (l - h) / width
    if probability(q) is None:
        return dict(error='INVALID_CALL_SPREAD', strikes_used=[lo, hi])
    return dict(probability=q, width=width, strikes_used=[lo, hi], interpolation='finite_call_spread',
                bid_ask_complete=all(number(c.get(side)) is not None for c in (lo, hi) for side in ('bid', 'ask')))


def derive(event, pm, surface, spot, history, now, config):
    flags = []
    base = dict(opt_yes=None, basis_method=None, model_version='gbm-zero-carry-1.0.0',
                calculation_version=CALCULATION_VERSION, model_confidence='UNAVAILABLE',
                mapping_confidence=event.get('mapping_review', 'UNVERIFIED'),
                model_inputs={}, quality_flags=flags, source_state='WAIT')
    def fail(state, flag):
        base.update(source_state=state)
        flags.append(flag)
        return base
    if event.get('mapping_review') == 'MISMATCH':
        return fail('MISMATCH', 'OPERATOR_REJECTED_MAPPING')
    if event.get('expiry', 0) <= now:
        return fail('ENDED', 'EVENT_CUTOFF_PASSED')
    if pm and pm.get('crossed'):
        return fail('BAD_QUOTE', 'CROSSED_PM_BOOK')
    if pm is None or probability(pm.get('yes')) is None:
        return fail('WAIT', 'MISSING_PM')
    pm_age = config.gamma_max_age_ms if pm.get('method') == 'gamma_indicative' else config.pm_max_age_ms
    if not 0 <= now - pm['source_ms'] <= pm_age:
        return fail('STALE', 'PM_AGE_LIMIT')
    if not surface:
        return fail('WAIT', 'MISSING_OPTIONS')
    option_max_age = max(config.yahoo_seconds * 2000, 90000) if surface.get('venue') == 'yahoo' else config.options_max_age_ms
    if not 0 <= now - surface['received_ms'] <= option_max_age or not 0 <= now - surface.get('source_ms', surface['received_ms']) <= option_max_age:
        return fail('STALE', 'OPTIONS_AGE_LIMIT')
    expiry = surface['expiry']
    offset = expiry - event['expiry']
    offset_limit=config.yahoo_max_expiry_offset_hours if surface.get('venue')=='yahoo' else config.max_expiry_offset_hours
    if not 0 <= offset <= offset_limit * 3600000:
        return fail('CUTOFF', 'OPTION_EXPIRY_DOES_NOT_COVER_EVENT')
    if surface.get('venue')=='yahoo' and offset>config.max_expiry_offset_hours*3600000:
        flags.append('EQUITY_LATER_EXPIRY_IV_PROXY_UP_TO_7_DAYS')
    if spot is None or number(spot.get('price')) is None or spot['price'] <= 0:
        return fail('WAIT', 'MISSING_SPOT')
    spot_age = max(config.yahoo_seconds * 2000, 90000) if spot.get('venue') == 'yahoo' else config.spot_max_age_ms
    market_closed=surface.get('venue')=='yahoo' and spot.get('context',{}).get('market_state')=='CLOSED'
    if spot.get('source_ms') is None:
        return fail('WAIT','MISSING_SPOT_TIMESTAMP')
    if not 0 <= now - spot['source_ms'] <= spot_age and not market_closed:
        return fail('STALE', 'SPOT_AGE_LIMIT')
    strike = event['strike_or_threshold']
    calls = surface.get('calls', [])
    base['model_inputs'] = dict(venue=surface.get('venue'), underlying=event['asset'], expiry=expiry,
        event_cutoff=event['expiry'], spot=spot['price'], spot_source=spot.get('venue'),
        forward=surface.get('forward'), rates=0.0, dividends=0.0, carry_assumption='zero',
        surface_timestamp=surface.get('source_ms'), expiry_offset_ms=offset)
    neighborhood = sorted([c for c in calls if number(c.get('iv')) is not None and 0.0001 < c['iv'] < 5], key=lambda c: abs(c['strike']-strike))[:3]
    neighborhood.sort(key=lambda c: c['strike'])
    base['surface_features'] = dict(local_iv=next((c['iv'] for c in sorted(neighborhood,key=lambda c:abs(c['strike']-strike))),None),
        local_iv_skew=(neighborhood[-1]['iv']-neighborhood[0]['iv'])/math.log(neighborhood[-1]['strike']/neighborhood[0]['strike']) if len(neighborhood)>1 and neighborhood[-1]['strike']>neighborhood[0]['strike']>0 else None,
        skew_units='IV per log strike; local finite difference', strikes=[c['strike'] for c in neighborhood],
        pm_spread=pm['ask']-pm['bid'] if pm.get('bid') is not None and pm.get('ask') is not None else None)
    if event.get('mapping_review') != 'VERIFIED':
        flags.append('UNVERIFIED_SETTLEMENT_SEMANTICS')
    if str(event.get('settlement_source') or '').lower() not in ('', str(spot.get('venue', '')).lower()):
        flags.append('REFERENCE_SOURCE_PROXY')
    if surface.get('venue') == 'yahoo':
        flags.extend(['YAHOO_DELAY_UNKNOWN', 'EQUITY_SESSION_CLOSE_PROXY','AMERICAN_OPTION_PROXY','ZERO_DIVIDEND_PROXY','OVERNIGHT_JUMP_MODEL_LIMIT'])
        if market_closed:flags.append('MARKET_CLOSED_LAST_SESSION_PRICES')
    if pm.get('method') == 'gamma_indicative':
        flags.append('INDICATIVE_PM_TIMESTAMP_IS_RETRIEVAL')
    width = (pm.get('ask') or 0) - (pm.get('bid') or 0)
    if width > config.max_pm_spread:
        flags.append('WIDE_PM_BOOK')
    digital = finite_spread(calls, strike) if offset == 0 and event['event_type'] == 'terminal' else None
    if digital and digital.get('error'):
        return fail('BAD_QUOTE', digital['error'])
    if digital:
        q = digital['probability'] if event['direction'] == 'up' else 1 - digital['probability']
        base['basis_method'] = 'SPREAD' if digital['bid_ask_complete'] else 'MID'
        base['model_inputs'].update(digital)
        base['model_confidence'] = 'MEDIUM'
        flags.append('FINITE_SPREAD_REPLICATION_ERROR')
        if digital['width'] / spot['price'] > 0.05:
            flags.append('SPARSE_STRIKES')
        lo, hi = digital['strikes_used']
        if abs((lo['strike'] + hi['strike']) / 2 - strike) > digital['width'] * 0.01:
            flags.append('ASYMMETRIC_STRIKE_BRACKET')
    else:
        nearby = sorted([c for c in calls if number(c.get('iv')) is not None and 0.0001 < c['iv'] < 5], key=lambda c: abs(c['strike'] - strike))[:3]
        if not nearby:
            return fail('WAIT', 'MISSING_IMPLIED_VOLATILITY')
        weights = [1 / max(1, abs(c['strike'] - strike)) for c in nearby]
        sigma = sum(c['iv'] * w for c, w in zip(nearby, weights)) / sum(weights)
        base['model_inputs'].update(iv=sigma, strikes_used=nearby, interpolation='inverse_distance_nearest_3_IV')
        years = (event['expiry'] - now) / YEAR_MS
        base['model_confidence'] = 'LOW'
        flags.append('FLAT_IV_MODEL')
        if event['event_type'] == 'touch':
            base['basis_method'] = 'TOUCH'
            start = event.get('window_start')
            if start is None or start > now:
                return fail('HISTORY', 'UNKNOWN_OR_FUTURE_PATH_WINDOW')
            if not history or history.get('error') or now - history['received_ms'] > config.history_max_age_ms:
                return fail('HISTORY', 'MISSING_COMPLETE_CURRENT_PATH')
            crossed = history['low'] <= strike if event['direction'] == 'down' else history['high'] >= strike
            if crossed:
                return fail('VERIFY_HIT', 'REFERENCE_HISTORY_CROSSED_BARRIER_CHECK_RULES')
            q = touch_probability(spot['price'], strike, sigma, years, event['direction'])
            flags.append('PATH_HISTORY_REFERENCE_PROXY')
        else:
            base['basis_method'] = 'IV'
            q = terminal_probability(spot['price'], strike, sigma, years, event['direction'])
        if offset:
            flags.append('EXPIRY_IV_PROXY')
    if probability(q) is None:
        return fail('MODEL_LIMIT', 'INVALID_TRANSFORM')
    if q < 0.01 or q > 0.99:
        flags.append('EXTREME_TAIL_MODEL_SENSITIVITY')
        base['model_confidence'] = 'LOW'
    if q in (0, 1):
        flags.append('MODEL_ENDPOINT')
    base.update(opt_yes=q, source_state='CLOSED' if market_closed else 'PROXY' if flags else 'OK')
    return base
