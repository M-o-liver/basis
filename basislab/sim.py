"""One funded account. Operator submits an exact displayed defined-risk structure."""
import copy
import json
import math
from pathlib import Path
import sqlite3
import threading
import time
import uuid
from .gap_math import execution_cost
from .market_math import execution_quote
from .store import encode


class Sim:
    def __init__(self,math_engine,path):
        self.math=math_engine;self.lock=threading.RLock();self.stop=threading.Event();self.thread=None
        self.db=sqlite3.connect(path,timeout=10,check_same_thread=False,isolation_level=None)
        self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL');self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS ledger(id INTEGER PRIMARY KEY,timestamp_ms INTEGER,kind TEXT,order_id TEXT,data TEXT)')
        for op in ('UPDATE','DELETE'):
            self.db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_sim_{op} BEFORE {op} ON ledger BEGIN SELECT RAISE(ABORT,'immutable BASIS SIM ledger'); END")
        self.cash=100000.;self.positions={};self.pending={};self.results={};self.error=None
        rows=self.db.execute('SELECT kind,data FROM ledger ORDER BY id').fetchall()
        if not rows:self.append('ACCOUNT',dict(starting_balance=100000,account='BASIS SIM',execution_version='basis-sim-1.0'))
        for r in rows:self.apply(r['kind'],json.loads(r['data']))

    def apply(self,kind,data):
        if kind=='ORDER':self.pending[data['order_id']]=data;self.results[data['order_id']]=data
        if kind in ('FILLED','REJECTED','CANCELLED'):
            self.pending.pop(data['order_id'],None);self.results[data['order_id']]=data
        if kind in ('FILLED','SETTLED'):
            self.cash=data['cash_after'];position=data['position'];key=position['position_id']
            if position['quantity']:self.positions[key]=position
            else:self.positions.pop(key,None)

    def append(self,kind,data):
        self.db.execute('INSERT INTO ledger(timestamp_ms,kind,order_id,data) VALUES(?,?,?,?)',
            (time.time_ns()//1000000,kind,data.get('order_id'),encode(data)))
        self.apply(kind,data)

    def submit(self,payload):
        with self.lock:
            trade=self.math.ticket(payload['structure_id']);now=time.time_ns()//1000000
            if not 0<=now-trade['math_calculated_ms']<=15000:raise ValueError('Displayed math snapshot expired; refresh')
            self.math.quote_legs(trade,now)
            quantity=math.floor(float(payload['budget'])/trade['debit']) if 'budget' in payload else float(payload.get('quantity',1))
            if 'budget' in payload:
                while quantity>=1 and execution_cost(trade['legs'],trade['multiplier'],quantity)['debit']>float(payload['budget']):quantity-=1
            if not math.isfinite(quantity) or quantity!=int(quantity) or not 1<=quantity<=10000:raise ValueError('Budget must cover at least one complete structure; quantity must be a positive integer')
            displayed_cost=execution_cost(trade['legs'],trade['multiplier'],quantity)['debit']
            if displayed_cost>min(self.cash,10000):raise ValueError('INSUFFICIENT_CASH / MAX_TRADE: funded debit limit is $10,000')
            if len(self.positions)>=10:raise ValueError('MAX_POSITIONS: close a structure first')
            order=dict(order_id=uuid.uuid4().hex,status='PENDING',submit_ms=now,fill_due_ms=now+1000,quantity=quantity,
                structure=trade,reason='Operator accepted displayed PM-reweighted option math',max_debit=displayed_cost)
            self.append('ORDER',order);return copy.deepcopy(order)

    def order(self,order_id):
        with self.lock:
            result=copy.deepcopy(self.results.get(order_id))
            if result and self.error:result['execution_error']=self.error
            return result

    def process(self,now=None):
        now=now if now is not None else time.time_ns()//1000000
        with self.lock:
            for order in list(self.pending.values()):
                if now<order['fill_due_ms']:continue
                try:
                    trade=order['structure'];qty=order['quantity']
                    if now-order['submit_ms']>30000:raise ValueError('ORDER_EXPIRED: recovery/latency gap exceeded 30 seconds')
                    if now>=trade['expiry']:raise ValueError('EXPIRED: option expiry passed')
                    legs,raw_id=self.math.quote_legs(trade,now);cost=execution_cost(legs,trade['multiplier'],qty)
                    if cost['debit']>order['max_debit']+.01:raise ValueError('QUOTE_COST_CHANGED: executable debit exceeded the displayed cost')
                    if order.get('position_id'):
                        p=self.positions.get(order['position_id'])
                        if not p or p['quantity']!=qty:raise ValueError('POSITION_CHANGED before close fill')
                        if self.cash-cost['debit']<0:raise ValueError('INSUFFICIENT_CASH for close')
                        data=dict(order_id=order['order_id'],status='FILLED',side='CLOSE_STRUCTURE',submit_ms=order['submit_ms'],fill_ms=now,
                            quantity=qty,legs=legs,credit=-cost['debit'],fees=cost['fees'],slippage=cost['slippage'],spread_cost=cost['spread_cost'],
                            realized_pnl=-cost['debit']-p['entry_debit'],cash_before=self.cash,cash_after=self.cash-cost['debit'],
                            position=dict(p,quantity=0),source_raw_id=raw_id)
                        self.append('FILLED',data);continue
                    if not 0<cost['debit']<=min(self.cash,10000):raise ValueError('INSUFFICIENT_CASH / COST_RISK_CHECK_FAILED')
                    pos=dict(position_id=order['order_id'],event_id=trade['event_id'],asset=trade['asset'],quantity=qty,
                        structure=dict(trade,legs=legs),entry_debit=cost['debit'],opened_ms=now,fill_raw_id=raw_id)
                    data=dict(order_id=order['order_id'],status='FILLED',submit_ms=order['submit_ms'],fill_ms=now,
                        instrument=trade['kind'],side='BUY_STRUCTURE',quantity=qty,legs=legs,**{k:v for k,v in cost.items() if k!='quantity'},
                        cash_before=self.cash,cash_after=self.cash-cost['debit'],position=pos,source_raw_id=raw_id,
                        quote_quality=legs[0]['quote_quality'])
                    self.append('FILLED',data)
                except (ValueError,KeyError) as error:self.append('REJECTED',dict(order_id=order['order_id'],status='REJECTED',reason=str(error),timestamp_ms=now))

    def close_position(self,position_id):
        with self.lock:
            p=self.positions.get(position_id)
            if not p:raise ValueError('No such open structure')
            if any(o.get('position_id')==position_id for o in self.pending.values()):raise ValueError('Close already pending')
            now=time.time_ns()//1000000;trade=copy.deepcopy(p['structure'])
            for leg in trade['legs']:leg['side']='SELL' if leg['side']=='BUY' else 'BUY'
            legs,raw_id=self.math.quote_legs(trade,now);cost=execution_cost(legs,trade['multiplier'],p['quantity'])
            if self.cash-cost['debit']<0:raise ValueError('INSUFFICIENT_CASH for closing costs')
            order=dict(order_id=uuid.uuid4().hex,status='PENDING',submit_ms=now,fill_due_ms=now+1000,
                position_id=position_id,quantity=p['quantity'],structure=dict(trade,legs=legs),max_debit=cost['debit'],
                reason='Operator closes complete defined-risk structure')
            self.append('ORDER',order);return copy.deepcopy(order)

    def settle(self,p,now):
        # Last source state available BY expiry; future prices never settle a past option.
        trade=p['structure'];expiry=trade['expiry']
        rows=self.math.engine.store.history(p['event_id'],expiry-120000,expiry,500,tail=True)
        valid=[r for r in rows if r.get('spot') and r.get('spot_timestamp') is not None and 0<=expiry-r['spot_timestamp']<=(120000 if trade['multiplier']==100 else 45000)]
        if not valid:return
        row=valid[-1];value=0
        for leg in trade['legs']:
            intrinsic=max(0,row['spot']-leg['strike']) if leg['option_type']=='call' else max(0,leg['strike']-row['spot'])
            value+=(1 if leg['side']=='BUY' else -1)*intrinsic*trade['multiplier']*p['quantity']
        if value<-.01:raise ValueError('Defined-risk payoff invariant failed')
        data=dict(status='SETTLED',cash_before=self.cash,cash_after=self.cash+value,position=dict(p,quantity=0),
            expiry=expiry,asof_price=row['spot'],source_raw_id=row['input_refs'].get('spot'),payoff=value,
            realized_pnl=value-p['entry_debit'],quality='AS_OF_EXPIRY_CASH_PAYOFF_PROXY; not official exchange settlement')
        self.append('SETTLED',data)

    def snapshot(self,event_id=None):
        with self.lock:
            positions=[]
            for p in self.positions.values():
                if event_id and p['event_id']!=event_id:continue
                trade=p['structure'];value=None;state='QUOTE_UNAVAILABLE'
                with self.math.engine.lock:
                    surface=self.math.engine.surfaces.get((trade['asset'],trade['expiry']))
                    if surface:
                        value=0
                        for leg in trade['legs']:
                            c=next((c for c in surface.get(leg['option_type']+'s',[]) if c['instrument']==leg['instrument']),None)
                            spot=self.math.engine.spots.get(trade['asset'],{}).get('price')
                            if c and spot:c=execution_quote(c,surface,spot)
                            quote=c.get('bid' if leg['side']=='BUY' else 'ask') if c else None
                            if quote is None:value=None;break
                            value+=(1 if leg['side']=='BUY' else -1)*quote*trade['multiplier']*p['quantity']
                        state='BID_ASK_LIQUIDATION_BEFORE_EXIT_COSTS' if value is not None else state
                        if time.time_ns()//1000000>=trade['expiry']:state='AWAITING_EXPIRY_PRICE'
                positions.append(dict(p,liquidation_value=value,unrealized_pnl=value-p['entry_debit'] if value is not None else None,mark_state=state))
            return dict(account='BASIS SIM',starting_balance=100000,cash=self.cash,positions=positions,error=self.error,
                        orders=[copy.deepcopy(o) for o in list(self.results.values())[-20:] if not event_id or o.get('structure',o.get('position',{})).get('event_id')==event_id])

    def run(self):
        while not self.stop.is_set():
            try:
                self.process();now=time.time_ns()//1000000
                with self.lock:
                    for p in list(self.positions.values()):
                        if now>=p['structure']['expiry']:self.settle(p,now)
                self.error=None
            except Exception as error:self.error=str(error)
            self.stop.wait(.25)

    def start(self):
        self.thread=threading.Thread(target=self.run,name='basis-sim',daemon=True);self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:self.thread.join(timeout=10)
        self.db.close()
