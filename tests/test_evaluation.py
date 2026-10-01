"""Six focused regressions for immutable, causal prospective evaluation."""
import copy
from datetime import datetime, timezone
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from basislab import CALCULATION_VERSION, FEATURE_VERSION
from basislab.evaluation import EvaluationDB, PROTOCOL, freeze_boundary
from basislab.evaluation_features import horizon_outcome, semantic_version, session_regime, temporal_predictor
from basislab.evaluation_record import EvidenceReader, Recorder
from basislab.evaluation_report import analyzer_overlap, calibration, report
from basislab.evaluation_resolutions import official_result
from basislab.engine import Engine
from basislab.store import Store, decode, encode
from test_research import feed, obs, NOW


def row(second=0, p=.5, q=.4, key='m'):
    r=obs(second,p,q,key)
    r.update(observation_id=second+1,raw_id=second+1,pm_source='clob',
        pm_timestamp=NOW+second*1000,options_received_ms=NOW+second*1000,
        spot_timestamp=NOW+second*1000,calculation_version=CALCULATION_VERSION,
        parameter_hash='fixture',code_hash='fixture',input_refs=dict(pm=second+1,options=second+1),
        features=dict(feature_version=FEATURE_VERSION,velocity_pp_s=.02,pm_travel=.8,spot_return=.001),
        surface_features=dict(local_iv=.5))
    return r


def campaign(start=NOW,first_raw=1,first_frame=1,panel=None):
    return dict(evaluation_id='fixture',evaluation_version=1,mode='PROSPECTIVE',started_ms=start,
        evaluation_started_at=datetime.fromtimestamp(start/1000,timezone.utc).isoformat(),
        first_eligible_global_raw_id=first_raw,first_eligible_frame_id=first_frame,
        calculation_version=CALCULATION_VERSION,feature_version=FEATURE_VERSION,
        reducer_version='fixture',config_hash='fixture',protocol=PROTOCOL,initial_panel=panel or [])


class Tape:
    def __init__(self, rows):
        self.rows=rows
        self.db=sqlite3.connect(':memory:');self.db.row_factory=sqlite3.Row
        self.db.execute('CREATE TABLE analyzers(id INTEGER PRIMARY KEY,raw_id INTEGER,timestamp_ms INTEGER,algo_name TEXT,scope TEXT,data TEXT)')
        self.db.execute('CREATE TABLE observations(id INTEGER PRIMARY KEY,timestamp_ms INTEGER)')
        self.db.executemany('INSERT INTO observations VALUES(?,?)',[(r['observation_id'],r['timestamp_wall']) for r in rows])
    def history(self,event_id=None,start=0,end=2**63-1,limit=500,after_id=0,tail=False,before_id=2**63-1):
        rows=[copy.deepcopy(r) for r in self.rows if (event_id is None or r['event_id']==event_id)
            and start<=r['timestamp_wall']<=end and after_id<r['observation_id']<before_id]
        return rows[-limit:] if tail else rows[:limit]
    def close(self):self.db.close()


