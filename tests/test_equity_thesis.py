import math
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch
import pandas as pd
from basislab.collector import price_event_slugs
from basislab.config import Config
from basislab.engine import Engine
from basislab.equities import calendar, session_close, snapshot
from basislab.pricing import derive
from basislab.semantics import infer_event, timestamp
from basislab.store import Store
from basislab.thesis import underlying_thesis, expression_thesis
from basislab.replay import EQUITY_MIGRATION, INTERACTION_MIGRATION, reducer_hash

NOW=timestamp('2026-09-28T18:00:00Z')


def stock():
    return dict(id='stock',question='Will Apple (AAPL) hit (LOW) $280 in September?',
        endDate='2026-10-01T03:59:59Z',outcomes=['Yes','No'],clobTokenIds=['y','n'],
        outcomePrices=['.2','.8'],description='NASDAQ regular trading hours')


class EquityThesisTests(unittest.TestCase):
    def test_low_and_new_york_month_boundary(self):
        e=infer_event(stock())
        self.assertEqual(e['direction'],'down');self.assertEqual(e['event_type'],'touch')
        self.assertEqual(e['window_start'],timestamp('2026-09-01T00:00:00-04:00'))
        e=infer_event(dict(stock(),question='Will Bitcoin hit $90000 in September?'))
        self.assertEqual(e['window_start'],timestamp('2026-09-01T00:00:00Z'))

    def test_calendar_excludes_holiday_and_tracks_dst_and_short_session(self):
        self.assertFalse(calendar().is_session('2026-09-07'))
        self.assertEqual(session_close('2026-09-28'),timestamp('2026-09-28T20:00:00Z'))
        self.assertEqual(session_close('2026-11-30'),timestamp('2026-11-30T21:00:00Z'))
        self.assertEqual(session_close('2026-11-27'),timestamp('2026-11-27T18:00:00Z'))

    def test_calendar_covers_listed_leaps_beyond_default_library_bounds(self):
        self.assertEqual(session_close('2029-01-19'),timestamp('2029-01-19T21:00:00Z'))

    def test_stock_chain_collection_survives_leaps_and_one_chain_failure(self):
        now=timestamp('2026-10-01T19:00:00Z')
        ticker=Mock()
        ticker.history.return_value=pd.DataFrame([dict(Low=299,High=301,Open=300,Close=300,Volume=1000)],
            index=pd.DatetimeIndex(['2026-10-01'],tz='America/New_York'))
        ticker.history_metadata=dict(regularMarketPrice=300,regularMarketTime=now//1000)
        ticker.calendar={};ticker.options=('2026-10-02','2026-11-06','2029-01-19')
        calls=pd.DataFrame([dict(contractSymbol='AAPL261106C00320000',strike=320,bid=4,ask=5,lastPrice=4.5,
            impliedVolatility=.3,lastTradeDate='2026-10-01',volume=10,openInterest=20)])
        puts=calls.assign(contractSymbol='AAPL261106P00320000',bid=23,ask=24)
        ticker.option_chain.return_value=Mock(calls=calls,puts=puts)
        cutoffs=[timestamp('2026-10-02T16:00:00Z'),timestamp('2026-11-01T03:59:59Z')]
        with patch('yfinance.Ticker',return_value=ticker):
            data=snapshot('AAPL',cutoffs,(),72,now)
            self.assertEqual([c['expiry'] for c in data['chains']],
                [session_close('2026-10-02'),session_close('2026-11-06')])
            self.assertEqual(data['partial_errors'],[])
            self.assertFalse(data['expiry_coverage'][1]['within_model_window'])
            surface=dict(data['chains'][1],venue='yahoo',received_ms=now,source_ms=now)
            event=dict(expiry=cutoffs[1],event_type='touch',direction='up',strike_or_threshold=320)
            result=derive(event,dict(yes=.2,source_ms=now),surface,dict(price=300),None,now,Config())
            self.assertIsNone(result['opt_yes']);self.assertEqual(result['source_state'],'CUTOFF')
            self.assertEqual(data['chains'][1]['puts'][0]['bid'],23)
            def chain_with_outage(date):
                if date=='2026-10-02':raise RuntimeError('provider timeout')
                return Mock(calls=calls,puts=puts)
            ticker.option_chain.side_effect=chain_with_outage
            partial=snapshot('AAPL',cutoffs,(),72,now)
            self.assertEqual([c['expiry'] for c in partial['chains']],[session_close('2026-11-06')])
            self.assertEqual(partial['partial_errors'][0]['date'],'2026-10-02')

    def test_catalog_targets_daily_eth_and_btc_with_correct_year(self):
        slugs=price_event_slugs(datetime(2026,10,1,tzinfo=timezone.utc))
        self.assertIn('ethereum-above-on-october-2-2026',slugs)
        self.assertIn('bitcoin-above-on-october-1-2026',slugs)
        self.assertNotIn('ethereum-above-on-october-2',slugs)
        rollover=price_event_slugs(datetime(2026,12,31,tzinfo=timezone.utc))
        self.assertIn('ethereum-above-on-january-1-2027',rollover)
        self.assertIn('what-price-will-bitcoin-hit-in-january-2027',rollover)

    def test_session_history_requires_every_expected_session(self):
        s=Store(':memory:');e=Engine(s)
        p=dict(start_ms=timestamp('2026-09-01T04:00:00Z'),session_calendar='XNYS',
            required_sessions=['2026-09-04','2026-09-08'],sessions=['2026-09-04','2026-09-08'],
            candles=[[timestamp(d+'T13:30:00Z')//1000,90,110,100,101,1000] for d in ('2026-09-04','2026-09-08')])
        e.ingest('yahoo','history','AAPL',p,received_ms=NOW)
        self.assertFalse(e.histories[('AAPL',p['start_ms'])]['error'])
        e.ingest('yahoo','history','AAPL',dict(p,sessions=p['sessions'][:1],candles=p['candles'][:1]),received_ms=NOW+1)
        self.assertTrue(e.histories[('AAPL',p['start_ms'])]['error']);s.close()

    def test_migration_records_reclassification_and_clears_only_equity_state(self):
        s=Store(':memory:');e=Engine(s)
        e.ingest('gamma','catalog','all',dict(markets=[stock()]),received_ms=NOW)
        e.catalog['stock']['direction']='up'
        e.spots={'AAPL':dict(price=300),'BTC':dict(price=90000)}
        e.restore_migration='equity-context-1';e.start_session()
        self.assertEqual(e.catalog['stock']['direction'],'down')
        self.assertNotIn('AAPL',e.spots);self.assertIn('BTC',e.spots)
        raw=s.raw_record(e.last_raw_id)
        self.assertEqual(raw['payload']['checkpoint_migration'],'equity-context-1')
        self.assertEqual(INTERACTION_MIGRATION[0],EQUITY_MIGRATION[1])
        self.assertEqual(reducer_hash('basislab'),INTERACTION_MIGRATION[1]);s.close()

    def test_underlying_opposes_bearish_alarm(self):
        row=dict(event_id='m',asset='BTC',spot=110,strike_or_threshold=120,event_type='touch',direction='up',
            expiry=NOW+86400000,pm_yes=.2,opt_yes=.3,gap_pp=-10,source_state='PROXY',surface_features=dict(local_iv=.5))
        history=[dict(timestamp_wall=NOW-(60-i)*60000,spot=100+i/6,source_state='PROXY') for i in range(61)]
        result=underlying_thesis(row,history,NOW)
        self.assertFalse(result['eligible'])
        self.assertTrue(any('opposes' in reason for reason in result['rejection_reasons']))
        future=dict(timestamp_wall=NOW+1,spot=1,source_state='PROXY')
        self.assertEqual(result,underlying_thesis(row,history+[future],NOW))

    def test_catalyst_and_closed_session_block_stock_automation(self):
        row=dict(event_id='m',asset='AAPL',spot=300,strike_or_threshold=320,event_type='touch',direction='up',
            expiry=NOW+86400000,pm_yes=.3,opt_yes=.2,gap_pp=10,source_state='PROXY',surface_features=dict(local_iv=.5),
            underlying_context=dict(market_state='CLOSED',catalyst=dict(status='AVAILABLE',earnings_dates=[NOW+3600000])))
        reasons=underlying_thesis(row,[],NOW)['rejection_reasons']
        self.assertTrue(any('closed' in r for r in reasons));self.assertTrue(any('Earnings' in r for r in reasons))


if __name__=='__main__':unittest.main()
