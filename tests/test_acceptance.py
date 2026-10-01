import fcntl
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from basislab.acceptance import chronology, gate_status, source_accounting
from basislab.engine import Engine
from basislab.replay import restore
from basislab.sample_replay import ReadTape, choose_regions, sampled_replay
from basislab.service import serve
from basislab.store import Store, encode
from test_research import feed, NOW


class AcceptanceTests(unittest.TestCase):
    def test_sampler_is_stable_causal_and_missing_versions_are_not_matches(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'tape.sqlite3';store=Store(path);engine=Engine(store);feed(engine)
            boundary=engine.last_raw_id
            engine.ingest('basis','timer','clock',{},received_ms=NOW+10);engine.save_checkpoint()
            cps=[dict(r) for r in store.db.execute('SELECT id,raw_id,code_hash FROM checkpoints')]
            regions=choose_regions(boundary,[],cps)
            self.assertEqual(regions,choose_regions(boundary,[],cps))
            self.assertEqual(regions,choose_regions(boundary,[],[]))
            self.assertEqual(regions,choose_regions(boundary,[],cps,
                [dict(raw_id=boundary+1,reason='future-transition')],[dict(raw_id=boundary+1)]))
            self.assertTrue(all(r['end']<=boundary for r in regions))
            split=choose_regions(100,[dict(raw_id=1,data=dict(code_hash='a')),dict(raw_id=50,data=dict(code_hash='b'))],[],
                                 [dict(raw_id=52,reason='source-ERROR-OK-transition')])
            self.assertTrue(all(not (r['start']<50<=r['end']) for r in split))
            tape=ReadTape(path);tape.boundary=boundary
            chosen=[dict(start=1,end=boundary,anchor=1,reasons=['earliest'])]
            actual=sampled_replay(tape,chosen,cps)
            self.assertEqual(actual['raw_records_replayed'],boundary)
            self.assertEqual(actual['mismatch_count'],0)
            self.assertNotIn('checkpoint_id',actual['regions'][0])
            with patch('basislab.sample_replay.archived_engine',return_value=None):
                missing=sampled_replay(tape,chosen,cps)
            self.assertEqual(missing['unavailable_version_count'],1)
            self.assertEqual(missing['regions_verified'],0)
            self.assertEqual(missing['observations_compared'],0)
            tape.close();store.close()

    def test_acceptance_gates_require_duration_and_distinguish_all_states(self):
        replay=dict(regions_verified=5,regions_requested=5,mismatch_count=0,unavailable_version_count=0)
        integrity=dict(result='ok',fresh=True)
        self.assertEqual(gate_status(24,86399,[],replay,integrity,True),'NOT YET ELAPSED')
        self.assertEqual(gate_status(24,86400,[],replay,integrity,True),'PASS')
        self.assertEqual(gate_status(24,86400,[],replay,integrity,False),'PARTIAL')
        self.assertEqual(gate_status(24,86400,[dict(seconds=61)],replay,integrity,True),'FAIL')
        self.assertEqual(gate_status(24,86400,[],dict(replay,mismatch_count=1),integrity,True),'FAIL')

    def test_chronology_keeps_unknown_gaps_and_excludes_unrecorded_health(self):
        def timer(raw,ms,session):return dict(raw_id=raw,timestamp_ms=ms,session=session)
        sessions=[dict(raw_id=1,data=dict(config={}))]
        timers=[timer(1,0,'a'),timer(2,5000,'a'),timer(3,15000,'b'),timer(4,20000,'b'),timer(5,100000,'b'),timer(6,105000,'b')]
        evidence=[dict(session='a',timestamp_ms=6000,reason='CONTROLLED_RESTART')]
        c=chronology(timers,sessions,evidence)
        self.assertEqual(c['recorded_seconds'],15)
        self.assertEqual([g['classification'] for g in c['gaps']],['CONTROLLED_RESTART','UNKNOWN_STOP'])
        health=[dict(raw_id=1,timestamp_ms=0,session='a',subject='yahoo',data=dict(state='ERROR')),
                dict(raw_id=3,timestamp_ms=15000,session='b',subject='yahoo',data=dict(state='OK')),
                dict(raw_id=5,timestamp_ms=100000,session='b',subject='yahoo',data=dict(state='ERROR'))]
        summary,_=source_accounting(health,c['segments'],sessions)
        self.assertEqual(summary['yahoo']['seconds']['ERROR'],10)
        self.assertEqual(summary['yahoo']['longest_unavailable_seconds'],5)

    def test_invalid_newest_checkpoint_falls_back_without_rewriting_tape(self):
        store=Store(':memory:');engine=Engine(store);feed(engine);engine.save_checkpoint()
        engine.ingest('basis','timer','clock',{},received_ms=NOW+50)
        expected=encode(engine.latest)
        store.db.execute('INSERT INTO checkpoints(raw_id,code_hash,sha256,data) VALUES(?,?,?,?)',
                         (engine.last_raw_id,engine.code_hash,'bad',b'not zlib'))
        restored=Engine(store);restore(restored)
        self.assertEqual(encode(restored.latest),expected)
        self.assertEqual(store.db.execute('SELECT COUNT(*) FROM checkpoints').fetchone()[0],2)
        self.assertEqual(len(store.checkpoint_errors),1)
        store.close()

    def test_duplicate_owner_fails_before_store_or_sources_are_started(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'tape.sqlite3';store=Store(path);Engine(store).ingest('basis','timer','clock',{})
            store.close();before=path.read_bytes()
            with open(str(path)+'.collector.lock','a') as owner:
                fcntl.flock(owner,fcntl.LOCK_EX|fcntl.LOCK_NB)
                with patch('basislab.service.Store') as opening,patch('basislab.service.Collector') as sources:
                    with self.assertRaisesRegex(RuntimeError,'already owns this tape'):
                        serve(path,collect=False)
                    opening.assert_not_called();sources.assert_not_called()
                self.assertEqual(path.read_bytes(),before)


if __name__=='__main__':unittest.main()
