"""Compact, segmented, immutable research tape; v1 is a read-only predecessor.

The public path remains the operator's DB alias. A small sidecar pointer opts
it into v2, leaving paper wallets, acceptance evidence and code archives in
their original homes. Representation and sampling never enter the reducer.
"""
from collections import OrderedDict
from contextlib import contextmanager
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import threading
import time
import zlib
from .store import Store, encode, decode
from .storage_codec import SourceCodec, checkpoint_pack, checkpoint_unpack

SEMANTICS = ('event_id','event_text','asset','expiry','event_type','direction','strike_or_threshold',
    'window_start','window_start_assumption','settlement_source','threshold_inclusive',
    'mapping_origin','mapping_hash','mapping_reason','event_url')
HOT = ('timestamp_wall','pm_yes','pm_bid','pm_ask','opt_yes','spot','gap_pp','relative_gap',
       'pm_timestamp','opt_timestamp','options_received_ms','spot_timestamp')
REFS = ('catalog','mapping','pm','options','spot','history')
TABLES = ('raw','observations','episodes','analyzers','checkpoints')
PHYSICAL = dict(raw='records',observations='frames',episodes='episodes',analyzers='analyzers',checkpoints='checkpoints')


def atomic_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.tmp')
    with temporary.open('w') as f:
        f.write(encode(value)+'\n');f.flush();os.fsync(f.fileno())
    os.replace(temporary,path)
    fd=os.open(path.parent,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)


class LegacyReader(Store):
    """No schema statements, pragmas that write, or fallback to a v1 writer."""
    def __init__(self,path,limits=None,immutable=False):
        self.path=str(path);self.lock=threading.RLock();self.checkpoint_errors=[]
        self.immutable=immutable
        self.db=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro'+('&immutable=1' if immutable else ''),uri=True,timeout=5,
                                check_same_thread=False,isolation_level=None)
        self.db.row_factory=sqlite3.Row
        self.limits=limits or {t:self.db.execute(f'SELECT COALESCE(MAX(id),0) FROM {t}').fetchone()[0] for t in TABLES}

    def raw(self,after=0,until=None):
        yield from self.between(after,self.limits['raw'],until)

    def between(self,after,end,until=None):
        end=min(end,self.limits['raw'])
        while after<end:
            with self.lock:
                rows=self.db.execute('SELECT * FROM raw WHERE id>? AND id<=? ORDER BY id LIMIT 256',(after,end)).fetchall()
            if not rows:return
            for row in rows:
                item=dict(row);body=zlib.decompress(item['payload'])
                if hashlib.sha256(body).hexdigest()!=item['sha256']:raise ValueError(f'Raw hash mismatch at {item["id"]}')
                item['payload']=json.loads(body);after=item['id']
                if until is None or item['received_ms']<=until:yield item

    def raw_record(self,raw_id):
        if raw_id>self.limits['raw']:return None
        return next(self.between(raw_id-1,raw_id),None)

    def history(self,event_id=None,start=0,end=2**63-1,limit=500,after_id=0,tail=False,before_id=2**63-1):
        return super().history(event_id,start,end,limit,after_id,tail,min(before_id,self.limits['observations']+1))

    def latest(self,table,key,limit=200,event_id=None):
        if (table,key) not in {('episodes','gap_event_id'),('analyzers','scope,algo_name'),('observations','event_id')}:
            raise ValueError('Unsupported table')
        field='scope' if table=='analyzers' else 'event_id';where='id<=?';args=[self.limits[table]]
        outer='';outer_args=[]
        if table=='episodes' and event_id is not None:
            # v1 has no event_id index. Its immutable episode IDs encode the
            # event prefix. Group the covering key range, then check the exact
            # event on those latest rows (also handles hyphenated-ID overlap).
            prefix='gap-'+event_id+'-'
            where+=' AND gap_event_id>=? AND gap_event_id<?';args.extend((prefix,prefix[:-1]+'.'))
            outer=' AND event_id=?';outer_args=[event_id]
        elif event_id is not None:where+=' AND '+field+'=?';args.append(event_id)
        index={'episodes':'episode_key','analyzers':'analyzer_key','observations':'obs_event'}[table]
        with self.reader() as db:
            rows=db.execute(f'SELECT id,data FROM {table} WHERE id IN (SELECT MAX(id) FROM {table} INDEXED BY {index} WHERE {where} GROUP BY {key}){outer} ORDER BY id DESC LIMIT ?',(*args,*outer_args,limit)).fetchall()
        return [dict(decode(r['data']),record_id=r['id']) for r in rows]

    def checkpoint(self,revision=None,before_raw_id=None):
        return super().checkpoint(revision,min(self.limits['raw'],before_raw_id if before_raw_id is not None else self.limits['raw']))

    def stats(self):
        first=self.db.execute('SELECT received_ms FROM raw ORDER BY id LIMIT 1').fetchone()
        last=self.db.execute('SELECT received_ms FROM raw WHERE id=?',(self.limits['raw'],)).fetchone()
        return dict(path=self.path,**{k:self.limits[k] for k in TABLES if k!='checkpoints'},
                    first_received_ms=first[0] if first else None,last_received_ms=last[0] if last else None)


