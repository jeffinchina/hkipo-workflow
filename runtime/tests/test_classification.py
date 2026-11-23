import os
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from hkipo.classification import OfficialDocumentEvidence,aggregate_evidence,title_evidence,inspect_cover,classify_offer,classify_followup,apply_classification
from hkipo.models import DocumentEvent
from hkipo.store import StateStore

FIX=Path(__file__).parent/'fixtures'
ROOT=Path(__file__).resolve().parents[2]
SOURCES=Path(os.environ.get('HKIPO_TEST_SOURCE_DIR','/optional-fixtures/downloads'))

class ClassificationTests(unittest.TestCase):
    def setUp(self):
        row=json.loads((FIX/'yifei-legacy-official-row.json').read_text());s=row['stock'];self.events=[];self.paths={};self.evidence=[]
        for key,role in [('announcement','announcement'),('prospectus','prospectus')]:
            event=DocumentEvent('','legacy-official-snapshot',key,'06871',s['name'],'offer','新上市公告' if role=='announcement' else '招股章程',s[key],'2026-05-08T00:00:00+08:00','2026-05-08T00:00:00+08:00')
            self.events.append(event)
            self.paths[event.event_id]=SOURCES/('6871_浙江翼菲智能科技股份有限公司_'+('公告' if role=='announcement' else '招股章程')+'.pdf')
            # Authentic historical row role metadata, not fabricated live HTML.
            self.evidence.append(OfficialDocumentEvidence(s[key],'06871',s['name'],role,'official_new_listing_table','https://www2.hkexnews.hk/New-Listings/New-Listing-Information/Main-Board?sc_lang=zh-HK',row['snapshot_sha256'],'legacy snapshot stock row.'+key,'2026-05-08'))
        if not all(p.exists() for p in self.paths.values()):self.skipTest('Existing authorized original PDFs not present')
    def test_real_yifei_cover_pair_auto_confirms(self):
        decision=classify_offer(self.events,self.paths,self.evidence)
        self.assertEqual(decision['decision'],'confirmed',decision)
        self.assertEqual({x['document_role'] for x in decision['assignments']},{'announcement','prospectus'})
        self.assertEqual(decision['prospectus_public_date'],'2026-05-08')
        self.assertTrue(all(x['cover']['evidence'] for x in decision['audit']))
        with tempfile.TemporaryDirectory() as tmp:
            store=StateStore(Path(tmp)/'db');store.ingest(self.events)
            self.assertEqual(apply_classification(store,decision),2)
            claim=store.claim_job_group('test');self.assertEqual(len(claim['event_ids']),2)
    def test_real_fast_title_roles_and_pdf_pair_auto_confirms(self):
        directory=ROOT/'research/sources/fast-probe'
        evidence=title_evidence((directory/'positive-day-new-listings.json').read_text())
        evidence+=title_evidence((directory/'positive-day-offer.json').read_text())
        result=classify_offer(self.events,self.paths,evidence)
        self.assertEqual(result['decision'],'confirmed',result)

    def test_official_role_missing_never_guessed_from_cover(self):
        result=classify_offer(self.events,self.paths,[]);self.assertEqual(result['decision'],'needs_classification')
    def test_wrong_code_and_wrong_role_rejected(self):
        cover=inspect_cover(next(iter(self.paths.values())),'01234');self.assertFalse(cover['pass'])
        swapped=[replace(x,role='prospectus' if x.role=='announcement' else 'announcement') for x in self.evidence]
        self.assertEqual(classify_offer(self.events,self.paths,swapped)['decision'],'needs_classification')
    def test_round_date_conflict_blocks(self):
        bad=[replace(self.evidence[0],public_date='2026-05-09'),self.evidence[1]]
        self.assertEqual(classify_offer(self.events,self.paths,bad)['reason'],'document_round_date_conflict')
    def test_only_announcement_waits_for_prospectus(self):
        self.assertEqual(classify_offer(self.events[:1],self.paths,self.evidence)['decision'],'needs_classification')
    def test_preliminary_kind_not_promoted(self):
        prelim=[replace(e,kind='preliminary',event_id='') for e in self.events]
        self.assertEqual(classify_offer(prelim,self.paths,self.evidence)['decision'],'needs_classification')
    def test_real_main_aggregate_roles_and_empty_gem(self):
        evidence=aggregate_evidence((FIX/'main-board-real.html').read_text(),'https://www2.hkexnews.hk/New-Listings/New-Listing-Information/Main-Board?sc_lang=zh-HK')
        self.assertEqual(len(evidence),16);self.assertEqual(sum(e.stock_code=='03275' for e in evidence),3)
        self.assertEqual(aggregate_evidence((FIX/'gem-real.html').read_text(),'https://www2.hkexnews.hk/New-Listings/New-Listing-Information/GEM?sc_lang=zh-HK'),[])
    def test_real_titles_do_not_promote_warrant_fund_or_routine_rows(self):
        raw=(FIX/'title-search-real.json').read_text();rows=json.loads(json.loads(raw)['result'])
        evidence=title_evidence(raw)
        derivative_urls={r['FILE_LINK'] for r in rows if any(k in r.get('LONG_TEXT','') for k in ['衍生權證','牛熊證','交易所買賣基金'])}
        self.assertTrue(derivative_urls)
        self.assertFalse(any(any(e.url.endswith(u) for u in derivative_urls) for e in evidence))
    def test_no_existing_ipo_episode_means_followup_unknown(self):
        source=ROOT/'research/cases/tongrentang_exception_20260327/source.pdf'
        event=DocumentEvent('','fixture','exception','2667','北京同仁堂醫養投資股份有限公司','exception','延遲全球發售','https://www.hkexnews.hk/listedco/listconews/sehk/2026/0327/2026032700186_c.pdf','2026-03-27T00:00:00+08:00','2026-03-27T00:00:00+08:00')
        self.assertEqual(classify_followup(event,source,[],{})['reason'],'previous_confirmed_ipo_episode_required')
    def test_real_exception_exact_prospectus_reference_matches_prior_episode(self):
        directory=ROOT/'research/cases/tongrentang_exception_20260327';source=json.loads((directory/'source.json').read_text())
        event=DocumentEvent('','fixture','exception','2667','北京同仁堂醫養投資股份有限公司','exception','延遲全球發售',source['source_url'],'2026-03-27T00:00:00+08:00','2026-03-27T00:00:00+08:00')
        # Existing confirmed episode is the explicit premise of this unit test;
        # source.json is original PDF provenance, not a fabricated title response.
        prior={'decision':'confirmed','stock_code':'02667','issuer':'北京同仁堂醫養投資股份有限公司','episode_id':'prior-confirmed-ipo','prospectus_public_date':'2026-03-20'}
        official=[OfficialDocumentEvidence(event.url,'02667',event.stock_name,'exception','prior verified official exception metadata',source['source_url'],source['sha256'],'authorized historical case source.json','2026-03-27')]
        result=classify_followup(event,directory/'source.pdf',official,prior)
        self.assertEqual(result['decision'],'confirmed',result)
        self.assertEqual(result['episode_id'],'prior-confirmed-ipo')
        wrong=dict(prior,prospectus_public_date='2026-03-21')
        self.assertEqual(classify_followup(event,directory/'source.pdf',official,wrong)['decision'],'needs_classification')


