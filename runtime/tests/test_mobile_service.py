import hashlib
import json
from email.message import EmailMessage
from pathlib import Path
import tempfile
import unittest
from hkipo.delivery import DEFAULT_RECIPIENT
from hkipo.mail_reply import MailReplyStore
from hkipo.mobile import MobileWorkflow
from hkipo.mobile_service import MobileService,MobileSettings
from hkipo.store import StateStore
from test_mobile import PNG,FakeWeChat


class MobileServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.cfg=self.root/'config.json';self.cfg.write_text(json.dumps({'email':{'user':DEFAULT_RECIPIENT,'password':'fake','smtp_host':'smtp.example.test','smtp_port':465},'wechat':{'appid':'fake','appsecret':'fake'}}))
        self.profile=self.root/'profile.json';self.profile.write_text(json.dumps({'production_accepted':False}))
        self.store=MailReplyStore(self.root/'intake.sqlite3',[DEFAULT_RECIPIENT]);self.sent=[];self.wechat=FakeWeChat();self.polls=[];self.layouts=[]
        def smtp(cfg,msg,to):self.sent.append(msg);return {'smtp_accepted':True,'message_id':str(msg['Message-ID'])}
        self.factory=lambda store,cfg:MobileWorkflow(store,cfg,smtp_sender=smtp,wechat_client=self.wechat)
        self.flow=self.factory(self.store,json.loads(self.cfg.read_text()))
        image=self.root/'table.png';image.write_bytes(PNG)
        self.event={'event_id':'allowed','stock_code':'00012','issuer':'样例','episode':'2026','source_url':'https://example.test/source','source_time':'2026-11-21','first_seen':'2026-11-21','brief':'synthetic','source_reviewed':True,'images':[{'path':str(image),'sha256':hashlib.sha256(PNG).hexdigest()}]}
        self.flow.register_event(self.event)
    def tearDown(self):self.temp.cleanup()
    def test_string_false_cannot_enable_delivery_or_bypass_acceptance_gate(self):
        for name in ('enable_deliveries','acceptance_mode','table_cover_authorized'):
            with self.subTest(name=name),self.assertRaises(ValueError):
                MobileSettings(str(self.root),str(self.cfg),str(self.profile),**{name:'false'})
        with self.assertRaises(ValueError):
            MobileSettings(str(self.root),str(self.cfg),str(self.profile),accepted_event_ids='allowed')
    def pending(self,event='allowed'):
        self.store.register_reminder('<source@example.test>',event)
        msg=EmailMessage();msg['From']=DEFAULT_RECIPIENT;msg['Message-ID']='<reply@example.test>';msg['In-Reply-To']='<source@example.test>'
        msg.set_content('标题: 作者标题\n正文:\n作者正文\n【正文结束】')
        return self.store.ingest(msg.as_bytes(),authenticated_sender=DEFAULT_RECIPIENT)
    def service(self,**kwargs):
        settings=MobileSettings(str(self.root),str(self.cfg),str(self.profile),**kwargs)
        def poll(*args):self.polls.append(1);return {'status':'idle'}
        def layout(directory,profile):
            self.layouts.append(1);manifest=json.loads((directory/'manifest.json').read_text())
            return {'status':'approved_local_layout','approval':{'confirmed':True,**manifest}}
        return MobileService(settings,poller=poll,layout_checker=layout,flow_factory=self.factory)
    def test_disabled_produces_only_preview_no_delivery_or_layout_renderer(self):
        self.pending();result=self.service().cycle()
        self.assertEqual(result['drafts'][0]['status'],'preview_ready_delivery_disabled')
        self.assertEqual(self.sent,[]);self.assertEqual(self.wechat.calls,[]);self.assertEqual(self.layouts,[])
    def test_unaccepted_production_blocks_all_delivery_and_poll(self):
        self.pending();result=self.service(enable_deliveries=True).cycle()
        self.assertEqual(result['reason'],'real_mail_and_wechat_acceptance_required')
        self.assertEqual(self.sent,[]);self.assertEqual(self.polls,[]);self.assertEqual(self.layouts,[])
    def test_enabled_empty_official_store_and_research_cycle_is_idle(self):
        state_path=self.root/'official.sqlite3'
        state=StateStore(state_path)
        with state.connect() as db:state._worker_schema(db)
        research=self.root/'research';research.mkdir()
        self.profile.write_text(json.dumps({'production_accepted':True}))
        settings=MobileSettings(str(self.root/'empty-mobile'),str(self.cfg),str(self.profile),
                                enable_deliveries=True,official_state_db=str(state_path),
                                research_root=str(research))
        def poll(*args):self.polls.append(1);return {'status':'idle'}
        service=MobileService(settings,poller=poll,flow_factory=self.factory)
        result=service.cycle()
        self.assertEqual(result,{'status':'ok','incoming':{'status':'idle'},
                                 'reminders':[],'drafts':[],'delivery_enabled':True})
        self.assertEqual(self.polls,[1])
        self.assertEqual(self.sent,[]);self.assertEqual(self.wechat.calls,[])
    def test_acceptance_allowlist_does_not_expand_authority(self):
        self.pending();result=self.service(enable_deliveries=True,acceptance_mode=True,accepted_event_ids=['different']).cycle()
        self.assertEqual(result['drafts'],[]);self.assertEqual(result['reminders'],[])
        self.assertEqual(self.sent,[]);self.assertEqual(self.wechat.calls,[])
    def test_pending_to_bound_layout_fake_draft_then_receipt(self):
        row=self.pending();result=self.service(enable_deliveries=True,acceptance_mode=True,accepted_event_ids=['allowed'],thumb_media_id='cover').cycle()
        self.assertEqual(result['drafts'][0]['status'],'confirmed')
        self.assertEqual(result['drafts'][0]['mail_receipt']['status'],'confirmed')
        self.assertEqual(len(self.layouts),1);self.assertEqual(len(self.wechat.mutations()),1)
        with self.store.connect() as c:self.assertEqual(c.execute('SELECT status FROM reply_messages WHERE raw_hash=?',(row['raw_hash'],)).fetchone()[0],'complete')
        self.assertTrue(all('publish' not in call[0] for call in self.wechat.calls))
    def test_cover_outside_reviewed_original_has_no_network_effect(self):
        row=self.pending();directory=self.root/'cover-preview';manifest=self.flow.preview(row,directory)
        outside=self.root/'unreviewed.png';outside.write_bytes(PNG)
        result=self.flow.process(row['raw_hash'],directory,layout_approval={'confirmed':True,**manifest},cover_path=outside,cover_authorized=True)
        self.assertEqual(result['status'],'needs_cover');self.assertEqual(self.wechat.calls,[])
