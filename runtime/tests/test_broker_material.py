import io
import tempfile
import unittest
from pathlib import Path
from PIL import Image
from hkipo.broker_material import preserve_originals, SourceNeeded, review_candidate

URL='https://nnqimage.futunn.com/test.png'
def png():
    out=io.BytesIO(); Image.new('RGB',(100,100)).save(out,format='PNG'); return out.getvalue()

def candidate():
    return {'identity_status':'single_stock_candidate','data_scope':'broker_customer_allotment','stock_codes':['01234'],'body':'甲公司(01234.HK)已经公布中签。11月20日上市。','published_timestamp':1795132800,'images':[URL],'url':'https://q.futunn.com/feed/123'}

class MaterialTests(unittest.TestCase):
    def test_originals_deduplicate_and_reject(self):
        with tempfile.TemporaryDirectory() as d:
            data=png(); result=preserve_originals([URL,URL+'?x'],d,lambda _:data)
            self.assertEqual(len(result),1); self.assertEqual(Path(result[0]['path']).read_bytes(),data)
            for url,raw in [('https://evil.com/test.png',data),(URL,data[:-5]),(URL,b'x'*1_000_000)]:
                with self.assertRaises(SourceNeeded): preserve_originals([url],d,lambda _:raw)
    def test_double_review_and_cache(self):
        calls=[]; gates=[]
        facts=[{'id':k,'value':v,'quote':v,'image_index':0,'bbox':[0,0,1,1]} for k,v in [('company','甲公司'),('code','01234'),('application_unit','股'),('allotment_unit','股'),('A1.application','100'),('A1.minimum','0'),('A1.maximum','100')]]
        def model(prompt,schema,out,name,**kw):
            calls.append(name); self.assertTrue(Path(kw['images'][0]).exists()); self.assertEqual(kw['effort'],'low')
            if name=='author': return {'facts':facts}
            return {'checks':[{**{k:f[k] for k in ('id','quote','image_index','bbox')},'agree':True} for f in facts]}
        with tempfile.TemporaryDirectory() as d:
            opts=dict(first_seen='2026-11-20T00:00:00Z',downloader=lambda _:png(),model_runner=model,quota=lambda **_:gates.append(1))
            result=review_candidate(candidate(),d,**opts)
            self.assertEqual(result['status'],'reviewed'); self.assertIn('客户样本',result['event']['brief']); self.assertEqual(len(gates),4)
            self.assertEqual(review_candidate(candidate(),d,**opts),result); self.assertEqual(len(calls),2)
    def test_block_and_cache(self):
        calls=[]
        def model(*a,**k): calls.append(1); return {'facts':[]}
        with tempfile.TemporaryDirectory() as d:
            opts=dict(first_seen='now',downloader=lambda _:png(),model_runner=model,quota=lambda **_:None)
            self.assertEqual(review_candidate(candidate(),d,**opts)['status'],'needs_review')
            review_candidate(candidate(),d,**opts); self.assertEqual(len(calls),1)
    def test_mapping_never_downloads(self):
        c=candidate(); c['identity_status']='needs_identity_mapping'
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(review_candidate(c,d,first_seen='now',downloader=lambda _:self.fail())['status'],'needs_identity_mapping')
    def test_independent_disagreement_and_episode(self):
        facts=[{'id':k,'value':v,'quote':v,'image_index':0,'bbox':[0,0,1,1]} for k,v in [('company','甲公司'),('code','unknown'),('application_unit','股'),('allotment_unit','股'),('B1.application','500'),('B1.minimum','100'),('B1.maximum','200')]]
        def model(*a,**kw):
            if a[3]=='author': return {'facts':facts}
            return {'checks':[{**{k:f[k] for k in ('id','quote','image_index','bbox')},'agree':True} for f in facts if f['value']!='unknown']}
        with tempfile.TemporaryDirectory() as d:
            c=candidate(); c['body']='甲公司(01234.HK)已经公布中签。'
            result=review_candidate(c,d,first_seen='now',downloader=lambda _:png(),model_runner=model,quota=lambda **_:None)
            self.assertEqual(result['status'],'needs_episode'); self.assertNotIn('event',result)
    def test_quota_deferred_propagates(self):
        from hkipo.quota import QuotaDeferred
        with tempfile.TemporaryDirectory() as d:
            def gate(**kw): raise QuotaDeferred({'reason':'test'})
            with self.assertRaises(QuotaDeferred):
                review_candidate(candidate(),d,first_seen='now',downloader=lambda _:png(),model_runner=lambda *a,**kw:self.fail(),quota=gate)
