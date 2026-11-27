"""Text-only subscription alerts based on confirmed HKEX offering documents."""
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from email.policy import SMTP
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from .delivery import BeforeArticleDelivery, DEFAULT_RECIPIENT, fingerprint, load_private_config, sha, smtp_send
from .models import canonical_url

HONG_KONG = ZoneInfo('Asia/Hong_Kong')
_ROW_DATE = r'(?:[（(]\d+[）)])?[\s.．…:：]{0,250}(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日'
_OPENING_DATE = re.compile(r'^香港公[開开][發发]售[：:]?(?:開始|开始)' + _ROW_DATE, re.MULTILINE)
_CLOSING_DATE = re.compile(r'^截止[辦办]理(?:香港公[開开][發发]售|[認认][購购])?申[請请]登[記记]' + _ROW_DATE, re.MULTILINE)


def opening_dates(text, pattern=_OPENING_DATE):
    # The separate opening of formal application registration often falls on
    # closing day. Match only the timetable row that starts the public offer.
    text = re.sub(r'(?m)^[ \t\u00a0]+', '', text)
    text = re.sub(r'[ \t\u00a0]+', '', text)
    return {date(int(y), int(m), int(d)) for y, m, d in pattern.findall(text)}


def official_timetable(decision):
    import fitz

    dates = set()
    closing_dates = set()
    sources = set()
    for entry in decision['audit']:
        cover = entry.get('cover', {})
        if cover.get('pass') is not True or entry.get('role_conflict'):
            continue
        links = {canonical_url(e['url']) for e in entry['official_evidence']
                 if e['role'] == cover['document_role'] and e['stock_code'] == decision['stock_code']}
        if not links:
            continue
        pdf = Path(cover['path']).read_bytes()
        if sha(pdf) != cover['sha256']:
            raise ValueError('classified_document_changed')
        with fitz.open(stream=pdf, filetype='pdf') as doc:
            texts = [doc[page].get_text() for page in range(min(16, len(doc)))]
        found = {day for page in texts for day in opening_dates(page)}
        if found:
            dates.update(found)
            closing_dates.update(day for page in texts for day in opening_dates(page, _CLOSING_DATE))
            sources.update(links)
    if len(dates) != 1:
        raise ValueError('missing_or_conflicting_opening_timetable')
    start = dates.pop()
    close = next(iter(closing_dates)) if len(closing_dates) == 1 else None
    return start, close if close is not None and close >= start else None, sorted(sources)


