"""Offline complete-PDF extraction. No network or model calls."""
from pathlib import Path
import hashlib
import json
import os
import unicodedata


def normalize_quote(text):
    """NFKC and whitespace only: no punctuation deletion or ellipsis expansion."""
    return ''.join(unicodedata.normalize('NFKC', text).split())


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


EXTRACTION_VERSION = 2
RENDER_SCALE = 2.0  # 144dpi, RGB, no alpha; stable within this PyMuPDF runtime


def render_page_bytes(path, pdf_page):
    import fitz
    with fitz.open(path) as pdf:
        if not 1 <= pdf_page <= len(pdf):
            raise ValueError('invalid render page')
        return pdf[pdf_page - 1].get_pixmap(matrix=fitz.Matrix(RENDER_SCALE, RENDER_SCALE),
                                           colorspace=fitz.csRGB, alpha=False).tobytes('png')


def _only_layout_rules(drawings):
    # Ignore unfilled, thin, axis-aligned rules/borders on otherwise text-rich
    # pages. Filled shapes, curves and diagonals remain visual-review material.
    for drawing in drawings:
        if drawing.get('fill') is not None or drawing.get('width', 1) > 1.5:
            return False
        for item in drawing.get('items', []):
            if item[0] == 'l':
                a, b = item[1:3]
                if abs(a.x - b.x) > 0.01 and abs(a.y - b.y) > 0.01:
                    return False
            elif item[0] != 're':
                return False
    return True


def read_pdf_pages(path):
    import fitz
    with fitz.open(path) as pdf:
        if pdf.needs_pass:
            raise ValueError('encrypted PDF requires password')
        pages = []
        for i, page in enumerate(pdf):
            text = page.get_text('text')
            normalized = normalize_quote(text)
            images = page.get_image_info()
            drawings = page.get_drawings()
            annotations = list(page.annots() or [])
            reasons = []
            if images:
                reasons.append('embedded_image')
            if drawings and not _only_layout_rules(drawings):
                reasons.append('vector_drawing_or_chart')
            if annotations:
                reasons.append('annotation')
            # No threshold ever asserts that visual material was read. Even a
            # normal text page with charts requires explicit visual disposition.
            if normalized and len(normalized) < 24:
                reasons.append('sparse_text')
            blank = not normalized and not images and not drawings and not annotations
            status = 'blank' if blank else ('visual_required' if reasons else 'readable')
            if not normalized and not blank:
                reasons.append('no_extractable_text')
                status = 'visual_required'
            pages.append({'pdf_page': i + 1, 'text': text, 'status': status,
                          'visual_reasons': reasons,
                          'native_blocks': [{'bbox': list(b[:4]), 'text': b[4]}
                                            for b in page.get_text('blocks') if b[6] == 0]})
        return pages


def _page_text(pages):
    return ''.join(f"===== PDF PAGE {p['pdf_page']} (1-based) [{p['status']}] =====\n{p['text']}\n" for p in pages)


