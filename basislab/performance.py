"""Causal paper-trade review: actual fills, explicit costs, no fitted backtest scores."""
from collections import Counter, defaultdict
import json

VERSION = 'paper-review-1.0.0'


def review_fills(records, now):
    inventory = {}
    closed = []
    duplicates = defaultdict(set)
    for record in records:
        if record['timestamp_fill'] > now:
            continue
        order, quote, fill = record['order'], record['quote'], record['fill']
        key = (order['run_id'], order['instrument'])
        quantity = order['quantity']
        units = quantity * quote['multiplier']
        if order['side'] == 'BUY':
            lot = inventory.setdefault(key, dict(quantity=0, mid=0, spread=0, fees=0, slippage=0,
                debit=0, opened_at=record['timestamp_fill'], entry_ids=[], strategy_version=order['strategy_version']))
            lot['quantity'] += quantity
            lot['mid'] += units * (quote['bid'] + quote['ask']) / 2
            lot['spread'] += units * (quote['ask'] - quote['bid']) / 2
            lot['fees'] += fill['fees']
            lot['slippage'] += fill['slippage']
            lot['debit'] += fill['total_debit']
            lot['entry_ids'].append(record['ledger_id'])
            duplicates[(order['instrument'], quantity, order['timestamp_submit']//15000)].add(order['wallet_id'])
            continue
        lot = inventory.get(key)
        if not lot or lot['quantity'] < quantity - 1e-9:
            closed.append(dict(wallet_id=order['wallet_id'], run_id=order['run_id'], ledger_id=record['ledger_id'],
                status='missing_entry', explanation='No matched entry; attribution unavailable'))
            continue
        fraction = min(1, quantity / lot['quantity'])
        mid = units * (quote['bid'] + quote['ask']) / 2 - lot['mid'] * fraction
        spread = units * (quote['ask'] - quote['bid']) / 2 + lot['spread'] * fraction
        fees = fill['fees'] + lot['fees'] * fraction
        slippage = fill['slippage'] + lot['slippage'] * fraction
        debit = lot['debit'] * fraction
        net = record['realized_pnl']
        residual = net - (mid - spread - fees - slippage)
        closed.append(dict(wallet_id=order['wallet_id'], run_id=order['run_id'], ledger_id=record['ledger_id'],
            entry_ids=list(lot['entry_ids']), instrument=order['instrument'], asset=quote['asset'],
            strategy_version=lot['strategy_version'], status='ok' if abs(residual)<.00001 else 'unreconciled',
            reconciliation_error=residual, opened_at=lot['opened_at'], closed_at=record['timestamp_fill'],
            held_ms=record['timestamp_fill']-lot['opened_at'], net_pnl=net, mid_move_pnl=mid,
            spread_cost=spread, fees=fees, slippage=slippage, invested=debit,
            net_return=net/debit if debit else None, exit_reason=order['reason'],
            complete=record.get('position_after') is None))
        for name in ('quantity','mid','spread','fees','slippage','debit'):
            lot[name] *= 1-fraction
        if lot['quantity'] < 1e-9:
            del inventory[key]
    groups = defaultdict(list)
    for row in closed:
        groups[row['wallet_id']].append(row)
    def summarize(rows):
        valid = [r for r in rows if r['status']=='ok']
        totals = {k: sum(r[k] for r in valid) for k in ('net_pnl','mid_move_pnl','spread_cost','fees','slippage')}
        return dict(totals, closes=len(valid), unreconciled=len(rows)-len(valid),
            over_two_hours=sum(r['held_ms']>7200000 for r in valid),
            long_hold_pnl=sum(r['net_pnl'] for r in valid if r['held_ms']>7200000),
            exit_reasons=dict(Counter(r['exit_reason'] for r in valid)))
    return dict(version=VERSION, as_of=now, totals=summarize(closed),
        wallets={w:summarize(rs) for w,rs in groups.items()}, closed=closed,
        shared_entry_groups=sum(len(v)>1 for v in duplicates.values()),
        identical_entry_pairs=[dict(wallets=list(pair), shared_entries=count) for pair,count in
            sorted(Counter((a,b) for group in duplicates.values() for a in sorted(group) for b in sorted(group) if a<b).items())],
        interpretation='Midpoint movement is attribution, never an executable backtest. Shared trades are not independent evidence.')


def feedback(review, wallet, run_id, now):
    """Fixed, prospective risk brake. No parameter search or profit guarantee."""
    rows = [r for r in review.get('closed',[]) if r['wallet_id']==wallet and r['run_id']==run_id
            and r['status']=='ok' and r['complete'] and r['closed_at']<=now]
    recent = rows[-5:]
    losing = (len(recent)>=3 and all(r['net_pnl']<0 for r in recent[-3:])) or (len(recent)==5 and sum(r['net_pnl'] for r in recent)<0)
    until = recent[-1]['closed_at']+3600000 if losing else 0
    return dict(version=VERSION, as_of=now, sample_count=len(rows), recent_net_pnl=sum(r['net_pnl'] for r in recent),
        last_close_id=recent[-1]['ledger_id'] if recent else None, entry_allowed=now>=until,
        cooldown_until=until, size_multiplier=.5 if losing else 1.,
        reason='Loss feedback: one-hour entry brake, then half-size probes' if losing else 'Collecting prospective outcomes; no edge established')


class PerformanceReview:
    def __init__(self, desk):
        self.desk = desk
        self.report = {}
        self.last_fill_id = None
        desk.db.execute('CREATE INDEX IF NOT EXISTS paper_kind_id ON ledger(kind,id)')
        desk.db.execute('CREATE TABLE IF NOT EXISTS paper_reviews(id INTEGER PRIMARY KEY,timestamp_ms INTEGER NOT NULL,as_of_ledger_id INTEGER NOT NULL,version TEXT NOT NULL,data TEXT NOT NULL)')
        for op in ('UPDATE','DELETE'):
            desk.db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_paper_reviews_{op} BEFORE {op} ON paper_reviews BEGIN SELECT RAISE(ABORT,'immutable paper review'); END")

    def refresh(self, now):
        desk = self.desk
        latest = desk.db.execute("SELECT MAX(id) FROM ledger WHERE kind='FILL' AND timestamp_ms<=?",(now,)).fetchone()[0] or 0
        if latest == self.last_fill_id:
            return self.report
        records = []
        for row in desk.db.execute("SELECT id,data FROM ledger WHERE kind='FILL' AND timestamp_ms<=? ORDER BY id",(now,)):
            data=json.loads(row['data']);data.pop('state_after',None);data['ledger_id']=row['id'];records.append(data)
        self.report=review_fills(records,now)
        self.report['as_of_ledger_id']=latest
        self.report['recording_gaps']=[dict(start=r[0],end=r[1],duration_ms=r[1]-r[0]) for r in desk.db.execute('''
            SELECT previous,timestamp_ms FROM (
              SELECT lag(timestamp_ms) OVER (ORDER BY timestamp_ms,id) previous,timestamp_ms
              FROM equity_points WHERE run_id IN (SELECT id FROM runs WHERE wallet='OLIVER') AND timestamp_ms<=?
            ) WHERE timestamp_ms-previous>90000 ORDER BY timestamp_ms DESC LIMIT 20''',(now,))]
        desk.db.execute('INSERT INTO paper_reviews(timestamp_ms,as_of_ledger_id,version,data) VALUES(?,?,?,?)',
            (now,latest,VERSION,json.dumps(self.report,allow_nan=False,sort_keys=True)))
        self.last_fill_id=latest
        return self.report
