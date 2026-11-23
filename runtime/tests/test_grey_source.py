"""Synthetic offline source-contract tests; never contact a broker."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from hkipo.grey_market import HK
from hkipo.grey_source import (GreySourceUnavailable, PROVIDER, SOURCE_URL,
    normalize_session, page_identity, source_wall_time, collect_session, _quote_once)


def fixture():
    start=datetime(2026,11,23,16,15,tzinfo=HK)
    end=start+timedelta(minutes=135)
    session={'provider':PROVIDER,'stock_code':'01234','issuer':'测试发行人',
        'trade_date':'2026-11-23','listing_date':'2026-11-24',
        'session_start':start.isoformat(),'session_end':end.isoformat(),
        'schedule_source_url':'https://www.futuhk.com/support/topic2_303'}
    def epoch(dt):return int(dt.replace(tzinfo=timezone.utc).timestamp()*1000)
    rows=[{'T':epoch(start-timedelta(minutes=1)),'FI':10,'H':10,'L':10,'LA':10,'V':0}]
    rows += [{'T':epoch(start+timedelta(minutes=i)),'FI':10 if i==0 else 11,
              'H':12 if i==0 else 11,'L':10 if i==0 else 11,'LA':11,'V':1} for i in range(135)]
    quote={'BC':'2','S':'01234','E':'HKG','CY':'HKD','LS':50,'NECN':'测试发行人',
        'TS':epoch(end-timedelta(minutes=1)),'O':12,'H':12,'L':10,'N':11,'C':10,'V':'135','T':'1.49K'}
    return session,quote,{'RT':'S','REF':'','Object':rows},end+timedelta(minutes=2)


class SourceContractTests(unittest.TestCase):
    def test_source_wall_clock_is_not_double_shifted_to_next_day(self):
        source=datetime(2026,11,23,16,15,tzinfo=timezone.utc)
        t=source_wall_time(int(source.timestamp()*1000))
        self.assertEqual(t.isoformat(),'2026-11-23T16:15:00+08:00')

    def test_page_maps_broker_two_to_futu_and_names_the_requested_security(self):
        page=b'<select id="broker"><option value="2">\xe5\xaf\x8c\xe9\x80\x94</option></select><select id="stockCode"><option value="01234">test</option></select>'
        self.assertIn('01234',page_identity(page,'01234')['stockCode'])
        with self.assertRaisesRegex(GreySourceUnavailable,'broker_mapping'):
            page_identity(page.replace(b'value="2"',b'value="1"'),'01234')
        with self.assertRaisesRegex(GreySourceUnavailable,'not_in_current'):
            page_identity(page,'09999')

    def test_full_minutes_and_independent_actual_open_are_kept_separate(self):
        session,quote,minutes,captured=fixture()
        result=normalize_session(session,quote,minutes,captured.isoformat())
        self.assertEqual(result['ohlc']['open'],'12')
        self.assertEqual(result['bars'][0]['open'],'10')
        self.assertEqual(result['reference_anchor_open'],'10')
        self.assertEqual(result['opening_basis'],'official_quote_day_open')
        self.assertEqual(result['volume'],'135')
        self.assertEqual(result['discarded_preopen_anchor_count'],1)
        self.assertEqual(len(result['bars']),135)
        self.assertNotIn('turnover',result) # a rounded display is not exact turnover

    def test_partial_minute_series_cannot_claim_a_full_session(self):
        session,quote,minutes,captured=fixture()
        minutes['Object'].pop(30)
        with self.assertRaisesRegex(GreySourceUnavailable,'coverage'):
            normalize_session(session,quote,minutes,captured.isoformat())

    def test_snapshot_and_minutes_high_or_close_disagreement_stays_blocked(self):
        for key in ['H','N']:
            session,quote,minutes,captured=fixture();quote[key]=13
            with self.subTest(key=key),self.assertRaisesRegex(GreySourceUnavailable,'day_price_conflict'):
                normalize_session(session,quote,minutes,captured.isoformat())

    def test_platform_and_date_never_mix(self):
        for key,value in [('BC','1'),('S','09999')]:
            session,quote,minutes,captured=fixture();quote[key]=value
            with self.subTest(key=key),self.assertRaisesRegex(GreySourceUnavailable,'mismatch'):
                normalize_session(session,quote,minutes,captured.isoformat())
        session,quote,minutes,captured=fixture()
        quote['TS']+=86400000
        with self.assertRaisesRegex(GreySourceUnavailable,'date_or_time'):
            normalize_session(session,quote,minutes,captured.isoformat())

    def test_missing_volume_rows_cannot_pass_rounded_quote_count_check(self):
        session,quote,minutes,captured=fixture();quote['V']='1.5K'
        with self.assertRaisesRegex(GreySourceUnavailable,'volume_conflict'):
            normalize_session(session,quote,minutes,captured.isoformat())

    def test_no_network_before_the_official_close(self):
        session,quote,minutes,captured=fixture();client=Mock()
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(GreySourceUnavailable,'not_closed'):
                collect_session(session,tmp,now=datetime(2026,11,23,18,29,tzinfo=HK),client=client)
        client.request.assert_not_called()

    def test_finite_quote_connection_registers_only_prices_and_always_aborts(self):
        session,quote,minutes,captured=fixture();client=Mock()
        messages=[{'H':'GreyHub','M':'updateFeed','A':[quote]}]
        replies=[{'ConnectionToken':'short-test-token'},{'C':'cursor','S':1,'M':[]},
                 {'Response':'started'},{'I':'0'},{'M':messages},{}]
        client.request.side_effect=[json.dumps(v).encode() for v in replies]
        value,application=_quote_once(client,'01234')
        self.assertEqual(value['BC'],'2');self.assertEqual(application[0]['M'],'updateFeed')
        calls=client.request.call_args_list
        self.assertEqual(calls[-1].args[0],'/signalr/abort')
        send=next(c for c in calls if c.args[0]=='/signalr/send')
        self.assertEqual(json.loads(send.kwargs['form']['data'])['M'],'Register')
        self.assertEqual(sum(c.args[0]=='/signalr/poll' for c in calls),1)


if __name__=='__main__':unittest.main()
