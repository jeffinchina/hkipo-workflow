import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from hkipo.broker_source import ingest_snapshot, linked_codes, validate_snapshot

FIXTURE=Path(__file__).parent/'fixtures'/'futu-public-discovery-real.json'

class BrokerSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.db=Path(self.temp.name)/'db.sqlite';self.archive=Path(self.temp.name)/'snapshots'
        self.snapshot=json.loads(FIXTURE.read_text())
    def ingest(self,snapshot=None):
        return ingest_snapshot(self.snapshot if snapshot is None else snapshot,self.db,self.archive)
    def test_actual_identity_and_original_images(self):
        posts=validate_snapshot(self.snapshot)
        self.assertEqual(linked_codes(posts[0]['body']),['09941'])
        self.assertEqual(linked_codes(posts[1]['body']),['06935'])
        self.assertEqual(linked_codes(posts[2]['body']),['02065','02681'])
        self.assertTrue(all('format/webp' not in u for p in posts for u in p['images']))
    def test_silent_baseline_then_idempotent_new_and_revision(self):
        first=copy.deepcopy(self.snapshot);first['posts']=first['posts'][1:]
        self.assertEqual(self.ingest(first)['status'],'baseline')
        result=self.ingest();self.assertEqual(len(result['candidates']),1)
        candidate=result['candidates'][0]
        self.assertEqual(candidate['stock_codes'],['09941']);self.assertFalse(candidate['notification_eligible'])
        self.assertEqual(self.ingest()['candidates'],[])
        self.snapshot['posts'][0]['body']+=' 更正客户获配情况。'
        revised=self.ingest()['candidates'];self.assertEqual(len(revised),1);self.assertEqual(revised[0]['change_type'],'revised')
        self.assertEqual(self.ingest()['candidates'],[])
    def test_multi_issue_requires_mapping(self):
        first=copy.deepcopy(self.snapshot);first['posts']=first['posts'][:2];self.ingest(first)
        candidate=self.ingest()['candidates'][0]
        self.assertEqual(candidate['identity_status'],'needs_identity_mapping')
        self.assertEqual(candidate['stock_codes'],['02065','02681'])
    def test_failure_empty_and_wrong_identity_preserve_last_success(self):
        success=self.ingest()['snapshot_hash']
        for mutate in [lambda s:s.update(status='failed'),lambda s:s.update(posts=[]),lambda s:s['posts'][0].update(author_id='other'),lambda s:s['posts'][0].update(url='https://q.futunn.com/feed/999')]:
            bad=copy.deepcopy(self.snapshot);mutate(bad)
            self.assertEqual(self.ingest(bad)['status'],'rejected')
            with sqlite3.connect(self.db) as db:self.assertEqual(db.execute('SELECT last_snapshot FROM broker_source_state').fetchone()[0],success)
        self.assertEqual(len(list(self.archive.glob('*.json'))),5)
    def test_image_order_and_arrival_time_are_not_material(self):
        self.ingest();self.snapshot['finished_at']='later'
        self.snapshot['posts'][0]['images'].reverse()
        self.assertEqual(self.ingest()['candidates'],[])

    def test_return_to_previous_content_is_durable_revision(self):
        self.ingest()
        original=copy.deepcopy(self.snapshot)
        self.snapshot['posts'][0]['body']+=' 实质更正。'
        first=self.ingest()['candidates'][0]
        reverted=self.ingest(original)['candidates'][0]
        self.assertGreater(reverted['revision_id'],first['revision_id'])
        self.assertEqual(reverted['change_type'],'revised')
        with sqlite3.connect(self.db) as db:
            rows=db.execute('SELECT revision_id,status FROM broker_review_queue ORDER BY revision_id').fetchall()
        self.assertEqual(rows,[(first['revision_id'],'pending'),(reverted['revision_id'],'pending')])
        self.assertEqual(self.ingest(original)['candidates'],[])
    def test_concurrent_first_success_serializes_baseline(self):
        import concurrent.futures
        import threading
        barrier=threading.Barrier(2)
        first=copy.deepcopy(self.snapshot);first['posts']=first['posts'][1:]
        def run(s):
            barrier.wait()
            return ingest_snapshot(s,self.db,self.archive)
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results=list(pool.map(run,[first,self.snapshot]))
        self.assertEqual(sum(r['status']=='baseline' for r in results),1)
        self.assertEqual(sum(r['status']=='success' for r in results),1)
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM broker_source_versions').fetchone()[0],3)
            count=db.execute('SELECT COUNT(*) FROM broker_review_queue').fetchone()[0]
        self.assertEqual(count,1 if results[0]['status']=='baseline' else 0)  # Smaller baseline must not swallow new 09941.

if __name__=='__main__':unittest.main()
