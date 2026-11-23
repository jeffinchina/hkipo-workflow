import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
from hkipo.delivery import Deliverer,DeliveryError,WeChatClient,verify_release
from hkipo.render import article_text,article_html
from hkipo.render_profile import defaults
from hkipo.store import StateStore

class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.store=StateStore(self.root/'db')
        self.config={'email':{'smtp_host':'smtp.invalid','smtp_port':465,'user':'sender@example.invalid','password':'SECRET_NEVER_LOG'},'wechat':{'appid':'fixture-account','appsecret':'SECRET_NEVER_LOG'}}
        self.article={'title':'港股打新 测试','blocks':[{'role':'paragraph','text':'这是冻结后的文章正文。','fact_ids':[]}]}
        self.bundle={'event_id':'event-1','article':self.article}
        self.release=self.make_release()
        self.validator=patch('hkipo.delivery.validate_bundle',return_value={'pass':True});self.validate=self.validator.start()
        self.smtp=Mock(return_value={'smtp_accepted':True,'message_id':'<test@local.invalid>'})
        self.wx=Mock();self.wx.token.return_value='PRIVATE_TOKEN';self.wx.cover.return_value='cover-id';self.wx.inline_image.return_value='https://mmbiz.qpic.cn/qr.jpg';self.wx.call.return_value={'media_id':'draft-id'}
        self.d=Deliverer(self.store,config=self.config,smtp_sender=self.smtp,wechat_client=self.wx)
    def tearDown(self):self.validator.stop();self.temp.cleanup()
    def make_release(self):
        out=self.root/'out';out.mkdir(exist_ok=True)
        (out/'article.txt').write_text(article_text(self.article));(out/'article.html').write_text(article_html(self.article));(out/'article.docx').write_bytes(b'FAKE_DOCX_OFFLINE_TEST')
        self.bundle['article']=copy.deepcopy(self.article)
        for n,v in [('bundle',self.bundle),('review',{'decision':'pass'}),('quality',{'pass':True}),('sources',{'documents':[]}),('qa',{'confirmed':True,'docx_sha256':hashlib.sha256((out/'article.docx').read_bytes()).hexdigest()})]:
            (self.root/(n+'.json')).write_text(json.dumps(v,ensure_ascii=False))
        return {'render_version':3,'render_profile':defaults()['style'],'event_id':'event-1','bundle_path':str(self.root/'bundle.json'),'review_path':str(self.root/'review.json'),'quality_path':str(self.root/'quality.json'),'sourcesmanifest_path':str(self.root/'sources.json'),'visual_qa_path':str(self.root/'qa.json'),'output_dir':str(out),'bundle_sha256':hashlib.sha256((self.root/'bundle.json').read_bytes()).hexdigest(),'review_sha256':hashlib.sha256((self.root/'review.json').read_bytes()).hexdigest(),'files_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in out.iterdir()}}
    def test_email_contains_article_both_mime_and_attachment(self):
        result=self.d.email(self.release);self.assertEqual(result['status'],'confirmed');self.validate.assert_called_once()
        cfg,msg,to=self.smtp.call_args.args
        self.assertEqual(to,'recipient@example.invalid');self.assertIn('这是冻结后的文章正文',msg.get_body(preferencelist=('plain',)).get_content())
        from hkipo.delivery import personal_qr_source
        from hkipo.render import qr_bytes
        self.assertEqual(msg.get_body(preferencelist=('html',)).get_content().strip(),article_html(self.article).replace(personal_qr_source(),'cid:hkipo-personal-qr'))
        related=[part for part in msg.walk() if part.get('Content-ID')=='<hkipo-personal-qr>']
        self.assertEqual(len(related),1);self.assertEqual(related[0].get_payload(decode=True),qr_bytes())
        self.assertEqual(len(list(msg.iter_attachments())),1)
        self.assertEqual(self.d.email(self.release)['status'],'already_confirmed');self.assertEqual(self.smtp.call_count,1)
    def test_unknown_stops_retry_even_changed_body(self):
        self.smtp.side_effect=RuntimeError('PRIVATE_TOKEN query secret')
        self.assertEqual(self.d.email(self.release)['status'],'needs_reconciliation')
        self.assertEqual(self.d.email(self.release)['status'],'needs_reconciliation')
        self.article['blocks'][0]['text']='新正文';self.release=self.make_release()
        self.assertEqual(self.d.email(self.release)['status'],'needs_reconciliation');self.assertEqual(self.smtp.call_count,1)
    def test_mutated_docx_and_missing_attachment_block(self):
        path=Path(self.release['output_dir'])/'article.docx';path.write_bytes(b'tampered')
        with self.assertRaises(DeliveryError):self.d.email(self.release)
        path.unlink()
        with self.assertRaises(DeliveryError):self.d.email(self.release)
        self.smtp.assert_not_called()
    def test_recompute_quality_and_visual_gate(self):
        self.validate.return_value={'pass':False}
        with self.assertRaises(DeliveryError):self.d.email(self.release)
        self.validate.return_value={'pass':True};(self.root/'qa.json').write_text('{"confirmed":false}')
        with self.assertRaises(DeliveryError):self.d.email(self.release)
        self.smtp.assert_not_called()
    def test_article_body_cannot_drift_with_refrozen_file_hash(self):
        path=Path(self.release['output_dir'])/'article.txt';path.write_text('完成通知')
        self.release['files_sha256']['article.txt']=hashlib.sha256(path.read_bytes()).hexdigest()
        with self.assertRaises(DeliveryError):self.d.email(self.release)
        self.smtp.assert_not_called()
    def test_missing_cover_never_calls_api(self):
        with patch('hkipo.delivery.defaults',return_value={'thumb_media_id':None}):
            self.assertEqual(self.d.draft(self.release)['status'],'needs_cover')
        self.wx.token.assert_not_called()
        cover=self.root/'approved.png';cover.write_bytes(b'fake approved image')
        self.assertEqual(self.d.draft(self.release,cover_path=cover)['status'],'needs_cover');self.wx.token.assert_not_called()
    def test_create_draft_idempotent_then_explicit_update(self):
        first=self.d.draft(self.release,thumb_media_id='approved-cover');self.assertEqual(first['status'],'confirmed')
        self.assertEqual(self.wx.call.call_args.args[0],'/cgi-bin/draft/add')
        self.assertFalse(first['receipt']['format_ui_verified'])
        self.assertEqual(self.d.draft(self.release,thumb_media_id='approved-cover')['status'],'already_confirmed')
        self.assertEqual(self.wx.call.call_count,1)
        self.article['blocks'][0]['text']='修订后的正文';self.release=self.make_release()
        self.assertEqual(self.d.draft(self.release,thumb_media_id='approved-cover')['status'],'update_requires_explicit_request')
        self.wx.call.return_value={'errcode':0,'errmsg':'ok'}
        self.assertEqual(self.d.draft(self.release,thumb_media_id='approved-cover',update_existing=True)['status'],'confirmed')
        self.assertEqual(self.wx.call.call_args.args[0],'/cgi-bin/draft/update')
        self.assertEqual(self.wx.call.call_args.args[2]['media_id'],'draft-id')
    def test_missing_wechat_receipt_remains_unknown(self):
        self.wx.call.return_value={}
        r=self.d.draft(self.release,thumb_media_id='approved-cover');self.assertEqual(r['status'],'needs_reconciliation')
        self.assertEqual(self.store.delivery(r['delivery_key'])['status'],'unknown')
        self.assertEqual(self.d.draft(self.release,thumb_media_id='approved-cover')['status'],'needs_reconciliation')
        self.assertEqual(self.wx.call.call_count,1)
    def test_approved_cover_upload(self):
        path=self.root/'cover.png';path.write_bytes(b'authorized image bytes')
        self.assertEqual(self.d.draft(self.release,cover_path=path,cover_authorized=True)['status'],'confirmed')
        self.wx.cover.assert_called_once()
    def test_api_allowlist_errors_and_safe_transport(self):
        request=Mock(return_value={'errcode':40001,'errmsg':'PRIVATE_TOKEN'});client=WeChatClient(request)
        with self.assertRaisesRegex(DeliveryError,'wechat_api_error_40001'):client.call('/cgi-bin/draft/add',{'access_token':'PRIVATE_TOKEN'},payload={})
        with self.assertRaises(DeliveryError):client.call('/cgi-bin/freepublish/submit',payload={})
        request.side_effect=RuntimeError('https://api.weixin.qq.com?access_token=PRIVATE_TOKEN')
        with self.assertRaises(DeliveryError) as caught:client.token(self.config['wechat'])
        self.assertNotIn('PRIVATE_TOKEN',str(caught.exception))
    def test_wechat_upload_inline_validates_receipt(self):
        client=WeChatClient(Mock(return_value={'url':'https://mmbiz.qpic.cn/test','errcode':0}))
        self.assertEqual(client.inline_image('PRIVATE_TOKEN','x.png',b'image'),'https://mmbiz.qpic.cn/test')
        with self.assertRaises(DeliveryError):client.inline_image('PRIVATE_TOKEN','x.exe',b'image')

    def test_draft_rollback_A_B_A_updates_current_draft(self):
        release_a=copy.deepcopy(self.release)
        self.d.draft(release_a,thumb_media_id='approved-cover')
        self.article['blocks'][0]['text']='版本B';self.release=self.make_release()
        self.wx.call.return_value={'errcode':0}
        self.d.draft(self.release,thumb_media_id='approved-cover',update_existing=True)
        self.article['blocks'][0]['text']='这是冻结后的文章正文。';release_a=self.make_release()
        result=self.d.draft(release_a,thumb_media_id='approved-cover',update_existing=True)
        self.assertEqual(result['status'],'confirmed');self.assertEqual(self.wx.call.call_count,3)
        self.assertEqual(self.wx.call.call_args.args[0],'/cgi-bin/draft/update')
        self.assertEqual(self.wx.call.call_args.args[2]['media_id'],'draft-id')
