"""Independent, restartable causal scorer over the compact research tape."""
import fcntl
import heapq
import json
import logging
from pathlib import Path
import threading
import time
import zlib
import numpy as np
from .algos import FAMILIES
from .evaluation import EvaluationDB, TEMPORAL_HYPOTHESIS, digest
from .evaluation_features import (compact, finite, horizon_outcome, matching_key,
    neighborhood, quality_reason, semantic_version, session_regime, sign, temporal_predictor)
from .replay import reducer_hash
from .store import decode, encode
from .tape import open_store


def packed(value):
    return zlib.compress(encode(value).encode(), 6)


class EvidenceReader:
    """Small indexed prefix reads across v1, closed v2 and the active segment."""
    def __init__(self, tape):
        self.tape = tape

    def sources(self, raw_id):
        if not hasattr(self.tape, 'manifest'):
            yield self.tape
            return
        self.tape._refresh()
        for item in reversed(self.tape.manifest['segments']):
            if item['base']['raw'] < raw_id:
                yield self.tape._handle(item)
        if self.tape.legacy:
            yield self.tape.legacy

    def analyzers(self, scope, raw_id, now, protocol):
        result = {}
        sources = list(self.sources(raw_id))
        for name in FAMILIES:
            rows = []
            for source in sources:
                # Equal-raw analyzer output follows Engine.observe(), so is not available at entry.
                rows.extend(source.db.execute('SELECT id,raw_id,data FROM analyzers '
                    'WHERE scope=? AND algo_name=? AND raw_id<? AND timestamp_ms<=? '
                    'ORDER BY id DESC LIMIT ?', (scope,name,raw_id,now,
                    protocol['analyzer_baseline_size']+1-len(rows))).fetchall())
                if len(rows) >= protocol['analyzer_baseline_size']+1:
                    break
            if not rows:
                result[name] = dict(status='MISSING', reason='No causally available output')
                continue
            outputs = [dict(decode(r['data']),record_id=r['id'],raw_id=r['raw_id']) for r in rows]
            current = outputs[0]
            current_score = current.get('raw_score')
            previous = [r['raw_score'] for r in outputs[1:] if r.get('status') == 'ok' and finite(r.get('raw_score'))]
            fresh = 0 <= now-current['timestamp'] <= protocol['analyzer_max_age_ms']
            threshold = float(np.quantile(previous, protocol['analyzer_quantile'])) if len(previous) >= protocol['analyzer_min_baseline'] else None
            result[name] = dict(output=current, available=fresh and current.get('status') == 'ok',
                score=current_score if fresh and current.get('status') == 'ok' and finite(current_score) else None,
                flagged=(current_score > threshold) if threshold is not None and fresh and finite(current_score) and current.get('status') == 'ok' else None,
                prior_scores=len(previous), threshold=threshold,
                reason=('STALE_OUTPUT' if not fresh else 'NO_SCALAR_SCORE' if current_score is None
                        else 'BASELINE_INSUFFICIENT' if threshold is None else None))
        return result

    def salient(self, scope, raw_id, now):
        result = []
        for source in self.sources(raw_id):
            if not hasattr(source, 'codec'):
                continue
            for row in source.db.execute('SELECT * FROM salient WHERE scope=? AND timestamp_ms BETWEEN ? AND ? '
                'AND raw_id<=? ORDER BY timestamp_ms DESC,id DESC LIMIT ?', (scope,now-30000,now,raw_id,32-len(result))):
                item = dict(row)
                for key in ('before_value','after_value'):
                    item[key] = json.loads(item[key])
                result.append(item)
            if len(result) >= 32:
                break
        return sorted(result,key=lambda x: (x['timestamp_ms'],x['raw_id'],x['id']))