class EvaluationTests(unittest.TestCase):
    def test_boundary_idempotent_and_immutable_even_replace(self):
        with tempfile.TemporaryDirectory() as directory:
            tape=Path(directory)/'tape.sqlite3';store=Store(tape);engine=Engine(store);feed(engine)
            state=engine.snapshot()
            with patch('basislab.evaluation.time.time_ns',return_value=(NOW+5000)*1000000):
                first=freeze_boundary(tape,state,'original')
            engine.ingest('coinbase','spot','BTC',dict(price=101,timestamp=NOW+6000),received_ms=NOW+6000)
            self.assertEqual(freeze_boundary(tape,engine.snapshot(),'unrelated-ui-revision'),first)
            db=EvaluationDB(tape);changed=dict(first,first_eligible_global_raw_id=999)
            with self.assertRaises(ValueError):db.register(changed)
            with self.assertRaises(sqlite3.IntegrityError):db.db.execute('DELETE FROM campaigns')
            other=sqlite3.connect(db.path)
            with self.assertRaises(sqlite3.IntegrityError):
                other.execute('INSERT OR REPLACE INTO campaigns SELECT * FROM campaigns')
            other.close()
            historical=dict(campaign(),evaluation_id='historical-fixture',mode='HISTORICAL')
            db.register(historical)
            self.assertIn('EXPLORATORY',report(tape,'historical-fixture')['status'])
            db.close();store.close()

    def test_opening_freezes_features_and_excludes_same_raw_future_analyzers(self):
        with tempfile.TemporaryDirectory() as directory:
            tape=Path(directory)/'fixture.sqlite3';reader=Tape([row(5)])
            for i,raw,stamp,score in [(1,4,NOW+4000,7),(2,6,NOW+5000,999),(3,7,NOW+3000,888),(4,3,NOW+6000,777)]:
                data=dict(algo_name='sliced_wasserstein',timestamp=stamp,status='ok',raw_score=score)
                reader.db.execute('INSERT INTO analyzers VALUES(?,?,?,?,?,?)',(i,raw,stamp,'sliced_wasserstein','m',encode(data)))
            db=EvaluationDB(tape);db.register(campaign());recorder=Recorder(tape,reader=reader,db=db)
            with patch.object(recorder,'version_reason',return_value=None),patch('basislab.evaluation_record.time.time_ns',return_value=(NOW+5000)*1000000):recorder.poll()
            frozen=decode(db.db.execute("SELECT data FROM entries WHERE kind='EPISODE'").fetchone()[0])
            self.assertEqual(frozen['analyzers']['sliced_wasserstein']['score'],7)
            reader.rows[0]['features']['velocity_pp_s']=99
            self.assertEqual(decode(db.db.execute("SELECT data FROM entries WHERE kind='EPISODE'").fetchone()[0])['features']['velocity_pp_s'],.02)
            with self.assertRaises(sqlite3.IntegrityError):db.db.execute("UPDATE entries SET raw_id=999")
            recorder.close()

    def test_horizon_uses_past_quote_and_records_missing_not_future_or_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            tape=Path(directory)/'fixture.sqlite3';reader=Tape([row(),row(29,q=.42),row(31,q=.99)])
            db=EvaluationDB(tape);db.register(campaign());recorder=Recorder(tape,reader=reader,db=db)
            with patch.object(recorder,'version_reason',return_value=None),patch('basislab.evaluation_record.time.time_ns',return_value=(NOW+301000)*1000000):recorder.poll()
            outcome=decode(db.db.execute("SELECT data FROM outcomes WHERE name='30s' ORDER BY due_ms LIMIT 1").fetchone()[0])
            self.assertAlmostEqual(outcome['directional_opt_move_pp'],2)
            self.assertEqual(outcome['asof_frame_id'],30)
            missing=decode(db.db.execute("SELECT data FROM outcomes WHERE name='5m' ORDER BY due_ms LIMIT 1").fetchone()[0])
            self.assertEqual(missing['status'],'MISSING');self.assertEqual(missing['reason'],'FRAME_STALE')
            self.assertIsNone(missing['opt_h']);self.assertIsNotNone(missing['last_known_state'])
            entry=decode(db.db.execute("SELECT data FROM entries WHERE kind='EPISODE' ORDER BY opened_ms LIMIT 1").fetchone()[0])
            entry['timestamp_monotonic']=1000000000
            apparently_past=dict(row(29),timestamp_monotonic=32000000000)
            guarded=horizon_outcome(entry,apparently_past,{},NOW+30000,PROTOCOL)
            self.assertEqual(guarded['status'],'MISSING');self.assertIn('CLOCK_DISCONTINUITY',guarded['reason'])
            self.assertEqual(EvidenceReader(reader).first_frame_in_range(NOW+29000,NOW+30000),30)
            self.assertIsNone(EvidenceReader(reader).first_frame_in_range(NOW+32000,NOW+40000))
            recorder.close()

    def test_deferred_regime_next_open_holiday_dst_and_post_open_availability(self):
        milliseconds=lambda s:int(datetime.fromisoformat(s).timestamp()*1000)
        stock=dict(row(),asset='AMZN',expiry=milliseconds('2026-12-31T21:00:00+00:00'))
        holiday=milliseconds('2026-07-03T16:00:00+00:00');session=session_regime(stock,holiday)
        self.assertFalse(session['target_open']);self.assertEqual(session['next_open'],milliseconds('2026-07-06T13:30:00+00:00'))
        session=session_regime(stock,milliseconds('2026-11-06T21:01:00+00:00'))
        opening=milliseconds('2026-11-09T14:30:00+00:00');self.assertEqual(session['next_open'],opening)
        entry=dict(stock,id='deferred',event_version=semantic_version(stock),eligible=True,kind='DEFERRED_RESPONSE',session=session)
        later=dict(stock,timestamp_wall=opening+50000,opt_yes=.42,options_received_ms=opening+30000,spot_timestamp=opening+30000)
        self.assertEqual(horizon_outcome(entry,later,{},opening,PROTOCOL)['status'],'MISSING')
        self.assertEqual(horizon_outcome(entry,later,{},opening+60000,PROTOCOL)['status'],'OBSERVED')
        later['options_received_ms']=opening-1
        self.assertEqual(horizon_outcome(entry,later,{},opening+60000,PROTOCOL)['reason'],'NO_POST_OPEN_TARGET_PROXY')

    def test_calibration_unique_propositions_and_official_not_path_hits(self):
        forecasts=[];resolutions=[]
        for i in range(4):
            f=dict(row(key=str(i)),timestamp_ms=NOW,event_version='v')
            forecasts.extend([f,dict(f,timestamp_ms=NOW+60000,pm_yes=.99)])
            resolutions.append(dict(event_id=str(i),event_version='v',kind='OFFICIAL_RESOLUTION',yes=i%2,
                available_ms=NOW+3600000,review='OFFICIAL_BINARY_API'))
        resolutions.append(dict(event_id='extra',kind='INDEPENDENT_PATH_HIT',yes=None))
        data=calibration(forecasts,resolutions,PROTOCOL)
        self.assertEqual(data['unique_resolved_events'],4)
        self.assertEqual(data['curves']['pm_yes'][0]['predicted_mean'],.5)
        self.assertIsNone(data['curves']['pm_yes'][0]['uncertainty']['interval_95'])
        resolutions[0]['resolution_timestamp']=NOW-1
        late=calibration(forecasts,resolutions,PROTOCOL)
        self.assertEqual(late['unique_resolved_events'],3)
        self.assertEqual(late['exclusions']['RESOLVED_BEFORE_FORECAST'],1)
        market=dict(conditionId='c',outcomes='["Yes","No"]')
        response=dict(data=[dict(condition_id='c',status='resolved',price='1000000000000000000',last_update_timestamp='1759428779')])
        self.assertEqual(official_result(market,response)['yes'],1)
        response['data'][0]['price']='69'
        self.assertEqual(official_result(market,response)['kind'],'AMBIGUOUS')

    def test_overlap_deterministic_and_never_counts_votes(self):
        entries=[dict(id=str(i),gap_pp=5,analyzers={name:dict(score=i,flagged=i%2==0,output=dict(timestamp=NOW+i))
            for name in ('sliced_wasserstein','covariance_manifold')}) for i in range(25)]
        a=analyzer_overlap(entries,PROTOCOL);b=analyzer_overlap(list(reversed(entries)),PROTOCOL)
        self.assertEqual(encode(a),encode(b))
        pair=a['matrix']['sliced_wasserstein']['covariance_manifold']
        self.assertAlmostEqual(pair['score_correlation'],1)
        self.assertEqual(pair['same_episode_flagged'],13)
        self.assertIn('NOT_DEFINED',pair['same_predicted_price_direction'])
        self.assertEqual(temporal_predictor('lead_lag_multiscale',dict(diagnostics=dict(scales=[
            dict(direction='PM_LEADS_OPT',strength=.4),dict(direction='OPT_LEADS_PM',strength=-.2),
            dict(direction='INSUFFICIENT',strength=99)]))),.1)


if __name__=='__main__':unittest.main()
