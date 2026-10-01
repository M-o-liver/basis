"""Evidence-based operational acceptance. Never opens the research tape for writing."""
import bisect
import hashlib
from collections import defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sqlite3
import time
import urllib.request
import zlib
from .operations import Journal
from .sample_replay import ReadTape, choose_regions, sampled_replay
from .store import decode, encode

CRITERIA = dict(version='operational-v1', timer_seconds=5, timer_gap_seconds=30,
    maximum_unexplained_gap_seconds=60, maximum_controlled_restart_seconds=60,
    integrity_max_age_hours=24,
    duration='Actual timer-covered intervals in the instrumented campaign; restarts and missing intervals excluded.',
    fail='Replay mismatch, integrity failure, or recording interruption longer than 60s without a proven timely recovery.',
    partial='Elapsed duration but incomplete replay/integrity/recovery evidence. Source outages alone do not fail recorder acceptance.',
    scope='Instrumented campaign starts once, at first instrumented service start. Full older chronology and historical gates are retained separately.',
    coverage='Health states describe recorded status coverage, not proof that every source packet was captured.')


def project_markers(tape, journal):
    """Incremental indexed projection; all rows keep their immutable raw hash/ID."""
    first = tape.db.execute('SELECT sha256 FROM raw ORDER BY id LIMIT 1').fetchone()
    identity = dict(tape=str(Path(tape.path).resolve()), first_raw_hash=first[0] if first else None)
    prior = journal.latest('tape_identity')
    if prior and any(prior.get(k)!=v for k,v in identity.items()):
        raise ValueError('Acceptance journal belongs to a different tape; refusing to replace historical evidence')
    if not prior:
        journal.record('tape_identity', **identity)
    deadline = time.monotonic()+30
    complete = True
    for kind in ('session', 'timer', 'health'):
        mark = journal.db.execute('SELECT raw_id FROM cursor WHERE kind=?', (kind,)).fetchone()
        after = mark[0] if mark else 0
        # timer has a fixed subject and benefits from the existing covering index.
        subject = " AND subject='clock'" if kind=='timer' else ''
        cursor = tape.db.execute(f'SELECT id,received_ms,kind,subject,session,sha256,payload FROM raw WHERE kind=?{subject} AND id>? AND id<=? ORDER BY id',
                                 (kind, after, tape.boundary))
        try:
            while True:
                rows = cursor.fetchmany(2000)
                if not rows:
                    break
                values = []
                for r in rows:
                    body = zlib.decompress(r['payload'])
                    if hashlib.sha256(body).hexdigest()!=r['sha256']:
                        raise ValueError(f'Operational marker raw hash mismatch at {r["id"]}')
                    payload = decode(r['payload'])
                    if kind=='session':
                        payload = {k:payload[k] for k in ('config','code_hash','parameter_hash','checkpoint_migration') if k in payload}
                    elif kind=='timer':
                        payload = {}
                    values.append((r['id'],r['received_ms'],kind,r['subject'],r['session'],r['sha256'],encode(payload)))
                with journal.lock:
                    journal.db.execute('BEGIN IMMEDIATE')
                    try:
                        journal.db.executemany('INSERT OR IGNORE INTO markers VALUES(?,?,?,?,?,?,?)', values)
                        journal.db.execute('INSERT INTO cursor VALUES(?,?) ON CONFLICT(kind) DO UPDATE SET raw_id=excluded.raw_id', (kind, rows[-1]['id']))
                        journal.db.execute('COMMIT')
                    except BaseException:
                        journal.db.execute('ROLLBACK'); raise
                if time.monotonic()>deadline:
                    complete = False; break
        finally:
            cursor.close()
        if not complete:
            break
    return complete


def markers(journal, kind, boundary):
    return [dict(r, data=decode(r['data'])) for r in journal.db.execute(
        'SELECT * FROM markers WHERE kind=? AND raw_id<=? ORDER BY raw_id', (kind,boundary))]


