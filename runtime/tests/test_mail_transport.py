"""Synthetic mailbox only: no sockets, credentials, or private mailbox access."""
from email.message import EmailMessage
from email import policy
from email.parser import BytesParser
from pathlib import Path
import tempfile
import unittest
from hkipo.mail_reply import MailReplyStore
from hkipo.mail_transport import poll_replies, IntakeError

SENDER='author@example.test'
REMINDER='<reminder@example.test>'


def message(mid='<reply@example.test>',thread=REMINDER,body='作者原文',sender=SENDER):
    msg=EmailMessage();msg['From']=sender;msg['Message-ID']=mid
    msg['In-Reply-To']=thread;msg['References']=thread
    msg['Authentication-Results']='attacker.invalid; dkim=pass'
    msg.set_content('标题: 手机标题\n正文:\n'+body+'\n【正文结束】\n> 原提醒')
    return msg.as_bytes()


class FakeIMAP:
    def __init__(self,inbox=None,sent=None,sizes=None):
        self.messages={'INBOX':inbox or {},'Sent Messages':sent or {}}
        self.sizes=sizes or {};self.calls=[];self.folder=None;self.logged_out=False
    def login(self,*args):self.calls.append(('login',));return 'OK',[]
    def select(self,folder,readonly=False):
        assert readonly is True
        self.folder=folder.strip('"');self.calls.append(('select',self.folder,readonly));return 'OK',[]
    def logout(self):self.logged_out=True
    def uid(self,command,*args):
        self.calls.append((command,self.folder,*args))
        if command=='SEARCH':
            # Deliberately overmatch, as IMAP HEADER searches are substring based.
            return 'OK',[b' '.join(self.messages[self.folder])]
        uid,fields=args;raw=self.messages[self.folder][uid]
        if 'HEADER.FIELDS' in fields:
            msg=BytesParser(policy=policy.default).parsebytes(raw)
            head=''.join(f'{key}: {msg[key]}\r\n' for key in ('From','To','Reply-To','Subject','Message-ID','In-Reply-To','References') if msg[key])+'\r\n'
            size=self.sizes.get((self.folder,uid),len(raw))
            return 'OK',[(f'1 (RFC822.SIZE {size} BODY[HEADER.FIELDS]'.encode(),head.encode()),b')']
        assert fields=='(BODY.PEEK[])'
        return 'OK',[(b'1 (BODY[]',raw),b')']
    def body_calls(self,folder=None):
        return [c for c in self.calls if c[0]=='FETCH' and c[-1]=='(BODY.PEEK[])' and (folder is None or c[1]==folder)]


class MailTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=MailReplyStore(Path(self.temp.name)/'state.db',[SENDER])
        self.store.register_reminder(REMINDER,'event-1')
        self.config={'user':SENDER,'password':'synthetic-secret'}
    def tearDown(self):self.temp.cleanup()
    def poll(self,client,**kwargs):return poll_replies(self.store,self.config,client_factory=lambda:client,**kwargs)
    def test_same_text_cannot_authenticate_replaced_image(self):
        msg=BytesParser(policy=policy.default).parsebytes(message())
        msg.add_attachment(b'\x89PNG\r\n\x1a\noriginal',maintype='image',subtype='png',filename='phone.png')
        original=msg.as_bytes()
        msg.get_payload()[-1].set_payload('aW5qZWN0ZWQ=')
        result=self.poll(FakeIMAP({b'1':msg.as_bytes()},{b'2':original}))
        self.assertEqual(result['results'][0]['reason'],'sent_copy_not_verified')
        self.assertEqual(self.store.pending(),[])

    def test_valid_same_account_sent_proof_and_repoll_no_body_fetch(self):
        raw=message();client=FakeIMAP({b'1':raw},{b'2':raw})
        result=self.poll(client);self.assertEqual(result['results'][0]['status'],'pending')
        self.assertTrue(client.logged_out);self.assertEqual(len(client.body_calls()),2)
        again=FakeIMAP({b'1':raw},{b'2':raw});self.poll(again)
        self.assertEqual(again.body_calls(),[])
    def test_unregistered_does_not_even_connect(self):
        empty=MailReplyStore(Path(self.temp.name)/'empty.db',[SENDER])
        def fail():raise AssertionError('must not connect')
        self.assertEqual(poll_replies(empty,self.config,client_factory=fail)['status'],'idle')
    def test_spoofed_from_and_unregistered_thread_never_read_body(self):
        for raw in [message(sender='evil@example.test'),message(thread='<reminder@example.test.evil.invalid>')]:
            client=FakeIMAP({b'1':raw},{b'2':raw});self.poll(client)
            self.assertEqual(client.body_calls(),[]);self.assertEqual(self.store.pending(),[])
    def test_authentication_header_cannot_replace_sent_copy(self):
        client=FakeIMAP({b'1':message()})
        self.assertEqual(self.poll(client)['results'][0]['status'],'deferred')
        self.assertEqual(self.store.pending(),[])
    def test_same_message_id_wrong_thread_or_body_fails_proof(self):
        for mirror in [message(thread='<other@example.test>'),message(body='篡改正文'),message(sender='evil@example.test')]:
            client=FakeIMAP({b'1':message()},{b'2':mirror})
            self.assertEqual(self.poll(client)['results'][0]['status'],'deferred')
            self.assertEqual(self.store.pending(),[])
    def test_inbox_and_sent_size_limits_before_body_fetch(self):
        client=FakeIMAP({b'1':message()},{b'2':message()},{('INBOX',b'1'):self.store.max_bytes+1})
        self.assertEqual(self.poll(client)['results'][0]['reason'],'message_too_large');self.assertEqual(client.body_calls(),[])
        client=FakeIMAP({b'1':message()},{b'2':message()},{('Sent Messages',b'2'):self.store.max_bytes+1})
        self.assertEqual(self.poll(client)['results'][0]['status'],'deferred');self.assertEqual(client.body_calls('Sent Messages'),[])
    def test_candidate_limit_and_ambiguous_events_no_body_fetch(self):
        client=FakeIMAP({b'1':message(),b'2':message(mid='<two@example.test>')})
        result=self.poll(client,max_messages=1)
        self.assertTrue(result['has_more']);self.assertEqual(len(client.body_calls('INBOX')),1)
        self.store.register_reminder('<other@example.test>','event-2')
        client=FakeIMAP({b'1':message(thread=REMINDER+' <other@example.test>')})
        self.poll(client);self.assertEqual(client.body_calls(),[])
    def test_custom_failures_are_sanitized_and_logout(self):
        class Broken(FakeIMAP):
            def uid(self,*args):raise IntakeError('synthetic-secret leaked password')
        client=Broken()
        with self.assertRaisesRegex(IntakeError,'^imap_intake_failed$'):self.poll(client)
        self.assertTrue(client.logged_out)
        def broken_factory():raise RuntimeError('synthetic-secret')
        with self.assertRaisesRegex(IntakeError,'^imap_intake_failed$'):
            poll_replies(self.store,self.config,client_factory=broken_factory)
    def test_return_no_private_body_or_credentials(self):
        raw=message(body='private author phrase');result=self.poll(FakeIMAP({b'1':raw},{b'2':raw}))
        self.assertNotIn('private author phrase',str(result));self.assertNotIn('synthetic-secret',str(result))

    def test_sent_substring_message_id_match_never_reads_mirror_body(self):
        client=FakeIMAP({b'1':message()},{b'2':message(mid='<reply@example.test.evil.invalid>')})
        self.assertEqual(self.poll(client)['results'][0]['status'],'deferred')
        self.assertEqual(client.body_calls('Sent Messages'),[])

    def test_known_history_does_not_starve_new_body_budget(self):
        old=message();self.store.ingest(old,authenticated_sender=SENDER)
        new=message(mid='<new@example.test>',body='新稿')
        client=FakeIMAP({b'1':old,b'2':new},{b'3':new})
        result=self.poll(client,max_messages=1)
        self.assertFalse(result['has_more'])
        self.assertEqual(result['results'][0]['status'],'pending')
        self.assertEqual(len(client.body_calls('INBOX')),1)
        self.assertEqual(client.body_calls('INBOX')[0][2],b'2')

    def test_deferred_page_rotates_after_restart(self):
        first=message();second=message(mid='<second@example.test>',body='第二稿')
        client=FakeIMAP({b'1':first,b'2':second})
        self.assertTrue(self.poll(client,max_messages=1)['has_more'])
        restored=MailReplyStore(Path(self.temp.name)/'state.db',[SENDER])
        following=FakeIMAP({b'1':first,b'2':second},{b'3':second})
        result=poll_replies(restored,self.config,client_factory=lambda:following,max_messages=1)
        self.assertEqual(result['results'][0]['status'],'pending')
        self.assertEqual(following.body_calls('INBOX')[0][2],b'2')
        # The remaining deferred item is still retried on the next wrapped page.
        last=FakeIMAP({b'1':first,b'2':second},{b'3':first})
        result=self.poll(last,max_messages=1)
        self.assertFalse(result['has_more'])
        self.assertEqual(result['results'][0]['status'],'pending')
        self.assertEqual(last.body_calls('INBOX')[0][2],b'1')

    def original(self, mid=REMINDER):
        msg=EmailMessage();msg['From']=SENDER;msg['To']=SENDER;msg['Reply-To']=SENDER
        msg['Subject']='【提醒】测试股票';msg['Message-ID']=mid
        msg.set_content('原始提醒正文，不能只匹配标题')
        msg.add_alternative('<p>原始提醒 HTML</p>',subtype='html')
        msg.add_attachment(b'original attachment',maintype='application',subtype='octet-stream',filename='source.bin')
        return msg

    def test_rewritten_reminder_alias_persists_and_preserves_sent_proof(self):
        original=self.original();self.store.remember_reminder(original.as_bytes(),'event-1')
        original.replace_header('Message-ID','<qq-rewritten@example.test>')
        reply=message(thread='<qq-rewritten@example.test>')
        client=FakeIMAP({b'1':reply},{b'2':original.as_bytes(),b'3':reply})
        result=self.poll(client)
        self.assertEqual(result['results'][0]['status'],'pending')
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT event_id FROM reply_reminders WHERE message_id=?',('<qq-rewritten@example.test>',)).fetchone()[0],'event-1')
        restored=MailReplyStore(self.store.dbpath,[SENDER])
        again=FakeIMAP({b'1':reply},{b'2':original.as_bytes(),b'3':reply})
        poll_replies(restored,self.config,client_factory=lambda:again)
        self.assertEqual(again.body_calls(),[])

    def test_alias_requires_exact_all_body_parts_and_attachments(self):
        for mutation in ('plain','html','attachment','subject','sender'):
            original=self.original();self.store.remember_reminder(original.as_bytes(),'event-1')
            original.replace_header('Message-ID','<qq-rewritten@example.test>')
            if mutation=='plain':original.get_payload()[0].get_payload()[0].set_content('different body')
            elif mutation=='html':original.get_payload()[0].get_payload()[1].set_content('<p>different</p>',subtype='html')
            elif mutation=='attachment':original.get_payload()[1].set_payload('cmVwbGFjZWQ=')
            elif mutation=='subject':original.replace_header('Subject','different subject')
            else:original.replace_header('From','evil@example.test')
            client=FakeIMAP({b'1':message(thread='<qq-rewritten@example.test>')},{b'2':original.as_bytes()})
            self.poll(client)
            self.assertEqual(client.body_calls('INBOX'),[])
            with self.store.connect() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM reply_reminder_aliases').fetchone()[0],0)
            if mutation in ('subject','sender'):self.assertEqual(client.body_calls('Sent Messages'),[])

    def test_alias_requires_original_and_confirmed_root_and_bounded_search(self):
        original=self.original('<unconfirmed@example.test>')
        self.store.remember_reminder(original.as_bytes(),'event-1')
        client=FakeIMAP(sent={b'2':original.as_bytes()});self.poll(client)
        self.assertEqual(client.body_calls(),[])
        self.store.remember_reminder(self.original().as_bytes(),'event-1')
        client=FakeIMAP(sent={str(i).encode():self.original().as_bytes() for i in range(11)})
        self.poll(client);self.assertEqual(client.body_calls(),[])
        sent_search=[c for c in client.calls if c[0]=='SEARCH' and c[1]=='Sent Messages'][0]
        self.assertIn('SINCE',sent_search);self.assertIn('BEFORE',sent_search);self.assertIn('Subject',sent_search)

    def test_alias_collision_never_rebinds_other_event(self):
        original=self.original();self.store.remember_reminder(original.as_bytes(),'event-1')
        original.replace_header('Message-ID','<other-event@example.test>')
        self.store.register_reminder('<other-event@example.test>','event-2')
        self.poll(FakeIMAP(sent={b'1':original.as_bytes()}))
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT event_id FROM reply_reminders WHERE message_id=?',('<other-event@example.test>',)).fetchone()[0],'event-2')
            self.assertEqual(c.execute('SELECT COUNT(*) FROM reply_reminder_aliases').fetchone()[0],0)

    def test_sent_overmatching_search_exact_headers_select_unique_proof(self):
        raw=message();unrelated=message(mid='<different@example.test>')
        result=self.poll(FakeIMAP({b'1':raw},{b'2':unrelated,b'3':raw}))
        self.assertEqual(result['results'][0]['status'],'pending')

    def test_alias_search_overmatch_filters_headers_before_body_budget(self):
        original=self.original();self.store.remember_reminder(original.as_bytes(),'event-1')
        original.replace_header('Message-ID','<rewritten@example.test>')
        unrelated=message(mid='<unrelated@example.test>')
        sent={str(i).encode():unrelated for i in range(50)}
        sent[b'100']=original.as_bytes()
        client=FakeIMAP(sent=sent);self.poll(client)
        self.assertEqual(len(client.body_calls('Sent Messages')),1)
        self.assertEqual(client.body_calls('Sent Messages')[0][2],b'100')
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT event_id FROM reply_reminders WHERE message_id=?',('<rewritten@example.test>',)).fetchone()[0],'event-1')
