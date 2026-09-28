"""Causal rolling features, append-only episode revisions, and semantic curve groups."""
from collections import defaultdict, deque
import math
from . import FEATURE_VERSION
HORIZONS = (1, 5, 15, 30, 60, 300, 900)


def valid(row):
    return row.get('gap_pp') is not None and row.get('source_state') in ('OK', 'PROXY') and 'WIDE_PM_BOOK' not in row.get('quality_flags', [])


class Dynamics:
    def __init__(self, config):
        self.config = config
        self.history = defaultdict(lambda: deque(maxlen=30000))
        self.salient = defaultdict(lambda: deque(maxlen=30000))
        self.previous = {}

    def update(self, row):
        key = (row['event_id'], row.get('mapping_hash'))
        history = self.history[key]
        now = row['timestamp_wall']
        previous = self.previous.get(key)
        native = self.salient[key]
        while native and now - native[0]['timestamp'] > 2 * 3600000:
            native.popleft()
        if not valid(row) or row.get('pm_source') == 'gamma_indicative':
            # A source gap or indicative timestamp breaks timing continuity.
            native.clear()
            self.previous.pop(key, None)
        else:
            if previous:
                changes = [('pm', 100*(row['pm_yes']-previous['pm_yes']), self.config.event_jump_pp),
                           ('opt', 100*(row['opt_yes']-previous['opt_yes']), self.config.event_jump_pp)]
                if row.get('spot') and previous.get('spot'):
                    changes.append(('spot', math.log(row['spot']/previous['spot']), self.config.spot_jump_return))
                for leg, change, threshold in changes:
                    if abs(change) >= threshold:
                        native.append(dict(leg=leg, timestamp=now, change=change, raw_id=row['raw_id'],
                            source_timestamp=row.get(leg+'_timestamp'), trigger_kind=row.get('trigger_kind')))
            self.previous[key] = {k: row.get(k) for k in ('pm_yes', 'opt_yes', 'spot')}
        while history and now - history[0]['timestamp_wall'] > 2 * 3600000:
            history.popleft()
        result = dict(feature_version=FEATURE_VERSION, changes={}, velocity_pp_s=None,
                      acceleration_pp_s2=None, pm_travel=None, opt_travel=None, trend='INSUFFICIENT',
                      pm_change_pp=None, opt_change_pp=None, spot_return=None)
        if valid(row):
            for seconds in HORIZONS:
                target = now - seconds * 1000
                past = next((x for x in reversed(history) if x['timestamp_wall'] <= target and valid(x)), None)
                if past and target - past['timestamp_wall'] <= max(1000, seconds * 200):
                    actual = (now - past['timestamp_wall']) / 1000
                    result['changes'][str(seconds)] = dict(gap_pp=row['gap_pp'] - past['gap_pp'],
                        pm_pp=100 * (row['pm_yes'] - past['pm_yes']), opt_pp=100 * (row['opt_yes'] - past['opt_yes']),
                        actual_seconds=actual, reference_raw_id=past['raw_id'])
            # The default diagnostic uses 30s, then 15s/5s/1s as history builds.
            change = next((result['changes'][str(h)] for h in (30, 15, 5, 1) if str(h) in result['changes']), None)
            if change:
                delta, seconds = change['gap_pp'], change['actual_seconds']
                result['velocity_pp_s'] = delta / seconds
                result['diagnostic_horizon_seconds'] = seconds
                result['pm_change_pp'], result['opt_change_pp'] = change['pm_pp'], change['opt_pp']
                travel = abs(change['pm_pp']) + abs(change['opt_pp'])
                if travel:
                    result['pm_travel'] = abs(change['pm_pp']) / travel
                    result['opt_travel'] = abs(change['opt_pp']) / travel
                previous_gap = row['gap_pp'] - delta
                widening = abs(row['gap_pp']) - abs(previous_gap)
                previous = next((x for x in reversed(history) if x['features'].get('velocity_pp_s') is not None and now - x['timestamp_wall'] >= 1000), None)
                if previous:
                    result['acceleration_pp_s2'] = (result['velocity_pp_s'] - previous['features']['velocity_pp_s']) / ((now - previous['timestamp_wall']) / 1000)
                    if previous.get('spot') and row.get('spot'):
                        result['spot_return'] = row['spot'] / previous['spot'] - 1
                fast = abs(widening / seconds) >= self.config.fast_pp_per_second
                result['trend'] = 'REVERSING' if previous_gap * row['gap_pp'] < 0 else 'STABLE' if abs(widening) <= self.config.stable_pp else ('OPENING FAST' if fast else 'OPENING') if widening > 0 else ('CLOSING FAST' if fast else 'CLOSING')
        row['features'] = result
        # Full observations are persisted. Rolling memory keeps one causal anchor per
        # second and only the fields needed by features, not repeated option books.
        compact = {k: row.get(k) for k in ('event_id', 'mapping_hash', 'timestamp_wall', 'raw_id', 'source_state',
                   'quality_flags', 'pm_yes', 'opt_yes', 'gap_pp', 'spot', 'opt_timestamp')}
        compact['features'] = {'velocity_pp_s': result['velocity_pp_s']}
        if history and history[-1]['timestamp_wall'] // 1000 == now // 1000:
            history[-1] = compact
        else:
            history.append(compact)
        return result

    def prune(self, active_keys, now):
        for key in list(self.history):
            history = self.history[key]
            if key not in active_keys and (not history or now-history[-1]['timestamp_wall'] > 2*3600000):
                del self.history[key]
                self.salient.pop(key, None)
                self.previous.pop(key, None)


