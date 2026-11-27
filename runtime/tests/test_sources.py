from datetime import datetime, timezone
import json
from pathlib import Path
import unittest
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit
from hkipo.sources import TitleSearchSource, SourceError, parse_title_json
from hkipo.classification import title_evidence


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
