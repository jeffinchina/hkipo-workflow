"""Text-only notice transport over the existing mobile/reply ledger.

Callers establish current IPO eligibility/baseline before make_official_event.
No article generation, PDF review, scheduling inference or automatic retries.
"""
from email.message import EmailMessage
from email.policy import SMTP
import json
import re
from .delivery import DEFAULT_RECIPIENT, DeliveryError, fingerprint
from .models import canonical_url

KINDS = {'allotment_writing', 'allotment_notice', 'exception_notice'}


def make_official_event(doc_event, episode_id=None, *, source_hash):
    d = doc_event.to_dict() if hasattr(doc_event, 'to_dict') else dict(doc_event)
    kind = {'allotment': 'allotment_notice', 'exception': 'exception_notice'}.get(d.get('kind'))
    if not kind or not source_hash:
        raise ValueError('official notice kind and source_hash required')
    url = canonical_url(d['url'])
    title = d.get('title', '')
    # Generic placings are not evidence of a new IPO.
    ipo_title = re.search(r'全球[發发]售|香港公[開开][發发]售|首次公[開开][發发]售|global offering|hong kong public offering|initial public offering', title, re.I)
    evidence = d.get('official_role_evidence') or {}
    role_ok = (isinstance(evidence, dict)
               and evidence.get('category') == 'official_new_listing_table'
               and evidence.get('role') in ('allotment', 'exception')
               and evidence.get('url') == url
               and evidence.get('source_sha256')
               and evidence.get('locator'))
    if role_ok:
        role_ok = bool(re.fullmatch(r'https://www2\.hkexnews\.hk/New-Listings/New-Listing-Information/(Main-Board|GEM)\?sc_lang=(zh-HK|en)', evidence.get('source_url', '')))
    if not ipo_title and not role_ok:
        raise ValueError('official IPO association required')
    code = str(d.get('stock_code', ''))
    if not re.fullmatch(r'\d{1,5}', code) or not d.get('stock_name') or not title or not d.get('published_at'):
        raise ValueError('official notice identity incomplete')
    eid = kind + ':' + fingerprint([url, title, d['published_at'], kind])
    return dict(event_id=eid, notification_kind=kind, stock_code=code.zfill(5),
                issuer=d['stock_name'], episode=episode_id or eid, source_url=url,
                source_time=d['published_at'], first_seen=d.get('discovered_at') or d['published_at'],
                brief='官方公告已发布：' + title, images=[], source_hash=source_hash)


