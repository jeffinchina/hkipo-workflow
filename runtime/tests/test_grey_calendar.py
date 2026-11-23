from datetime import datetime
from pathlib import Path
import unittest
from hkipo.grey_calendar import parse_calendar, CalendarSourceError, SCHEDULE_SOURCE_URL

FIXTURE = Path(__file__).parent / 'fixtures/grey-calendar-synthetic.html'
NOW = '2026-11-23T16:00:00+08:00'


class GreyCalendarTests(unittest.TestCase):
    def setUp(self):
        self.html = FIXTURE.read_text()

    def test_explicit_dates_rowspan_and_price_range(self):
        rows = parse_calendar(self.html, NOW)
        self.assertEqual([r['stock_code'] for r in rows], ['05432', '01234'])
        self.assertEqual(rows[0]['session_start'], '2026-11-23T16:15:00+08:00')
        self.assertEqual(rows[0]['session_end'], '2026-11-23T18:30:00+08:00')
        self.assertEqual(rows[1]['session_end'], '2026-12-03T18:30:00+08:00')
        self.assertEqual(rows[1]['listing_date'], '2026-12-04')
        self.assertEqual(rows[1]['ipo_price'], '12.07-15.69')
        self.assertIsNone(rows[1]['issue_price'])
        self.assertIsNone(rows[1]['lot_size'])
        self.assertEqual(rows[1]['schedule_source_url'], SCHEDULE_SOURCE_URL)

    def test_half_day_times_come_from_source_and_are_not_a_calendar_guess(self):
        body = self.html.replace('下午 4時15分 至 6時30分', '下午2時15分至4時30分')
        row = parse_calendar(body, NOW)[0]
        self.assertEqual(row['session_start'], '2026-11-23T14:15:00+08:00')
        self.assertEqual(row['session_end'], '2026-11-23T16:30:00+08:00')

    def test_previously_traded_session_is_not_replaced_by_previous_business_day(self):
        body = self.html.replace('2026<br>11-24', '2026<br>11-27')
        row = parse_calendar(body, NOW)[0]
        self.assertEqual(row['trade_date'], '2026-11-23')
        self.assertEqual(row['listing_date'], '2026-11-27')

    def test_strict_headers_caption_and_complete_cells(self):
        for old, new in [('輝立交易場啟動日期', '配發日期'),
                         ('<caption>輝立交易場時間表</caption>', ''),
                         ('rowspan="2"', 'rowspan="3"'),
                         ('rowspan="2"', 'colspan="2"'),
                         ('<td>2026年12月3日</td>', '')]:
            with self.subTest(old=old), self.assertRaises(CalendarSourceError):
                parse_calendar(self.html.replace(old, new), NOW)

    def test_identity_invalid_date_and_ambiguous_time_rejected(self):
        for old, new in [('>2026年11月23日<', '>2026年11月31日<'),
                         ('>2026年11月23日<', '>2026年11月24日<'),
                         ('下午 4時15分 至 6時30分', '4:15-6:30'),
                         ('12.07-15.69', '15.69-12.07'),
                         ('<td>5432</td><td>示例服務股份有限公司</td><td>2026年',
                          '<td>5432</td><td>另一家公司</td><td>2026年')]:
            with self.subTest(old=old), self.assertRaises(CalendarSourceError):
                parse_calendar(self.html.replace(old, new), NOW)

    def test_real_page_duplicate_sections_must_be_identical(self):
        start = self.html.index('<table class="ipo-phillipmart-calendar-items">')
        table = self.html[start:self.html.index('</table>', start) + len('</table>')]
        self.assertEqual(len(parse_calendar(self.html + table, NOW)), 2)
        with self.assertRaises(CalendarSourceError):
            parse_calendar(self.html + table.replace('2026年12月3日', '2026年12月2日'), NOW)

    def test_timezone_aware_capture_required(self):
        for value in (None, '2026-11-23T16:00:00', datetime(2026, 11, 23)):
            with self.subTest(value=value), self.assertRaises(CalendarSourceError):
                parse_calendar(self.html, value)

    def test_missing_table_does_not_look_like_valid_empty_day(self):
        with self.assertRaises(CalendarSourceError):
            parse_calendar('<html>Temporarily unavailable</html>', NOW)


if __name__ == '__main__':
    unittest.main()
