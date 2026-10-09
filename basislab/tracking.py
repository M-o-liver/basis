"""Immutable noticed signals and quote-based forward marks. No orders or money."""
from collections import OrderedDict
import copy
from itertools import islice
import json
from pathlib import Path
import sqlite3
import threading
import time
import uuid
from .engine import Engine
from .gap_math import VERSION,conditional_paths,conditional_ev,execution_cost,gap_math,payoff
from .gap_trades import exact_quotes,liquidation,next_open
from .equities import regular_session
from .pricing import YEAR_MS
from .replay import reducer_hash
from .semantics import infer_event
from .store import decode,encode
from .tape import open_store

TRACK_VERSION='star-track-1.0'
ENTRY_WAIT_MS=600000
ACTIVE=('WAITING','LIVE')


class Tracker:
    def __init__(self,math_engine,path):
        self.math=math_engine;self.lock=threading.RLock();self.stop_event=threading.Event();self.thread=None
        self.tape=None;self.reducer=None;self.error=None
        self.db=sqlite3.connect(path,timeout=10,check_same_thread=False,isolation_level=None);self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL');self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''CREATE TABLE IF NOT EXISTS stars(id TEXT PRIMARY KEY,starred_ms INTEGER NOT NULL,event_id TEXT NOT NULL,data BLOB NOT NULL);
          CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,tracking_id TEXT NOT NULL REFERENCES stars(id),kind TEXT NOT NULL,timestamp_ms INTEGER NOT NULL,raw_id INTEGER,data BLOB NOT NULL);
          CREATE INDEX IF NOT EXISTS tracking_events ON events(tracking_id,id);
          CREATE TABLE IF NOT EXISTS cursor(id INTEGER PRIMARY KEY CHECK(id=1),raw_id INTEGER NOT NULL);''')
        for table in ('stars','events'):
            for op in ('UPDATE','DELETE'):
                self.db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{op} BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT,'immutable starred-signal evidence'); END")
        self.db.execute('INSERT OR IGNORE INTO cursor VALUES(1,?)',(math_engine.engine.last_raw_id,));self.reload()

    def reload(self):
        self.stars={}
        for r in self.db.execute('SELECT data FROM stars ORDER BY starred_ms,id'):
            frozen=decode(r[0]);self.stars[frozen['tracking_id']]=dict(frozen=frozen,status=frozen['initial_status'],reason=frozen.get('reason'),entry=None,mark=None)
        for r in self.db.execute('SELECT tracking_id,kind,data FROM events ORDER BY id'):self.apply(r['tracking_id'],r['kind'],decode(r['data']))
        self.cursor=self.db.execute('SELECT raw_id FROM cursor WHERE id=1').fetchone()[0]

    def apply(self,identity,kind,data):
        s=self.stars[identity]
        if kind=='ENTRY':s.update(status='LIVE',entry=data,reason=None)
        elif kind=='MARK':s.update(mark=data,reason=None)
        elif kind in ('ENDED','MISSED'):
            s.update(status=kind,reason=data['reason'],ended_at=data['timestamp_ms'],end=data)
            if data.get('final') is not None:s['mark']=data['final']
        elif kind=='WAIT_REASON':s['reason']=data['reason']

    def append(self,identity,kind,data):
        self.db.execute('INSERT INTO events(tracking_id,kind,timestamp_ms,raw_id,data) VALUES(?,?,?,?,?)',
                        (identity,kind,data['timestamp_ms'],data.get('raw_id'),encode(data)))
        self.apply(identity,kind,data)

    def create(self,event_id,snapshot_id,now=None):
        now=now if now is not None else time.time_ns()//1000000
        with self.lock:
            row=self.math.displayed_snapshot(event_id,snapshot_id)
            if row['timestamp_wall']>now or row['calculated_at']>now:raise ValueError('Displayed snapshot has a future timestamp')
            trade=copy.deepcopy(row.get('trade'));equity=row['asset'] not in ('BTC','ETH')
            target=now+5000
            if equity and regular_session(now)['market_state']!='OPEN':target=next_open(now)+10000
            with self.math.engine.lock:
                event=row.get('event_semantics') or self.math.engine.events.get(event_id)
                if not event or event.get('mapping_hash')!=row.get('mapping_hash'):raise ValueError('Displayed event semantics unavailable; refresh this market')
                boundary=self.math.engine.last_raw_id
            fields=('event_id','event_text','asset','expiry','event_type','direction','strike_or_threshold','mapping_hash','window_start',
                    'settlement_source','threshold_inclusive','timestamp_wall','pm_yes','opt_yes','gap_pp','relative_gap','spot','spot_timestamp',
                    'pm_bid','pm_ask','pm_timestamp','pm_source','opt_timestamp','source_state','quality_flags','model_confidence','basis_method',
                    'model_inputs','model_version','calculation_version','surface_features','input_refs','raw_id','code_hash','parameter_hash','math','math_version','calculated_at','formation')
            snapshot={k:copy.deepcopy(row.get(k)) for k in fields};snapshot['trade']=trade
            frame=None
            for f in reversed(self.math.engine.store.history(event_id,row['timestamp_wall']-2000,row['timestamp_wall'],20,tail=True)):
                if f['raw_id']<=row['raw_id']:frame=f.get('observation_id',f.get('record_id'));break
            frozen=dict(tracking_id=uuid.uuid4().hex,starred_at=now,target_entry=target,event_id=event_id,event_semantics=copy.deepcopy(event),
                snapshot=snapshot,structure=trade,star_raw_boundary=boundary,opening_frame_id=frame,tracking_version=TRACK_VERSION,
                math_version=VERSION,calculation_version=row.get('calculation_version'),model_version=row.get('model_version'),initial_status='WAITING' if trade else 'MISSED',
                entry_policy='First complete valid exact-contract options receipt at/after target; max wait 10min; no wallet/cash rules',
                reason=None if trade else 'NO_DISPLAYED_STRUCTURE')
            if trade and trade['expiry']<=target:frozen.update(initial_status='MISSED',reason='OPTION_EXPIRY_BEFORE_TARGET')
            self.db.execute('INSERT INTO stars VALUES(?,?,?,?)',(frozen['tracking_id'],now,event_id,encode(frozen)))
            self.stars[frozen['tracking_id']]=dict(frozen=frozen,status=frozen['initial_status'],reason=frozen['reason'],entry=None,mark=None)
            return self.summary(self.stars[frozen['tracking_id']],now)

    def hydrate(self):
        if self.tape is None:self.tape=open_store(self.math.engine.store.path,read_only=True)
        cp=self.tape.checkpoint(before_raw_id=self.cursor)
        if cp is None:raise ValueError('No causal checkpoint for tracker recovery; recorder tape is preserved')
        directory=Path(self.tape.path).parent/'versions'/cp['code_hash']
        if reducer_hash(directory)!=reducer_hash(Path(__file__).parent):raise ValueError('Historical tracker reducer unavailable/changed; no current-code fallback')
        reducer=Engine(self.tape,persist=False);reducer.load_checkpoint(cp['state']);reducer.restoring=True;self.reducer=reducer
        self.pin()
        for record in self.tape.between(cp['raw_id'],self.cursor):self.source(record)

    def pin(self):
        if not self.reducer:return
        events={s['frozen']['event_id']:copy.deepcopy(s['frozen']['event_semantics']) for s in self.stars.values() if s['status'] in ACTIVE}
        self.reducer.events=events;self.reducer.manual=copy.deepcopy(events)
        self.reducer.catalog={k:dict(self.reducer.catalog.get(k,{}),**e) for k,e in events.items()}

    def source(self,r):
        e=self.reducer;e.last_raw_id=r['id'];e.last_received_ms=max(e.last_received_ms,r['received_ms'])
        kind=r['kind'];assets={s['frozen']['snapshot']['asset'] for s in self.stars.values() if s['status'] in ACTIVE}
        if kind=='session':
            e.apply(r);self.pin()
        elif kind=='pm':e.update_pm(r)
        elif kind=='options' and r['subject'] in assets:e.update_options(r)
        elif kind=='history' and r['subject'] in assets:e.update_history(r)
        elif kind=='spot' and r['subject'] in assets:e.apply(r);self.pin()
        elif kind=='catalog':
            for item in r['payload'].get('markets',[]):
                key=str(item.get('id',''))
                if key not in e.catalog:continue
                latest=infer_event(item)
                for field in ('pm_indicative','volume_24h'):e.catalog[key][field]=latest.get(field)
                e.catalog[key].update(catalog_raw_id=r['id'],catalog_received_ms=r['received_ms'])

    def entry(self,s,record,surface,spot,legs):
        frozen=s['frozen'];trade=frozen['structure'];now=record['received_ms'];e=self.reducer
        e.observe(frozen['event_semantics'],record,now);row=copy.deepcopy(e.latest[frozen['event_id']]);math=gap_math(row.get('pm_yes'),row.get('opt_yes'))
        cost=execution_cost(legs,trade['multiplier'])
        if cost['debit']<=0:raise ValueError('NONPOSITIVE_ENTRY_DEBIT')
        info=None;model_reason=None
        try:
            sigma=row.get('model_inputs',{}).get('iv') or row.get('surface_features',{}).get('local_iv')
            paths=conditional_paths(spot['price'],row['strike_or_threshold'],sigma,(row['expiry']-now)/YEAR_MS,(trade['expiry']-now)/YEAR_MS,row['direction'],row['event_type'])
            if min(paths['hit_effective_paths'],paths['no_hit_effective_paths'])<32:raise ValueError('INSUFFICIENT_CONDITIONAL_PATH_SAMPLE')
            info=conditional_ev(row['pm_yes'],dict(trade,legs=legs,**cost),paths,row['opt_yes'])
        except (ValueError,TypeError,KeyError) as error:model_reason=str(error)
        gap_star=frozen['snapshot']['math']['gap_pp'];gap_entry=math['gap_pp']
        data=dict(timestamp_ms=now,raw_id=record['id'],target_entry=frozen['target_entry'],actual_entry_receipt=surface['received_ms'],
            entry_delay_after_target_ms=surface['received_ms']-frozen['target_entry'],pm_entry=row.get('pm_yes'),q_entry=row.get('opt_yes'),gap_entry_pp=gap_entry,
            log_odds_entry=math['log_odds_gap'],spot_entry=spot['price'],input_refs=row.get('input_refs'),legs=legs,**cost,
            gap_capture_remaining=gap_entry/gap_star if gap_entry is not None and gap_star is not None and abs(gap_star)>=.1 else None,
            pre_entry_repricing_pp=gap_star-gap_entry if gap_star is not None and gap_entry is not None else None,
            math_version=VERSION,code_hash=e.code_hash,model_inputs=row.get('model_inputs'),modeled_payoff=info,model_reason=model_reason,
            quote_quality=legs[0].get('quote_quality'),option_source_ms=surface.get('source_ms'),event_version=frozen['event_semantics'].get('mapping_hash'))
        self.append(frozen['tracking_id'],'ENTRY',data)

    def mark(self,s,record,legs):
        t=s['frozen']['structure'];cost=liquidation(legs,t['multiplier']);entry=s['entry']['debit'];pnl=cost['liquidation_credit']-entry
        self.append(s['frozen']['tracking_id'],'MARK',dict(timestamp_ms=record['received_ms'],raw_id=record['id'],
            liquidation_credit=cost['liquidation_credit'],pnl_1x=pnl,return_fraction=pnl/entry,pnl_per_100=100*pnl/entry,
            mid_mark_pnl=cost['mid_value']-entry,exit_fees=cost['fees'],exit_slippage=cost['slippage'],exit_spread=cost['spread_cost'],
            bid_ask=[dict(instrument=l['instrument'],bid=l['bid'],ask=l['ask']) for l in legs]))

    def end_expiry(self,s,now):
        f=s['frozen'];t=f['structure'];expiry=t['expiry'];spot=self.reducer.spots.get(t['asset']) if self.reducer else None
        limit=120000 if t['multiplier']==100 else 45000;value=None
        if spot and spot.get('source_ms') is not None and spot.get('received_ms',expiry+1)<=expiry and 0<=expiry-spot['source_ms']<=limit:
            import numpy as np
            value=float(payoff(t,np.array([spot['price']]))[0])
        final=None
        if value is not None and s['entry']:
            pnl=value-s['entry']['debit'];final=dict(liquidation_credit=value,pnl_1x=pnl,return_fraction=pnl/s['entry']['debit'],pnl_per_100=100*pnl/s['entry']['debit'],timestamp_ms=expiry,raw_id=spot['raw_id'])
        self.append(f['tracking_id'],'ENDED',dict(timestamp_ms=expiry,raw_id=spot.get('raw_id') if spot else None,
            reason='EXPIRY_RECORDED_SPOT_PAYOFF_PROXY' if final is not None else 'SETTLEMENT_MISSING',final=final,
            settlement='Causal recorded spot proxy; not official exercise/assignment or exchange settlement'))
        if final is not None:s['mark']=final

    def consume(self,record):
        now=record['received_ms']
        for s in list(self.stars.values()):
            if s['status']=='LIVE' and now>=s['frozen']['structure']['expiry']:self.end_expiry(s,now)
        previous=self.reducer.last_received_ms
        if now<previous:return  # Clock-regressed packet cannot create a valid post-target receipt.
        self.source(record)
        if record['kind']!='options':return
        for s in list(self.stars.values()):
            if s['status'] not in ACTIVE:continue
            f=s['frozen'];t=f['structure']
            if not t or t['asset']!=record['subject'] or now<f['starred_at']:continue
            if s['status']=='WAITING' and now<f['target_entry']:continue
            if s['status']=='WAITING' and now>f['target_entry']+ENTRY_WAIT_MS:
                self.append(f['tracking_id'],'MISSED',dict(timestamp_ms=now,raw_id=record['id'],reason='NO_VALID_POST_TARGET_QUOTE: '+(s.get('reason') or 'no complete snapshot')));continue
            if s['status']=='WAITING' and now>=t['expiry']:
                self.append(f['tracking_id'],'MISSED',dict(timestamp_ms=now,raw_id=record['id'],reason='OPTION_EXPIRED_BEFORE_VALID_ENTRY'));continue
            surface=self.reducer.surfaces.get((t['asset'],t['expiry']));spot=self.reducer.spots.get(t['asset'])
            try:
                if not surface or surface['raw_id']!=record['id']:raise ValueError('NO_NEW_COMPLETE_SURFACE')
                legs=exact_quotes(t,surface,spot,now)
                if s['status']=='WAITING':self.entry(s,record,surface,spot,legs)
                self.mark(s,record,legs)
            except (ValueError,KeyError,TypeError) as error:
                reason=str(error)
                if reason!=s.get('reason'):self.append(f['tracking_id'],'WAIT_REASON',dict(timestamp_ms=now,raw_id=record['id'],reason=reason))

    def process(self):
        with self.lock:
            active=any(s['status'] in ACTIVE for s in self.stars.values())
            if self.tape is None:self.tape=open_store(self.math.engine.store.path,read_only=True)
            with self.math.engine.lock:boundary=self.math.engine.last_raw_id
            # Legacy readers intentionally freeze their prefix. This live follower
            # extends only to the writer's committed boundary, never future rows.
            if hasattr(self.tape,'limits'):self.tape.limits['raw']=boundary
            if active and self.reducer is None:self.hydrate()
            records=list(islice(self.tape.between(self.cursor,boundary),512)) if active else []
            try:
                self.db.execute('BEGIN IMMEDIATE')
                if not active:self.cursor=self.math.engine.last_raw_id;self.reducer=None
                for record in records:self.consume(record);self.cursor=record['id']
                self.db.execute('UPDATE cursor SET raw_id=? WHERE id=1',(self.cursor,))
                now=time.time_ns()//1000000
                # Timeouts run only after catching up to the recorded prefix, never ahead of unseen quotes.
                if not records or self.cursor>=boundary:
                    for s in list(self.stars.values()):
                        if s['status']=='WAITING' and now>s['frozen']['target_entry']+ENTRY_WAIT_MS:
                            self.append(s['frozen']['tracking_id'],'MISSED',dict(timestamp_ms=now,reason='NO_VALID_POST_TARGET_QUOTE: '+(s.get('reason') or 'no received options snapshot')))
                        if s['status']=='LIVE' and now>=s['frozen']['structure']['expiry']:self.end_expiry(s,now)
                self.db.execute('COMMIT')
            except Exception:
                self.db.execute('ROLLBACK');self.reload();self.reducer=None;raise

    def finish(self,identity,now=None):
        now=now if now is not None else time.time_ns()//1000000
        with self.lock:
            s=self.stars.get(identity)
            if not s:raise ValueError('No such starred signal')
            if s['status'] not in ACTIVE:return self.summary(s,now)
            row=self.db.execute("SELECT data FROM events WHERE tracking_id=? AND kind='MARK' AND timestamp_ms<=? ORDER BY id DESC LIMIT 1",(identity,now)).fetchone()
            mark=decode(row[0]) if row else None
            self.append(identity,'ENDED',dict(timestamp_ms=now,reason='OPERATOR_STOPPED',final=mark,
                mark_quality=self.quote_state(s,now),last_executable_mark_ms=mark['timestamp_ms'] if mark else None))
            return self.summary(s,now)

    def quote_state(self,s,now):
        t=s['frozen']['structure'];m=s.get('mark')
        if not m:return 'NO_EXECUTABLE_MARK'
        if t['multiplier']==100 and regular_session(now)['market_state']!='OPEN':return 'MARKET_CLOSED_LAST_MARK'
        if now-m['timestamp_ms']>(120000 if t['multiplier']==100 else 45000):return 'STALE_LAST_MARK'
        return 'VALID_RECEIVED_BOOK_MARK'

    def summary(self,s,now=None):
        now=now if now is not None else time.time_ns()//1000000;f=s['frozen'];r=f['snapshot'];e=s['entry'];m=s['mark'];t=f['structure']
        return dict(tracking_id=f['tracking_id'],status=s['status'],reason=s.get('reason'),event_id=f['event_id'],asset=r['asset'],event_text=r['event_text'],
            starred_at=f['starred_at'],star_gap_pp=r['math']['gap_pp'],provenance=(r.get('formation') or {}).get('provenance'),
            structure=t['kind'] if t else None,legs=[dict(instrument=l['instrument'],side=l['side'],strike=l['strike']) for l in t['legs']] if t else [],
            target_entry=f['target_entry'],actual_entry=e['actual_entry_receipt'] if e else None,entry_gap_pp=e['gap_entry_pp'] if e else None,
            entry_debit=e['debit'] if e else None,liquidation=m['liquidation_credit'] if m else None,pnl_1x=m['pnl_1x'] if m else None,
            return_fraction=m['return_fraction'] if m else None,pnl_per_100=m['pnl_per_100'] if m else None,last_mark=m['timestamp_ms'] if m else None,
            quote_state=self.quote_state(s,now),ended_at=s.get('ended_at'),age_ms=now-f['starred_at'])

    def snapshot(self,identity=None):
        with self.lock:
            if identity:
                s=self.stars.get(identity)
                if not s:raise ValueError('No such starred signal')
                series=[{k:d.get(k) for k in ('timestamp_ms','pnl_1x','return_fraction','raw_id')} for r in self.db.execute("SELECT data FROM events WHERE tracking_id=? AND kind='MARK' ORDER BY id DESC LIMIT 1200",(identity,)) for d in [decode(r[0])]][::-1]
                return dict(frozen=copy.deepcopy(s['frozen']),entry=copy.deepcopy(s['entry']),mark=copy.deepcopy(s['mark']),end=copy.deepcopy(s.get('end')),
                    summary=self.summary(s),series=series,series_limit=1200,error=self.error)
            return dict(rows=[self.summary(s) for s in reversed(list(self.stars.values()))],error=self.error,cursor_raw_id=self.cursor)

    def run(self):
        while not self.stop_event.is_set():
            try:self.process();self.error=None
            except Exception as error:self.error=str(error)
            self.stop_event.wait(.5)

    def start(self):
        self.thread=threading.Thread(target=self.run,name='basis-star-tracking',daemon=True);self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread:self.thread.join(timeout=10)
        if self.tape:self.tape.close()
        self.db.close()
