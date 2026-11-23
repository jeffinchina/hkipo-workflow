"""Read the broker's explicit grey-session calendar; never infer it from allotment.

Offline parser for the public Phillip Securities IPO information page. Dates and
session times are taken from the dedicated PhillipMart table. The separate IPO
listing-date table is only an identity/date cross-check, never a dark-day rule.
"""
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
import re
from zoneinfo import ZoneInfo

SCHEDULE_SOURCE_URL = 'https://www.poems.com.hk/zh-hk/product-and-service/initial-public-offerings/ipo-info/'
HK = ZoneInfo('Asia/Hong_Kong')
_TARGETS = {'ipo-scheduled-items', 'ipo-phillipmart-calendar-items'}


class CalendarSourceError(ValueError):
    """Missing or ambiguous source structure: do not manufacture a schedule."""


def _text(value):
    return re.sub(r'\s+', '', value).strip()


class _Tables(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = {}
        self.active = None
        self.row = None
        self.cell = None
        self.caption = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'table':
            if self.active is not None:
                raise CalendarSourceError('Nested table inside broker calendar')
            matches = set(attrs.get('class', '').split()) & _TARGETS
            if matches:
                if len(matches) != 1:
                    raise CalendarSourceError('Ambiguous broker calendar table')
                name = next(iter(matches))
                self.active = {'name': name, 'caption': '', 'rows': []}
            return
        if self.active is None:
            return
        if tag == 'caption':
            self.caption = True
        elif tag == 'tr':
            if self.row is not None:
                raise CalendarSourceError('Unclosed broker calendar row')
            self.row = []
        elif tag in ('td', 'th'):
            if self.row is None or self.cell is not None:
                raise CalendarSourceError('Malformed broker calendar cell')
            try:
                rowspan = int(attrs.get('rowspan', '1'))
                colspan = int(attrs.get('colspan', '1'))
            except (TypeError, ValueError):
                raise CalendarSourceError('Invalid broker calendar span') from None
            if not 1 <= rowspan <= 100 or colspan != 1:
                raise CalendarSourceError('Unsupported broker calendar span')
            self.cell = {'text': '', 'header': tag == 'th', 'rowspan': rowspan}
        elif tag == 'br' and self.cell is not None:
            self.cell['text'] += ' '

    def handle_data(self, value):
        if self.active is None:
            return
        if self.caption:
            self.active['caption'] += value
        elif self.cell is not None:
            self.cell['text'] += value

    def handle_endtag(self, tag):
        if self.active is None:
            return
        if tag == 'caption':
            self.caption = False
        elif tag in ('td', 'th'):
            if self.cell is None or self.row is None:
                raise CalendarSourceError('Unmatched broker calendar cell')
            self.cell['text'] = _text(self.cell['text'])
            self.row.append(self.cell)
            self.cell = None
        elif tag == 'tr':
            if self.row is None or self.cell is not None:
                raise CalendarSourceError('Unclosed broker calendar cell')
            self.active['rows'].append(self.row)
            self.row = None
        elif tag == 'table':
            if self.row is not None or self.cell is not None:
                raise CalendarSourceError('Unclosed broker calendar table')
            self.active['caption'] = _text(self.active['caption'])
            previous = self.tables.get(self.active['name'])
            # The real page repeats the same calendar in two visible sections.
            # Collapse identical data only; a disagreement must stop scheduling.
            if previous is not None and previous != self.active:
                raise CalendarSourceError('Conflicting repeated broker calendars')
            self.tables[self.active['name']] = self.active
            self.active = None
            self.caption = False


def _grid(table, expected_headers, *, prefix=False):
    rows = table['rows']
    if not rows or not all(c['header'] for c in rows[0]):
        raise CalendarSourceError('Broker calendar headers missing')
    headers = [c['text'] for c in rows[0]]
    if (headers[:len(expected_headers)] != expected_headers if prefix
            else headers != expected_headers):
        raise CalendarSourceError('Broker calendar headers changed')
    if any(c['rowspan'] != 1 for c in rows[0]):
        raise CalendarSourceError('Spanning broker calendar header')
    width = len(headers)
    pending = {}
    result = []
    for row in rows[1:]:
        if not row:
            continue
        expanded = {}
        for column, (text, remaining) in list(pending.items()):
            expanded[column] = text
            if remaining == 1:
                del pending[column]
            else:
                pending[column] = (text, remaining - 1)
        column = 0
        for cell in row:
            if cell['header']:
                raise CalendarSourceError('Unexpected header inside calendar data')
            while column in expanded:
                column += 1
            if column >= width:
                raise CalendarSourceError('Extra broker calendar cells')
            expanded[column] = cell['text']
            if cell['rowspan'] > 1:
                pending[column] = (cell['text'], cell['rowspan'] - 1)
            column += 1
        if set(expanded) != set(range(width)):
            raise CalendarSourceError('Missing broker calendar cells')
        result.append([expanded[column] for column in range(width)])
    if pending:
        raise CalendarSourceError('Calendar rowspan exceeds remaining rows')
    return result


def _code(value):
    if not re.fullmatch(r'\d{1,5}', value):
        raise CalendarSourceError('Invalid broker stock code')
    return value.zfill(5)


def _date(value):
    match = re.fullmatch(r'(\d{4})(?:年|-)?(\d{1,2})(?:月|-)(\d{1,2})日?', value)
    if match is None:
        raise CalendarSourceError('Full broker calendar date required')
    try:
        return date(*(int(v) for v in match.groups()))
    except ValueError:
        raise CalendarSourceError('Invalid broker calendar date') from None


def _times(value):
    pattern = r'(?:(上午|下午|中午))?(\d{1,2})(?:時|:)(\d{1,2})分?'
    matches = list(re.finditer(pattern, value))
    if len(matches) != 2 or re.sub(pattern, '', value) not in ('至', '~', '～', '-', '—'):
        raise CalendarSourceError('Explicit broker session start and end required')
    period = None
    values = []
    for match in matches:
        current, hour, minute = match.groups()
        period = current or period
        hour, minute = int(hour), int(minute)
        if period:
            if not 1 <= hour <= 12:
                raise CalendarSourceError('Invalid broker session time')
            hour = hour % 12 + (12 if period in ('下午', '中午') else 0)
        elif hour < 13:
            raise CalendarSourceError('Ambiguous broker session meridiem')
        try:
            values.append(time(hour, minute))
        except ValueError:
            raise CalendarSourceError('Invalid broker session time') from None
    if values[0] >= values[1]:
        raise CalendarSourceError('Broker session must end after start')
    return values


def _price_text(value):
    """Keep the published range, without choosing its midpoint or upper bound."""
    if value in ('', '-', '--'):
        return None
    parts = re.split(r'[-~～]', value)
    if len(parts) > 2:
        raise CalendarSourceError('Invalid indicative offer-price range')
    try:
        numbers = [Decimal(p) for p in parts]
    except InvalidOperation:
        raise CalendarSourceError('Invalid indicative offer-price range') from None
    if any(not n.is_finite() or n <= 0 for n in numbers) or numbers != sorted(numbers):
        raise CalendarSourceError('Invalid indicative offer-price range')
    return value


def parse_calendar(html, captured_at):
    """Return current explicit sessions; an empty table never cancels saved sessions.

    ``captured_at`` is an aware datetime/ISO value recording actual retrieval.
    ``ipo_price`` is an indicative source string, including an unmodified range;
    ``issue_price`` and ``lot_size`` remain None because these tables do not prove
    the final offer price or board lot.  No holiday or half-day inference occurs.
    """
    if isinstance(captured_at, str):
        try:
            captured_at = datetime.fromisoformat(captured_at)
        except ValueError:
            raise CalendarSourceError('Timezone-aware capture time required') from None
    if not isinstance(captured_at, datetime) or captured_at.utcoffset() is None:
        raise CalendarSourceError('Timezone-aware capture time required')
    if not isinstance(html, str) or not html or len(html) > 3_000_000:
        raise CalendarSourceError('Invalid broker calendar HTML')
    parser = _Tables()
    parser.feed(html)
    parser.close()
    if parser.active is not None or set(parser.tables) != _TARGETS:
        raise CalendarSourceError('Complete broker calendar tables required')
    listing = parser.tables['ipo-scheduled-items']
    grey = parser.tables['ipo-phillipmart-calendar-items']
    if _text(grey['caption']) != '輝立交易場時間表':
        raise CalendarSourceError('Explicit PhillipMart calendar caption required')
    offers = {}
    for row in _grid(listing, ['代號', '公司', '招股價', '上市日期'], prefix=True):
        code = _code(row[0])
        if code in offers or not row[1]:
            raise CalendarSourceError('Repeated or unnamed broker issuer')
        offers[code] = (row[1], _date(row[3]), _price_text(row[2]))
    results = []
    seen = set()
    for row in _grid(grey, ['代號', '公司', '輝立交易場啟動日期', '時間']):
        code = _code(row[0])
        if code in seen or code not in offers or not row[1] or row[1] != offers[code][0]:
            raise CalendarSourceError('Grey session issuer identity mismatch')
        seen.add(code)
        issuer, listing_date, price = offers[code]
        trade_date = _date(row[2])
        if trade_date >= listing_date:
            raise CalendarSourceError('Grey session must precede its stated listing date')
        start, end = _times(row[3])
        results.append(dict(stock_code=code, issuer=issuer,
                            listing_date=listing_date.isoformat(), trade_date=trade_date.isoformat(),
                            session_start=datetime.combine(trade_date, start, HK).isoformat(),
                            session_end=datetime.combine(trade_date, end, HK).isoformat(),
                            ipo_price=price, issue_price=None, lot_size=None,
                            schedule_source_url=SCHEDULE_SOURCE_URL,
                            captured_at=captured_at.isoformat()))
    return results


def fetch_calendar(directory, *, proxy_url=None, fetcher=None):
    """Read and archive the official calendar; no price monitoring or delivery."""
    import hashlib
    from pathlib import Path
    from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
    from urllib.parse import urlsplit
    class OfficialRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            p=urlsplit(newurl)
            if p.scheme!='https' or p.hostname!='www.poems.com.hk' or p.username or p.password or p.port not in (None,443):
                raise CalendarSourceError('Calendar redirect outside official host')
            return super().redirect_request(req,fp,code,msg,headers,newurl)
    if fetcher:
        raw=fetcher(SCHEDULE_SOURCE_URL)
    else:
        proxies={}
        if proxy_url:
            from .network import _proxy_endpoint
            normalized,_,_=_proxy_endpoint(proxy_url)
            proxies={'https':normalized}
        opener=build_opener(OfficialRedirect(),ProxyHandler(proxies))
        try:
            with opener.open(Request(SCHEDULE_SOURCE_URL,headers={'User-Agent':'Mozilla/5.0'}),timeout=20) as response:
                raw=response.read(3_000_001)
        except Exception:raise CalendarSourceError('Official grey calendar unavailable') from None
    if not isinstance(raw,bytes) or not 0<len(raw)<=3_000_000:raise CalendarSourceError('Invalid calendar response size')
    try:html=raw.decode('utf-8-sig')
    except UnicodeError:raise CalendarSourceError('Calendar encoding changed') from None
    rows=parse_calendar(html,datetime.now(HK))
    out=Path(directory);out.mkdir(parents=True,exist_ok=True,mode=0o700)
    target=out/(hashlib.sha256(raw).hexdigest()+'.html')
    if not target.exists():target.write_bytes(raw);target.chmod(0o600)
    return rows
