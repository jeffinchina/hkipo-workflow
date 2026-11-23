"""Render a local DOCX to PDF and numbered page PNGs using LibreOffice."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import fitz
parser=argparse.ArgumentParser()
parser.add_argument('docx',type=Path)
parser.add_argument('--output_dir',required=True,type=Path)
parser.add_argument('--emit_pdf',action='store_true')
args=parser.parse_args()
source=args.docx.resolve(strict=True)
out=args.output_dir.resolve();out.mkdir(parents=True,exist_ok=True)
soffice=os.environ.get('HKIPO_SOFFICE') or shutil.which('soffice') or shutil.which('libreoffice')
if not soffice:
    candidate=Path('/Applications/LibreOffice.app/Contents/MacOS/soffice')
    if candidate.is_file():soffice=str(candidate)
if not soffice:raise SystemExit('Install LibreOffice or set HKIPO_SOFFICE')
with tempfile.TemporaryDirectory(prefix='hkipo-office-') as tmp:
    subprocess.run([soffice,'-env:UserInstallation='+Path(tmp).as_uri(),'--headless',
                    '--convert-to','pdf','--outdir',str(out),str(source)],check=True,timeout=150)
pdf=out/(source.stem+'.pdf')
if not pdf.is_file():raise SystemExit('LibreOffice did not produce a PDF')
with fitz.open(pdf) as document:
    if not document.page_count:raise SystemExit('Empty PDF')
    for n,page in enumerate(document,1):
        page.get_pixmap(matrix=fitz.Matrix(1.5,1.5)).save(out/f'page-{n}.png')
if not args.emit_pdf:pdf.unlink()
