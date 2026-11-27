"""Official HKEX read-only sources with bounded transport and completeness gates."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import Request, build_opener, HTTPSHandler, HTTPRedirectHandler
from .models import DocumentEvent, canonical_url, utcnow

class SourceError(RuntimeError):
    def __init__(self,message,retry_after=None):
        super().__init__(message)
        self.retry_after=retry_after

class _OfficialRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        canonical_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)

class HttpClient:
    def __init__(self, attempts=2, timeout=25, max_retry_after=120, sleep=time.sleep):
        if attempts < 2 or attempts > 5:
            raise ValueError('attempts must be between 2 and 5')
        self.attempts,self.timeout,self.max_retry_after,self.sleep=attempts,timeout,max_retry_after,sleep
        self.opener=build_opener(HTTPSHandler(),_OfficialRedirect())

    def get(self,url):
        canonical_url(url)
        for attempt in range(self.attempts):
            try:
                with self.opener.open(Request(url,headers={'User-Agent':'HKIPO-LocalMonitor/0.1','Accept':'application/json,text/html'}),timeout=self.timeout) as response:
                    canonical_url(response.geturl())
                    raw=response.read(15_000_001)
                    if len(raw)>15_000_000:
                        raise SourceError('response too large')
                    return raw.decode('utf-8-sig')
            except HTTPError as e:
                if e.code not in {429,500,502,503,504}:
                    raise SourceError(f'HTTP {e.code}') from None
                delay=2**attempt
                if e.code==429:
                    value=e.headers.get('Retry-After','')
                    try:
                        delay=max(0,float(value))
                    except ValueError:
                        try: delay=max(0,(parsedate_to_datetime(value)-datetime.now(timezone.utc)).total_seconds())
                        except (ValueError,TypeError): delay=2**attempt
                    # Do not retry earlier than requested. Long waits are handed
                    # back as a deferred failure for the next scheduled scan.
                    if delay>self.max_retry_after:
                        raise SourceError('rate limited; deferred by Retry-After',retry_after=delay) from None
            except (URLError,TimeoutError,OSError,UnicodeError):
                delay=2**attempt
            if attempt+1<self.attempts:
                self.sleep(delay)
        raise SourceError('bounded HTTP recovery exhausted')

def classify(title, context=''):
    text=(title+' '+context).lower()
    if re.search(r'申請版本|申请版本|聆訊後|聆讯后|application proof|post.hearing|phip',text):
        return 'preliminary'
    if re.search(r'配發結果|配发结果|allotment results|配售結果|配售结果',text):
        return 'allotment'
    ipo = bool(re.search(r'全球發售|全球发售|公開發售|公开发售|招股|新上市|global offering|public offer|prospectus|上市文件',text))
    if ipo and re.search(r'延期|延遲|延迟|暫停|暂停|終止|终止|撤回|取消|補充|补充|更正|補發|补发|supplement|delay|postpon|cancel|withdraw|correction',text):
        return 'exception'
    if ipo:
        return 'offer'
    return 'other'

def make_event(source,source_id,code,name,title,url,published='',context='',kind=None,discovered=None):
    # URL identity retains every supplement and revised file independently.
    kind=kind or classify(title,context)
    version='revision' if re.search(r'補充|补充|更正|修訂|修订|supplement|correction|revis',title+' '+context,re.I) else 'original'
    return DocumentEvent('',source,str(source_id),str(code).strip().zfill(5),name.strip(),kind,title.strip(),url,published,discovered or utcnow(),'en' if re.search(r'_e\.',url) else 'zh-HK',version)

class TableParser(HTMLParser):
    """Preserve all rows, cell text and every link, independent of nested tags."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows=[]; self.row=None; self.cell=None; self.link=None
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag=='tr': self.row=[]
        elif tag in {'td','th'} and self.row is not None:
            self.cell={'text':'','links':[],'class':a.get('class','')}
        elif tag=='a' and self.cell is not None:
            self.link={'url':a.get('href',''),'text':a.get('title','')}
        elif tag=='br' and self.cell is not None: self.cell['text']+=' '
    def handle_data(self,data):
        if self.cell is not None:self.cell['text']+=data
        if self.link is not None:self.link['text']+=data
    def handle_endtag(self,tag):
        if tag=='a' and self.link is not None:
            if self.cell is not None:self.cell['links'].append(self.link)
            self.link=None
        elif tag in {'td','th'} and self.cell is not None:
            self.row.append(self.cell);self.cell=None
        elif tag=='tr' and self.row is not None:
            self.rows.append(self.row);self.row=None

