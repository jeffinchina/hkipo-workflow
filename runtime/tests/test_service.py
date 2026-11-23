import os
from dataclasses import replace
from datetime import datetime,timezone,timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock
from hkipo.service import LocalService,ServiceSettings,SnapshotArchive,singleton_lock
from hkipo.sources import SourceResult,SourceError,TitleSearchSource,AggregateSource,parse_aggregate
from hkipo.models import DocumentEvent
from hkipo.downloads import PDFDownloader,DownloadError
from hkipo.worker import Worker

FIX=Path(__file__).parent/'fixtures'
class ReplaySource:
    name='legacy-replay';timestamp_precision='minute'
    def __init__(self):self.empty=True;self.calls=0
    def fetch(self,start,end):
        self.calls+=1
        fixture=json.loads((FIX/'yifei-legacy-official-row.json').read_text());stock=fixture['stock'];events=[]
        body={'date':fixture['date'],'stocks':[] if self.empty else [stock],'provenance':fixture['provenance'],'snapshot_sha256':fixture['snapshot_sha256']}
        if not self.empty:
            for key in ['announcement','prospectus']:
                events.append(DocumentEvent('',self.name,key,stock['code'],stock['name'],'offer','新上市公告' if key=='announcement' else '招股章程',stock[key],'2026-05-08T07:00:00+08:00',end.isoformat()))
        return SourceResult(self.name,events,True,len(events),'honest legacy metadata replay',[{'url':'https://www2.hkexnews.hk/New-Listings/New-Listing-Information/Main-Board?sc_lang=zh-HK','format':'legacy_aggregate','body':json.dumps(body,ensure_ascii=False)}])

