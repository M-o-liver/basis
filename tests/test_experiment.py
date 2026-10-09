"""Forward-account integrity and actual-fill accounting, separate from stars."""
from pathlib import Path
import sqlite3
import tempfile
import unittest

from basislab.experiment import Experiment


class ExperimentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'experiment.sqlite3'
        self.journal = Experiment(self.path)
        self.journal.append('ACCOUNT', dict(account='paper-margin', equity=100000,
            option_buying_power=100000, cash=100000, quote_status='DELAYED',
            paper_money_visible=True, evidence='Visible GUI account and mode'), 1000)

    def tearDown(self):
        self.journal.close()
        self.tmp.cleanup()

    def plan(self):
        self.journal.append('PLAN', dict(trade_id='test', signal=dict(event_id='event', raw_id=10,
            timestamp_wall=1000, input_refs={'pm':10}), legs=[dict(instrument='EXACT', side='BUY', expiry=9000, ratio=1)],
            hypothesis='Repricing', entry_criteria='Fresh executable quotes', exit_conditions='Fixed horizon',
            model_version='v1', strategy_version='s1', signal_category='PM_CREATED', max_loss=500,
            evidence='Captured BASIS snapshot'), 1100)

    def fill(self, identity, action, quantity, price, fees):
        return self.journal.append('FILL', dict(trade_id='test', broker_fill_id=identity, action=action,
            quantity=quantity, unit_cash=price, fees=fees, contracts=['EXACT'], evidence='Actual paperMoney fill'), 1200)

    def test_baseline_survives_restart_and_drawdown_is_sampled(self):
        self.journal.append('ACCOUNT', dict(account='paper-margin', equity=99000,
            option_buying_power=99000, cash=99000, quote_status='DELAYED',
            paper_money_visible=True, evidence='Later GUI account'), 2000)
        self.journal.close()
        self.journal = Experiment(self.path)
        d = self.journal.snapshot()
        self.assertEqual(d['baseline']['equity'], 100000)
        self.assertEqual(d['equity_change'], -1000)
        self.assertEqual(d['sampled_max_drawdown'], 1000)
        with self.assertRaises(ValueError):
            self.journal.append('ACCOUNT', dict(d['current'], account='other-account'), 3000)
        with self.assertRaises(sqlite3.IntegrityError):
            self.journal.db.execute('DELETE FROM evidence')

    def test_partial_closes_use_fifo_actual_fills_and_fees(self):
        self.plan()
        self.fill('a', 'OPEN', 2, 100, 2)
        self.fill('b', 'OPEN', 1, 120, 1)
        self.fill('c', 'CLOSE', 1, 90, 1)
        d = self.journal.snapshot()
        self.assertEqual(d['realized_net'], -12)
        self.assertEqual(d['trades'][0]['open_quantity'], 2)
        self.fill('d', 'CLOSE', 2, 150, 2)
        d = self.journal.snapshot()
        self.assertEqual(d['realized_gross'], 70)
        self.assertEqual(d['realized_net'], 64)
        self.assertEqual(d['actual_fees'], 6)
        self.assertEqual(d['trade_count'], 1)
        self.assertEqual(d['completed_trades'], 1)
        with self.assertRaises(ValueError):
            self.fill('d', 'CLOSE', 1, 150, 1)
        with self.assertRaises(ValueError):
            self.fill('e', 'CLOSE', 1, 150, 1)
        self.assertEqual(self.journal.snapshot()['realized_net'], 64)

    def test_unknown_costs_are_not_zero_profit_and_quotes_are_not_fills(self):
        self.plan()
        self.journal.append('MARK', dict(trade_id='test', unit_credit=200, quote_status='DELAYED', evidence='Quote only'), 1150)
        self.assertEqual(self.journal.snapshot()['trade_count'], 0)
        self.fill('a', 'OPEN', 1, 100, None)
        self.fill('b', 'CLOSE', 1, 150, 1)
        d = self.journal.snapshot()
        self.assertEqual(d['realized_gross'], 50)
        self.assertIsNone(d['realized_net'])
        self.assertIsNone(d['actual_fees'])
        self.journal.append('COST',dict(broker_fill_id='a',fees=2,evidence='Broker fee confirmed later'),1500)
        self.assertEqual(self.journal.snapshot()['realized_net'],47)
        original=[r for r in self.journal.records() if r['kind']=='FILL'][0]
        self.assertIsNone(original['data']['fees'])

    def test_missing_paper_identity_and_future_observation_rejected(self):
        with self.assertRaises(ValueError):
            self.journal.append('ACCOUNT', dict(account='paper-margin', equity=100000,
                option_buying_power=100000, cash=100000, quote_status='DELAYED',
                paper_money_visible=False, evidence='Unverified'), 2000)
        with self.assertRaises(ValueError):
            self.journal.append('RESEARCH', dict(finding='Future', evidence='Invalid'), 2**63-1)
