import copy
from dataclasses import asdict
import json
import math
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
import numpy as np
from basislab.algos import asynchronous_sync, covariance_geometry, event_sync, lead_lag, ordinal_mmd, run_algos, sliced_wasserstein, topology
from basislab.config import Config
from basislab.engine import Engine
from basislab.features import Dynamics, Episodes, curves
from basislab.pricing import discrepancy, finite_spread, terminal_probability, touch_probability
from basislab.replay import restore, verify_replay
from basislab.semantics import infer_event, number, probability, timestamp, validate_mapping
from basislab.store import Store, encode

NOW = timestamp('2030-01-15T12:00:00Z')
END = timestamp('2030-01-16T08:00:00Z')


def market(key='m', strike=100):
    return dict(id=key, question=f'Will Bitcoin close above ${strike} on January 16?', outcomes=['Yes', 'No'], clobTokenIds=['yes'+key, 'no'+key],
                outcomePrices=['0.6', '0.4'], endDate='2030-01-16T08:00:00Z', volume24hr=10000, description='Coinbase BTC-USD at 08:00 UTC')


def options():
    return dict(result=[dict(instrument_name=f'BTC-16JAN30-{strike}-C', underlying_price=100, bid_price=bid,
                 ask_price=ask, mark_price=(bid+ask)/2, mark_iv=50) for strike, bid, ask in ((90, .10, .102), (110, .02, .022))])


def feed(engine, start=NOW):
    engine.ingest('basis', 'session', 'test', dict(config=asdict(engine.config)), received_ms=start)
    engine.ingest('gamma', 'catalog', 'all', dict(markets=[market()]), received_ms=start+1)
    engine.ingest('coinbase', 'spot', 'BTC', dict(price=100, timestamp=start+2), received_ms=start+2)
    engine.ingest('deribit', 'options', 'BTC', options(), received_ms=start+3)
    engine.ingest('clob', 'pm', 'tokens', dict(event_type='book', asset_id='yesm', timestamp=str(start+4), bids=[dict(price='.59',size='20')], asks=[dict(price='.61',size='30')]), received_ms=start+4)


