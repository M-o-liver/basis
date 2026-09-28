"""Inspectable diagnostics. Scores are neither trading orders nor independent votes."""
import math
from collections import Counter
import numpy as np
from . import FEATURE_VERSION

FAMILIES = {
 'lead_lag_multiscale': 'TEMPORAL', 'event_sync': 'TEMPORAL',
 'sliced_wasserstein': 'DISTRIBUTION', 'ordinal_mmd': 'DISTRIBUTION',
 'covariance_manifold': 'GEOMETRY', 'topology': 'GEOMETRY',
 'sequential_martingale': 'EVIDENCE',
}
VERSION = '1.1.0'


def output(name, scope, now, window, params, status='ok', score=None, explanation='', diagnostics=None, features=None):
    return dict(algo_name=name, algo_version=VERSION, family=FAMILIES[name], timestamp=now,
        market_scope=scope, input_window=window, feature_version=FEATURE_VERSION, parameters=params,
        status=status, raw_score=score, normalized_score=None, uncertainty=None,
        explanation=explanation, contributing_features=features or [], diagnostics=diagnostics or {},
        independence_assumption=False)


def corr(x, y):
    if len(x) < 8 or np.std(x) < 1e-12 or np.std(y) < 1e-12:
        return None
    return float(np.corrcoef(x, y)[0, 1])


def standardized(a, b):
    scale = np.std(a, axis=0)
    active = scale > 1e-10
    # Constant baseline features becoming variable are meaningful, with unit floor.
    scale = np.where(active, scale, 1.0)
    center = np.mean(a, axis=0)
    return (a - center) / scale, (b - center) / scale


def sliced_wasserstein(a, b, names, seed=17, projections=64):
    a, b = standardized(np.asarray(a), np.asarray(b))
    rng = np.random.default_rng(seed)
    directions = np.vstack([np.eye(a.shape[1]), rng.normal(size=(projections, a.shape[1]))])
    directions /= np.linalg.norm(directions, axis=1)[:, None]
    scores = np.mean(np.abs(np.sort(a @ directions.T, axis=0) - np.sort(b @ directions.T, axis=0)), axis=0)
    top = np.argsort(scores)[-3:][::-1]
    return float(np.mean(scores)), [dict(distance=float(scores[i]), weights={names[j]: float(directions[i, j]) for j in np.argsort(abs(directions[i]))[-4:][::-1]}) for i in top]


def covariance_geometry(a, b, names, ridge=1e-3):
    a, b = standardized(np.asarray(a), np.asarray(b))
    ca = np.atleast_2d(np.cov(a, rowvar=False)) + np.eye(a.shape[1]) * ridge
    cb = np.atleast_2d(np.cov(b, rowvar=False)) + np.eye(a.shape[1]) * ridge
    values, vectors = np.linalg.eigh(ca)
    whitening = vectors @ np.diag(1 / np.sqrt(np.maximum(values, ridge))) @ vectors.T
    eigen = np.linalg.eigvalsh(whitening @ cb @ whitening)
    distance = float(np.linalg.norm(np.log(np.maximum(eigen, 1e-12))))
    def correlation(c):
        sd = np.sqrt(np.diag(c))
        return c / (sd[:, None] * sd[None, :])
    delta = correlation(cb) - correlation(ca)
    pairs = sorted([(abs(delta[i, j]), i, j) for i in range(len(names)) for j in range(i)], reverse=True)[:5]
    return distance, [dict(features=[names[i], names[j]], correlation_change=float(delta[i, j])) for _, i, j in pairs]


def motifs(values):
    # Stable ordering of ties would invent motion; retain ties as explicit sign motifs.
    result = []
    for window in np.lib.stride_tricks.sliding_window_view(np.asarray(values), 3):
        if len(set(window)) < 3:
            result.append('ties:' + ','.join(str(int(x)) for x in np.sign(np.diff(window))))
        else:
            result.append('ordinal:' + ''.join(str(int(i)) for i in np.argsort(window)))
    return result


