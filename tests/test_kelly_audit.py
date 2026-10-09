import unittest

from basislab.kelly_audit import log_optimum, sizing
from tests import test_payoff_audit as payoff_fixtures


class KellyTests(unittest.TestCase):
    def test_binary_known_optimum(self):
        self.assertAlmostEqual(log_optimum([1, -1], [.6, .4]), .2)
        self.assertAlmostEqual(log_optimum([2, -1], [.5, .5]), .25)

    def test_no_positive_edge(self):
        self.assertEqual(log_optimum([1, -1], [.5, .5]), 0)
        self.assertEqual(log_optimum([1, -1], [.4, .6]), 0)

    def test_normalization_and_weighted_zero_mass(self):
        self.assertAlmostEqual(log_optimum([2, -1, 100], [5, 5, 0]), .25)

    def test_no_loss_boundary(self):
        self.assertEqual(log_optimum([.1, 1], [1, 1]), 1)

    def test_invalid_returns(self):
        for returns, weights in [([-2, 1], [1, 1]), ([1], [-1]), ([float('nan')], [1]), ([1], [0]), ([1], [1, 1])]:
            with self.assertRaises(ValueError):
                log_optimum(returns, weights)

    def test_report_preserves_sources_and_labels_horizon(self):
        row, surface = payoff_fixtures.PayoffAuditTests().fixture()
        original = repr(row)
        book = dict(asset=row['asset'], expiry=row['option_expiry'], spot=row['spot'],
                    received_ms=row['calculated_at'] + 1000, source_ms=None,
                    quote_status='DELAYED', contracts=row['chain'])
        result = sizing(row, book, 'put95', 1, 100000)
        self.assertEqual(repr(row), original)
        self.assertEqual(result['horizon'], 'OPTION_EXPIRY')
        self.assertEqual(len(result['results']), 18)
        self.assertEqual(result['signal_raw_id'], row['raw_id'])
        self.assertEqual(result['central']['scenario'], 'QUOTE_MID')
