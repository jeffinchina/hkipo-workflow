"""Hong Kong IPO writing reminders. No transport, no broker-result monitoring."""
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
import hashlib
import json
import sqlite3

HK = ZoneInfo('Asia/Hong_Kong')
SLOTS = ('16:30', '17:30')


def aware(value=None):
    value = value or datetime.now(HK)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('timezone-aware datetime required')
    return value.astimezone(HK)


class CalendarUnavailable(ValueError):
    """Calendar year pending verification; never guess a business day."""


class HKBusinessCalendar:
    def __init__(self, directory=None):
        self.directory = Path(directory or Path(__file__).resolve().parents[1] / 'data/hk-calendar')

    def is_business_day(self, day):
        path = self.directory / f'{day.year}.json'
        if not path.exists():
            raise CalendarUnavailable(f'HK calendar {day.year} pending verification')
        data = json.loads(path.read_text())
        if data.get('verified') is not True:
            raise CalendarUnavailable(f'HK calendar {day.year} pending verification')
        return day.weekday() < 5 and day.isoformat() not in data['holidays']

    def next_business_day(self, day):
        for offset in range(1, 367):
            candidate = day + timedelta(days=offset)
            if self.is_business_day(candidate):
                return candidate
        raise CalendarUnavailable('No business day within bounded horizon')


def writing_event_id(issue_id):
    if not issue_id or not isinstance(issue_id, str):
        raise ValueError('stable issue_id required (stock code plus issuance period)')
    return 'allotment_writing:' + hashlib.sha256(issue_id.encode()).hexdigest()[:24]


class ReminderStore:
    def __init__(self, path, calendar=None):
        self.path = str(path)
        self.calendar = calendar or HKBusinessCalendar()
        with self._db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS writing_reminders (
              issue_id TEXT, slot TEXT, event_id TEXT, due_at TEXT, status TEXT,
              payload TEXT, message_id TEXT, correction_required INTEGER DEFAULT 0,
              PRIMARY KEY(issue_id,slot));
            CREATE TABLE IF NOT EXISTS writing_schedule_evidence (
              id INTEGER PRIMARY KEY, issue_id TEXT, observed_at TEXT, payload TEXT);
            ''')

    def _db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    def schedule(self, issue_id, official_close, *, source_hash, evidence,
                 stock_code='', stock_name='', override_date=None, override_evidence=None,
                 status='active', now=None):
        """Upsert latest official schedule. Delayed/withdrawn cancels unsent slots.

        official_close must come from the formal offer timetable, never broker cutoff.
        A replacement date needs its own evidence. Sent/unknown slots never requeue.
        """
        now = aware(now)
        event_id = writing_event_id(issue_id)
        if status not in ('active', 'delayed', 'withdrawn'):
            raise ValueError('invalid schedule status')
        if not source_hash or not evidence:
            raise ValueError('official timetable evidence and source_hash required')
        close = date.fromisoformat(official_close) if isinstance(official_close, str) else official_close
        if isinstance(close, datetime) or not isinstance(close, date):
            raise ValueError('official_close must be HK calendar date')
        expected = None
        if status == 'active':
            if override_date:
                if not override_evidence:
                    raise ValueError('override evidence required')
                expected = date.fromisoformat(override_date) if isinstance(override_date, str) else override_date
                if not self.calendar.is_business_day(expected):
                    raise ValueError('override must be verified HK business day')
            else:
                expected = self.calendar.next_business_day(close)
        payload = dict(issue_id=issue_id, event_id=event_id, event_type='allotment_writing',
                       stock_code=stock_code, stock_name=stock_name, official_close=close.isoformat(),
                       expected_date=expected.isoformat() if expected else None, source_hash=source_hash,
                       evidence=evidence, override_date=expected.isoformat() if override_date else None,
                       override_evidence=override_evidence, schedule_status=status,
                       notice='写作提醒/预计出签日，请查看券商通知后回复标题、完整正文及可选配图。')
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT INTO writing_schedule_evidence(issue_id,observed_at,payload) VALUES(?,?,?)',
                       (issue_id, now.isoformat(), json.dumps(payload, ensure_ascii=False)))
            for slot in SLOTS:
                due = datetime.combine(expected, time.fromisoformat(slot), HK).isoformat() if expected else None
                row = db.execute('SELECT * FROM writing_reminders WHERE issue_id=? AND slot=?', (issue_id,slot)).fetchone()
                data = json.dumps(dict(payload, slot=slot), ensure_ascii=False)
                if row and row['status'] in ('confirmed','unknown'):
                    changed = row['due_at'] != due or status != 'active'
                    db.execute('UPDATE writing_reminders SET correction_required=MAX(correction_required,?),payload=? WHERE issue_id=? AND slot=?', (int(changed),data,issue_id,slot))
                    continue
                state = 'pending' if due and (datetime.fromisoformat(due) > now or (row and row['status'] == 'pending' and row['due_at'] == due)) else ('skipped_history' if due else 'cancelled')
                db.execute('INSERT INTO writing_reminders(issue_id,slot,event_id,due_at,status,payload) VALUES(?,?,?,?,?,?) ON CONFLICT(issue_id,slot) DO UPDATE SET due_at=excluded.due_at,status=excluded.status,payload=excluded.payload', (issue_id,slot,event_id,due,state,data))
        return self.rows(issue_id)

    def rows(self, issue_id=None):
        with self._db() as db:
            rows = db.execute('SELECT * FROM writing_reminders' + (' WHERE issue_id=?' if issue_id else '') + ' ORDER BY due_at,slot', (issue_id,) if issue_id else ()).fetchall()
            return [dict(row, payload=json.loads(row['payload'])) for row in rows]

    def due_reservations(self, now=None, limit=20):
        """Atomically mark due slots unknown BEFORE caller attempts delivery."""
        now = aware(now)
        if not 1 <= limit <= 100:
            raise ValueError('limit must be 1..100')
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute("SELECT * FROM writing_reminders WHERE status='pending' AND due_at<=? ORDER BY due_at,slot LIMIT ?", (now.isoformat(),limit)).fetchall()
            for row in rows:
                db.execute("UPDATE writing_reminders SET status='unknown' WHERE issue_id=? AND slot=?", (row['issue_id'],row['slot']))
            return [dict(row, status='unknown', payload=json.loads(row['payload'])) for row in rows]

    def finish_confirmed(self, issue_id, slot, message_id):
        if not message_id:
            raise ValueError('message_id required')
        with self._db() as db:
            row = db.execute('SELECT * FROM writing_reminders WHERE issue_id=? AND slot=?', (issue_id,slot)).fetchone()
            if not row or row['status'] not in ('unknown','confirmed'):
                raise ValueError('slot has no delivery reservation')
            if row['status'] == 'confirmed' and row['message_id'] != message_id:
                raise ValueError('conflicting delivery receipt')
            db.execute("UPDATE writing_reminders SET status='confirmed',message_id=? WHERE issue_id=? AND slot=?", (message_id,issue_id,slot))