def prepare_documents(paths, outdir, *, render_visual=True):
    """Return {'documents': [...]} with complete pages and SHA-addressed UTF-8 text.

    Identical SHA inputs share one document and retain aliases. Cache reuse requires
    unchanged text file SHA and structurally complete pages. True empty pages are blank; image/drawing/sparse pages require visual review.
    OCR is never silently invented.
    """
    out = Path(outdir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    docs = {}
    for given in paths:
        path = Path(given).expanduser().resolve(strict=True)
        digest = _sha(path)
        if digest in docs:
            docs[digest]['aliases'].append(str(path))
            continue
        document_id = digest[:12]
        cache = out / (digest + '.json')
        text_path = out / (digest + '.pages.txt')
        cached = None
        if cache.exists():
            try:
                c = json.loads(cache.read_text(encoding='utf-8'))
                numbers = [p['pdf_page'] for p in c['pages']]
                if (c.get('extraction_version') == EXTRACTION_VERSION and c['sha256'] == digest and c['pages_total'] > 0 and
                    numbers == list(range(1, c['pages_total'] + 1)) and
                    text_path.exists() and c['pages_sha256'] == _sha(text_path) and
                    _page_text(c['pages']) == text_path.read_text(encoding='utf-8') and
                    all(p['status'] in {'readable', 'blank', 'visual_required'} and
                        (not render_visual or p['status'] != 'visual_required' or (Path(p['image_path']).exists() and _sha(p['image_path']) == p['image_sha256'])) for p in c['pages'])):
                    cached = c
            except (ValueError, KeyError, TypeError, OSError):
                pass
        if cached is None:
            pages = read_pdf_pages(path)
            if not pages:
                raise ValueError('PDF has no pages: ' + str(path))
            for page in pages:
                if render_visual and page['status'] == 'visual_required':
                    image_path = out / f"{digest}.page-{page['pdf_page']}.png"
                    image_path.write_bytes(render_page_bytes(path, page['pdf_page']))
                    page['image_path'] = str(image_path)
                    page['image_sha256'] = _sha(image_path)
            text_path.write_text(_page_text(pages), encoding='utf-8')
            cached = {'extraction_version': EXTRACTION_VERSION, 'document_id': document_id, 'path': str(path), 'sha256': digest,
                      'pages_total': len(pages), 'pages_path': str(text_path),
                      'pages_sha256': _sha(text_path), 'pages': pages,
                      'unreadable_pages': [],
                      'visual_required_pages': [p['pdf_page'] for p in pages if p['status'] == 'visual_required'],
                      'blank_pages': [p['pdf_page'] for p in pages if p['status'] == 'blank']}
            temp = cache.with_suffix('.tmp')
            temp.write_text(json.dumps(cached, ensure_ascii=False, indent=2), encoding='utf-8')
            os.replace(temp, cache)
        cached['path'] = str(path)
        cached['aliases'] = [str(path)]
        docs[digest] = cached
    manifest = {'documents': list(docs.values()), 'extraction': 'fitz text and visual inventory v2, all pages, one-based; no OCR'}
    (out / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    return manifest

def load_visual_reviews(paths, trusted_sha256s, documents):
    """Validate trusted sidecar files against actual source and fresh rendering.

    Returns {(document_id, page): sidecar}. Trust anchors must be supplied by a
    caller-controlled independent audit process, never by the writer JSON.
    """
    from datetime import datetime
    indexed = {d['document_id']: d for d in documents}
    trusted = set(trusted_sha256s)
    accepted = {}
    for given in paths:
        path = Path(given).resolve(strict=True)
        digest = _sha(path)
        if digest not in trusted:
            raise ValueError('visual sidecar has no independent trusted hash')
        sidecar = json.loads(path.read_text(encoding='utf-8'))
        required = {'document_id', 'pdf_sha256', 'pdf_page', 'image_sha256',
                    'reviewer', 'reviewed_at', 'decision', 'transcription', 'reason'}
        if not isinstance(sidecar, dict) or set(sidecar) != required:
            raise ValueError('visual sidecar fields invalid')
        did, page = sidecar['document_id'], sidecar['pdf_page']
        if did not in indexed or not isinstance(page, int) or isinstance(page, bool):
            raise ValueError('visual sidecar document/page invalid')
        d = indexed[did]
        if sidecar['pdf_sha256'] != _sha(d['path']) or sidecar['pdf_sha256'] != d['sha256']:
            raise ValueError('visual sidecar PDF SHA mismatch')
        image = render_page_bytes(d['path'], page)
        if hashlib.sha256(image).hexdigest() != sidecar['image_sha256']:
            raise ValueError('visual sidecar rendered image SHA mismatch')
        if not isinstance(sidecar['reviewer'], str) or not sidecar['reviewer'].strip():
            raise ValueError('visual reviewer required')
        stamp = datetime.fromisoformat(sidecar['reviewed_at'].replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            raise ValueError('visual reviewed_at requires timezone')
        if not all(isinstance(sidecar[k], str) for k in ['decision', 'transcription', 'reason']):
            raise ValueError('visual decision/transcription/reason must be strings')
        if sidecar['decision'] == 'transcribed':
            if not sidecar['transcription'].strip() or not sidecar['reason'].strip():
                raise ValueError('visual transcription and scope/reason required')
        elif sidecar['decision'] == 'no_substantive_content':
            if not sidecar['reason'].strip() or sidecar['transcription'].strip():
                raise ValueError('non-substantive disposition requires reason and empty transcription')
        else:
            raise ValueError('visual decision not approved')
        key = (did, page)
        if key in accepted:
            raise ValueError('duplicate visual sidecar for page')
        sidecar['sidecar_path'] = str(path)
        sidecar['sidecar_sha256'] = digest
        accepted[key] = sidecar
    return accepted
