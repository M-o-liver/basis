"""Append-only evidence from GUI paperMoney sessions; never submits orders.

Stars and historical simulations remain independent. Unknown execution costs
stay unknown. The first observed account balance is never replaced on restart.
"""
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time

from .semantics import number
from .store import encode

VERSION = 'experiment-journal-1.2'


def execution_identity(data, kind='FILL'):
    """Never invent an external ID when a GUI only exposes execution evidence."""
    field = 'broker_fill_id' if kind == 'FILL' else 'broker_order_id'
    if field not in data:
        raise ValueError('Report broker identifier or explicitly mark it unknown')
    value = data[field]
    if value is not None:
        if not isinstance(value, str) or not value.strip():
            raise ValueError('Invalid broker identifier')
        return ('broker', value)
    local = data.get('local_fill_ref' if kind == 'FILL' else 'local_order_ref')
    if data.get('broker_id_status') != 'NOT_EXPOSED_IN_GUI' or not isinstance(local, str) or not local.strip():
        raise ValueError('Unknown broker ID needs explicit status and a local evidence reference')
    return ('local', local)
KINDS = ('MANDATE', 'ACCOUNT', 'RESEARCH', 'REVISION', 'STATE', 'PLAN', 'ORDER', 'FILL', 'COST', 'MARK', 'OUTCOME')


def required(data, *fields):
    if any(data.get(k) is None or data.get(k) == '' for k in fields):
        raise ValueError('Required evidence: ' + ', '.join(fields))


def numeric(data, field, minimum=None):
    value = number(data.get(field))
    if value is None or (minimum is not None and value < minimum):
        raise ValueError('Invalid ' + field)
    return value


def payoff_budget(snapshot):
    """Audit affordability under the active directive; this does not size orders."""
    if (snapshot.get('objective') or {}).get('risk_budget') == 'VENUE_BUYING_POWER':
        return (snapshot.get('current') or {}).get('option_buying_power')
    baseline = snapshot.get('baseline')
    return baseline['equity'] * .005 if baseline else None


