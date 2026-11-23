"""Post-close grey-market facts and arithmetic; never mix broker sessions.

All monetary calculations use exact Decimal values. Rendered numbers use at most
2 decimal places. This module has no network, scheduling or delivery side effects.
"""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.parse import urlsplit
import hashlib
import json
from zoneinfo import ZoneInfo

HK = ZoneInfo('Asia/Hong_Kong')
OFFICIAL_HOSTS = {'www.poems.com.hk', 'www1.poems.com.hk', 'trading.phillipmart.com.hk',
                  'www.futunn.com', 'quote.futunn.com', 'www.futuhk.com',
                  'www.bsgroup.com.hk', 'df.chiefgroup.com.hk', 'www.aastocks.com',
                  'ds1.aastocks.com', 'research.poems.com.hk', 'pmp06.poems.com.hk', 'research.cyberquote.com.hk'}

class GreyDataError(ValueError):
    """Incomplete, conflicting or non-final data may never be described as final."""


def timestamp(value):
    try:
        result=datetime.fromisoformat(value)
        if result.tzinfo is None or result.utcoffset() is None:raise ValueError()
        return result.astimezone(HK)
    except (TypeError,ValueError):raise GreyDataError('timezone_aware_timestamp_required') from None


def decimal(value, *, zero=False):
    if not isinstance(value,(str,int)) or isinstance(value,bool):raise GreyDataError('exact_decimal_required')
    try:result=Decimal(value)
    except (InvalidOperation,ValueError):raise GreyDataError('invalid_number') from None
    if not result.is_finite() or result<0 or (not zero and result==0) or result>Decimal('1e18'):
        raise GreyDataError('number_out_of_range')
    return result


def official_url(url, hosts=OFFICIAL_HOSTS):
    try:p=urlsplit(url)
    except (ValueError,TypeError):raise GreyDataError('official_source_required') from None
    if p.scheme!='https' or p.hostname not in hosts or p.username or p.password or p.port not in (None,443):
        raise GreyDataError('official_source_required')
    return url


def display(value, signed=False):
    result=Decimal(value).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)
    if result==0:result=abs(result)
    text=format(result,'f').rstrip('0').rstrip('.')
    if text in ('','-0'):text='0'
    return ('+' if signed and result>0 else '')+text


