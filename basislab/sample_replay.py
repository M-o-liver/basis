"""Deterministic, bounded chronological verification using exact archived reducers."""
import bisect
import hashlib
import importlib
import json
from pathlib import Path
import sqlite3
import threading
import time
import zlib
from .replay import archived_engine, reducer_hash
from .store import Store, encode, decode


class ReadTape(Store):
    def __init__(self, path):
        self.path = str(path)
        self.lock = threading.RLock()
        self.checkpoint_errors = []
        self.db = sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True, timeout=5, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        tables=('raw','observations','analyzers','episodes')
        row=self.db.execute('SELECT '+','.join(f'(SELECT COALESCE(MAX(id),0) FROM {t})' for t in tables)).fetchone()
        self.counts=dict(zip(tables,row))
        self.boundary=self.counts['raw']

    def between(self, after, end):
        while after < min(end, self.boundary):
            rows = self.db.execute('SELECT * FROM raw WHERE id>? AND id<=? ORDER BY id LIMIT 256',
                                   (after, min(end, self.boundary))).fetchall()
            if not rows:
                return
            for row in rows:
                item = dict(row)
                body = zlib.decompress(item['payload'])
                if hashlib.sha256(body).hexdigest() != item['sha256']:
                    raise ValueError(f'Raw hash mismatch at {item["id"]}')
                item['payload'] = json.loads(body)
                after = item['id']
                yield item


def choose_regions(boundary, sessions, checkpoints, transitions=(), quarantines=(), deep=False):
    """Fixed seed and ordered anchors; no wall-clock randomness or future checkpoint."""
    anchors = {}
    def add(raw_id, reason):
        raw_id = max(1, min(boundary, int(raw_id)))
        anchors.setdefault(raw_id, []).append(reason)
    if not boundary:
        return []
    # Metadata arriving after the captured prefix must not alter its sample plan.
    sessions=[s for s in sessions if s['raw_id']<=boundary]
    checkpoints=[c for c in checkpoints if c['raw_id']<boundary]
    transitions=[t for t in transitions if t['raw_id']<=boundary]
    quarantines=[q for q in quarantines if q['raw_id']<=boundary]
    add(1, 'earliest/crypto-era')
    add(max(1, boundary-63), 'latest')
    # Middle/checkpoint windows deliberately start just after a causal checkpoint.
    for fraction in (.2, .4, .6, .8):
        preceding = [c for c in checkpoints if c['raw_id'] < boundary*fraction]
        add(preceding[-1]['raw_id']+1 if preceding else int(boundary*fraction), 'ordinary-middle')
    ordered = sorted(checkpoints, key=lambda c: hashlib.sha256(f'basis-chronology-v1:{c["raw_id"]}'.encode()).digest())
    for cp in ordered[:8 if deep else 3]:
        add(cp['raw_id']+1, 'checkpoint-boundary')
    for session in sessions:
        add(session['raw_id'], 'session/restart/version-boundary')
        if session['data'].get('checkpoint_migration')=='equity-context-1':
            add(session['raw_id'], 'equity-era')
    # Preserve the selection of first/last and deterministic middle transitions.
    candidates = sorted(transitions, key=lambda t: t['raw_id'])
    if candidates:
        selected = [candidates[0], candidates[-1]]
        selected += sorted(candidates[1:-1], key=lambda t: hashlib.sha256(f'health-v1:{t["raw_id"]}'.encode()).digest())[:8 if deep else 3]
        for item in selected:
            add(item['raw_id'], item['reason'])
    for item in quarantines[:12 if deep else 4]:
        add(item['raw_id'], 'quarantine-neighborhood')
    regions=[]
    version_cuts=[s['raw_id'] for a,s in zip(sessions,sessions[1:]) if a['data'].get('code_hash')!=s['data'].get('code_hash')]
    for raw,reasons in anchors.items():
        start=max(1,raw-16) if any('transition' in s or 'quarantine' in s for s in reasons) else raw
        end=min(boundary,raw+63)
        cuts=[start]+[cut for cut in version_cuts if start<cut<=end]+[end+1]
        for a,b in zip(cuts,cuts[1:]):
            regions.append(dict(start=a,end=b-1,anchor=max(a,min(b-1,raw)),reasons=reasons))
    return regions