class OfferNotices:
    def __init__(self, store, classification_dir, config_path, enabled_since, *, sender=smtp_send):
        self.store = store
        self.classifications = Path(classification_dir)
        self.config_path = config_path
        self.enabled_since = enabled_since
        self.sender = sender
        with self.store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS offer_notices (
                episode_id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL)''')

    def _eligible(self, decision):
        if decision.get('decision') != 'confirmed' or not decision.get('prospectus_public_date'):
            return False
        with self.store.connect() as db:
            live = db.execute('''SELECT e.payload FROM worker_classifications c JOIN events e USING(event_id)
                JOIN jobs j USING(event_id) WHERE c.episode_id=? AND c.event_type='ipo_offer'
                AND c.issuer_type='common_equity' AND e.baseline=0''', (decision['episode_id'],)).fetchall()
        return any(datetime.fromisoformat(json.loads(r['payload'])['discovered_at']) >= self.enabled_since for r in live)

    def _discover(self, today):
        attention = set()
        blocked = set()
        visited = set()
        paths = sorted(self.classifications.glob('*.json'), key=lambda p: p.stat().st_mtime_ns, reverse=True)
        for path in paths:
            episode = None
            try:
                decision = json.loads(path.read_text())
                published = decision.get('prospectus_public_date')
                if not published or not today - timedelta(days=7) <= date.fromisoformat(published) <= today:
                    continue
                if not self._eligible(decision):
                    continue
                episode = decision['episode_id']
                if episode in visited:
                    continue
                visited.add(episode)
                with self.store.connect() as db:
                    known = db.execute('SELECT payload,status FROM offer_notices WHERE episode_id=?', (episode,)).fetchone()
                if known and (known['status'] in {'confirmed','needs_reconciliation'} or json.loads(known['payload']).get('classification') == path.name):
                    continue
                opening, closing, links = official_timetable(decision)
                payload = {'opening_date': opening.isoformat(), 'code': decision['stock_code'],
                           'closing_date': closing.isoformat() if closing else None,
                           'issuer': decision['issuer'], 'sources': links, 'classification': path.name}
                with self.store.connect() as db:
                    db.execute('''INSERT INTO offer_notices VALUES(?,?,?,?,NULL)
                        ON CONFLICT(episode_id) DO UPDATE SET payload=excluded.payload,status='pending'
                        WHERE offer_notices.status IN ('pending','expired')''',
                        (episode, json.dumps(payload, ensure_ascii=False), 'pending', 0))
            except (OSError, ValueError, KeyError, RuntimeError, TypeError):
                attention.add('offer_timetable_not_verified')
                if episode:blocked.add(episode)
        return sorted(attention), blocked

    def _send(self, episode, payload, *, late):
        key = fingerprint(['offer-opening-notice-v1', episode])
        prior = self.store.delivery(key)
        if prior:
            return 'confirmed' if prior['status'] == 'confirmed' else 'needs_reconciliation'
        try:
            cfg = load_private_config(self.config_path).get('email', {})
            if not DEFAULT_RECIPIENT or not all(cfg.get(k) for k in ('smtp_host', 'smtp_port', 'user', 'password')):
                return 'retryable'
            identity = f"{payload['issuer']}（{payload['code']}）"
            msg = EmailMessage(policy=SMTP)
            mid = f'<hkipo-offer-{key}@local.invalid>'
            msg['From'] = cfg['user']
            msg['To'] = DEFAULT_RECIPIENT
            msg['Subject'] = f'【港股招股提醒{"补发" if late else ""}】{identity}'
            msg['Message-ID'] = mid
            sources = '\n'.join(payload['sources'])
            day = payload['opening_date'] if late else f"今天（{payload['opening_date']}，香港时间）"
            msg.set_content(f"{identity} 的公开招股开始日期为{day}。\n\n"
                            f"新股分析流程已进入处理队列。文章经独立审核后将另行发送。\n\n港交所正式文件\n{sources}\n")
        except (OSError, ValueError, RuntimeError, TypeError):
            return 'retryable'
        if not self.store.begin_delivery(key, episode, 'offer_opening_email'):
            return 'needs_reconciliation'
        try:
            receipt = self.sender(cfg, msg, DEFAULT_RECIPIENT)
            if receipt.get('smtp_accepted') is True and receipt.get('message_id') == mid:
                self.store.finish_delivery(key, 'confirmed', {'message_id': mid, 'smtp_accepted': True})
                return 'confirmed'
        except BeforeArticleDelivery:
            if self.store.release_unsent_delivery(key):
                return 'retryable'
        except Exception:
            pass
        return 'needs_reconciliation'

    def cycle(self, now=None):
        now = now or datetime.now(timezone.utc)
        today = now.astimezone(HONG_KONG).date()
        attention, blocked = self._discover(today)
        with self.store.connect() as db:
            pending = db.execute("SELECT * FROM offer_notices WHERE status='pending' AND (retry_at IS NULL OR retry_at<=?)",
                                 (now.timestamp(),)).fetchall()
        results = []
        for row in pending:
            episode = row['episode_id']
            if episode in blocked:
                continue
            payload = json.loads(row['payload'])
            opening = date.fromisoformat(payload['opening_date'])
            closing = date.fromisoformat(payload['closing_date']) if payload.get('closing_date') else None
            if opening > today:
                continue
            state = self._send(episode, payload, late=opening < today) if opening == today or (closing and today < closing) else 'expired'
            attempt = row['attempts'] + 1
            retry = now.timestamp() + (60, 300, 900, 1800)[min(attempt, 4)-1] if state == 'retryable' else None
            with self.store.connect() as db:
                db.execute('UPDATE offer_notices SET status=?,attempts=?,retry_at=? WHERE episode_id=?',
                           ('pending' if retry else state, attempt, retry, episode))
            results.append({'episode_id': episode, 'status': state, 'retry_at': retry})
        with self.store.connect() as db:
            if db.execute("SELECT 1 FROM offer_notices WHERE status='needs_reconciliation' LIMIT 1").fetchone():
                attention.append('offer_email_outcome_unconfirmed')
            if db.execute("SELECT 1 FROM offer_notices WHERE status='expired' LIMIT 1").fetchone():
                attention.append('offer_email_window_missed')
        return {'results': results, 'attention': attention}