def validate_snapshot(value, now=None):
    """Require closed day OHLC and a complete, same-platform intraday series.

    The source adapter must establish terminal pagination and requested date/range;
    a quote or screenshot alone cannot establish complete intraday coverage.
    """
    if not isinstance(value,dict) or value.get('schema_version')!=1:raise GreyDataError('snapshot_schema')
    if value.get('currency')!='HKD' or not value.get('provider') or value.get('market')!='grey':raise GreyDataError('market_identity')
    code=value.get('stock_code','')
    if not isinstance(code,str) or len(code)!=5 or not code.isdigit() or not value.get('issuer'):raise GreyDataError('security_identity')
    official_url(value.get('source_url'));official_url(value.get('schedule_source_url'))
    try:
        day=date.fromisoformat(value['trade_date']);listing=date.fromisoformat(value['listing_date'])
    except (KeyError,ValueError,TypeError):raise GreyDataError('session_dates') from None
    start,end,captured=(timestamp(value.get(k)) for k in ('session_start','session_end','captured_at'))
    if start.date()!=day or end.date()!=day or not start<end or day>=listing:raise GreyDataError('session_dates')
    current=timestamp(now.isoformat()) if isinstance(now,datetime) else timestamp(now) if now else datetime.now(HK)
    if current<end or captured<end or captured>current:raise GreyDataError('session_not_closed')
    if value.get('closed') is not True:raise GreyDataError('source_final_status_required')
    coverage=value.get('coverage',{})
    if (coverage.get('complete') is not True or coverage.get('has_more') is not False
            or timestamp(coverage.get('requested_start'))!=start
            or timestamp(coverage.get('requested_end'))!=end):raise GreyDataError('full_session_coverage_required')
    lot=value.get('lot_size')
    if type(lot) is not int or not 1<=lot<=1_000_000:raise GreyDataError('lot_size_required')
    decimal(value.get('issue_price'))
    if value.get('series_kind')=='sampled_price_line':
        points=value.get('points')
        if not isinstance(points,list) or not points:raise GreyDataError('intraday_series_required')
        interval=coverage.get('interval_seconds')
        if interval!=300:raise GreyDataError('known_sampling_interval_required')
        from datetime import timedelta
        expected=[start+timedelta(seconds=interval*n) for n in range(1,int((end-start).total_seconds()/interval)+1)]
        times=[timestamp(point.get('time')) for point in points]
        if times!=expected:raise GreyDataError('full_sampled_session_coverage_required')
        summary=value.get('ohlc',{})
        o,h,l,c=[decimal(summary.get(k)) for k in ('open','high','low','close')]
        prices=[decimal(point.get('price')) for point in points]
        cross=value.get('ohlc_crosscheck',{})
        if cross.get('confirmed') is not True:raise GreyDataError('independent_broker_ohlc_required')
        official_url(cross.get('source_url'))
        if urlsplit(cross['source_url']).hostname not in {'pmp06.poems.com.hk','research.poems.com.hk','www.poems.com.hk','research.cyberquote.com.hk'}:
            raise GreyDataError('phillip_official_ohlc_required')
        if not l<=min(o,c)<=max(o,c)<=h or any(p<l or p>h for p in prices) or prices[-1]!=c:
            raise GreyDataError('sampled_series_and_day_quote_conflict')
        return value
    bars=value.get('bars')
    if not isinstance(bars,list) or not 1<=len(bars)<=100_000:raise GreyDataError('intraday_series_required')
    previous=None;prices=[]
    for bar in bars:
        t=timestamp(bar.get('time'))
        if not start<=t<=end or (previous is not None and t<=previous):raise GreyDataError('intraday_time_order')
        previous=t
        o,h,l,c=[decimal(bar.get(k)) for k in ('open','high','low','close')]
        if not l<=min(o,c)<=max(o,c)<=h:raise GreyDataError('bar_ohlc_conflict')
        if 'volume' in bar:decimal(bar['volume'],zero=True)
        prices.append((o,h,l,c))
    summary=value.get('ohlc',{})
    observed=(prices[0][0],max(p[1] for p in prices),min(p[2] for p in prices),prices[-1][3])
    if value.get('opening_basis')=='official_quote_day_open':
        # The official chart FI can inherit the issue-price reference in the
        # first minute. Preserve it as source data; actual day open is the
        # separately fetched same-platform quote O, never that reference.
        raw=value.get('raw_source_first_bar',{})
        if (urlsplit(value['source_url']).hostname!='df.chiefgroup.com.hk'
                or decimal(value.get('reference_anchor_open'))!=prices[0][0]
                or decimal(raw.get('FI'))!=prices[0][0]):raise GreyDataError('opening_reference_evidence_required')
        actual_open=decimal(summary.get('open'))
        if not observed[2]<=actual_open<=observed[1]:raise GreyDataError('day_open_outside_range')
        observed=(actual_open,observed[1],observed[2],observed[3])
    if tuple(decimal(summary.get(k)) for k in ('open','high','low','close'))!=observed:
        raise GreyDataError('intraday_and_day_ohlc_conflict')
    if 'volume' in value:
        volume=decimal(value['volume'],zero=True)
        if all('volume' in b for b in bars) and sum(decimal(b['volume'],zero=True) for b in bars)!=volume:
            raise GreyDataError('intraday_and_day_volume_conflict')
    if 'turnover' in value:decimal(value['turnover'],zero=True)
    return value


def validate_quote(value,now=None):
    """A same-day official final quote may survive a missing intraday series."""
    if value.get('status')!='quote_only' or value.get('currency')!='HKD' or value.get('market')!='grey':raise GreyDataError('quote_schema')
    official_url(value.get('source_url'));official_url(value.get('schedule_source_url'))
    start,end,captured=[timestamp(value.get(k)) for k in ('session_start','session_end','captured_at')]
    current=timestamp(now.isoformat()) if isinstance(now,datetime) else timestamp(now) if now else datetime.now(HK)
    if (value.get('closed') is not True or current<end or captured<end or captured>current
            or start.date().isoformat()!=value.get('trade_date') or end.date()!=start.date()):raise GreyDataError('quote_not_final')
    if not isinstance(value.get('stock_code'),str) or len(value['stock_code'])!=5 or not value['stock_code'].isdigit():raise GreyDataError('security_identity')
    if type(value.get('lot_size')) is not int or not 1<=value['lot_size']<=1_000_000:raise GreyDataError('lot_size_required')
    decimal(value.get('issue_price'))
    o,h,l,c=[decimal(value.get('ohlc',{}).get(k)) for k in ('open','high','low','close')]
    if not l<=min(o,c)<=max(o,c)<=h:raise GreyDataError('day_ohlc_conflict')
    return value


def calculate(snapshot, now=None):
    return _calculate_validated(validate_snapshot(snapshot,now))