def ordinal_mmd(a, b):
    ma, mb = motifs(a), motifs(b)
    ca, cb = Counter(ma), Counter(mb)
    keys = sorted(set(ca) | set(cb))
    pa, pb = {k: ca[k] / len(ma) for k in keys}, {k: cb[k] / len(mb) for k in keys}
    # Biased, nonnegative MMD^2 for the characteristic delta kernel on finite motifs.
    score = sum((pa[k] - pb[k]) ** 2 for k in keys)
    changed = sorted([dict(motif=k, baseline=pa[k], current=pb[k], change=pb[k]-pa[k]) for k in keys], key=lambda x: abs(x['change']), reverse=True)
    return score, changed[:6]


def lead_lag(pm, opt, spot, grid_seconds, scales, min_source_interval=0):
    p, q, s = np.diff(pm), np.diff(opt), np.diff(np.log(np.maximum(spot, 1e-10)))
    results = []
    for seconds in scales:
        lag = max(1, round(seconds / grid_seconds))
        if seconds < max(grid_seconds, min_source_interval) or len(p) - lag < 24:
            results.append(dict(scale=seconds, direction='INSUFFICIENT', reason='Below source cadence or fewer than 24 lag pairs', observation_count=max(0, len(p)-lag)))
            continue
        forward, reverse = corr(p[:-lag], q[lag:]), corr(q[:-lag], p[lag:])
        if forward is None or reverse is None:
            results.append(dict(scale=seconds, direction='INSUFFICIENT', reason='Constant or degenerate increments', observation_count=len(p)-lag))
            continue
        # Descriptive residualization on concurrent spot returns; not causal identification.
        def residual(x):
            design = np.column_stack([np.ones(len(s)), s])
            return x - design @ np.linalg.lstsq(design, x, rcond=None)[0]
        rp, rq = residual(p), residual(q)
        difference = forward - reverse
        block = max(2, int(math.sqrt(len(p) - lag)))
        rng = np.random.default_rng(71)
        boot = []
        n = len(p) - lag
        for _ in range(99):
            starts = rng.integers(0, max(1, n - block + 1), size=math.ceil(n / block))
            indexes = np.concatenate([np.arange(start, min(start + block, n)) for start in starts])[:n]
            f, r = corr(p[:-lag][indexes], q[lag:][indexes]), corr(q[:-lag][indexes], p[lag:][indexes])
            if f is not None and r is not None:
                boot.append(f-r)
        interval = [float(x) for x in np.quantile(boot, [0.025, 0.975])] if boot else None
        direction = 'AMBIGUOUS'
        if interval and interval[0] > 0 and forward > 0.2:
            direction = 'PM_LEADS_OPT'
        elif interval and interval[1] < 0 and reverse > 0.2:
            direction = 'OPT_LEADS_PM'
        results.append(dict(scale=seconds, effective_lag_seconds=lag*grid_seconds, direction=direction,
            strength=difference, pm_to_opt_correlation=forward, opt_to_pm_correlation=reverse,
            residual_pm_to_opt=corr(rp[:-lag], rq[lag:]), residual_opt_to_pm=corr(rq[:-lag], rp[lag:]),
            spot_to_pm=corr(s[:-lag], p[lag:]), spot_to_opt=corr(s[:-lag], q[lag:]),
            uncertainty=dict(method='paired moving-block bootstrap; exploratory, uncorrected across scales', interval_95=interval, block_size=block), observation_count=n))
    return results


def event_sync(pm, opt, spot, times, windows, threshold_pp=0.25):
    def jumps(values, threshold):
        return [int(t) for t, d in zip(times[1:], np.diff(values)) if abs(d) >= threshold]
    p, q = jumps(pm, threshold_pp / 100), jumps(opt, threshold_pp / 100)
    s = jumps(np.log(np.maximum(spot, 1e-10)), 0.0025)
    def conditional(first, second, window):
        # Exclude right-censored triggers whose entire follow-up window is not observed.
        eligible = [t for t in first if t + window * 1000 <= times[-1]]
        count = sum(any(t < u <= t + window * 1000 for u in second) for t in eligible)
        return dict(count=count, triggers=len(eligible), probability=count / len(eligible) if eligible else None)
    return [dict(window_seconds=w, pm_then_opt=conditional(p, q, w), opt_then_pm=conditional(q, p, w),
                 spot_then_pm=conditional(s, p, w), spot_then_opt=conditional(s, q, w)) for w in windows]


