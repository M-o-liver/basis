"""Isolated USD paper ledger. Analyzers never submit orders or change balances."""
from dataclasses import asdict, dataclass
from contextlib import contextmanager
import copy
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import threading
import time
import uuid
from .store import encode
from .policies import ALGOS, PolicyRunner, definition
from .performance import PerformanceReview

WALLETS=('OLIVER','SW','EVENT_SYNC','LEAD_LAG','COVARIANCE','TOPOLOGY','ORDINAL_MMD','MARTINGALE')
VERSION='paper-1.3.0'

@dataclass(frozen=True)
class PaperConfig:
    starting_balance: float=100000
    latency_ms: int=1000
    max_order_age_ms: int=30000
    recovery_gap_ms: int=90000
    recovery_hold_ms: int=120000
    quote_max_age_ms: int=45000
    estimated_spread_bps: float=20
    slippage_bps: float=10
    impact_bps_per_10000: float=5
    fee_bps: float=10
    fee_per_contract: float=.65
    max_trade_pct: float=.10
    max_position_pct: float=.25
    max_gross_exposure: float=1.0
    max_daily_loss: float=.10
    max_drawdown: float=.25
    max_quantity: float=10000
    max_concurrent_positions: int=10


def finite(value):
    if isinstance(value,bool): return None
    try:
        x=float(value)
        return x if math.isfinite(x) else None
    except (ValueError,TypeError): return None


class QuoteUnavailable(ValueError):
    def __init__(self,code,reason):
        self.code=code
        super().__init__(reason)


