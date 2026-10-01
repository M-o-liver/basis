"""Bounded v1 -> v2 same-stream validation; never starts a second source collector."""
import copy
import hashlib
from pathlib import Path
import resource
import time
from .operations import Journal, resources
from .replay import archived_engine
from .store import encode, decode
from .tape import LegacyReader, SegmentedTape, create_tape, atomic_json


def run_shadow(path,output,minutes=30):
    if not 30<=minutes<=120:raise ValueError('Live migration shadow must run for 30–120 actual minutes')
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    report_path=output/'validation.json'
    legacy=LegacyReader(path);cp=legacy.checkpoint()
    if not cp:raise ValueError('Shadow needs a committed causal checkpoint')
    compact=create_tape(path,output/'tape',legacy_boundary=cp['raw_id'])
    engine=archived_engine(compact,cp['code_hash'])
    if engine is None:raise ValueError('Exact historical reducer is unavailable; no current-code fallback')
    engine.load_checkpoint(copy.deepcopy(cp['state']));engine.persist=True;engine.restoring=False
    initial_algos={}
    for row in compact.legacy.latest('analyzers','scope,algo_name',1000):
        row.pop('record_id',None);initial_algos[(row['market_scope'],row['algo_name'])]=row
    engine.analyzer_latest=copy.deepcopy(initial_algos);compact.resume(engine)
    after=cp['raw_id'];started=time.monotonic();start_cpu=resource.getrusage(resource.RUSAGE_SELF)
    start_bytes=compact.v2_bytes();v1_start=legacy.logical_bytes()
    episode_cursor=compact.legacy.limits['episodes'];algo_cursor=compact.legacy.limits['analyzers']
    frame_cursor=compact.legacy.limits['observations'];v2_ep_cursor=episode_cursor;v2_algo_cursor=algo_cursor
    last_checkpoint=last_progress=started;wal_high=0
    counts=dict(raw_logical_events=0,source_hash_matches=0,frames_compared=0,episode_revisions_compared=0,analyzers_compared=0)
    mismatch=[];max_lag=0
    result=dict(state='RUNNING',source_tape=str(Path(path).resolve()),shadow='Committed v1 source events only; no duplicate source streams or legacy persistence',
        producer_version=cp['code_hash'],initial_checkpoint_raw_id=after,started_ms=time.time_ns()//1000000,
        requested_minutes=minutes,counts=counts,mismatches=mismatch)
    atomic_json(report_path,result);legacy.close()
    def fail(kind,**context):
        mismatch.append(dict(kind=kind,**context))
        if len(mismatch)>=10:raise RuntimeError('Shadow equivalence failed; refusing further validation')
    def rows_after(reader,table,cursor,end):
        return reader.db.execute(f'SELECT * FROM {table} WHERE id>? AND raw_id<=? ORDER BY id',(cursor,end)).fetchall()
    try:
        while time.monotonic()-started<minutes*60:
            reader=LegacyReader(path)
            end=reader.limits['raw']
            # Batches release each SQLite read before running the reducer.
            batch_end=min(end,after+1000)
            for record in reader.between(after,batch_end):
                engine.session=record['session']
                raw_id=engine.ingest(record['source'],record['kind'],record['subject'],record['payload'],
                    received_ms=record['received_ms'],source_ms=record['source_ms'],monotonic_ns=record['monotonic_ns'])
                if raw_id!=record['id']:fail('GLOBAL_SEQUENCE',expected=record['id'],actual=raw_id)
                stored=compact.raw_record(raw_id)
                counts['raw_logical_events']+=1
                if encode(stored)!=encode(record):fail('SOURCE_STATE',raw_id=raw_id)
                else:counts['source_hash_matches']+=1
                after=raw_id
            frames=compact.history(after_id=frame_cursor,limit=10000)
            expected_frames={}
            for frame in frames:
                raw_id=frame['raw_id']
                if raw_id not in expected_frames:
                    expected_frames[raw_id]={r['event_id']:decode(r['data']) for r in reader.db.execute('SELECT event_id,data FROM observations WHERE raw_id=?',(raw_id,))}
                frame_cursor=frame.pop('observation_id');expected=expected_frames[raw_id].get(frame['event_id'])
                counts['frames_compared']+=1
                if encode(frame)!=encode(expected):fail('FRAME',raw_id=raw_id,event_id=frame['event_id'],fields=[k for k in frame if expected is None or frame[k]!=expected.get(k)])
            # Legacy journal IDs diverge from compact IDs. Match by causal raw
            # reference/key; only scan the new row-ID tails, never whole tables.
            legacy_eps=rows_after(reader,'episodes',episode_cursor,after)
            expected_eps={(r['raw_id'],r['gap_event_id']):decode(r['data']) for r in legacy_eps}
            if legacy_eps:episode_cursor=legacy_eps[-1]['id']
            current=compact.journal_rows('episodes',v2_ep_cursor,after)
            for row in current:
                counts['episode_revisions_compared']+=1;v2_ep_cursor=row['id']
                if encode(decode(row['data']))!=encode(expected_eps.get((row['raw_id'],row['gap_event_id']))):fail('EPISODE',raw_id=row['raw_id'],gap_event_id=row['gap_event_id'])
            legacy_algos=rows_after(reader,'analyzers',algo_cursor,after)
            expected_algos={(r['raw_id'],r['scope'],r['algo_name']):decode(r['data']) for r in legacy_algos}
            if legacy_algos:algo_cursor=legacy_algos[-1]['id']
            current=compact.journal_rows('analyzers',v2_algo_cursor,after)
            for row in current:
                counts['analyzers_compared']+=1;v2_algo_cursor=row['id']
                if encode(decode(row['data']))!=encode(expected_algos.get((row['raw_id'],row['scope'],row['algo_name']))):fail('ANALYZER',raw_id=row['raw_id'],algo=row['algo_name'])
            v1_current=reader.logical_bytes();reader.close()
            now=time.monotonic();max_lag=max(max_lag,end-after)
            wal=Path(str(compact.active.path)+'-wal');wal_high=max(wal_high,wal.stat().st_size if wal.exists() else 0)
            if now-last_checkpoint>=300:engine.save_checkpoint();last_checkpoint=now
            if now-last_progress>=30:
                result.update(elapsed_seconds=now-started,raw_boundary=after,lag_records=end-after,
                    v2_logical_bytes=compact.v2_bytes(),v1_logical_bytes=v1_current,resources=resources(compact.active.path))
                atomic_json(report_path,result)
                print('BASIS storage shadow',round(now-started,1),'s',counts,'MiB/h',round((compact.v2_bytes()-start_bytes)*3600/(now-started)/1024**2,2),'mismatches',len(mismatch),flush=True)
                last_progress=now
            if after>=end:time.sleep(.5)
        engine.save_checkpoint()
        elapsed=time.monotonic()-started;steady_resources=resources(compact.active.path)
        usage=resource.getrusage(resource.RUSAGE_SELF)
        result.update(elapsed_seconds=elapsed,raw_boundary=after,maximum_lag_records=max_lag,
            v2_logical_bytes=compact.v2_bytes(),v2_growth_bytes=compact.v2_bytes()-start_bytes,
            v2_bytes_per_hour=(compact.v2_bytes()-start_bytes)*3600/elapsed,
            v1_growth_bytes=v1_current-v1_start,v1_bytes_per_hour=(v1_current-v1_start)*3600/elapsed,
            v2_wal_high_bytes=wal_high,steady_resources=steady_resources,
            shadow_cpu_percent_one_core=((usage.ru_utime+usage.ru_stime)-(start_cpu.ru_utime+start_cpu.ru_stime))*100/elapsed)
        # Full bounded shadow replay, including analyzer clocks, starts from the
        # original v1 checkpoint, not a later v2 checkpoint hiding encoding bugs.
        view=SegmentedTape(compact.manifest_path);replay=archived_engine(view,cp['code_hash'])
        replay.load_checkpoint(copy.deepcopy(cp['state']));replay.analyzer_latest=copy.deepcopy(initial_algos)
        replay_started=time.monotonic();raw_replayed=0
        for record in view.between(cp['raw_id'],after):replay.apply(record);raw_replayed+=1
        comparisons={
            'pm':(engine.books,replay.books),'spot':(engine.spots,replay.spots),
            'surfaces':([[list(k),v] for k,v in engine.surfaces.items()],[[list(k),v] for k,v in replay.surfaces.items()]),
            'latest_observations':(engine.latest,replay.latest),'episodes':(engine.episodes.active,replay.episodes.active),
            'analyzers':({k[1]+'/'+k[0]:v for k,v in engine.analyzer_latest.items()},
                        {k[1]+'/'+k[0]:v for k,v in replay.analyzer_latest.items()}),
            'dynamics':([[list(k),list(v)] for k,v in engine.dynamics.history.items()],[[list(k),list(v)] for k,v in replay.dynamics.history.items()]),
            'native_salient':([[list(k),list(v)] for k,v in engine.dynamics.salient.items()],[[list(k),list(v)] for k,v in replay.dynamics.salient.items()])}
        state_matches={k:encode(a)==encode(b) for k,(a,b) in comparisons.items()}
        for k,matched in state_matches.items():
            if not matched:fail('REPLAY_FINAL_STATE',component=k)
        result.update(replay=dict(raw_records=raw_replayed,seconds=time.monotonic()-replay_started,components=state_matches,mismatches=len(mismatch)))
        view.close()
        result['reduction_multiple']=result['v1_bytes_per_hour']/result['v2_bytes_per_hour'] if result['v2_bytes_per_hour'] else None
        result['accepted']=not mismatch and counts['frames_compared']>0 and counts['analyzers_compared']>0 and result['v2_bytes_per_hour']<150*1024**2 and elapsed>=30*60 and result['reduction_multiple']>=10
        result['state']='PASS' if result['accepted'] else 'FAIL'
        atomic_json(report_path,result)
        journal=Journal(path);journal.record('storage_shadow',**result);journal.close()
        return result
    except BaseException as error:
        result.update(state='ERROR',error=str(error),elapsed_seconds=time.monotonic()-started,raw_boundary=after)
        atomic_json(report_path,result);raise
    finally:compact.close()
