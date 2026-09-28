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
        curses.curs_set(0); screen.timeout(100)
        selected, paused, details, view, filter_text, scroll = 0, False, False, 'monitor', '', 0
        rows, data, error, next_poll = [], {}, '', 0
        while True:
            now = time.monotonic()
            if now >= next_poll and not paused:
                try:
                    identity = rows[selected].get('event_id') if rows and 0 <= selected < len(rows) and view == 'monitor' else None
                    data = fetch(base, '/api/state')
                    if view == 'monitor':
                        rows = sorted(data['rows'], key=lambda r: (r.get('gap_pp') is None, -abs(r.get('gap_pp') or 0)))
                        rows = [r for r in rows if filter_text.lower() in (r['asset'] + ' ' + r['event_text']).lower()]
                    else:
                        rows = fetch(base, '/api/' + view)['rows'] if view in ('algos', 'episodes','wallets','positions','history') else [dict(source=key,**value) for key,value in data['sources'].items()]
                    if identity:
                        selected = next((i for i, row in enumerate(rows) if row.get('event_id') == identity), selected)
                    error = ''
                except Exception as exc:
                    error = str(exc)
                next_poll = now + 1
            screen.erase(); height, width = screen.getmaxyx()
            def put(y, value, attr=0):
                if 0 <= y < height:
                    try: screen.addnstr(y, 0, value, max(0, width-1), attr)
                    except curses.error: pass
            put(0, f'BASIS  POLYMARKET -> CONVENTIONAL MARKETS   {"PAUSED" if paused else "LIVE"}  [{view.upper()}]', curses.A_BOLD)
            put(1, '1 monitor 2 gaps 3 algos 4 health 8 wallets 9 positions 0 ledger | Enter | q quit')
            put(2, error or f"Tape {data.get('stats', {}).get('raw', 0)} raw / {data.get('stats', {}).get('observations', 0)} observations   Filter: {filter_text}")
            selected = max(0, min(selected, len(rows)-1))
            if details and rows:
                lines = json.dumps(rows[selected], indent=2).splitlines()
                lines = [part for line in lines for part in textwrap.wrap(line, max(1, width-2), replace_whitespace=False) or ['']]
                scroll = max(0, min(scroll, max(0,len(lines)-(height-5))))
                put(3, f'j/k or PgUp/PgDn scroll   Esc back   lines {scroll+1}-{min(len(lines),scroll+height-5)} / {len(lines)}', curses.A_UNDERLINE)
                for index, line in enumerate(lines[scroll:scroll+height-5]): put(index+4, line)
            else:
                heading = 'ASSET EVENT           PM     OPT  GAP pp      REL SIDE  DIR STATE      '+(' BASIS  END UTC' if width>=100 else '') if view == 'monitor' else 'Select a record and press Enter for scrollable diagnostics'
                put(3, heading, curses.A_UNDERLINE)
                start = max(0, selected-(height-6))
                for i, row in enumerate(rows[start:start+height-5], start):
                    value = monitor_line(row,width) if view == 'monitor' else (f"{row['family']:<13} {row['algo_name']:<24} {row['market_scope']:<10} {row['status']:<18} {row['explanation']}" if view == 'algos' else f"{row['gap_event_id']}  {row['state']}  PEAK {row['max_abs_gap_pp']:.2f}pp" if view == 'episodes' else f"{row['source']:<16} {row['state']:<9} {row.get('age_ms',0)/1000:>5.0f}s  {row['detail']}" if view=='health' else '')
                    if view=='wallets':value=f"{row['wallet_id']:<14} RUN {row['run_number']:03}  ${row['equity']:>11,.2f}  {row['return_pct']:+6.2f}%  DD {row['max_drawdown']*100:.2f}%  {row['trades']} fills"
                    elif view=='positions':value=f"{row['instrument']:<27} {row['quantity']:g}  ${row['market_value']:,.2f}  PNL {row['unrealized_pnl']:+.2f}  {row['mark_status']}"
                    elif view=='history':value=f"{row['kind']:<9} {row.get('order',{}).get('instrument','')} {row.get('reason') or row.get('order',{}).get('reason','')}"
                    put(i-start+4, value, curses.A_REVERSE if i == selected else 0)
            screen.refresh()
            key = screen.getch()
            if key in (ord('q'), 3): break
            if key in (ord('j'), curses.KEY_DOWN):
                if details: scroll += 1
                else: selected = min(len(rows)-1, selected+1)
            elif key in (ord('k'), curses.KEY_UP):
                if details: scroll = max(0,scroll-1)
                else: selected = max(0, selected-1)
            elif key == curses.KEY_NPAGE: scroll += max(1,height-6) if details else 0
            elif key == curses.KEY_PPAGE: scroll = max(0,scroll-max(1,height-6))
            elif key in (10, 13): details = not details; scroll = 0
            elif key == 27: details = False; scroll = 0; filter_text = ''; next_poll = 0
            elif key == ord('p'): paused = not paused
            elif key == ord('r'): next_poll = 0
            elif key in map(ord, '1234890'):
                view = dict(zip(map(ord, '1234890'), ('monitor', 'episodes', 'algos', 'health','wallets','positions','history')))[key]; selected = 0; details = False; next_poll = 0
            elif key == ord('/'):
                curses.echo(); screen.timeout(-1); put(height-1, '/ '); screen.refresh()
                try: filter_text = screen.getstr(height-1, 2, 80).decode()
                finally: curses.noecho(); screen.timeout(100)
                next_poll = 0; selected = 0
    curses.wrapper(run)