class NoticeBridge:
    def __init__(self, flow, reminders=None):
        self.flow = flow
        self.reminders = reminders

    @staticmethod
    def delivery_key(event_id, notice_id):
        if not notice_id:
            raise ValueError('notice_id required')
        return fingerprint(['notice-v1', event_id, notice_id])

    def send_notice(self, event, notice_id, subject, text):
        if event.get('notification_kind') not in KINDS:
            raise ValueError('notification_kind required')
        self.flow.register_event(event)
        cfg = self.flow.config.get('email', {})
        if cfg.get('user', '').casefold() not in self.flow.replies.allowed:
            raise DeliveryError('reply_mailbox_must_be_author_account')
        if not all(cfg.get(k) for k in ('smtp_host', 'smtp_port', 'user', 'password')):
            raise DeliveryError('email_configuration_incomplete')
        key = self.delivery_key(event['event_id'], notice_id)
        mid = '<hkipo-notice-' + key + '@local.invalid>'
        with self.flow.replies.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            prior = c.execute('SELECT status FROM mobile_alerts WHERE delivery_key=?', (key,)).fetchone()
            if prior:
                if prior[0] == 'confirmed':
                    c.execute('INSERT OR IGNORE INTO reply_reminders VALUES(?,?)', (mid, event['event_id']))
                return dict(status='already_confirmed' if prior[0] == 'confirmed' else 'needs_reconciliation', delivery_key=key, message_id=mid)
            c.execute('INSERT INTO mobile_alerts VALUES(?,?,?,?,NULL)', (key, event['event_id'], mid, 'unknown'))
        msg = EmailMessage(policy=SMTP)
        msg['From'] = cfg['user']; msg['To'] = DEFAULT_RECIPIENT; msg['Reply-To'] = cfg['user']
        msg['Subject'] = subject; msg['Message-ID'] = mid
        msg.set_content(text + '\n\n' + event['issuer'] + '（' + event['stock_code'] + '）\n发行期：' + event['episode'] + '\n来源：' + event['source_url'] + '\n来源时间：' + event['source_time'] +
                        '\n\n请回复标题、完整正文及可选图片，仅生成本人公众号草稿。\n标题：填写标题\n正文：\n填写完整正文\n【正文结束】\n')
        self.flow.replies.remember_reminder(msg.as_bytes(), event['event_id'])
        try:
            receipt = self.flow.smtp_sender(cfg, msg, DEFAULT_RECIPIENT)
            if receipt.get('smtp_accepted') is not True or receipt.get('message_id') != mid:
                raise DeliveryError('invalid_notice_receipt')
            with self.flow.replies.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                c.execute("UPDATE mobile_alerts SET status='confirmed',receipt=? WHERE delivery_key=?", (json.dumps(dict(smtp_accepted=True, message_id=mid)), key))
                c.execute('INSERT OR IGNORE INTO reply_reminders VALUES(?,?)', (mid, event['event_id']))
            return dict(status='confirmed', delivery_key=key, message_id=mid)
        except Exception:
            return dict(status='needs_reconciliation', delivery_key=key, message_id=mid)

    @staticmethod
    def _writing_event(row):
        p = row['payload']
        evidence = p.get('evidence')
        if not isinstance(evidence, dict) or not (evidence.get('source_url') or evidence.get('quote_sha256')) or not p.get('source_hash'):
            raise ValueError('official timetable source_url or quote_sha256 required')
        source = evidence.get('source_url')
        if source:
            source = canonical_url(source)
        else:
            source = 'sha256:' + str(evidence['quote_sha256'])
        return dict(event_id=row['event_id'], notification_kind='allotment_writing',
                    stock_code=p['stock_code'], issuer=p['stock_name'], episode=p['issue_id'],
                    source_url=source, source_time=p['official_close'], first_seen=p['official_close'],
                    brief='写作提醒/预计出签日，请查看券商通知后回复。', images=[],
                    official_close=p['official_close'], expected_date=p['expected_date'], source_hash=p['source_hash'])

    def send_due(self, now=None):
        if self.reminders is None:
            raise ValueError('ReminderStore required')
        results = []
        # Repair cross-database crash boundary without another SMTP attempt.
        for row in self.reminders.rows():
            if row['status'] != 'unknown':
                continue
            key = self.delivery_key(row['event_id'], row['issue_id'] + ':' + row['slot'])
            with self.flow.replies.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                receipt = c.execute("SELECT message_id FROM mobile_alerts WHERE delivery_key=? AND status='confirmed'", (key,)).fetchone()
                if receipt:
                    c.execute('INSERT OR IGNORE INTO reply_reminders VALUES(?,?)', (receipt[0], row['event_id']))
            if receipt:
                self.reminders.finish_confirmed(row['issue_id'], row['slot'], receipt[0])
                results.append(dict(status='reconciled', message_id=receipt[0]))
        for row in self.reminders.due_reservations(now):
            event = self._writing_event(row)
            with self.flow.replies.connect() as c:
                existing = c.execute('SELECT payload FROM mobile_events WHERE event_id=?', (row['event_id'],)).fetchone()
            if existing:
                event = json.loads(existing[0])
            text = (event['brief'] + '\n正式截止认购日期：' + row['payload']['official_close'] +
                    '\n预计出签日：' + row['payload']['expected_date'] + '\n提醒槽位：' + row['slot'] +
                    '\n此时刻并非官方保证出签时间；尚未收到券商通知可稍后回复。')
            result = self.send_notice(event, row['issue_id'] + ':' + row['slot'], '【出签写作提醒】' + event['issuer'], text)
            if result['status'] in ('confirmed', 'already_confirmed'):
                self.reminders.finish_confirmed(row['issue_id'], row['slot'], result['message_id'])
            results.append(result)
        return results