def sampled_replay(tape, regions, checkpoints, deep=False):
    started = time.monotonic()
    seconds_budget = 180 if deep else 25
    raw_budget = 400_000 if deep else 70_000
    per_region_budget = 60_000 if deep else 25_000
    raw_count = checked = mismatch_count = 0
    details, mismatches, missing, assets = [], [], set(), {}
    fingerprints = {}
    def fingerprint(revision):
        if revision not in fingerprints:
            fingerprints[revision] = reducer_hash(Path(tape.path).parent/'versions'/revision)
        return fingerprints[revision]
    for region in regions:
        detail = dict(region, status='UNVERIFIED', observations=0, raw_records=0)
        details.append(detail)
        if time.monotonic()-started >= seconds_budget or raw_count >= raw_budget:
            detail['reason'] = 'REPLAY_BUDGET_REACHED'; continue
        first = tape.db.execute('SELECT raw_id,data FROM observations WHERE raw_id>=? AND raw_id<=? ORDER BY raw_id,id LIMIT 1',
                                (region['start'], region['end'])).fetchone()
        if not first:
            detail['reason'] = 'NO_OBSERVATIONS_IN_REGION'; continue
        revision = decode(first['data']).get('code_hash')
        detail['code_hash'] = revision
        engine = archived_engine(tape, revision)
        if engine is None:
            missing.add(revision); detail['reason'] = 'HISTORICAL_REDUCER_UNAVAILABLE'; continue
        engine.restoring = True  # Skip recomputing unrelated analyzer outputs, not observations.
        historical_replay = importlib.import_module(engine.__class__.__module__.rsplit('.',1)[0]+'.replay')
        migrations = tuple(getattr(historical_replay,name,None) for name in
                           ('EQUITY_MIGRATION','INTERACTION_MIGRATION','CHECKPOINT_GUARD_MIGRATION'))
        eligible = []
        for cp in reversed(checkpoints):
            if cp['raw_id'] >= region['start']:
                continue
            compatible = cp['code_hash']==revision or (fingerprint(revision) is not None and fingerprint(cp['code_hash'])==fingerprint(revision))
            migration = (fingerprint(cp['code_hash']), fingerprint(revision))
            if compatible or migration in migrations:
                eligible.append(cp)
        after = 0
        for cp in eligible[:10]:
            row = tape.db.execute('SELECT sha256,data FROM checkpoints WHERE id=?', (cp['id'],)).fetchone()
            try:
                body = zlib.decompress(row['data'])
                if hashlib.sha256(body).hexdigest()!=row['sha256']:
                    raise ValueError('checkpoint hash mismatch')
                state = json.loads(body)
                if state.get('last_raw_id')!=cp['raw_id']:
                    raise ValueError('checkpoint raw boundary mismatch')
                engine.load_checkpoint(state)
                after = cp['raw_id']; detail['checkpoint_id'] = cp['id']
                detail['checkpoint_code_hash'] = cp['code_hash']; break
            except (ValueError, zlib.error, KeyError, TypeError) as error:
                detail.setdefault('checkpoint_errors', []).append(str(error))
        if region['end']-after > per_region_budget or raw_count+region['end']-after > raw_budget:
            detail['reason'] = 'CAUSAL_WARMUP_EXCEEDS_BUDGET'; continue
        try:
            for record in tape.between(after, region['end']):
                if time.monotonic()-started >= seconds_budget:
                    detail['reason'] = 'REPLAY_BUDGET_REACHED'; break
                engine.apply(record)
                raw_count += 1; detail['raw_records'] += 1
                if record['id'] < region['start']:
                    continue
                rows = tape.db.execute('SELECT data FROM observations WHERE raw_id=? ORDER BY id', (record['id'],)).fetchall()
                for row in rows:
                    expected = decode(row['data'])
                    if expected.get('code_hash')!=revision:
                        detail['reason'] = 'VERSION_BOUNDARY_REQUIRES_ANOTHER_REGION'; continue
                    checked += 1; detail['observations'] += 1
                    asset=expected.get('asset','UNKNOWN');assets[asset]=assets.get(asset,0)+1
                    detail.setdefault('assets',{})[asset]=detail.get('assets',{}).get(asset,0)+1
                    actual = engine.latest.get(expected['event_id'])
                    if encode(actual)!=encode(expected):
                        mismatch_count += 1
                        if len(mismatches)<20:
                            mismatches.append(dict(raw_id=record['id'], event_id=expected['event_id'], code_hash=revision,
                                fields=[k for k in expected if not actual or actual.get(k)!=expected[k]]))
                        detail['status'] = 'MISMATCH'
            else:
                if detail['status']!='MISMATCH' and not detail.get('reason'):
                    detail['status'] = 'VERIFIED' if detail['observations'] else 'UNVERIFIED'
        except (ValueError, sqlite3.Error, zlib.error) as error:
            detail['reason'] = 'REPLAY_ERROR: '+str(error)
    return dict(selection_version='chronology-v1', mode='deep' if deep else 'bounded', raw_boundary=tape.boundary, regions_requested=len(regions),
                regions_verified=sum(d['status']=='VERIFIED' for d in details), raw_records_replayed=raw_count,
                observations_compared=checked, exact_matches=checked-mismatch_count, mismatch_count=mismatch_count,
                unavailable_version_count=len(missing), unavailable_versions=sorted(missing), mismatches=mismatches,
                observations_by_asset=assets,
                regions=details, seconds=time.monotonic()-started, seconds_budget=seconds_budget, raw_budget=raw_budget,
                note='Checkpoints precede each region. Exact archived reducer/config; skipped or unavailable regions are not matches.')