class PaperDesk:
    def __init__(self,engine,path,config=None,clock=None,auto_policies=True):
        self.engine,self.path=engine,str(path)
        self.config=config or PaperConfig()
        self.clock=clock or (lambda:time.time_ns()//1000000)
        self.auto_policies=auto_policies
        self.parameter_hash=hashlib.sha256(encode(asdict(self.config)).encode()).hexdigest()[:16]
        Path(path).parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(path,check_same_thread=False,isolation_level=None)
        self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL');self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY,wallet TEXT NOT NULL,run_number INTEGER NOT NULL,data TEXT NOT NULL,UNIQUE(wallet,run_number));
        CREATE TABLE IF NOT EXISTS ledger(id INTEGER PRIMARY KEY,run_id INTEGER NOT NULL,timestamp_ms INTEGER NOT NULL,kind TEXT NOT NULL,order_id TEXT,data TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS paper_run_ledger ON ledger(run_id,id);
        CREATE INDEX IF NOT EXISTS paper_order ON ledger(order_id,id);''')
        self.db.executescript('''CREATE TABLE IF NOT EXISTS equity_points(id INTEGER PRIMARY KEY,run_id INTEGER NOT NULL,timestamp_ms INTEGER NOT NULL,equity REAL NOT NULL,cash REAL NOT NULL,costs REAL NOT NULL,stale_marks INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS paper_equity_time ON equity_points(run_id,timestamp_ms,id);''')
        for table in ('runs','ledger','equity_points'):
            for op in ('UPDATE','DELETE'):
                self.db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{op} BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT,'immutable paper history'); END")
        self.lock=threading.RLock();self.states={};self.runs={};self.pending={};self.last_mark=0;self.raw_option_cache={}
        self.stop=threading.Event();self.thread=None;self.failure=None;self.theses={}
        for row in self.db.execute('SELECT * FROM runs WHERE id IN (SELECT MAX(id) FROM runs GROUP BY wallet)'):
            self.runs[row['wallet']]=dict(json.loads(row['data']),id=row['id'])
            latest=self.db.execute('SELECT data FROM ledger WHERE run_id=? ORDER BY id DESC LIMIT 1',(row['id'],)).fetchone()
            self.states[row['wallet']]=json.loads(latest['data'])['state_after'] if latest else self.empty_state()
        for wallet in WALLETS:
            if wallet not in self.runs:self.reset(wallet)
            if 'spread_paid' not in self.states[wallet]:
                spread=0.
                for item in self.db.execute("SELECT data FROM ledger WHERE run_id=? AND kind='FILL'",(self.runs[wallet]['id'],)):
                    f=json.loads(item['data']);q=f['quote'];spread+=(q['ask']-q['bid'])/2*f['order']['quantity']*q['multiplier']
                self.append(wallet,'COST_ACCOUNTING',dict(reason='Expose actual quoted half-spread costs; cash unchanged'),dict(self.states[wallet],spread_paid=spread))
        for row in self.db.execute("SELECT data FROM ledger WHERE kind IN ('ORDER','FILL','REJECT','CANCEL') ORDER BY id"):
            data=json.loads(row['data']);order=data['order'];key=order['order_id']
            if row and data['kind']=='ORDER' and order['run_id']==self.runs[order['wallet_id']]['id']:self.pending[key]=order
            else:self.pending.pop(key,None)
        for wallet in ALGOS:
            if auto_policies and self.policy_upgrade_pending(wallet):
                for key,order in list(self.pending.items()):
                    if order['wallet_id']==wallet and order['side']=='BUY':
                        self.append(wallet,'CANCEL',dict(order=order,reason='Policy upgrade: drain existing exposure first'));self.pending.pop(key)
                if not self.states[wallet]['positions'] and not any(o['wallet_id']==wallet for o in self.pending.values()):
                    self.reset(wallet,carry=True)
        self.policy_runner=PolicyRunner(self)
        # Preserve any existing performance history when adding the chart table.
        if not self.db.execute('SELECT 1 FROM equity_points LIMIT 1').fetchone():
            for row in self.db.execute("SELECT run_id,timestamp_ms,data FROM ledger WHERE kind IN ('OPEN_RUN','MARK','FILL','SETTLE') ORDER BY id").fetchall():
                data=json.loads(row['data']);state=data['state_after'];metrics=data.get('metrics',{})
                equity=metrics.get('equity',state['cash']+sum(p['quantity']*p['multiplier']*p['last_bid'] for p in state['positions'].values()))
                self.db.execute('INSERT INTO equity_points(run_id,timestamp_ms,equity,cash,costs,stale_marks) VALUES(?,?,?,?,?,?)',
                    (row['run_id'],row['timestamp_ms'],equity,state['cash'],state['fees_paid']+state['slippage_paid'],metrics.get('stale_marks',0)))
        self.last_advance=self.db.execute('SELECT MAX(timestamp_ms) FROM equity_points').fetchone()[0] or self.clock()
        self.performance=PerformanceReview(self);self.performance.refresh(self.clock())

    def empty_state(self):
        return dict(cash=self.config.starting_balance,positions={},realized_pnl=0.,fees_paid=0.,slippage_paid=0.,spread_paid=0.,
            turnover=0.,trades=0,closed_positions=0,winners=0,gross_profit=0.,gross_loss=0.,peak_equity=self.config.starting_balance,
            max_drawdown=0.,day=self.clock()//86400000,day_open_equity=self.config.starting_balance,dead=False,
            automation_enabled=True,last_policy_order=0,policy_status='Watching for a qualifying diagnostic')

    def append(self,wallet,kind,data,state=None):
        state=copy.deepcopy(state if state is not None else self.states[wallet])
        item=dict(data,kind=kind,state_after=state,engine_version=VERSION,parameter_hash=self.parameter_hash)
        self.db.execute('INSERT INTO ledger(run_id,timestamp_ms,kind,order_id,data) VALUES(?,?,?,?,?)',
            (self.runs[wallet]['id'],self.clock(),kind,data.get('order',{}).get('order_id'),encode(item)))
        self.states[wallet]=state
        return item

    def policy_upgrade_pending(self,wallet):
        policy=definition(wallet)
        return self.runs[wallet]['strategy_version']!=policy['strategy_version'] or self.runs[wallet].get('policy',{}).get('parameter_hash')!=policy['parameter_hash']

    @contextmanager
    def run_transaction(self):
        saved=copy.deepcopy((self.runs,self.states,self.pending))
        self.db.execute('SAVEPOINT new_paper_run')
        try:
            yield
        except Exception:
            self.db.execute('ROLLBACK TO new_paper_run')
            self.runs,self.states,self.pending=saved
            raise
        finally:
            self.db.execute('RELEASE new_paper_run')

    def reset(self,wallet,carry=False):
        with self.lock,self.run_transaction():
            if wallet=='all':return [self.reset(w) for w in WALLETS]
            if wallet not in WALLETS:raise ValueError('Unknown wallet')
            prior_state=copy.deepcopy(self.states.get(wallet))
            if carry and prior_state['positions']:raise ValueError('Close prior-policy positions before carrying capital into a new run')
            for key,order in list(self.pending.items()):
                if order['wallet_id']==wallet:
                    self.append(wallet,'CANCEL',dict(order=order,reason='New run'));self.pending.pop(key)
            previous=self.runs.get(wallet)
            if carry:self.append(wallet,'CLOSE_RUN',dict(reason='Policy version changed; capital carried forward',metrics=self.metrics(wallet)))
            run=dict(wallet_id=wallet,run_number=(previous['run_number'] if previous else 0)+1,
                strategy_id='manual' if wallet=='OLIVER' else wallet,strategy_version='manual-1' if wallet=='OLIVER' else 'unconfigured',
                policy_state='MANUAL' if wallet=='OLIVER' else 'DISABLED: no validated trading policy',
                opened_at=self.clock(),starting_balance=self.config.starting_balance,parameter_hash=self.parameter_hash,
                parameters=asdict(self.config),code_revision=self.engine.code_hash,engine_version=VERSION)
            if wallet in ALGOS and self.auto_policies:
                policy=definition(wallet)
                run.update(strategy_id=policy['strategy_id'],strategy_version=policy['strategy_version'],policy_state='AUTO PAPER',policy=policy)
            if carry:
                run.update(starting_balance=prior_state['cash'],prior_run_id=previous['id'],capital_carried=True,
                    account_starting_balance=previous.get('account_starting_balance',previous['starting_balance']),
                    account_prior_trades=previous.get('account_prior_trades',0)+prior_state['trades'],
                    account_prior_costs=previous.get('account_prior_costs',0)+sum(prior_state.get(k,0) for k in ('fees_paid','slippage_paid','spread_paid')),
                    account_peak_equity=max(previous.get('account_peak_equity',0),prior_state['peak_equity']),
                    account_prior_max_drawdown=max(previous.get('account_prior_max_drawdown',0),prior_state['max_drawdown']))
            cur=self.db.execute('INSERT INTO runs(wallet,run_number,data) VALUES(?,?,?)',(wallet,run['run_number'],encode(run)))
            self.runs[wallet]=dict(run,id=cur.lastrowid);self.states[wallet]=self.empty_state()
            if carry:
                self.states[wallet].update(cash=prior_state['cash'],peak_equity=prior_state['cash'],
                    automation_enabled=prior_state.get('automation_enabled',True),last_policy_order=prior_state.get('last_policy_order',0),
                    day=prior_state['day'],day_open_equity=prior_state['day_open_equity'],dead=prior_state['dead'])
            self.append(wallet,'OPEN_RUN',dict(run=self.runs[wallet]))
            return self.runs[wallet]

    def quote(self,instrument,now=None):
        now=self.clock() if now is None else now
        with self.engine.lock:
            if getattr(self.engine,'persistence_error',None):
                raise QuoteUnavailable('RECORDER_FAILED','Recorder persistence failed; paper execution unavailable: '+self.engine.persistence_error)
            if instrument.startswith('SPOT:'):
                asset=instrument[5:];spot=self.engine.spots.get(asset)
                if not spot:raise QuoteUnavailable('NO_SPOT','No spot quote for '+asset)
                raw=self.engine.store.raw_record(spot['raw_id']);p=raw['payload']
                bid,ask=finite(p.get('best_bid')),finite(p.get('best_ask'))
                estimated=bid is None or ask is None
                if estimated:
                    width=self.config.estimated_spread_bps/10000
                    bid,ask=spot['price']*(1-width),spot['price']*(1+width)
                q=dict(instrument=instrument,asset=asset,kind='spot',venue=spot['venue'],bid=bid,ask=ask,
                    multiplier=1,received_ms=spot['received_ms'],source_ms=spot['source_ms'],raw_id=spot['raw_id'],
                    estimated=estimated,quote_assumption='Estimated spread' if estimated else 'Quoted bid/ask',expiry=None,
                    market_state=spot.get('context',{}).get('market_state'),
                    settlement_model='USD cash; unlevered spot',fractional=asset in ('BTC','ETH'))
            else:
                match=None
                for surface in self.engine.surfaces.values():
                    for call in surface['calls']+surface.get('puts',[]):
                        if call.get('instrument')==instrument:match=(surface,call);break
                    if match:break
                if match is None and instrument.endswith('-P'):
                    # Deribit raw snapshots contain puts too; use the matching call
                    # only to identify expiry/underlying, never to synthesize a put price.
                    for surface in self.engine.surfaces.values():
                        anchor=next((c for c in surface['calls'] if c.get('instrument')==instrument[:-1]+'C'),None)
                        if not anchor or surface['venue']!='deribit':continue
                        raw_id=surface['raw_id']
                        if raw_id not in self.raw_option_cache:
                            raw=self.engine.store.raw_record(raw_id)['payload']
                            self.raw_option_cache[raw_id]={p.get('instrument_name'):p for p in raw.get('result',[])}
                            if len(self.raw_option_cache)>4:self.raw_option_cache.pop(next(iter(self.raw_option_cache)))
                        put=self.raw_option_cache[raw_id].get(instrument)
                        forward=finite(put.get('underlying_price')) if put else None
                        if put and forward is not None and forward>0:
                            bid,ask=finite(put.get('bid_price')),finite(put.get('ask_price'))
                            match=(surface,dict(instrument=instrument,strike=anchor['strike'],option_type='put',bid=bid*forward if bid is not None else None,ask=ask*forward if ask is not None else None,iv=finite(put.get('mark_iv'))/100 if finite(put.get('mark_iv')) is not None else None))
                        break
                if match is None:raise QuoteUnavailable('NO_SURFACE','Instrument is absent from current surfaces')
                surface,call=match
                asset=next(a for (a,e),s in self.engine.surfaces.items() if s is surface)
                bid,ask=finite(call.get('bid')),finite(call.get('ask'))
                if bid is None or ask is None:raise QuoteUnavailable('MISSING_BID_ASK','Option execution requires both bid and ask')
                q=dict(instrument=instrument,asset=asset,kind='option',venue=surface['venue'],bid=bid,ask=ask,
                    multiplier=100 if surface['venue']=='yahoo' else 1,strike=call['strike'],option_type=call.get('option_type','call'),
                    iv=call.get('iv'),
                    expiry=surface['expiry'],received_ms=surface['received_ms'],source_ms=surface['source_ms'],raw_id=surface['raw_id'],market_state=surface.get('context',{}).get('market_state'),
                    estimated=surface['venue']=='yahoo',quote_assumption='Yahoo delay unknown' if surface['venue']=='yahoo' else 'Coin premium converted to USD at reported forward',
                    settlement_model='USD cash-equivalent; physical exercise and coin collateral not simulated; explicit settlement required',fractional=False)
            if q['expiry'] and now>=q['expiry']:raise QuoteUnavailable('EXPIRED','Expired option: explicit settlement required')
            if q['venue']=='yahoo':
                from .equities import regular_session
                if q.get('market_state')=='CLOSED' or regular_session(now)['market_state']=='CLOSED':
                    raise QuoteUnavailable('MARKET_CLOSED','Yahoo regular session is closed; stock paper execution unavailable')
                if q.get('market_state')!='OPEN':
                    raise QuoteUnavailable('SESSION_UNKNOWN','Yahoo regular-session state is unknown')
                # Yahoo books have no trustworthy exchange timestamp. Receipt
                # freshness is a distinct, explicitly estimated execution model.
                limit=max(self.config.quote_max_age_ms,self.engine.config.yahoo_seconds*2000)
                q.update(estimated=True,quote_delay='unknown',freshness_basis='BASIS_RECEIPT',receipt_max_age_ms=limit,
                    underlying_source_ms=self.engine.spots.get(q['asset'],{}).get('source_ms'),
                    quote_assumption='Yahoo delayed/proxy/estimated; '+('option book timestamp unknown; ' if q['kind']=='option' else '')+'receipt freshness only')
                if q['kind']=='option':q['source_ms']=call.get('source_ms')
                if q['source_ms'] is not None and q['source_ms']>now:
                    raise QuoteUnavailable('QUOTE_STALE','Yahoo source timestamp is not yet available')
            else:
                limit=self.config.quote_max_age_ms
                q.update(freshness_basis='SOURCE_AND_RECEIPT',receipt_max_age_ms=limit)
                if q['source_ms'] is None or not 0<=now-q['source_ms']<=limit:
                    raise QuoteUnavailable('QUOTE_STALE','Quote source timestamp is stale or not yet available')
            if not 0<=now-q['received_ms']<=limit:
                raise QuoteUnavailable('QUOTE_STALE',f'Quote retrieval is stale or not yet available (limit {limit//1000}s)')
            if bid<0 or ask<=0 or bid>ask:raise QuoteUnavailable('MISSING_BID_ASK','Invalid, missing or crossed executable bid/ask')
            q['quoted_at']=now
            return q

    def event_context(self,event_id):
        with self.engine.lock:
            event=self.engine.events.get(str(event_id))
            if event is None:
                raise QuoteUnavailable('UNKNOWN_EVENT',f'Unknown or inactive event {event_id!r}; select a current market event')
            return dict(event,**self.engine.latest.get(str(event_id),{}))

    def instruments(self,event_id=None,asset=None):
        with self.engine.lock:
            event=self.event_context(event_id) if event_id is not None else {}
            if asset and event and asset.upper()!=event.get('asset'):
                raise QuoteUnavailable('EVENT_ASSET_MISMATCH','Explicit asset does not match the selected event')
            asset=event.get('asset') if event else str(asset or '').strip().upper()
            if not asset:raise QuoteUnavailable('UNSUPPORTED_EVENT','Select a supported event or provide an explicit asset')
            exclusions={}
            def exclude(code,reason,instrument=None):
                item=exclusions.setdefault(code,dict(code=code,reason=reason,count=0,examples=[]))
                item['count']+=1
                if instrument and len(item['examples'])<3:item['examples'].append(instrument)
            if event and event.get('event_type') not in ('touch','terminal'):
                return dict(rows=[],event=event,asset=asset,exclusions=[dict(code='UNSUPPORTED_EVENT',reason='Unsupported event mapping',count=1,examples=[])])
            if event and event['expiry']<=self.clock():
                return dict(rows=[],event=event,asset=asset,exclusions=[dict(code='EXPIRED',reason='Selected event has expired; select a current event',count=1,examples=[])])
            strike=event.get('strike_or_threshold') or self.engine.spots.get(asset,{}).get('price',0)
            cutoff=event.get('expiry',self.clock())
            available=[s for (a,e),s in self.engine.surfaces.items() if a==asset]
            surfaces=sorted((s for s in available if s['expiry']>=cutoff),key=lambda s:s['expiry'])[:2]
            if not available:exclude('NO_SURFACE','No option surface has been collected for '+asset)
            elif not surfaces:exclude('NO_CHAIN_AFTER_EVENT_CUTOFF','No option chain expires at or after the selected event cutoff')
            ids=['SPOT:'+asset]
            # A separate nearby quota for each right prevents calls crowding out puts.
            for s in surfaces:
                for right in ('calls','puts'):
                    contracts=s.get(right,[])
                    if not contracts:exclude('MISSING_RIGHT','No '+right+' in the current option snapshot')
                    ids.extend(c['instrument'] for c in sorted(contracts,key=lambda c:abs(c['strike']-strike))[:12])
        rows=[]
        for instrument in ids:
            try:rows.append(self.quote(instrument))
            except ValueError as error:exclude(getattr(error,'code','QUOTE_UNAVAILABLE'),str(error),instrument)
        return dict(rows=rows,event=event or None,asset=asset,exclusions=list(exclusions.values()),as_of=self.clock())

    def policy_instrument(self,row,direction,parameters,budget=None):
        with self.engine.lock:
            spot=self.engine.spots.get(row['asset'],{}).get('price')
            if not spot:return None
            surfaces=sorted((s for (asset,expiry),s in self.engine.surfaces.items() if asset==row['asset'] and expiry>=max(row['expiry'],self.clock()+parameters['min_expiry_ms'])),key=lambda s:s['expiry'])[:2]
            names=[]
            for s in surfaces:
                contracts=s['calls'] if direction>0 or s['venue']=='deribit' else s.get('puts',[])
                names.extend(c['instrument'][:-1]+'P' if direction<0 and s['venue']=='deribit' else c['instrument'] for c in sorted(contracts,key=lambda c:abs(c['strike']-spot))[:5])
        candidates=[]
        for name in names:
            try:
                quote=self.quote(name)
                if quote['bid']<=0 or (quote['ask']-quote['bid'])/quote['ask']>parameters['max_option_spread']:continue
                quantity=1 if budget is None else min(self.config.max_quantity,math.floor(budget/(quote['ask']*quote['multiplier']*1.01+self.config.fee_per_contract)))
                if quantity<1:continue
                buy=self.costs(quote,'BUY',quantity);sell=self.costs(quote,'SELL',quantity)
                if budget is not None and buy['total_debit']>budget:continue
                drag=(buy['total_debit']-sell['net_credit'])/buy['total_debit']
                if drag>parameters.get('max_roundtrip_drag',1):continue
                candidates.append((drag,quote['expiry'],abs(quote['strike']-spot),dict(quote,policy_quantity=quantity,roundtrip_drag=drag)))
            except ValueError:pass
        return min(candidates,key=lambda x:x[:3])[3] if candidates else None

    def costs(self,quote,side,quantity):
        price=quote['ask'] if side=='BUY' else quote['bid']
        notional=price*quantity*quote['multiplier']
        slip=price*(self.config.slippage_bps+self.config.impact_bps_per_10000*notional/10000)/10000
        fill=max(0,price+slip if side=='BUY' else price-slip)
        gross=fill*quantity*quote['multiplier']
        fees=gross*self.config.fee_bps/10000+(quantity*self.config.fee_per_contract if quote['kind']=='option' else 0)
        return dict(quoted_bid=quote['bid'],quoted_ask=quote['ask'],quote_price=price,fill_price=fill,
            spread=(quote['ask']-quote['bid'])/2*quantity*quote['multiplier'],slippage_per_unit=abs(fill-price),
            slippage=abs(fill-price)*quantity*quote['multiplier'],gross_premium=gross,fees=fees,
            total_debit=gross+fees if side=='BUY' else None,net_credit=gross-fees if side=='SELL' else None,
            assumptions='Configured fee/slippage/size model; depth impact estimated',depth_available=False)

    def resolve_run(self,wallet,run_id=None):
        if wallet not in self.runs:raise ValueError('Unknown wallet '+str(wallet))
        if run_id is None or int(run_id)==self.runs[wallet]['id']:return self.runs[wallet],self.states[wallet],False
        row=self.db.execute('SELECT * FROM runs WHERE id=? AND wallet=?',(run_id,wallet)).fetchone()
        if row is None:raise ValueError('Run does not belong to the selected wallet')
        latest=self.db.execute('SELECT data FROM ledger WHERE run_id=? ORDER BY id DESC LIMIT 1',(run_id,)).fetchone()
        return dict(json.loads(row['data']),id=row['id']),json.loads(latest['data'])['state_after'],True

    def metrics(self,wallet,run_id=None):
        run,state,archived=self.resolve_run(wallet,run_id)
        positions=[];market_value=0.;unrealized=0.;stale=0
        for instrument,p in state['positions'].items():
            try:
                if archived:raise ValueError('ARCHIVED: last recorded bid, not a current quote')
                q=self.quote(instrument);bid=q['bid'];status='QUOTED'
            except ValueError as error:
                bid=p['last_bid'];q=None;status=str(error);stale+=1
            value=p['quantity']*p['multiplier']*bid;market_value+=value;unrealized+=value-p['cost_basis']
            positions.append(dict(p,instrument=instrument,market_value=value,unrealized_pnl=value-p['cost_basis'],mark_status=status,mark_raw_id=q['raw_id'] if q else p.get('mark_raw_id')))
        equity=state['cash']+market_value;peak=max(state['peak_equity'],equity)
        starting=run['starting_balance'];account_start=run.get('account_starting_balance',starting)
        account_peak=max(run.get('account_peak_equity',peak),peak)
        policy_state=run['policy_state']
        if archived:policy_state='ARCHIVED: retained ledger state'
        elif wallet in ALGOS and self.auto_policies:policy_state=('AUTO: '+state.get('policy_status','Watching')) if state.get('automation_enabled',True) else 'PAUSED'
        return dict(wallet_id=wallet,run_id=run['id'],run_number=run['run_number'],policy_state=policy_state,archived=archived,
            starting_balance=starting,cash=state['cash'],market_value=market_value,equity=equity,
            gross_exposure=market_value,net_exposure=market_value,realized_pnl=state['realized_pnl'],unrealized_pnl=unrealized,
            total_pnl=equity-starting,return_pct=(equity/starting-1)*100 if starting>0 else None,
            account_pnl=equity-account_start,account_return_pct=(equity/account_start-1)*100 if account_start>0 else None,
            account_trades=run.get('account_prior_trades',0)+state['trades'],
            account_costs=run.get('account_prior_costs',0)+sum(state.get(k,0) for k in ('fees_paid','slippage_paid','spread_paid')),
            account_max_drawdown=max(run.get('account_prior_max_drawdown',0),state['max_drawdown'],(account_peak-equity)/account_peak if account_peak else 0),
            strategy_version=run['strategy_version'],prior_run_id=run.get('prior_run_id'),
            risk_feedback=state.get('risk_feedback'),
            performance_review=getattr(getattr(self,'performance',None),'report',{}).get('wallets',{}).get(wallet),
            fees_paid=state['fees_paid'],slippage_paid=state['slippage_paid'],spread_paid=state.get('spread_paid',0),trades=state['trades'],
            max_drawdown=max(state['max_drawdown'],(peak-equity)/peak if peak else 0),open_positions=len(positions),closed_positions=state['closed_positions'],
            win_rate=state['winners']/state['closed_positions'] if state['closed_positions'] else None,
            profit_factor=state['gross_profit']/state['gross_loss'] if state['gross_loss'] else None,
            average_winner=state['gross_profit']/state['winners'] if state['winners'] else None,
            average_loser=-state['gross_loss']/(state['closed_positions']-state['winners']) if state['closed_positions']>state['winners'] else None,
            turnover=state['turnover'],capital_utilization=market_value/equity if equity>0 else None,
            stale_marks=stale,bankrupt=state['dead'] or equity<=0,positions=positions,
            pending_orders=[] if archived else [o for o in self.pending.values() if o['wallet_id']==wallet])

    def risk(self,wallet,side,quantity,quote,cost):
        state=self.states[wallet];m=self.metrics(wallet);position=state['positions'].get(quote['instrument'])
        if m['bankrupt']:raise ValueError('Wallet is bankrupt for this run')
        if side=='SELL':
            if not position or quantity>position['quantity']+1e-9:raise ValueError('No shorting: sale exceeds owned quantity')
            return
        if m['stale_marks']:raise ValueError('Resolve stale/expired position marks before increasing risk')
        if m['account_max_drawdown']>=self.config.max_drawdown:raise ValueError('Drawdown risk limit reached')
        if m['equity']<state['day_open_equity']*(1-self.config.max_daily_loss):raise ValueError('Daily loss limit reached')
        debit=cost['total_debit'];existing=next((p['market_value'] for p in m['positions'] if p['instrument']==quote['instrument']),0)
        if debit>state['cash']:raise ValueError('Insufficient fully paid cash')
        if debit>m['equity']*self.config.max_trade_pct:raise ValueError('Maximum trade percentage exceeded')
        if existing+debit>m['equity']*self.config.max_position_pct:raise ValueError('Maximum position percentage exceeded')
        if m['market_value']+debit>m['equity']*self.config.max_gross_exposure:raise ValueError('Gross exposure limit exceeded')
        if not position and len(state['positions'])>=self.config.max_concurrent_positions:raise ValueError('Maximum concurrent positions reached')

    def preview(self,payload,policy=False):
        wallet=str(payload.get('wallet','OLIVER')).upper();side=str(payload.get('side','BUY')).upper();quantity=finite(payload.get('quantity'))
        if wallet not in self.runs or side not in ('BUY','SELL'):raise ValueError('Unknown wallet or side')
        if wallet!='OLIVER' and not policy:raise ValueError('Analyzer wallet orders are owned by their versioned policy')
        if quantity is None or not 0<quantity<=self.config.max_quantity:raise ValueError('Invalid quantity or contract limit exceeded')
        context=self.event_context(payload['basis_event_id']) if payload.get('basis_event_id') is not None else None
        quote=self.quote(str(payload.get('instrument','')))
        if context and context['asset']!=quote['asset']:raise ValueError('Instrument asset does not match the selected event')
        if not quote['fractional'] and not quantity.is_integer():raise ValueError('Whole shares/contracts required')
        costs=self.costs(quote,side,quantity);self.risk(wallet,side,quantity,quote,costs)
        return dict(wallet_id=wallet,side=side,quantity=quantity,quote=quote,costs=costs,event=context,paper_only=True,latency_ms=self.config.latency_ms)

    def submit(self,payload,policy=False):
        with self.lock:
            client_id=str(payload.get('client_order_id') or uuid.uuid4().hex)
            previous=self.db.execute("SELECT data FROM ledger WHERE order_id=? ORDER BY id DESC LIMIT 1",(client_id,)).fetchone()
            if previous:return json.loads(previous['data'])
            ticket=self.preview(payload,policy);wallet=ticket['wallet_id'];now=self.clock()
            reason=str(payload.get('reason') or '').strip()
            if not reason:raise ValueError('A paper trade needs a reason')
            event_id=payload.get('basis_event_id');context=self.engine.latest.get(event_id) if event_id else None
            strategy=definition(wallet) if policy else dict(strategy_id='manual',strategy_version='manual-1')
            signal=payload.get('algo_signal') if policy else None
            if signal and signal['timestamp']>now:raise ValueError('Future diagnostic cannot cause an order')
            order=dict(ticket,order_id=client_id,run_id=self.runs[wallet]['id'],strategy_id=strategy['strategy_id'],strategy_version=strategy['strategy_version'],
                timestamp_signal=signal['timestamp'] if signal else now,timestamp_decision=now,timestamp_submit=now,eligible_at=now+self.config.latency_ms,instrument=ticket['quote']['instrument'],
                basis_event_id=event_id,signal_raw_id=context['raw_id'] if context else ticket['quote']['raw_id'],algo_signal_id=f"{signal['algo_name']}:{signal['market_scope']}:{signal['raw_id']}" if signal else None,
                algo_signal=signal,policy_context=payload.get('policy_context'),reason=reason)
            state=dict(self.states[wallet],last_policy_order=now) if policy else None
            result=self.append(wallet,'ORDER',dict(order=order),state);self.pending[client_id]=order;return result

    def advance(self,now=None):
        with self.lock:
            now=self.clock() if now is None else now
            if now-self.last_advance>self.config.recovery_gap_ms:
                for wallet in ALGOS:
                    self.append(wallet,'RECOVERY',dict(gap_start=self.last_advance,gap_end=now,
                        reason='Execution clock gap; exits need fresh quotes; new entries held for two minutes'),
                        dict(self.states[wallet],entry_hold_until=now+self.config.recovery_hold_ms))
            self.last_advance=now
            for key,order in list(self.pending.items()):
                if now<order['eligible_at']:continue
                wallet=order['wallet_id']
                try:
                    if now-order['timestamp_submit']>self.config.max_order_age_ms:raise ValueError('Order expired after execution delay; no late fantasy fill')
                    quote=self.quote(order['instrument'],now);cost=self.costs(quote,order['side'],order['quantity'])
                    if order['side']=='BUY' and order.get('policy_context',{}):
                        limit=order['policy_context'].get('max_roundtrip_drag')
                        if limit is not None and (cost['total_debit']-self.costs(quote,'SELL',order['quantity'])['net_credit'])/cost['total_debit']>limit:
                            raise ValueError('Execution cost exceeded the policy limit during latency')
                    self.risk(wallet,order['side'],order['quantity'],quote,cost)
                    state=copy.deepcopy(self.states[wallet]);before=copy.deepcopy(state['positions'].get(order['instrument']));cash=state['cash']
                    quantity=order['quantity'];instrument=order['instrument'];realized=0.
                    if order['side']=='BUY':
                        position=state['positions'].get(instrument,dict(quantity=0.,cost_basis=0.,multiplier=quote['multiplier'],asset=quote['asset'],kind=quote['kind'],expiry=quote['expiry'],strike=quote.get('strike'),option_type=quote.get('option_type'),settlement_model=quote['settlement_model'],policy_context=order.get('policy_context') or {}))
                        position['quantity']+=quantity;position['cost_basis']+=cost['total_debit'];position.update(last_bid=quote['bid'],mark_raw_id=quote['raw_id'])
                        state['positions'][instrument]=position;state['cash']-=cost['total_debit']
                    else:
                        position=state['positions'][instrument];basis=position['cost_basis']*quantity/position['quantity']
                        realized=cost['net_credit']-basis;state['cash']+=cost['net_credit'];state['realized_pnl']+=realized
                        position['quantity']-=quantity;position['cost_basis']-=basis
                        state['closed_positions']+=1;state['winners']+=int(realized>0)
                        state['gross_profit']+=max(0,realized);state['gross_loss']+=max(0,-realized)
                        if position['quantity']<1e-9:del state['positions'][instrument]
                    state['trades']+=1;state['fees_paid']+=cost['fees'];state['slippage_paid']+=cost['slippage'];state['turnover']+=cost['gross_premium']
                    state['spread_paid']=state.get('spread_paid',0)+(quote['ask']-quote['bid'])/2*quantity*quote['multiplier']
                    self.append(wallet,'FILL',dict(order=order,timestamp_fill=now,quote=quote,fill=cost,cash_before=cash,cash_after=state['cash'],
                        position_before=before,position_after=state['positions'].get(instrument),realized_pnl=realized),state)
                except ValueError as error:self.append(wallet,'REJECT',dict(order=order,timestamp_rejected=now,reason=str(error)))
                self.pending.pop(key,None)
            if now-self.last_mark>=30000:
                for wallet,state in list(self.states.items()):
                    m=self.metrics(wallet);next_state=copy.deepcopy(state)
                    next_state['peak_equity']=max(state['peak_equity'],m['equity']);next_state['max_drawdown']=m['max_drawdown'];next_state['dead']=m['bankrupt']
                    day=now//86400000
                    if next_state['day']!=day:next_state.update(day=day,day_open_equity=m['equity'])
                    for p in m['positions']:
                        if p['mark_status']=='QUOTED':next_state['positions'][p['instrument']].update(last_bid=p['market_value']/p['quantity']/p['multiplier'],mark_raw_id=p['mark_raw_id'])
                    self.append(wallet,'MARK',dict(metrics=m),next_state)
                    self.db.execute('INSERT INTO equity_points(run_id,timestamp_ms,equity,cash,costs,stale_marks) VALUES(?,?,?,?,?,?)',
                        (self.runs[wallet]['id'],now,m['equity'],m['cash'],m['fees_paid']+m['slippage_paid'],m['stale_marks']))
                self.last_mark=now
            self.performance.refresh(now)
            if self.auto_policies:self.policy_runner.run(now)

    def settle(self,payload):
        """Explicit cash-equivalent settlement with a recorded underlying print."""
        with self.lock:
            wallet=str(payload.get('wallet','OLIVER')).upper();instrument=str(payload['instrument'])
            position=self.states[wallet]['positions'].get(instrument)
            if not position or position['kind']!='option' or self.clock()<position['expiry']:raise ValueError('Requires an owned expired option')
            raw=self.engine.store.raw_record(int(payload['raw_id']))
            price=finite(raw['payload'].get('price')) if raw else None
            if not raw or raw['kind']!='spot' or raw['subject']!=position['asset'] or price is None or raw['received_ms']>self.clock():raise ValueError('Requires an already available matching underlying spot record')
            if abs(raw['received_ms']-position['expiry'])>60000:raise ValueError('Settlement reference must be within 60 seconds of expiry')
            if not str(payload.get('reason','')).strip():raise ValueError('Explain the chosen settlement proxy')
            state=copy.deepcopy(self.states[wallet]);intrinsic=position['strike']-price if position.get('option_type')=='put' else price-position['strike']
            gross=max(0,intrinsic)*position['quantity']*position['multiplier']
            pnl=gross-position['cost_basis'];state['cash']+=gross;state['realized_pnl']+=pnl;state['closed_positions']+=1;state['winners']+=int(pnl>0)
            state['gross_profit']+=max(0,pnl);state['gross_loss']+=max(0,-pnl);del state['positions'][instrument]
            return self.append(wallet,'SETTLE',dict(instrument=instrument,reference_raw_id=raw['id'],reference_spot=price,payout=gross,realized_pnl=pnl,
                assumption='Explicit USD cash-equivalent proxy; not exchange settlement verification',reason=payload['reason']),state)

    def order_status(self,order_id):
        with self.lock:
            row=self.db.execute('SELECT * FROM ledger WHERE order_id=? ORDER BY id DESC LIMIT 1',(order_id,)).fetchone()
            if row is None:return None
            data=json.loads(row['data']);order=data['order']
            return dict(order_id=order_id,status={'ORDER':'PENDING','FILL':'FILLED','REJECT':'REJECTED','CANCEL':'CANCELLED'}[row['kind']],
                ledger_id=row['id'],timestamp=row['timestamp_ms'],wallet_id=order['wallet_id'],run_id=order['run_id'],
                order=order,quote=data.get('quote'),fill=data.get('fill'),reason=data.get('reason'),
                cash_after=data['state_after']['cash'],position_after=data['state_after']['positions'].get(order['instrument']),
                execution_error=self.failure)

    def wallet_runs(self,wallet):
        if wallet not in self.runs:raise ValueError('Unknown wallet '+str(wallet))
        return [dict(json.loads(r['data']),id=r['id']) for r in self.db.execute('SELECT * FROM runs WHERE wallet=? ORDER BY id DESC',(wallet,))]

    def history(self,wallet='OLIVER',run_id=None,limit=100,activity_only=False):
        with self.lock:
            run,_,_=self.resolve_run(wallet,run_id)
            clause=" AND kind IN ('ORDER','FILL','REJECT','CANCEL','SETTLE','CONTROL','CLOSE_RUN')" if activity_only else ''
            return [dict(json.loads(r['data']),ledger_id=r['id'],timestamp=r['timestamp_ms']) for r in self.db.execute('SELECT * FROM ledger WHERE run_id=?'+clause+' ORDER BY id DESC LIMIT ?',(run['id'],min(1000,max(1,int(limit)))))]

    def snapshot(self,wallet=None,run_id=None):
        with self.lock:
            if run_id is not None and wallet is None:raise ValueError('Select a wallet for a historical run')
            return dict(rows=sorted((self.metrics(w,run_id) for w in ([wallet] if wallet else WALLETS)),key=lambda m:-m['equity']),runs=[dict(json.loads(r['data']),id=r['id']) for r in self.db.execute('SELECT * FROM runs ORDER BY id DESC')],
                parameters=asdict(self.config),engine_version=VERSION,paper_only=True,failure=self.failure,
                automation_enabled=any(self.states[w].get('automation_enabled',True) for w in ALGOS) and self.auto_policies,
                policies={w:definition(w) for w in ALGOS},
                review_summary={k:self.performance.report.get(k) for k in ('version','as_of','totals','shared_entry_groups','recording_gaps')},
                limitation='Experimental diagnostic policies, not validated edges. MARTINGALE waits for a calibrated e-process. Options use USD cash-equivalent settlement; no exercise, assignment, shorting or coin collateral.')

    def set_automation(self,enabled):
        if not isinstance(enabled,bool):raise ValueError('enabled must be boolean')
        with self.lock:
            for wallet in ALGOS:
                if not enabled:
                    for key,order in list(self.pending.items()):
                        if order['wallet_id']==wallet:self.append(wallet,'CANCEL',dict(order=order,reason='Automation paused'));self.pending.pop(key)
                self.append(wallet,'CONTROL',dict(automation_enabled=enabled),dict(self.states[wallet],automation_enabled=enabled))
            return dict(enabled=enabled,note='Pause cancels pending algo orders; existing positions remain marked')

    def curves(self,hours=24,run_id=None):
        with self.lock:
            now=self.clock();start=now-int(max(0,hours)*3600000) if hours else 0
            runs=list(self.runs.values()) if run_id is None else [dict(json.loads(r['data']),id=r['id']) for r in self.db.execute('SELECT * FROM runs WHERE id=?',(run_id,))]
            result=[]
            for run in runs:
                beginning=max(start,run['opened_at']);bucket=max(1,(now-beginning)//480)
                samples=self.db.execute('SELECT timestamp_ms,equity,cash,costs,stale_marks FROM equity_points WHERE id IN (SELECT MAX(id) FROM equity_points WHERE run_id=? AND timestamp_ms>=? GROUP BY CAST(timestamp_ms/? AS INTEGER)) ORDER BY timestamp_ms',(run['id'],beginning,bucket)).fetchall()
                points=[dict(r) for r in samples]
                if start<=run['opened_at']:points.insert(0,dict(timestamp_ms=run['opened_at'],equity=run['starting_balance'],cash=run['starting_balance'],costs=0,stale_marks=0))
                if self.runs[run['wallet_id']]['id']==run['id']:
                    m=self.metrics(run['wallet_id']);points.append(dict(timestamp_ms=now,equity=m['equity'],cash=m['cash'],costs=m['fees_paid']+m['slippage_paid'],stale_marks=m['stale_marks']))
                result.append(dict(wallet_id=run['wallet_id'],run_id=run['id'],run_number=run['run_number'],starting_balance=run['starting_balance'],opened_at=run['opened_at'],points=points))
            return dict(series=result,as_of=now,units='USD',sampling='30s marks; latest point is a live bid mark; resets are separate runs')

    def start(self):
        def worker():
            while not self.stop.wait(.5):
                try:self.advance();self.failure=None
                except Exception as error:self.failure=str(error)
        self.thread=threading.Thread(target=worker,name='basis-paper',daemon=True);self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:self.thread.join(timeout=3)
        self.db.close()
