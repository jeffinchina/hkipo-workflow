from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit
from hkipo.sources import TitleSearchSource, SourceError, SourceResult, parse_title_json
from hkipo.classification import title_evidence
from hkipo.monitor import Monitor
from hkipo.service import LocalService, ServiceSettings
from hkipo.store import StateStore


class TitlePaginationTests(unittest.TestCase):
    def setUp(self):
        self.full=json.loads((Path(__file__).parent/'fixtures/title-search-real.json').read_text())
        self.rows=json.loads(self.full['result'])
        self.now=datetime(2026,11,19,tzinfo=timezone.utc)

    def page(self, rows, total):
        return json.dumps(dict(self.full,result=json.dumps(rows),loadedRecord=len(rows),recordCnt=total,hasNextRow=len(rows)<total))

    def fetch(self, pages, **kwargs):
        self.client=Mock();self.client.get.side_effect=pages
        return TitleSearchSource(self.client,**kwargs).fetch(self.now,self.now)

    def limits(self):
        return [int(parse_qs(urlsplit(c.args[0]).query)['rowRange'][0]) for c in self.client.get.call_args_list]

    def test_real_total_jumps_to_600_and_preserves_multi_security(self):
        r=self.fetch([self.page(self.rows[:100],587),json.dumps(self.full)])
        self.assertEqual(self.limits(),[100,600]);self.assertEqual(r.count,587)
        self.assertEqual(len(r.events),1203);self.assertEqual(len({e.event_id for e in r.events}),1203)
        self.assertEqual(len(r.responses),2)

    def test_first_page_complete_including_exact_page_and_empty(self):
        for n in (0,50,100):
            with self.subTest(n=n):
                self.assertTrue(self.fetch([self.page(self.rows[:n],n)]).complete)
                self.assertEqual(self.limits(),[100])

    def test_exact_multiple_and_nonmultiple_cap(self):
        for total,cap,limit in ((200,1000,200),(250,250,250)):
            with self.subTest(total=total):
                self.fetch([self.page(self.rows[:100],total),self.page(self.rows[:total],total)],max_records=cap)
                self.assertEqual(self.limits(),[100,limit])

    def test_over_cap_fails_first_request(self):
        with self.assertRaisesRegex(SourceError,'limit exceeded'):
            self.fetch([self.page(self.rows[:100],587)],max_records=300)
        self.assertEqual(self.limits(),[100])

    def test_changes_missing_prefix_duplicates_and_truncation_fail(self):
        for second in (self.page(self.rows,588),self.page(self.rows[1:],586),
                       self.page(self.rows[1:]+[dict(self.rows[0],FILE_LINK="/listedco/listconews/sehk/2026/1119/unique_c.pdf")],587),
                       self.page(self.rows[:586]+[self.rows[0]],587),
                       self.page(self.rows[:100],587),self.page(self.rows[:200],587)):
            with self.subTest(second=second[:40]),self.assertRaises(SourceError):
                self.fetch([self.page(self.rows[:100],587),second])
            self.assertEqual(self.limits(),[100,600])

    def test_6000_rows_requires_only_two_requests(self):
        rows=[dict(self.rows[0],NEWS_ID=str(90000000+i),FILE_LINK=f'/listedco/listconews/sehk/2026/1119/{i}_c.pdf') for i in range(6000)]
        self.fetch([self.page(rows[:100],6000),self.page(rows,6000)])
        self.assertEqual(self.limits(),[100,6000])

    def test_real_republication_same_pdf_preserves_both_rows_and_one_event(self):
        raw=(Path(__file__).parent/'fixtures/title-search-republication-real.json').read_text()
        result=self.fetch([raw])
        self.assertEqual(result.count,2);self.assertEqual(len(result.events),1)
        self.assertEqual(result.events[0].source_id,'12420611')
        self.assertEqual(result.events[0].published_at,'2026-11-20T18:38:00+08:00')
        self.assertIn('12420777',result.evidence);self.assertIn('12420611',result.evidence)
        self.assertEqual(result.responses[0]['body'],raw)

    def test_republication_alias_cannot_mask_lost_raw_prefix_or_conflict(self):
        raw=(Path(__file__).parent/'fixtures/title-search-republication-real.json').read_text()
        rows=json.loads(json.loads(raw)['result'])
        # Different publication rows for one PDF are legitimate, but replacing
        # one prefix NEWS_ID by a third alias must still fail completeness.
        changed=[dict(rows[0],NEWS_ID='99999999'),rows[1]]
        with self.assertRaises(SourceError):self.fetch([self.page(rows[:1],2),self.page(changed,2)])
        with self.assertRaisesRegex(SourceError,'conflicting'):
            self.fetch([self.page([rows[0],dict(rows[1],TITLE='changed headline')],2)])


