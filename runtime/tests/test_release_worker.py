import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
from PIL import Image, ImageDraw
from hkipo.release_worker import prepare_release, render_word, RENDER_SCRIPT
from hkipo.quota import QuotaDeferred


class ReleaseWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.job = Path(self.tmp.name)
        self.out = self.job / 'output'; self.out.mkdir()
        (self.job / 'sources').mkdir()
        (self.job / 'sources/manifest.json').write_text('{"documents":[]}')
        self.status = {'status': 'ready_for_delivery', 'request_id': 'r1',
                       'output_dir': str(self.out)}
        for key in ('bundle_path', 'review_path', 'quality_path'):
            path = self.job / (key + '.json'); path.write_text('{}')
            self.status[key] = str(path)
        (self.job / 'status.json').write_text(json.dumps(self.status))
        (self.out / 'article.docx').write_bytes(b'word')
        self.calls = []

    def renderer(self, docx, directory):
        directory.mkdir(parents=True)
        for number in range(1, 8):
            im = Image.new('RGB', (200, 200), 'white')
            ImageDraw.Draw(im).text((20, 20), str(number), fill='black')
            im.save(directory / f'page-{number}.png')

    def review(self, **kwargs):
        self.calls.append(kwargs)
        return {'pages': [{'page': int(p.stem.split('-')[1]), 'pass': True, 'issues': []}
                          for p in kwargs['images']]}

    def prepare(self, **kwargs):
        return prepare_release(self.job, renderer=self.renderer, reviewer=self.review,
                               quota=lambda **kw: {'allowed': True}, **kwargs)

    def test_all_images_batched_and_confirmed_only_after_review(self):
        with patch('hkipo.release_worker.freeze_release') as freeze, patch('hkipo.release_worker.verify_release') as verify:
            freeze.__wrapped__ = Mock(return_value={'release': True})
            self.assertEqual(self.prepare(), {'release': True})
            self.assertEqual([len(c['images']) for c in self.calls], [6, 1])
            qa = json.loads((self.out / 'visual-qa.json').read_text())
            self.assertTrue(qa['confirmed']); self.assertEqual(len(qa['pages_sha256']), 7)
            verify.assert_called_once()

    def test_reject_incomplete_page_coverage(self):
        self.review = lambda **kwargs: {'pages': []}
        with self.assertRaisesRegex(ValueError, 'coverage'):
            self.prepare()
        self.assertFalse((self.out / 'visual-qa.json').exists())
        self.assertFalse((self.job / 'release.json').exists())

    def test_mutation_during_review_rejected(self):
        original = self.review
        def mutate(**kwargs):
            (self.out / 'article.docx').write_bytes(b'changed')
            return original(**kwargs)
        self.review = mutate
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.prepare()
        self.assertFalse((self.out / 'visual-qa.json').exists())

    def test_blank_page_rejected_before_model(self):
        def blank(docx, directory):
            directory.mkdir(parents=True)
            Image.new('RGB', (200, 200), 'white').save(directory / 'page-1.png')
        self.renderer = blank
        with self.assertRaisesRegex(ValueError, 'Blank'):
            self.prepare()
        self.assertEqual(self.calls, [])

    def test_quota_deferred_propagates_without_confirmation(self):
        def quota(**kwargs):
            raise QuotaDeferred({'allowed': False})
        with self.assertRaises(QuotaDeferred):
            prepare_release(self.job, renderer=self.renderer, reviewer=self.review, quota=quota)
        self.assertFalse((self.out / 'visual-qa.json').exists())

    def test_existing_release_never_overwritten(self):
        path = self.job / 'release.json'; path.write_text('{"old":true}')
        with patch('hkipo.release_worker.verify_release'):
            self.assertEqual(self.prepare(), {'old': True})
        self.assertEqual(path.read_text(), '{"old":true}')
        self.assertEqual(self.calls, [])

    def test_failed_attempts_are_bounded(self):
        self.review = lambda **kwargs: {'pages': []}
        for _ in range(2):
            with self.assertRaises(ValueError): self.prepare()
        with self.assertRaisesRegex(ValueError, 'exhausted'): self.prepare()

    def test_renderer_selects_standalone_tool_in_child_only(self):
        before = os.environ.get('PATH')
        with patch('hkipo.release_worker.subprocess.Popen') as popen:
            popen.return_value.returncode = 0
            render_word(self.out / 'article.docx', self.job / 'render')
        env = popen.call_args.kwargs['env']
        self.assertEqual(Path(popen.call_args.args[0][1]), RENDER_SCRIPT)
        self.assertEqual(env.get('PATH'), before)
        self.assertIn('FONTCONFIG_FILE', env)
        self.assertEqual(os.environ.get('PATH'), before)
