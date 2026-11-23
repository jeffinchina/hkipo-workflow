import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
import fitz
from hkipo.contracts import CORE_FIELDS
from hkipo.evidence import prepare_documents
from hkipo.quality import validate_bundle


def fixture(root):
    source = root / 'source.pdf'; pdf = fitz.open()
    pdf.new_page().insert_text((72, 72), 'Issuer Example 1234 price 10.00 old 3 new 4 adjusted loss 50 unadjusted loss 100')
    pdf.new_page().insert_text((72, 72), 'Page two allocation data 200')
    pdf.save(source); pdf.close()
    docs = prepare_documents([source], root / 'texts'); d = docs['documents'][0]
    coverage = {'document_id': d['document_id'], 'sha256': d['sha256'], 'pages_total': 2, 'pages_reviewed': [1, 2], 'unreadable_pages': []}
    facts = []
    for key in CORE_FIELDS['offer']:
        facts.append({'id': key, 'key': key, 'value': '10.00', 'items': [], 'unit': '', 'currency': '', 'period': '', 'basis': 'fixture, semantic approval supplied separately', 'status': 'supported', 'evidence': [{'document_id': d['document_id'], 'pdf_page': 1, 'quote': 'price 10.00'}]})
    bundle = {'event_id': 'offline-test', 'kind': 'offer', 'sources_seen': [coverage], 'facts': facts, 'calculations': [], 'article': {'title': 'Offline test', 'title_fact_ids': [], 'blocks': [{'role': 'paragraph', 'text': 'Price 10.00', 'fact_ids': ['offer_price']}]}, 'gaps': [], 'author_inputs_used': []}
    review = {'decision': 'pass', 'title_checked': True, 'unreferenced_claims_checked': True, 'checked_fact_ids': [f['id'] for f in facts], 'reviewed_documents': [copy.deepcopy(coverage)], 'issues': [], 'summary': 'independent fixture review'}
    return bundle, docs, review


class QualityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.bundle, self.docs, self.review = fixture(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def report(self, review=True):
        return validate_bundle(self.bundle, self.docs, self.review if review else None)

    def test_pass_and_missing_review_blocks_delivery(self):
        self.assertTrue(self.report()['pass'])
        self.assertFalse(self.report(False)['pass'])

    def add_source_scoped_gap(self, status='not_disclosed'):
        self.bundle['facts'].append({'id': 'f20', 'key': 'replacement_offering_and_listing_dates',
            'value': '', 'items': [], 'unit': '', 'currency': '', 'period': '截至公告日期',
            'basis': '已阅读本公告全部页面；没有给出新的发售及上市日期',
            'status': status, 'evidence': []})
        self.bundle['article']['blocks'] = [
            {'role': 'heading', 'text': '下一步，等新的时间表', 'fact_ids': ['f20']},
            {'role': 'paragraph', 'text': '公司表示仍致力于适时完成全球发售及上市，但本公告没有给出新的发售及上市日期。', 'fact_ids': ['f20']}]
        self.review['checked_fact_ids'].append('f20')

    def test_scoped_gap_real_f20_shape_passes_with_independent_review(self):
        self.add_source_scoped_gap()
        report = self.report()
        self.assertTrue(report['pass'], report['blocking'])
        self.assertTrue(any('source-scoped gap reference f20' in a for a in report['advisory']))
        self.assertTrue(any('syntactic only' in a for a in report['limitations']))
        self.assertFalse(self.report(False)['pass'])

    def test_gap_missing_basis_or_unqualified_positive_absence_blocks(self):
        self.add_source_scoped_gap()
        fact = self.bundle['facts'][-1]
        fact['basis'] = ' '
        self.assertFalse(self.report()['pass'])
        fact['basis'] = 'Read all pages, disclosure not located'
        block = self.bundle['article']['blocks'][1]
        for text in ('没有给出新的发售日期。', '本公告说明了后续日期。',
                     '无基石', '本公告未披露，无基石。', '本公告未披露，没有绿鞋。'):
            with self.subTest(text=text):
                block['text'] = text
                self.assertFalse(self.report()['pass'])

    def test_colloquial_announcement_gap_from_real_replay_keeps_source_scope(self):
        self.add_source_scoped_gap()
        self.bundle['article']['blocks'][1]['text'] = '后面什么时候再来？公告只说仍致力于适时完成全球发售及上市，没有给出新日期。'
        self.assertTrue(self.report()['pass'])
        self.review['checked_fact_ids'].remove('f20')
        self.assertFalse(self.report()['pass'])

    def test_gap_heading_requires_scoped_explanation_and_no_absence_assertion(self):
        self.add_source_scoped_gap()
        self.bundle['article']['blocks'][0]['text'] = '无基石'
        self.assertFalse(self.report()['pass'])
        self.bundle['article']['blocks'] = self.bundle['article']['blocks'][:1]
        self.bundle['article']['blocks'][0]['text'] = '下一步，等新的时间表'
        self.assertFalse(self.report()['pass'])

    def test_source_scoped_not_specifically_disclosed_keeps_absence_guard(self):
        from hkipo.quality import _scoped_gap_text
        self.assertTrue(_scoped_gap_text('本招股书未具体披露基石安排。'))
        self.assertTrue(_scoped_gap_text('本招股书未明确披露基石安排。'))
        self.assertFalse(_scoped_gap_text('没有基石，本招股书未具体披露。'))

    def test_gap_must_be_checked_and_review_pass_and_full_coverage(self):
        self.add_source_scoped_gap()
        self.review['checked_fact_ids'].remove('f20')
        self.assertFalse(self.report()['pass'])
        self.review['checked_fact_ids'].append('f20')
        self.review['decision'] = 'needs_revision'
        self.assertFalse(self.report()['pass'])
        self.review['decision'] = 'pass'
        self.review['reviewed_documents'][0]['pages_reviewed'] = [1]
        self.assertFalse(self.report()['pass'])

    def test_not_applicable_can_be_source_scoped_but_unresolved_cannot(self):
        self.add_source_scoped_gap(status='not_applicable')
        self.bundle['article']['blocks'][1]['text'] = '本公告所述安排不适用于这一项。'
        self.assertTrue(self.report()['pass'])
        self.bundle['facts'][-1]['status'] = 'unresolved'
        self.assertFalse(self.report()['pass'])

    def test_false_page_and_false_quote_and_ellipsis(self):
        evidence = self.bundle['facts'][0]['evidence'][0]
        for page, quote in [(2, 'price 10.00'), (99, 'price 10.00'), (1, 'price...10.00'), (1, 'price 999')]:
            evidence.update(pdf_page=page, quote=quote)
            self.assertFalse(self.report()['pass'])

    def test_actual_hash_is_read_not_model_assertion(self):
        self.docs['documents'][0]['sha256'] = '0' * 64
        self.assertFalse(self.report()['pass'])

    def test_missing_core_and_hard_unsupported(self):
        self.bundle['facts'] = [f for f in self.bundle['facts'] if f['key'] != 'business']
        self.assertFalse(self.report()['pass'])
        self.bundle, self.docs, self.review = fixture(self.root)
        self.bundle['facts'][0]['status'] = 'unresolved'
        self.assertFalse(self.report()['pass'])

    def test_page_coverage_and_review_coverage(self):
        self.bundle['sources_seen'][0]['pages_reviewed'] = [1]
        self.assertFalse(self.report()['pass'])
        self.bundle['sources_seen'][0]['pages_reviewed'] = [1, 2]
        self.review['reviewed_documents'][0]['pages_reviewed'] = [1]
        self.assertFalse(self.report()['pass'])

    def test_unreadable_is_explicit_and_blocks(self):
        source = self.root / 'blank.pdf'; pdf = fitz.open(); pdf.new_page(); pdf.save(source); pdf.close()
        self.docs = prepare_documents([source], self.root / 'blank-texts')
        self.assertFalse(self.report()['pass'])

    def test_adjusted_vs_unadjusted_requires_semantic_review(self):
        f = next(f for f in self.bundle['facts'] if f['key'] == 'financials')
        f.update(value='50', basis='claims unadjusted loss')
        f['evidence'][0]['quote'] = 'adjusted loss 50'
        # Exact quote occurs, so the separate reviewer must reject its meaning.
        self.review['decision'] = 'needs_revision'
        self.review['issues'] = [{'severity': 'blocking', 'fact_id': f['id'], 'block_index': 0, 'problem': 'Adjusted loss mislabeled as unadjusted; source says 100', 'source_refs': [{'document_id': self.docs['documents'][0]['document_id'], 'pdf_page': 1, 'quote': 'unadjusted loss 100'}]}]
        report = self.report()
        self.assertFalse(report['pass'])
        self.assertTrue(any('mislabeled' in e for e in report['blocking']))

    def add_calculation(self, op='divide', result='0.75', basis='exact', unit='', currency=''):
        for fid, val in [('a', '3'), ('b', '4')]:
            self.bundle['facts'].append({'id': fid, 'key': fid, 'value': val, 'items': [], 'unit': '', 'currency': '', 'period': '', 'basis': '', 'status': 'supported', 'evidence': [{'document_id': self.docs['documents'][0]['document_id'], 'pdf_page': 1, 'quote': 'old 3 new 4'}]})
        self.bundle['calculations'] = [{'id': 'calc', 'operation': op, 'input_fact_ids': ['a', 'b'], 'value': result, 'unit': unit, 'currency': currency, 'basis': basis}]
        self.bundle['article']['blocks'][0]['fact_ids'] = ['calc']
        self.review['checked_fact_ids'] += ['calc', 'a', 'b']

    def test_decimal_arithmetic_all_operations(self):
        for op, value, unit, basis in [('divide', '0.75', '', ''), ('multiply', '12', '', ''), ('sum', '7', '', ''), ('subtract', '-1', '', ''), ('percent_change', '33.33', '%', 'round_half_up:2')]:
            self.bundle, self.docs, self.review = fixture(self.root)
            self.add_calculation(op, value, basis, unit)
            self.assertTrue(self.report()['pass'], self.report()['blocking'])
        self.bundle['calculations'][0]['value'] = '999'
        self.assertFalse(self.report()['pass'])

    def test_calculation_cross_currency_unit_divide_zero_and_bad_number(self):
        self.add_calculation()
        a, b = self.bundle['facts'][-2:]
        a['currency'], b['currency'] = 'HKD', 'CNY'
        self.assertFalse(self.report()['pass'])
        a['currency'] = b['currency'] = ''
        a['unit'], b['unit'] = 'shares', 'dollars'
        self.assertFalse(self.report()['pass'])
        a['unit'] = b['unit'] = ''; b['value'] = '0'
        self.assertFalse(self.report()['pass'])
        b['value'] = '4'; a['value'] = '3e2'
        self.assertFalse(self.report()['pass'])

    def test_explicit_shares_to_lots_and_directional_fx(self):
        from hkipo.quality import _calculate
        facts = {'shares': {'status':'supported','value':'3727600','unit':'shares','currency':''},
                 'lot': {'status':'supported','value':'100','unit':'shares_per_lot','currency':''}}
        calc = {'operation':'divide','input_fact_ids':['shares','lot'],'value':'37276',
                'unit':'lots','currency':'','basis':'exact'}
        self.assertEqual(_calculate(calc, facts), '37276')
        calc['unit'] = 'shares'
        with self.assertRaises(ValueError): _calculate(calc, facts)
        facts = {'profit': {'status':'supported','value':'5066000','unit':'元','currency':'CNY'},
                 'rate': {'status':'supported','value':'0.85604','unit':'CNY_per_HKD','currency':''}}
        calc.update(input_fact_ids=['profit','rate'], value='5917948', unit='元', currency='HKD', basis='round_half_up:0')
        self.assertTrue(_calculate(calc, facts).startswith('5917947.75945'))
        for change in [('rate','unit','HKD_per_CNY'), ('rate','value','-0.85604'), ('profit','unit','shares')]:
            fid,key,value=change; original=facts[fid][key];facts[fid][key]=value
            with self.assertRaises(ValueError): _calculate(calc, facts)
            facts[fid][key]=original
        calc['currency']='USD'
        with self.assertRaises(ValueError): _calculate(calc, facts)

    def test_same_unit_probability_and_percentage_keep_scale_and_rounding(self):
        self.add_calculation(result='0.0080092', unit='概率', basis='round_half_up:7')
        a, b = self.bundle['facts'][-2:]
        a.update(value='7', unit='人'); b.update(value='874', unit='人')
        calc = self.bundle['calculations'][0]
        self.assertTrue(self.report()['pass'], self.report()['blocking'])
        calc['basis'] = '四舍五入至小数点后7位'
        self.assertTrue(self.report()['pass'])
        for basis in ('约数', '四舍五入', '四舍五入至小数点后6位'):
            calc['basis'] = basis
            self.assertFalse(self.report()['pass'])
        calc.update(unit='%', value='0.80092', basis='round_half_up:5')
        self.assertTrue(self.report()['pass'], self.report()['blocking'])
        for unit, value in [('概率', '0.80092'), ('%', '0.0080092')]:
            calc.update(unit=unit, value=value, basis='round_half_up:7')
            self.assertFalse(self.report()['pass'])
        calc.update(unit='概率', value='0.0080092')
        a['currency'], b['currency'] = 'HKD', 'CNY'
        self.assertFalse(self.report()['pass'])

    def test_calculation_dependencies_resolve_without_order_and_require_full_review(self):
        self.add_calculation(op='multiply',result='12')
        downstream={'id':'downstream','operation':'divide','input_fact_ids':['calc','a'],
                    'value':'4','unit':'ratio','currency':'','basis':'exact'}
        self.bundle['calculations'].insert(0,downstream)
        self.bundle['article']['blocks'][0]['fact_ids']=['downstream']
        self.review['checked_fact_ids'].append('downstream')
        self.assertTrue(self.report()['pass'],self.report()['blocking'])
        self.review['checked_fact_ids'].remove('b')
        self.assertFalse(self.report()['pass'])
        self.review['checked_fact_ids'].append('b')
        self.bundle['calculations'][1]['input_fact_ids']=['downstream','b']
        self.assertFalse(self.report()['pass'])
        self.assertTrue(any('cyclic' in e for e in self.report()['blocking']))

    def test_explicit_amount_display_conversion_preserves_currency(self):
        from hkipo.quality import _calculate
        facts={'revenue':{'status':'supported','value':'434026','unit':'千元','currency':'CNY'}}
        calc={'id':'display','operation':'convert','input_fact_ids':['revenue'],'value':'4.34',
              'unit':'亿元','currency':'CNY','basis':'round_half_up:2'}
        self.assertEqual(_calculate(calc,facts),'4.34026')
        for changes in [{'currency':'HKD'},{'unit':'shares'},{'input_fact_ids':['revenue','revenue']},{'value':'43.4'}]:
            bad=dict(calc,**changes)
            with self.assertRaises(ValueError): _calculate(bad,facts)

    def test_canonical_price_times_lot_strict_pair_and_currency(self):
        self.add_calculation(op='multiply', result='5272', unit='per_lot', currency='HKD')
        a, b = self.bundle['facts'][-2:]
        a.update(value='26.36', unit='per_share', currency='HKD')
        b.update(value='200', unit='shares_per_lot')
        calc = self.bundle['calculations'][0]
        self.assertTrue(self.report()['pass'], self.report()['blocking'])
        calc['input_fact_ids'].reverse()
        self.assertTrue(self.report()['pass'])
        for changes in ({'unit': 'shares'}, {'currency': 'HKD'}, {'currency': 'CNY'}):
            original = b.copy(); b.update(changes)
            self.assertFalse(self.report()['pass']); b.update(original)
        calc['currency'] = 'CNY'
        self.assertFalse(self.report()['pass'])
        calc['currency'] = 'HKD'; calc['unit'] = '每手股份发售价金额'
        self.assertFalse(self.report()['pass'])
        calc['unit'] = 'per_lot'; calc['input_fact_ids'].append('b')
        self.assertFalse(self.report()['pass'])

    def test_replay_and_reading_metadata_supported_without_evidence_still_fail(self):
        for fid, key, value, items in [
                ('f33', 'reviewed_documents', '', ['已阅读全部逐页原文']),
                ('f34', 'event.replay_scope', '截至2026-05-08的历史回放研究稿', [])]:
            self.bundle['facts'].append({'id': fid, 'key': key, 'value': value,
                'items': items, 'unit': '', 'currency': '', 'period': '',
                'basis': '任务输入或阅读审计记录', 'status': 'supported', 'evidence': []})
            self.review['checked_fact_ids'].append(fid)
        report = self.report()
        for fid in ('f33', 'f34'):
            self.assertTrue(any('fact ' + fid + ': supported requires' in e for e in report['blocking']))

    def test_review_covers_refs_hard_and_calculation_inputs(self):
        self.add_calculation(); self.review['checked_fact_ids'].remove('a')
        self.assertFalse(self.report()['pass'])

    def test_numeric_block_requires_refs_except_enumeration_and_exempt_roles(self):
        block = self.bundle['article']['blocks'][0]; block.update(role='opinion', text='1、Opinion only', fact_ids=[])
        self.assertTrue(self.report()['pass'])
        block['text'] = '1、Revenue 123'
        self.assertFalse(self.report()['pass'])
        block['role'] = 'heading'
        self.assertTrue(self.report()['pass'])

    def test_optional_gap_does_not_block_and_supported_needs_quote(self):
        f = next(f for f in self.bundle['facts'] if f['key'] == 'cornerstones')
        f.update(status='not_disclosed', basis='No explicit disclosure found in reviewed offering section', evidence=[])
        self.assertTrue(self.report()['pass'])
        f['status'] = 'supported'
        self.assertFalse(self.report()['pass'])

    def test_exception_bulletin_can_omit_unknown_action_but_cannot_invent_one(self):
        self.bundle['kind'] = 'exception'; self.bundle['facts'] = []
        for key in CORE_FIELDS['exception']:
            self.bundle['facts'].append({'id': key, 'key': key, 'value': 'text', 'items': [], 'unit': '', 'currency': '', 'period': '', 'basis': '', 'status': 'supported', 'evidence': [{'document_id': self.docs['documents'][0]['document_id'], 'pdf_page': 1, 'quote': 'Issuer Example'}]})
        action = next(f for f in self.bundle['facts'] if f['key'] == 'investor_action')
        action.update(status='not_applicable', value='', evidence=[], basis='Source does not prescribe investor action')
        self.bundle['article']['blocks'] = [{'role': 'opinion', 'text': 'Keep a cautious view.', 'fact_ids': []}]
        self.review['checked_fact_ids'] = [f['id'] for f in self.bundle['facts']]
        self.assertTrue(self.report()['pass'])
        action.update(status='unresolved', basis='Action not established; no instruction in article')
        self.assertTrue(self.report()['pass'])
        self.bundle['article']['blocks'].append({'role': 'paragraph', 'text': 'Cancel the application now.', 'fact_ids': ['investor_action']})
        self.assertFalse(self.report()['pass'])
        self.bundle['article']['blocks'].pop()
        action['status'] = 'not_applicable'
        action['basis'] = ''
        self.assertFalse(self.report()['pass'])

    def test_duplicate_fact_id_and_unknown_article_reference(self):
        self.bundle['facts'].append(copy.deepcopy(self.bundle['facts'][0]))
        self.assertFalse(self.report()['pass'])
        self.bundle['facts'].pop()
        self.bundle['article']['blocks'][0]['fact_ids'] = ['made_up']
        self.assertFalse(self.report()['pass'])

    def test_nfkc_whitespace_quote_is_supported_but_punctuation_is_not_removed(self):
        self.bundle['facts'][0]['evidence'][0]['quote'] = 'price １０.００'
        self.assertTrue(self.report()['pass'])
        self.bundle['facts'][0]['evidence'][0]['quote'] = 'price 10,00'
        self.assertFalse(self.report()['pass'])

    def test_title_empty_body_and_numeric_title_without_refs(self):
        self.bundle['article']['title'] = 'Price 999999'
        self.assertFalse(self.report()['pass'])
        self.bundle['article']['title'] = ''
        self.assertFalse(self.report()['pass'])
        self.bundle['article']['title'] = 'Offline test'
        self.bundle['article']['blocks'] = []
        self.assertFalse(self.report()['pass'])
        self.bundle['article']['blocks'] = [{'role': 'paragraph', 'text': '  ', 'fact_ids': ['offer_price']}]
        self.assertFalse(self.report()['pass'])

    def test_nonnumeric_claim_requires_references_for_factual_roles(self):
        block = self.bundle['article']['blocks'][0]
        block.update(text='The company has no cornerstone investor', fact_ids=[])
        for role in ['paragraph', 'field', 'hook', 'item']:
            block['role'] = role
            self.assertFalse(self.report()['pass'])
        block.update(role='opinion', text='I prefer a cautious approach')
        self.assertTrue(self.report()['pass'])
        block['text'] = 'I expect 50 percent upside'
        self.assertFalse(self.report()['pass'])

    def test_review_explicit_title_and_unreferenced_claim_check_required(self):
        for field in ['title_checked', 'unreferenced_claims_checked']:
            self.review[field] = False
            self.assertFalse(self.report()['pass'])
            self.review[field] = 'true'
            self.assertFalse(self.report()['pass'])
            self.review[field] = True
        self.assertTrue(self.report()['pass'])

    def test_title_refs_supported_and_in_independent_review(self):
        self.bundle['article']['title'] = 'Price 10.00'
        self.bundle['article']['title_fact_ids'] = ['cornerstones']
        self.review['checked_fact_ids'].remove('cornerstones')
        self.assertFalse(self.report()['pass'])
        self.review['checked_fact_ids'].append('cornerstones')
        self.assertTrue(self.report()['pass'])
        f = next(f for f in self.bundle['facts'] if f['id'] == 'cornerstones')
        f.update(status='unresolved')
        self.assertFalse(self.report()['pass'])

    def visual_fixture(self, blank=False):
        source = self.root / 'with-visual.pdf'
        with fitz.open(self.docs['documents'][0]['path']) as pdf:
            page = pdf.new_page()
            if not blank:
                page.insert_text((40, 750), '3')
                image = fitz.open(); scan = image.new_page()
                scan.insert_text((40, 60), 'Scanned revenue 9876')
                png = scan.get_pixmap().tobytes('png'); image.close()
                page.insert_image(fitz.Rect(40, 40, 400, 650), stream=png)
            pdf.save(source)
        old_id = self.docs['documents'][0]['document_id']
        self.docs = prepare_documents([source], self.root / 'visual-text')
        d = self.docs['documents'][0]
        for f in self.bundle['facts']:
            for ref in f['evidence']:
                ref['document_id'] = d['document_id']
        coverage = {'document_id': d['document_id'], 'sha256': d['sha256'], 'pages_total': 3,
                    'pages_reviewed': [1, 2, 3], 'unreadable_pages': []}
        self.bundle['sources_seen'] = [coverage]
        self.review['reviewed_documents'] = [copy.deepcopy(coverage)]
        return d

    def make_sidecar(self, d, decision='transcribed'):
        page = d['pages'][2]
        sidecar = {'document_id': d['document_id'], 'pdf_sha256': d['sha256'], 'pdf_page': 3,
                   'image_sha256': page['image_sha256'], 'reviewer': 'offline-independent-fixture',
                   'reviewed_at': '2026-11-20T10:30:00+08:00', 'decision': decision,
                   'transcription': 'Scanned revenue 9876' if decision == 'transcribed' else '',
                   'reason': 'Fixture visual check of full page; all meaningful image text transcribed' if decision == 'transcribed' else 'Fixture reviewer confirms decorative image only'}
        path = self.root / 'visual-review.json'
        path.write_text(json.dumps(sidecar))
        return path

    def visual_report(self, path, trusted=True):
        return validate_bundle(self.bundle, self.docs, self.review, visual_reviews=[path],
                               trusted_visual_review_sha256s=[hashlib.sha256(path.read_bytes()).hexdigest()] if trusted else [])

    def test_scan_footer_needs_trusted_visual_review_and_can_quote_transcription(self):
        d = self.visual_fixture()
        self.assertFalse(self.report()['pass'])
        path = self.make_sidecar(d)
        self.assertFalse(self.visual_report(path, trusted=False)['pass'])
        self.assertTrue(self.visual_report(path)['pass'])
        f = next(f for f in self.bundle['facts'] if f['key'] == 'financials')
        f['value'] = '9876'; f['evidence'] = [{'document_id': d['document_id'], 'pdf_page': 3, 'quote': 'Scanned revenue 9876'}]
        self.assertTrue(self.visual_report(path)['pass'])
        self.assertFalse(self.report()['pass'])
        # Same PDF and same independently approved sidecar are reusable.
        self.assertTrue(self.visual_report(path)['pass'])

    def test_blank_page_does_not_permanently_block(self):
        d = self.visual_fixture(blank=True)
        self.assertEqual(d['pages'][2]['status'], 'blank')
        self.assertTrue(self.report()['pass'])

    def test_sidecar_tampering_source_page_image_and_audit_fields_fail(self):
        d = self.visual_fixture(); path = self.make_sidecar(d)
        original = json.loads(path.read_text())
        trusted = hashlib.sha256(path.read_bytes()).hexdigest()
        altered = dict(original, transcription='forged 9999'); path.write_text(json.dumps(altered))
        report = validate_bundle(self.bundle, self.docs, self.review, visual_reviews=[path], trusted_visual_review_sha256s=[trusted])
        self.assertFalse(report['pass'])
        for key, value in [('pdf_sha256', '0'*64), ('pdf_page', 99), ('image_sha256', '0'*64), ('reviewer', ''), ('reviewed_at', '2026-11-20'), ('decision', 'pass')]:
            altered = dict(original); altered[key] = value; path.write_text(json.dumps(altered))
            self.assertFalse(self.visual_report(path)['pass'], key)

    def test_non_substantive_visual_disposition_cannot_supply_image_numbers(self):
        d = self.visual_fixture(); path = self.make_sidecar(d, 'no_substantive_content')
        self.assertTrue(self.visual_report(path)['pass'])
        f = next(f for f in self.bundle['facts'] if f['key'] == 'financials')
        f['evidence'] = [{'document_id': d['document_id'], 'pdf_page': 3, 'quote': 'Scanned revenue 9876'}]
        self.assertFalse(self.visual_report(path)['pass'])