class Experiment:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, timeout=10, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS evidence(
            id INTEGER PRIMARY KEY, version TEXT NOT NULL, kind TEXT NOT NULL, recorded_ms INTEGER NOT NULL,
            observed_ms INTEGER NOT NULL, data TEXT NOT NULL,
            previous_hash TEXT NOT NULL, sha256 TEXT NOT NULL UNIQUE);
          CREATE TRIGGER IF NOT EXISTS immutable_evidence_update BEFORE UPDATE ON evidence
            BEGIN SELECT RAISE(ABORT,'immutable experiment evidence'); END;
          CREATE TRIGGER IF NOT EXISTS immutable_evidence_delete BEFORE DELETE ON evidence
            BEGIN SELECT RAISE(ABORT,'immutable experiment evidence'); END;
        ''')

    def close(self):
        with self.lock:
            self.db.close()

    def records(self):
        return [dict(id=r['id'], version=r['version'], kind=r['kind'], recorded_ms=r['recorded_ms'],
                     observed_ms=r['observed_ms'], data=json.loads(r['data']),
                     previous_hash=r['previous_hash'], sha256=r['sha256'])
                for r in self.db.execute('SELECT * FROM evidence ORDER BY id')]

    def validate(self, kind, data, rows):
        if kind not in KINDS or not isinstance(data, dict):
            raise ValueError('Unknown experiment record kind')
        required(data, 'evidence')
        if kind == 'MANDATE':
            if any(r['kind'] == kind for r in rows):
                raise ValueError('The experiment mandate cannot be reset')
            required(data, 'start_ms', 'end_ms', 'git_revision', 'objective')
            if numeric(data, 'end_ms') <= numeric(data, 'start_ms'):
                raise ValueError('Experiment must have a future end')
        elif kind == 'ACCOUNT':
            required(data, 'account', 'quote_status')
            if data.get('paper_money_visible') is not True:
                raise ValueError('Visible paperMoney identity required')
            for field in ('equity', 'option_buying_power', 'cash'):
                numeric(data, field, 0)
            previous = [r['data'] for r in rows if r['kind'] == kind]
            if previous and data['account'] != previous[0]['account']:
                raise ValueError('Do not combine a different account with this baseline')
        elif kind == 'REVISION':
            required(data, 'git_revision', 'reason', 'historical_result', 'forward_status')
            if 'objective_update' in data:
                objective = data['objective_update']
                required(objective, 'description', 'primary_metric', 'effective_ms', 'strategy_version',
                         'sizing_policy', 'holding_policy')
                numeric(objective, 'effective_ms', 1)
                if objective['primary_metric'] not in ('SAMPLED_PEAK_EQUITY', 'FINAL_EQUITY'):
                    raise ValueError('Unknown portfolio objective')
                if objective.get('risk_budget') not in (None, 'VENUE_BUYING_POWER', 'INITIAL_PROTOCOL_CAP'):
                    raise ValueError('Unknown affordability budget')
        elif kind == 'STATE':
            required(data, 'activity', 'finding', 'next_action', 'strategy_version', 'coverage')
        elif kind in ('RESEARCH', 'OUTCOME'):
            required(data, 'finding')
        elif kind == 'COST':
            identity = execution_identity(data)
            numeric(data, 'fees', 0)
            if not any(r['kind']=='FILL' and execution_identity(r['data'])==identity for r in rows):
                raise ValueError('Actual cost must refer to a recorded broker fill')
        elif kind == 'PLAN':
            required(data, 'trade_id', 'signal', 'legs', 'hypothesis', 'entry_criteria',
                     'exit_conditions', 'model_version', 'strategy_version', 'signal_category')
            if any(r['kind'] == kind and r['data']['trade_id'] == data['trade_id'] for r in rows):
                raise ValueError('Trade plan is already frozen; append a new hypothesis')
            signal = data['signal']
            required(signal, 'event_id', 'raw_id', 'timestamp_wall', 'input_refs')
            numeric(data, 'max_loss', 0)
            if not isinstance(data['legs'], list) or not data['legs']:
                raise ValueError('Exact option contracts required')
            for leg in data['legs']:
                required(leg, 'instrument', 'side', 'expiry', 'ratio')
                if leg['side'] not in ('BUY', 'SELL') or numeric(leg, 'ratio', 1) % 1:
                    raise ValueError('Invalid exact-contract leg')
        elif kind in ('ORDER', 'FILL', 'MARK'):
            required(data, 'trade_id')
            plans = [r['data'] for r in rows if r['kind'] == 'PLAN' and r['data']['trade_id'] == data['trade_id']]
            if not plans:
                raise ValueError('Freeze a BASIS-derived plan before recording execution')
            if kind == 'ORDER':
                execution_identity(data, 'ORDER')
                required(data, 'status')
            elif kind == 'MARK':
                numeric(data, 'unit_credit')
                required(data, 'quote_status')
            else:
                identity = execution_identity(data)
                required(data, 'action', 'contracts')
                if 'fees' not in data:
                    raise ValueError('Report actual fees or explicitly mark them unknown with null')
                if data['action'] not in ('OPEN', 'CLOSE'):
                    raise ValueError('Fill action must be OPEN or CLOSE')
                quantity = numeric(data, 'quantity', 1)
                if quantity % 1:
                    raise ValueError('Fill quantity must be whole structures')
                numeric(data, 'unit_cash', 0 if data['action'] == 'OPEN' else None)
                if data['fees'] is not None:
                    numeric(data, 'fees', 0)
                if sorted(data['contracts']) != sorted(l['instrument'] for l in plans[0]['legs']):
                    raise ValueError('Fill must use the frozen exact contracts')
                if any(r['kind'] == 'FILL' and execution_identity(r['data']) == identity for r in rows):
                    raise ValueError('Broker fill already recorded')
                fills = [r['data'] for r in rows if r['kind'] == 'FILL' and r['data']['trade_id'] == data['trade_id']]
                position = sum(f['quantity'] * (1 if f['action'] == 'OPEN' else -1) for f in fills)
                if data['action'] == 'CLOSE' and quantity > position:
                    raise ValueError('Cannot close more than the recorded filled position')

    def append(self, kind, data, observed_ms=None):
        now = time.time_ns() // 1000000
        observed_ms = now if observed_ms is None else observed_ms
        if isinstance(observed_ms, bool) or not isinstance(observed_ms, int) or not 0 < observed_ms <= now:
            raise ValueError('Observation time must be explicit, positive and not in the future')
        # JSON rejects NaN/Infinity, and no binary credentials or raw screenshots belong here.
        payload = encode(data)
        if len(payload.encode()) > 65536:
            raise ValueError('Keep the journal compact; link large local evidence artifacts')
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                rows = self.records()
                self.validate(kind, data, rows)
                if kind == 'ACCOUNT' and any(r['kind'] == kind and r['observed_ms'] >= observed_ms for r in rows):
                    raise ValueError('Account observations must advance in time')
                previous = rows[-1]['sha256'] if rows else '0' * 64
                digest = hashlib.sha256(encode([VERSION, kind, now, observed_ms, data, previous]).encode()).hexdigest()
                result = self.db.execute('INSERT INTO evidence(version,kind,recorded_ms,observed_ms,data,previous_hash,sha256) VALUES(?,?,?,?,?,?,?)',
                                         (VERSION, kind, now, observed_ms, payload, previous, digest))
                self.db.execute('COMMIT')
                return dict(id=result.lastrowid, sha256=digest, recorded_ms=now)
            except Exception:
                self.db.execute('ROLLBACK')
                raise

    def snapshot(self):
        as_of_ms = time.time_ns() // 1000000
        with self.lock:
            rows = self.records()
        previous = '0' * 64
        for row in rows:
            expected = hashlib.sha256(encode([row['version'], row['kind'], row['recorded_ms'], row['observed_ms'], row['data'], previous]).encode()).hexdigest()
            if row['previous_hash'] != previous or row['sha256'] != expected:
                raise ValueError('Experiment evidence hash chain failed at record ' + str(row['id']))
            previous = expected
        accounts = [dict(r['data'], observed_ms=r['observed_ms']) for r in rows if r['kind'] == 'ACCOUNT']
        baseline, current = (accounts[0], accounts[-1]) if accounts else (None, None)
        mandate = next((r['data'] for r in rows if r['kind'] == 'MANDATE'), None)
        window = dict(start_ms=mandate['start_ms'], end_ms=mandate['end_ms']) if mandate else None
        score_accounts = [a for a in accounts if a['observed_ms'] <= as_of_ms and
                          (not window or window['start_ms'] <= a['observed_ms'] <= window['end_ms'])]
        peak_account = max(score_accounts, key=lambda a: a['equity']) if score_accounts else None
        objective = dict(description=mandate['objective'], effective_ms=mandate['start_ms']) if mandate else None
        for row in rows:
            update = row['data'].get('objective_update') if row['kind'] == 'REVISION' else None
            if update and update['effective_ms'] <= as_of_ms:
                objective = dict(update, evidence_record_id=row['id'])
        states = [dict(r['data'], observed_ms=r['observed_ms'], evidence_record_id=r['id'])
                  for r in rows if r['kind'] == 'STATE' and r['observed_ms'] <= as_of_ms]
        peak = baseline['equity'] if baseline else 0
        drawdown = 0
        for account in accounts:
            peak = max(peak, account['equity'])
            drawdown = max(drawdown, peak - account['equity'])
        plans = {r['data']['trade_id']: r['data'] for r in rows if r['kind'] == 'PLAN'}
        costs_by_fill = {execution_identity(r['data']):r['data']['fees'] for r in rows if r['kind']=='COST'}
        trades = []
        for identity, plan in plans.items():
            fills = [r['data'] for r in rows if r['kind'] == 'FILL' and r['data']['trade_id'] == identity]
            lots, gross, net, costs = [], 0, 0, 0
            for fill in fills:
                quantity, fee = fill['quantity'], costs_by_fill.get(execution_identity(fill),fill['fees'])
                costs = costs + fee if costs is not None and fee is not None else None
                if fill['action'] == 'OPEN':
                    lots.append(dict(quantity=quantity, price=fill['unit_cash'], fee=None if fee is None else fee / quantity))
                    continue
                remaining = quantity
                for lot in lots:
                    closed = min(remaining, lot['quantity'])
                    if not closed:
                        continue
                    pnl = closed * (fill['unit_cash'] - lot['price'])
                    gross += pnl
                    net = net + pnl - closed * (lot['fee'] + fee / quantity) if net is not None and lot['fee'] is not None and fee is not None else None
                    lot['quantity'] -= closed
                    remaining -= closed
            position = sum(l['quantity'] for l in lots)
            marks = [r['data'] for r in rows if r['kind'] == 'MARK' and r['data']['trade_id'] == identity]
            unrealized = sum(l['quantity'] * (marks[-1]['unit_credit'] - l['price']) for l in lots) if marks else (None if position else 0)
            trades.append(dict(trade_id=identity, category=plan['signal_category'], model_version=plan['model_version'],
                               strategy_version=plan['strategy_version'], legs=plan['legs'], fill_count=len(fills), open_quantity=position,
                               completed=bool(fills) and position == 0, realized_gross=gross, realized_net=net,
                               actual_fees=costs, unrealized_gross_at_last_quote=unrealized))
        def total(field):
            values = [t[field] for t in trades if t['fill_count']]
            return None if any(v is None for v in values) else sum(values)
        def grouped(field):
            result={}
            for trade in trades:
                if not trade['fill_count']:continue
                group=result.setdefault(trade[field],dict(trades=0,completed=0,realized_gross=0,realized_net=0))
                group['trades']+=1;group['completed']+=trade['completed'];group['realized_gross']+=trade['realized_gross']
                group['realized_net']=group['realized_net']+trade['realized_net'] if group['realized_net'] is not None and trade['realized_net'] is not None else None
            return result
        return dict(version=VERSION, baseline=baseline, current=current,
                    objective=objective, work_state=states[-1] if states else None, window=window,
                    sampled_peak_equity=peak_account['equity'] if peak_account else None,
                    sampled_peak_observed_ms=peak_account['observed_ms'] if peak_account else None,
                    sampled_peak_gain=None if not peak_account or not baseline else peak_account['equity'] - baseline['equity'],
                    window_last_account=score_accounts[-1] if score_accounts else None,
                    equity_change=None if not baseline else current['equity'] - baseline['equity'],
                    sampled_max_drawdown=None if not baseline else drawdown,
                    trade_count=sum(bool(t['fill_count']) for t in trades), completed_trades=sum(t['completed'] for t in trades),
                    realized_gross=total('realized_gross'), realized_net=total('realized_net'), actual_fees=total('actual_fees'),
                    trades=trades, by_signal_category=grouped('category'),by_model=grouped('model_version'),by_strategy=grouped('strategy_version'),
                    record_count=len(rows), head_hash=previous, records=rows[-40:],
                    measurement='GUI-reported account equity; actual broker fills only. Quote marks and unknown costs are separate. Peak is the highest observed account sample inside the original experiment window, including the baseline when in that window; unseen intraday peaks are unknown. Drawdown uses observed account samples.')
