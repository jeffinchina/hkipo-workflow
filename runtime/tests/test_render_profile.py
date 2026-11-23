import hashlib
from pathlib import Path
import unittest
from unittest.mock import patch
from hkipo.render_profile import LEGACY,validate_style,draft_metadata,word_color
from hkipo.render import article_html,CTA
import test_delivery


class ProfileTests(unittest.TestCase):
    def test_body_heading_and_cta_use_independent_verified_colors(self):
        profile=dict(LEGACY,body_color='222222',heading_color='334455',cta_color='AABBCC',
                     font_size_px=17,line_height_px=34,paragraph_space_px=24)
        body=article_html({'title':'例','blocks':[{'role':'heading','text':'一、基本情况'},
            {'role':'paragraph','text':'正文'}]},profile=profile)
        self.assertIn('color:#222222',body);self.assertIn('color:#334455',body);self.assertIn('color:#AABBCC',body)
        self.assertIn('font-size:17px;line-height:34px;margin:0 0 24px',body)
        self.assertEqual(body.count(CTA),1)
        with self.assertRaises(ValueError):validate_style(dict(profile,cta_color='red;display:none'))

    def test_comments_on_by_default_and_author_is_not_guessed(self):
        with patch('hkipo.render_profile.PROFILE_PATH',Path('/nonexistent-test-profile')):
            self.assertEqual(draft_metadata(),{'author':'','need_open_comment':1,'only_fans_can_comment':0})
        self.assertEqual(draft_metadata(author='示例作者')['author'],'示例作者')
        with self.assertRaises(ValueError):draft_metadata(author='bad\nname')
    def test_verified_reference_highlights_only_cta_keywords(self):
        from hkipo.render_profile import defaults
        body=article_html({'title':'例','blocks':[]},profile=defaults()['style'])
        self.assertIn('color:rgba(0,0,0,0.9)',body)
        self.assertIn('color:#FFA900;font-weight:700;">点个赞</span>',body)
        self.assertIn('color:#FFA900;font-weight:700;">点个关注</span>',body)
        self.assertNotIn('color:#FFA900;font-weight:700;">看到这里',body)
        self.assertEqual(word_color('rgba(0,0,0,0.9)'),'1A1A1A')


class DraftMetadataTests(unittest.TestCase):
    setUp=test_delivery.DeliveryTests.setUp
    tearDown=test_delivery.DeliveryTests.tearDown
    make_release=test_delivery.DeliveryTests.make_release
    # Reuse the frozen-release seam; all transports remain mocks.
    def test_author_and_comments_reach_api_and_change_idempotency(self):
        result=self.d.draft(self.release,thumb_media_id='approved-cover',author='示例作者')
        self.assertEqual(result['status'],'confirmed')
        article=self.wx.call.call_args.args[2]['articles'][0]
        self.assertEqual(article['author'],'示例作者');self.assertEqual(article['need_open_comment'],1)
        self.assertEqual(article['only_fans_can_comment'],0)
        self.wx.call.return_value={'errcode':0}
        changed=self.d.draft(self.release,thumb_media_id='approved-cover',author='另一署名',update_existing=True)
        self.assertEqual(changed['status'],'confirmed');self.assertEqual(self.wx.call.call_count,2)

    def test_frozen_new_profile_does_not_follow_later_default_changes(self):
        profile=dict(LEGACY,body_color='123456',cta_color='654321')
        path=Path(self.release['output_dir'])/'article.html'
        path.write_text(article_html(self.article,profile=profile))
        self.release['render_version']=3;self.release['render_profile']=profile
        self.release['files_sha256']['article.html']=hashlib.sha256(path.read_bytes()).hexdigest()
        from hkipo.delivery import verify_release
        with patch('hkipo.render.defaults',return_value={'style':dict(LEGACY,body_color='ABCDEF')}):
            frozen=verify_release(self.release)
        self.assertIn('color:#123456',frozen['html'])
    def test_existing_legacy_release_remains_valid_after_new_defaults(self):
        from hkipo.delivery import verify_release
        path=Path(self.release['output_dir'])/'article.html'
        path.write_text(article_html(self.article,profile=LEGACY))
        self.release['render_version']=2;self.release.pop('render_profile',None)
        self.release['files_sha256']['article.html']=hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertIn('color:#191919',verify_release(self.release)['html'])


if __name__=='__main__':unittest.main()
