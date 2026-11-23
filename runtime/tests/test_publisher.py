import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock
from hkipo.publisher import Publisher, PublisherSettings
from hkipo.quota import QuotaDeferred


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.research = self.root / 'research'; self.research.mkdir()
        self.preparer = Mock(side_effect=self.prepare)
        self.sender = Mock()
        self.sender.email.return_value = {'status': 'confirmed'}
        self.sender.draft.return_value = {'status': 'confirmed'}
        self.settings = PublisherSettings(str(self.root / 'publisher'), str(self.root / 'state.db'),
            str(self.research), '/never/read/private.json', max_jobs=2)
        self.publisher = Publisher(self.settings, preparer=self.preparer, deliverer=self.sender)

    def prepare(self, job, **kwargs):
        status = json.loads((job / 'status.json').read_text())
        return {'event_id': 'event-' + job.name, 'request_id': status['request_id']}

    def add(self, number, baseline=0, malformed=False, status_request='r1'):
        rid = f'{number:064x}'; job = self.research / rid; job.mkdir()
        (job / 'status.json').write_text(json.dumps({'status': 'ready_for_delivery', 'request_id': status_request}))
        payload = {'event': {'event_id': 'event-' + rid, 'source_event_ids': ['source-' + rid]},
                   'writer_result': {'request_id': 'r1'}}
        with self.publisher.store.connect() as c:
            c.execute('INSERT INTO events VALUES(?,?,?,?)', ('source-' + rid, '{}', baseline, str(number)))
            c.execute('INSERT INTO jobs(event_id,status,updated_at) VALUES(?,?,?)', ('source-' + rid, 'ready_for_delivery', str(number)))
            c.execute('INSERT INTO research_runs VALUES(?,?,?,?)', (rid, '{bad' if malformed else json.dumps(payload),
                'ready_for_delivery', f'{number:03}'))
        return rid

    def enable(self):
        self.settings.enable_deliveries = True
        self.settings.production_accepted = True

    def test_disabled_delivery_prepares_all_jobs_without_starvation(self):
        for number in range(1, 6): self.add(number)
        results = [self.publisher.cycle() for _ in range(3)]
        self.assertEqual([len(r['results']) for r in results], [2, 2, 1])
        self.assertEqual(self.preparer.call_count, 5)
        self.sender.email.assert_not_called(); self.sender.draft.assert_not_called()
        self.assertEqual(self.publisher.cycle()['results'], [])

    def test_acceptance_allowlist_does_not_expand_to_other_or_baseline_events(self):
        allowed = self.add(1, baseline=1); self.add(2, baseline=0)
        self.settings.acceptance_mode = True
        self.settings.accepted_event_ids = ['event-' + allowed]
        self.settings.enable_deliveries = True
        result = self.publisher.cycle()['results']
        self.assertEqual([r['status'] for r in result], ['delivered', 'blocked'])
        self.assertEqual(self.sender.email.call_count, 1)

    def test_baseline_event_never_delivered_in_production(self):
        self.add(1, baseline=1); self.enable()
        self.assertEqual(self.publisher.cycle()['results'][0]['status'], 'blocked')
        self.preparer.assert_not_called(); self.sender.email.assert_not_called()

    def test_request_change_before_preparation_blocks(self):
        self.add(1, status_request='r2'); self.enable()
        result = self.publisher.cycle()['results'][0]
        self.assertEqual(result['reason'], 'queued_writer_request_changed')
        self.preparer.assert_not_called(); self.sender.email.assert_not_called()

    def test_request_change_during_preparation_blocks_delivery(self):
        self.add(1); self.enable()
        def mutate(job, **kwargs):
            release = self.prepare(job)
            (job / 'status.json').write_text('{"status":"ready_for_delivery","request_id":"r2"}')
            return release
        self.preparer.side_effect = mutate
        self.assertEqual(self.publisher.cycle()['results'][0]['reason'], 'queued_writer_request_changed')
        self.sender.email.assert_not_called()

    def test_malformed_job_does_not_block_other_jobs(self):
        self.add(1, malformed=True); self.add(2)
        self.assertEqual([r['status'] for r in self.publisher.cycle()['results']], ['blocked', 'prepared'])
        self.assertEqual(self.preparer.call_count, 1)

    def test_unknown_draft_requires_attention_without_polling_retry(self):
        self.add(1); self.enable()
        self.sender.draft.return_value = {'status': 'unknown'}
        self.assertEqual(self.publisher.cycle()['results'][0]['status'], 'needs_attention')
        for _ in range(3): self.assertEqual(self.publisher.cycle()['results'], [])
        self.assertEqual(self.sender.email.call_count, 1); self.assertEqual(self.sender.draft.call_count, 1)
        health=self.publisher.cycle()
        self.assertEqual(health['status'],'needs_attention');self.assertFalse(health['ok'])
        self.assertEqual(health['attention'][0]['status'],'needs_attention')

    def test_verified_quota_deferral_persists_retry_and_stops_cycle(self):
        first = self.add(1); self.add(2)
        now = time.time(); reset = now + 300
        observation = {'allowed': False, 'limit_id': 'codex', 'observed_at': now,
            'blocking_resets_at': reset, 'reserve': .2, 'defer_reason': 'quota_reserve',
            'blocking_windows': [{'usedPercent': 85, 'windowDurationMins': 300, 'resetsAt': reset}]}
        self.preparer.side_effect = QuotaDeferred(observation)
        results = self.publisher.cycle()['results']
        self.assertEqual(len(results), 1); self.assertEqual(results[0]['status'], 'deferred')
        self.assertEqual(self.preparer.call_count, 1)
        with self.publisher.store.connect() as c:
            row = c.execute('SELECT retry_at FROM publication_runs WHERE research_id=?', (first,)).fetchone()
        self.assertEqual(row['retry_at'], reset + 120)
        self.preparer.side_effect = self.prepare
        self.assertEqual(self.publisher.cycle()['results'][0]['status'], 'prepared')
        self.assertEqual(self.preparer.call_count, 2)
        stop = threading.Event(); stop.set()
        self.assertEqual(self.publisher.run_loop(stop)['cycles'], 0)
        self.assertEqual(self.preparer.call_count, 2)

    def test_delivery_requires_explicit_acceptance(self):
        self.add(1); self.settings.enable_deliveries = True
        self.assertEqual(self.publisher.cycle()['reason'], 'real_delivery_acceptance_required')
        self.preparer.assert_not_called(); self.sender.email.assert_not_called()

    def test_preparation_failure_does_not_block_other_jobs(self):
        self.add(1); self.add(2)
        def fail_first(job, **kwargs):
            if int(job.name, 16) == 1: raise RuntimeError('private diagnostic')
            return self.prepare(job)
        self.preparer.side_effect = fail_first
        result = self.publisher.cycle()['results']
        self.assertEqual([row['status'] for row in result], ['blocked', 'prepared'])
        self.assertNotIn('private diagnostic', json.dumps(result))

    def test_unverified_quota_deferral_is_blocked_without_unbounded_retry(self):
        self.add(1)
        self.preparer.side_effect = QuotaDeferred({'allowed': False})
        self.assertEqual(self.publisher.cycle()['results'][0]['status'], 'blocked')
        self.assertEqual(self.publisher.cycle()['results'], [])
        self.assertEqual(self.preparer.call_count, 1)
