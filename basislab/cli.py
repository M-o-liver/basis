import argparse
import curses
from datetime import datetime, timezone
import json
import textwrap
import time
import urllib.request
from .config import Config
from .engine import Engine
from .replay import verify_replay
from .semantics import timestamp
from .service import serve
from .store import Store, encode
from .tape import open_store


def fetch(base, path):
    with urllib.request.urlopen(base + path, timeout=5) as response:
        return json.load(response)


def post(base,path,payload):
    req=urllib.request.Request(base+path,encode(payload).encode(),{'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req,timeout=10) as response:return json.load(response)
    except urllib.error.HTTPError as error:
        raise SystemExit(json.loads(error.read()).get('error',str(error)))


def monitor_line(row, width=None):
    def prob(value):
        return '--' if value is None else f'{value*100:.2f}%'
    gap = '--' if row.get('gap_pp') is None else f'{row["gap_pp"]:+.2f}'
    if width is not None:
        event=f"{row.get('strike_or_threshold',0):g}{'^' if row.get('direction')=='up' else 'v'}{'T' if row.get('event_type')=='touch' else 'E'}"
        trend={'OPENING FAST':'>>','OPENING':'>','CLOSING FAST':'<<','CLOSING':'<','STABLE':'=','REVERSING':'REV'}.get(row.get('features',{}).get('trend'),'--')
        compact=f"{row['asset']:<5} {event:<11} {prob(row.get('pm_yes')):>7} {prob(row.get('opt_yes')):>7} {gap:>7} {prob(row.get('relative_gap')):>8} {(row.get('side') or '--'):<5} {trend:<3} {row['source_state']:<10}"
        return compact+(f" {(row.get('basis_method') or '--'):<6} {datetime.fromtimestamp(row['expiry']/1000,timezone.utc).strftime('%m-%d %H:%M')}" if width>=100 else '')
    return f"{row['asset']:<6} {row['event_text'][:57]:57} {prob(row.get('pm_yes')):>8} {prob(row.get('opt_yes')):>8} {gap:>8} {prob(row.get('relative_gap')):>9} {(row.get('side') or '--'):<5} {row.get('features', {}).get('trend', '--'):<13} {(row.get('basis_method') or '--'):<6} {row['source_state']}"


def watch(base):
    def run(screen):
        curses.curs_set(0);screen.timeout(1000)
        while True:
            screen.erase()
            try:
                data=fetch(base,'/api/markets')
                screen.addnstr(0,0,'BASIS / MARKETS   q quit',screen.getmaxyx()[1]-1)
                screen.addnstr(1,0,'ASSET EVENT PM OPT GAP pp REL SIDE STATE',screen.getmaxyx()[1]-1)
                for i,row in enumerate(data['rows'][:max(0,screen.getmaxyx()[0]-3)]):
                    row=dict(row,gap_pp=row['math']['gap_pp'],relative_gap=row['math']['relative_gap'],source_state=row.get('execution_state') or 'WAIT')
                    screen.addnstr(i+2,0,monitor_line(row,screen.getmaxyx()[1]),screen.getmaxyx()[1]-1)
            except Exception as error:screen.addnstr(0,0,str(error),screen.getmaxyx()[1]-1)
            screen.refresh()
            if screen.getch() in (ord('q'),3):break
    curses.wrapper(run)


def main():
    parser = argparse.ArgumentParser(description='BASIS source tape, gap math and independent starred signals; never orders')
    parser.add_argument('--db', default='data/basis.sqlite3')
    parser.add_argument('--url', default='http://127.0.0.1:8765')
    commands = parser.add_subparsers(dest='command', required=True)
    acceptance = commands.add_parser('acceptance', help='Bounded persisted operational evidence; does not write the research tape')
    acceptance.add_argument('--json', action='store_true')
    acceptance.add_argument('--deep', action='store_true', help='Larger bounded replay sample; does not rescan frozen legacy data')
    acceptance.add_argument('--quick-check', action='store_true', help='Explicit full v1 SQLite scan; v2 checks its small active segment and reuses closed integrity evidence')
    shadow=commands.add_parser('storage-shadow',help='30–120 minute lossless v2 validation on the existing committed source stream')
    shadow.add_argument('--output',required=True);shadow.add_argument('--minutes',type=float,default=30)
    cutover_parser=commands.add_parser('storage-cutover',help='Initialize validated v2 storage while the legacy writer is stopped; never rewrites history')
    cutover_parser.add_argument('--tape-dir',default='data/tape');cutover_parser.add_argument('--validation',required=True)
    serving = commands.add_parser('serve'); serving.add_argument('--port', type=int, default=8765); serving.add_argument('--config'); serving.add_argument('--no-collect', action='store_true')
    commands.add_parser('watch'); commands.add_parser('status'); commands.add_parser('doctor')
    commands.add_parser('episodes')
    raw = commands.add_parser('raw'); raw.add_argument('id', type=int)
    replay = commands.add_parser('replay'); replay.add_argument('selector', nargs='?'); replay.add_argument('--from', dest='start'); replay.add_argument('--to', dest='end'); replay.add_argument('--verify', action='store_true'); replay.add_argument('--json', action='store_true'); replay.add_argument('--limit', type=int, default=500)
    mapping = commands.add_parser('link'); mapping.add_argument('file', help='JSON mapping; append a versioned operator event')
    gaps=commands.add_parser('gaps',help='Exploratory continuous gap response and actual historical trade math')
    gaps.add_argument('--json',action='store_true');gaps.add_argument('--per-day',type=int,default=1)
    gaps.add_argument('--output',default='data/gap-shape.json');gaps.add_argument('--no-trade-math',action='store_true')
    gaps.add_argument('--from',dest='gap_start');gaps.add_argument('--to',dest='gap_end')
    math_command=commands.add_parser('math');math_command.add_argument('event_id')
    experiment_command=commands.add_parser('journal',help='GUI paperMoney evidence and account record; never orders')
    experiment_command.add_argument('--record',help='Append one local JSON evidence envelope: kind, data, observed_ms')
    experiment_command.add_argument('--output',help='Export a derived JSON summary; original evidence remains immutable')
    experiment_command.add_argument('--full',action='store_true',help='Include every original record in the derived export')
    args = parser.parse_args()
    if args.command=='journal':
        from pathlib import Path
        from .experiment import Experiment
        from .tape import atomic_json
        journal=Experiment(Path(args.db).with_suffix('.experiment.sqlite3'))
        try:
            if args.record:
                record=json.loads(Path(args.record).read_text())
                journal.append(record['kind'],record['data'],record.get('observed_ms'))
            result=journal.snapshot()
            if args.full:result['records']=journal.records()
            if args.output:atomic_json(args.output,result)
            print(json.dumps(result,indent=2))
        finally:journal.close()
        return
    if args.command=='storage-shadow':
        from .storage_shadow import run_shadow
        result=run_shadow(args.db,args.output,args.minutes);print(json.dumps(result,indent=2))
        if not result['accepted']:raise SystemExit(1)
        return
    if args.command=='storage-cutover':
        from .storage_cutover import cutover
        print(json.dumps(cutover(args.db,args.tape_dir,args.validation),indent=2));return
    if args.command == 'acceptance':
        from .acceptance import report, text_report
        data = report(args.db, args.url, args.deep, args.quick_check)
        print(json.dumps(data, indent=2) if args.json else text_report(data)); return
    if args.command == 'serve':
        serve(args.db, args.port, Config.load(args.config), not args.no_collect); return
    if args.command == 'watch': watch(args.url); return
    if args.command=='math':
        from urllib.parse import urlencode
        print(json.dumps(fetch(args.url,'/api/market?'+urlencode(dict(event_id=args.event_id))),indent=2));return
    if args.command=='gaps':
        from .gap_history import study,text_report
        from .gap_trades import historical_trades
        from .tape import atomic_json
        if not 0<=args.per_day<=24:parser.error('--per-day must be 0..24')
        data=study(args.db,args.per_day,start=timestamp(args.gap_start) if args.gap_start else None,end=timestamp(args.gap_end) if args.gap_end else None,progress=not args.json)
        openings=data.pop('_trade_openings')
        if not args.no_trade_math:data['trade_math']=historical_trades(args.db,openings,progress=not args.json)
        atomic_json(args.output,data)
        print(json.dumps(data,indent=2) if args.json else text_report(data));return
    if args.command == 'link':
        payload = open(args.file, 'rb').read()
        request = urllib.request.Request(args.url+'/api/mapping', payload, {'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=10) as response: print(response.read().decode())
        return
    if args.command in ('status', 'doctor'):
        data = fetch(args.url, '/api/state')
        print(json.dumps({k: data[k] for k in ('sources', 'stats', 'diagnostics', 'collector', 'versions')}, indent=2))
        if args.command == 'doctor':
            store = open_store(args.db,read_only=True)
            try:
                with store.lock: print('sqlite_quick_check:',store.integrity() if hasattr(store,'integrity') else store.db.execute('PRAGMA quick_check').fetchone()[0])
            finally: store.close()
        return
    store = open_store(args.db,read_only=True)
    try:
        if args.command == 'raw': print(json.dumps(store.raw_record(args.id), indent=2)); return
        if args.command == 'episodes': print(json.dumps(store.latest('episodes', 'gap_event_id', 1000), indent=2)); return
        if args.verify:
            result = verify_replay(store, timestamp(args.end) if args.end else None)
            print(json.dumps(result, indent=2))
            if not result['ok']: raise SystemExit(1)
            return
        selector = args.selector; start = timestamp(args.start) if args.start else 0; end = timestamp(args.end) if args.end else 2**63-1
        if start is None or end is None: parser.error('Use ISO timestamps with explicit timezone, e.g. 2026-09-26T12:00:00Z')
        event_id = selector
        if selector and selector.startswith('gap-'):
            episode = next((x for x in store.latest('episodes', 'gap_event_id', 10000) if x['gap_event_id'] == selector), None)
            if not episode: parser.error('Unknown gap event')
            event_id = episode['event_id']; start = max(start, episode['opened_at']); end = min(end, episode.get('closing_time') or end)
        elif selector:
            matches = [(r['event_id'],) for r in store.latest('observations', 'event_id', 10000) if r['event_id'] == selector or f"{r['asset']}-{r['strike_or_threshold']:g}".upper() == selector.upper()]
            if len(matches) > 1: parser.error('Ambiguous asset/strike; use an event_id: ' + ', '.join(r[0] for r in matches))
            if matches: event_id = matches[0][0]
        rows = store.history(event_id, start, end, args.limit)
        for row in rows:
            if args.json: print(encode(row))
            else:
                stamp = datetime.fromtimestamp(row['timestamp_wall']/1000, timezone.utc).isoformat(timespec='milliseconds')
                print(stamp, monitor_line(row))
    finally:
        store.close()