class Episodes:
    def __init__(self, config):
        self.config = config
        self.active = {}

    def update(self, row):
        key = row['event_id']
        now = row['timestamp_wall']
        episode = self.active.get(key)
        gap = row.get('gap_pp')
        close_reason = None
        if episode:
            if episode['mapping_hash'] != row.get('mapping_hash'):
                close_reason = 'MAPPING_CHANGED'
            elif row.get('expiry', now + 1) <= now:
                close_reason = 'EXPIRED'
            elif not valid(row):
                close_reason = 'DATA_GAP'
            elif episode['opening_gap_pp'] * gap < 0:
                close_reason = 'REVERSAL'
            elif abs(gap) <= self.config.close_gap_pp:
                close_reason = 'CONVERGENCE'
            if close_reason:
                episode.update(timestamp=now, state='EXPIRED' if close_reason == 'EXPIRED' else 'CLOSED', closing_time=now,
                    closing_gap=gap, close_reason=close_reason, duration_ms=now - episode['opened_at'])
                del self.active[key]
                yield dict(episode)
                episode = None
        if not valid(row):
            return
        if episode is None and abs(gap) >= self.config.open_gap_pp:
            episode = dict(gap_event_id=f"gap-{key}-{row['raw_id']}", event_id=key, market_pair=row['asset'],
                mapping_hash=row.get('mapping_hash'), opened_at=now, timestamp=now, opening_gap_pp=gap,
                opening_relative_gap=row['relative_gap'], max_abs_gap_pp=abs(gap), max_relative_gap=row['relative_gap'],
                time_to_max_ms=0, min_gap_after_open=gap, duration_ms=0, state='NEW', closing_time=None,
                closing_gap=None, close_reason=None, final_market_outcome=None, pm_travel=0.0, opt_travel=0.0,
                pm_distance_pp=0.0, opt_distance_pp=0.0, opening_spot=row['spot'], spot_move=None,
                last_pm=row['pm_yes'], last_opt=row['opt_yes'], last_abs_gap=abs(gap),
                feature_version=FEATURE_VERSION, thresholds=dict(open_pp=self.config.open_gap_pp, close_pp=self.config.close_gap_pp))
            self.active[key] = episode
        elif episode:
            episode['pm_distance_pp'] += 100 * abs(row['pm_yes'] - episode['last_pm'])
            episode['opt_distance_pp'] += 100 * abs(row['opt_yes'] - episode['last_opt'])
            total = episode['pm_distance_pp'] + episode['opt_distance_pp']
            episode['pm_travel'] = episode['pm_distance_pp'] / total if total else None
            episode['opt_travel'] = episode['opt_distance_pp'] / total if total else None
            new_max = abs(gap) > episode['max_abs_gap_pp']
            episode['state'] = 'PEAK' if new_max else 'WIDENING' if abs(gap) > episode['last_abs_gap'] else 'CONVERGING'
            if new_max:
                episode['max_abs_gap_pp'] = abs(gap)
                episode['time_to_max_ms'] = now - episode['opened_at']
            if row['relative_gap'] is not None:
                episode['max_relative_gap'] = max(episode['max_relative_gap'] or 0, row['relative_gap'])
            episode['min_gap_after_open'] = min(episode['min_gap_after_open'], gap)
            episode.update(last_pm=row['pm_yes'], last_opt=row['opt_yes'], last_abs_gap=abs(gap), timestamp=now,
                           duration_ms=now - episode['opened_at'], spot_move=row['spot'] / episode['opening_spot'] - 1 if row['spot'] and episode['opening_spot'] else None)
        if episode:
            yield dict(episode)


def curves(rows):
    groups = defaultdict(list)
    for row in rows:
        if valid(row):
            key = tuple(row.get(k) for k in ('asset', 'expiry', 'event_type', 'direction', 'window_start', 'settlement_source', 'threshold_inclusive'))
            groups[key].append(row)
    result = []
    for key, members in groups.items():
        members.sort(key=lambda x: x['strike_or_threshold'])
        if len(members) < 2:
            continue
        violations, humps, clusters = [], [], []
        sign = -1 if key[3] == 'up' else 1
        for a, b in zip(members, members[1:]):
            for field in ('pm_yes', 'opt_yes'):
                if sign * (b[field] - a[field]) < -1e-6:
                    violations.append(dict(source=field, lower=a['event_id'], upper=b['event_id']))
        for a, b, c in zip(members, members[1:], members[2:]):
            x, y = b['gap_pp'] - a['gap_pp'], c['gap_pp'] - b['gap_pp']
            if x * y < 0:
                humps.append(b['event_id'])
        cluster = []
        for row in members:
            if cluster and row['side'] != cluster[-1]['side']:
                if len(cluster) >= 2:
                    clusters.append([x['event_id'] for x in cluster])
                cluster = []
            cluster.append(row)
        if len(cluster) >= 2:
            clusters.append([x['event_id'] for x in cluster])
        result.append(dict(group=dict(zip(('asset', 'expiry', 'event_type', 'direction', 'window_start', 'settlement_source', 'threshold_inclusive'), key)),
            curve_type='touch_probability' if key[2] == 'touch' else 'terminal_probability',
            nodes=[{k: x.get(k) for k in ('event_id', 'strike_or_threshold', 'pm_yes', 'opt_yes', 'gap_pp', 'relative_gap', 'model_confidence')} for x in members],
            monotonicity_violations=violations, local_gap_extrema=humps, same_direction_clusters=clusters,
            interpretation='Descriptive neighboring-strike structure, not independent confirmation', feature_version=FEATURE_VERSION))
    return result
