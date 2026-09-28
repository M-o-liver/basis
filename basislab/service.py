"""Loopback API and static terminal. The collector owns data, the UI only reads it."""
import json
import logging
import mimetypes
import os
import fcntl
import signal
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from .collector import Collector
from .config import Config
from .engine import Engine
from .replay import restore
from .semantics import validate_mapping
from .store import Store, encode
from .paper import PaperDesk
ROOT = Path(__file__).resolve().parent.parent


def serve(db='data/basis.sqlite3', port=8765, config=None, collect=True):
    Path(db).parent.mkdir(parents=True, exist_ok=True)
    lock = open(str(db) + '.collector.lock', 'a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise RuntimeError('A BASIS service already owns this tape')
    store = Store(db)
    engine = Engine(store, config or Config())
    requested_config = engine.config
    restore(engine)
    engine.config = requested_config
    engine.dynamics.config = engine.episodes.config = requested_config
    engine.start_session()
    collector = Collector(engine)
    paper = PaperDesk(engine, Path(db).with_suffix('.paper.sqlite3'))
    paper.start()

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
            path = urlparse(self.path); q = parse_qs(path.query)
            try:
                if path.path == '/api/state':
                    data = engine.snapshot(); data.pop('catalog', None); data['collector'] = dict(running=bool(collector.thread and collector.thread.is_alive()), failure=collector.failed)
                    data['phase2']=dict(enabled=True,mode='experimental_paper',automated_policies=True,research_acceptance='open',reason='Versioned diagnostic policies and manual paper; no real orders')
                    self.send(200, data)
                elif path.path == '/api/wallets':
                    self.send(200,paper.snapshot())
                elif path.path == '/api/review':
                    with paper.lock:self.send(200,paper.performance.report)
                elif path.path == '/api/theses':
                    with paper.lock:self.send(200,dict(rows=[t for t in paper.theses.values() if not q.get('event_id') or t['event_id']==q['event_id'][0]]))
                elif path.path == '/api/equity':
                    self.send(200,paper.curves(float(q.get('hours',[24])[0]),int(q['run_id'][0]) if 'run_id' in q else None))
                elif path.path == '/api/instruments':
                    self.send(200,dict(rows=paper.instruments(q.get('event_id',[None])[0],q.get('asset',[None])[0])))
                elif path.path == '/api/positions':
                    with paper.lock:self.send(200,dict(rows=paper.metrics(q.get('wallet',['OLIVER'])[0])['positions']))
                elif path.path == '/api/history':
                    self.send(200,dict(rows=paper.history(q.get('wallet',['OLIVER'])[0],int(q['run_id'][0]) if 'run_id' in q else None,q.get('limit',[100])[0])))
                elif path.path == '/api/catalog':
                    with engine.lock:
                        self.send(200, dict(rows=list(engine.catalog.values())))
                elif path.path == '/api/health':
                    self.send(200, dict(ok=not collector.failed, collecting=bool(collector.thread and collector.thread.is_alive()), versions=engine.snapshot()['versions']))
                elif path.path == '/api/replay':
                    self.send(200, dict(rows=store.history(q.get('event_id', [None])[0], int(q.get('from', [0])[0]), int(q.get('to', [2**63-1])[0]), int(q.get('limit', [500])[0]), int(q.get('after', [0])[0]), q.get('tail', ['0'])[0] == '1', int(q.get('before', [2**63-1])[0]))))
                elif path.path == '/api/raw':
                    row = store.raw_record(int(q.get('id', [0])[0])); self.send(200 if row else 404, row or dict(error='No such raw record'))
                elif path.path == '/api/episodes':
                    self.send(200, dict(rows=store.latest('episodes', 'gap_event_id', 500)))
                elif path.path == '/api/algos':
                    self.send(200, dict(rows=store.latest('analyzers', 'scope,algo_name', 1000)))
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
                self.send(400, dict(error=str(error)))
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
                else:
                    self.send(404, dict(error='Not found'))
            except (ValueError, TypeError, KeyError) as error:
                self.send(400, dict(error=str(error)))

    httpd = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    if collect:
        collector.start()
    print(f'BASIS research terminal: http://127.0.0.1:{port} | tape {db}', flush=True)
    if __import__('threading').current_thread() is __import__('threading').main_thread():
        def terminate(signum, frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, terminate)
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close(); paper.close(); collector.close()
        engine.save_checkpoint()
        store.close(); lock.close()
