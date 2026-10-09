"""Targeted gap and actual-payoff regressions, no new framework."""
import copy
import math
from pathlib import Path
import tempfile
import time
import unittest
import numpy as np
from basislab.gap_math import conditional_ev,conditional_paths,gap_math,horizon_outcome,payoff,vertical
from basislab.market_math import MarketMath
from basislab.pricing import normal_cdf
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

    def test_terminal_conditional_neutral_and_actual_ramp(self):
        paths=conditional_paths(100,100,.4,.1,.1,'up','terminal',paths=65536)
        t=vertical(contract(95,5.5,5.6),contract(105,.8,.9),'call',1)
        q=paths['q_model'];neutral=conditional_ev(q,t,paths)
        self.assertAlmostEqual(neutral['information_value'],0)
        self.assertAlmostEqual(neutral['expected_payoff'],q*neutral['payoff_if_hit']+(1-q)*neutral['payoff_if_no_hit'])
        r=conditional_ev(.11,t,paths)
        self.assertGreater(abs(r['expected_payoff']-.11*t['max_payout']),.5)
        self.assertAlmostEqual(r['information_value'],(.11-q)*(r['payoff_if_hit']-r['payoff_if_no_hit']))
        self.assertAlmostEqual(r['net_ev'],r['ev']-r['estimated_exit_drag'])

    def test_gap_provenance_is_prior_only(self):
        from basislab.gap_math import formation
        a=dict(event_id='a',mapping_hash='x',timestamp_wall=30000,raw_id=8,pm_yes=.6,opt_yes=.4,spot=100)
        before=dict(a,timestamp_wall=0,raw_id=1,pm_yes=.5)
        r=formation(a,before);self.assertEqual(r['provenance'],'PM_CREATED');self.assertTrue(r['fresh_pm_unfollowed'])
        self.assertEqual(formation(a,dict(before,timestamp_wall=1))['reason'],'INSUFFICIENT_CAUSAL_LOOKBACK')
        self.assertEqual(formation(a,dict(before,mapping_hash='future'))['reason'],'INSUFFICIENT_CAUSAL_LOOKBACK')

    def test_nonlinear_curve_and_right_censoring(self):
        from basislab.gap_shape import response_curve,hazard_summary
        rows=[dict(block=str(i),event_id=str(i),gap_pp=.5,opt_toward=.2) for i in range(20)]
        rows += [dict(block=str(i+20),event_id=str(i+20),gap_pp=6,opt_toward=1.8) for i in range(20)]
        curve={p['x']:p for p in response_curve(rows)}
        self.assertAlmostEqual(curve[.5]['mean_pp'],.2);self.assertAlmostEqual(curve[6]['mean_pp'],1.8)
        r=dict(block='a',event_id='a',censor_s=15,censor_reason='MISSING',close25_s=None,half_s=None,full_s=None,double_s=None,time_to_max_s=0)
        self.assertIsNone(hazard_summary([r])['horizons']['7200']['p_50_closed'])
        self.assertEqual(hazard_summary([dict(r,censor_s=7200)])['horizons']['7200']['p_50_closed'],0)
