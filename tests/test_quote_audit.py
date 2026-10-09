"""Independent pricing/monitoring checks for the model-sensitivity diagnostic."""
import unittest

from basislab.pricing import touch_probability, YEAR_MS
from basislab.quote_audit import implied_iv, option_value, otm_iv, session_probability, monitoring_intervals
from basislab.semantics import timestamp


class QuoteAuditTests(unittest.TestCase):
    def test_inverse_prices_recover_iv_and_exclude_itm_provider_artifact(self):
        price = option_value(100, 90, .3, .1, 'put')
        self.assertAlmostEqual(implied_iv(100, 90, .1, price, 'put'), .3)
        self.assertIsNone(implied_iv(100, 90, .1, 10, 'call'))
        surface = dict(expiry=int(YEAR_MS*.1)+1000,
            calls=[dict(instrument='ITM', strike=90, bid=10, ask=11, iv=4)],
            puts=[dict(instrument='OTM', strike=90, bid=price-.01, ask=price+.01, iv=4)])
        d = otm_iv(surface, 100, 90, 1000)
        self.assertEqual(d['contracts'][0]['instrument'], 'OTM')
        self.assertAlmostEqual(d['mid_iv'], .3)
        self.assertLess(d['bid_iv'], .3)
        self.assertGreater(d['ask_iv'], .3)

    def test_monitored_session_agrees_with_analytic_continuous_barrier(self):
        start = timestamp('2026-10-09T13:30:00Z')
        end = timestamp('2026-10-09T20:00:00Z')
        d = session_probability(100, 99, .8, start, end, 'down')
        expected = touch_probability(100, 99, .8, (end-start)/YEAR_MS, 'down')
        self.assertAlmostEqual(d['probability'], expected, delta=4*d['standard_error'])
        self.assertEqual(d['probability'], d['continuous_control'])

    def test_nights_do_not_count_but_overnight_price_evolves(self):
        start = timestamp('2026-10-09T21:00:00Z')
        end = timestamp('2026-10-12T20:00:00Z')
        d = session_probability(100, 101, .6, start, end, 'up')
        self.assertLess(d['probability'], d['continuous_control'])
        self.assertGreater(d['probability'], 0)
        d = session_probability(102, 101, .6, start, end, 'up')
        self.assertEqual(d['continuous_control'], 1)
        self.assertLess(d['probability'], 1)

    def test_dst_weekend_and_early_close_use_the_exchange_calendar(self):
        d = monitoring_intervals(timestamp('2026-10-30T20:00:00Z'), timestamp('2026-11-02T22:00:00Z'))
        self.assertEqual(d, [(timestamp('2026-11-02T14:30:00Z'), timestamp('2026-11-02T21:00:00Z'))])
        d = monitoring_intervals(timestamp('2026-11-27T00:00:00Z'), timestamp('2026-11-28T00:00:00Z'))
        self.assertEqual(d, [(timestamp('2026-11-27T14:30:00Z'), timestamp('2026-11-27T18:00:00Z'))])