class Segment:
    def __init__(self,path,base,write=False,immutable=False):
        self.path=Path(path);self.write=write;self.base=dict(base)
        self.db=sqlite3.connect(str(path) if write else self.path.resolve().as_uri()+'?mode=ro'+('&immutable=1' if immutable else ''),
            uri=not write,timeout=10,check_same_thread=False,isolation_level=None)
        self.db.row_factory=sqlite3.Row
        if write:
            self.db.execute('PRAGMA journal_mode=WAL');self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('PRAGMA foreign_keys=ON')
            self.db.executescript('''
              CREATE TABLE IF NOT EXISTS blobs(id INTEGER PRIMARY KEY,sha256 BLOB UNIQUE NOT NULL,data BLOB NOT NULL);
              CREATE TABLE IF NOT EXISTS streams(id INTEGER PRIMARY KEY,source TEXT,kind TEXT,subject TEXT,session TEXT,
                UNIQUE(source,kind,subject,session));
              CREATE TABLE IF NOT EXISTS records(id INTEGER PRIMARY KEY,received_ms INTEGER NOT NULL,monotonic_ns INTEGER NOT NULL,
                stream_id INTEGER NOT NULL REFERENCES streams(id),source_ms INTEGER,encoding INTEGER NOT NULL,
                base_id INTEGER,chain_depth INTEGER NOT NULL,data BLOB NOT NULL,sha256 BLOB NOT NULL);
              CREATE INDEX IF NOT EXISTS record_stream ON records(stream_id,id);
              CREATE INDEX IF NOT EXISTS record_time ON records(received_ms,id);
              CREATE TABLE IF NOT EXISTS event_versions(id INTEGER PRIMARY KEY,event_id TEXT NOT NULL,sha256 BLOB UNIQUE NOT NULL,data BLOB NOT NULL);
              CREATE INDEX IF NOT EXISTS event_identity ON event_versions(event_id,id);
              CREATE TABLE IF NOT EXISTS frames(id INTEGER PRIMARY KEY,raw_id INTEGER NOT NULL REFERENCES records(id),
                event_version_id INTEGER NOT NULL REFERENCES event_versions(id),meta_id INTEGER NOT NULL REFERENCES blobs(id),
                model_id INTEGER NOT NULL REFERENCES blobs(id),context_id INTEGER REFERENCES blobs(id),depth_id INTEGER REFERENCES blobs(id),
                features BLOB NOT NULL,input_refs BLOB NOT NULL,
                timestamp_wall INTEGER NOT NULL,pm_yes REAL,pm_bid REAL,pm_ask REAL,opt_yes REAL,spot REAL,gap_pp REAL,relative_gap REAL,
                pm_timestamp INTEGER,opt_timestamp INTEGER,options_received_ms INTEGER,spot_timestamp INTEGER);
              CREATE INDEX IF NOT EXISTS frame_event ON frames(event_version_id,timestamp_wall,id);
              CREATE INDEX IF NOT EXISTS frame_raw ON frames(raw_id,id);
              CREATE TABLE IF NOT EXISTS salient(id INTEGER PRIMARY KEY,raw_id INTEGER NOT NULL REFERENCES records(id),
                timestamp_ms INTEGER NOT NULL,scope TEXT NOT NULL,kind TEXT NOT NULL,before_value BLOB,after_value BLOB,
                previous_raw_id INTEGER,source_ms INTEGER);
              CREATE INDEX IF NOT EXISTS salient_scope ON salient(scope,timestamp_ms,id);
              CREATE TABLE IF NOT EXISTS episodes(id INTEGER PRIMARY KEY,raw_id INTEGER NOT NULL REFERENCES records(id),
                gap_event_id TEXT NOT NULL,event_id TEXT NOT NULL,timestamp_ms INTEGER NOT NULL,data BLOB NOT NULL);
              CREATE INDEX IF NOT EXISTS episode_key ON episodes(gap_event_id,id);
              CREATE TABLE IF NOT EXISTS analyzers(id INTEGER PRIMARY KEY,raw_id INTEGER NOT NULL REFERENCES records(id),
                timestamp_ms INTEGER NOT NULL,algo_name TEXT NOT NULL,scope TEXT NOT NULL,version TEXT NOT NULL,data BLOB NOT NULL);
              CREATE INDEX IF NOT EXISTS analyzer_key ON analyzers(scope,algo_name,id);
              CREATE TABLE IF NOT EXISTS checkpoints(id INTEGER PRIMARY KEY,raw_id INTEGER NOT NULL,code_hash TEXT NOT NULL,sha256 BLOB NOT NULL,data BLOB NOT NULL);
              CREATE INDEX IF NOT EXISTS checkpoint_version ON checkpoints(code_hash,id);
              CREATE TABLE IF NOT EXISTS seal(id INTEGER PRIMARY KEY CHECK(id=1),data TEXT NOT NULL);
              PRAGMA user_version=2;
            ''')
            for table in ('blobs','streams','records','event_versions','frames','salient','episodes','analyzers','checkpoints','seal'):
                for operation in ('UPDATE','DELETE'):
                    self.db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{operation} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'immutable v2 tape'); END")
        self.ids={k:max(base[k],self.db.execute(f'SELECT COALESCE(MAX(id),0) FROM {v}').fetchone()[0]) for k,v in PHYSICAL.items()}
        self.blob_ids=OrderedDict();self.blob_values=OrderedDict();self.streams={};self.events={}
        self.codec=SourceCodec(self.put,self.get)
        self.vectors=OrderedDict()

    def put(self,value):
        body=encode(value).encode();sha=hashlib.sha256(body).digest()
        cached=self.blob_ids.get(sha)
        if cached is not None:self.blob_ids.move_to_end(sha);return cached
        row=self.db.execute('SELECT id FROM blobs WHERE sha256=?',(sha,)).fetchone()
        ref=row[0] if row else self.db.execute('INSERT INTO blobs(sha256,data) VALUES(?,?)',(sha,zlib.compress(body,6))).lastrowid
        self.blob_ids[sha]=ref
        if len(self.blob_ids)>4096:self.blob_ids.popitem(last=False)
        if len(body)<8192:
            self.blob_values[ref]=json.loads(body)
            if len(self.blob_values)>4096:self.blob_values.popitem(last=False)
        return ref

    def get(self,ref):
        if ref in self.blob_values:
            self.blob_values.move_to_end(ref);return copy.deepcopy(self.blob_values[ref])
        row=self.db.execute('SELECT sha256,data FROM blobs WHERE id=?',(ref,)).fetchone()
        if row is None:raise ValueError(f'Missing content reference {self.path.name}:{ref}')
        body=zlib.decompress(row['data'])
        if hashlib.sha256(body).digest()!=row['sha256']:raise ValueError('Content hash mismatch')
        value=json.loads(body)
        if len(body)<8192:
            self.blob_values[ref]=value
            if len(self.blob_values)>4096:self.blob_values.popitem(last=False)
        return copy.deepcopy(value)

    def stream(self,record):
        key=tuple(record[k] for k in ('source','kind','subject','session'))
        if key not in self.streams:
            self.db.execute('INSERT OR IGNORE INTO streams(source,kind,subject,session) VALUES(?,?,?,?)',key)
            self.streams[key]=self.db.execute('SELECT id FROM streams WHERE source=? AND kind=? AND subject=? AND session=?',key).fetchone()[0]
        return self.streams[key]

    def append_raw(self,record):
        if record['id']!=self.ids['raw']+1:raise ValueError('Raw global sequence must be contiguous')
        # Distinct PM markets get short independent encoding chains, but their
        # logical stream, IDs, ordering and provider fields remain untouched.
        payload=record['payload'];group=payload.get('market') if isinstance(payload,dict) and record['kind']=='pm' else None
        if not isinstance(group,str):group=None
        codec,base,depth,data=self.codec.pack((self.stream(record),group),record['id'],record['received_ms'],payload)
        sha=hashlib.sha256(encode(payload).encode()).digest()
        self.db.execute('INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?,?)',(record['id'],record['received_ms'],record['monotonic_ns'],self.stream(record),record['source_ms'],codec,base,depth,data,sha))
        self.ids['raw']=record['id']
        return dict(record,sha256=sha.hex())

    def raw_record(self,raw_id,level=0):
        if level>SourceCodec.max_chain:raise ValueError('Source reconstruction chain exceeds bound')
        row=self.db.execute('SELECT r.*,s.source,s.kind,s.subject,s.session FROM records r JOIN streams s ON s.id=r.stream_id WHERE r.id=?',(raw_id,)).fetchone()
        if row is None:return None
        if (row['encoding']==0)!=(row['base_id'] is None) or not 0<=row['chain_depth']<=SourceCodec.max_chain:
            raise ValueError('Invalid source anchor metadata')
        if raw_id in self.vectors:value=self.vectors[raw_id]
        else:
            base=row['base_id']
            if base is not None:
                if base>=raw_id:raise ValueError('Future source dependency')
                previous=self.raw_record(base,level+1)
                if previous is None:raise ValueError('Source anchor is outside its segment')
            value=SourceCodec.unpack(row['encoding'],row['data'],self.vectors[base] if base is not None else None)
            self.vectors[raw_id]=value
            if len(self.vectors)>256:self.vectors.popitem(last=False)
        payload=self.codec.logical(value);sha=hashlib.sha256(encode(payload).encode()).digest()
        if sha!=row['sha256']:raise ValueError(f'Logical source hash mismatch at {raw_id}')
        return dict(id=raw_id,received_ms=row['received_ms'],monotonic_ns=row['monotonic_ns'],source=row['source'],kind=row['kind'],subject=row['subject'],source_ms=row['source_ms'],session=row['session'],payload=payload,sha256=sha.hex())

    def between(self,after,end,until=None):
        while after<end:
            ids=[r[0] for r in self.db.execute('SELECT id FROM records WHERE id>? AND id<=? ORDER BY id LIMIT 256',(after,end))]
            if not ids:return
            for raw_id in ids:
                record=self.raw_record(raw_id);after=raw_id
                if until is None or record['received_ms']<=until:yield record

    def semantic(self,data):
        value={k:data.get(k) for k in SEMANTICS};body=encode(value).encode();sha=hashlib.sha256(body).digest()
        if sha not in self.events:
            row=self.db.execute('SELECT id FROM event_versions WHERE sha256=?',(sha,)).fetchone()
            self.events[sha]=row[0] if row else self.db.execute('INSERT INTO event_versions(event_id,sha256,data) VALUES(?,?,?)',(data['event_id'],sha,zlib.compress(body,6))).lastrowid
        return self.events[sha]

    def append_frame(self,raw_id,data):
        if data['raw_id']!=raw_id or any(v>raw_id for v in data['input_refs'].values()):raise ValueError('Frame has future source references')
        model=dict(data.get('model_inputs',{}));has_spot='spot' in model;model.pop('spot',None)
        special=set(SEMANTICS)|set(HOT)|{'model_inputs','features','input_refs','pm_depth','underlying_context',
                                     'raw_id','timestamp_received','timestamp_monotonic','trigger_kind','trigger_source'}
        extra={k:v for k,v in data.items() if k not in special}
        extra['_model_spot_present']=has_spot
        extra['_context_present']='underlying_context' in data
        self.ids['observations']+=1
        values=(self.ids['observations'],raw_id,self.semantic(data),self.put(extra),self.put(model),
            self.put(data['underlying_context']) if 'underlying_context' in data else None,
            self.put(data['pm_depth']) if data.get('pm_depth') is not None else None,
            zlib.compress(encode(self.codec.vector(data['features'])).encode(),6),
            encode([data['input_refs'].get(k) for k in REFS]),*(data.get(k) for k in HOT))
        self.db.execute('INSERT INTO frames VALUES('+','.join('?' for _ in values)+')',values)
        return self.ids['observations']

    def frame(self,row):
        semantic=self.db.execute('SELECT data FROM event_versions WHERE id=?',(row['event_version_id'],)).fetchone()
        data=decode(semantic[0]);extra=dict(self.get(row['meta_id']))
        has_spot=extra.pop('_model_spot_present');has_context=extra.pop('_context_present')
        data.update(extra);data.update({k:row[k] for k in HOT});data['model_inputs']=dict(self.get(row['model_id']))
        if has_spot:data['model_inputs']['spot']=data['spot']
        if has_context:data['underlying_context']=self.get(row['context_id'])
        data['pm_depth']=self.get(row['depth_id']) if row['depth_id'] is not None else None
        data['features']=self.codec.logical(decode(row['features']))
        data['input_refs']={k:v for k,v in zip(REFS,json.loads(row['input_refs'])) if v is not None}
        raw=self.db.execute('SELECT received_ms,monotonic_ns,s.source,s.kind FROM records r JOIN streams s ON s.id=r.stream_id WHERE r.id=?',(row['raw_id'],)).fetchone()
        data.update(raw_id=row['raw_id'],timestamp_received=raw['received_ms'],timestamp_monotonic=raw['monotonic_ns'],trigger_kind=raw['kind'],trigger_source=raw['source'])
        return data

    def history(self,event_id,start,end,limit,after,tail,before):
        where='f.timestamp_wall BETWEEN ? AND ? AND f.id>? AND f.id<?';args=[start,end,after,before]
        if event_id is not None:where+=' AND e.event_id=?';args.append(event_id)
        order='DESC' if tail else 'ASC'
        rows=self.db.execute(f'SELECT f.* FROM frames f JOIN event_versions e ON e.id=f.event_version_id WHERE {where} ORDER BY f.id {order} LIMIT ?',(*args,limit)).fetchall()
        return [dict(self.frame(r),observation_id=r['id']) for r in (reversed(rows) if tail else rows)]

    def append_checkpoint(self,raw_id,revision,state):
        if raw_id!=state['last_raw_id'] or raw_id>self.ids['raw']:raise ValueError('Checkpoint boundary is not causal')
        packed=checkpoint_pack(state,self.put);self.ids['checkpoints']+=1
        self.db.execute('INSERT INTO checkpoints VALUES(?,?,?,?,?)',(self.ids['checkpoints'],raw_id,revision,hashlib.sha256(encode(state).encode()).digest(),zlib.compress(encode(packed).encode(),6)))

    def checkpoint(self,revision,before,errors):
        rows=self.db.execute('SELECT * FROM checkpoints WHERE (? IS NULL OR code_hash=?) AND raw_id<=? ORDER BY id DESC LIMIT 10',(revision,revision,before)).fetchall()
        for row in rows:
            try:
                state=checkpoint_unpack(decode(row['data']),self.get)
                if state['last_raw_id']!=row['raw_id'] or hashlib.sha256(encode(state).encode()).digest()!=row['sha256']:raise ValueError('Checkpoint hash/boundary mismatch')
                return dict(raw_id=row['raw_id'],code_hash=row['code_hash'],state=state)
            except (ValueError,zlib.error,KeyError,TypeError) as error:errors.append(dict(id=row['id'],error=str(error)))

    def latest(self,table,key,limit,event_id):
        if table=='observations':
            rows=self.db.execute('SELECT f.* FROM frames f WHERE f.id IN (SELECT MAX(f.id) FROM frames f JOIN event_versions e ON e.id=f.event_version_id WHERE (? IS NULL OR e.event_id=?) GROUP BY e.event_id) ORDER BY f.id DESC LIMIT ?',(event_id,event_id,limit)).fetchall()
            return [dict(self.frame(r),record_id=r['id']) for r in rows]
        field='scope' if table=='analyzers' else 'event_id'
        rows=self.db.execute(f'SELECT id,data FROM {table} WHERE id IN (SELECT MAX(id) FROM {table} WHERE (? IS NULL OR {field}=?) GROUP BY {key}) ORDER BY id DESC LIMIT ?',(event_id,event_id,limit)).fetchall()
        return [dict(decode(r['data']),record_id=r['id']) for r in rows]

    def bytes(self):return self.db.execute('PRAGMA page_count').fetchone()[0]*self.db.execute('PRAGMA page_size').fetchone()[0]

    def close(self):self.db.close()


