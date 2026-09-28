import copy
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from basislab.paper import PaperConfig, PaperDesk
from basislab.performance import feedback, review_fills
from basislab.policies import definition, entry_evidence, PARAMETERS


class PaperFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.path=Path(self.directory.name)/'paper.sqlite3'
        self.now=1000000
        self.engine=SimpleNamespace(code_hash='test',lock=threading.RLock(),latest={},analyzer_latest={},surfaces={},spots={})
        self.desks=[]

    def tearDown(self):
        for desk in self.desks:desk.close()
        self.directory.cleanup()

    def desk(self,auto=False):
        d=PaperDesk(self.engine,self.path,PaperConfig(fee_per_contract=.01),clock=lambda:self.now,auto_policies=auto)
        self.desks.append(d)
        d.quote=lambda name,now=None:dict(instrument=name,asset='BTC',kind='option',venue='test',bid=9.9,ask=10.,
            multiplier=1,strike=100,expiry=self.now+86400000,received_ms=self.now,source_ms=self.now,raw_id=1,
            fractional=False,option_type='call',settlement_model='test',estimated=False)
        return d

    def test_attribution_reconciles_and_excludes_future_fills(self):
        buy=dict(ledger_id=1,timestamp_fill=10,order=dict(run_id=1,instrument='x',quantity=2,side='BUY',
            wallet_id='SW',timestamp_submit=9,strategy_version='v1'),quote=dict(bid=9,ask=10,multiplier=1,asset='BTC'),
            fill=dict(fees=1,slippage=.2,total_debit=21.2))
        sell=dict(ledger_id=2,timestamp_fill=20,order=dict(buy['order'],side='SELL',reason='test'),
            quote=dict(bid=11,ask=12,multiplier=1,asset='BTC'),fill=dict(fees=1,slippage=.2),realized_pnl=-.4,position_after=None)
        self.assertEqual(review_fills([buy,sell],19)['totals']['closes'],0)
        r=review_fills([buy,sell],20)
        self.assertAlmostEqual(r['totals']['mid_move_pnl'],4)
        self.assertAlmostEqual(r['totals']['spread_cost'],2)
        self.assertEqual(r['closed'][0]['status'],'ok')

    def test_loss_feedback_is_causal_and_version_run_specific(self):
        rows=[dict(wallet_id='SW',run_id=1,status='ok',complete=True,closed_at=t,ledger_id=t,net_pnl=-10) for t in (10,20,30)]
        self.assertTrue(feedback(dict(closed=rows),'SW',1,29)['entry_allowed'])
        self.assertFalse(feedback(dict(closed=rows),'SW',1,30)['entry_allowed'])
        self.assertTrue(feedback(dict(closed=rows),'SW',2,30)['entry_allowed'])
        self.assertEqual(feedback(dict(closed=rows),'SW',1,3600031)['size_multiplier'],.5)

    def test_upgrade_carries_loss_pause_and_history_without_refill(self):
        old=lambda w:dict(definition(w),strategy_version='old')
        with patch('basislab.paper.definition',side_effect=old):d=self.desk(auto=True)
        old_id=d.runs['SW']['id']
        d.append('SW','FIXTURE',{},dict(d.states['SW'],cash=98765,realized_pnl=-1235,automation_enabled=False))
        d.close();self.desks.remove(d)
        d=self.desk(auto=True)
        self.assertNotEqual(d.runs['SW']['id'],old_id)
        self.assertEqual(d.states['SW']['cash'],98765)
        self.assertEqual(d.runs['SW']['starting_balance'],98765)
        self.assertEqual(d.metrics('SW')['account_pnl'],-1235)
        self.assertFalse(d.states['SW']['automation_enabled'])
        self.assertEqual(d.states['OLIVER']['cash'],100000)
        self.assertTrue(d.history('SW',old_id))

    def test_upgrade_keeps_open_position_in_old_run_to_drain(self):
        with patch('basislab.paper.definition',side_effect=lambda w:dict(definition(w),strategy_version='old')):d=self.desk(auto=True)
        old_id=d.runs['SW']['id']
        d.append('SW','FIXTURE',{},dict(d.states['SW'],positions={'x':dict(quantity=1)}))
        d.close();self.desks.remove(d)
        d=self.desk(auto=True)
        self.assertEqual(d.runs['SW']['id'],old_id)
        self.assertTrue(d.policy_upgrade_pending('SW'))
        self.assertEqual(d.states['SW']['positions']['x']['quantity'],1)

    def test_execution_gap_rejects_old_order_and_holds_new_entries(self):
        d=self.desk();d.submit(dict(wallet='OLIVER',instrument='x',side='BUY',quantity=1,reason='test'))
        self.now+=100000;d.advance()
        self.assertEqual(d.states['OLIVER']['trades'],0)
        self.assertFalse(d.pending)
        self.assertGreater(d.states['SW']['entry_hold_until'],self.now)
        self.assertTrue(any(r['kind']=='REJECT' and 'expired' in r['reason'] for r in d.history()))

    def test_spread_deterioration_is_rechecked_at_fill(self):
        d=self.desk();d.submit(dict(wallet='SW',instrument='x',side='BUY',quantity=1,reason='test',
            policy_context=dict(max_roundtrip_drag=.05)),policy=True)
        original=d.quote;d.quote=lambda *a:dict(original(*a),bid=8)
        self.now+=1001;d.advance()
        self.assertEqual(d.states['SW']['trades'],0)
        self.assertTrue(any(r['kind']=='REJECT' and 'cost' in r['reason'] for r in d.history('SW')))

    def test_selects_affordable_lowest_cost_option(self):
        d=self.desk();self.engine.spots={'BTC':dict(price=100)}
        self.engine.surfaces={('BTC',self.now+86400000):dict(venue='deribit',expiry=self.now+86400000,
            calls=[dict(strike=100,instrument='ATM-C'),dict(strike=101,instrument='NEAR-C')])}
        original=d.quote;d.quote=lambda name:dict(original(name),bid=9.4 if name=='ATM-C' else 9.9)
        row=dict(asset='BTC',expiry=self.now+7200000)
        self.assertEqual(d.policy_instrument(row,1,PARAMETERS,100)['instrument'],'NEAR-C')
        self.assertIsNone(d.policy_instrument(row,1,PARAMETERS,1))

    def test_anomaly_needs_pm_movement_and_neighbor_agreement(self):
        row=dict(event_id='m',asset='BTC',expiry=self.now+999999,event_type='touch',direction='up',
            strike_or_threshold=100,timestamp_wall=self.now,gap_pp=4,source_state='PROXY',pm_source='clob_mid',
            pm_bid=.4,pm_ask=.405,features=dict(changes={'60':dict(pm_pp=.4,opt_pp=.05)}))
        rows={r['event_id']:r for r in [row,dict(row,event_id='n1',strike_or_threshold=105),dict(row,event_id='n2',strike_or_threshold=110)]}
        self.assertTrue(entry_evidence(row,rows,self.now)[0])
        rows['n1']['gap_pp']=-4
        self.assertFalse(entry_evidence(row,rows,self.now)[0])
        rows['n1']['gap_pp']=4;row['features']['changes']['60']['pm_pp']=0
        self.assertFalse(entry_evidence(row,rows,self.now)[0])


if __name__=='__main__':unittest.main()
