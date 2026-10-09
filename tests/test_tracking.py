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
        self.consume_quote(opened+720000,missing=True);self.assertEqual(self.tracker.stars[key]['status'],'WAITING')
        self.consume_quote(opened+1014000);self.assertEqual(self.tracker.stars[key]['entry']['actual_entry_receipt'],opened+1014000)
        self.assertEqual(self.tracker.stars[key]['entry']['entry_delay_after_target_ms'],1004000)
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

    def test_unavailable_structure_does_not_create_a_dead_star(self):
        self.math.displayed['shown'].update(trade=None,trade_reason='MISSING_ACTUAL_BID_ASK')
        with self.assertRaisesRegex(ValueError,'MISSING_ACTUAL_BID_ASK'):self.tracker.create('a','shown',self.now)
        self.assertEqual(self.tracker.snapshot()['rows'],[])
        self.assertEqual(self.tracker.db.execute('SELECT COUNT(*) FROM stars').fetchone()[0],0)

    def test_new_asset_recovers_its_prior_path_before_first_entry(self):
        self.tracker.create('a','shown',self.now);self.consume_quote(self.now+7000)
        start=(self.now//86400000-1)*86400000
        event=dict(self.engine.events['a'],event_id='b',asset='ETH',yes_token='ethyes',mapping_hash='eth-map',event_type='touch',strike_or_threshold=105,window_start=start)
        self.engine.manual['b']=event;self.engine.events['b']=event
        payload=self.options(self.now+8000)
        for c in payload['result']:c['instrument_name']=c['instrument_name'].replace('BTC-','ETH-')
        self.engine.ingest('coinbase','spot','ETH',dict(price=100,timestamp=self.now+8000),received_ms=self.now+8000)
        self.engine.ingest('deribit','options','ETH',payload,received_ms=self.now+8000,source_ms=self.now+8000)
        self.engine.ingest('clob','pm','tokens',dict(event_type='book',asset_id='ethyes',timestamp=self.now+9000,bids=[dict(price=.94,size=1)],asks=[dict(price=.96,size=1)]),received_ms=self.now+9000)
        history_id=self.engine.ingest('coinbase','history','ETH',dict(start_ms=start,candles=[[start//1000,90,101,100,100,1]]),received_ms=self.now+9000)
        self.tracker.process();self.assertNotIn(('ETH',start),self.tracker.reducer.histories)
        trade=dict(self.math.displayed['shown']['trade'],asset='ETH',event_id='b',event_version='eth-map')
        trade['legs']=[dict(l,instrument=l['instrument'].replace('BTC-','ETH-')) for l in trade['legs']]
        self.math.displayed['eth']=dict(self.engine.latest['b'],trade=trade,event_semantics=event,calculated_at=self.now+10000,math=gap_math(.95,self.engine.latest['b']['opt_yes']))
        star=self.tracker.create('b','eth',self.now+10000);self.tracker.process()
        self.assertEqual(self.tracker.reducer.histories[('ETH',start)]['raw_id'],history_id)
        for c in payload['result']:c['creation_timestamp']=self.now+16000
        self.engine.ingest('deribit','options','ETH',payload,received_ms=self.now+16000,source_ms=self.now+16000);self.tracker.process()
        entry=self.tracker.stars[star['tracking_id']]['entry']
        self.assertIsNotNone(entry['q_entry']);self.assertIsNotNone(entry['gap_entry_pp'])
        self.assertEqual(entry['input_refs']['history'],history_id)

    def test_remove_hides_failed_and_live_stars_preserving_history_after_restart(self):
        a=self.tracker.create('a','shown',self.now);self.consume_quote(self.now+7000)
        b=self.tracker.create('a','shown',self.now+8000)
        self.tracker.append(b['tracking_id'],'MISSED',dict(timestamp_ms=self.now+9000,reason='NO_VALID_POST_TARGET_QUOTE'))
        originals={key:encode(s['frozen']) for key,s in self.tracker.stars.items()};mark=self.tracker.stars[a['tracking_id']]['mark']
        self.tracker.remove(b['tracking_id'],self.now+10000);self.tracker.remove(a['tracking_id'],self.now+11000)
        self.assertEqual(self.tracker.snapshot()['rows'],[])
        self.assertEqual(self.tracker.snapshot(a['tracking_id'])['summary']['status'],'ENDED')
        self.assertEqual(self.tracker.snapshot(a['tracking_id'])['mark'],mark)
        events=self.tracker.db.execute('SELECT COUNT(*) FROM events').fetchone()[0]
        self.tracker.remove(b['tracking_id'],self.now+12000)
        self.assertEqual(self.tracker.db.execute('SELECT COUNT(*) FROM events').fetchone()[0],events)
        path=Path(self.tmp.name)/'stars.sqlite3';self.tracker.close();self.tracker=Tracker(self.math,path)
        self.assertEqual(self.tracker.snapshot()['rows'],[]);self.assertEqual(self.tracker.snapshot()['removed_count'],2)
        for key,original in originals.items():self.assertEqual(encode(self.tracker.stars[key]['frozen']),original)
