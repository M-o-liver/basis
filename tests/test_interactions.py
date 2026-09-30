"""Regressions for actual ticket/discovery failures; no external feed required."""
from pathlib import Path
import tempfile
import unittest
import sqlite3
import random
from unittest.mock import patch

from basislab.config import Config
from basislab.engine import Engine
from basislab.paper import PaperDesk, QuoteUnavailable
from basislab.pricing import discrepancy
from basislab.semantics import timestamp
from basislab.store import Store, encode
from test_research import feed, options, NOW


class InteractionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(':memory:')
        self.engine=Engine(self.store);feed(self.engine)
        self.now=NOW+10
        self.desk=PaperDesk(self.engine,Path(self.tmp.name)/'paper.sqlite3',clock=lambda:self.now,auto_policies=False)

    def tearDown(self):
        self.desk.close();self.store.close();self.tmp.cleanup()

    def yahoo(self,state='OPEN'):
        self.now=timestamp('2026-09-29T15:00:00Z');expiry=self.now+86400000
        self.engine.ingest('yahoo','options','AAPL',dict(spot=200,spot_source_ms=self.now-3600000,
            underlying_context=dict(market_state=state),chains=[dict(expiry=expiry,
            calls=[dict(instrument='AAPL-C',strike=200,bid=3,ask=3.1,iv=.3,option_type='call')],puts=[])]),received_ms=self.now)
        return self.now

    def test_explicit_context_never_substitutes_another_event_or_asset(self):
        for event_id in ('missing',''):
            with self.assertRaisesRegex(ValueError,'Unknown or inactive event'):self.desk.instruments(event_id)
            with self.assertRaisesRegex(ValueError,'Unknown or inactive event'):
                self.desk.preview(dict(instrument='SPOT:BTC',quantity=1,basis_event_id=event_id))
        with self.assertRaisesRegex(ValueError,'explicit asset'):self.desk.instruments()
        with self.assertRaisesRegex(ValueError,'does not match'):self.desk.instruments('m','ETH')
        self.assertEqual(self.desk.instruments('m')['asset'],'BTC')

    def test_real_deribit_puts_are_discoverable_without_changing_calls(self):
        before=self.engine.latest['m']['opt_yes'];calls=list(next(iter(self.engine.surfaces.values()))['calls'])
        payload=options();payload['result'].append(dict(instrument_name='BTC-16JAN30-110-P',underlying_price=100,
            bid_price=.11,ask_price=.12,mark_price=.115,mark_iv=62))
        self.engine.ingest('deribit','options','BTC',payload,received_ms=self.now)
        surface=next(iter(self.engine.surfaces.values()))
        self.assertEqual(surface['calls'],calls);self.assertEqual(self.engine.latest['m']['opt_yes'],before)
        put=next(q for q in self.desk.instruments('m')['rows'] if q['instrument'].endswith('-P'))
        self.assertEqual((put['bid'],put['ask'],put['iv'],put['option_type']),(11,12,.62,'put'))

    def test_yahoo_receipt_window_matches_cadence_and_retains_delay(self):
        received=self.yahoo();self.now+=60000
        q=self.desk.quote('AAPL-C');spot=self.desk.quote('SPOT:AAPL')
        self.assertEqual(q['receipt_max_age_ms'],self.engine.config.yahoo_seconds*2000)
        self.assertEqual(q['underlying_source_ms'],received-3600000);self.assertIsNone(q['source_ms'])
        self.assertTrue(q['estimated']);self.assertEqual(spot['source_ms'],received-3600000)
        self.now=received+120001
        with self.assertRaisesRegex(QuoteUnavailable,'retrieval is stale'):self.desk.quote('AAPL-C')
        self.now=NOW+46000
        with self.assertRaisesRegex(QuoteUnavailable,'stale'):self.desk.quote('SPOT:BTC')

    def test_closed_stock_discovery_explains_empty_result_and_missing_chains(self):
        self.yahoo('CLOSED');result=self.desk.instruments(asset='AAPL')
        self.assertFalse(result['rows'])
        closed=next(x for x in result['exclusions'] if x['code']=='MARKET_CLOSED')
        self.assertEqual(closed['count'],2);self.assertIn('stock paper execution unavailable',closed['reason'])
        self.now=timestamp('2026-09-29T22:00:00Z')
        self.engine.surfaces[('AAPL',timestamp('2026-09-30T15:00:00Z'))]['context']['market_state']='OPEN'
        with self.assertRaisesRegex(QuoteUnavailable,'closed'):self.desk.quote('AAPL-C')
        self.assertTrue(any(x['code']=='NO_SURFACE' for x in self.desk.instruments(asset='MISSING')['exclusions']))

    def test_order_status_covers_latency_fill_rejection_cancel_and_old_runs(self):
        payload=dict(wallet='OLIVER',instrument='SPOT:BTC',side='BUY',quantity=1,reason='interaction regression',basis_event_id='m',client_order_id='filled')
        self.desk.submit(payload);self.desk.advance(self.now+999)
        self.assertEqual(self.desk.order_status('filled')['status'],'PENDING')
        self.now+=1000;self.desk.advance();result=self.desk.order_status('filled')
        self.assertEqual(result['status'],'FILLED');self.assertEqual(result['position_after']['quantity'],1)
        self.assertAlmostEqual(result['cash_after'],100000-result['fill']['total_debit'])
        self.assertGreater(result['fill']['fill_price'],result['quote']['ask'])
        self.desk.submit(payload);self.assertEqual(self.desk.states['OLIVER']['trades'],1)
        self.desk.submit(dict(payload,client_order_id='rejected'))
        self.now+=1000;self.engine.spots['BTC']['source_ms']=self.now-46000;self.desk.advance()
        self.assertEqual(self.desk.order_status('rejected')['status'],'REJECTED')
        self.assertIn('stale',self.desk.order_status('rejected')['reason'])
        self.engine.spots['BTC'].update(source_ms=self.now,received_ms=self.now)
        self.desk.submit(dict(payload,client_order_id='cancelled'));old=self.desk.runs['OLIVER']['id'];self.desk.reset('OLIVER')
        self.assertEqual(self.desk.order_status('cancelled')['status'],'CANCELLED')
        self.assertEqual(self.desk.metrics('OLIVER',old)['positions'][0]['quantity'],1)
        self.assertEqual(self.desk.history('OLIVER',activity_only=True),[])
        with self.assertRaisesRegex(ValueError,'does not belong'):self.desk.metrics('SW',old)
        self.assertIsNone(self.desk.order_status('missing'))

    def test_selected_scope_applies_before_limit_and_global_count_is_real(self):
        initial=self.store.research_count('episodes')
        for i,event in enumerate(('selected','other','other')):
            row=dict(gap_event_id=str(i),event_id=event,timestamp=self.now)
            self.store.append_episode(self.engine.last_raw_id,row)
        rows=self.store.latest('episodes','gap_event_id',1,event_id='selected')
        self.assertEqual([r['event_id'] for r in rows],['selected']);self.assertEqual(self.store.research_count('episodes'),initial+3)
        self.assertEqual(self.store.latest('episodes','gap_event_id',1,event_id='none'),[])

    def test_tail_relative_overflow_does_not_poison_state_or_checkpoint(self):
        self.assertIsNone(discrepancy(.5,5e-324)['relative_gap'])
        from basislab.engine import derive
        def tail(*args):return dict(derive(*args),opt_yes=5e-324)
        with patch('basislab.engine.derive',side_effect=tail):
            self.engine.ingest('basis','timer','clock',{},received_ms=self.now)
        row=self.engine.latest['m']
        self.assertIn('RELATIVE_GAP_OVERFLOW',row['quality_flags'])
        self.assertAlmostEqual(row['gap_pp'],60);encode(self.engine.snapshot());self.engine.save_checkpoint()
        self.assertFalse(self.engine.errors)

    def test_database_full_error_survives_automatic_transaction_rollback(self):
        pages=self.store.db.execute('PRAGMA page_count').fetchone()[0]
        self.store.db.execute('PRAGMA max_page_count='+str(pages))
        with self.assertRaisesRegex(sqlite3.OperationalError,'database or disk is full'):
            self.engine.ingest('fixture','fixture','fixture',{'noise':random.Random(0).randbytes(1048576).hex()})
        self.assertFalse(self.store.db.in_transaction)
        self.assertEqual(self.engine.persistence_error,'database or disk is full')
        with self.assertRaisesRegex(QuoteUnavailable,'persistence failed'):
            self.desk.quote('SPOT:BTC')
        with self.assertLogs(level='ERROR'):
            self.engine.save_checkpoint()
        self.assertIsNone(self.store.checkpoint())
