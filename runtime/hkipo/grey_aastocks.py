"""Finite licensed-source price-line read; no streaming or account access.

AAStocks' official grey-market page labels .PH as Phillip Securities. Its public
chart is a five-minute price line, not minute OHLC. Webpage-generated Auth is
computed in memory exactly as the public client; no Auth/token is archived.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, ProxyHandler, build_opener, HTTPRedirectHandler
from .grey_market import HK, timestamp
from .grey_source import GreySourceUnavailable, _bad, _dec, _json, _store

HOSTS={'www.aastocks.com','ds1.aastocks.com','fctdata.aastocks.com'}
PAGE='https://www.aastocks.com/tc/stocks/market/ipo/greymarket.aspx'
CHART='https://ds1.aastocks.com/chart/getWatchList'
AUTH_JS='''const c=require('node:crypto');let s='';process.stdin.on('data',x=>s+=x).on('end',()=>{const p=JSON.parse(s),iv=Buffer.from(p.k2),key=Buffer.from(p.cc);const d=c.createDecipheriv('aes-'+key.length*8+'-cbc',key,iv);const k=Buffer.concat([d.update(Buffer.from(p.k1,'hex')),d.final()]);const e=c.createCipheriv('aes-'+k.length*8+'-cbc',k,iv);process.stdout.write(Buffer.concat([e.update('AA'+Date.now()),e.final()]).toString('hex'));});'''


class _Redirect(HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        if urlsplit(newurl).scheme!='https' or urlsplit(newurl).hostname not in HOSTS:
            _bad('aastocks_redirect_rejected')
        return super().redirect_request(req,fp,code,msg,headers,newurl)


class Reader:
    def __init__(self,proxy_url=None):
        proxies={}
        if proxy_url:
            p=urlsplit(proxy_url)
            if (p.scheme!='http' or p.hostname not in ('127.0.0.1','localhost','::1')
                    or not p.port or p.username or p.password or p.path not in ('','/') or p.query or p.fragment):
                _bad('loopback_proxy_required')
            proxies={'https':proxy_url}
        self.opener=build_opener(_Redirect(),ProxyHandler(proxies))

    def __call__(self,url,headers=None):
        p=urlsplit(url)
        if p.scheme!='https' or p.hostname not in HOSTS or p.username or p.password:
            _bad('aastocks_url_rejected')
        try:
            req=Request(url,headers={'User-Agent':'Mozilla/5.0','Referer':PAGE,**(headers or {})})
            with self.opener.open(req,timeout=15) as response:
                body=response.read(8_000_001)
                if len(body)>8_000_000:_bad('aastocks_response_oversized')
                return body
        except GreySourceUnavailable:raise
        except Exception:_bad('aastocks_source_unavailable')


def page_config(page,code):
    try:text=page.decode('utf-8-sig')
    except UnicodeError:_bad('aastocks_page_encoding')
    # The visible source label and chart symbol are both mandatory.
    if (not re.search(r'資料來源\s*:\s*輝立証券',text)
            or not re.search(r'symbol:\s*["\']'+re.escape(code)+r'\.PH["\']',text)):
        _bad('aastocks_phillip_venue_mapping_missing')
    try:
        part=re.search(r'Chart:\s*\{(.*?)\},\s*Timer:',text,re.S).group(1)
        params={k:re.search(r'\b'+k+r':\s*"([A-Za-z0-9]+)"',part).group(1) for k in ('cc','k1','k2')}
    except AttributeError:_bad('aastocks_public_chart_parameters_changed')
    scrubbed=text
    for value in params.values():scrubbed=scrubbed.replace(value,'[PUBLIC_CHART_PARAMETER_REDACTED]')
    return params,scrubbed.encode()


def chart_auth(params):
    node=os.environ.get('HKIPO_NODE') or shutil.which('node')
    if not node:_bad('node_required_for_public_chart_client')
    try:
        run=subprocess.run([str(node),'-e',AUTH_JS],input=json.dumps(params),capture_output=True,text=True,timeout=5,check=True)
    except Exception:_bad('aastocks_chart_client_unavailable')
    auth=run.stdout.strip()
    if not re.fullmatch(r'[0-9a-f]{32,128}',auth):_bad('aastocks_chart_client_invalid')
    return auth


def parse_price_line(response,session,captured_at):
    code=session['stock_code'];start,end=timestamp(session['session_start']),timestamp(session['session_end'])
    captured=timestamp(captured_at)
    if captured<end:_bad('session_not_closed')
    if response.get('status')!=1 or not isinstance(response.get('stocks'),list) or len(response['stocks'])!=1:
        _bad('aastocks_full_chart_missing')
    stock=response['stocks'][0]
    if stock.get('s')!=code+'.PH' or stock.get('i')!='5m':_bad('aastocks_phillip_symbol_or_interval')
    data=stock.get('data')
    if not isinstance(data,list):_bad('aastocks_full_chart_missing')
    points=[]
    for point in data:
        if not isinstance(point,list) or len(point)!=2 or type(point[0]) is not int:
            _bad('aastocks_price_point_invalid')
        t=datetime.fromtimestamp(point[0]/1000,timezone.utc).astimezone(HK)
        points.append({'time':t.isoformat(),'price':str(_dec(point[1]))})
    required=[start+timedelta(seconds=300*n) for n in range(1,int((end-start).total_seconds()/300)+1)]
    if [timestamp(p['time']) for p in points]!=required:_bad('aastocks_full_session_sampling_required')
    for field in ('time','lut'):
        value=response.get('time') if field=='time' else stock.get('lut')
        if type(value) is not int:_bad('aastocks_quote_update_time_required')
        t=datetime.fromtimestamp(value/1000,timezone.utc).astimezone(HK)
        if t.date().isoformat()!=session['trade_date'] or t>captured+timedelta(minutes=1):
            _bad('aastocks_chart_trade_date_conflict')
    return {'series_kind':'sampled_price_line','points':points,'source_url':PAGE+'?symbol='+code,
        'chart_source_url':CHART+'?id='+code+'.PH','venue_symbol':code+'.PH',
        'coverage':{'complete':True,'has_more':False,'requested_start':start.isoformat(),
                    'requested_end':end.isoformat(),'interval_seconds':300,'first_bucket_start':start.isoformat(),
                    'sampling':'official_chart_five_minute_right_edge','full_response':True},
        'source_attribution':'辉立证券行情，经AAStocks官方行情页转引',
        'captured_at':captured.isoformat(),'source_lut':stock['lut']}


def fetch_price_line(session,directory,*,proxy_url=None,now=None,fetcher=None,token_maker=None):
    current=timestamp(now.isoformat()) if isinstance(now,datetime) else timestamp(now) if now else datetime.now(HK)
    if current<timestamp(session['session_end']):_bad('session_not_closed')
    code=session.get('stock_code','')
    if not re.fullmatch(r'\d{5}',code):_bad('security_identity')
    fetcher=fetcher or Reader(proxy_url)
    page=fetcher(PAGE+'?symbol='+code)
    params,scrubbed=page_config(page,code)
    auth=(token_maker or chart_auth)(params)
    raw=fetcher(CHART+'?id='+code+'.PH',headers={'Authorization':auth})
    del auth,params
    root=Path(directory)/(session['trade_date']+'-'+code+'-PH')
    evidence={'page':_store(root,'aastocks-page',scrubbed),'chart':_store(root,'aastocks-ph-chart',raw),
              'original_page_sha256':hashlib.sha256(page).hexdigest(),'page_public_parameters_redacted':True}
    captured=current if now else datetime.now(HK)
    result=parse_price_line(_json(raw),session,captured.isoformat());result['source_artifacts']=evidence
    return result
