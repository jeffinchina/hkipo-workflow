import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from PIL import Image

from hkipo.delivery import DeliveryError
from hkipo.draft_covers import OfferDraftCovers
from hkipo.store import StateStore


class OfferDraftCoverTests(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = StateStore(self.root / 'state.sqlite3')
        self.default = 'fixture-default-media'
        self.red = self._image('red', (180, 30, 30))
        self.blue = self._image('blue', (30, 30, 180))

    def _image(self, name, color):
        path = self.root / (name + '.png')
        Image.new('RGB', (300, 150), color).save(path)
        return str(path)

    def _issue(self, code, publication_day, opening_day=None, *, trusted=True):
        event_id = hashlib.sha256(f'event:{code}'.encode()).hexdigest()
        source_bytes = f'source:{code}'.encode()
        source = self.root / (code + '.pdf')
        source.write_bytes(source_bytes)
        source_sha = hashlib.sha256(source_bytes).hexdigest()
        event = {
            'event_id': event_id, 'stock_code': code, 'kind': 'offer',
            'episode_id': f'ipo-{code}-{publication_day}-aabbcc112233',
            'source_hashes': [{'sha256': source_sha, 'path': str(source)}],
        }
        release = {'event_id': event_id}
        if opening_day:
            year, month, day = map(int, opening_day.split('-'))
            timetable = f'香港公開發售開始 ...... {year}年{month}月{day}日'
            manifest = self.root / (code + '.manifest.json')
            manifest.write_text(json.dumps({'documents': [{
                'document_id': source_sha[:12], 'sha256': source_sha if trusted else 'bad',
                'path': str(source), 'pages': [{'pdf_page': 1, 'text': timetable}],
            }]}, ensure_ascii=False))
            release['sourcesmanifest_path'] = str(manifest)
        return release, event

    def test_independent_opening_days_keep_the_default_cover(self):
        selector = OfferDraftCovers(self.store, [self.red])
        first = self._issue('01231', '2026-11-25', '2026-11-25')
        second = self._issue('01232', '2026-11-26', '2026-11-26')
        self.assertEqual(selector.select(*first, self.default), {'thumb_media_id': self.default})
        self.assertEqual(selector.select(*second, self.default), {'thumb_media_id': self.default})

    def test_same_opening_day_with_different_issue_days_allocates_distinct_covers(self):
        selector = OfferDraftCovers(self.store, [self.red, self.blue])
        a = self._issue('01231', '2026-11-25', '2026-11-27')
        b = self._issue('01232', '2026-11-26', '2026-11-27')
        c = self._issue('01233', '2026-11-27', '2026-11-27')
        self.assertEqual(selector.select(*a, self.default), {'thumb_media_id': self.default})
        self.assertEqual(selector.select(*b, self.default), {'cover_path': self.red, 'cover_authorized': True})
        self.assertEqual(selector.select(*c, self.default), {'cover_path': self.blue, 'cover_authorized': True})

    def test_opening_day_and_other_issues_prospectus_day_overlap(self):
        selector = OfferDraftCovers(self.store, [self.red])
        first = self._issue('01231', '2026-11-27')
        second = self._issue('01232', '2026-11-26', '2026-11-27')
        self.assertEqual(selector.select(*first, self.default), {'thumb_media_id': self.default})
        self.assertEqual(selector.select(*second, self.default), {'cover_path': self.red, 'cover_authorized': True})

    def test_restart_and_cover_order_change_preserve_assigned_cover(self):
        first = self._issue('01231', '2026-11-27')
        second = self._issue('01232', '2026-11-27')
        selector = OfferDraftCovers(self.store, [self.red, self.blue])
        selector.select(*first, self.default)
        self.assertEqual(selector.select(*second, self.default)['cover_path'], self.red)

        reopened = OfferDraftCovers(StateStore(self.store.dbpath), [self.blue, self.red])
        release, event = second
        revised = dict(event, research_id='next-reviewed-research')
        self.assertEqual(reopened.select(release, revised, self.default)['cover_path'], self.red)

    def test_duplicate_image_bytes_do_not_supply_another_cover(self):
        duplicate = self.root / 'duplicate.png'
        duplicate.write_bytes(Path(self.red).read_bytes())
        selector = OfferDraftCovers(self.store, [self.red, str(duplicate)])
        selector.select(*self._issue('01231', '2026-11-27'), self.default)
        self.assertEqual(selector.select(*self._issue('01232', '2026-11-27'), self.default)['cover_path'], self.red)
        with self.assertRaisesRegex(DeliveryError, '^offer_cover_pool_exhausted$'):
            selector.select(*self._issue('01233', '2026-11-27'), self.default)

    def test_unlinked_timetable_and_unverified_episode_cannot_assign_a_cover(self):
        selector = OfferDraftCovers(self.store, [self.red])
        release, event = self._issue('01231', 'not-a-date', '2026-11-27', trusted=False)
        with self.assertRaisesRegex(DeliveryError, '^offer_cover_date_unverified$'):
            selector.select(release, event, self.default)