def chronology(timers, sessions, stops=(), boundary_ms=None):
    segments, gaps = [], []
    if not timers:
        return dict(recorded_seconds=0,segments=[],gaps=[],sessions=len(sessions),restarts=max(0,len(sessions)-1))
    start = previous = timers[0]
    for item in timers[1:]:
        elapsed = item['timestamp_ms']-previous['timestamp_ms']
        changed = item['session']!=previous['session']
        if elapsed>30_000 or elapsed<0 or changed:
            segments.append(dict(start_ms=start['timestamp_ms'],end_ms=previous['timestamp_ms'],session=start['session']))
            # All restarts are listed, including short graceful ones. A missing
            # cadence does not by itself establish an all-feed crash or its cause.
            reason, cause = 'UNKNOWN_STOP', 'UNKNOWN'
            evidence = next((s for s in reversed(stops) if s.get('session')==previous['session']
                and previous['timestamp_ms']-5000<=s['timestamp_ms']<=item['timestamp_ms']), None)
            if evidence:
                if evidence['timestamp_ms']<=previous['timestamp_ms']+30_000:
                    reason = evidence.get('reason',reason); cause = reason
            gaps.append(dict(start_ms=previous['timestamp_ms'],end_ms=item['timestamp_ms'],
                seconds=max(0,elapsed/1000),before_raw_id=previous['raw_id'],after_raw_id=item['raw_id'],
                session_before=previous['session'],session_after=item['session'],classification=reason,
                cause=cause,session_changed=changed,clock_reversal=elapsed<0))
            start = item
        previous = item
    segments.append(dict(start_ms=start['timestamp_ms'],end_ms=previous['timestamp_ms'],session=start['session']))
    return dict(recorded_seconds=sum(max(0,(s['end_ms']-s['start_ms'])/1000) for s in segments),
                segments=segments,gaps=gaps,sessions=len(sessions),restarts=max(0,len(sessions)-1),
                last_timer_ms=timers[-1]['timestamp_ms'],expected_timer_seconds=5,missing_timer_threshold_seconds=30)


def source_accounting(health, segments, sessions):
    by_source = defaultdict(list)
    for row in health:
        by_source[row['subject']].append(row)
    starts = [s['raw_id'] for s in sessions]
    summary, transitions = {}, []
    for source, rows in sorted(by_source.items()):
        totals = defaultdict(float); count = 0; longest = run = 0; previous_state = None; covered_until=None
        for i,row in enumerate(rows):
            config = sessions[max(0,bisect.bisect_right(starts,row['raw_id'])-1)]['data'].get('config',{}) if sessions else {}
            ttl = (config.get('catalog_seconds',180)*2000 if source=='catalog' else
                   config.get('history_seconds',300)*2000 if source=='history' else
                   max(config.get('yahoo_seconds',60)*2000,90000) if source.startswith('yahoo') else 90000)
            state = row['data'].get('state','UNKNOWN')
            end = rows[i+1]['timestamp_ms'] if i+1<len(rows) else (segments[-1]['end_ms'] if segments else row['timestamp_ms'])
            if state!=previous_state:
                count += 1
                if previous_state is not None and (state in ('ERROR','PARTIAL','STALE') or previous_state in ('ERROR','PARTIAL','STALE')):
                    transitions.append(dict(raw_id=row['raw_id'],reason='source-'+previous_state+'-'+state+'-transition'))
            previous_state = state
            covered = False
            for segment in segments:
                if segment['session']!=row['session'] or segment['end_ms']<=row['timestamp_ms'] or segment['start_ms']>=end:
                    continue
                # Never carry a health claim through a missing recording interval.
                if covered:
                    break
                a=max(row['timestamp_ms'],segment['start_ms']); b=min(end,segment['end_ms'])
                if covered_until is not None and a>covered_until+5000:
                    run=0
                covered_until=b
                covered=True
                slices=[(state,a,b)]
                if state=='OK' and b>row['timestamp_ms']+ttl:
                    cut=max(a,row['timestamp_ms']+ttl)
                    slices=[('OK',a,cut),('STALE',cut,b)]
                    transitions.append(dict(raw_id=row['raw_id'],reason='stale/recovery-transition'))
                for label,a,b in slices:
                    duration=max(0,(b-a)/1000); totals[label]+=duration
                    if label in ('ERROR','STALE','EMPTY'):
                        run+=duration;longest=max(longest,run)
                    else:
                        run=0
            if not covered or i+1<len(rows) and rows[i+1]['session']!=row['session']:
                run=0
        summary[source]=dict(seconds={k:round(totals[k],3) for k in ('OK','PARTIAL','ERROR','STALE')},
            other_seconds={k:round(v,3) for k,v in totals.items() if k not in ('OK','PARTIAL','ERROR','STALE')},
            longest_unavailable_seconds=round(longest,3),transition_count=count,
            classification='BASIS_COLLECTOR_FAILURE' if source=='recorder' else 'EXTERNAL_SOURCE_OUTAGE',
            first_health_ms=rows[0]['timestamp_ms'],last_health_ms=rows[-1]['timestamp_ms'])
    return summary, transitions