@dataclass
class SourceResult:
    source: str
    events: list
    complete: bool = True
    count: int = 0
    evidence: str = ''
    responses: list = field(default_factory=list)

def parse_aggregate(html,base_url,source='new-listings-main'):
    parser=TableParser();parser.feed(html)
    header=next((r for r in parser.rows if len(r)>=5 and re.search(r'股份代號|股份代号|Stock Code',r[0]['text'],re.I)),None)
    if header is None or not re.search(r'招股|Prospectus',header[3]['text'],re.I):
        raise SourceError('aggregate table schema missing')
    events=[];rows=0
    for row in parser.rows:
        if len(row)!=5 or not re.fullmatch(r'\s*\d{1,5}\s*',row[0]['text'].replace('\xa0',' ')):
            continue
        rows+=1
        for col in (2,3,4):
            for link in row[col]['links']:
                url=urljoin(base_url,link['url'])
                if not urlsplit(url).path.lower().endswith('.pdf'): continue
                title=' '.join((header[col]['text']+' '+link['text']).split())
                # Explicit nearby supplement labels remain in provenance; document
                # URL makes unlabelled second links separate events as well.
                context=' '.join(row[col]['text'].split())
                match=re.search(r'/(20\d{2})/(\d{2})(\d{2})/',url)
                published=f'{match[1]}-{match[2]}-{match[3]}T00:00:00+08:00' if match else ''
                events.append(make_event(source,urlsplit(url).path,row[0]['text'].strip(),row[1]['text'],title,url,published,context))
    if not rows or not events:
        raise SourceError('aggregate unexpectedly empty; preserve last good state')
    return SourceResult(source,events,True,len(events),'official aggregate; completeness fallback, not fastest feed')

class AggregateSource:
    timestamp_precision='date'
    def __init__(self,board='Main-Board',client=None):
        if board not in {'Main-Board','GEM'}:raise ValueError('Unknown board')
        self.name='new-listings-'+board.lower();self.client=client or HttpClient()
        self.urls=[f'https://www2.hkexnews.hk/New-Listings/New-Listing-Information/{board}?sc_lang=zh-HK',f'https://www2.hkexnews.hk/New-Listings/New-Listing-Information/{board}?sc_lang=en']
    def fetch(self,start,end):
        for url in self.urls:
            try:
                raw=self.client.get(url)
                result=parse_aggregate(raw,url,self.name)
                result.responses=[{'url':url,'format':'html','body':raw}]
                return result
            except SourceError as error:
                if error.retry_after is not None:raise
                continue
            except ValueError:continue
        raise SourceError('aggregate primary and language fallback exhausted')

def title_row_exclusion(row):
    """Identify known non-IPO rows with genuinely absent security fields."""
    if row.get('STOCK_CODE') != '' or row.get('STOCK_NAME') != '':
        return None
    category = re.sub(r'\s+', '', unescape(str(row.get('LONG_TEXT', ''))))
    if category == '債券及結構性產品-[上市通告-債務證券發行計劃]':
        return 'non-IPO debt issuance programme notice'
    if (category.startswith('債券及結構性產品-[') and '基礎上市文件' in category
            and any(kind in category for kind in ('牛熊證', '衍生權證'))):
        return 'non-IPO structured-product base listing document'
    return None


def title_row_excluded(row):
    return title_row_exclusion(row) is not None


def parse_title_json(raw,source='title-search',exclusions=None):
    try:
        data=json.loads(raw)
        rows=json.loads(data['result']) if isinstance(data['result'],str) else data['result']
        total=int(data['recordCnt']);loaded=int(data['loadedRecord'])
        more=data['hasNextRow']
        if not isinstance(more,bool) or not isinstance(rows,list) or loaded!=len(rows) or loaded>total or total<0:
            raise ValueError()
        events=[];excluded=[]
        for row in rows:
            required={'NEWS_ID','STOCK_CODE','STOCK_NAME','TITLE','FILE_LINK','DATE_TIME'}
            if not required.issubset(row):raise ValueError()
            url=canonical_url(urljoin('https://www1.hkexnews.hk',row['FILE_LINK']))
            published=datetime.strptime(row['DATE_TIME'],'%d/%m/%Y %H:%M').isoformat()+'+08:00'
            reason = title_row_exclusion(row)
            if reason:
                excluded.append({'NEWS_ID': str(row['NEWS_ID']), 'reason': reason})
                continue
            codes=re.split(r'<br\s*/?>',str(row['STOCK_CODE']).strip(),flags=re.I)
            names=re.split(r'<br\s*/?>',str(row['STOCK_NAME']).strip(),flags=re.I)
            if len(codes)!=len(names) or any(not re.fullmatch(r'\d{1,5}',c.strip()) for c in codes) or any(not n.strip() for n in names):raise ValueError()
            for code,name in zip(codes,names):
                events.append(make_event(source,row['NEWS_ID'],code.strip(),unescape(name),unescape(row['TITLE']),url,published,unescape(row.get('LONG_TEXT',''))))
        if more != (loaded<total):raise ValueError()
        if len({x['NEWS_ID'] for x in excluded})!=len(excluded):raise ValueError()
        if exclusions is not None:exclusions.extend(excluded)
        return events,total,more
    except (ValueError,TypeError,KeyError,OverflowError):
        raise SourceError('title-search JSON schema or counts invalid') from None