class Recorder:
    """One sidecar transaction commits frozen evidence and its processing cursor."""
    def __init__(self, tape_path, evaluation_id=None, reader=None, db=None):
        self.tape_path = str(tape_path)
        self.tape = reader or open_store(tape_path, read_only=True)
        self.db = db or EvaluationDB(tape_path)
        self.campaign = self.db.campaign(evaluation_id)
        if not self.campaign:
            raise ValueError('No registered campaign. Use basis evaluate --start to freeze one.')
        self.cid = self.campaign['evaluation_id']
        self.ownership=open(str(self.db.path)+'.'+digest(self.cid)[:12]+'.worker.lock','a')
        try:fcntl.flock(self.ownership,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            self.ownership.close();self.tape.close();self.db.close()
            raise RuntimeError('An evaluator already owns this campaign; reports remain read-only')
        self.protocol = self.campaign['protocol']
        self.reader = EvidenceReader(self.tape)
        saved = self.db.db.execute('SELECT * FROM progress WHERE campaign_id=?',(self.cid,)).fetchone()
        self.frame_id = saved['frame_id'] if saved else self.campaign['first_eligible_frame_id']-1
        self.state = decode(saved['data']) if saved else dict(
            panel={r['event_id']:compact(r) for r in self.campaign.get('initial_panel',[])},
            episodes={}, controls={}, deferred={}, closes={}, frames_processed=0, raw_id=0,
            last_timestamp_ms=self.campaign['started_ms'], counts={}, stopped_versions={})
        if not saved:
            for key, row in self.state['panel'].items():
                if finite(row.get('gap_pp')) and abs(row['gap_pp']) >= self.protocol['open_gap_pp']:
                    self.state['episodes'][key] = dict(sign=sign(row['gap_pp']), version=semantic_version(row),
                        id=None, left_censored=True, gap_pp=row['gap_pp'])
        self.pending = []
        self.versions = {}
        self.hypotheses=[]
        self.reload_pending()

    def reload_pending(self):
        self.pending = [(r['due_ms'],r['entry_id'],r['name']) for r in self.db.db.execute(
            'SELECT h.* FROM horizons h JOIN entries e ON e.id=h.entry_id WHERE e.campaign_id=? '
            'AND NOT EXISTS(SELECT 1 FROM outcomes o WHERE o.entry_id=h.entry_id AND o.name=h.name)',(self.cid,))]
        heapq.heapify(self.pending)

    def version_reason(self, row):
        c = self.campaign
        if row.get('calculation_version') != c['calculation_version'] or (row.get('features') or {}).get('feature_version') != c['feature_version'] or row.get('parameter_hash') != c['config_hash']:
            return 'SCIENTIFIC_VERSION_CHANGED'
        code = row.get('code_hash')
        if code not in self.versions:
            self.versions[code] = reducer_hash(Path(self.tape_path).resolve().parent/'versions'/str(code))
        if self.versions[code] != c['reducer_version']:
            return 'REDUCER_VERSION_UNAVAILABLE' if self.versions[code] is None else 'REDUCER_CHANGED'
        return None

    def entry(self, row, kind, eligible=True, reason=None, session=None):
        now = row['timestamp_wall']
        session = session or session_regime(row, now)
        data = compact(row)
        data.update(id=f'{self.cid}:{kind}:{row["event_id"]}:{row["observation_id"]}',
            campaign_id=self.cid, event_version=semantic_version(row), opened_ms=now,
            opening_raw_id=row['raw_id'], opening_frame_id=row['observation_id'],
            kind=kind, eligible=bool(eligible), ineligible_reason=reason, session=session,
            time_to_expiry_ms=row['expiry']-now, neighbors=neighborhood(row,self.state['panel'],self.protocol),
            underlying_context=compact(row).get('underlying_context',{}),
            analyzers=self.reader.analyzers(row['event_id'],row['raw_id'],now,self.protocol),
            native_recent_events=self.reader.salient(row['event_id'],row['raw_id'],now),
            entry_recorded_at_ms=time.time_ns()//1000000,
            creation_mode='CAUSAL_RECONSTRUCTION_OF_PRE_REGISTERED_FRAME_ENTRY')
        for hypothesis in self.hypotheses:
            if hypothesis['definition']!=TEMPORAL_HYPOTHESIS or row['raw_id']<hypothesis['effective_raw_id'] or row['observation_id']<hypothesis['effective_frame_id']:
                continue
            for name in TEMPORAL_HYPOTHESIS['features']:
                analyzer=data['analyzers'][name]
                if analyzer.get('available'):
                    analyzer['registered_predictor']=temporal_predictor(name,analyzer['output'])
                    analyzer['predictor_hypothesis_id']=hypothesis['id']
        if row.get('deferred_context'):
            data['deferred_context']=row.get('deferred_context',{})
        key = encode(matching_key(row,session['regime'],self.protocol))
        self.db.db.execute('INSERT INTO entries VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (data['id'],self.cid,row['event_id'],data['event_version'],now,row['raw_id'],row['observation_id'],
             kind,int(eligible),key,packed(data)))
        if kind != 'CONTROL':
            candidates = self.db.db.execute('SELECT id FROM entries WHERE campaign_id=? AND kind=? AND eligible=1 '
                'AND match_key=? AND opened_ms BETWEEN ? AND ? AND event_id!=? ORDER BY id',
                (self.cid,'CONTROL',key,now-self.protocol['control_lookback_ms'],now,row['event_id'])).fetchall()
            # Outcomes are not read in control selection; matching is fixed before treatment follow-up.
            control = min((r['id'] for r in candidates), key=lambda i: digest([self.protocol['control_seed'],data['id'],i]), default=None)
            self.db.db.execute('INSERT INTO matches VALUES(?,?,?)',(data['id'],control,packed(dict(
                match_key=json.loads(key), candidate_count=len(candidates), status='MATCHED' if control else 'NO_MATCH',
                reason=None if control else 'No prior control from another event in all matching bins'))))
        horizons = dict(self.protocol['horizons'])
        if row.get('deferred_context') and session.get('next_open') is not None:
            horizons.update({k:session['next_open']-now+v for k,v in self.protocol['deferred_horizons'].items()})
        if row['expiry'] >= now:
            horizons['expiry'] = row['expiry']-now
        for name,offset in horizons.items():
            due = now+offset
            self.db.db.execute('INSERT INTO horizons VALUES(?,?,?)',(data['id'],name,due))
            heapq.heappush(self.pending,(due,data['id'],name))
        return data

    def forecast(self, row, reason):
        if reason or self.db.db.execute('SELECT 1 FROM forecasts WHERE campaign_id=? AND event_id=?',(self.cid,row['event_id'])).fetchone():
            return
        data = {k:row.get(k) for k in ('event_id','event_text','asset','event_type','direction','expiry','strike_or_threshold',
            'window_start','settlement_source','threshold_inclusive','pm_yes','opt_yes','spot','mapping_hash','input_refs',
            'source_state','quality_flags','model_confidence','surface_features','model_inputs')}
        data.update(event_version=semantic_version(row),timestamp_ms=row['timestamp_wall'],
                    opening_frame_id=row['observation_id'],raw_id=row['raw_id'])
        self.db.db.execute('INSERT INTO forecasts VALUES(?,?,?,?,?,?)',(self.cid,row['event_id'],data['event_version'],
            row['timestamp_wall'],row['raw_id'],packed(data)))

    def path_hit(self, row):
        if row['event_type']!='touch' or not finite(row.get('spot')) or not row.get('spot_timestamp'):
            return
        stamp=row['spot_timestamp'];start=row.get('window_start') or self.campaign['started_ms']
        if not max(start,self.campaign['started_ms'])<=stamp<=min(row['expiry'],row['timestamp_wall']):return
        ref=row.get('input_refs',{}).get('spot')
        if ref is None or ref<self.campaign['first_eligible_global_raw_id']:return
        if row['asset'] not in ('BTC','ETH') and not session_regime(row,stamp)['target_open']:return
        direction=row['direction'];price=row['spot'];threshold=row['strike_or_threshold']
        crossed=(price>=threshold if direction=='up' else price<=threshold) if row.get('threshold_inclusive') else (price>threshold if direction=='up' else price<threshold)
        if not crossed:return
        identity=digest([self.cid,row['event_id'],semantic_version(row),'INDEPENDENT_PATH_HIT'])
        if self.db.db.execute('SELECT 1 FROM resolutions WHERE id=?',(identity,)).fetchone():return
        data=dict(event_id=row['event_id'],event_version=semantic_version(row),kind='INDEPENDENT_PATH_HIT',
            yes=None,review='REFERENCE_PATH_ONLY',raw_id=ref,frame_id=row['observation_id'],spot=price,
            threshold=threshold,direction=direction,source_timestamp=stamp,
            recorded_at_ms=time.time_ns()//1000000,settlement_source=row.get('settlement_source'),
            note='Observed reference print crossed threshold; not official settlement or complete-path coverage')
        self.db.db.execute('INSERT INTO resolutions VALUES(?,?,?,?,?,?,?)',(identity,row['event_id'],
            data['event_version'],data['kind'],row['timestamp_wall'],ref,packed(data)))

    def record_outcome(self, entry, name, due, data):
        self.db.db.execute('INSERT INTO outcomes VALUES(?,?,?,?,?,?)',
            (entry['id'],name,due,data['status'],time.time_ns()//1000000,packed(data)))

    def finalize(self, until, inclusive=False):
        while self.pending and (self.pending[0][0] <= until if inclusive else self.pending[0][0] < until):
            due,key,name = heapq.heappop(self.pending)
            entry = decode(self.db.db.execute('SELECT data FROM entries WHERE id=?',(key,)).fetchone()[0])
            row = self.state['panel'].get(entry['event_id'])
            result = horizon_outcome(entry,row,self.state['panel'],due,self.protocol)
            version = self.version_reason(row) if row else None
            if version:
                result.update(status='MISSING',reason=version)
            self.record_outcome(entry,name,due,result)

    def deferred(self, row, session):
        key, now = row['event_id'], row['timestamp_wall']
        if session['target_open']:
            if not quality_reason(row,now,self.protocol):
                self.state['closes'][key] = compact(row)
            return
        identity = str(session['next_open'])+':'+semantic_version(row)
        baseline = self.state['closes'].get(key)
        if not baseline or not 0<=session['target_close']-baseline['timestamp_wall']<=self.protocol['frame_max_age_ms']:
            attempts=self.state.setdefault('close_attempts',{})
            if attempts.get(key)!=identity:
                before = self.tape.history(key,end=session['target_close'],limit=1,tail=True,
                    before_id=row['observation_id'])
                attempts[key]=identity
            else:before=[]
            baseline = compact(before[0]) if before else None
            if baseline and (baseline.get('source_state') not in ('OK','PROXY') or
                    not session_regime(baseline,baseline['timestamp_wall'])['target_open'] or
                    session['target_close']-baseline['timestamp_wall'] > self.protocol['frame_max_age_ms']):
                baseline = None
            if baseline:
                self.state['closes'][key] = baseline
        if baseline and (semantic_version(baseline)!=semantic_version(row) or self.version_reason(baseline)):
            baseline=None
        # A fresh PM update is the deferred signal; closed target estimates are deliberately frozen.
        if not finite(row.get('pm_yes')) or row.get('pm_source') == 'gamma_indicative' or now-(row.get('pm_timestamp') or 0) > 45000:
            return
        if row['expiry'] < now or row['expiry'] < session['next_open']:
            return
        frozen = dict(row)
        if baseline:
            for field in ('opt_timestamp','options_received_ms','opt_timestamp_origin','basis_method',
                          'model_inputs','surface_features','model_confidence'):
                frozen[field]=baseline.get(field)
            frozen['opt_yes'] = baseline['opt_yes']
            frozen['gap_pp'] = 100*(frozen['pm_yes']-frozen['opt_yes'])
            frozen['relative_gap'] = abs(frozen['pm_yes']-frozen['opt_yes'])/frozen['opt_yes'] if frozen['opt_yes'] else None
            frozen['input_refs'] = dict(row.get('input_refs',{}),options=baseline['input_refs'].get('options'))
        else:
            frozen.update(opt_yes=None,gap_pp=None,relative_gap=None)
        frozen['deferred_context']=dict(target_close_ms=session['target_close'],
            close_frame_id=baseline.get('observation_id') if baseline else None,
            close_raw_id=baseline.get('raw_id') if baseline else None,
            pm_at_target_close=baseline.get('pm_yes') if baseline else None,
            pm_change_since_target_close_pp=100*(row['pm_yes']-baseline['pm_yes']) if baseline else None,
            last_open_opt=baseline.get('opt_yes') if baseline else None,
            last_open_spot=baseline.get('spot') if baseline else None,
            current_target_model_state=row.get('source_state'))
        if baseline:self.maybe_control(frozen,session)
        if self.state['deferred'].get(key)==identity:return
        if baseline and abs(row['pm_yes']-baseline['pm_yes'])*100<self.protocol['deferred_pm_jump_pp']:return
        # Register lack of a target baseline rather than mislabel a closed-market experiment as stale.
        entry = self.entry(frozen,'DEFERRED_RESPONSE',eligible=bool(baseline),
            reason=None if baseline else 'MISSING_LAST_OPEN_TARGET_BASELINE',session=session)
        # Baseline provenance is appended separately, never mutating the frozen entry.
        self.db.record(self.cid,'deferred_close_reference',dict(entry_id=entry['id'],
            target_close_ms=session['target_close'],close_frame_id=baseline.get('observation_id') if baseline else None,
            close_raw_id=baseline.get('raw_id') if baseline else None,
            pm_at_close=baseline.get('pm_yes') if baseline else None,
            pm_change_since_close_pp=100*(row['pm_yes']-baseline['pm_yes']) if baseline else None,
            classification='DEFERRED_RESPONSE', reason=entry['ineligible_reason']))
        self.state['deferred'][key] = identity

    def maybe_control(self,row,session):
        key,now=row['event_id'],row['timestamp_wall']
        bucket=now//self.protocol['control_period_ms']
        phase=int(digest([self.protocol['control_seed'],key,bucket])[:12],16)%self.protocol['control_period_ms']
        scheduled=bucket*self.protocol['control_period_ms']+phase
        if (self.state['controls'].get(key)!=bucket and scheduled>=self.campaign['started_ms'] and
                0<=now-scheduled<=self.protocol['frame_max_age_ms']):
            self.entry(row,'CONTROL',session=session);self.state['controls'][key]=bucket

    def process(self, row):
        self.finalize(row['timestamp_wall'])
        if row['raw_id'] < self.campaign['first_eligible_global_raw_id'] or row['timestamp_wall'] < self.campaign['started_ms']:
            return
        row = compact(row)
        key, now = row['event_id'],row['timestamp_wall']
        prior_row=self.state['panel'].get(key)
        self.state['panel'][key] = row
        self.state['frames_processed'] += 1
        self.state['raw_id'] = row['raw_id']
        self.state['last_timestamp_ms'] = max(now,self.state['last_timestamp_ms'])
        version = self.version_reason(row)
        if version:
            token = str(row.get('code_hash'))+':'+str(row.get('parameter_hash'))
            if token not in self.state['stopped_versions']:
                self.state['stopped_versions'][token] = version
                self.db.record(self.cid,'eligibility_stopped',dict(reason=version,raw_id=row['raw_id'],frame_id=row['observation_id'],code_hash=row.get('code_hash')))
            return
        session = session_regime(row,now)
        self.path_hit(row)
        if row['asset'] not in ('BTC','ETH'):
            # Deferred entries are distinct from contemporaneous episode thresholding.
            self.deferred(row,session)
            self.expire_first_print(now,row)
        reason = quality_reason(row,now,self.protocol)
        self.forecast(row,reason)
        previous = self.state['episodes'].get(key)
        if reason:
            if previous and not previous.get('unavailable'):
                self.db.record(self.cid,'episode_data_gap',dict(entry_id=previous['id'],event_id=key,reason='DATA_GAP',
                    detail=reason,frame_id=row['observation_id'],raw_id=row['raw_id'],timestamp_ms=now,
                    left_censored=previous.get('left_censored',False)))
                previous['unavailable']=True
            return
        if previous and previous.get('unavailable'):
            self.db.record(self.cid,'episode_data_recovery',dict(entry_id=previous['id'],event_id=key,
                raw_id=row['raw_id'],frame_id=row['observation_id'],timestamp_ms=now))
            previous['unavailable']=False
        # Deterministic random moments are scheduled independently of discrepancy and future labels.
        self.maybe_control(row,session)
        g, direction = row['gap_pp'],sign(row['gap_pp'])
        version_id = semantic_version(row)
        if previous and (abs(g) <= self.protocol['close_gap_pp'] or direction != previous['sign'] or version_id != previous['version']):
            self.db.record(self.cid,'episode_end',dict(entry_id=previous['id'],event_id=key,
                reason='CLOSED' if abs(g) <= self.protocol['close_gap_pp'] else 'REVERSAL' if direction != previous['sign'] else 'MAPPING_CHANGED',
                raw_id=row['raw_id'],frame_id=row['observation_id'],timestamp_ms=now,left_censored=previous.get('left_censored',False)))
            self.state['episodes'].pop(key,None);previous=None
        if not previous and abs(g) >= self.protocol['open_gap_pp']:
            row['entry_origin']='OBSERVED_THRESHOLD_CROSSING' if prior_row and not quality_reason(prior_row,now,self.protocol) else 'FIRST_ELIGIBLE_FRAME_OR_RECOVERY; onset not observed'
            data = self.entry(row,'EPISODE',session=session)
            self.state['episodes'][key] = dict(sign=direction,version=version_id,id=data['id'],left_censored=False,gap_pp=g)

    def poll(self, until_ms=None, batch_size=500):
        self.hypotheses=[decode(r[0]) for r in self.db.db.execute('SELECT data FROM hypotheses WHERE campaign_id=? ORDER BY version',(self.cid,))]
        rows = self.tape.history(limit=batch_size,after_id=self.frame_id,
            end=until_ms if until_ms is not None else 2**63-1)
        with self.db.transaction():
            for row in rows:
                self.process(row)
                self.frame_id = row['observation_id']
            # Only close due horizons once every committed earlier frame has been consumed.
            if len(rows) < batch_size:
                now = min(time.time_ns()//1000000,until_ms) if until_ms is not None else time.time_ns()//1000000
                self.finalize(now,inclusive=True)
                self.expire_first_print(now)
            self.prune_working_state()
            self.db.db.execute('INSERT INTO progress VALUES(?,?,?,?) ON CONFLICT(campaign_id) DO UPDATE '
                'SET frame_id=excluded.frame_id,updated_ms=excluded.updated_ms,data=excluded.data',
                (self.cid,self.frame_id,time.time_ns()//1000000,packed(self.state)))
        return len(rows)

    def prune_working_state(self):
        # A years-long campaign must not write years of expired working panels every five seconds.
        # Frozen entries/outcomes remain intact; old unobserved horizons explicitly have no fresh frame.
        now=self.state['last_timestamp_ms']
        for key,row in list(self.state['panel'].items()):
            if now-row['timestamp_wall']<=7200000:continue
            self.state['panel'].pop(key,None);self.state['closes'].pop(key,None)
            self.state.get('close_attempts',{}).pop(key,None);self.state['controls'].pop(key,None)
            if row['expiry']<now:
                previous=self.state['episodes'].pop(key,None)
                if previous:self.db.record(self.cid,'episode_end',dict(entry_id=previous['id'],event_id=key,
                    reason='EXPIRED_WITHOUT_FRESH_FRAME',timestamp_ms=now,left_censored=previous.get('left_censored',False)))
                self.state['deferred'].pop(key,None)

    def expire_first_print(self, now, candidate=None):
        # First print is event-driven; do not substitute a later quote for a fixed next-open horizon.
        query='SELECT data FROM entries WHERE campaign_id=? AND kind=? AND NOT EXISTS(SELECT 1 FROM outcomes WHERE entry_id=entries.id AND name=?)'
        args=[self.cid,'DEFERRED_RESPONSE','FIRST_AVAILABLE_PROXY']
        if candidate is not None:
            query+=' AND event_id=?';args.append(candidate['event_id'])
        for r in self.db.db.execute(query,args):
            entry = decode(r[0]);opening = entry['session']['next_open'];row=candidate or self.state['panel'].get(entry['event_id'])
            end = opening+self.protocol['first_print_grace_ms']
            if row and opening <= row['timestamp_wall'] <= end and (row.get('options_received_ms') or 0) >= opening and (row.get('spot_timestamp') or 0) >= opening:
                due=row['timestamp_wall'];result=horizon_outcome(entry,row,self.state['panel'],due,self.protocol)
                if result['status']=='OBSERVED' and not self.version_reason(row):
                    result['reason']='FIRST_AVAILABLE_YAHOO_PROXY; not exchange auction quality'
                    self.record_outcome(entry,'FIRST_AVAILABLE_PROXY',due,result)
            elif now > end:
                self.record_outcome(entry,'FIRST_AVAILABLE_PROXY',end,dict(status='MISSING',reason='NO_TARGET_PROXY_IN_FIRST_5_MINUTES',due_ms=end))

    def close(self):
        self.tape.close();self.db.close();self.ownership.close()


class EvaluationWorker:
    """Evaluation outages are explicit, isolated from collection and paper execution."""
    def __init__(self, engine, journal):
        self.engine,self.journal=engine,journal
        self.stop=threading.Event();self.thread=None
        self.start_lock=threading.Lock()
        self.status=dict(state='DISABLED',reason='No prospective campaign registered')

    def start(self):
        with self.start_lock:
            path=Path(self.engine.store.path).with_suffix('.evaluation.sqlite3')
            if not path.exists() or self.thread and self.thread.is_alive():return
            self.thread=threading.Thread(target=self.run,name='basis-evaluation',daemon=True);self.thread.start()

    def run(self):
        recorder=None
        try:
            from .evaluation import ensure_temporal_hypothesis
            hypothesis=ensure_temporal_hypothesis(self.engine.store.path)
            if hypothesis:self.journal.record('evaluation_hypothesis',hypothesis=hypothesis)
            recorder=Recorder(self.engine.store.path)
            self.journal.record('evaluation_worker_start',evaluation_id=recorder.cid)
            from .evaluation_resolutions import ResolutionPoller
            resolutions=ResolutionPoller(self.engine,recorder,self.stop)
            while not self.stop.is_set():
                count=recorder.poll()
                self.status=dict(state='RUNNING',evaluation_id=recorder.cid,frame_id=recorder.frame_id,
                    frames_processed=recorder.state['frames_processed'],last_frame_ms=recorder.state['last_timestamp_ms'],
                    updated_ms=time.time_ns()//1000000,scientific_version_stops=recorder.state['stopped_versions'])
                if count<500:
                    resolutions.poll()
                    self.stop.wait(5)
                else:
                    self.stop.wait(.01)
        except Exception as error:
            logging.exception('Evaluation worker stopped; recorder remains independent')
            self.status=dict(state='ERROR',reason=str(error),updated_ms=time.time_ns()//1000000)
            self.journal.record('evaluation_worker_error',error=str(error))
        finally:
            if recorder:recorder.close()

    def close(self):
        self.stop.set()
        if self.thread:self.thread.join(timeout=20)
