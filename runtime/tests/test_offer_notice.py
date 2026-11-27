from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch, Mock

import fitz
from hkipo.delivery import BeforeArticleDelivery, sha
from hkipo.models import DocumentEvent
from hkipo.offer_notice import OfferNotices, opening_dates
from hkipo.store import StateStore


class OfferNoticeTests(unittest.TestCase):
    def setUp(self):
        temp=TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        self.store=StateStore(self.root/'state.db')
        self.activation=datetime(2026,11,27,0,0,tzinfo=timezone.utc)
        self.now=self.activation+timedelta(hours=1)
        self.config=self.root/'mail.json'
        self.config.write_text(json.dumps({'email': dict(smtp_host='mail.example.test',smtp_port=465,
                                                        user='owner@example.test',password='fixture')}))
        self.recipient=patch('hkipo.offer_notice.DEFAULT_RECIPIENT','test@example.test');self.recipient.start()
        self.addCleanup(self.recipient.stop)
        self.sender=Mock(side_effect=lambda cfg,msg,recipient: {'smtp_accepted':True,'message_id':msg['Message-ID']})
        self.notices=self.worker()

    def worker(self):
        return OfferNotices(self.store,self.root,self.config,self.activation,sender=self.sender)

    def add(self,code,opening='2026年11月27日',*,historic=False,baseline=False,other_day=None):
        minute=self.activation+timedelta(minutes=-1 if historic else 1)
        episode='offer-'+code
        audit=[]
        for index,role in enumerate(('announcement','prospectus')):
            pdf=self.root/f'{code}-{role}.pdf'
            with fitz.open() as document:
                page=document.new_page()
                page.insert_text((25,50),'香港公開發售開始 . . . '+(other_day if index and other_day else opening),fontname='china-t')
                document.save(pdf)
            url=f'https://www1.hkexnews.hk/listedco/{code}{index}.pdf'
            event=DocumentEvent('','fixture',str(index),code,'測試公司','offer','Formal document',url,minute.isoformat(),minute.isoformat())
            self.store.ingest([event],baseline=baseline)
            self.store.confirm_classification(event.event_id,'common_equity','ipo_offer',role,episode)
            audit.append({'cover':{'pass':True,'document_role':role,'path':str(pdf),'sha256':sha(pdf.read_bytes())},
                          'official_evidence':[{'url':url,'role':role,'stock_code':code}]})
        (self.root/f'{episode}.json').write_text(json.dumps({'decision':'confirmed','prospectus_public_date':'2026-11-27',
              'episode_id':episode,'stock_code':code,'issuer':'測試公司','audit':audit}))

    def test_verified_today_notice_sends_once_without_writer_or_article(self):
        self.add('09991');self.add('09992',historic=True);self.add('09993',baseline=True)
        result=self.notices.cycle(self.now)
        self.assertEqual(result['attention'],[])
        self.assertEqual([r['status'] for r in result['results']],['confirmed'])
        message=self.sender.call_args.args[1]
        self.assertIn('09991',message['Subject'])
        self.assertIn('港交所正式文件',message.get_content())
        self.assertIn('新股分析流程已进入处理队列',message.get_content())
        self.assertEqual(self.worker().cycle(self.now)['results'],[])
        self.sender.assert_called_once()

    def test_separate_registration_date_is_excluded_and_conflicting_documents_do_not_send(self):
        dates=opening_dates('香港公開發售開始 . . .\n2026年11月27日\n香港公開發售開始辦理認購申請登記. . . 2026年12月2日')
        self.assertEqual([day.isoformat() for day in dates],['2026-11-27'])
        self.add('09991',other_day='2026年11月28日')
        self.assertIn('offer_timetable_not_verified',self.notices.cycle(self.now)['attention'])
        self.sender.assert_not_called()

    def test_new_day_schedule_and_preflight_retry(self):
        self.add('09991',opening='2026年11月28日')
        self.notices.cycle(self.now);self.sender.assert_not_called()
        next_day=self.now+timedelta(days=1)
        self.sender.side_effect=BeforeArticleDelivery('temporary')
        self.assertEqual(self.notices.cycle(next_day)['results'][0]['status'],'retryable')
        self.assertEqual(self.worker().cycle(next_day+timedelta(seconds=30))['results'],[])
        self.sender.side_effect=lambda cfg,msg,recipient: {'smtp_accepted':True,'message_id':msg['Message-ID']}
        self.assertEqual(self.worker().cycle(next_day+timedelta(seconds=61))['results'][0]['status'],'confirmed')

    def test_ambiguous_delivery_never_replays(self):
        self.add('09991')
        self.sender.side_effect=TimeoutError
        self.assertEqual(self.notices.cycle(self.now)['results'][0]['status'],'needs_reconciliation')
        self.assertIn('offer_email_outcome_unconfirmed',self.worker().cycle(self.now+timedelta(hours=1))['attention'])
        self.sender.assert_called_once()