def gate_status(hours, recorded_seconds, gaps, replay, integrity, recovery_complete=False):
    # Duration precedence prevents pretending that an unobserved future gate failed/passed.
    if recorded_seconds<hours*3600:
        return 'NOT YET ELAPSED'
    if replay.get('mismatch_count',0) or integrity.get('result')=='FAIL':
        return 'FAIL'
    if any(g['seconds']>60 or g.get('clock_reversal') for g in gaps):
        return 'FAIL'
    if (integrity.get('result')!='ok' or not integrity.get('fresh') or
        replay.get('regions_verified',0)<replay.get('regions_requested',1) or
        replay.get('unavailable_version_count',0) or not recovery_complete):
        return 'PARTIAL'
    return 'PASS'


def report(path, url='http://127.0.0.1:8765', deep=False, quick_check=False):
    started=time.monotonic(); now=time.time_ns()//1_000_000
    tape=ReadTape(path); journal=Journal(path)
    try:
        complete=project_markers(tape,journal)
        session_rows=markers(journal,'session',tape.boundary)
        timer_rows=markers(journal,'timer',tape.boundary)
        health_rows=markers(journal,'health',tape.boundary)
        stops=journal.entries('service_stop')+journal.entries('observed_stop')
        starts_by_pid={s['pid']:s for s in journal.entries('session_start')}
        for child in journal.entries('child_exit'):
            session=starts_by_pid.get(child['pid'])
            if session and child.get('exit_code')!=0:
                stops.append(dict(session=session['session'],timestamp_ms=child['timestamp_ms'],
                                  reason='MACHINE_PROCESS_RECORDING_GAP',exit_code=child['exit_code']))
        continuity=chronology(timer_rows,session_rows,stops)
        # Inspect only the small raw neighborhoods around missing timer intervals.
        for gap in continuity['gaps']:
            span=gap['after_raw_id']-gap['before_raw_id']
            if span<=25_000:
                rows=tape.db.execute('SELECT received_ms FROM raw WHERE id BETWEEN ? AND ? ORDER BY id',
                                     (gap['before_raw_id'],gap['after_raw_id'])).fetchall()
                gap['longest_all_recording_gap_seconds']=max((max(0,(b[0]-a[0])/1000) for a,b in zip(rows,rows[1:])),default=0)
            else:
                gap['all_feed_gap_reason']='Raw neighborhood exceeds bounded inspection budget'
        source_health,transitions=source_accounting(health_rows,continuity['segments'],session_rows)
        checkpoints=[dict(r) for r in tape.db.execute('SELECT id,raw_id,code_hash FROM checkpoints WHERE raw_id<=? ORDER BY id',(tape.boundary,))]
        quarantine=list({q['raw_id']:q for q in journal.entries('quarantine')}.values())
        checkpoint=tape.checkpoint(before_raw_id=tape.boundary)
        counters=checkpoint['state'] if checkpoint else {}
        known={q['raw_id'] for q in quarantine}
        for error in counters.get('errors',[]):
            if error['raw_id'] not in known:
                journal.record('quarantine',**error);quarantine.append(error);known.add(error['raw_id'])
        regions=choose_regions(tape.boundary,session_rows,checkpoints,transitions,quarantine,deep)
        replay=sampled_replay(tape,regions,checkpoints,deep)
        journal.record('sample_replay',**replay)
        if deep:
            journal.record('sample_replay_deep',**replay)
        integrity=journal.latest('integrity')
        if quick_check or (deep and (not integrity or integrity.get('result')!='ok' or now-integrity['timestamp_ms']>86400_000)):
            t=time.monotonic()
            rows=[r[0] for r in tape.db.execute('PRAGMA quick_check')]
            integrity=dict(result='ok' if rows==['ok'] else 'FAIL',messages=rows,raw_boundary=tape.boundary,seconds=time.monotonic()-t)
            journal.record('integrity',**integrity);integrity=journal.latest('integrity')
        integrity=integrity or dict(result='NOT CHECKED',reason='Run acceptance --deep for full SQLite quick_check; ordinary reports reuse dated evidence.')
        integrity['age_seconds']=max(0,(time.time_ns()//1_000_000-integrity['timestamp_ms'])/1000) if integrity.get('timestamp_ms') else None
        integrity['fresh']=integrity['age_seconds'] is not None and integrity['age_seconds']<=86400
        latest_cp=checkpoints[-1] if checkpoints else None
        latest_cp_ms=tape.db.execute('SELECT received_ms FROM raw WHERE id=?',(latest_cp['raw_id'],)).fetchone()[0] if latest_cp else None
        # These counters survive restarts in checkpoints. The old error deque is
        # explicitly a lower bound, not a fabricated historical packet census.
        # MAX primary keys are exact counts under BASIS's dense, append-only invariant.
        totals=tape.counts
        last=tape.db.execute('SELECT received_ms,session FROM raw WHERE id=?',(tape.boundary,)).fetchone()
        first=tape.db.execute('SELECT received_ms FROM raw ORDER BY id LIMIT 1').fetchone()
        samples=journal.entries('resource')
        current=samples[-1] if samples else {}
        growth=None; cpu=None;growth_window=None;cpu_window=None
        logical=[s for s in samples if 'logical_database_bytes' in s]
        growth_samples=logical if len(logical)>1 else samples
        growth_field='logical_database_bytes' if len(logical)>1 else 'database_bytes'
        if len(growth_samples)>1:
            end_sample=growth_samples[-1]
            earlier=next((s for s in reversed(growth_samples[:-1]) if end_sample['timestamp_ms']-s['timestamp_ms']>=3600_000),growth_samples[0])
            seconds=(end_sample['timestamp_ms']-earlier['timestamp_ms'])/1000
            if seconds>0:
                growth=(end_sample[growth_field]-earlier[growth_field])*3600/seconds
                growth_window=seconds
        same_pid=[s for s in samples if s.get('pid')==current.get('pid')]
        if len(same_pid)>1:
            earlier=next((s for s in reversed(same_pid[:-1]) if current['timestamp_ms']-s['timestamp_ms']>=3600_000),same_pid[0])
            seconds=(current['timestamp_ms']-earlier['timestamp_ms'])/1000
            if seconds>0:
                cpu=(current['cpu_seconds']-earlier['cpu_seconds'])/seconds*100;cpu_window=seconds
        live=None;live_error=None
        try:
            with urllib.request.urlopen(url+'/api/state',timeout=3) as response:
                live=json.load(response)
            if Path(live.get('stats',{}).get('path','')).resolve()!=Path(path).resolve():
                live=None;live_error='API belongs to a different database'
        except (OSError,ValueError) as error:
            live_error=str(error)
        current_time=time.time_ns()//1_000_000
        row=tape.db.execute("SELECT received_ms FROM raw WHERE kind='timer' AND subject='clock' ORDER BY id DESC LIMIT 1").fetchone()
        last_timer=row[0] if row else None
        recent=last_timer is not None and 0<=current_time-last_timer<=30_000
        collector_state='RECORDING' if recent else 'STALE_OR_STOPPED'
        if live and (live['collector'].get('failure') or not live['collector'].get('running')):
            collector_state='FAILED_OR_NOT_COLLECTING'
        campaign=journal.latest('campaign_start')
        campaign_start=campaign['timestamp_ms'] if campaign else None
        campaign_seconds=sum(max(0,(s['end_ms']-max(s['start_ms'],campaign_start))/1000) for s in continuity['segments'] if campaign_start and s['end_ms']>=campaign_start)
        campaign_gaps=[g for g in continuity['gaps'] if campaign_start and g['end_ms']>=campaign_start]
        exercises=journal.entries('exercise')
        recovery_complete=all(any(e.get('path')==p and e.get('result')=='PASS' for e in exercises)
            for p in ('graceful_restart','supervisor_recovery','checkpoint_fallback','write_failure','duplicate_lock','disk_safety'))
        gates={str(h)+'h':gate_status(h,campaign_seconds,campaign_gaps,replay,integrity,recovery_complete) for h in (24,72,168)}
        historical_gates={str(h)+'h':gate_status(h,continuity['recorded_seconds'],continuity['gaps'],replay,integrity,recovery_complete) for h in (24,72,168)}
        result=dict(acceptance_version='operational-v1',timestamp_ms=current_time,snapshot_started_ms=now,code_hash=live.get('versions',{}).get('code_hash') if live else None,
            criteria=CRITERIA,collector=dict(state=collector_state,current_uptime_seconds=live.get('diagnostics',{}).get('uptime_seconds') if live else None,
                last_committed_raw_ms=last[0] if last else None,last_timer_ms=last_timer,api_error=live_error,live=live.get('collector') if live else None),
            tape=dict(path=str(Path(path).resolve()),raw_boundary=tape.boundary,first_received_ms=first[0] if first else None,
                database_bytes=Path(path).stat().st_size,wal_bytes=Path(str(path)+'-wal').stat().st_size if Path(str(path)+'-wal').exists() else 0,
                free_bytes=shutil.disk_usage(Path(path).parent).free,growth_bytes_per_hour=growth,counts=totals),
            chronology=continuity,source_health=source_health,marker_projection_complete=complete,
            packets=dict(delayed=current.get('delayed_packets',counters.get('delayed_packets')),clock_errors=current.get('clock_errors',counters.get('clock_errors')),
                counter_raw_boundary=current.get('raw_id',checkpoint['raw_id'] if checkpoint else None),
                quarantined_known=len(quarantine),quarantine_coverage='Lower bound: persisted monitor observations plus the checkpoint quarantine deque; older total is not reconstructible from the bounded error deque.'),
            checkpoints=dict(count=len(checkpoints),latest=latest_cp,latest_age_seconds=(now-latest_cp_ms)/1000 if latest_cp_ms else None,
                latest_valid_raw_id=checkpoint['raw_id'] if checkpoint else None,unavailable=tape.checkpoint_errors),
            sqlite_quick_check=integrity,replay=replay,latest_deep_replay=journal.latest('sample_replay_deep'),resources=dict(samples=len(samples),cpu_percent_one_core=cpu,
                cpu_window_seconds=cpu_window,growth_window_seconds=growth_window,growth_measurement=growth_field,
                current=current,wal_max_observed_bytes=max((s['wal_bytes'] for s in samples),default=None),
                rss_max_observed_bytes=max((s.get('rss_high_water_bytes',0) for s in samples),default=None),
                checkpoint_measurements=journal.entries('checkpoint')[-10:],restore_measurements=journal.entries('restore')[-10:]),
            recovery=dict(supervisor_starts=journal.entries('supervisor_start'),child_exits=journal.entries('child_exit'),
                exercises=exercises,complete=recovery_complete,legacy_log=str(Path(path).parent/'collector.log'),
                note='Structured recovery evidence begins with this instrumentation; older log evidence is retained in the dated acceptance document.'),
            campaign=dict(start_ms=campaign_start,recorded_seconds=campaign_seconds,gates=gates),historical_gates=historical_gates)
        if not complete or collector_state!='RECORDING':
            for gate,status in gates.items():
                if status=='PASS':gates[gate]='PARTIAL'
        result['report_seconds']=time.monotonic()-started
        journal.record('acceptance',**result)
        return result
    finally:
        tape.close();journal.close()


def text_report(data):
    def utc(ms):
        return datetime.fromtimestamp(ms/1000,timezone.utc).isoformat(timespec='seconds') if ms else 'unknown'
    tape=data['tape'];replay=data['replay'];continuity=data['chronology']
    growth='unmeasured' if tape['growth_bytes_per_hour'] is None else f"{tape['growth_bytes_per_hour']/1024**3:.2f} GiB/h"
    uptime=data['collector']['current_uptime_seconds']
    lines=[f"BASIS ACCEPTANCE  {utc(data['timestamp_ms'])}",
        f"Collector {data['collector']['state']} | uptime {uptime/3600:.2f}h" if uptime is not None else f"Collector {data['collector']['state']} | uptime unknown",
        f"DB {tape['database_bytes']/1024**3:.2f} GiB | WAL {tape['wal_bytes']/1024**2:.1f} MiB | free {tape['free_bytes']/1024**3:.1f} GiB | growth {growth}",
        'Tape '+encode(tape['counts']),
        f"Sessions {continuity['sessions']} | restarts {continuity['restarts']} | interruptions {len(continuity['gaps'])} | timer-covered hours {continuity['recorded_seconds']/3600:.2f}"]
    for gap in continuity['gaps']:
        lines.append(f"  {utc(gap['start_ms'])} -> {utc(gap['end_ms'])} {gap['seconds']:.1f}s {gap['classification']} cause={gap['cause']} all-recording gap={gap.get('longest_all_recording_gap_seconds','unproven')}s")
    for source,health in data['source_health'].items():
        lines.append(f"{source}: "+' '.join(f'{k}={v/3600:.2f}h' for k,v in health['seconds'].items())+f" outage={health['longest_unavailable_seconds']:.1f}s transitions={health['transition_count']}")
    packets=data['packets'];cp=data['checkpoints'];integrity=data['sqlite_quick_check'];resources=data['resources']
    lines += ['Health-state coverage only; missing recording intervals are excluded.',
        f"Packets delayed={packets['delayed']} clock_errors={packets['clock_errors']} quarantined>={packets['quarantined_known']} (older quarantine total unknown)",
        f"Checkpoints {cp['count']} | latest age {cp['latest_age_seconds']}s | valid through raw {cp['latest_valid_raw_id']} | unavailable {len(cp['unavailable'])}",
        f"SQLite quick_check {integrity['result']} | {utc(integrity.get('timestamp_ms'))} | raw {integrity.get('raw_boundary')} | duration {integrity.get('seconds')}s | fresh={integrity['fresh']}",
        f"Replay {replay['regions_verified']}/{replay['regions_requested']} regions | {replay['raw_records_replayed']} raw | {replay['observations_compared']} observations | {replay['mismatch_count']} mismatches | {replay['unavailable_version_count']} missing reducers | {replay['seconds']:.2f}s",
        'Unverified regions '+encode([dict(anchor=r['anchor'],reasons=r['reasons'],reason=r.get('reason')) for r in replay['regions'] if r['status']!='VERIFIED']),
        f"Instrumented campaign from {utc(data['campaign']['start_ms'])}, {data['campaign']['recorded_seconds']/3600:.2f} recorded hours: "+encode(data['campaign']['gates']),
        'Historical gates (including old interruptions): '+encode(data['historical_gates'])]
    if data.get('latest_deep_replay'):
        r=data['latest_deep_replay']
        lines.append(f"Dated deep replay {utc(r['timestamp_ms'])}: {r['regions_verified']}/{r['regions_requested']} regions, {r['observations_compared']} observations, {r['mismatch_count']} mismatches, {r['unavailable_version_count']} missing reducers")
    latest={(e['path'],e.get('scope','unknown')):e for e in data['recovery']['exercises']}
    for (path,scope),e in latest.items():
        lines.append(f"Recovery {path:<20} {scope:<10} {e['result']} {e.get('seconds','')}s")
    current=resources['current']
    lines += [f"Resource samples {resources['samples']} | RSS {current.get('rss_bytes')} bytes | peak {resources['rss_max_observed_bytes']} | WAL peak {resources['wal_max_observed_bytes']} | CPU {resources['cpu_percent_one_core']}% of one core",
        f"Growth measurement {resources['growth_measurement']} over {resources['growth_window_seconds']}s; short windows are provisional."]
    for kind in ('checkpoint','restore'):
        values=resources[kind+'_measurements']
        if values:
            durations=[r['seconds'] for r in values]
            lines.append(f"{kind} last={durations[-1]:.3f}s range={min(durations):.3f}-{max(durations):.3f}s ({len(values)} samples)")
    lines += ['Criteria operational-v1: exclude unrecorded time; >60s interruption fails an elapsed gate; integrity <=24h; incomplete replay/recovery is PARTIAL.',
        'Source outages alone do not fail recorder acceptance. Historical failures remain visible.',
        f"Report {data['report_seconds']:.2f}s; evidence in {tape['path']}.acceptance.sqlite3; --json includes full provenance."]
    return '\n'.join(lines)