def asynchronous_sync(events, now, windows):
    """Native receipt-time coincidences, without interpolating the source streams."""
    streams = {leg: [e['timestamp'] for e in events if e['leg'] == leg and e['timestamp'] <= now] for leg in ('pm', 'opt', 'spot')}
    def conditional(first, second, seconds):
        eligible = [t for t in streams[first] if t+seconds*1000 <= now]
        hits = sum(any(t < u <= t+seconds*1000 for u in streams[second]) for t in eligible)
        return dict(count=hits, triggers=len(eligible), probability=hits/len(eligible) if eligible else None)
    return [dict(window_seconds=w, pm_then_opt=conditional('pm','opt',w), opt_then_pm=conditional('opt','pm',w),
                 spot_then_pm=conditional('spot','pm',w), spot_then_opt=conditional('spot','opt',w)) for w in windows]


def sync_output(scope, now, window, params, config, native_events, windows):
    eligible = [e for e in native_events if e['timestamp'] <= now]
    return output('event_sync', scope, now, window,
        dict(params, windows=windows, threshold_pp=config.event_jump_pp, spot_log_return_threshold=config.spot_jump_return),
        status='ok' if eligible else 'insufficient_data',
        diagnostics=dict(windows=asynchronous_sync(eligible, now, windows), native_events=eligible[-50:],
            event_count=len(eligible), clock='receipt/availability; source timestamps retained', interpolation='none'),
        explanation='Native asynchronous jump coincidences; equal-time events excluded, right-censored triggers removed. OPT is the derived probability, including spot/model recalculations. Descriptive, not causal')


def topology(a, b, names, scales=(0.4, 0.8, 1.2, 1.6)):
    if len(names) < 3:
        return None, dict(reason='At least three semantically comparable strikes required')
    def graph(matrix, threshold):
        cov = np.atleast_2d(np.cov(matrix, rowvar=False))
        sd = np.sqrt(np.maximum(np.diag(cov), 1e-12))
        correlation = np.clip(cov / (sd[:, None] * sd[None, :]), -1, 1)
        edges = {(i, j) for i in range(len(names)) for j in range(i) if math.sqrt(max(0, 2 * (1-correlation[i, j]))) <= threshold}
        groups = [{i} for i in range(len(names))]
        for i, j in edges:
            left = next(g for g in groups if i in g); right = next(g for g in groups if j in g)
            if left is not right:
                left.update(right); groups.remove(right)
        return edges, [sorted(names[i] for i in g) for g in groups]
    changes, scores = [], []
    for scale in scales:
        ea, ga = graph(a, scale); eb, gb = graph(b, scale)
        distance = len(ea ^ eb) / len(ea | eb) if ea | eb else 0
        scores.append(distance)
        changes.append(dict(scale=scale, edge_jaccard_distance=distance, baseline_components=ga, current_components=gb,
                            new_clusters=[g for g in gb if len(g) > 1 and g not in ga]))
    return float(np.mean(scores)), dict(filtration=changes, method='H0 connected-component filtration of correlation distance; no higher homology claim')


