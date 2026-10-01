"""Append-only SQLite journal; receipt sequence, not source time, defines availability."""
from contextlib import contextmanager
import hashlib
import json
import logging
from pathlib import Path
import sqlite3
import threading
import time
import zlib


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def decode(value):
    return json.loads(zlib.decompress(value) if isinstance(value, bytes) else value)


class Store:
    def __init__(self, path='data/basis.sqlite3'):
        self.path = str(path)
        if self.path != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.checkpoint_errors = []
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS raw (
          id INTEGER PRIMARY KEY, received_ms INTEGER NOT NULL, monotonic_ns INTEGER NOT NULL,
          source TEXT NOT NULL, kind TEXT NOT NULL, subject TEXT NOT NULL, source_ms INTEGER,
          payload BLOB NOT NULL, sha256 TEXT NOT NULL, session TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS raw_time ON raw(received_ms,id);
        CREATE INDEX IF NOT EXISTS raw_kind ON raw(kind,subject,id);
        CREATE TABLE IF NOT EXISTS observations (
          id INTEGER PRIMARY KEY, raw_id INTEGER NOT NULL REFERENCES raw(id), event_id TEXT NOT NULL,
          timestamp_ms INTEGER NOT NULL, version TEXT NOT NULL, data TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS obs_event ON observations(event_id,timestamp_ms,id);
        CREATE INDEX IF NOT EXISTS obs_raw ON observations(raw_id,id);
        CREATE TABLE IF NOT EXISTS episodes (
          id INTEGER PRIMARY KEY, gap_event_id TEXT NOT NULL, raw_id INTEGER NOT NULL REFERENCES raw(id),
          event_id TEXT NOT NULL, timestamp_ms INTEGER NOT NULL, data TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS episode_key ON episodes(gap_event_id,id);
        CREATE TABLE IF NOT EXISTS analyzers (
          id INTEGER PRIMARY KEY, raw_id INTEGER NOT NULL REFERENCES raw(id), timestamp_ms INTEGER NOT NULL,
          algo_name TEXT NOT NULL, scope TEXT NOT NULL, version TEXT NOT NULL, data TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS analyzer_key ON analyzers(scope,algo_name,id);
        CREATE TABLE IF NOT EXISTS checkpoints (
          id INTEGER PRIMARY KEY, raw_id INTEGER NOT NULL REFERENCES raw(id), code_hash TEXT NOT NULL,
          sha256 TEXT NOT NULL, data BLOB NOT NULL);
        CREATE INDEX IF NOT EXISTS checkpoint_version ON checkpoints(code_hash,id);
        PRAGMA user_version=1;
        ''')
        for table in ('raw', 'observations', 'episodes', 'analyzers', 'checkpoints'):
            for operation in ('UPDATE', 'DELETE'):
                self.db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{operation} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'immutable research tape'); END")

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                yield
                self.db.execute('COMMIT')
            except BaseException:
                # SQLite may already abort a transaction (e.g. SQLITE_FULL).
                # Preserve that useful failure instead of masking it in cleanup.
                if self.db.in_transaction:
                    self.db.execute('ROLLBACK')
                raise

    def append_raw(self, source, kind, subject, payload, session, received_ms=None, source_ms=None, monotonic_ns=None):
        body = encode(payload).encode()
        record = dict(source=source, kind=kind, subject=subject, payload=payload, session=session,
                      received_ms=received_ms if received_ms is not None else time.time_ns() // 1_000_000,
                      source_ms=source_ms, monotonic_ns=monotonic_ns if monotonic_ns is not None else time.monotonic_ns())
        cur = self.db.execute('INSERT INTO raw(received_ms,monotonic_ns,source,kind,subject,source_ms,payload,sha256,session) VALUES(?,?,?,?,?,?,?,?,?)',
                              (record['received_ms'], record['monotonic_ns'], source, kind, subject, source_ms, zlib.compress(body), hashlib.sha256(body).hexdigest(), session))
        return dict(record, id=cur.lastrowid)

    def append_observation(self, raw_id, data):
        cur = self.db.execute('INSERT INTO observations(raw_id,event_id,timestamp_ms,version,data) VALUES(?,?,?,?,?)',
                             (raw_id, data['event_id'], data['timestamp_wall'], data['calculation_version'], zlib.compress(encode(data).encode(), 1)))
        return cur.lastrowid

    def append_episode(self, raw_id, data):
        self.db.execute('INSERT INTO episodes(gap_event_id,raw_id,event_id,timestamp_ms,data) VALUES(?,?,?,?,?)',
                        (data['gap_event_id'], raw_id, data['event_id'], data['timestamp'], encode(data)))

    def append_analyzer(self, raw_id, data):
        self.db.execute('INSERT INTO analyzers(raw_id,timestamp_ms,algo_name,scope,version,data) VALUES(?,?,?,?,?,?)',
                        (raw_id, data['timestamp'], data['algo_name'], data['market_scope'], data['algo_version'], encode(data)))

    def append_checkpoint(self, raw_id, revision, state):
        body = encode(state).encode()
        self.db.execute('INSERT INTO checkpoints(raw_id,code_hash,sha256,data) VALUES(?,?,?,?)',
                        (raw_id, revision, hashlib.sha256(body).hexdigest(), zlib.compress(body, 1)))

    def checkpoint(self, revision=None, before_raw_id=None):
        # An invalid newest checkpoint must not prevent committed raw-tape recovery.
        # Keep the bad row and its evidence; bound fallback searches before raw replay.
        before = 2**63-1
        for _ in range(10):
            with self.lock:
                where = 'id<?' + (' AND code_hash=?' if revision else '')
                params = (before, revision) if revision else (before,)
                if before_raw_id is not None:
                    where += ' AND raw_id<=?'
                    params += (before_raw_id,)
                row = self.db.execute(f'SELECT * FROM checkpoints WHERE {where} ORDER BY id DESC LIMIT 1', params).fetchone()
            if row is None:
                return None
            before = row['id']
            try:
                body = zlib.decompress(row['data'])
                if hashlib.sha256(body).hexdigest() != row['sha256']:
                    raise ValueError('content hash mismatch')
                return dict(raw_id=row['raw_id'], code_hash=row['code_hash'], state=json.loads(body))
            except (zlib.error, ValueError, UnicodeError) as error:
                failure = dict(checkpoint_id=row['id'], raw_id=row['raw_id'], error=str(error))
                if failure not in self.checkpoint_errors:
                    self.checkpoint_errors.append(failure)
                logging.error('BASIS checkpoint unavailable; trying older checkpoint/raw tape: %s', failure)
        return None

    def raw(self, after=0, until=None):
        # Keyset pages keep replay memory bounded and avoid a long-held read lock.
        with self.lock:
            boundary = self.db.execute('SELECT COALESCE(MAX(id),0) FROM raw').fetchone()[0]
        while True:
            with self.lock:
                rows = self.db.execute('SELECT * FROM raw WHERE id>? AND id<=? AND (? IS NULL OR received_ms<=?) ORDER BY id LIMIT 500', (after, boundary, until, until)).fetchall()
            if not rows:
                return
            for row in rows:
                data = dict(row)
                body = zlib.decompress(data['payload'])
                if hashlib.sha256(body).hexdigest() != data['sha256']:
                    raise ValueError(f"Raw record {data['id']} failed its content hash")
                data['payload'] = json.loads(body)
                after = data['id']
                yield data

    def raw_record(self, raw_id):
        with self.lock:
            row = self.db.execute('SELECT * FROM raw WHERE id=?', (raw_id,)).fetchone()
            if row is None:
                return None
            data = dict(row)
            data['payload'] = json.loads(zlib.decompress(data['payload']))
            return data

    def history(self, event_id=None, start=0, end=2**63-1, limit=500, after_id=0, tail=False, before_id=2**63-1):
        with self.reader() as db:
            order = 'DESC' if tail else 'ASC'
            # Select IDs from the covering event index before fetching large
            # blobs. An optional-parameter OR made empty selected scopes scan
            # the entire tape while holding the collector's connection lock.
            where='timestamp_ms BETWEEN ? AND ? AND id>? AND id<?'
            args=[start,end,after_id,before_id]
            index=''
            if event_id is not None:
                where='event_id=? AND '+where;args.insert(0,event_id)
                index=' INDEXED BY obs_event'
            ids=[r[0] for r in db.execute(f'SELECT id FROM observations{index} WHERE {where} ORDER BY id {order} LIMIT ?',
                                        (*args,max(0,min(limit,10000))))]
            rows=[db.execute('SELECT id,data FROM observations WHERE id=?',(i,)).fetchone() for i in ids]
        return [dict(decode(row['data']), observation_id=row['id']) for row in (reversed(rows) if tail else rows)]

    def latest(self, table, key, limit=200, event_id=None):
        if (table, key) not in {('episodes', 'gap_event_id'), ('analyzers', 'scope,algo_name'), ('observations', 'event_id')}:
            raise ValueError('Unsupported table')
        with self.reader() as db:
            field='scope' if table=='analyzers' else 'event_id'
            where=f' WHERE {field}=?' if event_id is not None else ''
            args=(event_id,limit) if event_id is not None else (limit,)
            rows = db.execute(f'SELECT id,data FROM {table} WHERE id IN (SELECT MAX(id) FROM {table}{where} GROUP BY {key}) ORDER BY id DESC LIMIT ?',args).fetchall()
        return [dict(decode(row['data']), record_id=row['id']) for row in rows]

    def research_count(self,view):
        with self.reader() as db:
            if view=='replay':return db.execute('SELECT COALESCE(MAX(id),0) FROM observations').fetchone()[0]
            table,key={'episodes':('episodes','gap_event_id'),'algos':('analyzers','scope,algo_name')}[view]
            return db.execute(f'SELECT COUNT(*) FROM (SELECT 1 FROM {table} GROUP BY {key})').fetchone()[0]

    @contextmanager
    def reader(self):
        """Short bounded history queries never occupy the recorder connection."""
        if self.path==':memory:':
            with self.lock:yield self.db
            return
        db=sqlite3.connect(Path(self.path).resolve().as_uri()+'?mode=ro'+('&immutable=1' if getattr(self,'immutable',False) else ''),uri=True,timeout=5,isolation_level=None)
        db.row_factory=sqlite3.Row
        deadline=time.monotonic()+5
        db.set_progress_handler(lambda:time.monotonic()>deadline,10000)
        try:yield db
        except sqlite3.OperationalError as error:
            if str(error)=='interrupted':raise RuntimeError('Historical request exceeded the 5s read budget; narrow its scope') from error
            raise
        finally:db.close()

    def logical_bytes(self):
        with self.lock:
            return self.db.execute('PRAGMA page_count').fetchone()[0]*self.db.execute('PRAGMA page_size').fetchone()[0]

    def stats(self):
        with self.lock:
            # IDs are allocated consecutively by this append-only store; deletion is
            # forbidden. Reading the last ID avoids scanning a growing tape each poll.
            counts = {name: self.db.execute(f'SELECT COALESCE(MAX(id),0) FROM {name}').fetchone()[0] for name in ('raw', 'observations', 'episodes', 'analyzers')}
            span = self.db.execute('SELECT (SELECT received_ms FROM raw ORDER BY received_ms LIMIT 1), (SELECT received_ms FROM raw ORDER BY received_ms DESC LIMIT 1)').fetchone()
        return dict(counts, first_received_ms=span[0], last_received_ms=span[1], path=self.path)

    def close(self):
        with self.lock:
            self.db.close()
