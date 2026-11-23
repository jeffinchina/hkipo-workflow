"""Phillip official day quote + explicitly attributed AAStocks five-minute line.

The final news release is discovered from Phillip's real link list, never a
fabricated filename. A complete price line is kept separately if the independent
broker day quote is not yet available; no deadline is postponed for the PDF.
"""
from datetime import datetime,date,time,timedelta
from email.utils import parsedate_to_datetime
import time as monotonic_clock
from decimal import Decimal,ROUND_HALF_UP
from html.parser import HTMLParser
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit,urljoin
from urllib.request import Request,build_opener,ProxyHandler,HTTPRedirectHandler
from .grey_market import HK,timestamp,validate_snapshot,GreyDataError
from .grey_source import GreySourceUnavailable,_bad,_dec,_store
from .grey_aastocks import fetch_price_line

PROVIDER='辉立暗盘'
LIST_URL='https://www.poems.com.hk/zh-hk/product-and-service/initial-public-offerings/press-release/'
PUBLIC_QUOTE_URL='https://www.poems.com.hk/zh-hk/product-and-service/initial-public-offerings/phillipmart/'
HOSTS={'www.poems.com.hk','www1.poems.com.hk','research.cyberquote.com.hk','research.poems.com.hk','pmp06.poems.com.hk'}


class _Links(HTMLParser):
    def __init__(self):super().__init__(convert_charrefs=True);self.urls=[]
    def handle_starttag(self,tag,attrs):
        if tag=='a':
            value=dict(attrs).get('href')
            if value:self.urls.append(urljoin(LIST_URL,value))


def final_pdf_url(body,code,trade_date):
    try:
        text=body.decode('utf-8-sig');day=date.fromisoformat(trade_date)
    except (UnicodeError,ValueError):_bad('phillip_release_list_invalid')
    parser=_Links();parser.feed(text)
    found=set()
    for url in parser.urls:
        p=urlsplit(url)
        if (p.scheme=='https' and p.hostname in {'research.cyberquote.com.hk','research.poems.com.hk'}
                and not p.username and not p.password and p.port in (None,443)
                and re.fullmatch(r'/[Pp]hillip[Mm]art/Files/'+day.strftime('%Y%m%d')+'-'+re.escape(code)+r'C_Final\.pdf',p.path)
                and not p.query and not p.fragment):found.add(url)
    if len(found)!=1:_bad('phillip_final_release_not_yet_available')
    return found.pop()


def _normalize_text(value):return re.sub(r'\s+','',value)


def parse_final_lines(lines,code,trade_date):
    """Read the table geometrically; source PDF text order is not row order."""
    texts='\n'.join(t for box,t in lines)
    dates=set(f'{int(y):04d}-{int(m):02d}-{int(d):02d}' for y,m,d in re.findall(r'(\d{4})年(\d{1,2})月(\d{1,2})日',texts))
    if dates!={trade_date} or '輝立交易場' not in texts or '最後收市價' not in texts:
        _bad('phillip_final_date_or_document_kind')
    markers=[(box,text) for box,text in lines if '股份代號' in text]
    if len(markers)!=1:_bad('phillip_final_security_marker')
    marker=markers[0][0];cy=(marker[1]+marker[3])/2
    codes=[text.strip().zfill(5) for box,text in lines
           if re.fullmatch(r'\d{1,5}',text.strip()) and abs((box[1]+box[3])/2-cy)<4 and box[0]>=marker[2]-2]
    if codes!=[code]:_bad('phillip_final_security_mismatch')
    labels={'issue_price':'招股價','open':'開市價格','high':'最高價格','low':'最低價格',
            'close':'最後收市價','volume':'成交股數','turnover':'總成交金額'}
    values={};evidence={}
    for key,label in labels.items():
        matching=[(box,text) for box,text in lines if _normalize_text(text).startswith(label)]
        if len(matching)!=1:_bad('phillip_final_table_labels')
        box,text=matching[0];cy=(box[1]+box[3])/2
        cells=[(b,t) for b,t in lines if b[0]>box[2]+20 and abs((b[1]+b[3])/2-cy)<6]
        row=' '.join(t for b,t in sorted(cells,key=lambda x:x[0][0]))
        pattern=r'([0-9][0-9,]*(?:\.[0-9]+)?)\s*股' if key=='volume' else r'HKD\s*([0-9][0-9,]*(?:\.[0-9]+)?)'
        numbers=re.findall(pattern,row)
        if len(numbers)!=1:_bad('phillip_final_table_values')
        value=_dec(numbers[0].replace(',',''),zero=key in ('volume','turnover'))
        values[key]=str(value)
        evidence[key]={'page':1,'label':label,'quote':row,'bbox':[round(v,2) for v in box]}
        pct=re.search(r'\(([+-]?[0-9]+(?:\.[0-9]+)?)%\)',row)
        if key in ('high','low','close') and pct:
            observed=(_dec(values[key])/_dec(values['issue_price'])-1)*100
            if observed.quantize(Decimal('.01'),rounding=ROUND_HALF_UP)!=Decimal(pct.group(1)):
                _bad('phillip_final_percent_conflict')
    opening,high,low,close=[_dec(values[k]) for k in ('open','high','low','close')]
    if not low<=min(opening,close)<=max(opening,close)<=high:_bad('phillip_final_ohlc_conflict')
    return {'issue_price':values['issue_price'],'ohlc':{k:values[k] for k in ('open','high','low','close')},
            'volume':values['volume'],'turnover':values['turnover'],'source_fields':evidence}


