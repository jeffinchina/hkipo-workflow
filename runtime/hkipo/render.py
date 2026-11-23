"""One structured article, one visible body, HTML and Word presentations."""
import base64
import hashlib
import html
import json
import os
import re
from pathlib import Path
from .render_profile import LEGACY, defaults, validate_style,css_color,word_color

BLUE = "2B77BF"
CTA = "看到这里，点个赞吧，新朋友点个关注，第一时间推送打新笔记。"
RISK = "风险提示：本文内容仅为个人分析记录，不构成任何投资建议。打新有风险，参与需谨慎。"
QR_PATH = Path(os.environ.get("HKIPO_QR_PATH", str(Path(__file__).resolve().parents[2] / "private/qr.jpg"))).expanduser()
QR_SHA256 = os.environ.get("HKIPO_QR_SHA256", "")
# Default display size; local original image remains private.
QR_WIDTH_PX = 225


def footer_blocks(article):
    """Return a copy with one deterministic footer; never mutate frozen input."""
    blocks = [dict(block) for block in article["blocks"]
              if block["role"] not in {"cta", "personal_qr"}]
    known_footer = {CTA, RISK, RISK.split("：", 1)[1], "风险提示",
                    "看到这里，点个赞吧，新朋友点个关注"}
    # Only normalize exact footer paragraphs at the end. Research limitations
    # and author prose elsewhere remain visible, even with a disclaimer role.
    while blocks and blocks[-1]["role"] != 'author' and blocks[-1]["text"].strip() in known_footer:
        blocks.pop()
    return blocks + [{"role": "cta", "text": CTA, "fact_ids": []},
                     {"role": "disclaimer", "text": RISK, "fact_ids": []}]


def qr_bytes():
    if not QR_PATH.is_file() or not re.fullmatch(r"[0-9a-f]{64}", QR_SHA256):
        raise ValueError("Configure HKIPO_QR_PATH and HKIPO_QR_SHA256 before rendering")
    data = QR_PATH.read_bytes()
    if hashlib.sha256(data).hexdigest() != QR_SHA256:
        raise ValueError("Personal QR original hash mismatch")
    return data


def qr_width_ratio(value):
    value = float(value)
    if not 0 < value <= 1:
        raise ValueError("QR width ratio must be in (0, 1]")
    return value


def legacy_article_text(article):
    return article["title"] + "\n\n" + "\n\n".join(b["text"] for b in article["blocks"])



def article_text(article, footer=True):
    return legacy_article_text({**article, "blocks": footer_blocks(article)}) if footer else legacy_article_text(article)


def article_html(article, footer=True, qr_ratio=None, profile=None):
    profile = validate_style(defaults()['style'] if profile is None else profile)
    parts = []
    in_list=False
    for block in (footer_blocks(article) if footer else article["blocks"]):
        role = block["role"]
        listed=role=='item' and profile['numbered_items']
        if in_list and not listed:parts.append('</ol>');in_list=False
        if listed and not in_list:
            parts.append('<ol style="margin:0;padding-left:1.5em;">');in_list=True
        text = html.escape(block["text"]).replace("\n", "<br/>")
        if listed:text=re.sub(r'^\s*\d+\s*[、.．)）]\s*','',text)
        spacing=profile['heading_space_px'] if role=='heading' else profile['paragraph_space_px']
        style = f"font-size:{profile['font_size_px']:g}px;line-height:{profile['line_height_px']:g}px;margin:0 0 {spacing:g}px;text-align:{profile['text_align']};white-space:normal;word-break:break-word;"
        if role == "heading":
            style += f"color:{css_color(profile['heading_color'])};font-weight:{profile['heading_font_weight']};"
        elif role == "disclaimer":
            style += f"color:{css_color(profile['risk_color'])};"
            if profile!=LEGACY:style+=f"font-weight:{profile['body_font_weight']};"
        else:
            style += f"color:{css_color(profile['cta_color'] if role=='cta' else profile['body_color'])};font-weight:{profile['body_font_weight']};"
        if role == "field" and "：" in text:
            label, value = text.split("：", 1)
            text = (f"<strong>{label}：</strong>{value}" if profile['field_label_font_weight']==700 else
                    f'<span style="font-weight:{profile["field_label_font_weight"]};">{label}：</span>{value}')
        if role=='cta':
            for phrase in profile['cta_highlight_phrases']:
                text=text.replace(phrase,f'<span style="color:{css_color(profile["cta_highlight_color"])};font-weight:700;">{phrase}</span>')
        branding=' data-branding="article-cta"' if role=='cta' and profile!=LEGACY else ''
        paragraph=f'<p{branding} style="{style}">{text}</p>'
        parts.append('<li>'+paragraph+'</li>' if listed else paragraph)
    if in_list:parts.append('</ol>')
    if footer:
        width_style = (f"width:{qr_width_ratio(qr_ratio) * 100:g}%;" if qr_ratio is not None
                       else f"width:{QR_WIDTH_PX}px;")
        src = "data:image/jpeg;base64," + base64.b64encode(qr_bytes()).decode("ascii")
        margins=f'margin-top:{profile["qr_margin_px"]:g}px;margin-bottom:{profile["qr_margin_px"]:g}px;' if profile['qr_margin_px'] else ''
        parts.append(f'<p data-branding="personal-wechat-qr" style="text-align:center;margin:0 0 {profile["paragraph_space_px"]:g}px;"><img alt="个人微信二维码" src="{src}" style="display:block;margin:0 auto;{width_style}max-width:100%;height:auto;{margins}"/></p>')
    return f'<section style="padding:0 {profile["side_padding_px"]:g}px;font-family:system-ui,-apple-system,sans-serif;font-size:{profile["font_size_px"]:g}px;line-height:{profile["line_height_px"]:g}px;">' + "".join(parts) + "</section>"


