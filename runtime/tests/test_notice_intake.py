import hashlib
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from test_notices import NoticesTests
from hkipo.notice_intake import dispatch_official, schedule_ready_offers
from hkipo.reminders import HK
from hkipo.store import StateStore

class IntakeTests(NoticesTests):
    def setUp(self):
        super().setUp()
        self.root=Path(self.tmp.name); self.state=StateStore(self.root/'state.db')
        with self.state.connect() as db: self.state._worker_schema(db)
        self.doc=dict(event_id='doc1',kind='allotment',url='https://www1.hkexnews.hk/a.pdf',title='全球發售 配發結果',stock_code='00012',stock_name='例子',published_at='2026-11-20',discovered_at='2026-11-20',language='zh-HK')
    def add_event(self,event,baseline=0):
        with self.state.connect() as db:
            db.execute('INSERT OR REPLACE INTO events VALUES(?,?,?,?)',(event['event_id'],json.dumps(event),baseline,'now'))
    def save(self,path,data):
        path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(data));return str(path)
    def ready(self,items=None,quote='正式截止2026年11月23日'):
        self.offer=dict(self.doc,event_id='offer1',kind='offer');self.add_event(self.offer)
        root=self.root/'research'/'research1'; pdf=root/'doc.pdf';pdf.parent.mkdir(parents=True,exist_ok=True);pdf.write_bytes(b'offline fake PDF identity')
        sha=hashlib.sha256(pdf.read_bytes()).hexdigest(); did=sha[:12]
        self.manifest={'documents':[dict(document_id=did,path=str(pdf),sha256=sha,pages=[dict(pdf_page=1,text=quote)])]}
        self.save(root/'sources/manifest.json',self.manifest)
        self.bundle={'event_id':'logical1','kind':'offer','facts':[dict(key='subscription_period',status='supported',items=items if items is not None else [{'close_date':'2026-11-23'}],evidence=[dict(document_id=did,pdf_page=1,quote=quote)])]}
        wr=dict(status='ready_for_delivery',request_id='request1',bundle_path=self.save(root/'bundle.json',self.bundle),quality_path=self.save(root/'quality.json',{'pass':True}))
        self.save(root/'status.json',wr)
        event=dict(self.offer,event_id='logical1',episode_id='episode1',source_event_ids=['offer1'],source_events=[self.offer],source_hashes=[dict(url=self.offer['url'],sha256=sha)])
        self.state.research_run('research1',dict(event=event,writer_result=wr),'ready_for_delivery')
        return root
    def run_schedule(self):
        return schedule_ready_offers(self.state,self.root/'research',self.store,datetime(2026,11,21,tzinfo=HK))
    def test_dispatch_live_only_and_restart(self):
        self.add_event(self.doc)
        self.add_event(dict(self.doc,event_id='history',url='https://www1.hkexnews.hk/b.pdf'),1)
        dispatch_official(self.state,self.flow)
        dispatch_official(self.state,self.flow)
        self.assertEqual(len(self.sent),0)
        result=dispatch_official(self.state,self.flow)
        self.assertEqual(result[0]['status'],'retained_for_grey_analysis')
    def test_dispatch_role_evidence_and_unknown(self):
        d=dict(self.doc,title='配發結果');self.add_event(d)
        self.assertEqual(dispatch_official(self.state,self.flow)[0]['status'],'skipped_unassociated')
        evidence=dict(category='official_new_listing_table',role='allotment',url=d['url'],stock_code='00012',source_sha256='sha',locator='row1',source_url='https://www2.hkexnews.hk/New-Listings/New-Listing-Information/Main-Board?sc_lang=zh-HK')
        with patch('hkipo.service.SnapshotArchive.evidence',return_value=([evidence],[])):
            dispatch_official(self.state,self.flow,self.root/'snapshots')
        self.assertEqual(len(self.sent),0)
    def test_ready_schedule_cached(self):
        self.ready()
        self.assertEqual(self.run_schedule()[0]['status'],'scheduled')
        self.assertEqual(self.run_schedule()[0]['status'],'already_scheduled')
        with self.store._db() as db: self.assertEqual(db.execute("SELECT count(*) FROM writing_schedule_evidence WHERE issue_id='episode1'").fetchone()[0],1)
    def test_no_items_or_conflicting_pricing(self):
        self.ready(items=[])
        self.assertEqual(self.run_schedule()[0]['status'],'needs_timetable')
        self.ready(items=[{'close_date':'2026-11-23','pricing_date':'2026-11-25'}],quote='2026-11-23 截止；2026-11-25 定价')
        self.assertEqual(self.run_schedule()[0]['status'],'needs_timetable')
    def test_stale_changed_source_or_unquoted_date(self):
        root=self.ready(quote='日期未定')
        self.assertEqual(self.run_schedule()[0]['status'],'needs_timetable')
        root=self.ready(); status=json.loads((root/'status.json').read_text());status['request_id']='new';self.save(root/'status.json',status)
        self.assertEqual(self.run_schedule()[0]['status'],'needs_timetable')
        self.ready(); (root/'doc.pdf').write_bytes(b'changed')
        self.assertEqual(self.run_schedule()[0]['status'],'needs_timetable')
    def test_explicit_delay_unique_issue_only(self):
        with self.store._db() as db: db.execute("DELETE FROM writing_reminders")
        self.ready();self.run_schedule()
        self.add_event(dict(self.doc,event_id='delay',kind='exception',title='全球發售 延期'))
        self.run_schedule()
        self.assertEqual({r['status'] for r in self.store.rows('episode1')},{'cancelled'})

    def test_allotment_facts_are_retained_by_handler_without_immediate_mail(self):
        self.add_event(self.doc)
        captured=[]
        result=dispatch_official(self.state,self.flow,allotment_handler=lambda event,doc:(captured.append((event,doc)) or {'status':'retained_for_grey_analysis'}))
        self.assertEqual(result[0]['status'],'retained_for_grey_analysis')
        self.assertEqual(captured[0][1]['event_id'],'doc1')
        self.assertEqual(self.sent,[])
    def test_exception_notice_still_sends_immediately(self):
        self.add_event(dict(self.doc,kind='exception',title='全球發售 延期'))
        result=dispatch_official(self.state,self.flow)
        self.assertEqual(len(self.sent),1)
        self.assertEqual(result[0]['status'],'confirmed')
