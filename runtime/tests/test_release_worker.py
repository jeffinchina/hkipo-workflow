import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
from PIL import Image, ImageDraw
import fitz
from hkipo.release_worker import (
    RUNTIME, RenderUnavailable, WordLayoutRejected, prepare_release, render_word,
    _documents_renderer,
)
from hkipo.word_render import render_word_backends
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

    def test_renderer_resolves_installed_version_after_plugin_upgrade(self):
        cache = self.job / 'documents-cache'
        for version in ('26.9.4', '26.11.2', 'nonversion'):
            script = cache / version / 'skills/documents/render_docx.py'
            script.parent.mkdir(parents=True)
            script.touch()
        (cache / '26.12.0').mkdir()
        self.assertEqual(_documents_renderer(cache),
                         cache / '26.11.2/skills/documents/render_docx.py')

    def test_renderer_selects_bundled_tools_in_child_only(self):
        before = os.environ.get('PATH')
        def backend(docx, destination, *, environments, **kwargs):
            return environments(destination / 'environment')
        child = dict(os.environ, FONTCONFIG_FILE='test-fonts.conf')
        with patch('hkipo.release_worker.render_word_backends', side_effect=backend), \
             patch('hkipo.release_worker.word_render_environment', return_value=child):
            env = render_word(self.out / 'article.docx', self.job / 'render')
        self.assertEqual(env['FONTCONFIG_FILE'], 'test-fonts.conf')
        self.assertEqual(env['PATH'].split(os.pathsep)[:2],
                         [str(RUNTIME / 'bin/override'), str(RUNTIME / 'bin/fallback')])
        self.assertEqual(os.environ.get('PATH'), before)

    def test_partial_managed_render_falls_back_to_complete_direct_pages(self):
        office = self.job / 'office'
        office.touch(mode=0o700)
        calls = []

        def convert(command, stage, env, timeout):
            calls.append(command[0])
            with fitz.open() as document:
                for number in (1, 2):
                    page = document.new_page(width=120, height=120)
                    page.insert_text((20, 20), str(number))
                    if command[0] == 'managed' and number == 1:
                        page.get_pixmap().save(stage / 'page-1.png')
                document.save(stage / 'article.pdf')

        destination = self.job / 'render'
        with patch.dict(os.environ, {'HKIPO_SOFFICE': ''}), \
             patch('hkipo.word_render.office_executables', return_value=iter([office])), \
             patch('hkipo.word_render.run_renderer', side_effect=convert):
            chosen = render_word_backends(self.out / 'article.docx', destination,
                environments=lambda _: {}, managed=('managed-engine', ['managed']))
        self.assertEqual(calls, ['managed', str(office)])
        self.assertEqual(chosen, {'backend': 'office', 'engine': str(office)})
        self.assertEqual(sorted(p.name for p in destination.glob('page-*.png')),
                         ['page-1.png', 'page-2.png'])
        with Image.open(destination / 'page-1.png') as image:
            self.assertEqual(image.size, (240, 240))

    def test_render_outages_leave_visual_attempts_available(self):
        def render(docx, pages, **kwargs):
            if backend.call_count <= 3:
                raise RenderUnavailable([('managed', 'RuntimeError'), ('office', 'RuntimeError')])
            self.renderer(docx, pages)
            return {'backend': 'office', 'engine': 'backup'}

        def finish():
            return prepare_release(self.job, reviewer=self.review,
                                   quota=lambda **kw: {'allowed': True}, max_attempts=1)

        with patch('hkipo.release_worker.render_word', side_effect=render) as backend, \
             patch('hkipo.release_worker.freeze_release') as freeze, \
             patch('hkipo.release_worker.verify_release'):
            freeze.__wrapped__ = Mock(return_value={'release': True})
            for _ in range(3):
                with self.assertRaises(RenderUnavailable):
                    finish()
            self.assertEqual(self.calls, [])
            self.assertEqual(finish(), {'release': True})
        self.assertEqual(len(list(self.job.glob('release-preparation/*/render-unavailable-*'))), 3)

    def test_layout_rejection_retries_distinct_pages_only(self):
        def render(docx, pages, *, exclude_engines, **kwargs):
            pages.mkdir(parents=True)
            self.assertEqual(exclude_engines, () if backend.call_count == 1 else ('primary',))
            image = Image.new('RGB', (200, 200), 'white')
            ImageDraw.Draw(image).text((20, 20), 'Beginning', fill='black')
            if backend.call_count == 2:
                ImageDraw.Draw(image).text((20, 60), 'End', fill='black')
            image.save(pages / 'page-1.png')
            return {'backend': 'office', 'engine': 'primary' if backend.call_count == 1 else 'backup'}

        def review(**kwargs):
            result = self.review(**kwargs)
            if len(self.calls) == 1:
                result['pages'][0].update({'pass': False, 'issues': ['End clipped']})
            return result

        with patch('hkipo.release_worker.render_word', side_effect=render) as backend, \
             patch('hkipo.release_worker.freeze_release') as freeze, \
             patch('hkipo.release_worker.verify_release'):
            freeze.__wrapped__ = Mock(return_value={'release': True})
            release = prepare_release(self.job, reviewer=review, quota=lambda **kw: {'allowed': True})
        self.assertEqual(release, {'release': True})
        self.assertEqual(len(self.calls), 2)
        qa = json.loads((self.out / 'visual-qa.json').read_text())
        self.assertEqual(qa['renderer']['engine'], 'backup')

    def test_identical_alternate_layout_does_not_seek_another_verdict(self):
        def render(docx, pages, **kwargs):
            self.renderer(docx, pages)
            return {'backend': 'office', 'engine': str(backend.call_count)}

        def review(**kwargs):
            result = self.review(**kwargs)
            result['pages'][0].update({'pass': False, 'issues': ['Clipping']})
            return result

        with patch('hkipo.release_worker.render_word', side_effect=render) as backend:
            with self.assertRaisesRegex(WordLayoutRejected, 'reproduced'):
                prepare_release(self.job, reviewer=review, quota=lambda **kw: {'allowed': True})
        self.assertEqual(backend.call_count, 2)
        self.assertEqual(len(self.calls), 1)
        self.assertFalse((self.out / 'visual-qa.json').exists())
