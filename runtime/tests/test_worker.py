import concurrent.futures
from datetime import datetime,timezone,timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from hkipo.models import DocumentEvent
from hkipo.store import StateStore
from hkipo.downloads import PDFDownloader
from hkipo.worker import Worker
from test_downloads import pdf_bytes

def event(n,code='123',kind='offer'):
    return DocumentEvent('','fixture',str(n),code,'Test issuer',kind,'Formal document',f'https://www1.hkexnews.hk/listedco/{n}.pdf','2026-11-20T07:00:00+08:00','2026-11-20T00:00:00+00:00')

class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.store=StateStore(self.root/'db')
        self.downloader=PDFDownloader(self.root/'pdf',self.store,fetcher=lambda u:(pdf_bytes(),u));self.writer=Mock(return_value={'status':'ready_for_delivery'})
        self.worker=Worker(self.store,self.downloader,self.root/'jobs',{'offer':'fixture-skill','allotment':'fixture-skill','exception':'fixture-skill'},writer=self.writer)
    def tearDown(self):self.tmp.cleanup()
    def confirm(self,e,role,issuer='common_equity',episode='offer-20261120'):
        self.store.confirm_classification(e.event_id,issuer,{'offer':'ipo_offer','allotment':'ipo_allotment','exception':'ipo_exception'}[e.kind],role,episode)
    def test_announcement_waits_then_pairs_existing_same_episode(self):
        a=event(1);p=event(2);self.store.ingest([a]);self.confirm(a,'announcement')
        self.assertEqual(self.worker.run_one()['status'],'waiting_sources');self.writer.assert_not_called()
        self.store.ingest([p]);self.confirm(p,'prospectus')
        result=self.worker.run_one();self.assertEqual(result['status'],'ready_for_delivery')
        call=self.writer.call_args.kwargs;self.assertEqual(len(call['pdf_paths']),2)
        self.assertEqual(set(call['event']['source_event_ids']),{a.event_id,p.event_id});self.assertEqual(len(self.store.jobs('ready_for_delivery')),2)
        self.assertEqual(self.worker.run_one()['status'],'idle')
    def test_unconfirmed_and_etf_never_trigger_writer(self):
        e=event(1);self.store.ingest([e]);self.assertEqual(self.worker.run_one()['status'],'needs_classification')
        self.confirm(e,'prospectus',issuer='etf');self.assertEqual(self.worker.run_one()['status'],'needs_classification');self.writer.assert_not_called()
    def test_single_allotment_and_exception_independently_supported(self):
        for n,kind in [(1,'allotment'),(2,'exception')]:
            e=event(n,kind=kind);self.store.ingest([e]);self.confirm(e,kind,episode=kind)
        result=self.worker.run_batch();self.assertEqual(len(result),2);self.assertEqual(self.writer.call_count,2)
        self.assertNotEqual(result[0]['logical_event_id'],result[1]['logical_event_id'])
    def test_concurrent_claim_only_one_owner(self):
        e=event(1);self.store.ingest([e]);self.confirm(e,'announcement')
        def claim(n):return self.store.claim_job_group(str(n))
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:results=list(pool.map(claim,range(12)))
        self.assertEqual(sum(r is not None for r in results),1)
    def test_progressing_revision_continuation_releases_and_preserves_attempt_budget(self):
        e=event(1,kind='allotment');self.store.ingest([e]);self.confirm(e,'allotment')
        claim=self.store.claim_job_group('reviser')
        self.assertEqual(self.store.finish_research_claim(claim['token'],'revision',
            {'writer_result':{'status':'revision_pending'}},'revision_pending','revision_batch_continuation'),1)
        row=self.store.jobs()[0]
        self.assertEqual(row['status'],'pending');self.assertEqual(row['attempts'],0)
        self.assertIsNotNone(row['retry_after']);self.assertIsNone(self.store.claim_job_group('early'))
        self.assertEqual(self.store.research_run('revision')['status'],'revision_pending')
        self.assertEqual(self.store.finish_research_claim(claim['token'],'revision',{},'ready_for_delivery'),0)
    def test_expired_lease_recovery_fencing_and_retry_bound(self):
        e=event(1);self.store.ingest([e]);self.confirm(e,'announcement');now=datetime.now(timezone.utc)
        one=self.store.claim_job_group('one',lease_seconds=1,max_attempts=2,now=now)
        two=self.store.claim_job_group('two',lease_seconds=1,max_attempts=2,now=now+timedelta(seconds=2))
        self.assertNotEqual(one['token'],two['token']);self.assertEqual(self.store.finish_claim(one['token'],'complete',now=now+timedelta(seconds=2)),0)
        self.assertIsNone(self.store.claim_job_group('three',lease_seconds=1,max_attempts=2,now=now+timedelta(seconds=4)))
        self.assertEqual(self.store.jobs()[0]['status'],'blocked')
    def wait_for_write_lock(self, operation):
        import threading
        import time
        started=threading.Event();results=[]
        blocker=self.store.connect();blocker.execute('BEGIN IMMEDIATE')
        def run():
            started.set()
            results.append(operation())
        thread=threading.Thread(target=run)
        try:
            thread.start();self.assertTrue(started.wait(1))
            time.sleep(1.3)
            self.assertTrue(thread.is_alive())
        finally:
            blocker.commit();blocker.close();thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(results),1)
        return results[0]

    def test_finish_and_renew_lock_wait_cannot_revive_expired_lease(self):
        for operation in ('finish','renew','research'):
            with self.subTest(operation=operation):
                self.store=StateStore(self.root/operation)
                e=event(1,kind='allotment');self.store.ingest([e]);self.confirm(e,'allotment')
                claim=self.store.claim_job_group(operation,lease_seconds=1)
                before=self.store.jobs()[0]
                self.assertIsNotNone(claim)
                calls={
                    'finish':lambda:self.store.finish_claim(claim['token'],'complete'),
                    'renew':lambda:self.store.renew_claim(claim['token']),
                    'research':lambda:self.store.finish_research_claim(claim['token'],'lock-wait',{'old':True},'ready_for_delivery'),
                }
                self.assertEqual(self.wait_for_write_lock(calls[operation]),0)
                self.assertEqual(self.store.jobs()[0],before)
                self.assertIsNone(self.store.research_run('lock-wait'))

    def test_claim_wait_uses_lock_time_for_recovery_and_new_lease(self):
        e=event(1,kind='allotment');self.store.ingest([e]);self.confirm(e,'allotment')
        old=self.store.claim_job_group('old',lease_seconds=1)
        claim=self.wait_for_write_lock(lambda:self.store.claim_job_group('new',lease_seconds=2))
        self.assertIsNotNone(claim)
        self.assertNotEqual(claim['token'],old['token'])
        self.assertGreater(datetime.fromisoformat(claim['lease_until']),datetime.now(timezone.utc)+timedelta(seconds=1))
        self.assertEqual(self.store.finish_claim(old['token'],'complete'),0)
        self.assertFalse(self.store.renew_claim(old['token']))
        self.store.research_run('owner',{'new':True},'ready_for_delivery')
        self.assertEqual(self.store.finish_research_claim(old['token'],'owner',{'old':True},'blocked'),0)
        self.assertEqual(self.store.jobs()[0]['claim_token'],claim['token'])
        self.assertEqual(self.store.research_run('owner')['payload'],'{"new": true}')

    def test_finish_claim_explicit_clock_remains_deterministic(self):
        e=event(1,kind='allotment');self.store.ingest([e]);self.confirm(e,'allotment')
        now=datetime(2020,1,1,tzinfo=timezone.utc)
        claim=self.store.claim_job_group('clock',lease_seconds=10,now=now)
        self.assertEqual(self.store.finish_claim(claim['token'],'failed',retry_seconds=60,now=now+timedelta(seconds=1)),1)
        self.assertEqual(self.store.jobs()[0]['retry_after'],(now+timedelta(seconds=61)).isoformat())

    def test_failed_stock_does_not_block_next(self):
        a=event(1,kind='allotment');b=event(2,code='456',kind='allotment');self.store.ingest([a,b]);self.confirm(a,'allotment');self.confirm(b,'allotment')
        self.writer.side_effect=[RuntimeError('do not log secret'),{'status':'ready_for_delivery'}]
        results=self.worker.run_batch();self.assertEqual([r['status'] for r in results],['failed','ready_for_delivery'])
    def test_new_hash_changes_research_identity_and_retains_old(self):
        e=event(1,kind='allotment');self.store.ingest([e]);self.confirm(e,'allotment')
        first=self.worker.run_one();self.store.set_job_status(e.event_id,'pending')
        self.downloader.fetcher=lambda u:(pdf_bytes('changed source'),u)
        second=self.worker.run_one();self.assertNotEqual(first['research_id'],second['research_id']);self.assertEqual(first['logical_event_id'],second['logical_event_id']);self.assertEqual(self.writer.call_count,2)
        self.assertEqual(len(self.downloader.history(e.url)),2)
    def test_different_offer_episodes_do_not_merge(self):
        events=[event(n) for n in range(1,5)];self.store.ingest(events)
        for i,e in enumerate(events):self.confirm(e,'announcement' if i%2==0 else 'prospectus',episode='A' if i<2 else 'B')
        results=self.worker.run_batch();self.assertEqual(len(results),2)
        for call in self.writer.call_args_list:self.assertEqual(len(call.kwargs['event']['source_event_ids']),2)
    def test_baseline_candidate_requires_explicit_promotion(self):
        e=event(1,kind='allotment');self.store.commit_scan('title',[],'2026-11-20T00:00:00+00:00')
        self.store.commit_scan('aggregate',[e],'2026-11-20T00:02:00+00:00')
        self.assertEqual(len(self.store.pending_classification()),1);self.assertEqual(self.worker.run_one()['status'],'idle')
        self.store.confirm_classification(e.event_id,'common_equity','ipo_allotment','allotment','episode',promote_candidate=True)
        self.assertEqual(self.store.pending_classification(),[]);self.assertEqual(self.worker.run_one()['status'],'ready_for_delivery')
    def test_retry_after_600_is_persisted(self):
        from hkipo.downloads import DownloadError
        e=event(1,kind='allotment');self.store.ingest([e]);self.confirm(e,'allotment')
        self.downloader.download=Mock(side_effect=DownloadError('http_429_retry_later',retry_after=600))
        before=datetime.now(timezone.utc);self.assertEqual(self.worker.run_one()['status'],'failed')
        retry=datetime.fromisoformat(self.store.jobs()[0]['retry_after']);self.assertGreaterEqual((retry-before).total_seconds(),600)
    def test_lease_loss_after_writer_does_not_mark_ready(self):
        e=event(1,kind='allotment');self.store.ingest([e]);self.confirm(e,'allotment')
        def writer(**options):
            with self.store.connect() as c:c.execute("UPDATE jobs SET lease_until='2000-01-01T00:00:00+00:00' WHERE event_id=?",(e.event_id,))
            return {'status':'ready_for_delivery'}
        self.worker.writer=writer
        self.assertEqual(self.worker.run_one()['status'],'lease_lost');self.assertEqual(self.store.jobs('ready_for_delivery'),[])


