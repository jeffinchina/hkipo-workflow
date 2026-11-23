import tempfile
from pathlib import Path
import unittest
import fitz
from hkipo.classification import inspect_cover


class CoverCodeSpacingTests(unittest.TestCase):
    def inspect(self,code,second=None,date_line=None):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'prospectus.pdf'
            with fitz.open() as pdf:
                page=pdf.new_page()
                page.insert_text((60,60),'Example Company Limited\n(incorporated in Hong Kong)\nStock Code: '+code+
                    '\nGLOBAL OFFERING\nNumber of Offer Shares\npayable in full on application\nIMPORTANT NOTICE\n342C')
                if date_line:page.insert_text((60,240),date_line,fontname='china-t')
                if second:pdf.new_page().insert_text((60,60),'Stock Code: '+second)
                pdf.save(path)
            return inspect_cover(path,'09941')

    def test_spaced_digits_on_cover_and_unspaced_inside_are_the_same_security(self):
        result=self.inspect('9 9 41','9941')
        self.assertTrue(result['pass'],result)
        self.assertEqual(result['codes'],['09941'])
        self.assertIn('Stock Code: 9 9 41',[e['quote'] for e in result['evidence'] if e['rule']=='labelled_stock_code'])

    def test_different_code_extra_digit_or_next_line_number_cannot_be_joined(self):
        for code in ('9942','999941','9\n9 41'):
            with self.subTest(code=code):self.assertFalse(self.inspect(code)['pass'])

    def test_standalone_cover_date_allows_typographical_spaces_but_not_body_dates(self):
        self.assertEqual(self.inspect('9941',date_line='2026 年11 月16 日')['publication_dates'],['2026-11-16'])
        self.assertEqual(self.inspect('9941',date_line='截止2026年11月16日')['publication_dates'],[])

if __name__=='__main__':unittest.main()
