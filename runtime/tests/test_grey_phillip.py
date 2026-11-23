"""Synthetic licensed-line/official-quote contracts; no live requests."""
from datetime import datetime,timedelta,timezone
import json
import tempfile
import unittest
from unittest.mock import Mock,patch
from hkipo.grey_market import HK
from hkipo.grey_source import GreySourceUnavailable
from hkipo.grey_aastocks import parse_price_line,page_config,fetch_price_line
from hkipo.grey_phillip import (parse_public_quote,combine_line_and_quote,final_pdf_url,
    public_guest_parameters,public_ajax_url,parse_final_lines,read_public_pmp,collect_live_quote)


def fixture():
    start=datetime(2026,11,23,16,15,tzinfo=HK);end=start+timedelta(minutes=135)
    captured=end+timedelta(minutes=2)
    s={'provider':'辉立暗盘','stock_code':'01234','issuer':'示例公司','trade_date':'2026-11-23',
       'listing_date':'2026-11-24','session_start':start.isoformat(),'session_end':end.isoformat(),
       'schedule_source_url':'https://www.poems.com.hk/zh-hk/product-and-service/initial-public-offerings/ipo-info/'}
    raw={'status':1,'time':int(captured.timestamp()*1000),'stocks':[{'s':'01234.PH','i':'5m',
         'lut':int(end.timestamp()*1000),'data':[[int((start+timedelta(minutes=i*5)).timestamp()*1000),11] for i in range(1,28)]}]}
    f={'P1':'1234','P3':'示例公司','P5':'11','P6':'10','P9':'12','P10':'13','P11':'9',
       'P13':'13.5K','P14':'149K','P15':'50','P16':'18:29'}
    return s,raw,f,captured