class WorkerQuotaDeferralTests(unittest.TestCase):
    setUp = WorkerTests.setUp
    tearDown = WorkerTests.tearDown
    confirm = WorkerTests.confirm

    def observation(self, now):
        from hkipo.quota import evaluate_limits
        return evaluate_limits({'rateLimits': {'limitId': 'codex', 'primary': {
            'windowDurationMins': 300, 'usedPercent': 80, 'resetsAt': now.timestamp()+300}}}, now=now.timestamp())

    def test_worker_structured_defer_preserved_but_plain_defer_blocked(self):
        from hkipo.quota import quota_deferral
        e=event(1,kind='allotment');self.store.ingest([e]);self.confirm(e,'allotment')
        now=datetime.now(timezone.utc)
        self.writer.return_value={'status':'deferred','quota_defer':quota_deferral(self.observation(now),now.timestamp())}
        result=self.worker.run_one()
        self.assertEqual(result['status'],'deferred')
        row=self.store.jobs()[0]
        self.assertEqual(row['status'],'pending');self.assertEqual(row['attempts'],0)
        self.assertEqual(self.store.research_run(result['research_id'])['status'],'deferred')
        restarted=StateStore(self.root/'db')
        self.assertIsNone(restarted.claim_job_group('early',now=now+timedelta(seconds=419)))
        supplement=event(3,kind='allotment');self.store.ingest([supplement]);self.confirm(supplement,'allotment')
        self.assertIsNone(restarted.claim_job_group('new-material-early',now=now+timedelta(seconds=419)))
        self.assertIsNotNone(restarted.claim_job_group('due',now=now+timedelta(seconds=421)))
        other=event(2,code='456',kind='allotment');self.store.ingest([other]);self.confirm(other,'allotment')
        self.writer.return_value={'status':'deferred','reason':'QuotaUnavailable'}
        self.assertEqual(self.worker.run_one()['status'],'blocked')

    def test_repeated_quota_deferrals_do_not_consume_attempts_and_fence(self):
        from hkipo.quota import quota_deferral
        e=event(1,kind='allotment');self.store.ingest([e]);self.confirm(e,'allotment')
        now=datetime.now(timezone.utc)
        stale_token=None
        for index in range(5):
            current=now+timedelta(seconds=index*421)
            claim=self.store.claim_job_group('fake',max_attempts=1,now=current)
            self.assertIsNotNone(claim)
            deferred=quota_deferral(self.observation(current),current.timestamp())
            if stale_token:
                self.assertEqual(self.store.defer_research_claim(stale_token,'stale',{},deferred,now=current),0)
                self.assertIsNone(self.store.research_run('stale'))
            self.assertEqual(self.store.defer_research_claim(claim['token'],'research',{},deferred,now=current),1)
            self.assertEqual(self.store.jobs()[0]['attempts'],0)
            self.assertEqual(self.store.research_run('research')['status'],'deferred')
            stale_token=claim['token']
        due=now+timedelta(seconds=5*421)
        claim=self.store.claim_job_group('new',lease_seconds=1,now=due)
        deferred=quota_deferral(self.observation(due),due.timestamp())
        self.assertEqual(self.store.defer_research_claim(claim['token'],'expired',{},deferred,now=due+timedelta(seconds=2)),0)
        self.assertIsNone(self.store.research_run('expired'))

    def test_stale_observation_cannot_release_claim(self):
        from hkipo.quota import quota_deferral
        e=event(1,kind='allotment');self.store.ingest([e]);self.confirm(e,'allotment')
        now=datetime.now(timezone.utc)
        claim=self.store.claim_job_group('fake',now=now)
        deferred=quota_deferral(self.observation(now),now.timestamp())
        self.assertEqual(self.store.defer_research_claim(claim['token'],'stale',{},deferred,now=now+timedelta(seconds=61)),0)
        self.assertEqual(self.store.jobs()[0]['status'],'running')
        self.assertIsNone(self.store.research_run('stale'))

    def test_quota_defer_lock_wait_crossing_lease_cannot_commit(self):
        from hkipo.quota import quota_deferral
        import threading
        import time
        e=event(1,kind='allotment');self.store.ingest([e]);self.confirm(e,'allotment')
        now=datetime.now(timezone.utc)
        claim=self.store.claim_job_group('old',lease_seconds=1,now=now)
        deferred=quota_deferral(self.observation(now),now.timestamp())
        started=threading.Event()
        result=[]
        blocker=self.store.connect()
        blocker.execute('BEGIN IMMEDIATE')
        def defer():
            started.set()
            result.append(self.store.defer_research_claim(claim['token'],'lock-wait',{},deferred))
        thread=threading.Thread(target=defer)
        try:
            thread.start()
            self.assertTrue(started.wait(1))
            time.sleep(1.3)
            self.assertTrue(thread.is_alive())
        finally:
            blocker.commit();blocker.close()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result,[0])
        row=self.store.jobs()[0]
        self.assertEqual(row['status'],'running')
        self.assertEqual(row['attempts'],1)
        self.assertEqual(row['claim_token'],claim['token'])
        self.assertIsNone(self.store.research_run('lock-wait'))
