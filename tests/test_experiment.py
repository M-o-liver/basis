"""Forward-account integrity and actual-fill accounting, separate from stars."""
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from basislab.experiment import Experiment, payoff_budget


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

    def test_peak_survives_a_later_loss_and_restart_without_resetting_baseline(self):
        def account(equity, observed):
            self.journal.append('ACCOUNT', dict(account='paper-margin', equity=equity,
                option_buying_power=equity, cash=equity, quote_status='DELAYED',
                paper_money_visible=True, evidence='Observed paperMoney account'), observed)
        account(120000, 2000)
        account(120000, 2500)
        account(95000, 3000)
        self.journal.close()
        self.journal = Experiment(self.path)
        d = self.journal.snapshot()
        self.assertEqual(d['baseline']['equity'], 100000)
        self.assertEqual(d['current']['equity'], 95000)
        self.assertEqual(d['sampled_peak_equity'], 120000)
        self.assertEqual(d['sampled_peak_observed_ms'], 2000)
        self.assertEqual(d['sampled_peak_gain'], 20000)
        self.assertEqual(d['sampled_max_drawdown'], 25000)

    def test_post_experiment_balance_and_quote_marks_cannot_raise_score(self):
        self.journal.append('MANDATE', dict(start_ms=1000, end_ms=2000, git_revision='v1',
            objective='Thirty days', evidence='Original instruction'), 1100)
        self.plan()
        self.journal.append('MARK', dict(trade_id='test', unit_credit=900000,
            quote_status='DELAYED', evidence='Option quote, not account equity'), 1500)
        self.journal.append('ACCOUNT', dict(account='paper-margin', equity=999999,
            option_buying_power=999999, cash=999999, quote_status='DELAYED',
            paper_money_visible=True, evidence='After experiment ended'), 3000)
        d = self.journal.snapshot()
        self.assertEqual(d['sampled_peak_equity'], 100000)
        self.assertEqual(d['window_last_account']['equity'], 100000)
        self.assertEqual(d['current']['equity'], 999999)
        self.assertEqual(d['trade_count'], 0)

    def test_future_objective_waits_for_activation_and_preserves_old_mandate(self):
        self.journal.append('MANDATE', dict(start_ms=1000, end_ms=9000, git_revision='v1',
            objective='Original research objective', evidence='Original instruction'), 1100)
        self.journal.append('REVISION', dict(git_revision='v2', reason='Operator changed score',
            historical_result='No retrospective strategy result', forward_status='Effective at 2000',
            objective_update=dict(description='Maximize peak equity', primary_metric='SAMPLED_PEAK_EQUITY',
                effective_ms=2000, strategy_version='s2', sizing_policy='Half Kelly',
                holding_policy='Thesis horizon', risk_budget='VENUE_BUYING_POWER'),
            evidence='Later operator instruction'), 1500)
        with patch('basislab.experiment.time.time_ns', return_value=1900 * 1000000):
            self.assertEqual(self.journal.snapshot()['objective']['description'], 'Original research objective')
            self.assertEqual(payoff_budget(self.journal.snapshot()), 500)
        with patch('basislab.experiment.time.time_ns', return_value=2000 * 1000000):
            d = self.journal.snapshot()
        self.assertEqual(d['objective']['primary_metric'], 'SAMPLED_PEAK_EQUITY')
        self.assertEqual(payoff_budget(d), 100000)
        original = next(r for r in self.journal.records() if r['kind'] == 'MANDATE')
        self.assertEqual(original['data']['objective'], 'Original research objective')
        self.assertEqual(d['window']['end_ms'], 9000)

    def test_work_state_survives_display_tail_and_positions_come_from_fills(self):
        self.plan()
        self.journal.append('STATE', dict(activity='RESEARCH', finding='Inspecting BASIS event',
            next_action='Read actual offered contracts', strategy_version='s2', coverage='ON_DEMAND',
            evidence='Current session'), 1150)
        for i in range(45):
            self.journal.append('RESEARCH', dict(finding='Observation', evidence='Captured tape'), 1160+i)
        self.fill('open', 'OPEN', 2, 100, 2)
        d = self.journal.snapshot()
        self.assertEqual(d['work_state']['next_action'], 'Read actual offered contracts')
        self.assertFalse(any(r['kind'] == 'STATE' for r in d['records']))
        self.assertEqual(d['trades'][0]['open_quantity'], 2)
        self.assertEqual(d['trades'][0]['legs'][0]['instrument'], 'EXACT')
        self.fill('close', 'CLOSE', 2, 90, 2)
        self.assertEqual(self.journal.snapshot()['trades'][0]['open_quantity'], 0)

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

    def test_unexposed_ids_keep_local_identity_and_actual_costs_separate(self):
        self.plan()
        def unknown(local, action, price, fee):
            return dict(trade_id='test', broker_fill_id=None, broker_id_status='NOT_EXPOSED_IN_GUI',
                        local_fill_ref=local, action=action, quantity=1, unit_cash=price,
                        fees=fee, contracts=['EXACT'], evidence='GUI filled row and statement')
        opening = unknown('open-proof', 'OPEN', 100, None)
        self.journal.append('FILL', opening, 1200)
        with self.assertRaises(ValueError):
            self.journal.append('FILL', opening, 1300)
        self.journal.append('FILL', unknown('close-proof', 'CLOSE', 120, 3), 1400)
        self.journal.append('COST', dict(broker_fill_id=None, broker_id_status='NOT_EXPOSED_IN_GUI',
                            local_fill_ref='open-proof', fees=2, evidence='Later statement'), 1500)
        self.assertEqual(self.journal.snapshot()['realized_net'], 15)
        self.assertEqual(self.journal.snapshot()['actual_fees'], 5)
        self.assertIsNone(self.journal.records()[2]['data']['broker_fill_id'])
        with self.assertRaises(ValueError):
            self.journal.append('FILL', dict(opening, local_fill_ref='other', broker_id_status='UNKNOWN'), 1600)