class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.now=datetime(2026,5,8,tzinfo=timezone.utc)
    def tearDown(self):self.tmp.cleanup()
    def test_real_metadata_and_pdf_replay_to_fake_writer_once(self):
        sources_dir=Path(os.environ.get('HKIPO_TEST_SOURCE_DIR','/optional-fixtures/downloads'))
        if not (sources_dir/'6871_浙江翼菲智能科技股份有限公司_公告.pdf').exists():self.skipTest('Authorized existing originals missing')
        source=ReplaySource();settings=ServiceSettings(str(self.root),allow_legacy_replay=True,skill_paths={'offer':'fixture-skill'},writer_options={'settings_path':'/fixture/local-settings.json'})
        service=LocalService(settings,sources=[source]);fetches=[]
        def fetch(url):
            fetches.append(url)
            role='公告' if url.endswith('00002_c.pdf') else '招股章程'
            return (sources_dir/('6871_浙江翼菲智能科技股份有限公司_'+role+'.pdf')).read_bytes(),url
        # Use actual role URLs, not filename-based guessed roles in production.
        stock=json.loads((FIX/'yifei-legacy-official-row.json').read_text())['stock']
        files={stock['announcement']:sources_dir/'6871_浙江翼菲智能科技股份有限公司_公告.pdf',stock['prospectus']:sources_dir/'6871_浙江翼菲智能科技股份有限公司_招股章程.pdf'}
        service.downloader=PDFDownloader(self.root/'pdf',service.store,fetcher=lambda u:(files[u].read_bytes(),u))
        writer=Mock(return_value={'status':'ready_for_delivery'})
        service.worker=Worker(service.store,service.downloader,self.root/'research',settings.skill_paths,writer=writer)
        baseline=service.cycle(scan=True,classify=True,work=True,now=self.now)
        self.assertEqual(service.store.jobs(),[]);writer.assert_not_called()
        source.empty=False
        result=service.cycle(scan=True,classify=True,work=True,now=self.now+timedelta(minutes=1))
        self.assertEqual(result['classification']['decisions'][0]['decision'],'confirmed')
        self.assertEqual(result['work']['results'][0]['status'],'ready_for_delivery');self.assertEqual(writer.call_count,1)
        event=writer.call_args.kwargs['event'];self.assertEqual(len(event['source_event_ids']),2)
        self.assertEqual(writer.call_args.kwargs['settings_path'],'/fixture/local-settings.json')
        service.cycle(scan=True,classify=True,work=True,now=self.now+timedelta(minutes=2));self.assertEqual(writer.call_count,1)
        self.assertTrue((self.root/'health.json').exists());self.assertFalse(json.loads((self.root/'health.json').read_text())['external_delivery_enabled'])
    def test_raw_official_snapshots_hash_and_no_baseline_download(self):
        raw=(FIX/'main-board-real.html').read_text();url='https://www2.hkexnews.hk/New-Listings/New-Listing-Information/Main-Board?sc_lang=zh-HK'
        source=Mock();source.name='new-listings-main-board';source.timestamp_precision='date'
        result=parse_aggregate(raw,url,source.name);result.responses=[{'url':url,'format':'html','body':raw}];source.fetch.return_value=result
        downloader=Mock();service=LocalService(ServiceSettings(str(self.root)),sources=[source],downloader=downloader)
        service.cycle(scan=True,classify=True,now=self.now)
        downloader.download.assert_not_called()
        manifest=next(service.archive.manifests());record=manifest['responses'][0]
        self.assertEqual(record['sha256'],hashlib.sha256(raw.encode()).hexdigest());self.assertEqual(Path(record['path']).read_text(),raw)
        evidence,errors=service.archive.evidence();self.assertEqual(len(evidence),16);self.assertFalse(errors)
        self.assertEqual(Path(record['path']).stat().st_mode&0o777,0o600)
    def test_audit_failure_does_not_advance_source_watermark(self):
        source=Mock();source.name='title-search';source.timestamp_precision='minute';source.fetch.return_value=SourceResult(source.name,[],True,0)
        service=LocalService(ServiceSettings(str(self.root)),sources=[source])
        result=service.cycle(scan=True,now=self.now)
        self.assertFalse(result['scan']['ok']);self.assertIsNone(service.store.source_state(source.name)['cutoff']);self.assertEqual(len(service.store.alerts()),1)
    def test_bad_source_and_retry_after_health_then_recovery(self):
        source=Mock();source.name='title-search';source.timestamp_precision='minute';source.fetch.side_effect=SourceError('rate limited',retry_after=600)
        service=LocalService(ServiceSettings(str(self.root)),sources=[source]);service.cycle(now=self.now)
        self.assertFalse(json.loads((self.root/'health.json').read_text())['ok'])
        restored=LocalService(service.settings,sources=[source]);restored.cycle(now=self.now+timedelta(seconds=60));self.assertEqual(source.fetch.call_count,1)
        source.fetch.side_effect=None;raw=json.dumps({'result':'[]','recordCnt':0,'loadedRecord':0,'hasNextRow':False})
        source.fetch.return_value=SourceResult(source.name,[],True,0,responses=[{'url':'https://www1.hkexnews.hk/search/titleSearchServlet.do','format':'json','body':raw}])
        restored.cycle(now=self.now+timedelta(seconds=601));self.assertEqual(restored.store.alerts(),[])
    def test_snapshot_session_values_redacted_and_tamper_detected(self):
        archive=SnapshotArchive(self.root/'snapshots')
        body='<html><input type="hidden" name="javax.faces.ViewState" value="PRIVATE_SESSION"><a href="/x;jsessionid=PRIVATE_SESSION">link</a></html>'
        result=SourceResult('fixture',[],responses=[{'url':'https://www2.hkexnews.hk/New-Listings/New-Listing-Information/Main-Board?sc_lang=zh-HK','format':'html','body':body}])
        record=archive.save_result(result)[0];self.assertTrue(record['sanitized']);self.assertNotIn('PRIVATE_SESSION',Path(record['path']).read_text())
        Path(record['path']).write_text('tampered');self.assertTrue(archive.evidence()[1])
    def test_no_new_event_never_calls_writer_and_default_work_disabled(self):
        service=LocalService(ServiceSettings(str(self.root)),sources=[])
        self.assertEqual(service.cycle(scan=False,classify=True,work=True)['work']['status'],'disabled')
    def test_blocked_writer_is_unhealthy_even_with_successful_scan(self):
        service=LocalService(ServiceSettings(str(self.root)),sources=[])
        event=DocumentEvent('','fixture','1','03275','issuer','offer','offer',
            'https://www1.hkexnews.hk/listedco/1.pdf','2026-11-23T06:00:00+08:00',self.now.isoformat())
        service.store.ingest([event])
        with service.store.connect() as c:
            service.store._worker_schema(c)
            c.execute("UPDATE jobs SET status='blocked',reason='revision_stalled'")
        health=service._health({'scan':{'ok':True}})
        self.assertFalse(health['ok'])
        self.assertEqual(health['queue_attention'][0]['reason'],'revision_stalled')
        self.assertEqual(health['queue_counts']['blocked'],1)
    def test_singleton_locks_and_background_dispatch_are_bounded(self):
        service=LocalService(ServiceSettings(str(self.root)),sources=[])
        with singleton_lock(self.root/'locks/pipeline.lock') as acquired:
            self.assertTrue(acquired);self.assertEqual(service.cycle(scan=False,classify=True)['pipeline']['status'],'busy')
        fake=Mock();fake.is_alive.return_value=True;factory=Mock(return_value=fake);service.process_factory=factory
        self.assertEqual(service.dispatch_pipeline()['status'],'started');self.assertEqual(service.dispatch_pipeline()['status'],'already_running');factory.assert_called_once()
        fake.start.assert_called_once()
    def test_loop_scans_without_foreground_work(self):
        service=LocalService(ServiceSettings(str(self.root)),sources=[]);service.cycle=Mock(return_value={});service.dispatch_pipeline=Mock()
        self.assertEqual(service.run_loop(threading.Event(),max_cycles=1)['cycles'],1)
        service.cycle.assert_called_once_with(scan=True,classify=False,work=False);service.dispatch_pipeline.assert_called_once()
    def test_short_overlap_and_persisted_source_cutoff(self):
        raw=json.dumps({'result':'[]','recordCnt':0,'loadedRecord':0,'hasNextRow':False})
        source=Mock();source.name='title-search';source.timestamp_precision='minute';source.fetch.return_value=SourceResult(source.name,[],True,0,responses=[{'url':'https://www1.hkexnews.hk/search/titleSearchServlet.do','format':'json','body':raw}])
        service=LocalService(ServiceSettings(str(self.root)),sources=[source]);service.cycle(now=self.now)
        restored=LocalService(service.settings,sources=[source]);restored.cycle(now=self.now+timedelta(hours=3))
        start,end=source.fetch.call_args.args;self.assertEqual(start,self.now-timedelta(minutes=15));self.assertEqual(end,self.now+timedelta(hours=3))

    def test_ambiguous_same_day_baseline_not_replayed_by_type_confirmation(self):
        source=ReplaySource();source.empty=False
        settings=ServiceSettings(str(self.root),allow_legacy_replay=True)
        service=LocalService(settings,sources=[source])
        # Anchor established by another source; late aggregate date cannot prove
        # whether these same-day documents precede startup.
        service.store.commit_scan('title-search',[],self.now.isoformat())
        result=source.fetch(self.now,self.now+timedelta(minutes=1))
        service.store.commit_scan('aggregate',result.events,(self.now+timedelta(minutes=1)).isoformat(),timestamp_precision='date')
        service.archive.save_result(result)
        sources_dir=Path(os.environ.get('HKIPO_TEST_SOURCE_DIR','/optional-fixtures/downloads'));stock=json.loads((FIX/'yifei-legacy-official-row.json').read_text())['stock']
        files={stock['announcement']:sources_dir/'6871_浙江翼菲智能科技股份有限公司_公告.pdf',stock['prospectus']:sources_dir/'6871_浙江翼菲智能科技股份有限公司_招股章程.pdf'}
        if not all(p.exists() for p in files.values()):self.skipTest('Authorized original PDFs missing')
        service.downloader=PDFDownloader(self.root/'pdf',service.store,fetcher=lambda u:(files[u].read_bytes(),u))
        service.cycle(scan=False,classify=True)
        self.assertEqual(service.store.jobs(),[]);self.assertEqual(len(service.store.pending_classification()),2)
