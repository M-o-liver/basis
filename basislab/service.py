"""Loopback API and static terminal. The collector owns data, the UI only reads it."""
import json
import logging
import mimetypes
import os
import fcntl
import signal
import time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from .collector import Collector
from .config import Config
from .engine import Engine
from .replay import restore
from .semantics import validate_mapping
from .store import Store, encode
from .tape import open_store
from .market_math import MarketMath
from .tracking import Tracker
from .operations import Journal, RecorderMonitor
ROOT = Path(__file__).resolve().parent.parent


def serve(db='data/basis.sqlite3', port=8765, config=None, collect=True):
    db=str(Path(db).resolve())
    Path(db).parent.mkdir(parents=True, exist_ok=True)
    lock = open(str(db) + '.collector.lock', 'a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise RuntimeError('A BASIS service already owns this tape')
    store = open_store(db)
    journal = Journal(db)
    journal.record('service_start', pid=os.getpid(), collect=collect)
    if collect and not journal.latest('campaign_start'):
        journal.record('campaign_start', pid=os.getpid(), reason='First instrumented production collector start; never reset by restart')
    started = time.monotonic()
    engine = Engine(store, config or Config())
    requested_config = engine.config
    restore(engine)
    journal.record('restore', pid=os.getpid(), seconds=time.monotonic()-started,
                   raw_id=engine.last_raw_id, checkpoint_errors=store.checkpoint_errors)
    engine.config = requested_config
    engine.dynamics.config = engine.episodes.config = requested_config
    if hasattr(store,'resume'):store.resume(engine)
    engine.start_session()
    journal.record('session_start', session=engine.session, raw_id=engine.last_raw_id,
                   code_hash=engine.code_hash, pid=os.getpid(), collect=collect)
    monitor = RecorderMonitor(engine, journal)
    collector = Collector(engine, monitor)
    math_engine = MarketMath(engine)
    math_engine.start()
    tracker = Tracker(math_engine, Path(db).with_suffix('.stars.sqlite3'))
    tracker.start()

    history_path=Path(db).parent/'gap-shape.json'
    history_cache={'mtime':None,'data':{}}
    def historical_match(row):
        try:
            mtime=history_path.stat().st_mtime_ns
            if mtime!=history_cache['mtime']:
                history_cache.update(mtime=mtime,data=json.loads(history_path.read_text()))
        except (OSError,ValueError):
            return dict(n_blocks=0,status='INSUFFICIENT',match='Run ./basis gaps to measure historical response')
        from .gap_history import bin_gap
        gap=row.get('math',{}).get('gap_pp')
        if gap is None:return dict(n_blocks=0,status='NO_COMPARABLE_MODEL')
        key='|'.join((row['asset'],row['event_type'],row['direction'],bin_gap(gap)))
        result=history_cache['data'].get('comparables',{}).get(key)
        if result:return dict(result,match=key+' @30m; exploratory chronology-window sample'+(' / earlier equity proxy mappings' if row['asset'] not in ('BTC','ETH') else ''))
        return dict(n_blocks=0,status='INSUFFICIENT',match=key+' has no measured comparable observations')
    def collector_state():
        return dict(running=bool(collector.thread and collector.thread.is_alive() and not engine.persistence_error),
                    failure=collector.failed or engine.persistence_error)

    class Handler(BaseHTTPRequestHandler):
        server_version = 'BasisResearch/1.0'
        def log_message(self, *args):
            pass
        def send(self, status, data):
            payload = encode(data).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass
        def do_GET(self):
            path = urlparse(self.path); q = parse_qs(path.query,keep_blank_values=True)
            try:
                if path.path == '/api/state':
                    data = engine.snapshot(); data.pop('catalog', None)
                    data['collector'] = collector_state()
                    self.send(200, data)
                elif path.path == '/api/markets':
                    data = math_engine.snapshot()
                    fields = ('event_id','event_text','asset','expiry','event_type','direction','strike_or_threshold',
                              'pm_yes','opt_yes','math','trade','trade_reason','execution_state','formation','snapshot_id')
                    rows = [dict({k:r.get(k) for k in fields},history=historical_match(r)) for r in data['rows']]
                    self.send(200, dict(rows=rows, error=data['error'], collector=collector_state(),
                        sources=engine.snapshot()['sources'],tracking=tracker.snapshot()))
                elif path.path == '/api/market':
                    event_id = q.get('event_id',[''])[0]
                    with math_engine.lock:row = math_engine.rows.get(event_id)
                    if row is None:raise ValueError('Explicit event_id is unavailable; select a current market')
                    self.send(200, dict(row=dict(row,history=historical_match(row))))
                elif path.path == '/api/gap-history':
                    event_id=q.get('event_id',[''])[0];minutes=int(q.get('minutes',[30])[0])
                    if not event_id or minutes not in (5,30,120):raise ValueError('Require event_id and 5, 30 or 120 minutes')
                    now=time.time_ns()//1000000
                    rows=store.history(event_id,now-minutes*60000,now,10000,tail=True)
                    step=max(1,len(rows)//720)
                    self.send(200,dict(rows=[dict(t=r['timestamp_wall'],pm=r.get('pm_yes'),opt=r.get('opt_yes'),gap=r.get('gap_pp')) for r in rows[::step]],reason='No recorded observations in this time window'))
                elif path.path == '/api/stars':
                    self.send(200,tracker.snapshot())
                elif path.path == '/api/star':
                    data=tracker.snapshot(q.get('tracking_id',[''])[0]);data['frozen']['snapshot']['history']=historical_match(data['frozen']['snapshot'])
                    self.send(200,data)
                elif path.path == '/api/health':
                    self.send(200,dict(ok=not(collector.failed or engine.persistence_error),collecting=collector_state()['running'],versions=engine.snapshot()['versions']))
                else:
                    static = {'/': 'index.html', '/index.html': 'index.html', '/styles.css': 'styles.css', '/app.js': 'app.js'}
                    name = static.get(path.path)
                    if not name:
                        self.send(404, dict(error='Not found')); return
                    content = (ROOT / name).read_bytes()
                    self.send_response(200)
                    self.send_header('Content-Type', (mimetypes.guess_type(name)[0] or 'text/plain') + '; charset=utf-8')
                    self.send_header('Content-Length', str(len(content)))
                    self.send_header('Cache-Control', 'no-cache')
                    self.end_headers(); self.wfile.write(content)
            except (ValueError, TypeError, KeyError) as error:
                self.send(400, dict(error=str(error),code=getattr(error,'code','INVALID_REQUEST')))
            except Exception as error:
                logging.exception('BASIS GET %s failed',path.path)
                self.send(500,dict(error=str(error),code='INTERNAL_ERROR'))
        def do_POST(self):
            # Same-origin local control only; no CORS or remote account operations.
            origin = self.headers.get('Origin')
            host = self.headers.get('Host')
            if host not in (f'127.0.0.1:{port}', f'localhost:{port}') or (origin and urlparse(origin).netloc != host):
                self.send(403, dict(error='Same-origin local control required')); return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if size > 65536 or size <= 0:
                    raise ValueError('Body must be 1-65536 bytes')
                payload = json.loads(self.rfile.read(size))
                if self.path == '/api/star':
                    self.send(200,tracker.create(payload['event_id'],payload['snapshot_id']))
                elif self.path == '/api/star/stop':
                    self.send(200,tracker.finish(payload['tracking_id']))
                elif self.path == '/api/star/remove':
                    self.send(200,tracker.remove(payload['tracking_id']))
                elif self.path == '/api/mapping':
                    event = validate_mapping(payload)
                    raw_id = engine.ingest('operator', 'mapping', event['event_id'], event)
                    self.send(200, dict(raw_id=raw_id, mapping=event))
                elif self.path == '/api/dismiss':
                    key = str(payload['event_id'])
                    raw_id = engine.ingest('operator', 'dismiss', key, dict(event_id=key))
                    self.send(200, dict(raw_id=raw_id))
                elif self.path == '/api/refresh':
                    collector.refresh.set(); self.send(200, dict(requested=True))
                else:
                    self.send(404, dict(error='Not found'))
            except (ValueError, TypeError, KeyError) as error:
                self.send(400, dict(error=str(error),code=getattr(error,'code','INVALID_REQUEST')))
            except Exception as error:
                logging.exception('BASIS POST %s failed',self.path)
                self.send(500,dict(error=str(error),code='INTERNAL_ERROR'))

    class RecorderServer(ThreadingHTTPServer):
        def service_actions(self):
            # A live HTTP process is not proof of a live recorder. Exit so the
            # supervisor can recover, rather than leaving a dead collector online.
            if collector.failed or engine.persistence_error:
                raise RuntimeError(collector.failed or engine.persistence_error)
            if collect and collector.thread and not collector.thread.is_alive():
                collector.failure_kind = 'BASIS_COLLECTOR_FAILURE'
                raise RuntimeError('Collector exited without a healthy recording loop')
            monitor.sample()

    httpd = RecorderServer(('127.0.0.1', port), Handler)
    if collect:
        collector.start()
    print(f'BASIS research terminal: http://127.0.0.1:{port} | tape {db}', flush=True)
    if __import__('threading').current_thread() is __import__('threading').main_thread():
        def terminate(signum, frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, terminate)
    shutdown_reason = 'UNKNOWN_STOP'
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        shutdown_reason = 'CONTROLLED_RESTART'
    finally:
        reason = collector.failure_kind or ('BASIS_COLLECTOR_FAILURE' if engine.persistence_error else shutdown_reason)
        journal.record('service_stop', session=engine.session, raw_id=engine.last_raw_id, pid=os.getpid(),
                       reason=reason, error=collector.failed or engine.persistence_error,
                       min_free_mb=engine.config.min_free_mb)
        httpd.server_close(); tracker.close(); math_engine.close(); collector.close()
        monitor.checkpoint()
        journal.record('service_stopped', session=engine.session, raw_id=engine.last_raw_id, reason=reason)
        store.close(); journal.close(); lock.close()