class TitleExclusionTests(unittest.TestCase):
    def setUp(self):
        self.raw=(Path(__file__).parent/'fixtures/title-search-structured-exclusions-real.json').read_text()
        self.data=json.loads(self.raw);self.rows=json.loads(self.data['result'])

    def altered(self, **changes):
        return json.dumps(dict(self.data,result=[dict(self.rows[0],**changes)],recordCnt=1,loadedRecord=1))

    def test_real_rows_excluded_with_counts_and_raw_audit(self):
        exclusions=[]
        self.assertEqual(parse_title_json(self.raw,exclusions=exclusions),([],2,False))
        self.assertEqual([x['NEWS_ID'] for x in exclusions],['12416777','12416775'])
        self.assertEqual(title_evidence(self.raw),[])
        client=Mock();client.get.return_value=self.raw
        now=datetime(2026,11,20,tzinfo=timezone.utc)
        result=TitleSearchSource(client).fetch(now,now)
        self.assertEqual(result.count,2);self.assertEqual(result.events,[])
        for x in exclusions:self.assertIn(x['NEWS_ID'],result.evidence)
        self.assertEqual(result.responses[0]['body'],self.raw)

    def test_code_less_debt_programme_notice_does_not_block_complete_scan(self):
        notice = dict(self.rows[0], NEWS_ID='123', STOCK_CODE='', STOCK_NAME='',
                      LONG_TEXT='債券及結構性產品 - [上市通告 - 債務證券發行計劃]')
        equity = dict(notice, NEWS_ID='456', STOCK_CODE='01234', STOCK_NAME='測試公司',
                      LONG_TEXT='上市文件 - [發售以供認購]', TITLE='全球發售',
                      FILE_LINK='/listedco/listconews/sehk/2026/1127/2026112700001_c.pdf')
        data = dict(self.data, result=[notice, equity], recordCnt=2,
                    loadedRecord=2, hasNextRow=False)
        raw = json.dumps(data)
        client = Mock()
        client.get.return_value = raw
        now = datetime(2026, 11, 27, tzinfo=timezone.utc)
        result = TitleSearchSource(client).fetch(now, now)
        self.assertEqual(result.count, 2)
        self.assertEqual([e.stock_code for e in result.events], ['01234'])
        self.assertIn('non-IPO debt issuance programme notice', result.evidence)
        self.assertEqual([item.stock_code for item in title_evidence(raw)], ['01234'])

    def test_unknown_blank_and_one_sided_blank_remain_rejected(self):
        for change in ({'LONG_TEXT':'上市文件 - [發售以供認購]'},
                       {'STOCK_CODE':'01234'},{'STOCK_NAME':'issuer'},
                       {'STOCK_CODE':' '},{'STOCK_CODE':'abc'},
                       {'LONG_TEXT':'債券及結構性產品 - [其他]'}):
            with self.subTest(change=change),self.assertRaises(SourceError):
                parse_title_json(self.altered(**change))
        self.assertEqual(title_evidence(self.altered(LONG_TEXT='上市文件 - [發售以供認購]')),[])

    def test_exclusions_still_validate_date_url_fields_and_counts(self):
        for change in ({'DATE_TIME':'bad'},{'FILE_LINK':'https://evil.example/doc.pdf'}):
            with self.subTest(change=change),self.assertRaises(SourceError):
                parse_title_json(self.altered(**change))
        for key,value in (('loadedRecord',1),('recordCnt',1),('hasNextRow',True)):
            with self.subTest(key=key),self.assertRaises(SourceError):
                parse_title_json(json.dumps(dict(self.data,**{key:value})))
        row=dict(self.rows[0]);del row['TITLE']
        with self.assertRaises(SourceError):parse_title_json(json.dumps(dict(self.data,result=[row],recordCnt=1,loadedRecord=1)))


