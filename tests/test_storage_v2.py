"""Six representation regressions; existing model/interaction tests stay intact."""
import copy
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from basislab.config import Config
from basislab.engine import Engine
from basislab.replay import restore
from basislab.store import Store, encode
from basislab.tape import create_tape, SegmentedTape, publish_tape, open_store
from test_research import feed, market, options, NOW


class StorageV2Tests(unittest.TestCase):
    def test_lossless_source_anchors_and_deltas(self):
        with tempfile.TemporaryDirectory() as directory:
            store=create_tape(Path(directory)/'alias',Path(directory)/'tape')
            expected=[]
            for i in range(40):
                body=dict(options(),timestamp=str(NOW+i),unused_provider_field={'zero':-0.0,'nested':[True,None,1,1.0]})
                body['result'][0]['bid_price']+=i*.00001
                with store.transaction():
                    record=store.append_raw('deribit','options','BTC',body,'session',NOW+i,None,i)
                expected.append(encode(record['payload']))
            self.assertGreater(store.db.execute('SELECT COUNT(*) FROM records WHERE encoding!=0').fetchone()[0],0)
            self.assertLessEqual(store.db.execute('SELECT MAX(chain_depth) FROM records').fetchone()[0],15)
            self.assertEqual([encode(r['payload']) for r in store.raw()],expected)
            manifest=store.manifest_path;store.close()
            reader=SegmentedTape(manifest)
            self.assertEqual([encode(r['payload']) for r in reader.raw()],expected);reader.close()

    def test_frames_semantics_causality_and_bounded_amplification(self):
        with tempfile.TemporaryDirectory() as directory:
            legacy=Store(Path(directory)/'v1.sqlite3');compact=create_tape(Path(directory)/'alias',Path(directory)/'tape')
            config=Config(max_events=30,max_per_asset=30)
            a,b=Engine(legacy,config),Engine(compact,config)
            records=[('basis','session','s',dict(config=asdict(config))),
                ('gamma','catalog','all',dict(markets=[market(str(i),100+i) for i in range(20)])),
                ('deribit','options','BTC',options())]
            records += [('coinbase','spot','BTC',dict(price=100+i*.0001,timestamp=NOW+i+10)) for i in range(1000)]
            for i,(source,kind,subject,body) in enumerate(records):
                for e in (a,b):e.ingest(source,kind,subject,body,received_ms=NOW+i,monotonic_ns=i)
            self.assertEqual(encode(a.latest),encode(b.latest))
            self.assertLess(compact.stats()['observations'],legacy.stats()['observations']/20)
            self.assertEqual(compact.db.execute('SELECT COUNT(*) FROM event_versions').fetchone()[0],20)
            self.assertLess(compact.logical_bytes(),legacy.logical_bytes()/5)
            for row in compact.history(limit=10000):
                self.assertTrue(all(ref<=row['raw_id'] for ref in row['input_refs'].values()))
                original=legacy.db.execute('SELECT data FROM observations WHERE raw_id=? AND event_id=?',(row['raw_id'],row['event_id'])).fetchone()[0]
                from basislab.store import decode
                self.assertEqual(encode({k:v for k,v in row.items() if k!='observation_id'}),encode(decode(original)))
            legacy.close();compact.close()

    def test_full_research_replay_equivalence_and_checkpoint_chunks(self):
        with tempfile.TemporaryDirectory() as directory:
            store=create_tape(Path(directory)/'alias',Path(directory)/'tape');engine=Engine(store);feed(engine)
            for i in range(80):
                engine.ingest('coinbase','spot','BTC',dict(price=100+i*.1,timestamp=NOW+1000+i*1000),received_ms=NOW+1000+i*1000,monotonic_ns=i)
                if i%15==0:engine.ingest('basis','timer','clock',dict(analyze=True),received_ms=NOW+1000+i*1000,monotonic_ns=i)
            engine.save_checkpoint();first=store.checkpoint()['state'];blobs=store.db.execute('SELECT COUNT(*) FROM blobs').fetchone()[0]
            engine.save_checkpoint()
            self.assertEqual(store.db.execute('SELECT COUNT(*) FROM blobs').fetchone()[0],blobs)
            engine.ingest('coinbase','spot','BTC',dict(price=108.2,timestamp=NOW+82000),received_ms=NOW+82000)
            self.assertEqual(encode(store.checkpoint()['state']),encode(first))
            replay=Engine(store,persist=False)
            for record in store.raw():replay.apply(record)
            self.assertEqual(encode(replay.latest),encode(engine.latest))
            self.assertEqual(encode(replay.episodes.active),encode(engine.episodes.active))
            self.assertEqual(encode([[list(k),list(v)] for k,v in replay.dynamics.history.items()]),encode([[list(k),list(v)] for k,v in engine.dynamics.history.items()]))
            restored=Engine(store);restore(restored);self.assertEqual(encode(restored.latest),encode(engine.latest));store.close()

    def test_rotation_order_and_closed_integrity_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            store=create_tape(Path(directory)/'alias',Path(directory)/'tape')
            for i in range(6):
                with store.transaction():store.append_raw('coinbase','spot','BTC',dict(price=100+i),'s',NOW+i,None,i)
                if i in (1,3):store.rotate('fixture')
            self.assertEqual([r['id'] for r in store.raw()],list(range(1,7)))
            first=store.integrity();second=store.integrity()
            self.assertEqual(first['segments'][:2],second['segments'][:2])
            self.assertTrue(all(r['cached'] for r in second['segments'][:2]))
            self.assertEqual([x['close_reason'] for x in store.manifest['segments'][:2]],['fixture','fixture'])
            closed=Path(directory)/'tape'/'segment-000001.sqlite3'
            connection=sqlite3.connect(closed)
            try:
                with self.assertRaises(sqlite3.OperationalError):connection.execute('DELETE FROM records')
            finally:connection.close()
            reader=sqlite3.connect(store.active.path,isolation_level=None)
            reader.execute('BEGIN');reader.execute('SELECT COUNT(*) FROM records').fetchone()
            with store.transaction():store.append_raw('coinbase','spot','BTC',dict(price=107),'s',NOW+7,None,7)
            self.assertFalse(store.rotate('reader-busy'))
            reader.close();self.assertTrue(store.rotate('reader-released'))
            manifest=store.manifest_path;store.close();reader=SegmentedTape(manifest)
            self.assertEqual([r['id'] for r in reader.raw()],list(range(1,8)));reader.close()

    def test_legacy_and_new_segments_share_global_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            alias=Path(directory)/'v1.sqlite3';legacy=Store(alias);engine=Engine(legacy);feed(engine);engine.save_checkpoint();legacy.close()
            before=alias.read_bytes();store=create_tape(alias,Path(directory)/'tape');new=Engine(store);restore(new);store.resume(new)
            new.ingest('coinbase','spot','BTC',dict(price=101,timestamp=NOW+1000),received_ms=NOW+1000,monotonic_ns=123)
            new.save_checkpoint();store.rotate();new.ingest('basis','timer','clock',dict(analyze=True),received_ms=NOW+2000,monotonic_ns=124)
            self.assertEqual([r['id'] for r in store.raw()],list(range(1,8)))
            self.assertTrue(any(r['raw_id']<=5 for r in store.history(limit=100)))
            self.assertTrue(any(r['raw_id']>5 for r in store.history(limit=100)))
            replay=Engine(store,persist=False)
            for record in store.raw():replay.apply(record)
            self.assertEqual(encode(replay.latest),encode(new.latest))
            new.start_session()  # Archive the exact producer for acceptance sampling.
            publish_tape(store);store.close();view=open_store(alias,read_only=True)
            self.assertEqual(view.stats()['raw'],8)
            expected=[r for r in view.latest('episodes','gap_event_id',200) if r['event_id']=='m']
            self.assertEqual(view.latest('episodes','gap_event_id',200,'m'),expected)
            self.assertEqual(view.latest('episodes','gap_event_id',200,'unknown'),[])
            view.close()
            from basislab.acceptance import report
            acceptance=report(alias,url='http://127.0.0.1:1')
            self.assertEqual(acceptance['tape']['storage_version'],2)
            self.assertEqual(acceptance['sqlite_quick_check']['result'],'ok')
            self.assertEqual(acceptance['replay']['mismatch_count'],0)
            self.assertGreater(acceptance['replay']['observations_compared'],0)
            self.assertEqual(alias.read_bytes(),before)

    def test_failed_transaction_cannot_leave_dangling_dictionary_refs(self):
        with tempfile.TemporaryDirectory() as directory:
            store=create_tape(Path(directory)/'alias',Path(directory)/'tape')
            with self.assertRaises(RuntimeError):
                with store.transaction():
                    store.append_raw('coinbase','spot','BTC',dict(price=100),'s',NOW,None,1)
                    raise RuntimeError('injected rollback')
            with store.transaction():store.append_raw('coinbase','spot','BTC',dict(price=101),'s',NOW+1,None,2)
            self.assertEqual(store.raw_record(1)['payload'],dict(price=101));self.assertEqual(store.stats()['raw'],1);store.close()


if __name__=='__main__':unittest.main()
