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
from .paper import PaperDesk
from .operations import Journal, RecorderMonitor
from .evaluation_record import EvaluationWorker
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
    paper = PaperDesk(engine, Path(db).with_suffix('.paper.sqlite3'))
    paper.start()
    evaluation = EvaluationWorker(engine, journal)
    if collect:evaluation.start()

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
                    data = engine.snapshot(); data.pop('catalog', None); data['collector'] = dict(running=bool(collector.thread and collector.thread.is_alive() and not engine.persistence_error), failure=collector.failed or engine.persistence_error)
                    data['evaluation'] = dict(evaluation.status)
                    data['phase2']=dict(enabled=True,mode='experimental_paper',automated_policies=True,research_acceptance='open',reason='Versioned diagnostic policies and manual paper; no real orders')
                    self.send(200, data)
                elif path.path == '/api/wallets':
                    self.send(200,paper.snapshot(q.get('wallet',[None])[0],int(q['run_id'][0]) if 'run_id' in q else None))
                elif path.path == '/api/review':
                    with paper.lock:self.send(200,paper.performance.report)
                elif path.path == '/api/theses':
                    with paper.lock:self.send(200,dict(rows=[t for t in paper.theses.values() if not q.get('event_id') or t['event_id']==q['event_id'][0]]))
                elif path.path == '/api/equity':
                    self.send(200,paper.curves(float(q.get('hours',[24])[0]),int(q['run_id'][0]) if 'run_id' in q else None))
                elif path.path == '/api/instruments':
                    self.send(200,paper.instruments(q.get('event_id',[None])[0],q.get('asset',[None])[0]))
                elif path.path == '/api/order':
                    data=paper.order_status(q.get('order_id',[''])[0])
                    self.send(200 if data else 404,data or dict(error='No such paper order'))
                elif path.path in ('/api/positions','/api/history'):
                    wallet=q.get('wallet',['OLIVER'])[0];run_id=int(q['run_id'][0]) if 'run_id' in q else None
                    with paper.lock:
                        run,_,archived=paper.resolve_run(wallet,run_id)
                        positions=path.path=='/api/positions'
                        rows=paper.metrics(wallet,run_id)['positions'] if positions else paper.history(wallet,run_id,q.get('limit',[100])[0],activity_only=True)
                        self.send(200,dict(rows=rows,wallet=wallet,run_id=run['id'],archived=archived,runs=paper.wallet_runs(wallet),
                            empty_reason=f'No {wallet} '+('positions' if positions else 'order or trade activity')+f' in run {run["run_number"]}.',
                            note='Archived positions use retained marks.' if positions else 'Orders, fills, rejections, cancellations, settlements and controls; periodic marks omitted.'))
                elif path.path == '/api/catalog':
                    with engine.lock:
                        self.send(200, dict(rows=list(engine.catalog.values())))
                elif path.path == '/api/health':
                    self.send(200, dict(ok=not (collector.failed or engine.persistence_error), collecting=bool(collector.thread and collector.thread.is_alive() and not engine.persistence_error), versions=engine.snapshot()['versions']))
                elif path.path == '/api/evaluation':
                    from .evaluation_report import report
                    self.send(200,report(db,q.get('campaign',[None])[0],episode_id=q.get('episode',[None])[0]))
                elif path.path == '/api/replay':
                    self.send(200, dict(rows=store.history(q.get('event_id', [None])[0], int(q.get('from', [0])[0]), int(q.get('to', [2**63-1])[0]), int(q.get('limit', [500])[0]), int(q.get('after', [0])[0]), q.get('tail', ['0'])[0] == '1', int(q.get('before', [2**63-1])[0])),global_count=store.research_count('replay'),empty_reason='No recorded observations in this scope/page.'))
                elif path.path == '/api/raw':
                    row = store.raw_record(int(q.get('id', [0])[0])); self.send(200 if row else 404, row or dict(error='No such raw record'))
                elif path.path == '/api/episodes':
                    self.send(200, dict(rows=store.latest('episodes', 'gap_event_id', 500,q.get('event_id',[None])[0]),global_count=store.research_count('episodes'),empty_reason='No gap episodes in this scope. Episodes require the configured opening threshold.'))
                elif path.path == '/api/algos':
                    minutes=engine.config.analysis_window*2*engine.config.analysis_grid_seconds/60
                    self.send(200, dict(rows=store.latest('analyzers', 'scope,algo_name', 1000,q.get('event_id',[None])[0]),global_count=store.research_count('algos'),empty_reason=f'No analyzer output in this scope. Distribution diagnostics need about {minutes:g} minutes of continuous fresh history.'))
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
                if self.path == '/api/automation':
                    self.send(200,paper.set_automation(payload['enabled']))
                elif self.path == '/api/preview':
                    with paper.lock:self.send(200,paper.preview(payload))
                elif self.path == '/api/trade':
                    self.send(200,paper.submit(payload))
                elif self.path == '/api/close':
                    with paper.lock:
                        wallet=payload.get('wallet','OLIVER');position=paper.states[wallet]['positions'].get(payload['instrument'])
                        if not position:raise ValueError('No position to close')
                        self.send(200,paper.submit(dict(payload,side='SELL',quantity=position['quantity'])))
                elif self.path == '/api/wallet/reset':
                    self.send(200,dict(run=paper.reset(payload.get('wallet','OLIVER'))))
                elif self.path == '/api/settle':
                    self.send(200,paper.settle(payload))
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
                elif self.path == '/api/evaluation/start':
                    from .evaluation import freeze_boundary
                    import subprocess
                    revision=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
                    campaign=freeze_boundary(db,engine.snapshot(),revision)
                    if not evaluation.thread or not evaluation.thread.is_alive():evaluation.start()
                    self.send(200,dict(evaluation_id=campaign['evaluation_id'],boundary=campaign['evaluation_started_at'],
                        first_raw_id=campaign['first_eligible_global_raw_id'],first_frame_id=campaign['first_eligible_frame_id']))
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
        httpd.server_close(); evaluation.close(); paper.close(); collector.close()
        monitor.checkpoint()
        journal.record('service_stopped', session=engine.session, raw_id=engine.last_raw_id, reason=reason)
        store.close(); journal.close(); lock.close()
