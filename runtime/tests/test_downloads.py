import concurrent.futures
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError
import fitz
from hkipo.downloads import PDFDownloader,DownloadError,official_url,OfficialRedirect
from hkipo.store import StateStore

def pdf_bytes(text='valid source'):
    doc=fitz.open();page=doc.new_page();page.insert_text((72,72),text);data=doc.tobytes();doc.close();return data

class DownloadTests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.store=StateStore(self.root/'db');self.url='https://www1.hkexnews.hk/listedco/test.pdf'
    def tearDown(self):self.tmp.cleanup()
    def test_pdf_verified_content_addressed_and_changed_preserved(self):
        data=[pdf_bytes('version one'),pdf_bytes('version two')]
        d=PDFDownloader(self.root/'pdf',self.store,fetcher=lambda u:(data.pop(0),u))
        first=d.download(self.url);second=d.download(self.url)
        self.assertFalse(first.changed);self.assertTrue(second.changed);self.assertEqual(second.previous_sha256,first.sha256)
        self.assertNotEqual(first.path,second.path);self.assertTrue(Path(first.path).exists());self.assertEqual(len(d.history(self.url)),2)
        self.assertEqual(list((self.root/'pdf').glob('.pdf-*')),[])
    def test_html_bad_pdf_http_and_redirect_rejected(self):
        for data in (b'<html>404</html>',b'%PDF-not really PDF'):
            with self.assertRaises(DownloadError):PDFDownloader(self.root/'pdf',self.store,fetcher=lambda u:(data,u)).download(self.url)
        with self.assertRaises(DownloadError):PDFDownloader(self.root/'pdf',self.store,fetcher=lambda u:(pdf_bytes(),'https://attacker.invalid/file.pdf')).download(self.url)
        for url in ('http://www1.hkexnews.hk/a.pdf','https://evil.hkexnews.hk/a.pdf','https://www1.hkexnews.hk/a.html','https://www1.hkexnews.hk@attacker.invalid/a.pdf'):
            with self.assertRaises(DownloadError):official_url(url)
    def test_network_failure_bounded_and_http_429_not_retried(self):
        attempts=[];sleeps=[]
        def broken(u):attempts.append(1);raise TimeoutError()
        with self.assertRaises(DownloadError):PDFDownloader(self.root/'pdf',self.store,fetcher=broken,sleep=sleeps.append).download(self.url)
        self.assertEqual(len(attempts),2);self.assertEqual(sleeps,[1])
        attempts.clear()
        def limited(u):attempts.append(1);raise HTTPError(u,429,'limited',{'Retry-After':'600'},None)
        with self.assertRaises(DownloadError):PDFDownloader(self.root/'pdf',self.store,fetcher=limited).download(self.url)
        self.assertEqual(len(attempts),1)