def legacy_article_html(article):
    return article_html(article, footer=False, profile=LEGACY)


def word_render_environment(directory):
    """Return child-only fontconfig settings for the bundled macOS renderer.

    This does not mutate os.environ or install fonts. Pass this environment to
    documents/render_docx.py; a DOCX font name alone cannot repair its font scan.
    """
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    cache = directory / "font-cache"
    cache.mkdir(exist_ok=True)
    config = directory / "fonts.conf"
    bundled_fonts = (Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/native/"
                     "libreoffice-headless/libreoffice/LibreOfficeDev.app/Contents/Resources/fonts/truetype")
    dirs = [p for p in (Path("/System/Library/Fonts"),
                       Path("/System/Library/Fonts/Supplemental"),
                       Path("/Library/Fonts"), Path("/usr/share/fonts"), Path.home()/".local/share/fonts", bundled_fonts) if p.is_dir()]
    if not dirs:
        raise RuntimeError("No system font directories available for Word render QA")
    config.write_text('<?xml version="1.0"?><fontconfig>' +
                      ''.join('<dir>' + html.escape(str(p)) + '</dir>' for p in dirs) +
                      '<cachedir>' + html.escape(str(cache)) + '</cachedir></fontconfig>', encoding="utf-8")
    env = os.environ.copy()
    env["FONTCONFIG_FILE"] = str(config)
    return env


