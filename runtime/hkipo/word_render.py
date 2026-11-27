"""Render the actual Word attachment through local, read-only conversion tools."""

import os
import shutil
import signal
import subprocess
import tempfile
import time
from contextlib import suppress
from pathlib import Path


class RenderUnavailable(RuntimeError):
    """None of the available local renderers produced a complete page set."""

    def __init__(self, failures=()):
        super().__init__('No local Word renderer produced a complete page set')
        self.failures = tuple(failures)


def run_renderer(command, directory, env, timeout):
    with (directory / 'renderer.log').open('wb') as log:
        process = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            raise RuntimeError('Word renderer timed out; see local renderer.log') from None
    if process.returncode:
        raise RuntimeError('Word renderer failed; see local renderer.log')


def office_executables(env, bundled=(), wrappers=()):
    """Discover real executables in preference order, without starting a GUI."""
    explicit = env.get('HKIPO_SOFFICE')
    candidates = [
        explicit,
        '/Applications/LibreOffice.app/Contents/MacOS/soffice',
        '/opt/homebrew/bin/soffice',
        '/usr/local/bin/soffice',
        shutil.which('soffice', path=env.get('PATH')),
        shutil.which('libreoffice', path=env.get('PATH')),
        *bundled,
    ]
    seen = {Path(path).resolve() for path in wrappers}
    for name in candidates:
        if not name:
            continue
        path = Path(name).expanduser().resolve()
        if path not in seen and path.is_file() and os.access(path, os.X_OK):
            seen.add(path)
            yield path


def render_with_office(executable, docx, directory, env, timeout, max_pages):
    """Convert a DOCX in an isolated LibreOffice profile and rasterize every page."""
    import fitz

    with tempfile.TemporaryDirectory(prefix='office-', dir=directory) as profile:
        run_renderer([
            str(executable), '-env:UserInstallation=' + Path(profile).as_uri(),
            '--headless', '--convert-to', 'pdf:writer_pdf_Export', '--outdir',
            str(directory), str(docx),
        ], directory, env, timeout)

    pdf = directory / (docx.stem + '.pdf')
    if not pdf.is_file():
        raise RuntimeError('LibreOffice produced no PDF')
    with fitz.open(pdf) as document:
        if document.is_encrypted or not 1 <= document.page_count <= max_pages:
            raise ValueError('Invalid Word PDF page count')
        for index, page in enumerate(document, 1):
            if max(page.rect.width, page.rect.height) > 2500:
                raise ValueError('Word PDF page exceeds rendering size')
            page.get_pixmap(dpi=144, colorspace=fitz.csRGB, alpha=False).save(
                directory / f'page-{index}.png')


def pages(directory, max_pages):
    from PIL import Image, ImageChops

    files = list(directory.glob('page-*.png'))
    try:
        files.sort(key=lambda path: int(path.stem.split('-')[1]))
    except ValueError:
        raise ValueError('Invalid rendered page name') from None
    if not 1 <= len(files) <= max_pages or [p.name for p in files] != [
        f'page-{number}.png' for number in range(1, len(files) + 1)
    ]:
        raise ValueError('Missing or abnormal rendered page count')
    for page in files:
        with Image.open(page) as source:
            if source.format != 'PNG' or min(source.size) < 100 or max(source.size) > 10000:
                raise ValueError('Invalid rendered PNG')
            source.load()
            rgb = source.convert('RGB')
            if ImageChops.difference(rgb, Image.new('RGB', rgb.size, 'white')).getbbox() is None:
                raise ValueError('Blank rendered page')
    return files


def render_word_backends(docx, destination, *, environments, managed=None, timeout=180,
                         max_pages=20, bundled=(), wrappers=(), exclude_engines=()):
    """Try isolated renderers in order. Accept only a PDF with all page images."""
    try:
        import fitz
    except ImportError as error:
        raise RenderUnavailable([('pdf-validation', type(error).__name__)]) from error

    source = Path(docx).resolve(strict=True)
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    if list(destination.glob('page-*.png')) or (destination / (source.stem + '.pdf')).exists():
        raise ValueError('Word renderer requires a fresh page directory')

    backends = [('office', str(executable), executable)
                for executable in office_executables(os.environ, bundled, wrappers)]
    if managed:
        engine, command = managed
        explicit = os.environ.get('HKIPO_SOFFICE')
        preferred = explicit and backends and Path(explicit).expanduser().resolve() == backends[0][2]
        backends.insert(1 if preferred else 0, ('managed', engine, command))
    backends = [backend for backend in backends if backend[1] not in exclude_engines]
    failures = []
    deadline = time.monotonic() + timeout
    for index, (kind, engine, executable) in enumerate(backends):
        # Reserve time for later backends even if the first process hangs.
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        budget = remaining / (len(backends) - index)
        stage = Path(tempfile.mkdtemp(prefix=kind + '-', dir=destination))
        try:
            env = environments(stage / 'environment')
            if kind == 'managed':
                run_renderer([*executable, str(source), '--output_dir', str(stage), '--emit_pdf'],
                             stage, env, budget)
            else:
                render_with_office(executable, source, stage, env, budget, max_pages)
            rendered = pages(stage, max_pages)
            pdf = stage / (source.stem + '.pdf')
            with fitz.open(pdf) as document:
                if document.is_encrypted or document.page_count != len(rendered):
                    raise ValueError('Rendered images do not cover the Word PDF')
        except (ImportError, OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
            failures.append((kind, type(error).__name__))
            continue
        for item in [*rendered, pdf]:
            item.replace(destination / item.name)
        return {'backend': kind, 'engine': engine}
    raise RenderUnavailable(failures)
