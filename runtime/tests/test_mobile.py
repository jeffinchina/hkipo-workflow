import re
"""Offline author workflow acceptance: all external effects are fake."""
import base64
from email.message import EmailMessage
import hashlib
import html
from pathlib import Path
import tempfile
import unittest
from hkipo.delivery import DEFAULT_RECIPIENT, DeliveryError
from hkipo.mail_reply import MailReplyStore
from hkipo.mobile import MobileWorkflow
from hkipo.render import CTA, RISK

# Valid 1x1 PNG; intentionally synthetic, not a claimed allotment evidence image.
PNG=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6Hf0AAAAASUVORK5CYII=')


class FakeWeChat:
    def __init__(self):self.calls=[];self.fail_add=False
    def token(self,config):self.calls.append(('token',));return 'fake-token'
    def inline_image(self,token,name,data):
        self.calls.append(('image',name,hashlib.sha256(data).hexdigest()))
        return 'https://mmbiz.qpic.cn/'+name
    def call(self,path,query,payload):
        self.calls.append((path,payload))
        if path.endswith('/add'):
            if self.fail_add:raise TimeoutError('synthetic uncertainty after server create')
            return {'media_id':'draft-fixed'}
        if path.endswith('/update'):return {'errcode':0}
        raise AssertionError(path)
    def mutations(self):return [c for c in self.calls if c[0].startswith('/cgi-bin/draft/')]


class MobileTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.image=self.root/'allotment.png';self.image.write_bytes(PNG)
        self.store=MailReplyStore(self.root/'state.db',[DEFAULT_RECIPIENT])
        self.sent=[];self.wechat=FakeWeChat()
        self.config={'email':{'smtp_host':'smtp.example.test','smtp_port':465,'user':DEFAULT_RECIPIENT,'password':'fake'},'wechat':{'appid':'fake','appsecret':'fake'}}
        self.flow=MobileWorkflow(self.store,self.config,smtp_sender=self.smtp,wechat_client=self.wechat)
        self.event={'event_id':'issuer-episode-2026','stock_code':'00012','issuer':'样例公司','episode':'2026-11',
                    'source_url':'https://example.test/source','source_time':'2026-11-21T08:00:00+08:00',
                    'first_seen':'2026-11-21T08:01:00+08:00','brief':'测试简读，不代表真实分配。','source_reviewed':True,
                    'images':[{'path':str(self.image),'sha256':hashlib.sha256(PNG).hexdigest()}]}
        self.flow.register_event(self.event)
    def tearDown(self):self.temp.cleanup()
    def smtp(self,config,msg,to):
        with self.store.connect() as c:
            original=c.execute('SELECT raw,raw_hash FROM reply_reminder_originals WHERE message_id=?',(str(msg['Message-ID']),)).fetchone()
        self.assertEqual(bytes(original['raw']),msg.as_bytes())
        self.assertEqual(original['raw_hash'],hashlib.sha256(msg.as_bytes()).hexdigest())
        self.sent.append(msg)
        return {'smtp_accepted':True,'message_id':str(msg['Message-ID'])}
    def reply(self,body='我的 <原话> & 数字  100\n第二段',mid='one',tail=''):
        reminder=self.flow.send_reminder(self.event['event_id'],authorized=True)
        _,thread,_=self.flow.reminder(self.event['event_id'])
        msg=EmailMessage();msg['From']=DEFAULT_RECIPIENT;msg['Message-ID']=f'<{mid}@example.test>'
        msg['In-Reply-To']=thread;msg['References']=thread
        msg.set_content('标题: 作者标题\n正文:\n'+body+'\n【正文结束】'+tail)
        return self.store.ingest(msg.as_bytes(),authenticated_sender=DEFAULT_RECIPIENT)
    def process(self,row,**kwargs):
        directory=self.root/('preview-'+str(row.get('version')))
        if kwargs.pop('layout_approved',False):
            manifest=self.flow.preview(row,directory)
            kwargs['layout_approval']={'confirmed':True,**manifest}
        return self.flow.process(row['raw_hash'],directory,**kwargs)
    def test_reminder_and_reply_never_inherit_source_images(self):
        _,_,msg=self.flow.reminder(self.event['event_id'])
        self.assertFalse(any(p.get_content_maintype()=='image' for p in msg.walk()))
        row=self.reply();manifest=self.flow.preview(row,self.root/'no-source-images')
        self.assertEqual(manifest['source_images'],[])

    def test_new_event_kinds_text_only_and_reply_images(self):
        import json
        for kind in ('allotment_writing','allotment_notice','exception_notice'):
            event={**self.event,'event_id':kind,'notification_kind':kind,'images':[],'source_reviewed':False,'brief':'仅已确认事实'}
            self.flow.register_event(event)
            _,thread,msg=self.flow.reminder(kind)
            self.assertNotIn('券商先行',msg.get_body(preferencelist=('plain',)).get_content())
            self.store.register_reminder(thread,kind)
            reply=EmailMessage();reply['From']=DEFAULT_RECIPIENT;reply['Message-ID']=f'<{kind}@example.test>';reply['In-Reply-To']=thread
            reply.set_content('标题: 新稿\n正文:\n原文\n【正文结束】')
            row=self.store.ingest(reply.as_bytes(),authenticated_sender=DEFAULT_RECIPIENT)
            manifest=self.flow.preview(row,self.root/kind)
            self.assertEqual(manifest['source_images'],[])
            self.assertEqual((self.root/kind/'author.txt').read_text(),'原文')
            self.assertIn(manifest['word_archive'].split(':')[0],('complete','background_failed'))

    def test_reminder_exact_reply_to_and_registered_thread(self):
        key,mid,msg=self.flow.reminder(self.event['event_id'])
        self.assertEqual(str(msg['Reply-To']),DEFAULT_RECIPIENT)
        self.assertEqual(str(msg['Message-ID']),mid)
        self.assertEqual(msg.get_body(preferencelist=('plain',)).get_content().count('【正文结束】'),1)
        result=self.flow.send_reminder(self.event['event_id'],authorized=True)
        self.assertEqual(result['message_id'],mid)
        with self.store.connect() as c:self.assertEqual(c.execute('SELECT event_id FROM reply_reminders WHERE message_id=?',(mid,)).fetchone()[0],self.event['event_id'])
        with self.assertRaises(DeliveryError):self.flow.send_reminder(self.event['event_id'])
        self.assertEqual(len(self.sent),1)
    def test_smtp_unknown_persisted_no_resend_after_restart(self):
        def timeout(*args):self.sent.append('attempt');raise TimeoutError('synthetic-secret')
        self.flow.smtp_sender=timeout
        result=self.flow.send_reminder(self.event['event_id'],authorized=True)
        self.assertEqual(result['status'],'needs_reconciliation');self.assertNotIn('synthetic-secret',str(result))
        restarted=MobileWorkflow(self.store,self.config,smtp_sender=self.smtp,wechat_client=self.wechat)
        self.assertEqual(restarted.send_reminder(self.event['event_id'],authorized=True)['status'],'needs_reconciliation')
        self.assertEqual(len(self.sent),1)
        with self.store.connect() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM reply_reminders').fetchone()[0],0)
    def test_source_hash_changed_before_reminder_or_process_no_external_action(self):
        row=self.reply();before=len(self.sent)
        self.image.write_bytes(PNG+b'changed')
        with self.assertRaises(ValueError):self.flow.send_reminder(self.event['event_id'],authorized=True)
        with self.assertRaises(ValueError):self.process(row,thumb_media_id='cover',layout_approved=True)
        self.assertEqual(len(self.sent),before);self.assertEqual(self.wechat.calls,[])
    def test_original_author_archive_footer_once_and_local_preview(self):
        body='  我的 <原话> & 数字  100\n\n第二段'
        row=self.reply(body=body)
        directory=self.root/'preview';manifest=self.flow.preview(row,directory)
        self.assertEqual((directory/'author.txt').read_text(),body)
        rendered=(directory/'article.html').read_text()
        self.assertIn(html.escape('我的 <原话> & 数字  100'),rendered)
        self.assertEqual(re.sub(r'<[^>]*>', '', rendered).count(CTA),1);self.assertEqual(rendered.count(RISK),1)
        self.assertEqual(rendered.count('data-branding="personal-wechat-qr"'),1)
        self.assertEqual(manifest['author_sha256'],hashlib.sha256(body.encode()).hexdigest())
        self.assertFalse(manifest['requires_word']);self.assertEqual(self.wechat.calls,[])
    def test_author_existing_footer_is_single_but_archive_unchanged(self):
        for i,separator in enumerate(('\n\n','\n')):
            body='我的正文'+separator+CTA+separator+RISK
            row=self.reply(body=body,mid='footer-'+str(i));directory=self.root/('footer-'+str(i))
            self.flow.preview(row,directory)
            self.assertEqual((directory/'author.txt').read_text(),body)
            rendered=(directory/'article.html').read_text()
            self.assertEqual(re.sub(r'<[^>]*>', '', rendered).count(CTA),1);self.assertEqual(rendered.count(RISK),1)
    def test_needs_layout_or_cover_without_network(self):
        row=self.reply()
        self.assertEqual(self.process(row)['status'],'needs_layout_acceptance')
        self.assertEqual(self.process(row,layout_approved=True)['status'],'needs_cover')
        self.assertEqual(self.wechat.calls,[])
        self.assertEqual(self.store.pending()[0]['raw_hash'],row['raw_hash'])
    def test_first_add_then_versions_update_same_draft_and_a_b_a(self):
        a=self.reply(body='原稿A');result=self.process(a,thumb_media_id='cover',layout_approved=True)
        self.assertEqual(result['status'],'confirmed');self.assertEqual(result['receipt']['operation'],'add')
        self.assertEqual(self.process(a,thumb_media_id='cover',layout_approved=True)['status'],'not_pending')
        b=self.reply(body='新稿B',mid='two');result=self.process(b,thumb_media_id='cover',layout_approved=True)
        self.assertEqual(result['receipt']['operation'],'update');self.assertEqual(result['receipt']['draft_id'],'draft-fixed')
        a2=self.reply(body='原稿A',mid='three');self.assertEqual(a2['version'],3)
        result=self.process(a2,thumb_media_id='cover',layout_approved=True)
        self.assertEqual(result['receipt']['operation'],'update')
        mutations=self.wechat.mutations();self.assertEqual(len(mutations),3)
        self.assertTrue(mutations[0][0].endswith('/add'))
        self.assertTrue(all(m[1]['media_id']=='draft-fixed' for m in mutations[1:]))
        content=mutations[-1][1]['articles']['content']
        self.assertIn('原稿A',content);self.assertNotIn('hkipo-image:',content)
        self.assertNotIn('data:image/',content);self.assertEqual(re.sub(r'<[^>]*>', '', content).count(CTA),1)
    def test_unknown_timeout_blocks_newer_version_no_second_draft(self):
        self.wechat.fail_add=True;first=self.reply()
        self.assertEqual(self.process(first,thumb_media_id='cover',layout_approved=True)['status'],'needs_reconciliation')
        self.wechat.fail_add=False;second=self.reply(body='第二版',mid='two')
        count=len(self.wechat.calls)
        self.assertEqual(self.process(second,thumb_media_id='cover',layout_approved=True)['status'],'needs_reconciliation_or_superseded')
        self.assertEqual(len(self.wechat.calls),count);self.assertEqual(len(self.wechat.mutations()),1)
    def test_phone_quoted_reminder_template_end_marker_ignored(self):
        _,_,reminder=self.flow.reminder(self.event['event_id'])
        quoted='\n\nOn date sender wrote:\n'+reminder.get_body(preferencelist=('plain',)).get_content()
        row=self.reply(body='手机新写正文',tail=quoted)
        self.assertEqual(row['status'],'pending');self.assertEqual(row['body'],'手机新写正文')

    def test_layout_approval_must_bind_exact_current_author_and_html(self):
        row=self.reply();directory=self.root/'bound-preview'
        manifest=self.flow.preview(row,directory)
        fields=('raw_hash','version','html_sha256','source_event_sha256','author_sha256')
        for key in fields:
            approval={'confirmed':True,**manifest};approval[key]='stale-or-wrong'
            result=self.flow.process(row['raw_hash'],directory,thumb_media_id='cover',layout_approval=approval)
            self.assertEqual(result['status'],'needs_layout_acceptance',key)
        approval={'confirmed':False,**manifest}
        self.assertEqual(self.flow.process(row['raw_hash'],directory,thumb_media_id='cover',layout_approval=approval)['status'],'needs_layout_acceptance')
        self.assertEqual(self.wechat.calls,[])

    def test_confirmed_reminder_missing_binding_recovers_without_resend(self):
        first=self.flow.send_reminder(self.event['event_id'],authorized=True)
        self.assertEqual(first['status'],'confirmed')
        with self.store.connect() as c:
            c.execute('DELETE FROM reply_reminders WHERE message_id=?',(first['message_id'],))
        restarted=MobileWorkflow(self.store,self.config,smtp_sender=self.smtp,wechat_client=self.wechat)
        self.assertEqual(restarted.send_reminder(self.event['event_id'],authorized=True)['status'],'already_confirmed')
        self.assertEqual(len(self.sent),1)
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT event_id FROM reply_reminders WHERE message_id=?',(first['message_id'],)).fetchone()[0],self.event['event_id'])
    def test_author_exact_risk_heading_is_preserved(self):
        row=self.reply(body='风险提示');directory=self.root/'risk-heading'
        self.flow.preview(row,directory)
        self.assertEqual((directory/'author.txt').read_text(),'风险提示')
        rendered=(directory/'article.html').read_text()
        self.assertIn('>风险提示</p>',rendered)
        self.assertEqual(rendered.count(RISK),1)
        self.assertIn('作者标题\n\n风险提示\n\n', (directory/'article.txt').read_text())

    def test_receipt_only_complete_and_requires_explicit_authorization(self):
        row=self.reply();count=len(self.sent)
        self.assertEqual(self.flow.send_receipt(row['raw_hash'],authorized=True)['status'],'draft_not_confirmed')
        self.assertEqual(len(self.sent),count)
        with self.assertRaises(DeliveryError):self.flow.send_receipt(row['raw_hash'])
        self.assertEqual(self.wechat.calls,[])
    def test_receipt_thread_registered_then_next_reply_updates_same_draft(self):
        first=self.reply();self.process(first,thumb_media_id='cover',layout_approved=True)
        result=self.flow.send_receipt(first['raw_hash'],authorized=True)
        self.assertEqual(result['status'],'confirmed');receipt=self.sent[-1]
        self.assertEqual(str(receipt['In-Reply-To']),first['message_id'])
        self.assertIn(first['message_id'],str(receipt['References']))
        _,root,_=self.flow.reminder(self.event['event_id'])
        self.assertIn(root,str(receipt['References']))
        self.assertEqual(str(receipt['Reply-To']),DEFAULT_RECIPIENT)
        msg=EmailMessage();msg['From']=DEFAULT_RECIPIENT;msg['Message-ID']='<after-status@example.test>'
        msg['In-Reply-To']=str(receipt['Message-ID'])
        msg['References']=str(receipt['References'])+' '+str(receipt['Message-ID'])
        msg.set_content('标题: 回执后修改\n正文:\n第二稿\n【正文结束】')
        second=self.store.ingest(msg.as_bytes(),authenticated_sender=DEFAULT_RECIPIENT)
        self.assertEqual(second['event_id'],first['event_id'])
        result=self.process(second,thumb_media_id='cover',layout_approved=True)
        self.assertEqual(result['receipt']['operation'],'update');self.assertEqual(result['receipt']['draft_id'],'draft-fixed')
        self.assertTrue(all('publish' not in call[0] for call in self.wechat.calls))
        count=len(self.sent)
        self.assertEqual(self.flow.send_receipt(first['raw_hash'],authorized=True)['status'],'already_confirmed')
        self.assertEqual(len(self.sent),count)
    def test_receipt_smtp_unknown_never_resends(self):
        row=self.reply();self.process(row,thumb_media_id='cover',layout_approved=True)
        attempts=[]
        def timeout(*args):attempts.append(1);raise TimeoutError('secret must stay private')
        self.flow.smtp_sender=timeout
        result=self.flow.send_receipt(row['raw_hash'],authorized=True)
        self.assertEqual(result,{'status':'needs_reconciliation'})
        restarted=MobileWorkflow(self.store,self.config,smtp_sender=timeout,wechat_client=self.wechat)
        self.assertEqual(restarted.send_receipt(row['raw_hash'],authorized=True)['status'],'needs_reconciliation')
        self.assertEqual(len(attempts),1)