def write_docx(article, destination, footer=True, qr_ratio=None, profile=None):
    # Import lazily so monitoring remains standard-library only.
    from docx import Document
    from docx.shared import Pt, Cm, RGBColor
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    profile=validate_style(defaults()['style'] if profile is None else profile)
    doc = Document()
    section = doc.sections[0]
    section.top_margin = section.bottom_margin = Cm(1.9)
    section.left_margin = section.right_margin = Cm(2.3)
    normal = doc.styles["Normal"]
    normal.font.name = "Hiragino Sans GB"
    normal.font.size = Pt(profile['font_size_px']*.75)
    # Explicit CJK faces avoid missing-glyph fallback in headless LibreOffice.
    for style_name in ("Normal", "Title", "Heading 1", "Heading 2"):
        style = doc.styles[style_name]
        style.font.name = "Hiragino Sans GB"
        style.font.color.rgb = RGBColor(0, 0, 0)
        fonts = style.element.get_or_add_rPr().get_or_add_rFonts()
        for attr in ("asciiTheme", "hAnsiTheme", "eastAsiaTheme", "cstheme"):
            fonts.attrib.pop(qn("w:" + attr), None)
        for attr in ("ascii", "hAnsi", "eastAsia", "cs"):
            fonts.set(qn("w:" + attr), "Hiragino Sans GB")
        # python-docx's bundled template includes a blue bottom border on Title.
        for border in list(style.element.xpath(".//w:pBdr")):
            border.getparent().remove(border)
    normal.font.color.rgb=RGBColor.from_string(word_color(profile['body_color']))
    normal.paragraph_format.line_spacing = profile['line_height_px']/profile['font_size_px']
    normal.paragraph_format.space_after = Pt(profile['paragraph_space_px']*.75)
    title = doc.add_paragraph(article["title"], "Title")
    for run in title.runs:
        run.font.color.rgb = RGBColor.from_string(word_color(profile['title_color']))
        run.font.size = Pt(18)
        run.font.bold = True
    for border in list(title._p.xpath(".//w:pBdr")):
        border.getparent().remove(border)
    title.paragraph_format.space_after = Pt(12)
    title.paragraph_format.keep_with_next = True
    for block in (footer_blocks(article) if footer else article["blocks"]):
        role, text = block["role"], block["text"]
        listed=role=='item' and profile['numbered_items']
        p=doc.add_paragraph(style='List Number' if listed else None)
        if listed:text=re.sub(r'^\s*\d+\s*[、.．)）]\s*','',text)
        if role == "field" and "：" in text:
            label, value = text.split("：", 1)
            p.add_run(label + "：").bold = profile['field_label_font_weight']>=600
            p.add_run(value)
        elif role=='cta' and profile['cta_highlight_phrases']:
            pieces=re.split('('+'|'.join(map(re.escape,profile['cta_highlight_phrases']))+')',text)
            for piece in pieces:
                run=p.add_run(piece);highlight=piece in profile['cta_highlight_phrases']
                run.font.color.rgb=RGBColor.from_string(word_color(profile['cta_highlight_color'] if highlight else profile['cta_color']))
                run.bold=highlight
        else:
            run = p.add_run(text)
            if role == "heading":
                run.font.bold = profile['heading_font_weight']>=600
                run.font.color.rgb = RGBColor.from_string(word_color(profile['heading_color']))
                p.paragraph_format.keep_with_next = True
            elif role == "disclaimer":
                run.font.color.rgb = RGBColor.from_string(word_color(profile['risk_color']))
            elif role == 'cta':
                run.font.color.rgb = RGBColor.from_string(word_color(profile['cta_color']))
        if role=='heading':
            p.paragraph_format.space_after=Pt(profile['heading_space_px']*.75)
        if footer and (role == "cta" or text == RISK):
            p.paragraph_format.keep_with_next = True
        p.paragraph_format.widow_control = True
    if footer:
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        width = (int((section.page_width - section.left_margin - section.right_margin) * qr_width_ratio(qr_ratio))
                 if qr_ratio is not None else int(QR_WIDTH_PX / 96 * 914400))
        # This original JPEG uses markers python-docx cannot parse. Supply the
        # verified image dimensions to its package API without rewriting bytes.
        from docx.image.image import Image
        from docx.image.jpeg import Jfif
        from docx.oxml.shape import CT_Inline
        from docx.opc.constants import RELATIONSHIP_TYPE as RT
        from io import BytesIO
        from PIL import Image as PillowImage
        data = qr_bytes()
        with PillowImage.open(BytesIO(data)) as original:
            if original.format != "JPEG":
                raise ValueError("Configure an original JPEG for the footer image")
            width_px, height_px = original.size
        image = Image(data, "footer-qr.jpg", Jfif(width_px, height_px, 72, 72))
        part = doc.part.package.image_parts._add_image_part(image)
        rid = doc.part.relate_to(part, RT.IMAGE)
        cx, cy = image.scaled_dimensions(width, None)
        inline = CT_Inline.new_pic_inline(doc.part.next_id, rid, image.filename, cx, cy)
        p.add_run()._r.add_drawing(inline)
    doc.core_properties.title = article["title"]
    doc.core_properties.author = defaults().get('author') or "Author"
    doc.save(str(destination))


def render_bundle(bundle, directory, include_docx=True, profile=None):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    article = bundle["article"]
    profile=validate_style(defaults()['style'] if profile is None else profile)
    text = article_text(article)
    body = article_html(article, profile=profile)
    (directory / "article.txt").write_text(text, encoding="utf-8")
    (directory / "article.html").write_text(body, encoding="utf-8")
    (directory / "preview.html").write_text('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>' + html.escape(article["title"]) + '</title><body style="max-width:660px;margin:32px auto;padding:0 12px;"><h1 style="font-size:22px;">' + html.escape(article["title"]) + '</h1>' + body + '</body></html>', encoding="utf-8")
    if include_docx:
        write_docx(article, directory / "article.docx", profile=profile)
    audit = {"event_id": bundle["event_id"], "kind": bundle["kind"],
             "body_sha256": hashlib.sha256(text.encode()).hexdigest(),
             "facts": bundle["facts"], "calculations": bundle["calculations"],
             "sources_seen": bundle["sources_seen"], "gaps": bundle["gaps"]}
    (directory / "audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
             for p in directory.iterdir() if p.name.startswith("article.") and p.is_file()}
    manifest = {"event_id": bundle["event_id"], "title": article["title"],
                "body_sha256": audit["body_sha256"], "files": files,
                "render_version": 3, "render_profile": profile, "branding": {"sha256": QR_SHA256, "width_px": QR_WIDTH_PX, "centered": True,
                "needs_layout_acceptance": True, "measurement_status": "published_footer_display_evidence",
                "evidence": {"source": "local private branding configuration"}}}
    (directory / "render-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest
