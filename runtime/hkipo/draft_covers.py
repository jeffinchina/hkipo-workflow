"""Reserve distinct private WeChat covers for concurrent IPO drafts."""
from datetime import date
import json
from pathlib import Path
import re

from .delivery import DeliveryError, sha
from .offer_notice import opening_dates
from .render_profile import defaults


def _date(value):
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError):
        return None


def _frozen_opening_day(release, event):
    try:
        sources = {source['sha256'] for source in event.get('source_hashes', [])}
        if not sources or not release.get('sourcesmanifest_path'):
            return None
        manifest = json.loads(Path(release['sourcesmanifest_path']).read_text())
        days = set()
        for document in manifest['documents']:
            if document.get('sha256') in sources:
                for page in document['pages'][:16]:
                    days.update(opening_dates(page['text']))
        return days.pop().isoformat() if len(days) == 1 else None
    except (OSError, KeyError, TypeError, ValueError):
        return None


def _cover_key(path):
    try:
        if path.suffix.lower() not in {'.jpg', '.jpeg', '.png'} or not 0 < path.stat().st_size <= 10 * 1024 * 1024:
            return None
        return 'file:' + sha(path.read_bytes())
    except OSError:
        return None


class OfferDraftCovers:
    def __init__(self, store, alternate_paths):
        self.store = store
        self.alternates = [Path(path).expanduser().resolve() for path in alternate_paths]
        with store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS offer_draft_covers (
                event_id TEXT PRIMARY KEY, opening_day TEXT, prospectus_day TEXT,
                cover_key TEXT NOT NULL, media_id TEXT, cover_path TEXT)''')

    @staticmethod
    def _reserved(row):
        if row['cover_path'] is None:
            return {'thumb_media_id': row['media_id']}
        if _cover_key(Path(row['cover_path'])) != row['cover_key']:
            raise DeliveryError('offer_cover_reserved_asset_changed')
        return {'cover_path': row['cover_path'], 'cover_authorized': True}

    def select(self, release, event, default_media_id=None):
        event_id = event['event_id']
        episode_id = event.get('episode_id', '')
        official_episode = re.fullmatch(r'ipo-\d{5}-(\d{4}-\d{2}-\d{2})-[0-9a-f]{12}', episode_id)
        prospectus = _date(official_episode[1]) if official_episode else None
        opening = _frozen_opening_day(release, event)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT * FROM offer_draft_covers WHERE event_id=?', (event_id,)).fetchone()
            if previous:
                return self._reserved(previous)
            if opening is None and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='offer_notices'").fetchone():
                notice = db.execute('SELECT payload FROM offer_notices WHERE episode_id=?', (episode_id,)).fetchone()
                if notice:
                    opening = _date(json.loads(notice['payload']).get('opening_date'))
            if not opening and not prospectus:
                raise DeliveryError('offer_cover_date_unverified')
            # An issue may be queued long after another one in the same cohort.
            # Compare both official dates and keep the allocation across retries.
            peers = db.execute('''SELECT cover_key FROM offer_draft_covers
                WHERE opening_day IN (?,?) OR prospectus_day IN (?,?)''',
                (opening, prospectus, opening, prospectus)).fetchall()
            used = {row['cover_key'] for row in peers}
            media_id = default_media_id or defaults().get('thumb_media_id')
            key = 'default'
            path = None
            if key in used:
                for candidate in self.alternates:
                    digest = _cover_key(candidate)
                    if digest and digest not in used:
                        key, path, media_id = digest, str(candidate), None
                        break
                else:
                    raise DeliveryError('offer_cover_pool_exhausted')
            db.execute('INSERT INTO offer_draft_covers VALUES(?,?,?,?,?,?)',
                       (event_id, opening, prospectus, key, media_id, path))
            return {'thumb_media_id': media_id} if path is None else {'cover_path': path, 'cover_authorized': True}
