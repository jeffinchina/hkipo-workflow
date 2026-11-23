from datetime import datetime
from decimal import Decimal
import unittest
from hkipo.grey_market import calculate, commentary, display, report_text, validate_snapshot, GreyDataError

NOW=datetime.fromisoformat('2026-11-23T18:35:00+08:00')

def snapshot():
    return {'schema_version':1,'provider':'测试券商','market':'grey','stock_code':'01234','issuer':'示例公司',
            'currency':'HKD','issue_price':'10','lot_size':100,'trade_date':'2026-11-23','listing_date':'2026-11-24',
            'session_start':'2026-11-23T16:15:00+08:00','session_end':'2026-11-23T18:30:00+08:00',
            'captured_at':'2026-11-23T18:31:00+08:00','closed':True,
            'source_url':'https://www.poems.com.hk/quote','schedule_source_url':'https://www.poems.com.hk/calendar',
            'coverage':{'complete':True,'has_more':False,'requested_start':'2026-11-23T16:15:00+08:00','requested_end':'2026-11-23T18:30:00+08:00'},
            'ohlc':{'open':'10','high':'15','low':'9','close':'12'},
            'bars':[{'time':'2026-11-23T16:15:00+08:00','open':'10','high':'11','low':'9','close':'10'},
                    {'time':'2026-11-23T17:00:00+08:00','open':'10','high':'15','low':'10','close':'13'},
                    {'time':'2026-11-23T18:29:00+08:00','open':'13','high':'13','low':'11','close':'12'}]}


class GreyMarketTests(unittest.TestCase):
    def test_returns_and_per_lot_gains_are_relative_to_issue_price(self):
        out=calculate(snapshot(),NOW)
        self.assertEqual(Decimal(out['close']['change_pct']),20)
        self.assertEqual(Decimal(out['low']['change_pct']),-10)
        self.assertEqual(Decimal(out['close']['pnl_per_lot']),200)
        self.assertEqual(Decimal(out['max_profit_per_lot']),500)
        self.assertEqual(Decimal(out['max_loss_per_lot']),100)
        self.assertFalse(out['fees_included']);self.assertFalse(out['actual_trade'])

    def test_display_rounds_without_losing_internal_precision(self):
        self.assertEqual(display(Decimal('1.235')),'1.24')
        self.assertEqual(display(Decimal('-1.235')),'-1.24')
        self.assertEqual(display(Decimal('-0.001')),'0')
        self.assertEqual(display(Decimal('100.00')),'100')
        self.assertEqual(display(Decimal('12.50'),True),'+12.5')
        s=snapshot();s['issue_price']='7.77';out=calculate(s,NOW)
        self.assertGreater(len(out['close']['change_pct'].split('.')[1]),2)

    def test_day_ohlc_and_full_series_must_agree(self):
        for key in ('open','high','low','close'):
            s=snapshot();s['ohlc'][key]='19'
            with self.subTest(key=key),self.assertRaises(GreyDataError):calculate(s,NOW)
        s=snapshot();s['bars']=s['bars'][1:]
        with self.assertRaises(GreyDataError):calculate(s,NOW)

    def test_missing_coverage_or_series_cannot_pass(self):
        for edit in [lambda s:s['coverage'].update(complete=False),lambda s:s['coverage'].update(has_more=True),
                     lambda s:s['coverage'].pop('requested_end'),lambda s:s.update(bars=[]),lambda s:s.update(closed=False)]:
            s=snapshot();edit(s)
            with self.assertRaises(GreyDataError):validate_snapshot(s,NOW)

    def test_wrong_day_timezone_order_and_live_quote_rejected(self):
        for edit in [lambda s:s.update(captured_at='2026-11-23T18:29:00+08:00'),
                     lambda s:s.update(captured_at='2026-11-23T18:31:00'),
                     lambda s:s['bars'].reverse(),
                     lambda s:s['bars'][0].update(time='2026-11-22T16:15:00+08:00')]:
            s=snapshot();edit(s)
            with self.assertRaises(GreyDataError):validate_snapshot(s,NOW)
        with self.assertRaises(GreyDataError):validate_snapshot(snapshot(),datetime.fromisoformat('2026-11-23T18:00:00+08:00'))

    def test_source_reference_and_lot_size_are_required(self):
        for change in [{'source_url':'https://social.example.invalid/tip'},{'source_url':'https://www.poems.com.hk@evil.invalid/'},
                       {'issue_price':None},{'issue_price':'NaN'},{'issue_price':1.1},{'lot_size':True},
                       {'lot_size':0},{'currency':'CNY'}]:
            with self.subTest(change=change),self.assertRaises(GreyDataError):validate_snapshot(dict(snapshot(),**change),NOW)

    def test_report_keeps_only_requested_fields_and_names_pending_material(self):
        text=report_text(snapshot(),now=NOW)
        for field in ('开盘','收盘','最高','最低','一手最高浮盈','最大浮亏','正式配发公告尚未公布','走势简评'):
            self.assertIn(field,text)
        for field in ('成交额','振幅','高点至收盘','VWAP'):
            self.assertNotIn(field,text)
        self.assertIn('概要数据尚在核验',report_text(snapshot(),{'status':'published_pending_review'},NOW))
        self.assertIn('公开认购100倍',report_text(snapshot(),{'status':'verified','summary':'公开认购100倍'},NOW))

    def test_max_loss_and_profit_never_become_negative_labels(self):
        s=snapshot();s['issue_price']='8';out=calculate(s,NOW)
        self.assertEqual(out['max_loss_per_lot'],'0')
        s['issue_price']='20';out=calculate(s,NOW)
        self.assertEqual(out['max_profit_per_lot'],'0')
        self.assertEqual(Decimal(out['max_loss_per_lot']),1100)

    def test_commentary_never_invents_personal_trades(self):
        text=commentary(snapshot(),calculate(snapshot(),NOW))
        self.assertIn('16:15',text);self.assertIn('17:00',text)
        for forbidden in ('我中','已卖','已经走','稳赚','明天必涨'):self.assertNotIn(forbidden,text)

    def test_flat_prices_are_described_as_flat(self):
        s=snapshot();s['ohlc']={k:'10' for k in ('open','high','low','close')}
        for b in s['bars']:b.update({k:'10' for k in ('open','high','low','close')})
        self.assertIn('全程持平',commentary(s,calculate(s,NOW)))

if __name__=='__main__':unittest.main()