def obs(t, p=.6, q=.4, key='m', gap=None):
    row = dict(event_id=key, event_text='Event', asset='BTC', expiry=END, event_type='terminal', direction='up',
        window_start=None, settlement_source='coinbase', threshold_inclusive=False, mapping_hash='mapping',
        strike_or_threshold=100, timestamp_wall=NOW+t*1000, raw_id=t+1, pm_yes=p, opt_yes=q, spot=100+t*.01,
        source_state='OK', quality_flags=[], opt_timestamp=NOW+t*1000, model_confidence='MEDIUM')
    row.update(discrepancy(p,q))
    if gap is not None: row['gap_pp'] = gap
    return row


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.store=Store(':memory:'); self.engine=Engine(self.store)
    def tearDown(self): self.store.close()
    def test_missing_nonfinite_and_bounds(self):
        for x in (None, '', ' ', False, float('nan'), float('inf'), 'bad'):
            self.assertIsNone(number(x))
        for x in (-.1, 1.1, float('nan')): self.assertIsNone(probability(x))
        self.assertEqual(number('0'),0)
    def test_relative_direction_and_zero_denominator(self):
        row=discrepancy(.0295,.0451)
        self.assertAlmostEqual(row['gap_pp'],-1.56); self.assertAlmostEqual(row['relative_gap'],.0156/.0451); self.assertEqual(row['side'],'RICH')
        self.assertEqual(discrepancy(.6,.4)['side'],'CHEAP'); self.assertIsNone(discrepancy(.1,0)['relative_gap'])
    def test_timestamp_units_and_unknown_timezone(self):
        self.assertEqual(timestamp('2030-01-15T12:00:00Z'), NOW)
        self.assertEqual(timestamp(NOW/1000), NOW)
        self.assertIsNone(timestamp('2030-01-15T12:00:00'))
    def test_parsing_does_not_map_an_index_to_an_etf(self):
        self.assertIsNone(infer_event(dict(market(), question='Will S&P 500 close above $6500?'))['asset'])
        self.assertEqual(infer_event(dict(market(), question='Will Ethereum close above $2000?'))['strike_or_threshold'],2000)
        self.assertEqual(infer_event(dict(market(), question='Will Bitcoin close between $90 and $110?'))['event_type'],'unmapped')
        self.assertEqual(infer_event(dict(market(), question='Will Bitcoin market cap exceed $100 million?'))['event_type'],'unmapped')
    def test_probability_invariants_grid(self):
        for spot in (50,100,200):
            for strike in (1,50,100,200,10000):
                for sigma in (.1,.5,2):
                    up=terminal_probability(spot,strike,sigma,1,'up'); down=terminal_probability(spot,strike,sigma,1,'down')
                    self.assertAlmostEqual(up+down,1); self.assertTrue(0<=up<=1)
                    hit=touch_probability(spot,strike,sigma,1,'up'); self.assertTrue(0<=hit<=1); self.assertGreaterEqual(hit+1e-12,up)
        self.assertIsNone(terminal_probability(0,100,.5,1,'up'))
    def test_gap_sign_symmetry(self):
        for p in np.linspace(0,1,31):
            for q in np.linspace(0,1,31):
                self.assertAlmostEqual(discrepancy(p,q)['gap_pp'],-discrepancy(q,p)['gap_pp'])
    def test_spread_invalid_and_sparse(self):
        self.assertIsNone(finite_spread([dict(strike=90,mark=1)],100))
        self.assertIn('error',finite_spread([dict(strike=90,mark=1),dict(strike=110,mark=4)],100))
    def test_normalized_observation_provenance(self):
        feed(self.engine); row=self.engine.latest['m']
        self.assertAlmostEqual(row['opt_yes'],.4); self.assertEqual(row['basis_method'],'SPREAD')
        self.assertEqual(set(row['input_refs']), {'catalog','pm','options','spot'})
        for ref in row['input_refs'].values(): self.assertLessEqual(ref,row['raw_id']); self.assertIsNotNone(self.store.raw_record(ref))
        self.assertEqual(row['pm_depth']['bids'][0]['size'],'20')
    def test_stale_and_recovery(self):
        feed(self.engine)
        self.engine.ingest('basis','timer','clock',{},received_ms=NOW+100000)
        self.assertEqual(self.engine.latest['m']['source_state'],'STALE')
        feed(self.engine,NOW+101000)
        self.assertIsNotNone(self.engine.latest['m']['gap_pp'])
    def test_delayed_and_future_packets_preserved_but_cannot_rewind_state(self):
        feed(self.engine); old=self.engine.latest['m']['pm_yes']
        for stamp in (NOW-100, NOW+100000):
            self.engine.ingest('clob','pm','tokens',dict(event_type='best_bid_ask',asset_id='yesm',timestamp=stamp,best_bid='.01',best_ask='.02'),received_ms=NOW+10)
        self.assertEqual(self.engine.latest['m']['pm_yes'],old)
        self.assertEqual(self.engine.delayed_packets,1); self.assertEqual(self.engine.clock_errors,1)
        self.assertEqual(self.store.stats()['raw'],7)
    def test_crossed_book_not_gamma_fallback(self):
        feed(self.engine)
        self.engine.ingest('clob','pm','tokens',dict(event_type='best_bid_ask',asset_id='yesm',timestamp=NOW+10,best_bid='.7',best_ask='.2'),received_ms=NOW+10)
        self.assertIsNone(self.engine.latest['m']['gap_pp'])
        self.assertEqual(self.engine.latest['m']['pm_source'],'clob_mid')
    def test_intraday_mismatch_uses_model(self):
        event=market(); event['endDate']='2030-01-16T04:00:00Z'
        feed(self.engine)
        self.engine.ingest('gamma','catalog','all',dict(markets=[event]),received_ms=NOW+10)
        self.assertEqual(self.engine.latest['m']['basis_method'],'IV')
        self.assertIn('EXPIRY_IV_PROXY',self.engine.latest['m']['quality_flags'])
    def test_manual_mapping_survives_refresh(self):
        feed(self.engine); event=dict(self.engine.events['m'], direction='down', mapping_review='PROXY')
        self.engine.ingest('operator','mapping','m',event,received_ms=NOW+10)
        self.engine.ingest('gamma','catalog','all',dict(markets=[market()]),received_ms=NOW+20)
        self.assertEqual(self.engine.events['m']['direction'],'down')
    def test_raw_and_derived_are_immutable(self):
        feed(self.engine)
        for table in ('raw','observations','episodes'):
            with self.assertRaises(sqlite3.IntegrityError): self.store.db.execute(f'DELETE FROM {table}')
            with self.assertRaises(sqlite3.IntegrityError): self.store.db.execute(f'UPDATE {table} SET id=id')
    def test_raw_hash_integrity(self):
        feed(self.engine)
        records=list(self.store.raw()); self.assertEqual(len(records),5); self.assertEqual(records[0]['kind'],'session')
    def test_replay_matches_all_historical_states(self):
        feed(self.engine)
        for i in range(1,40):
            self.engine.ingest('coinbase','spot','BTC',dict(price=100+i*.01,timestamp=NOW+i*1000),received_ms=NOW+i*1000)
        result=verify_replay(self.store)
        self.assertTrue(result['ok'],result); self.assertGreater(result['checked'],40)
    def test_restart_restores_episodes_without_rewriting_history(self):
        feed(self.engine); count=self.store.stats(); restarted=Engine(self.store); restore(restarted)
        self.assertEqual(self.store.stats(),count); self.assertEqual(encode(restarted.latest),encode(self.engine.latest)); self.assertEqual(restarted.episodes.active,self.engine.episodes.active)
    def test_no_future_features_and_asof_replay(self):
        feed(self.engine); prefix=copy.deepcopy(self.engine.latest['m'])
        self.engine.ingest('coinbase','spot','BTC',dict(price=900,timestamp=NOW+1000),received_ms=NOW+1000)
        historical=self.store.history('m',end=NOW+4)[-1]
        historical.pop('observation_id'); self.assertEqual(encode(prefix),encode(historical))
        result=verify_replay(self.store,until=NOW+4); self.assertTrue(result['ok'],result)
    def test_dynamics_and_travel(self):
        d=Dynamics(Config())
        for t in range(32): row=obs(t,p=.5+t*.001,q=.4+t*.0001); d.update(row)
        self.assertAlmostEqual(row['features']['pm_travel'],10/11)
        self.assertAlmostEqual(row['features']['velocity_pp_s'],.09)
        self.assertEqual(row['features']['trend'],'OPENING FAST')
        self.assertLessEqual(row['features']['changes']['30']['reference_raw_id'],row['raw_id'])
    def test_lifecycle_thresholds_and_reversal(self):
        e=Episodes(Config())
        a=list(e.update(obs(0,p=.45,q=.4))); self.assertEqual(a[0]['state'],'NEW')
        b=list(e.update(obs(1,p=.50,q=.4))); self.assertEqual(b[0]['state'],'PEAK')
        c=list(e.update(obs(2,p=.399,q=.4))); self.assertEqual(c[0]['close_reason'],'REVERSAL')
        self.assertEqual(len(e.active),0)
    def test_lifecycle_outage_does_not_claim_convergence(self):
        e=Episodes(Config()); list(e.update(obs(0)))
        row=obs(1); row.update(source_state='STALE',gap_pp=None)
        self.assertEqual(list(e.update(row))[0]['close_reason'],'DATA_GAP')
    def test_curve_semantics_and_monotonicity(self):
        rows=[obs(0,p=p,q=q,key=str(i)) for i,(p,q) in enumerate(((.6,.6),(.7,.5),(.2,.4)))]
        for i,r in enumerate(rows): r['strike_or_threshold']=100+i*10
        curve=curves(rows)[0]; self.assertEqual(len(curve['monotonicity_violations']),1)
        rows[1]['event_type']='touch'; self.assertEqual(len(curves(rows)[0]['nodes']),2)
    def test_analyzers_are_versioned_and_sequential_is_disabled(self):
        feed(self.engine); self.engine.ingest('basis','timer','clock',dict(analyze=True),received_ms=NOW+100)
        rows=self.store.latest('analyzers','scope,algo_name'); self.assertEqual(len(rows),7)
        self.assertTrue(all(x['algo_version'] and x['input_window']['raw_id_max']<=x['raw_id'] for x in rows))
        self.assertEqual(next(x for x in rows if x['algo_name']=='sequential_martingale')['status'],'disabled')

    def test_invalid_transform_time_cannot_become_probability(self):
        for value in (float('nan'),float('inf'),None,-1,0):
            self.assertIsNone(touch_probability(100,120,.4,value,'up'))
            self.assertIsNone(terminal_probability(100,120,.4,value,'up'))

    def test_source_timestamp_stale_even_when_packet_arrives_now(self):
        self.engine.ingest('gamma','catalog','all',dict(markets=[market()]),received_ms=NOW)
        self.engine.ingest('coinbase','spot','BTC',dict(price=100,timestamp=NOW),received_ms=NOW)
        self.engine.ingest('deribit','options','BTC',options(),source_ms=NOW-60000,received_ms=NOW+10)
        self.assertEqual(self.engine.latest['m']['source_state'],'STALE')

    def test_malformed_packet_is_replayable_quarantine(self):
        feed(self.engine)
        with self.assertLogs(level='ERROR'):
            self.engine.ingest('clob','pm','tokens',[None],received_ms=NOW+10)
        self.assertEqual(len(self.engine.errors),1)
        restored=Engine(self.store); restore(restored)
        self.assertEqual(list(restored.errors),list(self.engine.errors))
        self.assertTrue(verify_replay(self.store)['ok'])

    def test_native_jump_timing_preserves_subsecond_order(self):
        d=Dynamics(Config())
        d.update(obs(0,p=.4,q=.4))
        d.update(obs(.1,p=.41,q=.4))
        d.update(obs(.7,p=.41,q=.41))
        events=list(d.salient[('m','mapping')])
        self.assertEqual([e['timestamp']-NOW for e in events],[100,700])
        result=asynchronous_sync(events,NOW+2000,[1])[0]
        self.assertEqual(result['pm_then_opt']['probability'],1)
        self.assertEqual(result['opt_then_pm']['probability'],0)
        broken=obs(1); broken['source_state']='STALE'; d.update(broken)
        self.assertEqual(list(d.salient[('m','mapping')]),[])

    def test_rolling_memory_is_bounded_per_second(self):
        d=Dynamics(Config())
        for i in range(5000): d.update(obs(i/1000,p=.4+i*.000001))
        history=d.history[('m','mapping')]
        self.assertEqual(len(history),5)
        self.assertNotIn('model_inputs',history[-1])

    def test_checkpoint_restart_and_incremental_resume(self):
        feed(self.engine)
        self.engine.save_checkpoint()
        self.engine.ingest('coinbase','spot','BTC',dict(price=101,timestamp=NOW+10),received_ms=NOW+10)
        before=self.store.stats(); restored=Engine(self.store); restore(restored)
        self.assertEqual(encode(restored.latest),encode(self.engine.latest))
        self.assertEqual(restored.episodes.active,self.engine.episodes.active)
        self.assertEqual(before,self.store.stats())
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute('DELETE FROM checkpoints')

    def test_disk_reopen_retains_tape_and_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'tape.sqlite3'
            store=Store(path); engine=Engine(store); feed(engine); engine.save_checkpoint()
            latest=encode(engine.latest); count=store.stats(); store.close()
            store=Store(path); restored=Engine(store); restore(restored)
            self.assertEqual(encode(restored.latest),latest)
            self.assertEqual(store.stats(),count)
            self.assertEqual(store.db.execute('PRAGMA quick_check').fetchone()[0],'ok')
            store.close()

    def test_touch_path_requires_complete_history_and_flags_hit(self):
        feed(self.engine)
        start=timestamp('2030-01-01T00:00:00Z')
        mapping=dict(self.engine.events['m'],event_type='touch',window_start=start,strike_or_threshold=120)
        self.engine.ingest('operator','mapping','m',mapping,received_ms=NOW+10)
        self.assertEqual(self.engine.latest['m']['source_state'],'HISTORY')
        candles=[[t//1000,80,110,100,100,1] for t in range(start,NOW,86400000)]
        self.engine.ingest('coinbase','history','BTC',dict(start_ms=start,candles=candles),received_ms=NOW+11)
        self.assertIsNotNone(self.engine.latest['m']['opt_yes'])
        candles[0][2]=121
        self.engine.ingest('coinbase','history','BTC',dict(start_ms=start,candles=candles),received_ms=NOW+12)
        self.assertEqual(self.engine.latest['m']['source_state'],'VERIFY_HIT')
        self.assertIsNone(self.engine.latest['m']['gap_pp'])

    def test_config_and_mapping_validate_external_values(self):
        for params in ({'pm_max_age_ms':-1},{'event_jump_pp':float('nan')},{'analysis_window':2},{'analysis_scales':(0,)}):
            with self.assertRaises(ValueError): Config(**params)
        mapping=infer_event(market())
        normalized=validate_mapping(dict(mapping,strike_or_threshold='100',expiry='2030-01-16T08:00:00Z'))
        self.assertEqual(normalized['strike_or_threshold'],100)
        with self.assertRaises(ValueError): validate_mapping(dict(mapping,window_start='2030-01-15T12:00:00'))

    def test_tape_pagination_does_not_skip_equal_timestamps(self):
        feed(self.engine)
        for i in range(20): self.engine.ingest('basis','timer','clock',{},received_ms=NOW+10)
        all_ids=[r['observation_id'] for r in self.store.history('m')]
        seen=[];before=2**63-1
        while True:
            page=self.store.history('m',limit=7,tail=True,before_id=before)
            if not page: break
            seen=[r['observation_id'] for r in page]+seen
            before=page[0]['observation_id']
        self.assertEqual(seen,all_ids)

    def test_checkpoint_compatibility_requires_identical_reducer_sources(self):
        import hashlib, shutil
        from basislab.replay import reducer_hash
        with tempfile.TemporaryDirectory() as directory:
            archive=Path(directory)/'archive';archive.mkdir()
            source=Path(__file__).resolve().parents[1]/'basislab'
            for path in source.glob('*.py'): shutil.copyfile(path,archive/path.name)
            (archive/'cli.py').write_text('# Interface-only changes do not alter reducer state\n')
            self.assertEqual(reducer_hash(source),reducer_hash(archive))
            (archive/'pricing.py').write_text('# A pricing change must rebuild state\n')
            self.assertNotEqual(reducer_hash(source),reducer_hash(archive))


class SyntheticAlgoTests(unittest.TestCase):
    def test_wasserstein_detects_distribution_shift(self):
        rng=np.random.default_rng(12); a=rng.normal(size=(128,4)); b=a.copy(); b[:,2]+=3
        quiet,_=sliced_wasserstein(a,a,['a','b','c','d']); shift,contributors=sliced_wasserstein(a,b,['a','b','c','d'])
        self.assertEqual(quiet,0); self.assertGreater(shift,1); self.assertTrue(any(abs(x['weights'].get('c',0))>.8 for x in contributors))
    def test_covariance_detects_relationship_change(self):
        rng=np.random.default_rng(4); x=rng.normal(size=300)
        a=np.column_stack((x,x+rng.normal(size=300)*.1)); b=np.column_stack((x,-x+rng.normal(size=300)*.1))
        same,_=covariance_geometry(a,a,['x','y']); changed,pairs=covariance_geometry(a,b,['x','y'])
        self.assertLess(same,1e-8); self.assertGreater(changed,3); self.assertLess(pairs[0]['correlation_change'],-1.5)
    def test_ordinal_mmd_detects_motif_change(self):
        same,_=ordinal_mmd(np.arange(100),np.arange(100)); changed,m=ordinal_mmd(np.arange(100),-np.arange(100))
        self.assertEqual(same,0); self.assertGreater(changed,1); self.assertTrue(m)
    def test_known_pm_leads_and_reverse(self):
        rng=np.random.default_rng(9); returns=rng.normal(size=500)*.001
        pm=.5+np.cumsum(returns); opt=np.r_[np.repeat(pm[0],5),pm[:-5]]; spot=100*np.exp(np.cumsum(rng.normal(size=500)*.0001))
        row=lead_lag(pm,opt,spot,1,[5])[0]; self.assertEqual(row['direction'],'PM_LEADS_OPT',row)
        reverse=lead_lag(opt,pm,spot,1,[5])[0]; self.assertEqual(reverse['direction'],'OPT_LEADS_PM',reverse)
    def test_event_sync_excludes_censored_and_simultaneous_events(self):
        times=np.arange(8)*1000; p=np.array([0,1,1,1,2,2,2,3]); q=np.array([0,0,1,1,2,2,2,2])
        result=event_sync(p,q,np.ones(8)*100,times,[2],threshold_pp=10)[0]
        self.assertEqual(result['pm_then_opt']['triggers'],2); self.assertEqual(result['pm_then_opt']['count'],1)
    def test_topology_detects_coherent_island(self):
        rng=np.random.default_rng(2); a=rng.normal(size=(400,4)); x=rng.normal(size=400)
        b=np.column_stack([x+rng.normal(size=400)*.03 for _ in range(3)]+[rng.normal(size=400)])
        score,diag=topology(a,b,['3100','3200','3300','central'])
        self.assertGreater(score,.3); self.assertTrue(any(['3100','3200','3300'] in x['new_clusters'] for x in diag['filtration']))
    def test_full_graph_has_no_nonfinite_outputs_or_fake_evidence(self):
        rows=[obs(i*15,p=.4+.02*math.sin(i/5),q=.4+.015*math.sin((i-3)/5)) for i in range(128)]
        outputs=run_algos('test',rows,Config(),rows[-1]['timestamp_wall'])
        self.assertEqual(len(outputs),7); encode(outputs)
        self.assertIsNone(outputs[-1]['raw_score']); self.assertEqual(outputs[-1]['status'],'disabled')

if __name__=='__main__': unittest.main()
