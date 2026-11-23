"""Synthetic independent approvals only; no real source is approved here."""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from test_quality import fixture
from hkipo.evidence import _sha, render_page_bytes
from hkipo.scoped_visual import (load_scoped_pages, render_crop_bytes,
    dependency_binding, load_scoped_dependency_review, CHECKS)
from hkipo.quality import validate_bundle
from hkipo.writer import finalize_scoped_job


class ScopedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        self.bundle, self.docs, self.review = fixture(self.root)
        d = self.docs['documents'][0]
        def crop(bbox, name):
            raw, _ = render_crop_bytes(d['path'], 1, bbox, 144)
            p = self.root / name; p.write_bytes(raw)
            return dict(bbox_points=bbox, dpi=144, crop_path=str(p), crop_sha256=_sha(p))
        region = dict(region_id='synthetic-mark', kind='optional_non_sponsor_institution_mark',
            confirmed_non_sponsor_role_group=True, contains_core_numeric_condition_footnote=False,
            unknown_content='Unidentified synthetic mark', forbidden_topics=['identity', 'complete lists'],
            **crop([100, 100, 120, 120], 'mark.png'))
        region['role_context_crop'] = crop([90, 90, 150, 150], 'context.png')
        self.record = dict(document_id=d['document_id'], pdf_page=1, pdf_sha256=d['sha256'],
            image_sha256=hashlib.sha256(render_page_bytes(d['path'], 1)).hexdigest(),
            reviewer='synthetic test reviewer', reviewed_at='2026-11-20T00:00:00Z',
            inspection_record='synthetic fixture only', coordinate_system='unrotated_pdf_points_top_left',
            pdf_rotation=0, remainder_fully_inspected=True, transcription_excludes_unknown_region=True,
            trusted_transcription_segments=['price 10.00', 'old 3'], regions=[region])
        self.registry = self.root / 'scopes.json'; self.record_path = self.root / 'scope.json'
        self.write_scope()
        self.bundle['gaps'] = ['synthetic-mark: source identity remains needs_source']
        self.bp = self.root / 'bundle.json'; self.rp = self.root / 'review.json'
        self.bp.write_text(json.dumps(self.bundle)); self.rp.write_text(json.dumps(self.review))

    def tearDown(self): self.tmp.cleanup()

    def write_scope(self):
        self.record_path.write_text(json.dumps(self.record))
        self.registry.write_text(json.dumps({'version': 1, 'scoped_pages': [{'path': str(self.record_path), 'sha256': _sha(self.record_path)}]}))

    def approve(self, decision='independent', use='unused'):
        scopes = load_scoped_pages(self.registry, self.docs)
        binding = dependency_binding(self.bp, self.rp, self.docs, scopes, {}, self.registry)
        approval = dict(binding=binding, decision=decision, reviewer='independent synthetic reviewer',
            reviewed_at='2026-11-20T00:00:00Z', inspection_record='synthetic fixture semantic check',
            checks={k: True for k in CHECKS}, regions=[{'scope_sha256': self.record_path and _sha(self.record_path),
            'region_id': 'synthetic-mark', 'use': use, 'reason': 'synthetic unused region'}])
        p = self.root / 'approval.json'; p.write_text(json.dumps(approval))
        reg = self.root / 'approvals.json'; reg.write_text(json.dumps({'version': 1, 'scope_approvals': [{'path': str(p), 'sha256': _sha(p)}]}))
        return reg, binding, scopes

    def report(self, reg=None):
        return validate_bundle(self.bundle, self.docs, self.review, scoped_visual_registry=self.registry,
            scope_approval_registry=reg, bundle_path=self.bp, review_path=self.rp)

    def test_research_only_and_exact_independent_approval(self):
        report = self.report(); self.assertFalse(report['pass'])
        self.assertEqual(report['source_visual_gaps'][0]['status'], 'needs_source')
        reg, _, _ = self.approve(); self.assertTrue(self.report(reg)['pass'])

    def test_untrusted_or_used_uncertain_never_pass(self):
        for decision, use in [('uncertain', 'unused'), ('dependent', 'used'), ('independent', 'uncertain')]:
            reg, _, _ = self.approve(decision, use); self.assertFalse(self.report(reg)['pass'])
        reg, _, _ = self.approve(); reg.write_text('{"version":1,"scope_approvals":[]}')
        self.assertFalse(self.report(reg)['pass'])

    def test_bytes_bundle_review_registry_and_source_changes_invalidate(self):
        for p in [self.bp, self.rp, self.registry]:
            reg, _, _ = self.approve(); original = p.read_bytes(); p.write_bytes(original + b' ')
            self.assertFalse(self.report(reg)['pass']); p.write_bytes(original)
        reg, _, _ = self.approve(); self.docs['documents'][0]['sha256'] = '0'*64
        self.assertFalse(self.report(reg)['pass'])

    def test_scope_tampering_duplicate_and_full_conflict(self):
        with self.assertRaises(ValueError):
            load_scoped_pages(self.registry, self.docs, {(self.record['document_id'], 1): {}})
        data = json.loads(self.registry.read_text()); data['scoped_pages'] *= 2
        self.registry.write_text(json.dumps(data))
        with self.assertRaises(ValueError): load_scoped_pages(self.registry, self.docs)
        self.write_scope(); self.record_path.write_text('{}')
        with self.assertRaises(ValueError): load_scoped_pages(self.registry, self.docs)

    def test_core_regions_and_malformed_geometry_rejected(self):
        original = copy.deepcopy(self.record)
        for edit in [('pdf_page', True), ('remainder_fully_inspected', False), ('transcription_excludes_unknown_region', False)]:
            self.record = copy.deepcopy(original); self.record[edit[0]] = edit[1]; self.write_scope()
            with self.assertRaises(ValueError): load_scoped_pages(self.registry, self.docs)
        for value in ['sponsor', 'price', 'issuer']:
            self.record = copy.deepcopy(original); self.record['regions'][0]['kind'] = value; self.write_scope()
            with self.assertRaises(ValueError): load_scoped_pages(self.registry, self.docs)
        for bbox in [[0,0,float('nan'),10], [True,0,20,20], [-1,0,20,20], [0,0,0,10], [0,0,99999,99999]]:
            with self.assertRaises(ValueError): render_crop_bytes(self.docs['documents'][0]['path'],1,bbox,144)

    def test_contiguous_quote_only_never_raw_or_gap_description(self):
        for quote in ['price 10.00 old 3', 'Unidentified synthetic mark', 'Issuer Example']:
            self.bundle['facts'][0]['evidence'][0]['quote'] = quote
            self.assertTrue(any('quote absent' in x for x in self.report()['blocking']))

    def test_missing_gap_disclosure_blocks(self):
        self.bundle['gaps'] = []; self.assertTrue(any('bundle.gaps' in x for x in self.report()['blocking']))

    def test_dependency_must_cover_every_region_and_every_check(self):
        reg, binding, scopes = self.approve()
        p = self.root / 'approval.json'; a = json.loads(p.read_text()); a['regions'] = []
        p.write_text(json.dumps(a)); r=json.loads(reg.read_text()); r['scope_approvals'][0]['sha256']=_sha(p); reg.write_text(json.dumps(r))
        with self.assertRaises(ValueError): load_scoped_dependency_review(reg,binding,scopes)

    def test_stale_finalizer_no_model_or_render(self):
        (self.root/'status.json').write_text(json.dumps({'status':'needs_scoped_visual_review','request_id':'new'}))
        with patch('hkipo.writer.render_bundle') as render:
            with self.assertRaises(ValueError): finalize_scoped_job(self.root,'unused','old')
            render.assert_not_called()

    def test_writer_stops_for_approval_without_revision_and_finalize_no_model(self):
        from hkipo.writer import write_article
        job = self.root / 'job'
        def prepare(*args):
            (job/'sources').mkdir(exist_ok=True)
            (job/'sources/manifest.json').write_text(json.dumps(self.docs))
            return self.docs
        def model(prompt, schema, outdir, name, *args, **kwargs):
            outdir.mkdir(parents=True, exist_ok=True)
            value = self.bundle if name == 'draft' else self.review
            (outdir/(name+'.json')).write_text(json.dumps(value))
            return value
        with patch('hkipo.writer.prepare_documents', side_effect=prepare), patch('hkipo.writer.load_skill', return_value='fixture'), patch('hkipo.writer.run_codex', side_effect=model) as cli:
            result = write_article({'kind':'offer','event_id':'offline-test'},[],job,self.root,
                max_revisions=2, scoped_visual_registry=self.registry, include_docx=False, source_mode='legacy_strict')
            self.assertEqual(result['status'],'needs_scoped_visual_review')
            self.assertEqual(cli.call_count,2)
        self.bp=Path(result['bundle_path']); self.rp=Path(result['review_path'])
        reg, _, _ = self.approve()
        with patch('hkipo.writer.run_codex') as cli:
            ready = finalize_scoped_job(job,reg,result['request_id'])
            self.assertEqual(ready['status'],'ready_for_delivery'); cli.assert_not_called()
        self.assertTrue((Path(ready['output_dir'])/'article.txt').is_file())
        from hkipo.writer import freeze_release
        from hkipo.delivery import verify_release, DeliveryError
        out = Path(ready['output_dir'])
        # Bytes here are a fixture attachment, not a claim of real Word QA.
        (out/'article.docx').write_bytes(b'synthetic Word attachment')
        with self.assertRaises(FileNotFoundError): freeze_release(job)
        qa = out/'visual-qa.json'
        qa.write_text(json.dumps({'confirmed': True, 'docx_sha256': _sha(out/'article.docx')}))
        release = freeze_release(job)
        self.assertEqual(verify_release(release)['event_id'], 'offline-test')
        qa.write_text(qa.read_text() + ' ')
        with self.assertRaises(DeliveryError): verify_release(release)


    def test_missing_other_page_still_blocks(self):
        import hkipo.quality as quality
        original = quality.read_pdf_pages
        def pages(path):
            result = original(path)
            result[1]['status']='visual_required'; result[1]['visual_reasons']=['synthetic second page']
            return result
        reg, _, _ = self.approve()
        with patch('hkipo.quality.read_pdf_pages',side_effect=pages):
            report=self.report(reg)
        self.assertFalse(report['pass'])
        self.assertTrue(any('PDF page 2' in x for x in report['blocking']))

    def test_missing_semantic_check_extra_source_and_full_evidence_invalidate(self):
        reg,binding,scopes=self.approve()
        changed=copy.deepcopy(binding); changed['evidence_fingerprint']='changed'
        with self.assertRaises(ValueError): load_scoped_dependency_review(reg,changed,scopes)
        p=self.root/'approval.json'; approval=json.loads(p.read_text()); approval['checks'].pop('unreferenced_claims')
        p.write_text(json.dumps(approval)); data=json.loads(reg.read_text()); data['scope_approvals'][0]['sha256']=_sha(p); reg.write_text(json.dumps(data))
        with self.assertRaises(ValueError): load_scoped_dependency_review(reg,binding,scopes)

    def test_auto_scope_review_real_images_frozen_context_controller_only_trust(self):
        from hkipo.scoped_visual import run_scoped_dependency_review
        scopes = load_scoped_pages(self.registry,self.docs)
        captured = {}
        def model(prompt,schema,outdir,name,model,effort,**kwargs):
            captured.update(prompt=prompt,images=kwargs['images'],model=model,effort=effort)
            return {'decision':'independent','checks':{k:True for k in CHECKS},
                'regions':[{'scope_index':0,'region_id':'synthetic-mark','use':'unused','reason':'No direct or indirect identity dependence in the frozen fixture'}],
                'summary':'all expressions checked'}
        with patch('hkipo.quota.quota_gate',return_value={'allowed':True}) as quota:
            result=run_scoped_dependency_review(self.bp,self.rp,self.docs,scopes,{},self.registry,
                self.root/'auto',model)
            self.assertEqual(quota.call_count,2)
        self.assertTrue(result['approved']); self.assertEqual(captured['model'],'gpt-6.1-sol')
        self.assertEqual(captured['effort'],'high'); self.assertEqual(len(captured['images']),3)
        self.assertEqual(_sha(captured['images'][0]),self.record['image_sha256'])
        self.assertEqual(_sha(captured['images'][1]),self.record['regions'][0]['crop_sha256'])
        self.assertIn('price 10.00 old 3',captured['prompt'])
        self.assertNotIn(self.review['summary'],captured['prompt'])
        self.assertTrue(self.report(result['registry_path'])['pass'])

    def test_auto_scope_review_rejects_protocol_used_uncertain_and_changed_inputs(self):
        from hkipo.scoped_visual import run_scoped_dependency_review
        scopes=load_scoped_pages(self.registry,self.docs)
        base={'decision':'independent','checks':{k:True for k in CHECKS},
            'regions':[{'scope_index':0,'region_id':'synthetic-mark','use':'unused','reason':'fixture semantic reason'}], 'summary':'checked'}
        for index,use in enumerate(['used','uncertain']):
            raw=copy.deepcopy(base); raw['regions'][0]['use']=use
            with patch('hkipo.quota.quota_gate',return_value={'allowed':True}):
                result=run_scoped_dependency_review(self.bp,self.rp,self.docs,scopes,{},self.registry,
                    self.root/f'auto-{index}',lambda *a,**k:raw)
            self.assertFalse(result['approved']); self.assertFalse((self.root/f'auto-{index}'/'controller-scope-approvals.json').exists())
        for index,mutation in enumerate(['extra','missing','check','changed']):
            raw=copy.deepcopy(base)
            if mutation=='extra': raw['trusted_hash']='model self approval forbidden'
            if mutation=='missing': raw['regions']=[]
            if mutation=='check': raw['checks'].pop('title')
            def model(*a,**k):
                if mutation=='changed': self.bp.write_text(self.bp.read_text()+' ')
                return raw
            with patch('hkipo.quota.quota_gate',return_value={'allowed':True}):
                with self.assertRaises(ValueError):
                    run_scoped_dependency_review(self.bp,self.rp,self.docs,scopes,{},self.registry,
                        self.root/f'bad-auto-{index}',model)

    def test_writer_auto_once_used_or_bad_protocol_never_revises(self):
        from hkipo.writer import write_article
        for number,outcome in enumerate(['unused','used','uncertain','bad']):
            job=self.root/f'auto-job-{number}'; calls=[]
            def prepare(*args):
                (job/'sources').mkdir(exist_ok=True)
                (job/'sources/manifest.json').write_text(json.dumps(self.docs)); return self.docs
            def model(prompt,schema,outdir,name,*args,**kwargs):
                calls.append(name); outdir.mkdir(parents=True,exist_ok=True)
                value=self.bundle if name=='draft' else self.review
                if name=='scope-dependency-review':
                    value={'decision':'independent','checks':{k:True for k in CHECKS},
                        'regions':[{'scope_index':0,'region_id':'synthetic-mark','use':outcome,'reason':'synthetic semantic check'}], 'summary':'synthetic complete audit'}
                (outdir/(name+'.json')).write_text(json.dumps(value)); return value
            with patch('hkipo.writer.prepare_documents',side_effect=prepare), patch('hkipo.writer.load_skill',return_value='fixture'), patch('hkipo.writer.run_codex',side_effect=model), patch('hkipo.writer.render_bundle',return_value={'synthetic':True}), patch('hkipo.quota.quota_gate',return_value={'allowed':True}):
                status=write_article({'kind':'offer','event_id':'offline-test'},[],job,self.root,
                    max_revisions=2,scoped_visual_registry=self.registry,auto_scope_review=True,source_mode='legacy_strict')
            self.assertEqual(calls,['draft','review','scope-dependency-review'])
            self.assertEqual(status['status'],'ready_for_delivery' if outcome=='unused' else 'needs_scoped_visual_review')
            if outcome!='unused': self.assertFalse(status['auto_scope_review']['approved'])

    def test_auto_raw_audit_tampering_invalidates_controller_approval(self):
        from hkipo.scoped_visual import run_scoped_dependency_review
        scopes=load_scoped_pages(self.registry,self.docs)
        raw={'decision':'independent','checks':{k:True for k in CHECKS},
            'regions':[{'scope_index':0,'region_id':'synthetic-mark','use':'unused','reason':'fixture audit'}], 'summary':'checked'}
        with patch('hkipo.quota.quota_gate',return_value={'allowed':True}):
            result=run_scoped_dependency_review(self.bp,self.rp,self.docs,scopes,{},self.registry,self.root/'raw-tamper',lambda *a,**k:raw)
        Path(result['raw_review_path']).write_text(json.dumps(raw)+' ')
        self.assertFalse(self.report(result['registry_path'])['pass'])

    def test_auto_quota_checked_after_runner_failure_and_deferred_no_approval(self):
        from hkipo.scoped_visual import run_scoped_dependency_review
        from hkipo.quota import QuotaDeferred
        scopes=load_scoped_pages(self.registry,self.docs)
        def fail(*a,**k): raise RuntimeError('synthetic CLI failure')
        with patch('hkipo.quota.quota_gate',return_value={'allowed':True}) as quota:
            with self.assertRaisesRegex(RuntimeError,'synthetic CLI failure'):
                run_scoped_dependency_review(self.bp,self.rp,self.docs,scopes,{},self.registry,self.root/'failed-cli',fail)
            self.assertEqual(quota.call_count,2)
        with patch('hkipo.quota.quota_gate',side_effect=QuotaDeferred({'synthetic':True})):
            with self.assertRaises(QuotaDeferred):
                run_scoped_dependency_review(self.bp,self.rp,self.docs,scopes,{},self.registry,self.root/'quota-deferred',fail)
        self.assertFalse((self.root/'quota-deferred'/'controller-scope-approvals.json').exists())
