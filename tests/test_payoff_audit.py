"""Pricing identities, quote uncertainty and source isolation for payoff audit."""
import copy
import math
import unittest

import numpy as np

from basislab.gap_math import execution_cost, payoff
from basislab.payoff_audit import audit, conditional_statistics, european_value, from_snapshot, scenario_iv, session_paths
from basislab.pricing import YEAR_MS, touch_probability
from basislab.semantics import timestamp


class PayoffAuditTests(unittest.TestCase):
    def test_single_session_hit_matches_reflection_principle(self):
        start, end = timestamp('2026-10-09T13:30:00Z'), timestamp('2026-10-09T20:00:00Z')
        s = session_paths(100, 99, .8, start, end, end, 'down')
        expected = touch_probability(100, 99, .8, (end-start)/YEAR_MS, 'down')
        self.assertAlmostEqual(s['q'], expected, delta=4*s['q_se'])
        self.assertEqual(s['q'], s['continuous_control'])

    def test_later_expiry_carry_and_variance_allocation_preserve_vanilla_value(self):
        now, cutoff, expiry = (timestamp(s) for s in ('2026-10-09T21:00:00Z', '2026-10-12T20:00:00Z', '2026-10-16T20:00:00Z'))
        expected = european_value(100, 102, .6, (expiry-now)/YEAR_MS, 'call', .05, .01)
        samples = []
        for share in (None, .75):
            s = session_paths(100, 101, .6, now, cutoff, expiry, 'up', paths=65536, session_share=share, rate=.05, dividend=.01)
            values = np.maximum(s['terminal']-102, 0) * s['discount']
            se = values.std(ddof=1)/math.sqrt(len(values))
            self.assertAlmostEqual(float(values.mean()), expected, delta=4*se)
            self.assertAlmostEqual(s['total_variance'], .6**2*(expiry-now)/YEAR_MS)
            self.assertLess(s['q'], s['continuous_control'])
            samples.append(s)
        self.assertAlmostEqual(samples[1]['session_variance_share'], .75)
        self.assertGreater(abs(samples[0]['q']-samples[1]['q']), .01)

    def test_pm_interval_contains_no_information_and_baseline_is_analytic(self):
        now, end = timestamp('2026-10-09T13:30:00Z'), timestamp('2026-10-12T20:00:00Z')
        sample = session_paths(100, 99, .6, now, end, end, 'down')
        legs = [dict(instrument='C100', strike=100, option_type='call', side='BUY', bid=1.9, ask=2.1, price=2.1)]
        trade = dict(legs=legs, **execution_cost(legs, 100))
        baseline = european_value(100, 100, .6, (end-now)/YEAR_MS, 'call')*100
        neutral = conditional_statistics(trade, sample, [sample['q'], sample['q']], baseline)
        self.assertEqual(neutral['information_range'], [0, 0])
        self.assertAlmostEqual(neutral['expiry_ev_range'][0], baseline-trade['debit'])
        uncertain = conditional_statistics(trade, sample, [0, 1], baseline)
        self.assertLess(uncertain['information_range'][0], 0)
        self.assertGreater(uncertain['information_range'][1], 0)
        self.assertLess(uncertain['net_information_lower'], 0)
        h, n, q = neutral['payoff_if_hit'], neutral['payoff_if_no_hit'], sample['q']
        self.assertAlmostEqual(q*h+(1-q)*n, float((payoff(trade,sample['terminal'])*sample['discount']).mean()))

    def test_price_inversion_recalibrates_carry_instead_of_reusing_iv(self):
        price = european_value(100, 90, .3, .1, 'put', .05, .01)
        estimate = dict(contracts=[dict(strike=90, option_type='put', bid=price, ask=price, weight=1)])
        self.assertAlmostEqual(scenario_iv(estimate, 100, .1, 'mid', .05, .01), .3)
        self.assertNotAlmostEqual(scenario_iv(estimate, 100, .1, 'mid', 0, 0), .3, places=3)

    def fixture(self):
        now, cutoff, expiry = (timestamp(s) for s in ('2026-10-09T15:00:00Z', '2026-10-30T20:00:00Z', '2026-11-06T21:00:00Z'))
        years = (expiry-now)/YEAR_MS
        chain = []
        for kind in ('call', 'put'):
            for k in (90, 95, 100, 105):
                value = european_value(100, k, .3, years, kind)
                chain.append(dict(instrument=kind+str(k), option_type=kind, strike=k, bid=max(.01,value-.02), ask=value+.02, received_ms=now, contract_size='REGULAR'))
        row = dict(event_id='test', raw_id=4, asset='TEST', event_type='touch', direction='down', strike_or_threshold=95,
                   spot=100, pm_bid=.1, pm_ask=.9, opt_yes=.6, gap_pp=-10, calculated_at=now,
                   timestamp_wall=now, expiry=cutoff, option_expiry=expiry, chain=chain, surface_raw_id=3)
        surface = dict(venue='yahoo', expiry=expiry, raw_id=3, received_ms=now,
                       calls=[c for c in chain if c['option_type']=='call'], puts=[c for c in chain if c['option_type']=='put'])
        return row, surface

    def test_leg_fit_cannot_hide_offsetting_mispricing_or_overwrite_signal(self):
        row, surface = self.fixture()
        # Raise both call books equally: their vertical debit is unchanged.
        # Aggregate price matching cannot excuse individual model/book errors.
        for c in surface['calls']:
            c['bid'] += 5; c['ask'] += 5
        original = copy.deepcopy(row)
        result = audit(row, surface, 500, paths=4096)
        self.assertEqual(row, original)
        self.assertEqual(result['passing_candidates'], 0)
        calls = [c for c in result['candidates'] if c['kind']=='call_vertical']
        self.assertTrue(calls)
        self.assertTrue(all('MODEL_LEG_MISMATCH' in c['failures'] for c in calls))

    def test_gui_book_rejects_mismatched_contracts_and_pre_signal_receipts(self):
        row, _ = self.fixture()
        original = copy.deepcopy(row)
        book = dict(asset='TEST', expiry=row['option_expiry'], spot=100, received_ms=row['calculated_at']+1000,
                    source_ms=None, quote_status='DELAYED_DATA_VISIBLE', contracts=copy.deepcopy(row['chain']))
        result = from_snapshot(row, book, 500, paths=4096)
        self.assertEqual(row, original)
        self.assertEqual(result['quote_origin'], 'paperMoney_GUI')
        self.assertFalse(result['quote_latency_verified'])
        self.assertEqual(result['signal_to_quote_receipt_ms'], 1000)
        book['contracts'][0]['strike'] = 99
        with self.assertRaisesRegex(ValueError, 'CONTRACT'):
            from_snapshot(row, book, 500)
        book['contracts'] = copy.deepcopy(row['chain'])
        book['received_ms'] = row['calculated_at']-1
        with self.assertRaisesRegex(ValueError, 'PRECEDES'):
            from_snapshot(row, book, 500)

    def test_unverified_past_hit_and_future_monitoring_cannot_be_relabelled(self):
        row, surface = self.fixture()
        row['source_state'] = 'VERIFY_HIT'
        self.assertEqual(audit(row,surface,500)['status'], 'UNAVAILABLE')
        row['source_state'] = 'PROXY'
        row['window_start'] = row['calculated_at']+1
        self.assertEqual(audit(row,surface,500)['reason'], 'FUTURE_MONITORING_WINDOW_UNSUPPORTED')
