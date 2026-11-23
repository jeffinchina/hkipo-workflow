import concurrent.futures
from email.message import EmailMessage
from pathlib import Path
import tempfile
import unittest
from hkipo.mail_reply import MailReplyStore


def mail(mid='one',text='标题: 原样标题\n正文:\n  作者的  原文\n第二段\n【正文结束】\nSent from my iPhone\n> old reminder',sender='me@example.test',refs='<reminder@example.test>'):
    msg=EmailMessage();msg['From']=sender;msg['To']='intake@example.test'
    msg['Subject']='Re: subject is never used';msg['Message-ID']=f'<{mid}@example.test>'
    if refs:msg['In-Reply-To']=refs
    msg.set_content(text);return msg


class MailReplyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'state.db'
        self.store=MailReplyStore(self.path,['me@example.test'])
        self.store.register_reminder('<reminder@example.test>','stock-issue-2026')
    def tearDown(self):self.temp.cleanup()
    def ingest(self,msg,**kw):return self.store.ingest(msg.as_bytes(),authenticated_sender=kw.get('sender','me@example.test'))
    def test_optional_images_versioned_private_and_inline_ignored(self):
        import json, hashlib
        data=b'\x89PNG\r\n\x1a\noriginal'
        msg=mail();msg.add_attachment(data,maintype='image',subtype='png',filename='phone.png')
        row=self.ingest(msg);self.assertEqual(row['status'],'pending')
        image=json.loads(row['attachments_json'])[0]
        self.assertEqual(Path(image['path']).read_bytes(),data)
        self.assertEqual(image['sha256'],hashlib.sha256(data).hexdigest())
        self.assertEqual(Path(image['path']).stat().st_mode & 0o777,0o600)
        changed=mail(mid='two');changed.add_attachment(data+b'2',maintype='image',subtype='png',filename='phone.png')
        self.assertEqual(self.ingest(changed)['version'],2)
        inline=mail(mid='three');inline.add_attachment(data,maintype='image',subtype='png',filename='old.png',disposition='inline')
        self.assertEqual(json.loads(self.ingest(inline)['attachments_json']),[])

    def test_accepts_image_between_one_and_five_mb(self):
        msg=mail();msg.add_attachment(b'\x89PNG\r\n\x1a\n'+b'x'*2_000_000,maintype='image',subtype='png',filename='phone.png')
        self.assertEqual(self.ingest(msg)['status'],'pending')

    def test_oversized_image_requests_resize_without_pending_work(self):
        msg=mail();msg.add_attachment(b'\x89PNG\r\n\x1a\n'+b'x'*5_000_000,maintype='image',subtype='png',filename='large.png')
        row=self.ingest(msg)
        self.assertEqual(row['reason'],'needs_image_resize');self.assertEqual(self.store.pending(),[])

    def test_preserves_exact_author_and_ignores_tail(self):
        row=self.ingest(mail());self.assertEqual(row['status'],'pending')
        self.assertEqual(row['body'],'  作者的  原文\n第二段');self.assertEqual(row['version'],1)
        self.assertEqual(row['raw'],mail().as_bytes())
    def test_not_subject_guess_and_no_spoofed_from(self):
        self.assertEqual(self.ingest(mail(refs=''))['reason'],'unmatched_or_ambiguous_thread')
        self.assertEqual(self.ingest(mail(mid='two'),sender='attacker@example.test')['status'],'rejected')
        self.assertEqual(self.store.ingest(mail(mid='three').as_bytes())['status'],'rejected')
        self.assertEqual(self.store.pending(),[])
    def test_exact_thread_and_ambiguous_events(self):
        self.store.register_reminder('<other@example.test>','other-issue')
        self.assertEqual(self.ingest(mail(refs='<reminder@example.test.evil.invalid>'))['status'],'rejected')
        self.assertEqual(self.ingest(mail(mid='two',refs='<reminder@example.test> <other@example.test>'))['status'],'rejected')
        with self.assertRaises(ValueError):self.store.register_reminder('<reminder@example.test>','other')
    def test_ambiguous_text_does_not_guess_or_strip(self):
        for i,text in enumerate(['普通标题\n普通正文','标题: abc\n正文:\n正文无终止','标题: abc\n正文:\n> old\n【正文结束】']):
            self.assertEqual(self.ingest(mail(mid=str(i),text=text))['status'],'needs_clarification')
        self.assertEqual(self.store.pending(),[])
    def test_attachments_not_interpreted_plain_preferred(self):
        msg=mail();msg.add_alternative('<p>标题: malicious</p><p>正文:</p><p>bad</p><p>【正文结束】</p>',subtype='html')
        msg.add_attachment(b'execute something',maintype='application',subtype='octet-stream',filename='x.sh')
        row=self.ingest(msg);self.assertEqual(row['title'],'原样标题')
        attached=mail(mid='attached');attached.set_content('missing explicit input')
        attached.add_attachment(mail(),filename='old.eml')
        self.assertEqual(self.ingest(attached)['status'],'needs_clarification')
    def test_html_fallback_no_script_or_quote(self):
        msg=mail();msg.set_content('<div>标题: html</div><div>正文:</div><div>正文</div><div>【正文结束】</div><script>bad</script><blockquote>old</blockquote>',subtype='html')
        row=self.ingest(msg);self.assertEqual(row['status'],'pending');self.assertIn('正文',row['body']);self.assertNotIn('bad',row['body'])
    def test_size_limit(self):
        store=MailReplyStore(self.path,['me@example.test'],max_bytes=20)
        self.assertEqual(store.ingest(b'x'*21)['reason'],'message_too_large');self.assertEqual(store.pending(),[])
    def test_restart_dedupe_collision_and_versions_update_same_draft(self):
        first=self.ingest(mail());self.assertTrue(self.ingest(mail())['duplicate'])
        self.assertEqual(self.ingest(mail(text='标题: changed\n正文:\nchanged\n【正文结束】'))['reason'],'message_id_collision')
        work=self.store.reserve(first['raw_hash']);self.assertIsNone(work['draft_id'])
        self.assertIsNone(self.store.reserve(first['raw_hash']))
        second=self.ingest(mail(mid='two',text='标题: 新版本\n正文:\n新作者正文\n【正文结束】'))
        self.assertIsNone(self.store.reserve(second['raw_hash']))
        self.assertTrue(self.store.complete(first['raw_hash'],'draft-1'))
        restored=MailReplyStore(self.path,['me@example.test'])
        work=restored.reserve(second['raw_hash']);self.assertEqual(work['draft_id'],'draft-1');self.assertEqual(work['version'],2)
        with self.assertRaises(ValueError):restored.complete(second['raw_hash'],'draft-2')
        self.assertTrue(restored.complete(second['raw_hash'],'draft-1'))
        self.assertTrue(self.ingest(mail(mid='third',text='标题: 新版本\n正文:\n新作者正文\n【正文结束】'))['duplicate'])
        self.assertEqual(restored.pending(),[])
    def test_concurrent_same_input_one_pending(self):
        raw=mail().as_bytes()
        def run(_):return self.store.ingest(raw,authenticated_sender='me@example.test')
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:rows=list(pool.map(run,range(10)))
        self.assertEqual(sum(not r.get('duplicate',False) for r in rows),1);self.assertEqual(len(self.store.pending()),1)
    def test_new_version_supersedes_only_unstarted(self):
        self.ingest(mail());self.ingest(mail(mid='two',text='标题: second\n正文:\nnew\n【正文结束】'))
        self.assertEqual(len(self.store.pending()),1);self.assertEqual(self.store.pending()[0]['version'],2)

    def test_return_to_old_content_is_new_version(self):
        first=self.ingest(mail())
        self.ingest(mail(mid='two',text='标题: second\n正文:\nnew\n【正文结束】'))
        back=self.ingest(mail(mid='three'))
        self.assertEqual(back['version'],3);self.assertEqual(back['body'],first['body'])
        same=self.ingest(mail(mid='four'))
        self.assertTrue(same['duplicate']);self.assertEqual(same['message_id'],'<four@example.test>')

    def test_html_quoted_template_is_not_a_new_submission(self):
        msg=mail();msg.set_content('<blockquote><div>标题: 旧标题</div><div>正文:</div><div>旧正文</div><div>【正文结束】</div></blockquote>',subtype='html')
        row=self.ingest(msg);self.assertEqual(row['status'],'needs_clarification')
        self.assertEqual(self.store.pending(),[])
    def test_html_author_blockquote_requires_clarification_never_silent_loss(self):
        msg=mail();msg.set_content('<div>标题: 新标题</div><div>正文:</div><div>我的观点</div><blockquote>作者有意引用的重要文字</blockquote><div>我的结论</div><div>【正文结束】</div>',subtype='html')
        row=self.ingest(msg);self.assertEqual(row['status'],'needs_clarification')
        self.assertEqual(row['reason'],'quoted_history_inside_body')
        self.assertEqual(row['raw'],msg.as_bytes())

    def test_iphone_collapsed_labels_and_inline_end_keep_author_text(self):
        row=self.ingest(mail(text='标题：手机标题 正文：\n  原文\n最后一句 【正文结束】\n尾部签名'))
        self.assertEqual(row['status'],'pending')
        self.assertEqual(row['title'],'手机标题')
        self.assertEqual(row['body'],'  原文\n最后一句 ')

    def test_reparse_clarification_retains_raw_and_audits_previous_reason(self):
        from unittest.mock import patch
        with patch('hkipo.mail_reply._submission',side_effect=ValueError('missing_body_marker')):
            old=self.ingest(mail(text='标题：手机标题 正文：\n原文 【正文结束】'))
        with self.assertRaises(ValueError):self.store.reparse_clarification(old['raw_hash'])
        restored=MailReplyStore(self.path,['me@example.test'])
        row=restored.reparse_clarification(old['raw_hash'],authenticated_sender='me@example.test')
        self.assertEqual(row['status'],'pending');self.assertEqual(row['raw'],old['raw'])
        self.assertEqual(row['message_id'],old['message_id']);self.assertEqual(row['event_id'],old['event_id'])
        with restored.connect() as c:
            audit=c.execute('SELECT previous_reason,new_status FROM reply_reparse_audit').fetchone()
            self.assertEqual(tuple(audit),('missing_body_marker','pending'))
        restored.reserve(row['raw_hash'])
        with self.assertRaises(ValueError):restored.reparse_clarification(row['raw_hash'],authenticated_sender='me@example.test')
        restored.complete(row['raw_hash'],'draft-1')
        with self.assertRaises(ValueError):restored.reparse_clarification(row['raw_hash'],authenticated_sender='me@example.test')

    def test_reparse_rechecks_event_and_rejects_untrusted_rows(self):
        old=self.ingest(mail(text='invalid template'))
        with self.store.connect() as c:c.execute('UPDATE reply_reminders SET event_id=?',('wrong-event',))
        with self.assertRaisesRegex(ValueError,'thread association'):self.store.reparse_clarification(old['raw_hash'],authenticated_sender='me@example.test')
        rejected=self.store.ingest(mail(mid='untrusted').as_bytes())
        with self.assertRaises(ValueError):self.store.reparse_clarification(rejected['raw_hash'],authenticated_sender='me@example.test')
