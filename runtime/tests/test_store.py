import concurrent.futures
from pathlib import Path
import tempfile
import unittest
from hkipo.models import DocumentEvent
from hkipo.store import StateStore

def event(n=1,kind='offer',source='fixture'):
    return DocumentEvent('',source,str(n),'12','测试公司',kind,'招股章程',f'https://www1.hkexnews.hk/listedco/{n}.pdf','2026-11-19T07:00:00+08:00','2026-11-19T00:00:00+00:00')

class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'state.db';self.store=StateStore(self.path)
    def tearDown(self):self.temp.cleanup()
    def test_same_day_dedupe_and_restart(self):
        self.assertEqual(len(self.store.ingest([event()])),1)
        self.assertEqual(StateStore(self.path).ingest([event()]),[])
        self.assertEqual(len(self.store.jobs()),1)
        self.assertEqual(event().stock_code,'00012')
    def test_first_baseline_then_new(self):
        _,baseline=self.store.commit_scan('a',[event()], '2026-11-19T00:00:00+00:00')
        self.assertTrue(baseline);self.assertEqual(self.store.jobs(),[])
        _,baseline=self.store.commit_scan('a',[event(),event(2)],'2026-11-19T00:01:00+00:00')
        self.assertFalse(baseline);self.assertEqual(len(self.store.jobs()),1)
    def test_failure_does_not_advance_watermark(self):
        self.store.commit_scan('a',[event()],'2026-11-19T00:00:00+00:00')
        self.store.record_failure('a','bounded recovery exhausted')
        self.assertEqual(self.store.source_state('a')['cutoff'],'2026-11-19T00:00:00+00:00')
        self.assertEqual(len(self.store.alerts()),1)
        self.store.commit_scan('a',[],'2026-11-19T00:02:00+00:00');self.assertEqual(self.store.alerts(),[])
    def test_watermark_never_regresses(self):
        self.store.commit_scan('a',[],'2026-11-19T00:02:00+00:00')
        self.store.commit_scan('a',[],'2026-11-19T00:01:00+00:00')
        self.assertEqual(self.store.source_state('a')['cutoff'],'2026-11-19T00:02:00+00:00')
    def test_concurrent_ingest_exactly_one_job(self):
        def worker(_):return len(StateStore(self.path).ingest([event()]))
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            self.assertEqual(sum(ex.map(worker,range(20))),1)
        self.assertEqual(len(self.store.jobs()),1)
    def test_unknown_delivery_survives_restart_and_blocks_resend(self):
        self.store.ingest([event()])
        self.assertTrue(self.store.begin_delivery('email:key',event().event_id,'email'))
        restored=StateStore(self.path)
        self.assertFalse(restored.begin_delivery('email:key',event().event_id,'email'))
        self.assertEqual(restored.delivery('email:key')['status'],'unknown')
        restored.finish_delivery('email:key','confirmed',{'id':'receipt'})
        self.assertEqual(restored.delivery('email:key')['status'],'confirmed')
    def test_version_and_document_identity(self):
        self.assertNotEqual(event().event_id,event(2).event_id)
        self.assertNotEqual(event().event_id,event(kind='exception').event_id)
        self.assertEqual(event().event_id,event(source='other-official-feed').event_id)
    def test_recovered_aggregate_candidate_promoted_by_established_title(self):
        self.store.commit_scan('title',[],'2026-11-19T00:00:00+00:00')
        self.store.record_failure('aggregate','failure')
        self.store.commit_scan('aggregate',[event(2)],'2026-11-19T00:02:00+00:00')
        self.assertEqual(self.store.jobs(),[])
        self.assertEqual(len(self.store.pending_classification()),1)
        self.store.commit_scan('title',[event(2)],'2026-11-19T00:03:00+00:00')
        self.assertEqual(len(self.store.jobs()),1)
        self.assertEqual(self.store.pending_classification(),[])
        self.store.commit_scan('title',[event(2)],'2026-11-19T00:04:00+00:00')
        self.assertEqual(len(self.store.jobs()),1)
    def test_recovered_source_does_not_queue_prelaunch_history(self):
        self.store.commit_scan('title',[],'2026-11-19T00:00:00+00:00')
        from dataclasses import replace
        old=replace(event(2),published_at='2026-11-18T00:00:00+08:00')
        self.store.commit_scan('aggregate',[old],'2026-11-19T00:02:00+00:00')
        self.store.commit_scan('title',[old],'2026-11-19T00:03:00+00:00')
        self.assertEqual(self.store.jobs(),[]);self.assertEqual(self.store.pending_classification(),[])