class FormalNoticeTitleTests(unittest.TestCase):
    def test_real_leaf_and_missing_pdf_stays_unknown(self):
        raw=(FIX/'positive-day-new-listings.json').read_text()
        notice=next(e for e in title_evidence(raw) if e.locator=='NEWS_ID:12152406')
        self.assertEqual(notice.category,'公告及通告-[正式通告]')
        self.assertEqual((notice.stock_code,notice.role),('06871','announcement'))
        event={'event_id':'notice','kind':'offer','stock_code':'06871','title':'全球發售','url':notice.url}
        result=classify_offer([event],{},[notice])
        self.assertEqual(result['decision'],'needs_classification')
        self.assertEqual(result['audit'][0]['rule'],'local_pdf_missing')

    def test_english_and_non_ipo_boundary_variants_never_confirm(self):
        raw=(FIX/'positive-day-new-listings.json').read_text()
        original=next(r for r in json.loads(json.loads(raw)['result']) if r['NEWS_ID']=='12152406')
        # Explicit synthetic boundary variants derived from a real saved row.
        for category,title,expected in [
            ('Announcements and Notices - [Formal Notice]','Routine notice',1),
            ('公告及通告 - [正式通告]','供股結果',1),
            ('公告及通告 - [其他-雜項]','Formal Notice 全球發售',0),
            ('Formal Notice','全球發售',0),
            ('公告及通告 - [正式通告 / 其他]','全球發售',0),
        ]:
            with self.subTest(category=category,title=title):
                evidence=title_evidence(json.dumps({'result':[dict(original,LONG_TEXT=category,TITLE=title)]}))
                self.assertEqual(len(evidence),expected)
                event={'event_id':'non-ipo','kind':'offer','stock_code':'06871','title':title,'url':evidence[0].url if evidence else original['FILE_LINK']}
                self.assertEqual(classify_offer([event],{},evidence)['decision'],'needs_classification')
