"""Five focused regressions for immutable, delayed, exact-contract tracking."""
import copy
from pathlib import Path
import sqlite3
import tempfile
import unittest
from basislab.engine import Engine
from basislab.gap_math import gap_math,vertical
from basislab.gap_trades import next_open,liquidation
from basislab.market_math import MarketMath
from basislab.semantics import timestamp
from basislab.store import Store,encode
from basislab.tracking import Tracker

NOW=timestamp('2030-01-15T12:00:00Z');EXPIRY=timestamp('2030-01-16T08:00:00Z')


class TrackingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name)/'tape.sqlite3');self.engine=Engine(self.store);self.engine.start_session()
        self.asset='BTC';self.now=NOW;self.expiry=EXPIRY
        self.setup_market()
    def tearDown(self):
        self.tracker.close();self.store.close();self.tmp.cleanup()
    def setup_market(self,asset='BTC',now=NOW,expiry=EXPIRY):
        self.asset=asset;self.now=now;self.expiry=expiry
        e=dict(event_id='a',event_text='A fixed terminal proposition',asset=asset,expiry=expiry,event_type='terminal',direction='up',
            strike_or_threshold=100,window_start=None,yes_token='yes',mapping_hash='frozen-map',mapping_origin='operator',
            settlement_source='coinbase' if asset=='BTC' else 'yahoo',threshold_inclusive=False,pm_indicative=.95,active=True,volume_24h=1)
        self.engine.manual['a']=e;self.engine.events['a']=e
        if asset=='BTC':self.engine.ingest('coinbase','spot',asset,dict(price=100,timestamp=now),received_ms=now)
        self.engine.ingest('clob','pm','tokens',dict(event_type='book',asset_id='yes',timestamp=now,bids=[dict(price=.94,size=1)],asks=[dict(price=.96,size=1)]),received_ms=now)
        self.engine.ingest('deribit' if asset=='BTC' else 'yahoo','options',asset,self.options(now),received_ms=now,source_ms=now)
        surface=self.engine.surfaces[(asset,expiry)];trade=vertical(surface['calls'][0],surface['calls'][-1],'call',1 if asset=='BTC' else 100)
        trade.update(asset=asset,event_id='a',expiry=expiry,event_cutoff=expiry,event_version='frozen-map',math_version='gap-math-2.0',information_value=1,structure_id='exact')
        self.math=MarketMath(self.engine);row=dict(self.engine.latest['a'],trade=trade,calculated_at=now,math=gap_math(.95,.49),event_semantics=e,
            pm_yes=.95,opt_yes=.49,formation=dict(provenance='PM_CREATED'),math_version='gap-math-2.0')
        self.math.displayed['shown']=row;self.engine.save_checkpoint()
        self.tracker=Tracker(self.math,Path(self.tmp.name)/'stars.sqlite3')
    def options(self,at,missing=False):
        strikes=(95,100) if missing else (95,100,105)
        if self.asset=='BTC':
            return dict(result=[dict(instrument_name=f'BTC-16JAN30-{k}-C',underlying_price=100,bid_price=b/100,ask_price=a/100,mark_price=(b+a)/200,mark_iv=40,creation_timestamp=at)
                for k,b,a in ((95,5.0,5.05),(100,.7,.75),(105,.02,.03)) if k in strikes])
        return dict(chains=[dict(expiry=self.expiry,calls=[dict(instrument=f'{self.asset}{k}C',strike=k,option_type='call',bid=b,ask=a,mark=(b+a)/2,iv=.4) for k,b,a in ((95,5.0,5.05),(100,.7,.75),(105,.02,.03)) if k in strikes],puts=[])],spot=100,spot_source_ms=at,underlying_context=dict(market_state='OPEN' if at>=next_open(self.now) else 'CLOSED'))
    def consume_quote(self,at,missing=False):
        self.engine.ingest('deribit' if self.asset=='BTC' else 'yahoo','options',self.asset,self.options(at,missing),received_ms=at,source_ms=at)
        record=self.store.raw_record(self.engine.last_raw_id);self.tracker.process()
        return record
    def test_crypto_first_valid_receipt_after_five_seconds(self):
        star=self.tracker.create('a','shown',self.now);key=star['tracking_id'];self.consume_quote(self.now+4999)
        self.assertEqual(self.tracker.stars[key]['status'],'WAITING')
        self.consume_quote(self.now+7000);s=self.tracker.stars[key]
        self.assertEqual(s['status'],'LIVE');self.assertEqual(s['entry']['actual_entry_receipt'],self.now+7000)
        self.assertEqual(s['entry']['entry_delay_after_target_ms'],2000)
        self.assertEqual(s['frozen']['snapshot']['pm_yes'],.95);self.assertIsNotNone(s['entry']['q_entry'])
    def test_closed_stock_waits_for_first_valid_post_open_receipt(self):
        self.tracker.close();self.store.close();self.store=Store(Path(self.tmp.name)/'stock.sqlite3');self.engine=Engine(self.store);self.engine.start_session()
        now=timestamp('2030-01-11T23:00:00Z');expiry=timestamp('2030-01-18T21:00:00Z')
        self.setup_market('AAPL',now,expiry);star=self.tracker.create('a','shown',now);key=star['tracking_id'];opened=next_open(now)
        self.assertEqual(star['target_entry'],opened+10000)
        self.consume_quote(opened+9999);self.assertEqual(self.tracker.stars[key]['status'],'WAITING')
        self.consume_quote(opened+54000);self.assertEqual(self.tracker.stars[key]['entry']['actual_entry_receipt'],opened+54000)
        self.assertEqual(self.tracker.stars[key]['entry']['entry_delay_after_target_ms'],44000)
    def test_exact_contracts_never_substitute(self):
        star=self.tracker.create('a','shown',self.now);key=star['tracking_id'];symbols=[l['instrument'] for l in self.tracker.stars[key]['frozen']['structure']['legs']]
        self.consume_quote(self.now+6000,missing=True)
        self.assertEqual(self.tracker.stars[key]['status'],'WAITING');self.assertEqual(self.tracker.stars[key]['reason'],'EXACT_CONTRACT_UNAVAILABLE')
        self.math.displayed['shown']['trade']['legs'][1]['instrument']='a different better quote'
        self.consume_quote(self.now+8000)
        self.assertEqual([l['instrument'] for l in self.tracker.stars[key]['entry']['legs']],symbols)
    def test_executable_liquidation_long_bid_short_ask_with_exit_drag(self):
        star=self.tracker.create('a','shown',self.now);key=star['tracking_id'];self.consume_quote(self.now+7000)
        s=self.tracker.stars[key];mark=liquidation(s['entry']['legs'],s['frozen']['structure']['multiplier'])
        self.assertAlmostEqual(s['mark']['liquidation_credit'],5.0-.03-mark['fees']-mark['slippage'])
        self.assertAlmostEqual(s['mark']['pnl_1x'],s['mark']['liquidation_credit']-s['entry']['debit'])
        self.assertGreater(s['mark']['mid_mark_pnl'],s['mark']['pnl_1x'])
    def test_restart_keeps_waiting_live_and_original_snapshot_immutable(self):
        a=self.tracker.create('a','shown',self.now);self.consume_quote(self.now+7000)
        b=self.tracker.create('a','shown',self.now+8000);before=encode(self.tracker.stars[a['tracking_id']]['frozen'])
        path=Path(self.tmp.name)/'stars.sqlite3';self.tracker.close();self.tracker=Tracker(self.math,path)
        self.assertEqual(self.tracker.stars[a['tracking_id']]['status'],'LIVE');self.assertEqual(self.tracker.stars[b['tracking_id']]['status'],'WAITING')
        self.assertEqual(encode(self.tracker.stars[a['tracking_id']]['frozen']),before)
        with self.assertRaises(sqlite3.IntegrityError):self.tracker.db.execute('DELETE FROM stars')
        ended=self.tracker.finish(a['tracking_id'],self.now+9000);self.assertEqual(ended['status'],'ENDED')
        self.assertEqual(encode(self.tracker.stars[a['tracking_id']]['frozen']),before)
