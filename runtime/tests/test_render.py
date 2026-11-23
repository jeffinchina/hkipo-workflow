import re
import os
import zipfile
from xml.etree import ElementTree as ET
import tempfile
import unittest
from pathlib import Path
from html.parser import HTMLParser
from hkipo.render import article_text, article_html, render_bundle, write_docx, word_render_environment, CTA, RISK, QR_SHA256, qr_bytes, footer_blocks, legacy_article_text, legacy_article_html


class RenderTests(unittest.TestCase):
    def test_escaped_html_preserves_body_and_excludes_audit(self):
        article = {"title": "发行例子", "blocks": [{"role": "field", "text": "基石：A&B <例>", "fact_ids": ["f1"]}]}
        body = article_html(article)
        self.assertIn("A&amp;B &lt;例&gt;", body)
        self.assertNotIn("fact_ids", body)
        self.assertNotIn("fact_ids=", body)
        self.assertEqual(legacy_article_text(article), "发行例子\n\n基石：A&B <例>")
        self.assertTrue(article_text(article).endswith(CTA + "\n\n" + RISK))

    def test_footer_is_single_ordered_and_preserves_source(self):
        import copy
        import hashlib
        article = {"title": "例", "blocks": [{"role": "paragraph", "text": "正文"},
            {"role": "cta", "text": CTA}, {"role": "disclaimer", "text": RISK}]}
        before = copy.deepcopy(article)
        body = article_html(article)
        self.assertEqual(article, before)
        self.assertEqual(re.sub(r'<[^>]*>', '', body).count(CTA), 1)
        self.assertEqual(body.count(RISK), 1)
        self.assertLess(re.sub(r'<[^>]*>', '', body).index(CTA), re.sub(r'<[^>]*>', '', body).index(RISK))
        self.assertLess(body.index(RISK), body.index('data-branding="personal-wechat-qr"'))
        self.assertEqual(body.count('data:image/jpeg;base64,'), 1)
        self.assertEqual(hashlib.sha256(qr_bytes()).hexdigest(), QR_SHA256)
        self.assertNotIn('data:image', legacy_article_html(article))
        self.assertEqual(footer_blocks({**article, "blocks": footer_blocks(article)}), footer_blocks(article))
        with self.assertRaises(ValueError):
            article_html(article, qr_ratio=1.1)

    def test_footer_preserves_prose_and_research_limitations(self):
        blocks = [{"role": "paragraph", "text": "看到这里，点个赞吧，这句话是历史文章引用。"},
                  {"role": "disclaimer", "text": "尚未取得完整招股书，以下信息存在缺口。"},
                  {"role": "paragraph", "text": "风险提示"},
                  {"role": "paragraph", "text": "正文继续。"}]
        article = {"title": "例", "blocks": blocks}
        self.assertEqual(footer_blocks(article)[:4], blocks)
        self.assertIn("225px", article_html(article))

    def test_docx_embeds_original_qr_centered_with_aspect_ratio(self):
        article = {"title": "例", "blocks": [{"role": "paragraph", "text": "正文"}]}
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp, "article.docx")
            write_docx(article, destination)
            with zipfile.ZipFile(destination) as z:
                media = [n for n in z.namelist() if n.startswith("word/media/")]
                self.assertEqual(len(media), 1)
                self.assertEqual(z.read(media[0]), qr_bytes())
                document = ET.fromstring(z.read("word/document.xml"))
            ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
                  "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"}
            picture_p = document.find('.//w:drawing/../..', ns)
            self.assertEqual(picture_p.find('w:pPr/w:jc', ns).get('{'+ns['w']+'}val'), 'center')
            size = document.find('.//wp:extent', ns)
            self.assertAlmostEqual(int(size.get('cx')) / int(size.get('cy')), 888 / 1134, places=5)

    def test_docx_cjk_faces_and_title_border_are_explicit(self):
        article = {"title": "中文标题", "blocks": [{"role": "paragraph", "text": "退款安排与123。", "fact_ids": []}]}
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp, "article.docx")
            write_docx(article, destination)
            with zipfile.ZipFile(destination) as z:
                styles = ET.fromstring(z.read("word/styles.xml"))
                document = ET.fromstring(z.read("word/document.xml"))
            for name in ("Normal", "Title"):
                style = styles.find("w:style[@w:styleId='" + name + "']", ns)
                fonts = style.find("w:rPr/w:rFonts", ns)
                self.assertEqual(fonts.get("{" + ns["w"] + "}eastAsia"), "Hiragino Sans GB")
                self.assertFalse(any("Theme" in key for key in fonts.attrib))
                self.assertIsNone(style.find("w:pPr/w:pBdr", ns))
            self.assertIn("中文标题", "".join(document.itertext()))
            self.assertIsNone(document.find(".//w:pBdr", ns))

    def test_render_font_environment_is_child_local(self):
        before = os.environ.get("FONTCONFIG_FILE")
        with tempfile.TemporaryDirectory() as tmp:
            env = word_render_environment(tmp)
            self.assertEqual(os.environ.get("FONTCONFIG_FILE"), before)
            config = ET.parse(env["FONTCONFIG_FILE"]).getroot()
            self.assertTrue(config.findall("dir"))
            font_dirs = {node.text for node in config.findall("dir")}
            for directory in (Path("/System/Library/Fonts/Supplemental"),
                              Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/native/libreoffice-headless/libreoffice/LibreOfficeDev.app/Contents/Resources/fonts/truetype"):
                if directory.is_dir():
                    self.assertIn(str(directory), font_dirs)
            self.assertTrue(Path(config.find("cachedir").text).is_dir())

    def test_shared_frozen_content_not_internal_appendix(self):
        bundle = {"event_id": "e1", "kind": "exception", "article": {"title": "延期", "blocks": [{"role": "paragraph", "text": "新时间尚未公布。", "fact_ids": ["f1"]}]},
                  "facts": [{"id": "f1", "private_audit": "source-page"}], "calculations": [], "sources_seen": [], "gaps": []}
        with tempfile.TemporaryDirectory() as tmp:
            manifest = render_bundle(bundle, tmp, include_docx=False)
            self.assertEqual(Path(tmp, "article.txt").read_text(), article_text(bundle["article"]))
            self.assertNotIn("source-page", Path(tmp, "article.html").read_text())
            self.assertIn("article.html", manifest["files"])


if __name__ == "__main__":
    unittest.main()
