"""Bounded anonymous post-close collection from Chief Securities' official page.

The page explicitly identifies broker=2 as Futu. Its minute endpoint is the
page's own full-chart request (last=0); quote metadata comes from one short
SignalR connection, closed immediately after the first matching updateFeed.
No login, account reads, order methods, subscription persistence or live
monitoring is implemented. Raw evidence remains in the supplied private folder.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from html.parser import HTMLParser
from http.cookiejar import CookieJar
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlencode, urlsplit
from urllib.request import (Request, build_opener, ProxyHandler,
                            HTTPCookieProcessor, HTTPRedirectHandler)
from .grey_market import HK, GreyDataError, timestamp, validate_snapshot, decimal

PROVIDER = '富途暗盘'
BASE = 'https://df.chiefgroup.com.hk'
SOURCE_URL = BASE + '/home/logon?language=3'
MINUTE_URL = BASE + '/Grey/GetGrey1MinsModel_V1'
RULES_URL = 'https://www.futuhk.com/support/topic2_303'
MAX_BODY = 8_000_000


class GreySourceUnavailable(GreyDataError):
    """A safe reason code; transport bodies/tokens never enter diagnostics."""


def _bad(code):
    raise GreySourceUnavailable(code) from None


def _json(data):
    try:
        return json.loads(data, parse_float=Decimal,
                          parse_constant=lambda value: _bad('nonfinite_source_value'))
    except (ValueError, TypeError):
        _bad('official_json_unreadable')


def _store(directory, name, data):
    """Retain source bytes by content hash; never overwrite earlier captures."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    digest = hashlib.sha256(data).hexdigest()
    path = directory / (name + '-' + digest + ('.html' if name.endswith('page') else '.json'))
    if not path.exists():
        path.write_bytes(data)
        path.chmod(0o600)
    return {'path': str(path.resolve()), 'sha256': digest}


class _SameHost(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        p = urlsplit(newurl)
        if p.scheme != 'https' or p.hostname != 'df.chiefgroup.com.hk' or p.username or p.password:
            _bad('official_redirect_rejected')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _HTTP:
    def __init__(self, proxy_url=None, timeout=15):
        proxies = {}
        if proxy_url:
            p = urlsplit(proxy_url)
            if (p.scheme != 'http' or p.hostname not in ('127.0.0.1', 'localhost', '::1')
                    or not p.port or p.username or p.password or p.path not in ('', '/')
                    or p.query or p.fragment):
                _bad('loopback_proxy_required')
            proxies = {'https': proxy_url}
        self.opener = build_opener(_SameHost(), ProxyHandler(proxies),
                                   HTTPCookieProcessor(CookieJar()))
        self.timeout = timeout

    def request(self, path, *, params=None, form=None):
        if not path.startswith('/') or path.startswith('//'):
            _bad('official_request_path')
        url = BASE + path + ('?' + urlencode(params) if params else '')
        headers = {'User-Agent': 'Mozilla/5.0', 'Referer': SOURCE_URL}
        data = None
        if form is not None:
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
            data = urlencode(form).encode()
        try:
            with self.opener.open(Request(url, data=data, headers=headers), timeout=self.timeout) as response:
                body = response.read(MAX_BODY + 1)
                if len(body) > MAX_BODY:
                    _bad('official_response_oversized')
                return body
        except GreySourceUnavailable:
            raise
        except Exception:
            _bad('official_source_unavailable')


class _Options(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.active = None
        self.value = None
        self.options = {'broker': {}, 'stockCode': {}}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'select':
            self.active = attrs.get('id') if attrs.get('id') in self.options else None
        elif tag == 'option' and self.active:
            self.value = attrs.get('value')
            self.options[self.active][self.value] = ''

    def handle_data(self, value):
        if self.active and self.value is not None:
            self.options[self.active][self.value] += value

    def handle_endtag(self, tag):
        if tag == 'option':
            self.value = None
        elif tag == 'select':
            self.active = self.value = None


def page_identity(body, stock_code):
    parser = _Options()
    try:
        parser.feed(body.decode('utf-8-sig'))
    except (UnicodeError, ValueError):
        _bad('official_page_unreadable')
    if parser.options['broker'].get('2', '').strip() != '富途':
        _bad('official_broker_mapping_changed')
    if stock_code not in parser.options['stockCode']:
        _bad('security_not_in_current_official_grey_list')
    return parser.options


def source_wall_time(value):
    """Source T/TS encode HK wall time in UTC milliseconds for Highcharts.

    The official page leaves Highcharts' useUTC=False option commented out.
    Its 1795450500000 point is displayed as 2026-11-23 16:15, not next-day
    00:15 Hong Kong time. Preserve raw timestamp and explicitly reinterpret
    the UTC calendar components as HK wall-clock values.
    """
    if type(value) is not int or not 0 < value < 10**14:
        _bad('official_timestamp_invalid')
    return datetime.fromtimestamp(value / 1000, timezone.utc).replace(tzinfo=HK)


def _dec(value, zero=False):
    # JSON Decimal retains source precision; common deterministic validator takes strings.
    if isinstance(value, Decimal):
        value = str(value)
    return decimal(value, zero=zero)


def _display_range(value):
    """Formatted K/M/B quote counts are checks, never exact raw fact values."""
    if not isinstance(value, str):
        _bad('rounded_quote_count_expected')
    match = re.fullmatch(r'([0-9]+(?:\.[0-9]+)?)([KMB]?)', value.replace(',', ''))
    if not match:
        _bad('rounded_quote_count_invalid')
    number, suffix = match.groups()
    factor = {'': 1, 'K': 1000, 'M': 1000000, 'B': 1000000000}[suffix]
    step = Decimal(10) ** (-len(number.partition('.')[2])) * factor
    return Decimal(number) * factor, step


def normalize_session(session, quote, minute_response, captured_at, *, page_evidence=None):
    """Cross-check one platform's official day quote against its full minute chart.

    No-data pre-open anchors are excluded; the first chart FI can inherit the
    issue price. Preserve that chart bar and use the independent quote O for
    actual day opening. The validator must understand this explicit basis.
    """
    code = session.get('stock_code')
    if not isinstance(code, str) or not re.fullmatch(r'\d{5}', code):
        _bad('security_identity')
    if session.get('provider') != PROVIDER:
        _bad('session_platform_mismatch')
    start, end = timestamp(session['session_start']), timestamp(session['session_end'])
    captured = timestamp(captured_at)
    if captured < end:
        _bad('session_not_closed')
    if ((start.strftime('%H:%M'), end.strftime('%H:%M')) not in
            {('16:15', '18:30'), ('14:15', '16:30')}):
        _bad('official_futu_session_hours_required')
    if quote.get('BC') != '2' or quote.get('S') != code or quote.get('E') != 'HKG' or quote.get('CY') != 'HKD':
        _bad('quote_platform_or_security_mismatch')
    quote_time = source_wall_time(quote.get('TS'))
    if quote_time.date().isoformat() != session['trade_date'] or not start <= quote_time <= end:
        _bad('quote_trade_date_or_time_mismatch')
    if type(quote.get('LS')) is not int:
        _bad('official_lot_size_required')
    if not isinstance(minute_response, dict) or minute_response.get('RT') != 'S' or not isinstance(minute_response.get('Object'), list):
        _bad('official_full_chart_unavailable')
    bars = []
    anchors = []
    raw_rows = minute_response['Object']
    for row in raw_rows:
        t = source_wall_time(row.get('T'))
        if t < start:
            if t == start - timedelta(minutes=1) and _dec(row.get('V'), zero=True) == 0:
                anchors.append(row)
                continue
            _bad('minute_outside_requested_day')
        if t > end or t.date().isoformat() != session['trade_date']:
            _bad('minute_outside_requested_day')
        o, h, l, c = [_dec(row.get(k)) for k in ('FI', 'H', 'L', 'LA')]
        v = _dec(row.get('V'), zero=True)
        bars.append({'time': t.isoformat(), 'open': str(o), 'high': str(h),
                     'low': str(l), 'close': str(c), 'volume': str(v),
                     'source_time_ms': row['T']})
    if len(anchors) > 1:
        _bad('multiple_preopen_anchors')
    if not bars:
        _bad('official_full_chart_empty')
    times = [timestamp(b['time']) for b in bars]
    # Full-chart all-history endpoint is unpaginated in the official JS.
    # Also require every completed session minute; a lone snapshot or truncated
    # response cannot claim full-session coverage. A final end-time point is optional.
    required = [start + timedelta(minutes=n) for n in range(int((end-start).total_seconds() // 60))]
    if times not in (required, required + [end]):
        _bad('full_minute_session_coverage_required')
    day = {k: str(_dec(quote.get(source))) for k, source in
           [('open', 'O'), ('high', 'H'), ('low', 'L'), ('close', 'N')]}
    issue = _dec(quote.get('C'))
    if session.get('issue_price') is not None and _dec(session['issue_price']) != issue:
        _bad('issue_price_conflict')
    if session.get('lot_size') is not None and session['lot_size'] != quote['LS']:
        _bad('lot_size_conflict')
    volume = sum(_dec(b['volume'], zero=True) for b in bars)
    center, step = _display_range(quote.get('V'))
    if not max(Decimal(0), center-step) <= volume <= center+step:
        _bad('minute_and_quote_volume_conflict')
    snapshot = dict(schema_version=1, provider=PROVIDER, market='grey', currency='HKD',
        stock_code=code, issuer=quote.get('NECN') or quote.get('NETW') or session['issuer'],
        source_url=SOURCE_URL, schedule_source_url=session['schedule_source_url'],
        trade_date=session['trade_date'], listing_date=session['listing_date'],
        session_start=start.isoformat(), session_end=end.isoformat(), captured_at=captured.isoformat(),
        closed=True, close_basis='official_session_elapsed_and_full_chart_day_quote_crosscheck',
        rule_source_url=RULES_URL, quote_time=quote_time.isoformat(),
        coverage={'complete': True, 'has_more': False, 'requested_start': start.isoformat(),
                  'requested_end': end.isoformat(), 'source_request': 'official_full_chart_last_zero',
                  'completed_minute_count': len(required), 'minute_endpoint': MINUTE_URL},
        lot_size=quote['LS'], issue_price=str(issue), ohlc=day, bars=bars, volume=str(volume),
        opening_basis='official_quote_day_open', reference_anchor_open=bars[0]['open'],
        raw_source_first_bar={k: str(v) if isinstance(v, Decimal) else v for k,v in raw_rows[len(anchors)].items()},
        source_timestamp_encoding='hk_wall_time_as_utc_milliseconds',
        quote_volume_display=quote.get('V'), quote_turnover_display=quote.get('T'),
        discarded_preopen_anchor_count=len(anchors), source_artifacts=page_evidence or {})
    # Exact independent H/L/last checks remain mandatory, even if the chart
    # reference anchor causes an irreconcilable first-minute low/high artifact.
    if (max(_dec(b['high']) for b in bars) != _dec(day['high']) or
            min(_dec(b['low']) for b in bars) != _dec(day['low']) or
            _dec(bars[-1]['close']) != _dec(day['close'])):
        _bad('minute_and_official_day_price_conflict')
    return validate_snapshot(snapshot, captured)


def _quote_once(client, stock_code):
    """A finite official public SignalR read, not a price-monitoring loop."""
    cd = json.dumps([{'name': 'greyhub'}], separators=(',', ':'))
    negotiate = _json(client.request('/signalr/negotiate', params={'clientProtocol': '2.1', 'connectionData': cd}))
    token = negotiate.get('ConnectionToken')
    if not isinstance(token, str) or not token:
        _bad('official_quote_connection_unavailable')
    params = {'transport': 'longPolling', 'clientProtocol': '2.1',
              'connectionToken': token, 'connectionData': cd}
    messages = []
    try:
        init = _json(client.request('/signalr/connect', params=params))
        if _json(client.request('/signalr/start', params=params)).get('Response') != 'started':
            _bad('official_quote_connection_unavailable')
        register = json.dumps({'H': 'greyhub', 'M': 'Register', 'A': [stock_code, '2'], 'I': 0}, separators=(',', ':'))
        client.request('/signalr/send', params=params, form={'data': register})
        poll = dict(params)
        if init.get('C'):
            poll['messageId'] = init['C']
        if init.get('G'):
            poll['groupsToken'] = init['G']
        for _ in range(3):
            response = _json(client.request('/signalr/poll', params=poll))
            application = response.get('M', [])
            if not isinstance(application, list):
                _bad('official_quote_messages_invalid')
            messages += application
            for message in application:
                if message.get('M', '').casefold() == 'updatefeed':
                    args = message.get('A', [])
                    if len(args) == 1 and args[0].get('BC') == '2' and args[0].get('S') == stock_code:
                        return args[0], messages
            if response.get('C'):
                poll['messageId'] = response['C']
            if response.get('G'):
                poll['groupsToken'] = response['G']
        _bad('official_quote_snapshot_missing')
    finally:
        try:
            client.request('/signalr/abort', params=params, form={})
        except Exception:
            pass


def collect_session(session, directory, *, proxy_url=None, now=None, client=None):
    """Fetch after the official close, validate before returning sendable facts."""
    current = timestamp(now.isoformat()) if isinstance(now, datetime) else timestamp(now) if now else datetime.now(HK)
    if current < timestamp(session['session_end']):
        _bad('session_not_closed')
    code = session.get('stock_code')
    if not isinstance(code, str) or not re.fullmatch(r'\d{5}', code):
        _bad('security_identity')
    root = Path(directory) / (session['trade_date'] + '-' + code)
    transport = client or _HTTP(proxy_url)
    page = transport.request('/home/logon', params={'language': '3'})
    page_identity(page, code)
    evidence = {'page': _store(root, 'official-page', page)}
    quote, messages = _quote_once(transport, code)
    # Store only application price messages. Connection tokens, groups/cursors,
    # cookies and handshake responses are intentionally not archived.
    application = json.dumps(messages, ensure_ascii=False, default=str).encode()
    evidence['quote_application'] = _store(root, 'quote-application', application)
    response = transport.request('/Grey/GetGrey1MinsModel_V1', form={
        'exchange': 'HKG', 'symbol': code, 'last': '0', 'byPassChecking': 'true', 'broker': '2'})
    evidence['minutes'] = _store(root, 'official-minutes', response)
    captured = current if now else datetime.now(HK)
    try:
        snapshot = normalize_session(session, quote, _json(response), captured.isoformat(), page_evidence=evidence)
    except GreyDataError as error:
        # Do not discard an independently valid official final day quote merely
        # because the minute chart is missing or its reference anchor conflicts.
        from .grey_market import validate_quote
        qtime=source_wall_time(quote.get('TS'))
        if (quote.get('BC')!='2' or quote.get('S')!=code or quote.get('E')!='HKG'
                or quote.get('CY')!='HKD' or qtime.date().isoformat()!=session['trade_date']
                or not timestamp(session['session_start'])<=qtime<=timestamp(session['session_end'])):
            _bad('official_day_quote_identity_required')
        snapshot=dict(session,schema_version=1,status='quote_only',intraday_status='unverified',
            provider=PROVIDER,market='grey',currency='HKD',source_url=SOURCE_URL,
            captured_at=captured.isoformat(),closed=True,quote_time=qtime.isoformat(),
            issue_price=str(_dec(quote.get('C'))),lot_size=quote.get('LS'),
            ohlc={key:str(_dec(quote.get(field))) for key,field in [('open','O'),('high','H'),('low','L'),('close','N')]},
            source_gaps=[str(error)],source_artifacts=evidence)
        snapshot['reference_conflict']=((session.get('issue_price') is not None and _dec(session['issue_price'])!=_dec(snapshot['issue_price']))
            or (session.get('lot_size') is not None and session['lot_size']!=snapshot['lot_size']))
        validate_quote(snapshot,captured)
    normalized = json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode()
    _store(root, 'quote-only-snapshot' if snapshot.get('status')=='quote_only' else 'verified-snapshot', normalized)
    return snapshot
