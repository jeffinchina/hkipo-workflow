import tempfile
import unittest
from pathlib import Path
from datetime import datetime
from hkipo.mobile import MobileWorkflow
from hkipo.mail_reply import MailReplyStore
from hkipo.delivery import DEFAULT_RECIPIENT
from hkipo.reminders import ReminderStore, HK
from hkipo.notices import NoticeBridge, make_official_event

class Calendar:
    def next_business_day(self, day):
        from datetime import timedelta
        return day + timedelta(days=1)

class NoticesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); root = Path(self.tmp.name)
        self.sent = []
        self.replies = MailReplyStore(root/'reply.db', [DEFAULT_RECIPIENT])
        cfg = {'email':dict(user=DEFAULT_RECIPIENT,password='secret',smtp_host='fake',smtp_port=465)}
        self.flow = MobileWorkflow(self.replies, cfg, smtp_sender=self.smtp)
        self.store = ReminderStore(root/'calendar.db', Calendar())
        self.bridge = NoticeBridge(self.flow,self.store)
        self.store.schedule('00012:2026', '2026-11-19', source_hash='abc', evidence={'source_url':'https://www1.hkexnews.hk/a.pdf'},stock_code='00012',stock_name='例子',now=datetime(2026,11,18,tzinfo=HK))
    def tearDown(self): self.tmp.cleanup()
    def smtp(self,cfg,msg,to):
        with self.replies.connect() as c:
            original=c.execute('SELECT raw FROM reply_reminder_originals WHERE message_id=?',(str(msg['Message-ID']),)).fetchone()
        self.assertEqual(bytes(original['raw']),msg.as_bytes())
        self.sent.append(msg)
        return dict(smtp_accepted=True,message_id=str(msg['Message-ID']))
    def test_two_slots_share_event_and_reboot_no_resend(self):
        self.bridge.send_due(datetime(2026,11,20,18,tzinfo=HK))
        self.assertEqual(len(self.sent),2)
        self.assertNotEqual(self.sent[0]['Message-ID'],self.sent[1]['Message-ID'])
        with self.replies.connect() as c:
            self.assertEqual(c.execute('SELECT count(DISTINCT event_id) FROM reply_reminders').fetchone()[0],1)
        NoticeBridge(self.flow,self.store).send_due(datetime(2026,11,20,19,tzinfo=HK))
        self.assertEqual(len(self.sent),2)
    def test_crash_after_confirmed_repairs_calendar_and_thread(self):
        self.bridge.send_due(datetime(2026,11,20,17,tzinfo=HK))
        with self.store._db() as c: c.execute("UPDATE writing_reminders SET status='unknown' WHERE slot='16:30'")
        with self.replies.connect() as c: c.execute('DELETE FROM reply_reminders')
        self.bridge.send_due(datetime(2026,11,20,17,tzinfo=HK))
        self.assertEqual(len(self.sent),1)
        self.assertEqual(self.store.rows()[0]['status'],'confirmed')
        with self.replies.connect() as c: self.assertEqual(c.execute('SELECT count(*) FROM reply_reminders').fetchone()[0],1)
    def test_unknown_never_retries_or_leaks(self):
        def fail(*args): raise RuntimeError('secret')
        self.flow.smtp_sender=fail
        result=self.bridge.send_due(datetime(2026,11,20,17,tzinfo=HK))
        self.assertNotIn('secret',str(result))
        self.flow.smtp_sender=self.smtp
        self.bridge.send_due(datetime(2026,11,20,17,tzinfo=HK))
        self.assertEqual(self.sent,[])
    def test_withdrawn_cancels_pending(self):
        self.store.schedule('00012:2026','2026-11-19',source_hash='b',evidence={'quote_sha256':'b'},status='withdrawn',now=datetime(2026,11,19,tzinfo=HK))
        self.assertEqual(self.bridge.send_due(datetime(2026,11,20,18,tzinfo=HK)),[])
    def test_changed_source_after_first_slot_preserves_shared_event(self):
        self.bridge.send_due(datetime(2026,11,20,17,tzinfo=HK))
        self.store.schedule('00012:2026','2026-11-19',source_hash='new',evidence={'source_url':'https://www1.hkexnews.hk/new.pdf'},stock_code='00012',stock_name='例子',now=datetime(2026,11,20,17,tzinfo=HK))
        self.bridge.send_due(datetime(2026,11,20,18,tzinfo=HK))
        self.assertEqual(len(self.sent),2)
        with self.replies.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM mobile_events').fetchone()[0],1)
    def test_delayed_cancels_pending(self):
        self.store.schedule('00012:2026','2026-11-19',source_hash='b',evidence={'quote_sha256':'b'},status='delayed',now=datetime(2026,11,19,tzinfo=HK))
        self.assertEqual(self.bridge.send_due(datetime(2026,11,20,18,tzinfo=HK)),[])
    def test_official_categories_and_guard(self):
        d=dict(kind='allotment',url='https://www1.hkexnews.hk/a.pdf',title='全球發售 配發結果',stock_code='12',stock_name='例子',published_at='2026-11-20',discovered_at='2026-11-20')
        a=make_official_event(d,source_hash='a')
        b=make_official_event(dict(d,kind='exception'),source_hash='a')
        self.assertNotEqual(a['event_id'],b['event_id'])
        self.assertEqual(a['images'],[])
        self.assertNotIn('中签率',a['brief'])
        for changes in ({'url':'https://example.test/a.pdf'},{'title':'配售现有股份'}):
            with self.assertRaises(ValueError): make_official_event(dict(d,**changes),source_hash='a')

if __name__=='__main__': unittest.main()
