import copy,unittest
from hkipo.contracts import CORE_FIELDS
from hkipo.offer_editorial import apply_offer_editorial,SECTIONS,FIELDS

class OfferEditorialTests(unittest.TestCase):
    def bundle(self):
        blocks=[{'role':'hook','text':'一句行业看点。'}]
        for title in SECTIONS:
            blocks.append({'role':'heading','text':title})
            if title==SECTIONS[1]:blocks.extend([{'role':'heading','text':'营收'},{'role':'paragraph','text':'2025年：1亿人民币'},{'role':'heading','text':'净亏损'},{'role':'paragraph','text':'2025年：亏损0.1亿'}])
            if title==SECTIONS[2]:blocks.extend({'role':'field','text':k+'：'+('香港初始1万手' if k=='发行数量' else '测试')} for k in FIELDS)
        return {'kind':'offer','article':{'blocks':blocks},'facts':[{'key':k,'status':'supported'} for k in CORE_FIELDS['offer']]}
    def check(self,b):return apply_offer_editorial({'pass':True,'blocking':[]},b)
    def test_valid_structure_does_not_replace_fact_check(self):
        b=self.bundle();self.assertTrue(self.check(b)['pass'])
        result=apply_offer_editorial({'pass':False,'blocking':['bad source quote']},b)
        self.assertFalse(result['pass']);self.assertIn('bad source quote',result['blocking'])
    def test_unresolved_greenshoe_cannot_be_hidden(self):
        b=self.bundle();next(f for f in b['facts'] if f['key']=='over_allotment')['status']='unresolved';self.assertFalse(self.check(b)['pass'])
    def test_missing_fields_and_wrong_sponsor_title(self):
        b=self.bundle();b['article']['blocks']=[{**p,'text':p['text'].replace('保荐人：','独家保荐人：')} for p in b['article']['blocks'] if not p['text'].startswith('绿鞋：')]
        self.assertFalse(self.check(b)['pass'])
    def test_stock_only_quantity_or_collapsed_financials_rejected(self):
        b=self.bundle();b['article']['blocks']=[{**p,'text':'发行数量：100万股'} if p['text'].startswith('发行数量：') else p for p in b['article']['blocks']]
        self.assertFalse(self.check(b)['pass'])
        b=self.bundle();b['article']['blocks']=[p for p in b['article']['blocks'] if p['text']!='营收'];self.assertFalse(self.check(b)['pass'])

    def test_unverified_listing_and_unsupported_absence_blocked(self):
        b=self.bundle();next(f for f in b['facts'] if f['key']=='listing_date')['status']='unresolved'
        self.assertFalse(self.check(b)['pass'])
        b=self.bundle();f=next(f for f in b['facts'] if f['key']=='cornerstones');f.update(status='not_disclosed',basis='未检索到')
        self.assertFalse(self.check(b)['pass'])
    def test_extra_preface_not_hidden_as_disclaimer(self):
        b=self.bundle();b['article']['blocks'].insert(1,{'role':'disclaimer','text':'按历史时点记录。'})
        self.assertFalse(self.check(b)['pass'])
    def test_public_rounding_keeps_full_internal_precision(self):
        b=self.bundle()
        next(p for p in b['article']['blocks'] if p['text']=='2025年：1亿人民币')['text']='2025年：7.96亿元人民币'
        b['facts'].append({'key':'financial.revenue.2025','status':'supported','value':'7.95504'})
        self.assertTrue(self.check(b)['pass'])
        next(p for p in b['article']['blocks'] if '7.96亿元' in p['text'])['text']='2025年：7.95504亿元人民币'
        self.assertFalse(self.check(b)['pass'])
    def test_internal_audit_chain_is_not_reader_prose(self):
        b=self.bundle();b['article']['blocks'].append({'role':'paragraph','text':'经PDF第282页逐页核验，状态not_disclosed。'})
        self.assertFalse(self.check(b)['pass'])
    def test_simple_greenshoe_does_not_require_routine_conditions_in_body(self):
        b=self.bundle()
        for p in b['article']['blocks']:
            if p['text'].startswith('绿鞋：'):p['text']='绿鞋：有'
            if p['text'].startswith('基石：'):p['text']='基石：无'
        self.assertTrue(self.check(b)['pass'])
    def test_author_currency_subheading_is_valid(self):
        b=self.bundle()
        next(p for p in b['article']['blocks'] if p['text']=='营收')['text']='营收（人民币）'
        self.assertTrue(self.check(b)['pass'])