def run_algos(scope, rows, config, now, curve_matrix=None, curve_names=None, native_events=None):
    """Rows are an as-of grid formed only from historical observations <= now."""
    window = dict(start=rows[0]['timestamp_wall'] if rows else None, end=now,
                  observations=len(rows), raw_id_max=max((r['raw_id'] for r in rows), default=0))
    params = dict(window=config.analysis_window, grid_seconds=config.analysis_grid_seconds, parameter_hash=config.hash)
    needed = config.analysis_window * 2
    result = []
    if len(rows) < needed:
        for name in FAMILIES:
            if name == 'event_sync' and native_events is not None:
                result.append(sync_output(scope, now, window, params, config, native_events, config.analysis_scales))
                continue
            result.append(output(name, scope, now, window, params, 'disabled' if name == 'sequential_martingale' else 'insufficient_data',
                explanation='No conditionally calibrated component e-values; shared-data alarms are not multiplied' if name == 'sequential_martingale' else f'Need {needed} complete, fresh as-of samples; have {len(rows)}'))
        return result
    temporal_rows = rows
    rows = rows[-needed:]
    window = dict(window, baseline_start=rows[0]['timestamp_wall'], current_start=rows[config.analysis_window]['timestamp_wall'])
    names = ['pm_yes', 'opt_yes', 'gap_pp', 'spot']
    matrix = np.array([[r[name] for name in names] for r in rows], dtype=float)
    if not np.isfinite(matrix).all():
        raise ValueError('Analyzer input contains nonfinite data')
    n = config.analysis_window
    a, b = matrix[:n], matrix[n:]
    score, contributors = sliced_wasserstein(a, b, names)
    result.append(output('sliced_wasserstein', scope, now, window, dict(params, projections=64, seed=17, standardization='baseline_only'), score=score, features=contributors,
        explanation='Mean projected empirical W1 distance in baseline-standardized feature space; descriptive regime distance'))
    score, contributors = covariance_geometry(a, b, names)
    result.append(output('covariance_manifold', scope, now, window, dict(params, ridge=0.001), score=score, features=contributors,
        explanation='Affine-invariant SPD covariance distance after ridge regularization; PM/OPT/GAP are linearly related'))
    score, contributors = ordinal_mmd(a[:, 2], b[:, 2])
    result.append(output('ordinal_mmd', scope, now, window, dict(params, motif_length=3, kernel='delta'), score=score, features=contributors,
        explanation='Biased MMD squared between ordinal gap motifs. Overlapping motifs are dependent; no p-value claimed'))
    unique_opt = sorted({r['opt_timestamp'] for r in temporal_rows if r.get('opt_timestamp') is not None})
    cadence = float(np.median(np.diff(unique_opt))) / 1000 if len(unique_opt) >= 2 else float('inf')
    temporal = np.array([[r[name] for name in ('pm_yes','opt_yes','spot')] for r in temporal_rows], dtype=float)
    lags = lead_lag(temporal[:, 0], temporal[:, 1], temporal[:, 2], config.analysis_grid_seconds, config.analysis_scales, cadence)
    result.append(output('lead_lag_multiscale', scope, now, window, dict(params, scales=config.analysis_scales), diagnostics=dict(scales=lags, source_interval_seconds=cadence if math.isfinite(cadence) else None),
        explanation='Lagged return correlations with moving-block uncertainty and spot residuals. Association is not causal identification; receipt latency and multiple scales matter'))
    useful = [r['scale'] for r in lags if r['direction'] not in ('INSUFFICIENT', 'AMBIGUOUS')]
    windows = useful or [s for s in config.analysis_scales if s >= config.analysis_grid_seconds]
    if native_events is not None:
        result.append(sync_output(scope, now, window, params, config, native_events, windows))
    else:
        sync = event_sync(matrix[:, 0], matrix[:, 1], matrix[:, 3], [r['timestamp_wall'] for r in rows], windows, config.event_jump_pp)
        result.append(output('event_sync', scope, now, window, dict(params, windows=windows, threshold_pp=config.event_jump_pp), diagnostics=dict(windows=sync, clock='regular as-of grid'),
            explanation='Grid-based event coincidence frequencies; native event stream unavailable. Equal-time events excluded and right-censored triggers removed'))
    if curve_matrix is not None and len(curve_matrix) >= needed and len(curve_names or []) >= 3:
        score, diagnostics = topology(curve_matrix[-needed:-n], curve_matrix[-n:], curve_names)
        result.append(output('topology', scope, now, window, params, score=score, diagnostics=diagnostics, explanation='Comparable-strike correlation graph changes across distance thresholds'))
    else:
        result.append(output('topology', scope, now, window, params, 'insufficient_data', explanation='Need three comparable strikes with complete aligned baseline/current windows'))
    result.append(output('sequential_martingale', scope, now, window, params, 'disabled',
        explanation='No conditional null or calibrated e-values established. Component diagnostics share tape; no evidence multiplier or combined significance is reported',
        diagnostics=dict(component_algos=[x['algo_name'] for x in result], requirement='Predeclare a conditional null and validate component e-processes before enabling sequential evidence')))
    return result
