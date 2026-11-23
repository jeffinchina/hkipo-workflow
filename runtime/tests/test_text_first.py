"""Offline v0.6 regression: actual PDFs, mocked model boundaries only."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fitz
from hkipo.evidence import prepare_documents
from hkipo.quality import validate_bundle
from hkipo.writer import write_article, freeze_release
from test_quality import fixture


class TextFirstTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bundle, _, self.review = fixture(self.root)
        self.source = self.root / 'source.pdf'
        pdf = fitz.open(self.source)
        pdf[0].draw_rect(fitz.Rect(50, 120, 100, 200), fill=(1, 0, 0))
        pdf.saveIncr(); pdf.close()
        self.docs = prepare_documents([self.source], self.root / 'native', render_visual=False)
        d = self.docs['documents'][0]
        coverage = dict(document_id=d['document_id'], sha256=d['sha256'], pages_total=2,
                        pages_reviewed=[1, 2], unreadable_pages=[])
        self.bundle['sources_seen'] = [coverage]
        self.review['reviewed_documents'] = [copy.deepcopy(coverage)]
        for fact in self.bundle['facts']:
            for ref in fact['evidence']:
                ref['document_id'] = d['document_id']

    def tearDown(self):
        self.tmp.cleanup()

    def test_text_first_preserves_unread_visual_without_preflight_render(self):
        d = self.docs['documents'][0]
        self.assertEqual(d['pages'][0]['status'], 'visual_required')
        self.assertNotIn('image_path', d['pages'][0])
        self.assertTrue(d['pages'][0]['native_blocks'])
        with patch('hkipo.evidence.read_pdf_pages', side_effect=AssertionError('native cache not reused')):
            cached = prepare_documents([self.source], self.root / 'native', render_visual=False)
        self.assertEqual(cached['documents'][0]['pages_sha256'], d['pages_sha256'])
        report = validate_bundle(self.bundle, self.docs, self.review, source_mode='text_first',
                                 scoped_visual_registry='/irrelevant/missing/registry.json')
        self.assertTrue(report['pass'], report['blocking'])
        self.assertEqual(report['visual_reviews_used'], [])
        self.assertEqual(report['unreviewed_visual_pages'][0]['pdf_page'], 1)
        legacy = validate_bundle(self.bundle, self.docs, self.review)
        self.assertFalse(legacy['pass'])
        self.assertTrue(any('visual review required' in x for x in legacy['blocking']))

    def test_quote_core_and_independent_review_stay_hard_gates(self):
        bad = copy.deepcopy(self.bundle)
        bad['facts'][0]['evidence'][0]['quote'] = 'invented number 999'
        self.assertFalse(validate_bundle(bad, self.docs, self.review, source_mode='text_first')['pass'])
        bad = copy.deepcopy(self.bundle)
        bad['facts'] = [f for f in bad['facts'] if f['key'] != 'business']
        self.assertFalse(validate_bundle(bad, self.docs, self.review, source_mode='text_first')['pass'])
        self.assertFalse(validate_bundle(self.bundle, self.docs, source_mode='text_first')['pass'])

    def run_writer(self, **kwargs):
        calls = []
        def fake(prompt, schema, outdir, name, *args, **kw):
            calls.append((name, prompt, Path(outdir)))
            value = copy.deepcopy(self.bundle if name == 'draft' else self.review)
            Path(outdir).mkdir(parents=True, exist_ok=True)
            (Path(outdir) / (name + '.json')).write_text(json.dumps(value))
            return value
        with patch('hkipo.writer.load_skill', return_value='fixed specification'), \
             patch('hkipo.writer.run_codex', side_effect=fake), \
             patch('hkipo.visual_worker.run_visual_worker', side_effect=AssertionError('whole book visual review forbidden')):
            status = write_article({'kind': 'offer', 'event_id': 'offline-test'}, [self.source],
                                   self.root / 'job', self.root, include_docx=False,
                                   max_revisions=0, **kwargs)
        return status, calls

    def test_default_reaches_draft_and_separate_independent_review(self):
        status, calls = self.run_writer(auto_visual_review=True,
                                       scoped_visual_registry='/irrelevant/missing/registry.json')
        self.assertEqual(status['status'], 'ready_for_delivery', status)
        self.assertEqual(status['source_mode'], 'text_first')
        self.assertEqual([c[0] for c in calls], ['draft', 'review'])
        self.assertIn('先核原文', calls[1][1])
        self.assertIn('Page two allocation data 200', calls[1][1])
        self.assertNotIn('上一轮未通过', calls[1][1])
        manifest = json.loads((self.root / 'job/sources/manifest.json').read_text())
        self.assertEqual(manifest['documents'][0]['pages'][0]['status'], 'visual_required')
        self.assertNotIn('image_path', manifest['documents'][0]['pages'][0])
        release = freeze_release(self.root / 'job')
        self.assertEqual(release['source_mode'], 'text_first')

    def test_disabling_auto_in_legacy_mode_still_stops_before_draft(self):
        status, calls = self.run_writer(source_mode='legacy_strict', auto_visual_review=False)
        self.assertEqual(status['status'], 'needs_visual_review')
        self.assertEqual(calls, [])

    def test_independent_reviewer_can_request_bounded_specific_page(self):
        did = self.docs['documents'][0]['document_id']
        self.review['decision'] = 'needs_source'
        self.review['issues'] = [{'severity': 'blocking', 'fact_id': 'offer_price',
            'block_index': 0, 'problem': f'[visual_page:{did}:1] core price table columns ambiguous',
            'source_refs': []}]
        status, calls = self.run_writer()
        self.assertEqual(status['status'], 'needs_source')
        self.assertEqual(status['pages'][0]['pdf_page'], 1)
        self.assertEqual(status['pages'][0]['document_id'], did)
        self.assertEqual([c[0] for c in calls], ['draft', 'review'])


if __name__ == '__main__':
    unittest.main()
