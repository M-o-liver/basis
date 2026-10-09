"""Model-dependent option sizing from captured BASIS/GUI evidence; never orders.

An expiry distribution does not identify an intraday repricing distribution.
The reported central half Kelly is a sizing reference, not a validated edge.
"""
import math

import numpy as np

from .payoff_audit import from_snapshot, session_paths

VERSION = 'kelly-audit-1.0'


def log_optimum(returns, weights):
    returns, weights = np.asarray(returns, dtype=float), np.asarray(weights, dtype=float)
    if (returns.ndim != 1 or returns.shape != weights.shape or not len(returns)
            or not np.all(np.isfinite(returns)) or not np.all(np.isfinite(weights))
            or np.any(weights < 0) or weights.sum() <= 0 or returns.min() < -1):
        raise ValueError('INVALID_WEIGHTED_DEFINED_RISK_RETURNS')
    weights = weights / weights.sum()
    derivative = lambda f: float(np.dot(weights, returns / (1 + f * returns)))
    if derivative(0) <= 0:
        return 0.
    lo, hi = 0., 1. - 1e-12
    if derivative(hi) >= 0:
        return 1.
    for _ in range(70):
        mid = (lo + hi) / 2
        if derivative(mid) > 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def sizing(row, book, instrument, entry_price, equity, fee_per_contract=.65):
    if not all(math.isfinite(v) and v > 0 for v in (entry_price, equity)) or not math.isfinite(fee_per_contract) or fee_per_contract < 0:
        raise ValueError('INVALID_PRICE_EQUITY_OR_FEE')
    audit = from_snapshot(row, book, equity)
    candidates = [c for c in audit['candidates'] if c['kind'] in ('long_call', 'long_put')
                  and c['legs'][0]['instrument'] == instrument]
    if len(candidates) != 1:
        raise ValueError('EXACT_CAPTURED_SINGLE_LONG_OPTION_REQUIRED')
    candidate = candidates[0]
    if any(s['status'] != 'OK' for s in audit['scenarios']) or len(candidate['scenario_results']) != len(audit['scenarios']):
        raise ValueError('ALL_DECLARED_SCENARIOS_REQUIRED_FOR_SIZING')
    leg = candidate['legs'][0]
    p_bid, p_ask = audit['pm_interval']
    p_mid = (p_bid + p_ask) / 2
    results = []
    for scenario, detail in zip(audit['scenarios'], candidate['scenario_results']):
        if scenario['status'] != 'OK':
            continue
        share = scenario['session_variance_share'] if scenario['name'] in ('HALF_SESSION_VARIANCE', 'THREE_QUARTER_SESSION_VARIANCE') else None
        sample = session_paths(book['spot'], row['strike_or_threshold'], scenario['sigma'],
                               int(audit['calculated_at']), row['expiry'], row['option_expiry'],
                               row['direction'], session_share=share, rate=scenario['rate'], dividend=scenario['dividend'])
        intrinsic = np.maximum(sample['terminal'] - leg['strike'], 0) if leg['option_type'] == 'call' else np.maximum(leg['strike'] - sample['terminal'], 0)
        values = intrinsic * leg['multiplier'] * sample['discount']
        cost = entry_price * leg['multiplier'] + fee_per_contract + detail['estimated_exit_drag']
        returns = (values - cost) / cost
        for p in (p_bid, p_mid, p_ask):
            weights = p * sample['hit'] / sample['q'] + (1 - p) * (1 - sample['hit']) / (1 - sample['q'])
            full = log_optimum(returns, weights)
            dollars = equity * full / 2
            results.append(dict(scenario=scenario['name'], pm=p, full_kelly_fraction=full,
                                half_kelly_dollars=dollars, whole_contracts=int(dollars // cost),
                                modeled_cost_per_contract=cost, expected_pnl_per_contract=float(np.mean(weights * values) - cost),
                                calibration=detail['calibration']))
    central = next(r for r in results if r['scenario'] == 'QUOTE_MID' and r['pm'] == p_mid)
    return dict(version=VERSION, instrument=instrument, entry_price=entry_price, equity=equity,
                signal_raw_id=row['raw_id'], signal_timestamp=row['timestamp_wall'],
                book_received_ms=book['received_ms'], central=central, results=results,
                calibrated_scenarios=candidate['calibrated_scenarios'],
                half_kelly_range=[min(r['half_kelly_dollars'] for r in results), max(r['half_kelly_dollars'] for r in results)],
                robust_edge_pass=candidate['sensitivity_pass'], horizon='OPTION_EXPIRY',
                limits='PM mass tilt is an unvalidated physical-distribution hypothesis. Expiry Kelly does not identify 30-minute repricing Kelly. IID Monte Carlo tail sampling and fixed current-book exit friction affect sizing; no numerical or model confidence bound is applied. No-trade and rejected scenarios remain evidence.',
                method='Maximize weighted E[log(1+f*R)] on 0<=f<=1; halve f and floor whole contracts including modeled costs.',
                mathematical_reference='https://www.nokia.com/bell-labs/publications-and-media/publications/a-new-interpretation-of-information-rate/')
