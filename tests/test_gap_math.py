"""Seven targeted math and funded-structure regressions, no new framework."""
import copy
import math
from pathlib import Path
import tempfile
import time
import unittest
import numpy as np
from basislab.gap_math import binary_ev,conditional_ev,conditional_paths,gap_math,horizon_outcome,payoff,vertical
from basislab.market_math import MarketMath
from basislab.pricing import normal_cdf
from basislab.sim import Sim
from basislab.store import Store
from basislab.engine import Engine


def contract(k,bid,ask,kind='call'):
    return dict(instrument=str(k)+kind,option_type=kind,strike=k,bid=bid,ask=ask,iv=.4,mark=(bid+ask)/2)


class GapMathTests(unittest.TestCase):
    def test_tail_gap_odds_and_binary_equivalent_kelly(self):
        r=gap_math(.11,.025)
        self.assertAlmostEqual(r['gap_pp'],8.5)
        self.assertAlmostEqual(r['relative_gap'],3.4)
        self.assertAlmostEqual(r['odds_ratio'],4.8202247191)
        self.assertAlmostEqual(r['binary_kelly'],.08717948718)
        self.assertTrue(math.isfinite(gap_math(1,0)['log_odds_gap']))
        self.assertGreater(gap_math(.0095,.00017)['odds_ratio'],50)
        self.assertIsNone(gap_math(None,.5)['gap_pp'])

    def test_terminal_call_vertical_actual_ask_bid_costs(self):
        t=vertical(contract(100,1.9,2),contract(105,1,1.1),'call',100)
        self.assertEqual([l['price'] for l in t['legs']],[2,1])
        self.assertAlmostEqual(t['debit'],101.9045)
        self.assertEqual(t['max_payout'],500)
        self.assertAlmostEqual(t['q_exec'],.203809)
        self.assertAlmostEqual(binary_ev(.3,t)['ev'],48.0955)

    def test_terminal_put_vertical_buy_high_sell_low(self):
        t=vertical(contract(100,1,1.1,'put'),contract(105,1.9,2,'put'),'put',100)
        self.assertEqual([(l['side'],l['strike'],l['price']) for l in t['legs']],[('BUY',105,2),('SELL',100,1)])
        self.assertAlmostEqual(t['debit'],101.9045)
        self.assertAlmostEqual(t['q_exec'],.203809)
        self.assertEqual(t['max_loss'],t['debit'])

    def test_touch_conditional_reweighting_and_neutral_price(self):
        paths=conditional_paths(100,140,.4,1,1.5,'up',paths=65536)
        t=vertical(contract(100,10,11),contract(120,3,4),'call',1)
        values=payoff(t,paths['terminal']);q=paths['q_sample']
        result=conditional_ev(q,t,paths)
        w=paths['hit_weights']+paths['no_hit_weights']
        self.assertAlmostEqual(result['expected_payoff'],np.dot(w,values)/w.sum(),places=10)
        p=.11;result=conditional_ev(p,t,paths)
        hit_mass=p*paths['hit_weights']/paths['hit_weights'].sum()
        no_hit_mass=(1-p)*paths['no_hit_weights']/paths['no_hit_weights'].sum()
        self.assertAlmostEqual(float(hit_mass.sum()+no_hit_mass.sum()),1)
        self.assertAlmostEqual(float(hit_mass.sum()),p)
        self.assertAlmostEqual(result['expected_payoff'],p*result['payoff_if_hit']+(1-p)*result['payoff_if_no_hit'])
        def call(k):
            d1=(math.log(100/k)+.4**2*1.5/2)/(.4*math.sqrt(1.5))
            return 100*normal_cdf(d1)-k*normal_cdf(d1-.4*math.sqrt(1.5))
        self.assertAlmostEqual(conditional_ev(paths['q_model'],t,paths)['expected_payoff'],call(100)-call(120),delta=.15)

    def test_horizon_quotes_never_see_future_or_other_event(self):
        a=dict(event_id='a',timestamp_wall=1000,pm_yes=.11,opt_yes=.025,spot=100,raw_id=10)
        b=dict(a,timestamp_wall=2000,opt_yes=.03,raw_id=11)
        self.assertAlmostEqual(horizon_outcome(a,b,2000)['opt_toward'],.5)
        self.assertIsNone(horizon_outcome(a,dict(b,timestamp_wall=2001),2000))
        self.assertIsNone(horizon_outcome(a,dict(b,options_received_ms=2001),2000))
        self.assertIsNone(horizon_outcome(a,dict(b,event_id='b'),2000))
        self.assertIsNone(horizon_outcome(a,dict(b,input_refs={'options':12}),2000))

    def test_error_correction_recovers_known_direction_with_controls(self):
        from basislab.gap_history import regression
        rng=np.random.default_rng(42);rows=[]
        for i in range(120):
            z=float(rng.normal());spot=float(rng.normal()*.01)
            rows.append(dict(z=z,spot_recent=spot,iv=.4,tte_days=2,distance=.1,direction='up',block=str(i//3),
                future_spot_return=0,dy=.3*z+2*spot,dx=-.1*z))
        result=regression(rows)
        self.assertAlmostEqual(result['options_beta']['coefficient'],.3)
        self.assertAlmostEqual(result['poly_reversion_b']['coefficient'],.1)
        self.assertIn('IV',result['constant_controls_dropped'])

    def test_funded_atomic_structure_pending_fill_reject_restore(self):
        now=time.time_ns()//1000000
        store=Store(':memory:');engine=Engine(store)
        legs=[contract(100,1.9,2),contract(105,1,1.1)]
        for c in legs:c.update(source_ms=now,quote_quality='DERIBIT_PUBLIC_SNAPSHOT_USD_CONVERSION')
        surface=dict(venue='deribit',expiry=now+86400000,calls=legs,puts=[],received_ms=now,source_ms=now,raw_id=1)
        engine.surfaces[('BTC',surface['expiry'])]=surface
        engine.spots['BTC']=dict(price=100,source_ms=now,received_ms=now)
        engine.events['a']=dict(mapping_hash=None,expiry=surface['expiry'])
        math_engine=MarketMath(engine);trade=dict(vertical(*legs,'call',1),asset='BTC',event_id='a',expiry=surface['expiry'],math_calculated_ms=now,quote_received_ms=now)
        math_engine.tickets['exact']=trade
        with tempfile.TemporaryDirectory() as d:
            sim=Sim(math_engine,Path(d)/'sim.sqlite3')
            order=sim.submit(dict(structure_id='exact',quantity=1))
            self.assertEqual(order['status'],'PENDING');sim.process(order['fill_due_ms']-1)
            self.assertEqual(sim.order(order['order_id'])['status'],'PENDING')
            sim.process(order['fill_due_ms']);fill=sim.order(order['order_id'])
            self.assertEqual(fill['status'],'FILLED');self.assertAlmostEqual(sim.cash,100000-fill['debit'])
            order=sim.submit(dict(structure_id='exact',quantity=1));surface['received_ms']=now-45001
            sim.process(order['fill_due_ms']);self.assertIn('QUOTE_STALE',sim.order(order['order_id'])['reason'])
            balance=sim.cash;sim.close();sim=Sim(math_engine,Path(d)/'sim.sqlite3')
            self.assertEqual(sim.cash,balance);self.assertEqual(len(sim.positions),1);sim.close()
        store.close()