def _calculate_validated(value):
    issue=decimal(value['issue_price']);lot=value['lot_size'];out={}
    for key in ('open','close','high','low'):
        price=decimal(value['ohlc'][key]);out[key]={'price':str(price),
            'change_pct':str((price/issue-1)*100),'pnl_per_lot':str((price-issue)*lot)}
    high,low,close,opening=[decimal(value['ohlc'][k]) for k in ('high','low','close','open')]
    out.update(max_profit_per_lot=str(max(Decimal(0),(high-issue)*lot)),
               max_loss_per_lot=str(max(Decimal(0),(issue-low)*lot)),
               reference='issue_price',currency='HKD',fees_included=False,
               actual_trade=False,lot_size=lot,issue_price=str(issue))
    return out


def commentary(snapshot, facts):
    """Two short retrospective observations grounded in the complete series."""
    if snapshot.get('series_kind')=='sampled_price_line':
        prices=[decimal(p['price']) for p in snapshot['points']]
        opening,close=[decimal(snapshot['ohlc'][k]) for k in ('open','close')]
        if max(prices)==min(prices):shape='分时图采样价格变化不大，'
        elif prices[-1]<prices[0]:shape='分时图显示开盘后整体走弱，'
        elif prices[-1]>prices[0]:shape='分时图显示开盘后整体回升，'
        else:shape='分时图显示盘中有所反复，'
        shape+=('收盘高于开盘。' if close>opening else '收盘低于开盘。' if close<opening else '收盘与开盘持平。')
        return shape+f'相对发行价收盘{display(facts["close"]["change_pct"],True)}%，一手账面盈亏{display(facts["close"]["pnl_per_lot"],True)}港元。'
    bars=snapshot['bars'];hi=decimal(snapshot['ohlc']['high']);lo=decimal(snapshot['ohlc']['low'])
    hindex=next(i for i,b in enumerate(bars) if decimal(b['high'])==hi)
    lindex=next(i for i,b in enumerate(bars) if decimal(b['low'])==lo)
    close=decimal(snapshot['ohlc']['close']);opening=decimal(snapshot['ohlc']['open'])
    hi_time=timestamp(bars[hindex]['time']).strftime('%H:%M')
    lo_time=timestamp(bars[lindex]['time']).strftime('%H:%M')
    if hi==lo:shape='暗盘价格全程持平。'
    elif hindex<lindex and close<opening:shape=f'{hi_time}见高点后走弱，{lo_time}触及日内低位，收盘低于开盘。'
    elif lindex<hindex and close>opening:shape=f'{lo_time}见低点后走高，{hi_time}触及日内高位，收盘高于开盘。'
    elif close<hi and close<opening:shape='盘中有冲高，但收盘回落至开盘价下方。'
    elif close>opening:shape=f'收盘高于开盘，最高点出现在{hi_time}，尾盘未能守住高位。' if close<hi else f'收盘高于开盘，并收在日内高位。'
    else:shape=f'日内高点出现在{hi_time}、低点出现在{lo_time}，收盘与开盘持平。'
    return shape+f'相对发行价收盘{display(facts["close"]["change_pct"],True)}%，一手账面盈亏{display(facts["close"]["pnl_per_lot"],True)}港元。'


def report_text(snapshot, allotment=None, now=None):
    partial=snapshot.get('status')=='quote_only'
    facts=_calculate_validated(validate_quote(snapshot,now)) if partial else calculate(snapshot,now)
    lines=[f'{snapshot["issuer"]}（{snapshot["stock_code"]}）暗盘收盘资料',
           f'{snapshot["trade_date"]}｜{snapshot["provider"]}｜港元',
           f'发行价：{display(facts["issue_price"])}；每手：{facts["lot_size"]}股','']
    for key,label in [('open','开盘'),('close','收盘'),('high','最高'),('low','最低')]:
        p=facts[key];lines.append(f'{label}：{display(p["price"])}（{display(p["change_pct"],True)}%）；一手{display(p["pnl_per_lot"],True)}港元')
    lines += [f'一手最高浮盈：{display(facts["max_profit_per_lot"])}港元；最大浮亏：{display(facts["max_loss_per_lot"])}港元']
    note='已取得官方开收高低，完整分时尚未取得或未通过交叉核验；暂不判断盘中先后走势。' if partial else commentary(snapshot,facts)
    lines+=['','走势简评：'+note,'',
            '配发概要：'+(allotment['summary'] if allotment and allotment.get('status')=='verified' else
                         '正式配发公告尚未公布。' if not allotment or allotment.get('status')=='not_published' else
                         '当前发行期的配发公告发布状态尚在核验。' if allotment.get('status')=='publication_unverified' else '正式配发公告已发布，概要数据尚在核验。'),
            '', '盈亏均为按发行价中签一手的账面数，未扣费用，不代表实际成交收益。',
            '数据仅对应上述券商暗盘；不拼接其他平台的高低价或成交。',
            '行情来源：'+snapshot['source_url']]
    return '\n'.join(lines)
