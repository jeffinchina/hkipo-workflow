import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from hkipo.writer import write_article
from hkipo.quota import QuotaDeferred, QuotaUnavailable, evaluate_limits


class AutoVisualTests(unittest.TestCase):
    def invoke(self, worker, states=None, quota=None):
        with tempfile.TemporaryDirectory() as tmp:
            self.job = Path(tmp)
            manifest = {'documents': [{'document_id': 'd', 'sha256': 'pdf', 'pages_sha256': 'pages', 'pages': [
                {'pdf_page': 1, 'status': 'visual_required'}]}]}
            with patch('hkipo.writer.prepare_documents', return_value=manifest), \
                 patch('hkipo.writer.trusted_visuals', side_effect=states or [([], [], {})]), \
                 patch('hkipo.scoped_visual.load_scoped_pages', return_value={}), \
                 patch('hkipo.visual_worker.run_visual_worker', side_effect=worker) as visual, \
                 patch('hkipo.writer.load_skill', return_value='rules'), \
                 patch('hkipo.writer.documents_context', return_value='source'), \
                 patch('hkipo.writer.run_codex') as model, \
                 patch('hkipo.writer.quota_gate', side_effect=quota) as gate:
                model.return_value = {'event_id': 'wrong'}
                try:
                    status = write_article({'kind': 'offer', 'event_id': 'e'}, [], tmp, '/skill',
                                           auto_visual_review=True, settings_path='/settings', source_mode='legacy_strict')
                except ValueError as exc:
                    self.assertIn('mismatched event', str(exc))
                    status = {'status': 'draft_called'}
                return status, visual.call_args, model.call_args_list, gate.call_args_list

    def test_visual_precedes_draft_and_forwards_settings(self):
        def worker(*args, **kwargs):
            self.assertEqual(kwargs['max_attempts'], 1)
            self.assertEqual(kwargs['batch_size'], 4)
            self.assertEqual(kwargs['effort'], 'low')
            kwargs['run_model'](prompt='visual', schema={}, outdir='/unused', name='v')
            kwargs['quota_check']()
            kwargs['quota_check']()
            return {'status': 'complete'}
        status, call, models, gates = self.invoke(worker,
            [([], [], {}), ([], [], {('d', 1): {'decision': 'transcribed'}})],
            quota=[{'allowed': True}, {'allowed': True}])
        self.assertEqual(status['status'], 'draft_called')
        self.assertEqual([c.kwargs['settings_path'] for c in models], ['/settings', '/settings'])
        self.assertEqual(len(gates), 2)

    def test_trusted_page_skips_worker(self):
        status, call, models, _ = self.invoke(lambda *a, **k: self.fail('unexpected visual'),
            [([], [], {('d', 1): {}})])
        self.assertIsNone(call)
        self.assertEqual(len(models), 1)

    def test_needs_source_and_batch_limit_do_not_draft(self):
        for state in ('needs_source', 'batch_limit'):
            with self.subTest(state=state):
                status, _, models, _ = self.invoke(lambda *a, **k: {'status': state},
                                                   [([], [], {}), ([], [], {})])
                self.assertEqual(status['status'], 'needs_visual_review')
                self.assertEqual(models, [])
                if state == 'batch_limit':
                    self.assertIn('bounded', status['message'])

    def test_quota_deferred_is_persisted_without_draft(self):
        now = time.time()
        observation = evaluate_limits({'rateLimits': {'limitId': 'codex', 'primary': {
            'windowDurationMins': 300, 'usedPercent': 80, 'resetsAt': now+300}}}, now=now)
        def worker(*args, **kwargs):
            observed = kwargs['quota_check']()
            return {'status': 'quota_stopped', 'batches_called': 0,
                    'quota_observations': [{'observation': observed}]}
        status, _, models, _ = self.invoke(worker, quota=QuotaDeferred(observation))
        self.assertEqual(status['reason'], 'quota_reserve')
        self.assertIn('quota_defer', status)
        self.assertEqual(models, [])

    def test_unavailable_quota_is_not_exhaustion_and_blocks_even_if_pages_ready(self):
        def worker(*args, **kwargs):
            with self.assertRaises(QuotaUnavailable):
                kwargs['quota_check']()
            return {'status': 'quota_stopped', 'quota_observations': [],
                    'failures': [{'error': 'unavailable'}]}
        status, _, models, _ = self.invoke(worker,
            [([], [], {}), ([], [], {('d', 1): {}})],
            quota=QuotaUnavailable('unavailable'))
        self.assertEqual(status['status'], 'needs_visual_review')
        self.assertNotIn('quota_defer', status)
        self.assertEqual(models, [])