def parse_final_pdf(data,code,trade_date):
    import fitz
    if not isinstance(data,bytes) or not data.startswith(b'%PDF-'):_bad('phillip_final_pdf_required')
    try:
        with fitz.open(stream=data,filetype='pdf') as document:
            if document.needs_pass or len(document)!=1:_bad('phillip_final_pdf_layout_changed')
            lines=[]
            for block in document[0].get_text('dict')['blocks']:
                for line in block.get('lines',[]):lines.append((line['bbox'],''.join(s['text'] for s in line['spans'])))
    except GreySourceUnavailable:raise
    except Exception:_bad('phillip_final_pdf_unreadable')
    return parse_final_lines(lines,code,trade_date)


class _Redirect(HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        if urlsplit(newurl).scheme!='https' or urlsplit(newurl).hostname not in HOSTS:_bad('phillip_redirect_rejected')
        return super().redirect_request(req,fp,code,msg,headers,newurl)


class Reader:
    def __init__(self,proxy_url=None):
        proxies={}
        if proxy_url:
            p=urlsplit(proxy_url)
            if (p.scheme!='http' or p.hostname not in ('127.0.0.1','localhost','::1') or not p.port
                    or p.username or p.password or p.path not in ('','/') or p.query or p.fragment):_bad('loopback_proxy_required')
            proxies={'https':proxy_url}
        self.opener=build_opener(_Redirect(),ProxyHandler(proxies))
        self.last_http_date=None
        self.last_source_url=None
    def __call__(self,url):
        p=urlsplit(url)
        if p.scheme!='https' or p.hostname not in HOSTS or p.username or p.password:_bad('phillip_url_rejected')
        try:
            with self.opener.open(Request(url,headers={'User-Agent':'Mozilla/5.0'}),timeout=12) as response:
                self.last_http_date=response.headers.get('Date')
                self.last_source_url=url
                data=response.read(12_000_001)
                if len(data)>12_000_000:_bad('phillip_source_oversized')
                return data
        except GreySourceUnavailable:raise
        except Exception:_bad('phillip_source_unavailable')



def public_ajax_url(page,code):
    try:text=page.decode('utf-8-sig')
    except UnicodeError:_bad('phillip_public_page_encoding')
    parser=_Links();parser.feed(text);found=set()
    from urllib.parse import parse_qs
    for url in parser.urls:
        p=urlsplit(url)
        if (p.scheme=='https' and p.hostname=='research.poems.com.hk'
                and p.path=='/page/Phillip/share_recommend/PMP_1/ZH/streaming.asp'
                and parse_qs(p.query)=={'code':[str(int(code))]}):found.add(url)
    if len(found)!=1:_bad('phillip_current_public_quote_link_missing')
    return found.pop()


def public_guest_parameters(page):
    try:
        text=page.decode('big5')
        call=re.search(r'pmp\.create_connectionByUserName\(\s*([^;]+)\);',text,re.S).group(1)
        values=re.findall(r'"([^"\r\n]*)"',call)
    except (AttributeError,UnicodeError):_bad('phillip_public_client_initialization_changed')
    if (len(values)!=3 or values[0]!='https://pmp06.poems.com.hk/'
            or not all(0<len(v)<=64 for v in values[1:])):_bad('phillip_public_client_initialization_changed')
    scrubbed=text.replace(values[1],'[PUBLIC_GUEST_PARAMETER]').replace(values[2],'[PUBLIC_GUEST_PARAMETER]')
    return values[0],values[1],values[2],scrubbed.encode('utf-8')


def _callbacks(data,name):
    text=data.decode('utf-8','replace');decoder=json.JSONDecoder();values=[]
    for match in re.finditer(r'(?<![A-Za-z0-9_])'+re.escape(name)+r'\s*\(\s*(\{)',text):
        try:value,_=decoder.raw_decode(text[match.start(1):])
        except ValueError:continue
        values.append(value)
    return values


def _multipart(value):
    # Fixed framing is safe here: JSON escapes CR/LF and no uploaded files exist.
    boundary='----hkipo-readonly-public-quote'
    body=('--'+boundary+'\r\nContent-Disposition: form-data; name="p"\r\n\r\n'+
          json.dumps(value,separators=(',',':'))+'\r\n--'+boundary+'--\r\n').encode()
    return body,'multipart/form-data; boundary='+boundary


def read_public_pmp(host,user,password,code,opener,ajax_url):
    """Read one public visitor quote, then close the finite streaming response.

    These credentials are dynamically provided to everyone by the broker's
    public page. They are never hardcoded, printed, or used for account/trading
    methods. Only the declared PFeeder topic and fields are requested.
    """
    endpoint=host+'publish.aspx?'+str(int(monotonic_clock.time()*1000))
    def post(value):
        body,content_type=_multipart(value)
        req=Request(endpoint,data=body,headers={'Content-Type':content_type,
            'Referer':ajax_url,'User-Agent':'Mozilla/5.0'})
        return opener.open(req,timeout=10)
    stream=None;application=None
    deadline=monotonic_clock.monotonic()+40
    try:
        stream=post({'id':'login','name':user,'pwd':password})
        server_date=stream.headers.get('Date')
        raw=b'';login=None
        for _ in range(10):
            if monotonic_clock.monotonic()>deadline:break
            raw+=stream.read1(65536)
            callbacks=_callbacks(raw,'l')
            if callbacks:login=callbacks[-1];break
            if len(raw)>256000:_bad('phillip_public_quote_oversized')
        if not login or login.get('id')!='login' or login.get('rc')!=0:
            _bad('phillip_public_quote_visitor_unavailable')
        topic='\\RealTime\\PFeeder\\'+str(int(code))
        subscribe={'id':'data','name':user,'sid':login['sid'],'cid':login['cid'],
            'sub':[{'tpc':topic,'param':{'fids':['P'+str(i) for i in range(1,190)]},'enable':True}],
            'qry':None}
        with post(subscribe) as ack:ack.read1(65536)
        raw=b''
        for _ in range(12):
            if monotonic_clock.monotonic()>deadline:break
            chunk=stream.read1(65536)
            if not chunk:break
            raw+=chunk
            for message in _callbacks(raw,'u'):
                if message.get('id')!='data':continue
                for item in message.get('val',[]):
                    if item.get('tpc')!=topic:continue
                    rows=item.get('item',[])
                    if len(rows)==1 and isinstance(rows[0].get('val'),dict) and 'P5' in rows[0]['val']:
                        application=message
                        return rows[0]['val'],server_date,application
            if len(raw)>512000:_bad('phillip_public_quote_oversized')
        _bad('phillip_public_quote_not_received')
    except GreySourceUnavailable:raise
    except Exception:_bad('phillip_public_quote_unavailable')
    finally:
        if stream is not None:stream.close()


def parse_public_quote(fields,session,server_date,captured_at,source_url):
    code=session['stock_code'];captured=timestamp(captured_at);end=timestamp(session['session_end'])
    if captured<end:_bad('session_not_closed')
    if str(fields.get('P1','')).zfill(5)!=code:_bad('phillip_live_security_mismatch')
    try:
        server=parsedate_to_datetime(server_date).astimezone(HK)
        last_time=time.fromisoformat(fields['P16'])
    except (ValueError,TypeError,KeyError):_bad('phillip_live_source_time_required')
    if server.date().isoformat()!=session['trade_date'] or abs((server-captured).total_seconds())>180:
        _bad('phillip_live_current_day_required')
    asof=datetime.combine(server.date(),last_time,HK)
    if not timestamp(session['session_start'])<=asof<=end:_bad('phillip_live_trade_time_conflict')
    values={k:str(_dec(fields.get(source))) for k,source in
            [('issue_price','P6'),('open','P9'),('high','P10'),('low','P11'),('close','P5')]}
    if not re.fullmatch(r'[1-9][0-9]*',fields.get('P15','')):_bad('phillip_live_lot_size_required')
    lot=int(fields['P15'])
    if not 1<=lot<=1000000:_bad('phillip_live_lot_size_required')
    o,h,l,c=[_dec(values[k]) for k in ('open','high','low','close')]
    if not l<=min(o,c)<=max(o,c)<=h:_bad('phillip_live_ohlc_conflict')
    if session.get('issue_price') is not None and _dec(session['issue_price'])!=_dec(values['issue_price']):
        _bad('phillip_issue_price_conflict')
    return {'schema_version':1,'status':'quote_only','intraday_status':'unavailable',
        'provider':PROVIDER,'market':'grey','currency':'HKD','stock_code':code,
        'issuer':fields.get('P3') or session['issuer'],'trade_date':session['trade_date'],
        'listing_date':session['listing_date'],'session_start':session['session_start'],
        'session_end':session['session_end'],'captured_at':captured.isoformat(),'closed':True,
        'close_basis':'official_session_elapsed_and_current_public_pmp_quote',
        'source_url':source_url,'schedule_source_url':session['schedule_source_url'],
        'source_http_date':server_date,'quote_time':asof.isoformat(),
        'source_date_basis':'official_calendar_current_public_quote_page_http_date_and_live_pmp_trade_time',
        'issue_price':values['issue_price'],'lot_size':lot,
        'ohlc':{k:values[k] for k in ('open','high','low','close')},
        'volume_display':fields.get('P13'),'turnover_display':fields.get('P14'),
        'trade_count_display':fields.get('P12'),
        'source_fields':{k:fields.get(k) for k in ('P1','P3','P5','P6','P9','P10','P11','P13','P14','P15','P16')}}


def collect_live_quote(session,directory,*,proxy_url=None,now=None,reader=None,pmp_reader=None):
    current=timestamp(now.isoformat()) if isinstance(now,datetime) else timestamp(now) if now else datetime.now(HK)
    if current<timestamp(session['session_end']):_bad('session_not_closed')
    code=session.get('stock_code','')
    if not re.fullmatch(r'\d{5}',code) or session.get('provider')!=PROVIDER:_bad('phillip_session_identity')
    reader=reader or Reader(proxy_url);root=Path(directory)/(session['trade_date']+'-'+code+'-PH')
    parent=reader(PUBLIC_QUOTE_URL);ajax_url=public_ajax_url(parent,code)
    page=reader(ajax_url);host,user,password,scrubbed=public_guest_parameters(page)
    page_http_date=getattr(reader,'last_http_date',None)
    if pmp_reader is None:pmp_reader=read_public_pmp
    fields,http_date,application=pmp_reader(host,user,password,code,reader.opener,ajax_url)
    http_date=http_date or page_http_date
    del user,password
    captured=current if now else datetime.now(HK)
    quote=parse_public_quote(fields,session,http_date,captured.isoformat(),ajax_url)
    quote['source_artifacts']={'official_parent':_store(root,'phillip-parent-page',parent),
        'public_client':_store(root,'phillip-client-page',scrubbed),
        'public_client_original_sha256':hashlib.sha256(page).hexdigest(),
        'application':_store(root,'phillip-quote-application',json.dumps(application,ensure_ascii=False).encode())}
    return quote


def collect_day_quote(session,directory,*,proxy_url=None,now=None,fetcher=None):
    current=timestamp(now.isoformat()) if isinstance(now,datetime) else timestamp(now) if now else datetime.now(HK)
    if current<timestamp(session['session_end']):_bad('session_not_closed')
    code=session.get('stock_code','')
    if not re.fullmatch(r'\d{5}',code) or session.get('provider')!=PROVIDER:_bad('phillip_session_identity')
    fetcher=fetcher or Reader(proxy_url);root=Path(directory)/(session['trade_date']+'-'+code+'-PH')
    listing=fetcher(LIST_URL);list_artifact=_store(root,'phillip-release-page',listing)
    url=final_pdf_url(listing,code,session['trade_date']);raw=fetcher(url)
    evidence=_store(root,'phillip-final-pdf',raw);facts=parse_final_pdf(raw,code,session['trade_date'])
    if session.get('issue_price') is not None and _dec(session['issue_price'])!=_dec(facts['issue_price']):_bad('phillip_issue_price_conflict')
    return {'schema_version':1,'status':'quote_only','intraday_status':'unavailable',
        'provider':PROVIDER,'market':'grey','currency':'HKD','stock_code':code,'issuer':session['issuer'],
        'trade_date':session['trade_date'],'listing_date':session['listing_date'],
        'session_start':session['session_start'],'session_end':session['session_end'],
        'captured_at':(current if now else datetime.now(HK)).isoformat(),'closed':True,
        'source_url':url,'schedule_source_url':session['schedule_source_url'],
        **facts,'lot_size':session.get('lot_size'),
        'lot_size_source':session.get('lot_size_source'),
        'source_artifacts':{'release_list':list_artifact,'official_final_pdf':evidence}}


def combine_line_and_quote(session,line,quote,now=None):
    if quote.get('provider')!=PROVIDER or quote.get('stock_code')!=session['stock_code'] or quote.get('trade_date')!=session['trade_date']:
        _bad('phillip_day_quote_identity_conflict')
    if not quote.get('lot_size'):_bad('phillip_verified_lot_size_required')
    result={**quote,**line,'status':'verified','intraday_status':'complete',
            'source_url':line['source_url'],'official_quote_source_url':quote['source_url'],
            'line_captured_at':line['captured_at'],
            'captured_at':max(timestamp(line['captured_at']),timestamp(quote['captured_at'])).isoformat(),
            'ohlc_crosscheck':{'confirmed':True,'source_url':quote['source_url'],
                              'method':'independent_phillip_official_day_quote'},
            'source_artifacts':{'line':line.get('source_artifacts',{}),'day_quote':quote.get('source_artifacts',{})}}
    return validate_snapshot(result,now)


def collect_session(session,directory,*,proxy_url=None,now=None,quote_fetcher=None,line_fetcher=None):
    """Full combination if independently confirmed; retain actual partial facts."""
    line=None;quote=None;errors=[]
    try:line=(line_fetcher or fetch_price_line)(session,directory,proxy_url=proxy_url,now=now)
    except GreySourceUnavailable as error:errors.append(str(error))
    try:
        quote=collect_live_quote(session,directory,proxy_url=proxy_url,now=now,reader=quote_fetcher)
    except GreySourceUnavailable as error:
        errors.append(str(error))
        try:quote=collect_day_quote(session,directory,proxy_url=proxy_url,now=now,fetcher=quote_fetcher)
        except GreySourceUnavailable as error:errors.append(str(error))
    if line and quote:
        try:return combine_line_and_quote(session,line,quote,now)
        except GreyDataError as error:errors.append(str(error))
    if quote:
        return dict(quote,source_gaps=errors,price_line=line)
    if line:
        return {'schema_version':1,'status':'line_only','intraday_status':'complete',
                'provider':PROVIDER,'stock_code':session['stock_code'],'trade_date':session['trade_date'],
                'price_line':line,'source_gaps':errors,
                'independent_broker_ohlc_confirmed':False}
    _bad('phillip_day_quote_and_full_line_unavailable')


def probe_live_quote(stock_code,directory,*,proxy_url=None):
    """Explicit finite live-source acceptance probe; never returns sendable facts."""
    if not re.fullmatch(r'\d{5}',stock_code):_bad('security_identity')
    reader=Reader(proxy_url);parent=reader(PUBLIC_QUOTE_URL)
    ajax_url=public_ajax_url(parent,stock_code);page=reader(ajax_url)
    host,user,password,scrubbed=public_guest_parameters(page)
    page_http_date=getattr(reader,'last_http_date',None)
    fields,http_date,application=read_public_pmp(host,user,password,stock_code,reader.opener,ajax_url)
    http_date=http_date or page_http_date
    del user,password
    artifact=_store(Path(directory),'phillip-probe-application',json.dumps(application,ensure_ascii=False).encode())
    return {'status':'probe_only','sendable':False,'provider':PROVIDER,'stock_code':stock_code,
            'fields':{k:fields.get(k) for k in ('P1','P3','P5','P6','P9','P10','P11','P13','P14','P15','P16')},
            'source_http_date':http_date,'source_url':ajax_url,'artifact':artifact,
            'captured_at':datetime.now(HK).isoformat()}