def main():
    parser = argparse.ArgumentParser(description='BASIS research and isolated paper execution; never real orders')
    parser.add_argument('--db', default='data/basis.sqlite3')
    parser.add_argument('--url', default='http://127.0.0.1:8765')
    commands = parser.add_subparsers(dest='command', required=True)
    serving = commands.add_parser('serve'); serving.add_argument('--port', type=int, default=8765); serving.add_argument('--config'); serving.add_argument('--no-collect', action='store_true')
    commands.add_parser('watch'); commands.add_parser('status'); commands.add_parser('doctor')
    commands.add_parser('algos'); commands.add_parser('episodes')
    raw = commands.add_parser('raw'); raw.add_argument('id', type=int)
    replay = commands.add_parser('replay'); replay.add_argument('selector', nargs='?'); replay.add_argument('--from', dest='start'); replay.add_argument('--to', dest='end'); replay.add_argument('--verify', action='store_true'); replay.add_argument('--json', action='store_true'); replay.add_argument('--limit', type=int, default=500)
    mapping = commands.add_parser('link'); mapping.add_argument('file', help='JSON mapping; append a versioned operator event')
    wallet=commands.add_parser('wallet');wallet.add_argument('action',nargs='?',default='show',choices=('show','reset'));wallet.add_argument('name',nargs='?',default='OLIVER')
    commands.add_parser('wallets');commands.add_parser('review')
    thesis=commands.add_parser('thesis');thesis.add_argument('event_id',nargs='?')
    for name in ('positions','history'):
        p=commands.add_parser(name);p.add_argument('--wallet',default='OLIVER');p.add_argument('--run-id',type=int)
    instruments=commands.add_parser('instruments');instruments.add_argument('asset',nargs='?',default='ETH');instruments.add_argument('--event')
    for name in ('buy','sell','trade'):
        p=commands.add_parser(name);p.add_argument('instrument');p.add_argument('quantity',type=float);p.add_argument('--wallet',default='OLIVER')
        p.add_argument('--side',choices=('BUY','SELL'),default='BUY');p.add_argument('--reason',required=True);p.add_argument('--event');p.add_argument('--preview',action='store_true')
    p=commands.add_parser('close');p.add_argument('instrument');p.add_argument('--wallet',default='OLIVER');p.add_argument('--reason',required=True)
    p=commands.add_parser('settle');p.add_argument('instrument');p.add_argument('raw_id',type=int);p.add_argument('--wallet',default='OLIVER');p.add_argument('--reason',required=True)
    p=commands.add_parser('paper-replay');p.add_argument('plan');p.add_argument('--output',required=True)
    p=commands.add_parser('automation');p.add_argument('action',choices=('pause','resume'))
    args = parser.parse_args()
    if args.command == 'serve':
        serve(args.db, args.port, Config.load(args.config), not args.no_collect); return
    if args.command == 'watch': watch(args.url); return
    if args.command=='thesis':
        from urllib.parse import urlencode
        print(json.dumps(fetch(args.url,'/api/theses'+('?'+urlencode(dict(event_id=args.event_id)) if args.event_id else '')),indent=2));return
    if args.command=='review':
        data=fetch(args.url,'/api/review')
        print('WALLET          CLOSES     NET PNL    MID MOVE     SPREAD       FEES       SLIP')
        for wallet,row in data.get('wallets',{}).items():
            print(f"{wallet:<16}{row['closes']:>6} {row['net_pnl']:>11.2f} {row['mid_move_pnl']:>11.2f} {row['spread_cost']:>10.2f} {row['fees']:>10.2f} {row['slippage']:>10.2f}")
        print('Midpoint movement is attribution, not an executable return. Reviews include all retained runs.')
        print('Shared-entry groups:',data.get('shared_entry_groups',0),'Recording gaps:',len(data.get('recording_gaps',[])))
        return
    if args.command=='automation':
        print(json.dumps(post(args.url,'/api/automation',dict(enabled=args.action=='resume')),indent=2));return
    if args.command=='paper-replay':
        from .paper_replay import replay_orders
        print(json.dumps(replay_orders(args.db,args.plan,args.output),indent=2));return
    if args.command in ('wallet','wallets'):
        result=post(args.url,'/api/wallet/reset',dict(wallet=args.name)) if args.command=='wallet' and args.action=='reset' else fetch(args.url,'/api/wallets')
        print(json.dumps(result,indent=2));return
    if args.command in ('positions','history'):
        path='/api/'+args.command+'?wallet='+args.wallet+('&run_id='+str(args.run_id) if args.run_id else '')
        print(json.dumps(fetch(args.url,path),indent=2));return
    if args.command=='instruments':
        from urllib.parse import urlencode
        print(json.dumps(fetch(args.url,'/api/instruments?'+urlencode(dict(event_id=args.event) if args.event else dict(asset=args.asset))),indent=2));return
    if args.command in ('buy','sell','trade'):
        payload=dict(wallet=args.wallet,instrument=args.instrument,quantity=args.quantity,side=args.command.upper() if args.command!='trade' else args.side,reason=args.reason,basis_event_id=args.event)
        print(json.dumps(post(args.url,'/api/preview' if args.preview else '/api/trade',payload),indent=2));return
    if args.command in ('close','settle'):
        payload=dict(wallet=args.wallet,instrument=args.instrument,reason=args.reason)
        if args.command=='settle':payload['raw_id']=args.raw_id
        print(json.dumps(post(args.url,'/api/'+args.command,payload),indent=2));return
    if args.command == 'link':
        payload = open(args.file, 'rb').read()
        request = urllib.request.Request(args.url+'/api/mapping', payload, {'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=10) as response: print(response.read().decode())
        return
    if args.command in ('status', 'doctor'):
        data = fetch(args.url, '/api/state')
        print(json.dumps({k: data[k] for k in ('sources', 'stats', 'diagnostics', 'collector', 'versions', 'phase2')}, indent=2))
        if args.command == 'doctor':
            store = Store(args.db)
            try:
                with store.lock: print('sqlite_quick_check:', store.db.execute('PRAGMA quick_check').fetchone()[0])
            finally: store.close()
        return
    store = Store(args.db)
    try:
        if args.command == 'raw': print(json.dumps(store.raw_record(args.id), indent=2)); return
        if args.command == 'algos': print(json.dumps(store.latest('analyzers', 'scope,algo_name', 1000), indent=2)); return
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