class TitleSearchSource:
    """Official cumulative rowRange pagination. No assumption that first 100
    rows cover the day. After discovery, request the rounded total once.
    Any truncated/unstable result fails the whole scan."""
    name='title-search'
    timestamp_precision='minute'
    def __init__(self,client=None,page_size=100,max_records=20000):
        self.client=client or HttpClient();self.page_size=page_size;self.max_records=max_records
        if page_size<1 or max_records<page_size:raise ValueError('Invalid pagination bounds')
    def fetch(self,start,end):
        previous_ids=set();previous_rows={};previous_total=None;responses=[]
        limit=self.page_size
        for attempt in range(2):
            params={'sortDir':'0','sortByOptions':'DateTime','category':'0','market':'SEHK','stockId':'-1','documentType':'-2','fromDate':start.astimezone(ZoneInfo('Asia/Hong_Kong')).strftime('%Y%m%d'),'toDate':end.astimezone(ZoneInfo('Asia/Hong_Kong')).strftime('%Y%m%d'),'title':'','searchType':'0','t1code':'-2','t2Gcode':'-2','t2code':'-2','rowRange':limit,'lang':'zh'}
            request_url='https://www1.hkexnews.hk/search/titleSearchServlet.do?'+urlencode(params)
            raw=self.client.get(request_url)
            exclusions=[]
            events,total,more=parse_title_json(raw,self.name,exclusions)
            responses.append({'url':request_url,'format':'json','body':raw})
            if total>self.max_records:
                raise SourceError('title-search pagination limit exceeded')
            # NEWS_ID identifies a source row, while event_id identifies a
            # document/security. HKEX can legitimately republish the same PDF
            # under a new NEWS_ID and later time. Validate pagination at the raw
            # row boundary, so such aliases neither stop all monitoring nor hide
            # a missing row behind an identical document URL.
            envelope=json.loads(raw)
            rows=json.loads(envelope['result']) if isinstance(envelope['result'],str) else envelope['result']
            ids={str(row['NEWS_ID']) for row in rows}
            if len(ids)!=len(rows) or not previous_ids.issubset(ids) or (more and ids==previous_ids):
                raise SourceError('title-search duplicate or unstable pagination')
            current_rows={str(row['NEWS_ID']):row for row in rows}
            if any(current_rows[key]!=row for key,row in previous_rows.items()):
                raise SourceError('title-search prefix observations changed during pagination')
            canonical={};aliases=[]
            for event in events:
                prior=canonical.get(event.event_id)
                if prior is None:
                    canonical[event.event_id]=event;continue
                # Distinct source IDs alone are not enough: differing names or
                # headlines at one canonical identity remain an actual conflict.
                fields=('stock_name','title','url','stock_code','kind','version','language')
                if prior.source_id==event.source_id or any(getattr(prior,k)!=getattr(event,k) for k in fields):
                    raise SourceError('title-search conflicting document observations')
                aliases.append({'event_id':event.event_id,'NEWS_IDs':[prior.source_id,event.source_id],
                                'published_at':[prior.published_at,event.published_at],
                                'reason':'distinct official publication rows for identical document association'})
                if event.published_at<prior.published_at:canonical[event.event_id]=event
            # Publication can arrive while fetching; require stable totals before
            # committing a cumulative last page. A later poll recovers overlap.
            if previous_total is not None and total!=previous_total:
                raise SourceError('title-search changed during pagination')
            if not more:
                audit='official cumulative JSON; raw NEWS_ID total and prefix verified'
                if exclusions:audit+='; exclusions='+json.dumps(exclusions,ensure_ascii=False)
                if aliases:audit+='; republication_aliases='+json.dumps(aliases,ensure_ascii=False)
                return SourceResult(self.name,list(canonical.values()),True,total,audit,responses)
            previous_ids,previous_total=ids,total
            previous_rows=current_rows
            limit=min(self.max_records,((total+self.page_size-1)//self.page_size)*self.page_size)
        raise SourceError('title-search pagination limit exceeded')
