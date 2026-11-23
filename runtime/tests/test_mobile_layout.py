import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from hkipo.mobile_layout import approve_layout, template_fingerprint


class MobileLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        (self.root/'article.html').write_text('<p>作者正文</p>')
        (self.root/'preview.html').write_text('<p>local preview</p>')
        self.manifest={'raw_hash':'raw','version':1,'html_sha256':hashlib.sha256((self.root/'article.html').read_bytes()).hexdigest(),'preview_sha256':hashlib.sha256((self.root/'preview.html').read_bytes()).hexdigest(),'author_sha256':'author','source_event_sha256':'event','source_images':['image']}
        (self.root/'manifest.json').write_text(json.dumps(self.manifest))
        self.profile=self.root/'profile.json';self.profile.write_text(json.dumps({'template_sha256':template_fingerprint(),'local_visual_confirmed':True}))
        self.geometry={'viewport_width':390,'scroll_width':390,'overflow_count':0,'broken_images':0,'image_count':2,'qr_width':225,'height':1500}
    def tearDown(self):self.temp.cleanup()
    def test_green_geometry_produces_exact_bound_approval(self):
        result=approve_layout(self.root,self.profile,renderer=lambda _:self.geometry)
        self.assertEqual(result['status'],'approved_local_layout')
        for key in ('raw_hash','version','html_sha256','author_sha256','source_event_sha256'):self.assertEqual(result['approval'][key],self.manifest[key])
        self.assertFalse(result['approval']['wechat_ui_verified'])
        self.assertTrue((self.root/'layout-approval.json').is_file())
    def test_template_changed_does_not_render_or_approve(self):
        self.profile.write_text(json.dumps({'template_sha256':'obsolete','local_visual_confirmed':True}))
        def forbidden(_):raise AssertionError('renderer must not run')
        self.assertEqual(approve_layout(self.root,self.profile,renderer=forbidden)['status'],'needs_template_visual_review')
        self.assertFalse((self.root/'layout-approval.json').exists())
    def test_geometry_faults_never_approve(self):
        for key,bad in [('overflow_count',1),('broken_images',1),('scroll_width',400),('image_count',1),('qr_width',224),('height',40001)]:
            with self.subTest(key=key):
                result=approve_layout(self.root,self.profile,renderer=lambda _,key=key,bad=bad:self.geometry|{key:bad})
                self.assertEqual(result['status'],'needs_layout_attention')
        self.assertFalse((self.root/'layout-approval.json').exists())
    def test_render_tamper_before_or_during_renderer_rejected(self):
        def mutate(_):
            (self.root/'preview.html').write_text('changed')
            return self.geometry
        self.assertEqual(approve_layout(self.root,self.profile,renderer=mutate)['status'],'render_changed')
        (self.root/'article.html').write_text('changed')
        self.assertEqual(approve_layout(self.root,self.profile,renderer=lambda _:self.geometry)['status'],'render_changed')
        self.assertFalse((self.root/'layout-approval.json').exists())

    def test_preview_tampered_before_renderer_does_not_receive_approval(self):
        (self.root/'preview.html').write_text('<p>unrelated document</p>')
        def forbidden(_):raise AssertionError('must reject before renderer')
        self.assertEqual(approve_layout(self.root,self.profile,renderer=forbidden)['status'],'render_changed')
        self.assertFalse((self.root/'layout-approval.json').exists())
