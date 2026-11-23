"""Local geometry checks using a previously inspected, frozen HTML template.

This does not assert that a screenshot has received human/AI visual review or
that the WeChat client is identical to Chrome. Initial real draft UI acceptance
remains a deployment prerequisite, recorded separately.
"""
import hashlib
import os
import shutil
import inspect
import json
from pathlib import Path
import subprocess
from . import render
from .mobile import MobileWorkflow


def template_fingerprint():
    source='\n'.join(inspect.getsource(f) for f in (MobileWorkflow.preview,render.article_html,render.footer_blocks))
    source+='\n'+render.QR_SHA256+'\n'+str(render.QR_WIDTH_PX)
    source+='\n'+json.dumps(render.defaults()['style'],sort_keys=True)
    return hashlib.sha256(source.encode()).hexdigest()


def approve_layout(directory,template_approval,*,renderer=None):
    directory=Path(directory).resolve()
    try:
        profile=json.loads(Path(template_approval).read_text())
        if profile.get('template_sha256')!=template_fingerprint() or profile.get('local_visual_confirmed') is not True:
            return {'status':'needs_template_visual_review'}
        manifest=json.loads((directory/'manifest.json').read_text())
        original=(directory/'preview.html').read_bytes()
        expected_html=hashlib.sha256((directory/'article.html').read_bytes()).hexdigest()
        if (expected_html!=manifest['html_sha256'] or
                hashlib.sha256(original).hexdigest()!=manifest.get('preview_sha256')):return {'status':'render_changed'}
        if renderer is None:
            root=Path(__file__).resolve().parents[2]
            node=os.environ.get('HKIPO_NODE') or shutil.which('node')
            module=Path(os.environ.get('HKIPO_PLAYWRIGHT_MODULE', str(root/'node_modules/playwright/index.mjs')))
            if not node or not module.is_file():return {'status':'layout_renderer_unavailable'}
            script=Path(__file__).resolve().parents[1]/'scripts/mobile_layout.mjs'
            result=subprocess.run([str(node),str(script),str(directory),str(module)],capture_output=True,text=True,timeout=60)
            if result.returncode:return {'status':'layout_renderer_unavailable'}
            geometry=json.loads(result.stdout)
        else:geometry=renderer(directory)
        if ((directory/'preview.html').read_bytes()!=original or
                hashlib.sha256((directory/'article.html').read_bytes()).hexdigest()!=expected_html):
            return {'status':'render_changed'}
        valid=(geometry.get('viewport_width')==390 and geometry.get('scroll_width')==390
               and geometry.get('overflow_count')==0 and geometry.get('broken_images')==0
               and geometry.get('image_count')==len(manifest['source_images'])+1
               and geometry.get('qr_width')==225 and 0<geometry.get('height',0)<=40000)
        if not valid:return {'status':'needs_layout_attention','geometry':geometry}
        approval={'confirmed':True,**{k:manifest[k] for k in ('raw_hash','version','html_sha256','preview_sha256','author_sha256','source_event_sha256')},
                  'template_sha256':profile['template_sha256'],'verification_kind':'geometry_with_preinspected_template',
                  'wechat_ui_verified':False,'geometry':geometry}
        (directory/'layout-approval.json').write_text(json.dumps(approval,ensure_ascii=False,indent=2))
        return {'status':'approved_local_layout','approval':approval}
    except Exception:return {'status':'layout_renderer_or_input_unavailable'}
