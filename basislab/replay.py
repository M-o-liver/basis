"""Receipt-ordered replay, using archived reducer code for historical versions."""
import hashlib
import importlib.util
from pathlib import Path
import sys
from .engine import Engine
from .store import decode, encode

# Explicit one-way migration: crypto calculations are unchanged; stock state is
# reclassified and invalidated in a recorded session event before live use.
EQUITY_MIGRATION=(
    '179cc76ed95032ca4a94abf0932f47ae98de86172f3db528b30bc23d255a7581',
    'fb24f0823dd7a0480443d218c91e3afab9a2fe947d11a1ad8b312d077a987f24')

# Calls and all finite research values are unchanged. New snapshots add real
# puts; overflowed relative disagreement becomes unavailable. The session
# journals this exact-revision upgrade; historical observations stay immutable.
INTERACTION_MIGRATION=(EQUITY_MIGRATION[1],
    '1133d8d67863377be04f0c00edf36f1295577503d272d97599177ba3a2cd560e')
# The intermediate interaction build was already collecting. Its committed
# checkpoint is compatible; persistence failure state is deliberately transient.
CHECKPOINT_GUARD_MIGRATION=(
    '9233d3c792403e868d4936761cd2505a1b2398c017d5d6aca1c5f8252af5e695',INTERACTION_MIGRATION[1])
# Explicit product reset: retain source/dynamics state, retire analyzer work,
# allow labeled equity later-expiry proxies in probability-1.3.0. The session
# records the new definition; historical tape uses its exact archived reducer.
MARKETS_MIGRATION=(INTERACTION_MIGRATION[1],
    '9a7e3596badea2646c40759542be058f17135b094a7a4b08352913b70265628e')
MARKETS_PROXY_MIGRATION=('43a340d6ba63664e6e9bca9ee939b8725228c4f063b2d4c87848c7728385e47a',MARKETS_MIGRATION[1])
MARKETS_CHECKPOINT_MIGRATION=('0b2e9e080decba394fbf8c3e7f85fa2270b9db35ec48ecd0211c511df3cd0b2f',MARKETS_MIGRATION[1])


def reducer_hash(directory):
    digest=hashlib.sha256()
    for name in ('__init__.py','algos.py','config.py','engine.py','features.py','pricing.py','semantics.py'):
        path=Path(directory)/name
        digest.update(name.encode());digest.update(path.read_bytes() if path.exists() else b'<retired-module>')
    return digest.hexdigest()


def restore(engine):
    persistent = engine.persist
    engine.persist = False
    engine.restoring = True
    try:
        checkpoint = engine.store.checkpoint(engine.code_hash)
        if checkpoint is None:
            candidate=engine.store.checkpoint()
            if candidate:
                archived=Path(engine.store.path).parent/'versions'/candidate['code_hash']
                if reducer_hash(archived)==reducer_hash(Path(__file__).parent):
                    checkpoint=candidate
                elif (reducer_hash(archived),reducer_hash(Path(__file__).parent))==EQUITY_MIGRATION:
                    checkpoint=candidate;engine.restore_migration='equity-context-1'
                elif (reducer_hash(archived),reducer_hash(Path(__file__).parent)) in (INTERACTION_MIGRATION,CHECKPOINT_GUARD_MIGRATION):
                    checkpoint=candidate;engine.restore_migration='interaction-1'
                elif (reducer_hash(archived),reducer_hash(Path(__file__).parent)) in (MARKETS_MIGRATION,MARKETS_PROXY_MIGRATION,MARKETS_CHECKPOINT_MIGRATION):
                    checkpoint=candidate;engine.restore_migration='markets-math-1'
        if checkpoint:
            engine.load_checkpoint(checkpoint['state'])
            print(f'BASIS restored checkpoint at raw #{checkpoint["raw_id"]}; '+getattr(engine,'restore_migration','reducer source unchanged'),flush=True)
        for record in engine.store.raw(after=checkpoint['raw_id'] if checkpoint else 0):
            engine.apply(record)
            if record['id'] % 25000 == 0:
                print(f'BASIS restoring raw #{record["id"]}', flush=True)
    finally:
        engine.persist = persistent
        engine.restoring = False


def archived_engine(store, revision):
    current = Engine(store, persist=False)
    if current.code_hash == revision:
        return current
    directory = Path(store.path).parent / 'versions' / revision
    digest = hashlib.sha256()
    for path in sorted(directory.glob('*.py')):
        digest.update(path.name.encode()); digest.update(path.read_bytes())
    if digest.hexdigest()[:20] != revision:
        return None
    # Only execute the exact content-addressed local source archive made by BASIS.
    package = 'basis_archive_' + revision
    if package not in sys.modules:
        spec = importlib.util.spec_from_file_location(package, directory/'__init__.py', submodule_search_locations=[str(directory)])
        module = importlib.util.module_from_spec(spec); sys.modules[package] = module; spec.loader.exec_module(module)
    module = __import__(package+'.engine', fromlist=['Engine'])
    return module.Engine(store, persist=False)


def verify_replay(store, until=None):
    # Capture a stable prefix even while the live collector continues appending.
    with store.lock:
        if hasattr(store,'iter_observations'):
            boundary=store.stats_counts()['raw']
            if until is not None:
                boundary=max((r['id'] for r in store.raw(until=until)),default=0)
            observations=store.iter_observations(boundary)
        else:
            boundary = store.db.execute('SELECT COALESCE(MAX(id),0) FROM raw WHERE (? IS NULL OR received_ms<=?)', (until, until)).fetchone()[0]
            observations=((r['raw_id'],decode(r['data'])) for r in store.db.execute('SELECT raw_id,data FROM observations WHERE raw_id<=? ORDER BY id',(boundary,)))
        versions = {}
        for raw_id,data in observations:
            versions[data['code_hash']] = max(versions.get(data['code_hash'],0),raw_id)
    checked, mismatches, unsupported = 0, [], []
    for revision, last_raw in versions.items():
        engine = archived_engine(store, revision)
        if engine is None:
            unsupported.append(revision); continue
        for record in store.raw(until=until):
            if record['id'] > min(last_raw, boundary):
                break
            engine.apply(record)
            with store.lock:
                rows = store.frames_at(record['id']) if hasattr(store,'frames_at') else [decode(r['data']) for r in store.db.execute('SELECT data FROM observations WHERE raw_id=? ORDER BY id',(record['id'],))]
            for expected in rows:
                if expected['code_hash'] != revision:
                    continue
                actual = engine.latest.get(expected['event_id']); checked += 1
                if encode(actual) != encode(expected):
                    mismatches.append(dict(raw_id=record['id'], event_id=expected['event_id'], version=revision,
                        fields=[k for k in expected if not actual or actual.get(k) != expected[k]]))
                    if len(mismatches) >= 20:
                        return dict(checked=checked,mismatches=mismatches,unsupported_versions=unsupported,ok=False)
    return dict(checked=checked,mismatches=mismatches,unsupported_versions=unsupported,raw_boundary=boundary,
                versions_verified=len(versions)-len(unsupported),ok=checked>0 and not mismatches and not unsupported)
