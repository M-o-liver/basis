"""Small append-only operational journal, separate from immutable research/paper tape."""
import os
from pathlib import Path
import resource
import shutil
import sqlite3
import threading
import time
from .store import encode, decode


class Journal:
    def __init__(self, tape):
        self.path = str(tape) + '.acceptance.sqlite3'
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, timeout=15, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS evidence(id INTEGER PRIMARY KEY, timestamp_ms INTEGER NOT NULL,
            kind TEXT NOT NULL, data TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS evidence_kind ON evidence(kind,id);
          CREATE TABLE IF NOT EXISTS markers(raw_id INTEGER PRIMARY KEY, timestamp_ms INTEGER NOT NULL,
            kind TEXT NOT NULL, subject TEXT NOT NULL, session TEXT NOT NULL, sha256 TEXT NOT NULL,
            data TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS markers_kind ON markers(kind,raw_id);
          CREATE TABLE IF NOT EXISTS cursor(kind TEXT PRIMARY KEY, raw_id INTEGER NOT NULL);
        ''')
        for table in ('evidence', 'markers'):
            for op in ('UPDATE', 'DELETE'):
                self.db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{op} BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT,'immutable acceptance evidence'); END")

    def record(self, event_kind, **data):
        with self.lock:
            self.db.execute('INSERT INTO evidence(timestamp_ms,kind,data) VALUES(?,?,?)',
                            (time.time_ns() // 1_000_000, event_kind, encode(data)))

    def entries(self, kind):
        with self.lock:
            return [dict(decode(r['data']), evidence_id=r['id'], timestamp_ms=r['timestamp_ms'])
                    for r in self.db.execute('SELECT * FROM evidence WHERE kind=? ORDER BY id', (kind,))]

    def latest(self, kind):
        with self.lock:
            row = self.db.execute('SELECT * FROM evidence WHERE kind=? ORDER BY id DESC LIMIT 1', (kind,)).fetchone()
            return dict(decode(row['data']), timestamp_ms=row['timestamp_ms']) if row else None

    def close(self):
        self.db.close()


def resources(tape):
    usage = resource.getrusage(resource.RUSAGE_SELF)
    sizes = {name: Path(str(tape) + suffix).stat().st_size if Path(str(tape) + suffix).exists() else 0
             for name, suffix in (('database_bytes', ''), ('wal_bytes', '-wal'))}
    rss = None
    try:
        rss = int(Path('/proc/self/statm').read_text().split()[1]) * os.sysconf('SC_PAGE_SIZE')
    except (OSError, ValueError, IndexError):
        pass
    return dict(sizes, free_bytes=shutil.disk_usage(Path(tape).parent).free, rss_bytes=rss,
                rss_high_water_bytes=int(usage.ru_maxrss * 1024), cpu_seconds=usage.ru_utime + usage.ru_stime)


class RecorderMonitor:
    def __init__(self, engine, journal):
        self.engine, self.journal = engine, journal
        self.seen_errors = set()
        self.last_sample = 0

    def sample(self, force=False):
        now = time.monotonic()
        if not force and now - self.last_sample < 30:
            return
        with self.engine.lock:
            data = dict(raw_id=self.engine.last_raw_id, session=self.engine.session,
                        uptime_seconds=(time.time_ns()//1_000_000-self.engine.started_ms)/1000,
                        delayed_packets=self.engine.delayed_packets, clock_errors=self.engine.clock_errors,
                        persistence_error=self.engine.persistence_error)
            # Main-file growth can pause behind a WAL reader. SQLite's logical
            # page count includes committed growth still residing in the WAL.
            with self.engine.store.lock:
                db=self.engine.store.db
                data['logical_database_bytes']=db.execute('PRAGMA page_count').fetchone()[0]*db.execute('PRAGMA page_size').fetchone()[0]
            retained_errors=set()
            for error in self.engine.errors:
                key = encode(error)
                retained_errors.add(key)
                if key not in self.seen_errors:
                    self.journal.record('quarantine', **error)
            self.seen_errors=retained_errors
        self.journal.record('resource', pid=os.getpid(), **data, **resources(self.engine.store.path))
        self.last_sample = now

    def checkpoint(self):
        started = time.monotonic()
        self.engine.save_checkpoint()
        self.journal.record('checkpoint', session=self.engine.session, raw_id=self.engine.last_raw_id,
                            seconds=time.monotonic()-started, ok=not self.engine.persistence_error)
