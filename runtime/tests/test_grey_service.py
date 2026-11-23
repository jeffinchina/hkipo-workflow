from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from hkipo.grey_service import GreyService, GreySettings
from hkipo.grey_store import GreyStore
from hkipo.mobile import MobileWorkflow
from hkipo.mail_reply import MailReplyStore
from hkipo.delivery import DEFAULT_RECIPIENT
from hkipo.store import StateStore
from test_grey_market import snapshot

def at(value):return datetime.fromisoformat('2026-11-23T'+value+':00+08:00')

def session():
    s=snapshot()
    return dict({k:s[k] for k in ('provider','market','stock_code','issuer','trade_date','listing_date',
                             'session_start','session_end','schedule_source_url')},episode_id='episode1')


class GreyServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.state=StateStore(self.root/'official.db')
        with self.state.connect() as c:
            self.state._worker_schema(c)
            c.execute("INSERT INTO sources(source,last_success,failure_count) VALUES('title-search',?,0)",(at('18:40').isoformat(),))
        self.sent=[];self.smtp_fails=False;self.reads=[]
        replies=MailReplyStore(self.root/'reply.db',[DEFAULT_RECIPIENT])
        cfg={'email':{'user':DEFAULT_RECIPIENT,'password':'fake','smtp_host':'smtp.example.invalid','smtp_port':465}}
        def smtp(cfg,msg,to):
            self.sent.append(msg)
            if self.smtp_fails:raise RuntimeError('private password must never leak')
            return {'smtp_accepted':True,'message_id':str(msg['Message-ID'])}
        self.flow=MobileWorkflow(replies,cfg,smtp_sender=smtp)
        self.settings=GreySettings(str(self.root/'grey'),str(self.root/'official.db'),str(self.root/'research'),
                                   enabled=True,provider='测试券商')
        def collect(s):
            self.reads.append(s)
            return snapshot()
        self.service=GreyService(self.settings,self.flow,calendar_fetcher=lambda:[session()],collector=collect,clock=lambda:at('18:40'))

    def test_preclose_never_reads_prices_and_1840_is_fixed(self):
        self.service.cycle(at('16:00'))
        self.assertEqual(self.reads,[]);self.assertEqual(self.sent,[])
        self.assertEqual(self.service.store.rows()[0]['due_at'],'2026-11-23T18:40:00+08:00')
        self.service.cycle(at('18:31'))
        self.assertEqual(len(self.reads),1);self.assertEqual(self.sent,[])
        self.service.cycle(at('18:39'))
        self.assertEqual(self.sent,[])
        self.service.cycle(at('18:40'))
        self.assertEqual(len(self.sent),1)
        self.assertIn('【暗盘分析】',str(self.sent[0]['Subject']))
        text=self.sent[0].get_content()
        self.assertIn('正式配发公告尚未公布',text);self.assertIn('开盘：10',text)
        self.assertNotIn('成交额',text);self.assertNotIn('振幅',text)
        self.assertEqual(self.service.store.rows()[0]['delivery_status'],'confirmed')

    def test_source_failure_does_not_delay_fixed_mail_or_invent_prices(self):
        self.service.cycle(at('16:00'))
        def fail(s):raise OSError('secret')
        self.service.collector=fail
        self.service.cycle(at('18:31'));self.service.cycle(at('18:40'))
        self.assertEqual(len(self.sent),1)
        text=self.sent[0].get_content()
        self.assertIn('尚未取得或尚未通过校验',text)
        self.assertNotIn('开盘：',text);self.assertNotIn('secret',text)
        self.assertIn('正式配发公告尚未公布',text)

    def test_restart_no_duplicate_and_unknown_outcome_never_retries(self):
        self.service.cycle(at('16:00'));self.service.cycle(at('18:31'));self.smtp_fails=True
        self.service.cycle(at('18:40'))
        self.smtp_fails=False
        again=GreyService(self.settings,self.flow,calendar_fetcher=lambda:[],collector=lambda _:snapshot())
        again.cycle(at('18:41'))
        self.assertEqual(len(self.sent),1)
        self.assertEqual(again.store.rows()[0]['delivery_status'],'unknown')

    def test_crash_after_confirmed_notice_repairs_without_resending(self):
        self.service.cycle(at('16:00'));self.service.cycle(at('18:31'));self.service.cycle(at('18:40'))
        with self.service.store.connect() as c:c.execute("UPDATE grey_sessions SET delivery_status='unknown',message_id=NULL")
        self.service.cycle(at('18:41'))
        self.assertEqual(len(self.sent),1)
        self.assertEqual(self.service.store.rows()[0]['delivery_status'],'confirmed')

    def test_no_backfill_on_first_launch_after_deadline(self):
        self.service.cycle(at('18:41'))
        self.assertEqual(self.sent,[])
        self.assertEqual(self.service.store.rows()[0]['delivery_status'],'skipped_history')

    def test_calendar_failure_at_deadline_cannot_block_existing_reminder(self):
        self.service.cycle(at('16:00'))
        def failure():raise OSError('unavailable')
        self.service.calendar_fetcher=failure
        self.service.cycle(at('18:40'))
        self.assertEqual(len(self.sent),1)

    def test_late_allotment_is_retained_without_a_second_mail(self):
        self.service.cycle(at('16:00'));self.service.cycle(at('18:31'));self.service.cycle(at('18:40'))
        e={'event_id':'source1','kind':'allotment','stock_code':'01234','stock_name':'示例公司',
           'published_at':'2026-11-23T22:30:00+08:00','url':'https://www.hkexnews.hk/allotment.pdf'}
        with self.state.connect() as c:c.execute('INSERT INTO events VALUES(?,?,0,?)',('source1',json.dumps(e),'now'))
        self.state.confirm_classification('source1','common_equity','ipo_allotment','allotment','episode1')
        self.service.cycle(at('22:31'))
        self.assertEqual(len(self.sent),1)
        self.assertEqual(self.service.store.rows()[0]['allotment']['status'],'published_pending_review')
        with self.service.store.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM grey_allotment_material').fetchone()[0],1)

    def test_source_conflicts_stay_unverified(self):
        self.service.cycle(at('16:00'))
        wrong=snapshot();wrong['stock_code']='09999'
        self.service.collector=lambda _:wrong
        self.service.cycle(at('18:31'))
        row=self.service.store.rows()[0]
        self.assertEqual(row['data_status'],'needs_source');self.assertIsNone(row['snapshot'])

    def test_half_day_collects_after_close_but_reminder_is_still_1840(self):
        s=dict(session(),session_start='2026-11-23T14:15:00+08:00',session_end='2026-11-23T16:30:00+08:00')
        self.service.calendar_fetcher=lambda:[s]
        self.service.cycle(at('14:00'))
        self.assertEqual(self.service.store.rows()[0]['due_at'],'2026-11-23T18:40:00+08:00')
        self.assertEqual(self.service.store.collection_due(at('16:29')),[])
        self.assertEqual(len(self.service.store.collection_due(at('16:31'))),1)

    def test_edited_listing_date_preserves_traded_session_identity(self):
        self.service.cycle(at('16:00'))
        sid=self.service.store.rows()[0]['session_id']
        self.service.store.register(dict(session(),listing_date='2026-11-27'),at('18:31'))
        self.assertEqual(len(self.service.store.rows()),1)
        self.assertEqual(self.service.store.rows()[0]['session_id'],sid)
        self.assertEqual(self.service.store.rows()[0]['trade_date'],'2026-11-23')

    def test_two_platforms_share_one_mail_but_keep_prices_separate(self):
        self.service.settings.provider=None
        self.service.settings.providers=('辉立暗盘','富途暗盘')
        def collect(item):
            s=snapshot();s['provider']=item['provider']
            if item['provider']=='富途暗盘':s['ohlc']['close']='13';s['bars'][-1]['close']='13'
            return s
        self.service.collector=collect
        self.service.cycle(at('16:00'));self.service.cycle(at('18:31'));self.service.cycle(at('18:40'))
        self.assertEqual(len(self.sent),1)
        text=self.sent[0].get_content()
        for term in ('辉立暗盘','富途暗盘','收盘：12','收盘：13'):self.assertIn(term,text)
        self.assertEqual({r['delivery_status'] for r in self.service.store.rows()},{'confirmed'})
        self.service.cycle(at('18:41'));self.assertEqual(len(self.sent),1)

    def test_positive_reschedule_before_open_supersedes_old_day(self):
        self.service.cycle(at('13:00'))
        later=dict(session(),trade_date='2026-11-24',listing_date='2026-11-25',
                   session_start='2026-11-24T16:15:00+08:00',session_end='2026-11-24T18:30:00+08:00')
        self.service.store.register(later,at('13:01'))
        self.service.cycle(at('18:40'))
        self.assertEqual(self.sent,[])
        self.assertIn('superseded',{r['delivery_status'] for r in self.service.store.rows()})

    def test_old_async_result_cannot_downgrade_a_new_verified_packet(self):
        self.service.cycle(at('16:00'));row=self.service.store.rows()[0]
        expected=self.service.store.material_sha(None)
        packet={'status':'verified','summary':'已独立复核'}
        self.service.store.attach_allotment(row['session_id'],packet)
        actual=self.service.store.attach_allotment(row['session_id'],{'status':'published_pending_review'},expected)
        self.assertEqual(actual,packet)
        self.assertEqual(self.service.store.get(row['session_id'])['allotment'],packet)

    def test_collection_validates_with_completion_clock(self):
        self.service.cycle(at('16:00'))
        s=snapshot();s['captured_at']='2026-11-23T18:31:01+08:00'
        self.service.collector=lambda _:s;self.service.clock=lambda:at('18:32')
        self.service.cycle(at('18:31'))
        self.assertEqual(self.service.store.rows()[0]['data_status'],'verified')

    def test_unrelated_same_code_episode_does_not_claim_current_allotment(self):
        self.service.cycle(at('16:00'))
        e={'event_id':'unrelated','kind':'allotment','stock_code':'01234','published_at':'2026-11-23',
           'url':'https://www.hkexnews.hk/old.pdf'}
        with self.state.connect() as c:c.execute('INSERT INTO events VALUES(?,?,0,?)',('unrelated',json.dumps(e),'now'))
        self.state.confirm_classification('unrelated','common_equity','ipo_allotment','allotment','other-episode')
        self.service.cycle(at('18:40'))
        text=self.sent[0].get_content()
        self.assertIn('当前发行期的配发公告发布状态尚在核验',text)
        self.assertNotIn('正式配发公告已发布',text)

    def test_same_episode_late_allotment_survives_postponed_listing(self):
        from unittest.mock import patch
        self.service.cycle(at('16:00'));self.service.cycle(at('18:31'));self.service.cycle(at('18:40'))
        self.service.store.register(dict(session(),listing_date='2026-11-27'),at('22:00'))
        e={'event_id':'delayed-allot','kind':'allotment','stock_code':'01234','published_at':'2026-11-27',
           'url':'https://www.hkexnews.hk/current.pdf'}
        with self.state.connect() as c:c.execute('INSERT INTO events VALUES(?,?,0,?)',('delayed-allot',json.dumps(e),'now'))
        self.state.confirm_classification('delayed-allot','common_equity','ipo_allotment','allotment','episode1')
        event=dict(e,episode_id='episode1',research_id='r1',source_event_ids=['delayed-allot'])
        self.state.research_run('r1',{'event':event,'writer_result':{}},'ready_for_delivery')
        packet={'status':'verified','summary':'最终定价：10港元','listing_date':'2026-11-27','source_event_ids':['delayed-allot']}
        with patch('hkipo.grey_material.verified_allotment',return_value=packet):
            actual=self.service._allotment(self.service.store.rows()[0])
        self.assertEqual(actual['status'],'verified')
        self.assertEqual(actual['listing_date'],'2026-11-27')
        self.assertEqual(len(self.sent),1)

    def test_background_collection_cannot_block_fixed_mail(self):
        import threading
        entered=threading.Event();release=threading.Event()
        self.service.settings.background_collection=True
        from concurrent.futures import ThreadPoolExecutor
        self.service.executor=ThreadPoolExecutor(max_workers=2)
        def collect(item):
            entered.set();release.wait(2);return snapshot()
        self.service.collector=collect
        try:
            self.service.cycle(at('16:00'));self.service.cycle(at('18:31'))
            self.assertTrue(entered.wait(1))
            self.service.cycle(at('18:40'))
            self.assertEqual(len(self.sent),1)
            self.assertIn('尚未取得或尚未通过校验',self.sent[0].get_content())
        finally:
            release.set();self.service.executor.shutdown(wait=True,cancel_futures=True)

    def test_same_day_time_correction_emits_only_current_platform_section(self):
        self.service.cycle(at('13:00'))
        corrected=dict(session(),session_start='2026-11-23T14:15:00+08:00',session_end='2026-11-23T16:30:00+08:00')
        self.service.store.register(corrected,at('13:01'))
        self.service.cycle(at('18:40'))
        self.assertEqual(len(self.sent),1)
        self.assertEqual(self.sent[0].get_content().count('测试券商'),1)

    def test_stale_old_calendar_row_does_not_cancel_replacement(self):
        self.service.cycle(at('13:00'))
        later=dict(session(),trade_date='2026-11-24',listing_date='2026-11-25',
                   session_start='2026-11-24T16:15:00+08:00',session_end='2026-11-24T18:30:00+08:00')
        new_id=self.service.store.register(later,at('13:01'))
        self.service.store.register(session(),at('13:02'))
        self.assertEqual(self.service.store.get(new_id)['delivery_status'],'pending')

    def test_stale_official_monitor_does_not_assert_unpublished(self):
        self.service.cycle(at('16:00'))
        with self.state.connect() as c:c.execute("UPDATE sources SET last_success=?,failure_count=1",(at('13:00').isoformat(),))
        self.service.cycle(at('18:40'))
        text=self.sent[0].get_content()
        self.assertIn('发布状态尚在核验',text)
        self.assertNotIn('正式配发公告尚未公布',text)

    def test_replayed_confirmed_old_session_cannot_cancel_new_future_session(self):
        self.service.cycle(at('16:00'));self.service.cycle(at('18:31'));self.service.cycle(at('18:40'))
        later=dict(session(),trade_date='2026-11-24',listing_date='2026-11-25',
                   session_start='2026-11-24T16:15:00+08:00',session_end='2026-11-24T18:30:00+08:00')
        sid=self.service.store.register(later,at('18:41'))
        self.service.store.register(session(),at('18:42'))
        self.assertEqual(self.service.store.get(sid)['delivery_status'],'pending')

    def test_shared_ipo_baseline_conflict_keeps_prices_without_false_returns(self):
        self.service.settings.provider=None;self.service.settings.providers=('辉立暗盘','富途暗盘')
        def collect(item):
            s=snapshot();s['provider']=item['provider']
            if item['provider']=='富途暗盘':s['issue_price']='11';s['lot_size']=200
            return s
        self.service.collector=collect
        self.service.cycle(at('16:00'));self.service.cycle(at('18:31'));self.service.cycle(at('18:40'))
        text=self.sent[0].get_content()
        self.assertIn('统一基准尚待核实',text);self.assertIn('开盘：10',text)
        self.assertNotIn('最高浮盈：',text);self.assertNotIn('（+20%',text)
        self.assertEqual(len(self.sent),1)

    def test_research_blocked_or_status_changed_invalidates_cached_summary(self):
        import hashlib
        self.service.cycle(at('16:00'));row=self.service.store.rows()[0]
        paths=[]
        for name in ('bundle','review','source','status'):
            p=self.root/(name+'.txt');p.write_text('version1');paths.append(p)
        digest=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
        packet={'status':'verified','summary':'已经复核','bundle_path':str(paths[0]),'bundle_sha256':digest(paths[0]),
                'review_path':str(paths[1]),'review_sha256':digest(paths[1]),'research_id':'r1','request_id':'q1',
                'source_documents':[{'path':str(paths[2]),'sha256':digest(paths[2])}],
                'guard_files':[{'path':str(paths[3]),'sha256':digest(paths[3])}]}
        event={'kind':'allotment','stock_code':'01234','episode_id':'episode1','research_id':'r1'}
        payload={'event':event,'writer_result':{'request_id':'q1'}}
        self.state.research_run('r1',payload,'ready_for_delivery')
        self.service.store.attach_allotment(row['session_id'],packet)
        self.assertEqual(self.service._allotment(row,verify=False)['status'],'verified')
        self.state.research_run('r1',payload,'blocked')
        self.assertEqual(self.service._allotment(row,verify=False)['status'],'published_pending_review')
        self.service.store.attach_allotment(row['session_id'],packet)
        self.state.research_run('r1',payload,'ready_for_delivery')
        paths[3].write_text('version2')
        self.assertNotEqual(self.service._allotment(row,verify=False)['status'],'verified')

if __name__=='__main__':unittest.main()
