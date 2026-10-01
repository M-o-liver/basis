"""Prospective protocol and append-only evidence, independent of the market reducer."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid
import zlib
from .store import encode, decode

VERSION = '1.0.0'
PROTOCOL = dict(
    version=VERSION, primary_horizon='30m', horizons={'30s':30000,'5m':300000,'30m':1800000,'2h':7200000},
    primary_outcome='sign(PM_0-OPT_0)*(OPT_h-OPT_0)*100',
    convergence='abs(GAP_0)-abs(GAP_h); descriptive, not causal leadership',
    entry_clock='first committed compact frame crossing threshold; not a subsecond entry',
    open_gap_pp=4.0, close_gap_pp=1.0, movement_epsilon_pp=.10, frame_max_age_ms=15000,
    deferred_pm_jump_pp=.25, deferred_horizons={'next_open':0,'open_1m':60000,'open_5m':300000,'open_30m':1800000},
    first_print_grace_ms=300000, expiry_near_ms=7200000,
    control_seed=1729, control_period_ms=1800000, control_lookback_ms=7200000,
    control_matching=['asset','event_type','direction','OPT_probability_bin','time_to_expiry_bin',
                      'session_regime','IV_bin','absolute_log_threshold_distance_bin'],
    control_selection='deterministic random phase per event/half-hour; match only already available anchors, never outcomes',
    control_comparison='apply treatment GAP sign to matched control OPT movement; report control reuse',
    probability_bins=[0,.02,.05,.10,.20,.40,.60,.80,.95,1.00000001],
    time_to_expiry_bins_hours=[0,2,24,168,720,100000], iv_bins=[0,.2,.5,1,100],
    distance_bins=[0,.01,.05,.15,100], gap_bins_pp=[0,4,8,100], rel_threshold=.30,
    pm_travel_threshold=.70, fresh_pm_movement_pp=.25,
    analyzer_flag='score above prior 64 available scores 90th percentile, after >=32 scores; not a trading direction',
    analyzer_quantile=.90, analyzer_min_baseline=32, analyzer_baseline_size=64,
    analyzer_max_age_ms=180000, sequential_martingale='DISABLED: no calibrated sequential combination',
    regression_base=['gap_pp','relative_gap','recent_spot_return','log_time_to_expiry_hours','local_iv','event_direction','neighbor_agreement'],
    regression='expanding prequential least-squares; training labels must have matured before entry; base+one analyzer score or PM-specific movement',
    regression_min_training=50, regression_min_events=20,
    uncertainty='deterministic bootstrap by asset/UTC-day, not by frames; >=8 blocks, descriptive unadjusted 95% intervals',
    minimum_episodes=50, minimum_unique_events=20, minimum_dependence_blocks=8,
    calibration='first eligible forecast per unique proposition, never repeated frames; sparse bins merged by adjacent probability region',
    calibration_min_bin_events=20, equity_proxy_min_events=30,
    version_policy='stop eligibility on reducer/calculation/feature/analyzer/config mismatch; source/UI changes do not reset boundary',
    historical_label='EXPLORATORY / HISTORICAL', prospective_label='PROSPECTIVE',
    causality='repricing prediction, forecast calibration and trading profitability are separate; options are risk-neutral/model proxies',
)

# Supplementary feature definitions get their own future-effective registration.
# They are not quietly attached to entries reconstructed before that prefix.
TEMPORAL_HYPOTHESIS = dict(name='temporal-diagnostic-increment-v1',
    prediction='Existing temporal diagnostics improve 30-minute conventional repricing predictions beyond the frozen BASE features',
    primary_outcome='delta_opt_pp; same prequential BASE versus BASE+one feature protocol',
    features=dict(
        lead_lag_multiscale='Unweighted mean finite strength across existing non-INSUFFICIENT scales; preserve individual scales; leadership association, not price direction',
        event_sync='Unweighted mean P(OPT event within window | PM event) minus P(PM event within window | OPT event) across existing windows with positive triggers in both legs; native receipt clock'),
    flag_policy='No extra alarm threshold or price-direction vote; continuous regression predictors only',
    data_policy='Only entries with raw/frame IDs at or after this hypothesis registration; no retroactive scoring')


def digest(data):
    return hashlib.sha256(encode(data).encode()).hexdigest()


class EvaluationDB:
    def __init__(self, tape, read_only=False):
        self.path=Path(tape).with_suffix('.evaluation.sqlite3')
        self.db=sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro' if read_only else str(self.path),
            uri=read_only,timeout=10,isolation_level=None,check_same_thread=False)
        self.db.row_factory=sqlite3.Row
        if read_only:return
        self.db.execute('PRAGMA journal_mode=WAL');self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA foreign_keys=ON');self.db.execute('PRAGMA recursive_triggers=ON')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS campaigns(id TEXT PRIMARY KEY,started_ms INTEGER NOT NULL,
            first_raw_id INTEGER NOT NULL,first_frame_id INTEGER NOT NULL,mode TEXT NOT NULL,protocol_hash TEXT NOT NULL,data BLOB NOT NULL);
          CREATE TABLE IF NOT EXISTS entries(id TEXT PRIMARY KEY,campaign_id TEXT NOT NULL REFERENCES campaigns(id),
            event_id TEXT NOT NULL,event_version TEXT NOT NULL,opened_ms INTEGER NOT NULL,raw_id INTEGER NOT NULL,
            frame_id INTEGER NOT NULL,kind TEXT NOT NULL,eligible INTEGER NOT NULL,match_key TEXT,data BLOB NOT NULL);
          CREATE INDEX IF NOT EXISTS entry_campaign ON entries(campaign_id,opened_ms,id);
          CREATE INDEX IF NOT EXISTS entry_control ON entries(campaign_id,kind,match_key,opened_ms);
          CREATE TABLE IF NOT EXISTS matches(entry_id TEXT PRIMARY KEY REFERENCES entries(id),control_id TEXT REFERENCES entries(id),data BLOB NOT NULL);
          CREATE TABLE IF NOT EXISTS horizons(entry_id TEXT REFERENCES entries(id),name TEXT,due_ms INTEGER NOT NULL,
            PRIMARY KEY(entry_id,name));
          CREATE INDEX IF NOT EXISTS horizon_due ON horizons(due_ms);
          CREATE TABLE IF NOT EXISTS outcomes(entry_id TEXT REFERENCES entries(id),name TEXT,due_ms INTEGER NOT NULL,
            status TEXT NOT NULL,created_ms INTEGER NOT NULL,data BLOB NOT NULL,PRIMARY KEY(entry_id,name));
          CREATE TABLE IF NOT EXISTS forecasts(campaign_id TEXT REFERENCES campaigns(id),event_id TEXT,event_version TEXT,
            timestamp_ms INTEGER,raw_id INTEGER,data BLOB NOT NULL,PRIMARY KEY(campaign_id,event_id));
          CREATE TABLE IF NOT EXISTS resolutions(id TEXT PRIMARY KEY,event_id TEXT NOT NULL,event_version TEXT,
            kind TEXT NOT NULL,available_ms INTEGER NOT NULL,raw_id INTEGER,data BLOB NOT NULL);
          CREATE INDEX IF NOT EXISTS resolution_event ON resolutions(event_id,available_ms);
          CREATE TABLE IF NOT EXISTS evidence(id INTEGER PRIMARY KEY,campaign_id TEXT,kind TEXT,timestamp_ms INTEGER,data BLOB NOT NULL);
          CREATE TABLE IF NOT EXISTS hypotheses(id TEXT PRIMARY KEY,campaign_id TEXT REFERENCES campaigns(id),
            version INTEGER,effective_raw_id INTEGER,effective_frame_id INTEGER,registered_ms INTEGER,data BLOB NOT NULL);
          CREATE TABLE IF NOT EXISTS progress(campaign_id TEXT PRIMARY KEY REFERENCES campaigns(id),frame_id INTEGER NOT NULL,
            updated_ms INTEGER NOT NULL,data BLOB NOT NULL);
        ''')
        for table in ('campaigns','entries','matches','horizons','outcomes','forecasts','resolutions','evidence','hypotheses'):
            for action in ('UPDATE','DELETE'):
                self.db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'immutable evaluation evidence'); END")
        # REPLACE is deletion in disguise even on connections with recursive triggers off.
        for table,condition in [('campaigns','id=NEW.id'),('entries','id=NEW.id'),('matches','entry_id=NEW.entry_id'),
            ('horizons','entry_id=NEW.entry_id AND name=NEW.name'),('outcomes','entry_id=NEW.entry_id AND name=NEW.name'),
            ('forecasts','campaign_id=NEW.campaign_id AND event_id=NEW.event_id'),('resolutions','id=NEW.id'),('hypotheses','id=NEW.id')]:
            self.db.execute(f"CREATE TRIGGER IF NOT EXISTS no_replace_{table} BEFORE INSERT ON {table} WHEN EXISTS(SELECT 1 FROM {table} WHERE {condition}) BEGIN SELECT RAISE(ABORT,'evaluation record already frozen'); END")

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:yield;self.db.execute('COMMIT')
        except BaseException:self.db.execute('ROLLBACK');raise

    def campaign(self, evaluation_id=None, mode='PROSPECTIVE'):
        row=self.db.execute('SELECT data FROM campaigns WHERE '+('id=?' if evaluation_id else 'mode=?')+' ORDER BY started_ms LIMIT 1',
            (evaluation_id or mode,)).fetchone()
        return decode(row[0]) if row else None

    def register(self, data):
        with self.transaction():
            old=self.campaign(data['evaluation_id'])
            if old:
                if old!=data:raise ValueError('Campaign boundary/definition is immutable; register an explicit new campaign version')
                return old
            self.db.execute('INSERT INTO campaigns VALUES(?,?,?,?,?,?,?)',(data['evaluation_id'],data['started_ms'],
                data['first_eligible_global_raw_id'],data['first_eligible_frame_id'],data['mode'],digest(data['protocol']),encode(data)))
        return data

    def record(self, campaign_id, kind, data):
        self.db.execute('INSERT INTO evidence(campaign_id,kind,timestamp_ms,data) VALUES(?,?,?,?)',
            (campaign_id,kind,time.time_ns()//1000000,encode(data)))

    def close(self):self.db.close()


def freeze_boundary(tape, live_state, git_revision, mode='PROSPECTIVE'):
    """Read a committed prefix atomically; never acquire research-writer ownership."""
    from .algos import FAMILIES, VERSION as ALGO_VERSION
    from .replay import reducer_hash
    from .tape import open_store
    from .operations import Journal
    db=EvaluationDB(tape)
    try:
        existing=db.campaign(mode=mode)
        if existing:return existing
        # Redundant append-only journal survives accidental loss of the sidecar.
        journal=Journal(str(Path(tape).resolve()))
        try:
            previous=next((r['campaign'] for r in journal.entries('evaluation_campaign') if r['campaign']['mode']==mode),None)
            if previous:return db.register(previous)
            reader=open_store(tape,read_only=True)
            try:
                if hasattr(reader,'manifest'):
                    reader._refresh();item=reader.manifest['segments'][-1];active=reader._handle(item)
                    active.db.execute('BEGIN')
                    try:
                        raw=active.db.execute('SELECT COALESCE(MAX(id),?) FROM records',(item['base']['raw'],)).fetchone()[0]
                        frame=active.db.execute('SELECT COALESCE(MAX(id),?) FROM frames',(item['base']['observations'],)).fetchone()[0]
                        started=time.time_ns()//1000000
                    finally:active.db.execute('COMMIT')
                    # A rotation during capture is a legitimate prefix; IDs remain global.
                    storage_version=2
                else:
                    reader.db.execute('BEGIN')
                    try:
                        raw=reader.db.execute('SELECT COALESCE(MAX(id),0) FROM raw').fetchone()[0]
                        frame=reader.db.execute('SELECT COALESCE(MAX(id),0) FROM observations').fetchone()[0]
                        started=time.time_ns()//1000000
                    finally:reader.db.execute('COMMIT')
                    storage_version=1
                panel=[]
                for event in live_state.get('rows',[]):
                    panel.extend(reader.history(event['event_id'],limit=1,tail=True,before_id=frame+1))
                panel=[r for r in panel if r['raw_id']<=raw]
            finally:reader.close()
            versions=live_state['versions']
            data=dict(evaluation_id='basis-edge-'+uuid.uuid4().hex[:12],evaluation_version=1,mode=mode,
                started_ms=started,evaluation_started_at=datetime.fromtimestamp(started/1000,timezone.utc).isoformat(),
                first_eligible_global_raw_id=raw+1,first_eligible_frame_id=frame+1,git_revision=git_revision,
                producer_code_hash=versions['code_hash'],reducer_version=reducer_hash(Path(__file__).parent),
                calculation_version=versions['calculation'],feature_version=versions['feature'],config_hash=versions['config'],
                analyzer_versions={name:ALGO_VERSION for name in FAMILIES},storage_version=storage_version,
                protocol=PROTOCOL,initial_panel=panel,
                registration_note='Protocol registered before scoring; implementation backfill uses only committed causal features. No historical hypothesis selection is called prospective.')
            db.register(data);journal.record('evaluation_campaign',campaign=data)
            return data
        finally:journal.close()
    finally:db.close()


def register_hypothesis(tape, definition, evaluation_id=None):
    """A later exploratory discovery starts at a future committed prefix, never at old outcomes."""
    from .tape import open_store
    if not isinstance(definition,dict) or not definition.get('name') or not definition.get('prediction'):
        raise ValueError('Hypothesis JSON requires name and prediction, with explicitly defined features/metrics')
    db=EvaluationDB(tape);reader=open_store(tape,read_only=True)
    try:
        campaign=db.campaign(evaluation_id)
        if not campaign:raise ValueError('Freeze a campaign first')
        if hasattr(reader,'manifest'):
            reader._refresh();item=reader.manifest['segments'][-1];handle=reader._handle(item)
            handle.db.execute('BEGIN')
            try:
                raw=handle.db.execute('SELECT COALESCE(MAX(id),?) FROM records',(item['base']['raw'],)).fetchone()[0]
                frame=handle.db.execute('SELECT COALESCE(MAX(id),?) FROM frames',(item['base']['observations'],)).fetchone()[0]
            finally:handle.db.execute('COMMIT')
        else:
            reader.db.execute('BEGIN')
            try:
                raw=reader.db.execute('SELECT COALESCE(MAX(id),0) FROM raw').fetchone()[0]
                frame=reader.db.execute('SELECT COALESCE(MAX(id),0) FROM observations').fetchone()[0]
            finally:reader.db.execute('COMMIT')
        with db.transaction():
            version=db.db.execute('SELECT COALESCE(MAX(version),0)+1 FROM hypotheses WHERE campaign_id=?',(campaign['evaluation_id'],)).fetchone()[0]
            data=dict(id='hypothesis-'+uuid.uuid4().hex[:12],campaign_id=campaign['evaluation_id'],version=version,
                effective_raw_id=raw+1,effective_frame_id=frame+1,registered_ms=time.time_ns()//1000000,
                definition=definition,definition_hash=digest(definition),status='REGISTERED; no retroactive scoring')
            db.db.execute('INSERT INTO hypotheses VALUES(?,?,?,?,?,?,?)',(data['id'],data['campaign_id'],version,raw+1,frame+1,data['registered_ms'],encode(data)))
        return data
    finally:reader.close();db.close()


def ensure_temporal_hypothesis(tape):
    db=EvaluationDB(tape)
    try:
        campaign=db.campaign()
        if not campaign:return None
        for r in db.db.execute('SELECT data FROM hypotheses WHERE campaign_id=? ORDER BY version',(campaign['evaluation_id'],)):
            data=decode(r[0])
            if data['definition']==TEMPORAL_HYPOTHESIS:return data
    finally:db.close()
    return register_hypothesis(tape,TEMPORAL_HYPOTHESIS,campaign['evaluation_id'])


def historical_campaign(tape, start, end):
    """Explicit bounded exploration; a historical campaign cannot become prospective."""
    from .algos import FAMILIES, VERSION as ALGO_VERSION
    from .tape import open_store
    from .replay import reducer_hash
    if not isinstance(start,int) or not isinstance(end,int) or not start < end or end-start>6*3600000:
        raise ValueError('Historical evaluation requires an explicit positive range of at most six hours; repeat the command to resume its bounded cursor')
    db=EvaluationDB(tape);reader=open_store(tape,read_only=True)
    try:
        rows=reader.history(start=start,end=end,limit=500)
        if not rows:raise ValueError('No frames in the historical range')
        first=rows[0];code=first.get('code_hash')
        identity='basis-historical-'+digest([str(Path(tape).resolve()),start,end,VERSION])[:12]
        existing=db.campaign(identity)
        if existing:return existing
        panel=[]
        for key in sorted({r['event_id'] for r in rows}):
            panel+=reader.history(key,limit=1,tail=True,before_id=first['observation_id'])
        data=dict(evaluation_id=identity,evaluation_version=1,mode='HISTORICAL',started_ms=start,
            evaluation_started_at=datetime.fromtimestamp(start/1000,timezone.utc).isoformat(),end_ms=end,
            first_eligible_global_raw_id=first['raw_id'],first_eligible_frame_id=first['observation_id'],
            git_revision='HISTORICAL_EXPLORATION',producer_code_hash=code,
            reducer_version=reducer_hash(Path(tape).resolve().parent/'versions'/str(code)),
            calculation_version=first.get('calculation_version'),feature_version=first.get('features',{}).get('feature_version'),
            config_hash=first.get('parameter_hash'),analyzer_versions={name:ALGO_VERSION for name in FAMILIES},
            storage_version=2 if hasattr(reader,'manifest') else 1,protocol=PROTOCOL,initial_panel=panel,
            registration_note='EXPLORATORY / HISTORICAL: definition registered after this data existed; never confirmatory')
        return db.register(data)
    finally:reader.close();db.close()