class PhillipTests(unittest.TestCase):
    def test_whole_27_five_minute_chart_endpoints_are_not_ohlc_bars(self):
        s,raw,f,captured=fixture();line=parse_price_line(raw,s,captured.isoformat())
        self.assertEqual(len(line['points']),27)
        self.assertEqual(line['points'][0]['time'],'2026-11-23T16:20:00+08:00')
        self.assertEqual(line['coverage']['interval_seconds'],300)
        self.assertNotIn('bars',line)

    def test_different_venue_missing_bucket_wrong_day_are_rejected(self):
        s,raw,f,captured=fixture();raw['stocks'][0]['s']='01234.FU'
        with self.assertRaisesRegex(GreySourceUnavailable,'symbol_or_interval'):parse_price_line(raw,s,captured.isoformat())
        s,raw,f,captured=fixture();raw['stocks'][0]['data'].pop(5)
        with self.assertRaisesRegex(GreySourceUnavailable,'sampling'):parse_price_line(raw,s,captured.isoformat())

    def test_official_current_public_guest_parameters_are_dynamic_not_constants(self):
        html=b'pmp.create_connectionByUserName("https://pmp06.poems.com.hk/", {subscribeHandler: handler}, "example-visitor", "example-pass");'
        host,user,password,scrubbed=public_guest_parameters(html)
        self.assertEqual(user,'example-visitor');self.assertNotIn(b'example-visitor',scrubbed)
        self.assertNotIn(b'example-pass',scrubbed)
        with self.assertRaises(GreySourceUnavailable):public_guest_parameters(html.replace(b'pmp06.poems',b'evil.example'))

    def test_current_broker_page_must_actually_link_requested_public_quote(self):
        page=b'<a href="https://research.poems.com.hk/page/Phillip/share_recommend/PMP_1/ZH/streaming.asp?code=1234">quote</a>'
        self.assertIn('code=1234',public_ajax_url(page,'01234'))
        with self.assertRaisesRegex(GreySourceUnavailable,'link_missing'):public_ajax_url(page,'09999')

    def test_official_quote_is_preserved_and_combines_with_sampled_line(self):
        s,raw,f,captured=fixture()
        q=parse_public_quote(f,s,'Mon, 23 Nov 2026 10:32:00 GMT',captured.isoformat(),
          'https://research.poems.com.hk/page/Phillip/share_recommend/PMP_1/ZH/streaming.asp?code=1234')
        self.assertEqual(q['ohlc']['open'],'12');self.assertEqual(q['lot_size'],50)
        self.assertNotIn('volume',q);self.assertEqual(q['volume_display'],'13.5K')
        result=combine_line_and_quote(s,parse_price_line(raw,s,captured.isoformat()),q,captured)
        self.assertEqual(result['series_kind'],'sampled_price_line')
        self.assertTrue(result['ohlc_crosscheck']['confirmed'])

    def test_stale_quote_date_and_inconsistent_price_do_not_pass(self):
        s,raw,f,captured=fixture()
        with self.assertRaisesRegex(GreySourceUnavailable,'current_day'):
            parse_public_quote(f,s,'Sun, 22 Nov 2026 10:32:00 GMT',captured.isoformat(),'https://research.poems.com.hk/')
        f['P11']='20'
        with self.assertRaisesRegex(GreySourceUnavailable,'ohlc_conflict'):
            parse_public_quote(f,s,'Mon, 23 Nov 2026 10:32:00 GMT',captured.isoformat(),'https://research.poems.com.hk/')

    def test_missing_stream_date_uses_actual_public_page_http_date(self):
        s,raw,f,captured=fixture()
        class Reader:
            opener=object()
            last_http_date='Mon, 23 Nov 2026 10:32:00 GMT'
            def __call__(self,url):
                if 'streaming.asp' in url:
                    return b'pmp.create_connectionByUserName("https://pmp06.poems.com.hk/", {subscribeHandler: h}, "example-visitor", "example-pass");'
                return b'<a href="https://research.poems.com.hk/page/Phillip/share_recommend/PMP_1/ZH/streaming.asp?code=1234">quote</a>'
        pmp=Mock(return_value=(f,None,{'id':'data','val':[]}))
        with tempfile.TemporaryDirectory() as tmp:
            result=collect_live_quote(s,tmp,now=captured,reader=Reader(),pmp_reader=pmp)
        self.assertEqual(result['source_http_date'],'Mon, 23 Nov 2026 10:32:00 GMT')
        self.assertEqual(result['lot_size'],50)

    def test_pdf_link_must_be_discovered_and_match_exact_day_code_final(self):
        page=b'<a href="https://research.cyberquote.com.hk/PhillipMart/Files/20261123-01234C_Final.pdf">final</a>'
        self.assertIn('01234C_Final',final_pdf_url(page,'01234','2026-11-23'))
        with self.assertRaisesRegex(GreySourceUnavailable,'not_yet'):final_pdf_url(page,'01234','2026-11-24')

    def test_no_chart_http_before_close(self):
        s,raw,f,captured=fixture();fetcher=Mock()
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(GreySourceUnavailable,'not_closed'):
                fetch_price_line(s,tmp,now=timestamp_before(),fetcher=fetcher)
        fetcher.assert_not_called()

    def test_public_pmp_closes_after_first_matching_quote_and_never_archives_login(self):
        s,raw,f,captured=fixture()
        class Response:
            def __init__(self,chunks):self.chunks=iter(chunks);self.headers={'Date':'Mon, 23 Nov 2026 10:32:00 GMT'};self.closed=False
            def read1(self,n):return next(self.chunks,b'')
            def close(self):self.closed=True
            def __enter__(self):return self
            def __exit__(self,*args):self.close()
        login=b'<script>l({"id":"login","rc":0,"sid":"synthetic-session","cid":"synthetic-connection"});</script>'
        message={'id':'data','val':[{'tpc':'\\RealTime\\PFeeder\\1234','item':[{'val':f}]}]}
        stream=Response([login,('<script>u('+json.dumps(message)+');</script>').encode()]);ack=Response([b'ok'])
        opener=Mock();opener.open.side_effect=[stream,ack]
        quote,date,application=read_public_pmp('https://pmp06.poems.com.hk/','visitor','guest','01234',opener,'https://research.poems.com.hk/')
        self.assertTrue(stream.closed);self.assertEqual(quote['P5'],'11')
        self.assertNotIn('sid',json.dumps(application));self.assertNotIn('pwd',json.dumps(application))
        self.assertIn('multipart/form-data',opener.open.call_args_list[0].args[0].headers['Content-type'])


def timestamp_before():return datetime(2026,11,23,18,29,tzinfo=HK)


if __name__=='__main__':unittest.main()