class TitleRecoveryTests(unittest.TestCase):
    def setUp(self):
        data=json.loads((Path(__file__).parent/'fixtures/title-search-real.json').read_text())
        self.template=json.loads(data['result'])[0]
        self.start=datetime(2026,11,19,tzinfo=timezone.utc)
        self.end=self.start+timedelta(days=1,hours=1)
        self.client=Mock()

    def row(self,id,day,minute='08:30',**changes):
        defaults={'NEWS_ID':str(id),'DATE_TIME':f'{day}/11/2026 {minute}',
                  'STOCK_CODE':'01234','STOCK_NAME':'測試發行人','TITLE':'全球發售',
                  'FILE_LINK':f'/listedco/listconews/sehk/2026/11{day}/{id}_c.pdf',
                  'LONG_TEXT':'上市文件 - [發售以供認購]'}
        return dict(self.template,**(defaults|changes))

    @staticmethod
    def page(*rows):
        return json.dumps({'result':rows,'recordCnt':len(rows),'loadedRecord':len(rows),'hasNextRow':False})

    def test_recovery_keeps_old_watermark_and_audits_valid_rows_once(self):
        bad=self.row(1,19,STOCK_CODE='01234<br>unknown',STOCK_NAME='測試發行人<br>另一發行人')
        latest=self.row(2,20)
        older=self.row(3,19)
        previous=self.row(4,19,'07:30')
        fixed=dict(bad,STOCK_CODE='01234',STOCK_NAME='測試發行人')
        primary=self.page(bad,latest,older,previous)
        self.client.get.side_effect=[
            self.page(),primary,self.page(latest),self.page(bad,older),
            primary,self.page(latest),self.page(bad,older,previous),
            self.page(fixed,latest,older,previous)]
        with tempfile.TemporaryDirectory() as root:
            source=TitleSearchSource(self.client)
            def service():
                return LocalService(ServiceSettings(root),sources=[source])
            first=service()
            first.scan_cycle(self.start)
            result=first.scan_cycle(self.end)
            self.assertFalse(result['ok'])
            self.assertEqual(result['sources'][0]['recovery']['new_events'],2)
            self.assertEqual(first.store.source_state(source.name)['cutoff'],self.start.isoformat())
            manifest=next(m for m in first.archive.manifests() if m['source']==source.recovery_name)
            self.assertFalse(manifest['complete'])
            self.assertIn('quarantined NEWS_IDs=["1"]',json.loads(manifest['parse_audit'])['partitions'][1])
            evidence,errors=first.archive.evidence()
            self.assertEqual({item.locator for item in evidence},{'NEWS_ID:2','NEWS_ID:3'})
            self.assertFalse(errors)

            restarted=service()
            result=restarted.scan_cycle(self.end+timedelta(minutes=1))
            self.assertEqual(len(restarted.store.jobs()),2)
            self.assertEqual(result['sources'][0]['recovery']['new_events'],1)
            self.assertEqual(len(restarted.store.alerts()),1)
            recovered_at=self.end+timedelta(minutes=2)
            self.assertTrue(restarted.scan_cycle(recovered_at)['ok'])
            self.assertEqual(len(restarted.store.jobs()),3)
            self.assertEqual(restarted.store.alerts(),[])
            self.assertEqual(restarted.store.source_state(source.name)['cutoff'],recovered_at.isoformat())
            self.assertEqual([m['source'] for m in restarted.archive.manifests()],[source.name])

    def test_recovery_certifies_all_days_or_leaves_coverage_incomplete(self):
        self.client.get.side_effect=[self.page(self.row(1,20)),self.page(self.row(2,19))]
        source=TitleSearchSource(self.client)
        result=source.recover(self.start,self.end)
        self.assertTrue(result.complete)
        self.assertEqual(result.source,source.name)
        self.assertEqual({row.source_id for row in result.events},{'1','2'})
        dates=[parse_qs(urlsplit(call.args[0]).query) for call in self.client.get.call_args_list]
        self.assertEqual([query['fromDate'] for query in dates],[['20261120'],['20261119']])
        self.assertEqual([query['toDate'] for query in dates],[['20261120'],['20261119']])

        self.client.get.side_effect=[self.page(self.row(1,19)),self.page(self.row(2,19))]
        result=source.recover(self.start,self.end)
        self.assertFalse(result.complete)
        self.assertEqual([row.source_id for row in result.events],['2'])
        self.assertIn('outside requested recovery day',result.evidence)

        self.client.reset_mock()
        self.client.get.side_effect=[self.page()]*4
        result=source.recover(self.start-timedelta(days=7),self.end)
        self.assertFalse(result.complete)
        self.assertEqual(self.client.get.call_count,4)

    def test_backup_runs_before_recovery_and_rate_limit_is_retained(self):
        backup=Mock()
        backup.name='new-listings-main-board'
        backup.fetch.return_value=SourceResult(backup.name,[])
        def limited(url):
            backup.fetch.assert_called_once()
            raise SourceError('rate limited',retry_after=600)
        def primary(url):
            if self.client.get.call_count==1:
                return 'not JSON'
            return limited(url)
        self.client.get.side_effect=primary
        with tempfile.TemporaryDirectory() as root:
            store=StateStore(Path(root)/'state.sqlite3')
            monitor=Monitor(store,[TitleSearchSource(self.client),backup])
            result=monitor.scan(self.end)
            self.assertFalse(result['ok'])
            self.assertTrue(result['sources'][1]['ok'])
            self.assertEqual(result['sources'][0]['recovery']['error'],'rate limited')
            self.assertTrue(monitor.scan(self.end+timedelta(minutes=1))['sources'][0]['deferred'])
            self.assertEqual(self.client.get.call_count,2)
            self.assertEqual(backup.fetch.call_count,2)
