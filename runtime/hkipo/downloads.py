"""Content-addressed, verified official PDF acquisition; no model calls."""
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

class DownloadError(RuntimeError):
    def __init__(self,message,retry_after=None):
        super().__init__(message);self.retry_after=retry_after

# These exact HTTPS hosts have been observed in official document links. Do not
# accept arbitrary subdomains merely because they end with hkexnews.hk.
OFFICIAL_HOSTS=frozenset({'www1.hkexnews.hk','www.hkexnews.hk','www2.hkexnews.hk'})
def official_url(url):
    p=urlsplit(url)
    if p.scheme!='https' or p.hostname not in OFFICIAL_HOSTS or p.username or p.password or p.port not in (None,443):
        raise DownloadError('unapproved_document_url')
    if not p.path.lower().endswith('.pdf'):raise DownloadError('document_url_not_pdf')
    return urlunsplit(('https',p.hostname,p.path,p.query,''))

class OfficialRedirect(HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        official_url(newurl)
        return super().redirect_request(req,fp,code,msg,headers,newurl)

@dataclass(frozen=True)
class DownloadedPDF:
    url: str
    final_url: str
    path: str
    sha256: str
    pages: int
    downloaded_at: str
    changed: bool
    previous_sha256: str = ''
    def to_dict(self):return dict(self.__dict__)

class PDFDownloader:
    def __init__(self,root,store,fetcher=None,attempts=2,max_bytes=150*1024*1024,sleep=time.sleep):
        self.root=Path(root).resolve();self.root.mkdir(parents=True,exist_ok=True)
        self.fetcher=fetcher or self._fetch;self.attempts=attempts;self.max_bytes=max_bytes;self.sleep=sleep
        if not 1<=attempts<=4:raise ValueError('attempts out of bounds')
        self.db=store.dbpath
        with sqlite3.connect(self.db) as c:
            c.execute('CREATE TABLE IF NOT EXISTS pdf_versions(url TEXT NOT NULL,sha TEXT NOT NULL,path TEXT NOT NULL,downloaded_at TEXT NOT NULL,final_url TEXT NOT NULL,pages INTEGER NOT NULL,PRIMARY KEY(url,sha))')
    def _fetch(self,url):
        # urllib raises HTTP errors, so a 404 HTML body never reaches validation.
        with build_opener(OfficialRedirect()).open(Request(url,headers={'User-Agent':'HKIPO-LocalMonitor/0.1','Accept':'application/pdf'}),timeout=30) as response:
            final=official_url(response.geturl())
            data=response.read(self.max_bytes+1)
            return data,final
    def download(self,url):
        url=official_url(url)
        data=None
        for attempt in range(self.attempts):
            try:
                data,final=self.fetcher(url);official_url(final)
                break
            except HTTPError as exc:
                # A Retry-After is never ignored. Hand a rate-limit response to
                # the job retry schedule instead of attempting immediately.
                if exc.code==429:
                    value=exc.headers.get('Retry-After','') if exc.headers else ''
                    try:delay=max(0,float(value))
                    except ValueError:
                        try:delay=max(0,(parsedate_to_datetime(value)-datetime.now(timezone.utc)).total_seconds())
                        except (ValueError,TypeError):delay=300
                    raise DownloadError('http_429_retry_later',retry_after=delay) from None
                if exc.code not in {500,502,503,504}:raise DownloadError('http_status_'+str(exc.code)) from None
            except DownloadError:raise
            except (URLError,TimeoutError,OSError):pass
            except Exception:raise DownloadError('download_transport_failed') from None
            if attempt+1<self.attempts:self.sleep(2**attempt)
        if data is None:raise DownloadError('download_recovery_exhausted')
        if not isinstance(data,bytes) or not data.startswith(b'%PDF-') or len(data)>self.max_bytes:
            raise DownloadError('invalid_pdf_signature_or_size')
        import fitz
        try:
            with fitz.open(stream=data,filetype='pdf') as pdf:
                if pdf.needs_pass or pdf.page_count<1:raise ValueError()
                pages=pdf.page_count
                for page in pdf:page.get_text('text')
        except Exception:raise DownloadError('pdf_unreadable_or_encrypted') from None
        digest=hashlib.sha256(data).hexdigest();target=self.root/(digest+'.pdf')
        fd,temp=tempfile.mkstemp(prefix='.pdf-',dir=self.root)
        try:
            with os.fdopen(fd,'wb') as stream:
                stream.write(data);stream.flush();os.fsync(stream.fileno())
            os.replace(temp,target)
        finally:
            if os.path.exists(temp):os.unlink(temp)
        now=datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.db,timeout=30) as c:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('SELECT sha FROM pdf_versions WHERE url=? ORDER BY downloaded_at DESC LIMIT 1',(url,)).fetchone()
            previous=old[0] if old else ''
            c.execute('INSERT INTO pdf_versions VALUES(?,?,?,?,?,?) ON CONFLICT(url,sha) DO UPDATE SET downloaded_at=excluded.downloaded_at,final_url=excluded.final_url',(url,digest,str(target),now,final,pages))
        return DownloadedPDF(url,final,str(target),digest,pages,now,bool(previous and previous!=digest),previous)
    def history(self,url):
        with sqlite3.connect(self.db) as c:
            c.row_factory=sqlite3.Row
            return [dict(r) for r in c.execute('SELECT * FROM pdf_versions WHERE url=? ORDER BY downloaded_at',(official_url(url),))]
