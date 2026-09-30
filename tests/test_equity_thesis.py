import math
import unittest
from basislab.engine import Engine
from basislab.equities import calendar, session_close
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