class SegmentedTape:
    frame_ms=1000

    def __init__(self,manifest,write=False):
        self.manifest_path=Path(manifest).resolve();self.root=self.manifest_path.parent
        self.manifest=json.loads(self.manifest_path.read_text());self.write=write
        self.path=self.manifest['alias'];self.lock=threading.RLock();self.checkpoint_errors=[]
        self.legacy=LegacyReader(self.manifest['legacy']['path'],self.manifest['legacy']['limits'],self.manifest['legacy'].get('frozen',False)) if self.manifest.get('legacy') else None
        self.handles={};self.active=None;self.last_rotation_check=0
        self._frames={};self._previous={};self._episodes={};self._health={};self.params={}
        if write:
            item=self.manifest['segments'][-1]
            if not item.get('closed') and item.get('sealing'):
                self._finish_close(item,item['sealing']['reason']);self._new_segment()
            else:self._open_active()
        self.counts=self.stats_counts();self.boundary=self.counts['raw']

    @property
    def db(self):
        if self.active is None:raise RuntimeError('Use the tape interface for segmented reads')
        return self.active.db

    def _open_active(self):
        item=self.manifest['segments'][-1]
        if item.get('closed'):
            if self.write:self._new_segment()
            return
        self.active=Segment(self.root/item['file'],item['base'],True)
        self.handles[item['id']]=self.active

    def _new_segment(self):
        base=self.stats_counts();identity=len(self.manifest['segments'])+1
        name=f'segment-{identity:06}.sqlite3'
        path=self.root/name
        if path.exists():
            probe=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
            nonempty=probe.execute('SELECT COUNT(*) FROM records').fetchone()[0];probe.close()
            if nonempty:raise RuntimeError('Unmanifested nonempty segment; refuse to overwrite evidence')
        self.active=Segment(path,base,True)
        item=dict(id=identity,file=name,base=base,closed=False,storage_version=2)
        self.manifest['segments'].append(item);self.handles[identity]=self.active
        atomic_json(self.manifest_path,self.manifest)
        self._frames.clear();self._episodes.clear()

    def _handle(self,item):
        if item['id'] not in self.handles:self.handles[item['id']]=Segment(self.root/item['file'],item['base'],immutable=item.get('closed',False) or bool(item.get('sealing')))
        return self.handles[item['id']]

    def _refresh(self):
        if not self.write:
            self.manifest=json.loads(self.manifest_path.read_text())
            for item in self.manifest['segments']:
                if item['id'] in self.handles:
                    handle=self.handles[item['id']]
                    handle.ids=dict(item['limits']) if item.get('closed') else {k:max(handle.base[k],handle.db.execute(f'SELECT COALESCE(MAX(id),0) FROM {v}').fetchone()[0]) for k,v in PHYSICAL.items()}

    def _finish_close(self,item,reason):
        segment=self._handle(item)
        limits=dict(segment.ids);bounds=segment.db.execute('SELECT MIN(received_ms),MAX(received_ms),MIN(id),MAX(id) FROM records').fetchone()
        versions={r[0] for r in segment.db.execute('SELECT DISTINCT code_hash FROM checkpoints')}
        for row in segment.db.execute('SELECT DISTINCT meta_id FROM frames'):
            value=segment.get(row[0]).get('code_hash')
            if value:versions.add(value)
        segment.close();self.handles.pop(item['id'],None);self.active=None
        path=self.root/item['file'];started=time.monotonic()
        wal=Path(str(path)+'-wal')
        if wal.exists() and wal.stat().st_size:raise RuntimeError('Sealing interrupted with a nonempty WAL; preserve files for explicit recovery')
        db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro&immutable=1',uri=True)
        messages=[r[0] for r in db.execute('PRAGMA quick_check')];db.close()
        if messages!=['ok']:raise RuntimeError('Closed segment integrity failed: '+str(messages))
        sha=hashlib.sha256()
        with path.open('rb') as f:
            for block in iter(lambda:f.read(1024*1024),b''):sha.update(block)
        path.chmod(0o444);stat=path.stat()
        item.update(closed=True,limits=limits,start_ms=bounds[0],end_ms=bounds[1],first_raw=bounds[2],last_raw=bounds[3],
            reducer_versions=sorted(versions),close_reason=reason,sha256=sha.hexdigest(),bytes=stat.st_size,mtime_ns=stat.st_mtime_ns,
            integrity=dict(result='ok',timestamp_ms=time.time_ns()//1000000,seconds=time.monotonic()-started,messages=messages))
        atomic_json(self.manifest_path,self.manifest)
        return True

    def rotate(self,reason='operator'):
        with self.lock:
            if not self.write:raise RuntimeError('Read-only tape')
            segment=self.active
            # Never write a seal row after checkpointing: that would itself
            # create an uncheckpointed WAL. A durable manifest intent records
            # closure. Busy readers postpone rotation without stopping sources.
            segment.db.execute('PRAGMA busy_timeout=0')
            try:busy,_,_=segment.db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
            finally:segment.db.execute('PRAGMA busy_timeout=10000')
            if busy:return False
            self.manifest['segments'][-1]['sealing']=dict(reason=reason,limits=dict(segment.ids))
            atomic_json(self.manifest_path,self.manifest)
            self._finish_close(self.manifest['segments'][-1],reason);self._new_segment()
            return True

    @contextmanager
    def transaction(self):
        with self.lock:
            if not self.write:raise RuntimeError('Read-only research tape')
            self.active.db.execute('BEGIN IMMEDIATE')
            try:
                yield
                self.active.db.execute('COMMIT')
            except BaseException:
                if self.active.db.in_transaction:self.active.db.execute('ROLLBACK')
                self.active.blob_ids.clear();self.active.blob_values.clear();self.active.codec.previous.clear()
                self.active.streams.clear();self.active.events.clear()
                self.active.ids={k:max(self.active.base[k],self.active.db.execute(f'SELECT COALESCE(MAX(id),0) FROM {v}').fetchone()[0]) for k,v in PHYSICAL.items()}
                self._frames.clear();self._previous.clear();self._episodes.clear()
                raise
            now=time.monotonic()
            if now-self.last_rotation_check>=60:
                self.last_rotation_check=now
                row=self.active.db.execute('SELECT MIN(received_ms),MAX(received_ms) FROM records').fetchone()
                if self.active.bytes()>=self.manifest['segment_max_bytes']:self.rotate('size')
                elif row[0] and row[1]//86400000>row[0]//86400000:self.rotate('UTC-day')

    def resume(self,engine):
        self.params=dict(vars(engine.config));self._previous={k:dict(v) for k,v in engine.latest.items()}
        self._health={k:v.get('state') for k,v in engine.health.items()}

    def append_raw(self,source,kind,subject,payload,session,received_ms=None,source_ms=None,monotonic_ns=None):
        raw_id=self.active.ids['raw']+1
        record=dict(id=raw_id,source=source,kind=kind,subject=subject,payload=payload,session=session,
            received_ms=received_ms if received_ms is not None else time.time_ns()//1000000,
            monotonic_ns=monotonic_ns if monotonic_ns is not None else time.monotonic_ns(),source_ms=source_ms)
        record=self.active.append_raw(record);self.boundary=raw_id
        if kind=='session':self.params=payload.get('config',self.params)
        if kind=='health':
            before=self._health.get(subject);after=payload.get('state')
            if before!=after:self._salient(raw_id,record['received_ms'],subject,'source_state',before,after)
            self._health[subject]=after
        if kind=='timer':
            stale=[k for k,v in self._previous.items() if record['received_ms']-v.get('timestamp_wall',record['received_ms'])>7200000]
            for key in stale:self._previous.pop(key,None);self._frames.pop(key,None)
        return record

    def _salient(self,raw_id,now,scope,kind,before,after,previous_raw=None,source_ms=None):
        self.active.db.execute('INSERT INTO salient(raw_id,timestamp_ms,scope,kind,before_value,after_value,previous_raw_id,source_ms) VALUES(?,?,?,?,?,?,?,?)',
            (raw_id,now,scope,kind,encode(before),encode(after),previous_raw,source_ms))

    def append_observation(self,raw_id,data):
        from .features import valid
        event=data['event_id'];previous=self._previous.get(event);now=data['timestamp_wall']
        if previous:
            if previous.get('source_state')!=data.get('source_state'):
                self._salient(raw_id,now,event,'event_source_state',previous.get('source_state'),data.get('source_state'),previous['raw_id'])
            if previous.get('mapping_hash')!=data.get('mapping_hash'):
                self._salient(raw_id,now,event,'mapping_change',previous.get('mapping_hash'),data.get('mapping_hash'),previous['raw_id'])
            trend=data.get('features',{}).get('trend')
            if previous.get('trend')!=trend:
                self._salient(raw_id,now,event,'gap_trend',previous.get('trend'),trend,previous['raw_id'])
            if previous.get('mapping_hash')==data.get('mapping_hash') and valid(data) and valid(previous) and data.get('pm_source')!='gamma_indicative' and previous.get('pm_source')!='gamma_indicative':
                for leg in ('pm','opt','spot'):
                    field=leg+'_yes' if leg!='spot' else 'spot';a,b=previous.get(field),data.get(field)
                    if a is None or b is None:continue
                    change=math.log(b/a) if leg=='spot' and a>0 and b>0 else 100*(b-a)
                    threshold=self.params.get('spot_jump_return',.001) if leg=='spot' else self.params.get('event_jump_pp',.25)
                    if abs(change)>=threshold:self._salient(raw_id,now,event,leg+'_jump',a,b,previous['raw_id'],data.get(leg+'_timestamp'))
                a,b=previous.get('gap_pp'),data.get('gap_pp')
                if a is not None and b is not None and a*b<0:self._salient(raw_id,now,event,'gap_reversal',a,b,previous['raw_id'])
        self._previous[event]={k:data.get(k) for k in ('raw_id','timestamp_wall','mapping_hash','source_state','quality_flags','pm_source','pm_yes','opt_yes','spot','gap_pp')}
        self._previous[event]['trend']=data.get('features',{}).get('trend')
        identity=(now//self.frame_ms,tuple(data.get(k) for k in SEMANTICS),data.get('source_state'),
                  tuple(data.get('quality_flags',[])),data.get('pm_source'),data.get('basis_method'),data.get('code_hash'),data.get('parameter_hash'))
        if self._frames.get(event)==identity:return None
        self._frames[event]=identity
        return self.active.append_frame(raw_id,data)

    def append_episode(self,raw_id,data):
        key=data['gap_event_id'];previous=self._episodes.get(key)
        terminal=data['state'] in ('NEW','CLOSED','EXPIRED')
        mode=lambda x:'WIDENING' if x['state'] in ('PEAK','WIDENING') else x['state']
        meaningful=previous is None or terminal or data['max_abs_gap_pp']-(previous['max_abs_gap_pp'] if previous else 0)>=.1
        if previous and not meaningful:
            meaningful=mode(data)!=mode(previous) and data['timestamp']-previous['timestamp']>=self.frame_ms
        if not meaningful:return
        self.active.ids['episodes']+=1
        self.active.db.execute('INSERT INTO episodes VALUES(?,?,?,?,?,?)',(self.active.ids['episodes'],raw_id,key,data['event_id'],data['timestamp'],zlib.compress(encode(data).encode(),6)))
        self._episodes[key]=dict(data)
        if data['state'] in ('CLOSED','EXPIRED'):self._episodes.pop(key,None)

    def append_analyzer(self,raw_id,data):
        self.active.ids['analyzers']+=1
        self.active.db.execute('INSERT INTO analyzers VALUES(?,?,?,?,?,?,?)',(self.active.ids['analyzers'],raw_id,data['timestamp'],data['algo_name'],data['market_scope'],data['algo_version'],zlib.compress(encode(data).encode(),6)))

    def append_checkpoint(self,raw_id,revision,state):self.active.append_checkpoint(raw_id,revision,state)

    def stats_counts(self):
        counts=dict((self.manifest.get('legacy') or {}).get('limits',{k:0 for k in TABLES}))
        for item in self.manifest['segments']:
            values=item['limits'] if item.get('closed') else self._handle(item).ids
            counts={k:max(counts[k],values[k]) for k in TABLES}
        return counts

    def raw_record(self,raw_id):
        with self.lock:
            self._refresh()
            if self.legacy and raw_id<=self.legacy.limits['raw']:return self.legacy.raw_record(raw_id)
            for item in reversed(self.manifest['segments']):
                if raw_id>item['base']['raw']:
                    result=self._handle(item).raw_record(raw_id)
                    if result:return result
            return None

    def raw(self,after=0,until=None):
        end=self.stats_counts()['raw']
        yield from self.between(after,end,until)

    def between(self,after,end,until=None):
        self._refresh()
        if self.legacy and after<self.legacy.limits['raw']:
            yield from self.legacy.between(after,end,until);after=min(end,self.legacy.limits['raw'])
        for item in list(self.manifest['segments']):
            if end<=item['base']['raw']:break
            handle=self._handle(item)
            upper=min(end,handle.ids['raw'])
            for record in handle.between(max(after,item['base']['raw']),upper,until):yield record
            after=max(after,upper)

    def history(self,event_id=None,start=0,end=2**63-1,limit=500,after_id=0,tail=False,before_id=2**63-1):
        if self.write:
            view=SegmentedTape(self.manifest_path)
            try:return view.history(event_id,start,end,limit,after_id,tail,before_id)
            finally:view.close()
        limit=max(0,min(int(limit),10000));result=[]
        with self.lock:
            self._refresh();items=list(self.manifest['segments'])
            sources=[self.legacy]+[self._handle(i) for i in items] if self.legacy else [self._handle(i) for i in items]
            if tail:sources.reverse()
            for source in sources:
                if len(result)>=limit:break
                rows=source.history(event_id,start,end,limit-len(result),after_id,tail,before_id)
                result.extend(rows)
        return sorted(result,key=lambda r:r['observation_id'])

    def latest(self,table,key,limit=200,event_id=None):
        if self.write:
            view=SegmentedTape(self.manifest_path)
            try:return view.latest(table,key,limit,event_id)
            finally:view.close()
        if (table,key) not in {('episodes','gap_event_id'),('analyzers','scope,algo_name'),('observations','event_id')}:raise ValueError('Unsupported table')
        unique={};fields=key.split(',')
        with self.lock:
            self._refresh()
            sources=[self._handle(i) for i in reversed(self.manifest['segments'])]+([self.legacy] if self.legacy else [])
            for source in sources:
                for row in source.latest(table,key,limit,event_id):unique.setdefault(tuple(row[k if k!='scope' else 'market_scope'] for k in fields),row)
                if len(unique)>=limit:break
        return sorted(unique.values(),key=lambda r:r['record_id'],reverse=True)[:limit]

    def research_count(self,view):
        if self.write:
            reader=SegmentedTape(self.manifest_path)
            try:return reader.research_count(view)
            finally:reader.close()
        if view=='replay':return self.stats_counts()['observations']
        table,key={'episodes':('episodes','gap_event_id'),'algos':('analyzers','scope,algo_name')}[view]
        # Small immutable key indexes, never full observation blobs.
        keys=set()
        with self.lock:
            sources=[(self._handle(i).db,None) for i in self.manifest['segments']]
            if self.legacy:sources.append((self.legacy.db,self.legacy.limits[table]))
            for db,boundary in sources:
                where='' if boundary is None else ' WHERE id<=?'
                keys.update(tuple(r) for r in db.execute(f'SELECT {key} FROM {table}{where} GROUP BY {key}',() if boundary is None else (boundary,)))
        return len(keys)

    def checkpoint(self,revision=None,before_raw_id=None):
        before=self.stats_counts()['raw'] if before_raw_id is None else before_raw_id
        with self.lock:
            for item in reversed(self.manifest['segments']):
                cp=self._handle(item).checkpoint(revision,before,self.checkpoint_errors)
                if cp:return cp
            if self.legacy:
                cp=self.legacy.checkpoint(revision,before);self.checkpoint_errors.extend(self.legacy.checkpoint_errors);return cp

    def checkpoint_metadata(self):
        rows=[]
        if self.legacy:rows=[dict(r) for r in self.legacy.db.execute('SELECT id,raw_id,code_hash FROM checkpoints WHERE id<=?',(self.legacy.limits['checkpoints'],))]
        for item in self.manifest['segments']:rows.extend(dict(r) for r in self._handle(item).db.execute('SELECT id,raw_id,code_hash FROM checkpoints'))
        return rows

    def frames_at(self,raw_id):
        if self.legacy and raw_id<=self.legacy.limits['raw']:
            rows=self.legacy.db.execute('SELECT data FROM observations WHERE raw_id=? ORDER BY id',(raw_id,)).fetchall()
            return [decode(r[0]) for r in rows]
        for item in reversed(self.manifest['segments']):
            if raw_id>item['base']['raw']:
                s=self._handle(item)
                return [s.frame(r) for r in s.db.execute('SELECT * FROM frames WHERE raw_id=? ORDER BY id',(raw_id,))]
        return []

    def observations_in_range(self,start,end,limit=10000):
        rows=[]
        if self.legacy and start<=self.legacy.limits['raw']:
            rows=[dict(r) for r in self.legacy.db.execute('SELECT raw_id,data FROM observations WHERE raw_id BETWEEN ? AND ? ORDER BY raw_id,id LIMIT ?',
                  (start,min(end,self.legacy.limits['raw']),limit))]
        for item in self.manifest['segments']:
            if len(rows)>=limit:break
            if end<=item['base']['raw']:break
            s=self._handle(item)
            rows.extend(dict(raw_id=r['raw_id'],data=encode(s.frame(r))) for r in s.db.execute('SELECT * FROM frames WHERE raw_id BETWEEN ? AND ? ORDER BY raw_id,id LIMIT ?',
                        (start,end,limit-len(rows))))
        return rows

    def checkpoint_record(self,identity):
        if self.legacy and identity<=self.legacy.limits['checkpoints']:
            row=self.legacy.db.execute('SELECT * FROM checkpoints WHERE id=?',(identity,)).fetchone()
            return dict(row) if row else None
        for item in self.manifest['segments']:
            s=self._handle(item);row=s.db.execute('SELECT * FROM checkpoints WHERE id=?',(identity,)).fetchone()
            if row:
                body=encode(checkpoint_unpack(decode(row['data']),s.get)).encode()
                return dict(row,sha256=row['sha256'].hex(),data=zlib.compress(body))

    def iter_observations(self,boundary):
        if self.legacy:
            cursor=self.legacy.db.execute('SELECT raw_id,data FROM observations WHERE raw_id<=? ORDER BY id',(min(boundary,self.legacy.limits['raw']),))
            for row in cursor:yield row['raw_id'],decode(row['data'])
        for item in self.manifest['segments']:
            s=self._handle(item)
            for row in s.db.execute('SELECT * FROM frames WHERE raw_id<=? ORDER BY id',(boundary,)):yield row['raw_id'],s.frame(row)

    def marker_records(self,kind,after,end):
        if self.legacy and after<self.legacy.limits['raw']:
            rows=self.legacy.db.execute('SELECT id FROM raw WHERE kind=? AND id>? AND id<=? ORDER BY id',(kind,after,min(end,self.legacy.limits['raw']))).fetchall()
            for row in rows:yield self.legacy.raw_record(row[0])
        for item in self.manifest['segments']:
            s=self._handle(item)
            for row in s.db.execute('SELECT r.id FROM records r JOIN streams s ON s.id=r.stream_id WHERE s.kind=? AND r.id>? AND r.id<=? ORDER BY r.id',(kind,after,end)):
                yield s.raw_record(row[0])

    def journal_rows(self,table,after,before_raw):
        if table not in ('episodes','analyzers'):raise ValueError('Unsupported journal')
        rows=[]
        for item in self.manifest['segments']:
            rows.extend(dict(r) for r in self._handle(item).db.execute(f'SELECT * FROM {table} WHERE id>? AND raw_id<=? ORDER BY id',(after,before_raw)))
        return rows

    def stats(self):
        with self.lock:
            self._refresh();counts=self.stats_counts();first=None;last=None
            if self.legacy:first=self.legacy.stats()['first_received_ms']
            for item in self.manifest['segments']:
                row=self._handle(item).db.execute('SELECT (SELECT received_ms FROM records ORDER BY received_ms,id LIMIT 1),(SELECT received_ms FROM records ORDER BY received_ms DESC,id DESC LIMIT 1)').fetchone()
                if first is None and row[0] is not None:first=row[0]
                if row[1] is not None:last=row[1]
            if last is None and self.legacy:last=self.legacy.stats()['last_received_ms']
            return dict(path=self.path,**{k:counts[k] for k in TABLES if k!='checkpoints'},first_received_ms=first,last_received_ms=last,
                storage_version=2,frame_cadence_ms=self.frame_ms,segments=len(self.manifest['segments']),legacy_raw_boundary=(self.manifest.get('legacy') or {}).get('limits',{}).get('raw'))

    def logical_bytes(self):
        return self.v2_bytes()+((self.manifest.get('legacy') or {}).get('bytes_at_attach',0))

    def v2_bytes(self):
        return sum((self.root/i['file']).stat().st_size if i.get('closed') else self._handle(i).bytes() for i in self.manifest['segments'])

    def integrity(self,check_active=True):
        rows=[]
        legacy_identity=None
        legacy=self.manifest.get('legacy')
        if legacy and legacy.get('frozen'):
            stat=Path(legacy['path']).stat()
            unchanged=stat.st_size==legacy['bytes_at_attach'] and stat.st_mtime_ns==legacy['mtime_ns']
            legacy_identity=dict(result='ok' if unchanged else 'FAIL',bytes=stat.st_size,mtime_ns=stat.st_mtime_ns,
                reason='Frozen legacy file size/mtime unchanged; full structural check remains dated evidence' if unchanged else 'Frozen legacy file changed')
        for item in self.manifest['segments']:
            path=self.root/item['file'];stat=path.stat()
            if item.get('closed'):
                unchanged=stat.st_size==item['bytes'] and stat.st_mtime_ns==item['mtime_ns']
                rows.append(dict(segment=item['id'],result=item['integrity']['result'] if unchanged else 'FAIL',cached=True,
                    reason='Recorded closed-file size/mtime unchanged; read-only segment, SHA-256 recorded at seal' if unchanged else 'Closed segment changed',**{k:item['integrity'][k] for k in ('timestamp_ms','seconds')}))
            elif check_active:
                started=time.monotonic();db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=5,isolation_level=None)
                try:messages=[r[0] for r in db.execute('PRAGMA quick_check')]
                finally:db.close()
                rows.append(dict(segment=item['id'],result='ok' if messages==['ok'] else 'FAIL',cached=False,messages=messages,timestamp_ms=time.time_ns()//1000000,seconds=time.monotonic()-started))
        return dict(result='ok' if rows and all(r['result']=='ok' for r in rows) and (legacy_identity is None or legacy_identity['result']=='ok') else 'FAIL',segments=rows,fresh=True,
            timestamp_ms=time.time_ns()//1000000,seconds=sum(r['seconds'] for r in rows if not r['cached']),
            legacy_file_identity=legacy_identity,
            legacy='Frozen v1 uses separately retained dated integrity evidence; never rescanned by routine v2 acceptance.')

    def close(self):
        for s in list(self.handles.values()):s.close()
        if self.legacy:self.legacy.close()


def legacy_limits(reader,boundary):
    """Locate causal row-ID limits using indexes/binary search, not blob scans."""
    result={'raw':boundary}
    for table in TABLES[1:]:
        if table=='observations':
            row=reader.db.execute('SELECT id FROM observations WHERE raw_id<=? ORDER BY raw_id DESC,id DESC LIMIT 1',(boundary,)).fetchone()
            result[table]=row[0] if row else 0;continue
        lo=0;hi=reader.db.execute(f'SELECT COALESCE(MAX(id),0)+1 FROM {table}').fetchone()[0]
        while lo+1<hi:
            mid=(lo+hi)//2;row=reader.db.execute(f'SELECT raw_id FROM {table} WHERE id=?',(mid,)).fetchone()
            if row and row[0]<=boundary:lo=mid
            else:hi=mid
        result[table]=lo
    return result


def create_tape(alias,root,legacy_boundary=None,segment_max_bytes=256*1024**2):
    root=Path(root).resolve();root.mkdir(parents=True,exist_ok=True);path=root/'manifest.json'
    if path.exists():raise ValueError('Choose a new tape directory; never replace an existing manifest')
    alias=Path(alias).resolve();legacy=None
    if alias.exists():
        reader=LegacyReader(alias)
        boundary=reader.limits['raw'] if legacy_boundary is None else legacy_boundary
        limits=legacy_limits(reader,boundary);first=reader.raw_record(1);reader.close()
        legacy=dict(path=str(alias),limits=limits,first_raw_hash=first['sha256'] if first else None,bytes_at_attach=alias.stat().st_size,
                    storage_version=1,reason='Preserved in place; logical prefix bounded, never rewritten')
    base=legacy['limits'] if legacy else {k:0 for k in TABLES}
    manifest=dict(storage_version=2,alias=str(alias),created_ms=time.time_ns()//1000000,legacy=legacy,
        segment_max_bytes=segment_max_bytes,source_codec=SourceCodec.version,frame_cadence_ms=1000,
        episode_journal=dict(material_peak_pp=.1,state_min_interval_ms=1000,terminal='immediate'),
        segments=[dict(id=1,file='segment-000001.sqlite3',base=base,closed=False,storage_version=2)])
    Segment(root/'segment-000001.sqlite3',base,True).close();atomic_json(path,manifest)
    return SegmentedTape(path,True)


def publish_tape(tape):
    """Only the explicit validated cutover calls this, while owning the DB lock."""
    pointer=Path(tape.path+'.storage.json')
    if pointer.exists():raise ValueError('Storage alias already points to a tape; never replace it silently')
    atomic_json(pointer,dict(storage_version=2,manifest=str(tape.manifest_path),alias=tape.path))


def open_store(path,read_only=False):
    if str(path)==':memory:':return Store(path)
    pointer=Path(str(Path(path).resolve())+'.storage.json')
    if pointer.exists():
        value=json.loads(pointer.read_text())
        return SegmentedTape(value['manifest'],write=not read_only)
    if Path(path).exists():
        db=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)
        version=db.execute('PRAGMA user_version').fetchone()[0];db.close()
        if version==2:raise ValueError('A segment is not a service tape; use its manifest alias for global ownership and history')
    return LegacyReader(path) if read_only else Store(path)
