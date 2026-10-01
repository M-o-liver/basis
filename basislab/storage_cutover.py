"""Explicit validated cutover. No legacy rewrite, automatic cleanup or wallet reset."""
import fcntl
import json
from pathlib import Path
from .operations import Journal
from .replay import reducer_hash
from .tape import create_tape, publish_tape, atomic_json


def cutover(alias,root,validation):
    alias=Path(alias).resolve();validation=Path(validation).resolve()
    evidence=json.loads(validation.read_text())
    if not evidence.get('accepted') or evidence.get('state')!='PASS':raise ValueError('A completed passing live storage shadow is required')
    if evidence.get('elapsed_seconds',0)<1800 or evidence.get('v2_bytes_per_hour',float('inf'))>=150*1024**2 or evidence.get('reduction_multiple',0)<10:
        raise ValueError('The live duration/storage target has not been proven')
    components=evidence.get('replay',{}).get('components',{})
    required={'pm','spot','surfaces','latest_observations','episodes','analyzers','dynamics','native_salient'}
    if evidence.get('mismatches') or evidence.get('replay',{}).get('mismatches',1) or not required.issubset(components) or not all(components.values()):
        raise ValueError('Replay/state equivalence is incomplete')
    if Path(evidence.get('source_tape','')).resolve()!=alias:raise ValueError('Live validation belongs to another source tape')
    archived=alias.parent/'versions'/evidence['producer_version']
    if reducer_hash(archived)!=reducer_hash(Path(__file__).parent):raise ValueError('Reducer changed since shadow; repeat validation')
    if Path(str(alias)+'.storage.json').exists():raise ValueError('This tape already has a storage pointer; never replace it')
    lock=open(str(alias)+'.collector.lock','a')
    try:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise ValueError('Stop the existing supervised writer normally before storage cutover') from None
        wal=Path(str(alias)+'-wal')
        if wal.exists() and wal.stat().st_size:
            raise ValueError('Legacy WAL is not fully checkpointed; release readers and use normal SQLite checkpointing, never delete it')
        tape=create_tape(alias,root)
        try:
            stat=alias.stat();tape.manifest['legacy'].update(frozen=True,bytes_at_attach=stat.st_size,mtime_ns=stat.st_mtime_ns,
                validation=str(validation),scientific_change=False)
            atomic_json(tape.manifest_path,tape.manifest)
            publish_tape(tape)
            journal=Journal(alias)
            journal.record('storage_cutover',legacy=str(alias),manifest=str(tape.manifest_path),legacy_limits=tape.manifest['legacy']['limits'],
                legacy_bytes=stat.st_size,legacy_mtime_ns=stat.st_mtime_ns,validation=str(validation),
                paper_database=str(alias.with_suffix('.paper.sqlite3')),calculation_changed=False,
                reason='Lossless representation/cadence change; existing scientific campaign, paper runs and operational evidence preserved')
            journal.close()
            return dict(alias=str(alias),manifest=str(tape.manifest_path),legacy=tape.manifest['legacy'],
                next_raw_id=tape.manifest['legacy']['limits']['raw']+1,storage_version=2)
        finally:tape.close()
    finally:lock.close()
