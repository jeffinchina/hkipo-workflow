from datetime import datetime,timezone,timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from urllib.error import HTTPError
from hkipo.monitor import Monitor
from hkipo.sources import SourceResult,SourceError,TitleSearchSource,AggregateSource,HttpClient,parse_aggregate,parse_title_json,classify
from hkipo.store import StateStore
from test_store import event

FIX=Path(__file__).parent/'fixtures'
class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=StateStore(Path(self.temp.name)/'db')
        self.source=Mock();self.source.name='fixture';self.now=datetime(2026,11,19,tzinfo=timezone.utc)
    def tearDown(self):self.temp.cleanup()
    def test_baseline_restart_overlap_late_document(self):
        self.source.fetch.return_value=SourceResult('fixture',[event()],count=1)
        m=Monitor(self.store,[self.source]);self.assertTrue(m.scan(self.now)['sources'][0]['baseline'])
        self.source.fetch.return_value=SourceResult('fixture',[event(),event(2)],count=2)
        r=Monitor(self.store,[self.source]).scan(self.now+timedelta(minutes=1))
        self.assertEqual(r['sources'][0]['new_events'],1);self.assertEqual(len(self.store.jobs()),1)
        start,_=self.source.fetch.call_args.args
        self.assertEqual(start,self.now-timedelta(hours=72))
    def test_bad_response_keeps_cutoff_and_one_open_alert(self):
        self.source.fetch.return_value=SourceResult('fixture',[event()]);m=Monitor(self.store,[self.source]);m.scan(self.now)
        self.source.fetch.side_effect=SourceError('empty response')
        for _ in range(2):self.assertFalse(m.scan(self.now+timedelta(minutes=1))['ok'])
        self.assertEqual(self.store.source_state('fixture')['cutoff'],self.now.isoformat())
        self.assertEqual(len(self.store.alerts()),1)
    def test_real_aggregate_preserves_multiple_pdfs(self):
        result=parse_aggregate((FIX/'main-board-real.html').read_text(),'https://www2.hkexnews.hk')
        self.assertEqual(len(result.events),16)
        same=[e for e in result.events if e.stock_code=='03275']
        self.assertEqual(len(same),3);self.assertEqual(len({e.event_id for e in same}),3)
    def test_real_title_full_day_and_multi_security(self):
        events,total,more=parse_title_json((FIX/'title-search-real.json').read_text())
        self.assertEqual(total,587);self.assertEqual(len(events),1203);self.assertFalse(more)
        self.assertEqual(len({e.event_id for e in events}),1203)
    def test_bad_schema_and_empty_aggregate_fail(self):
        for text in ('','<html>blocked</html>',(FIX/'gem-real.html').read_text()):
            with self.assertRaises(SourceError):parse_aggregate(text,'https://www2.hkexnews.hk')
        for raw in ('{}','[]','bad'):
            with self.assertRaises(SourceError):parse_title_json(raw)
    def test_preliminary_and_correction(self):
        self.assertEqual(classify('申請版本 招股章程'),'preliminary')
        self.assertEqual(classify('全球發售 更正公告'),'exception')
        self.assertEqual(classify('股份配發結果'),'allotment')
        self.assertEqual(classify('正面盈利預告'),'other')
    def test_pagination_detects_partial_repeated_page(self):
        full=json.loads((FIX/'title-search-real.json').read_text());rows=json.loads(full['result'])
        page=dict(full,result=json.dumps(rows[:100]),loadedRecord=100,hasNextRow=True,rowRange=100)
        client=Mock();client.get.return_value=json.dumps(page)
        with self.assertRaises(SourceError):TitleSearchSource(client,max_records=600).fetch(self.now,self.now)
        self.assertEqual(client.get.call_count,2)
    def test_pagination_complete_checks(self):
        full=json.loads((FIX/'title-search-real.json').read_text());rows=json.loads(full['result'])
        page=dict(full,result=json.dumps(rows[:100]),loadedRecord=100,hasNextRow=True,rowRange=100)
        client=Mock();client.get.side_effect=[json.dumps(page),json.dumps(full)]
        result=TitleSearchSource(client).fetch(self.now,self.now)
        self.assertEqual(result.count,587);self.assertEqual(len(result.events),1203)
    def test_retry_after_respected_and_bounded(self):
        sleeps=[];client=HttpClient(sleep=sleeps.append);client.opener=Mock()
        client.opener.open.side_effect=HTTPError('https://www1.hkexnews.hk',429,'rate',{'Retry-After':'3'},None)
        with self.assertRaises(SourceError):client.get('https://www1.hkexnews.hk/x')
        self.assertEqual(sleeps,[3]);self.assertEqual(client.opener.open.call_count,2)
    def test_aggregate_verified_language_fallback(self):
        client=Mock();client.get.side_effect=[SourceError('bad'),(FIX/'main-board-real.html').read_text()]
        self.assertEqual(len(AggregateSource(client=client).fetch(self.now,self.now).events),16)
        self.assertEqual(client.get.call_count,2)

    def test_long_retry_after_persists_across_restart(self):
        self.source.fetch.side_effect=SourceError('rate limited; deferred by Retry-After',retry_after=600)
        Monitor(self.store,[self.source]).scan(self.now)
        self.source.fetch.reset_mock()
        result=Monitor(StateStore(self.store.dbpath),[self.source]).scan(self.now+timedelta(seconds=60))
        self.assertTrue(result['sources'][0]['deferred'])
        self.source.fetch.assert_not_called()
    def test_title_query_dates_are_hong_kong_not_utc(self):
        from urllib.parse import parse_qs,urlsplit
        client=Mock();client.get.return_value=json.dumps({'result':'[]','loadedRecord':0,'recordCnt':0,'hasNextRow':False})
        end=datetime(2026,11,18,17,tzinfo=timezone.utc)
        TitleSearchSource(client).fetch(end-timedelta(hours=2),end)
        query=parse_qs(urlsplit(client.get.call_args.args[0]).query)
        self.assertEqual(query['fromDate'],['20261118']);self.assertEqual(query['toDate'],['20261119'])
    def test_restart_retains_system_anchor_during_source_failure(self):
        self.source.fetch.side_effect=SourceError('failure')
        Monitor(self.store,[self.source]).scan(self.now)
        self.source.fetch.side_effect=None
        from dataclasses import replace
        later=replace(event(3),published_at=(self.now+timedelta(minutes=1)).isoformat())
        self.source.fetch.return_value=SourceResult('fixture',[later],count=1)
        result=Monitor(StateStore(self.store.dbpath),[self.source]).scan(self.now+timedelta(minutes=2))
        self.assertFalse(result['sources'][0]['baseline']);self.assertEqual(len(self.store.jobs()),1)
