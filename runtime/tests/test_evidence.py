import hashlib
import tempfile
import unittest
from pathlib import Path
import fitz
from hkipo.evidence import prepare_documents


class EvidenceTests(unittest.TestCase):
    def test_full_extraction_duplicate_reuse_and_blank_page(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); source = root / 'source.pdf'
            pdf = fitz.open()
            pdf.new_page().insert_text((72, 72), 'first 123')
            pdf.new_page()
            pdf.new_page().insert_text((72, 72), 'last 456')
            pdf.save(source); pdf.close()
            manifest = prepare_documents([source, source], root / 'extracted')
            d = manifest['documents'][0]
            self.assertEqual(len(manifest['documents']), 1)
            self.assertEqual(d['sha256'], hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertEqual(d['pages_total'], 3)
            self.assertEqual(d['unreadable_pages'], [])
            self.assertEqual(d['blank_pages'], [2])
            self.assertIn('last 456', d['pages'][2]['text'])
            self.assertEqual(Path(d['pages_path']).read_text().count('===== PDF PAGE'), 3)
            cached = Path(d['pages_path']); mtime = cached.stat().st_mtime_ns
            again = prepare_documents([source], root / 'extracted')
            self.assertEqual(cached.stat().st_mtime_ns, mtime)
            self.assertEqual(again['documents'][0]['pages_total'], 3)
            cached.write_text('tampered')
            repaired = prepare_documents([source], root / 'extracted')
            self.assertIn('last 456', Path(repaired['documents'][0]['pages_path']).read_text())

    def test_visual_inventory_scan_chart_blank_and_layout_rule(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); source = root / 'visual.pdf'
            image_doc = fitz.open(); p = image_doc.new_page()
            p.insert_text((40, 60), 'Scanned body amount 9876')
            image = p.get_pixmap().tobytes('png'); image_doc.close()
            pdf = fitz.open()
            pdf.new_page()  # physically blank
            p = pdf.new_page(); p.insert_text((40, 750), '2'); p.insert_image(fitz.Rect(40, 40, 400, 650), stream=image)
            p = pdf.new_page(); p.insert_text((40, 60), 'Normal text content with a meaningful filled chart below.')
            p.draw_rect(fitz.Rect(40, 100, 120, 230), color=(0, 0, 1), fill=(0, 0, 1))
            p = pdf.new_page(); p.insert_text((40, 60), 'Normal complete text page with just a simple separator rule.')
            p.draw_line((40, 80), (500, 80), width=0.5)
            pdf.save(source); pdf.close()
            d = prepare_documents([source], root / 'texts')['documents'][0]
            self.assertEqual([p['status'] for p in d['pages']], ['blank', 'visual_required', 'visual_required', 'readable'])
            self.assertEqual(d['blank_pages'], [1])
            self.assertEqual(d['visual_required_pages'], [2, 3])
            self.assertEqual(d['unreadable_pages'], [])
            for p in d['pages'][1:3]:
                self.assertTrue(Path(p['image_path']).exists())
                self.assertEqual(p['image_sha256'], hashlib.sha256(Path(p['image_path']).read_bytes()).hexdigest())
