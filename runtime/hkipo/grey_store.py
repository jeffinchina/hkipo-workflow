"""Durable post-close jobs; fixed 18:40 HK reminder, independent of allotment.

A real broker session has its own identity. A later postponed listing must not
rewrite an already traded session. No historical backfill and no uncertain resend.
"""
from datetime import datetime, time, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
from .grey_market import HK, GreyDataError, timestamp, official_url, validate_snapshot, validate_quote


def identity(session):
    raw=[session['provider'],session['stock_code'],session['trade_date'],session['session_start'],session['session_end']]
    return hashlib.sha256(json.dumps(raw,separators=(',',':')).encode()).hexdigest()


class GreyStore:
    def __init__(self,path):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        with self.connect() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS grey_sessions(
              session_id TEXT PRIMARY KEY,trade_date TEXT NOT NULL,due_at TEXT NOT NULL,
              session_end TEXT NOT NULL,first_seen TEXT NOT NULL,payload TEXT NOT NULL,
              data_status TEXT NOT NULL DEFAULT 'pending',snapshot TEXT,attempts INTEGER NOT NULL DEFAULT 0,
              retry_at TEXT,reason TEXT,delivery_status TEXT NOT NULL DEFAULT 'pending',
              event_id TEXT,message_id TEXT,allotment TEXT,updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS grey_reports(
              report_id TEXT PRIMARY KEY,event_id TEXT NOT NULL,status TEXT NOT NULL,message_id TEXT);
            CREATE TABLE IF NOT EXISTS grey_source_refresh(
              source TEXT PRIMARY KEY,last_attempt TEXT NOT NULL,last_success TEXT,reason TEXT);
            CREATE TABLE IF NOT EXISTS grey_allotment_material(
              source_event_id TEXT PRIMARY KEY,stock_code TEXT NOT NULL,payload TEXT NOT NULL,updated_at TEXT NOT NULL);
            ''')

    def connect(self):
        c=sqlite3.connect(str(self.path),timeout=15);c.row_factory=sqlite3.Row;return c

    def register(self,session,now=None):
        now=timestamp(now.isoformat()) if now else datetime.now(HK)
        d=timestamp(session['session_end']).date()
        if session.get('market')!='grey' or not session.get('provider'):raise GreyDataError('session_identity')
        if d.isoformat()!=session['trade_date'] or timestamp(session['session_start']).date()!=d:raise GreyDataError('session_dates')
        if not timestamp(session['session_start'])<timestamp(session['session_end']):raise GreyDataError('session_time_order')
        official_url(session['schedule_source_url'])
        sid=identity(session);due=datetime.combine(d,time(18,40),HK)
        # Only current/future sessions are scheduled. A process restart preserves
        # a previously pending session, including a reminder that became due.
        state='pending' if due>=now else 'skipped_history'
        raw=json.dumps(session,ensure_ascii=False,sort_keys=True)
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            previous=c.execute('SELECT * FROM grey_sessions WHERE session_id=?',(sid,)).fetchone()
            if previous:
                # Replayed known rows can refresh metadata, never supersede a
                # newer identity. Frozen e-mails live in the notice ledger.
                if previous['delivery_status']!='superseded':
                    c.execute('UPDATE grey_sessions SET payload=?,updated_at=? WHERE session_id=?',(raw,now.isoformat(),sid))
                return sid
            # Only a positively new future arrangement (or a same-day time
            # correction) can replace an unsent plan. Past/known rows cannot
            # cancel a later session after a previously traded day.
            if state=='pending':
                for old in c.execute("SELECT * FROM grey_sessions WHERE delivery_status='pending' AND session_id<>?",(sid,)).fetchall():
                    prior=json.loads(old['payload'])
                    if (prior.get('provider')==session['provider'] and prior.get('stock_code')==session['stock_code']
                            and ((now<timestamp(session['session_start']) and now<timestamp(prior['session_start']))
                                 or prior['trade_date']==session['trade_date'])):
                        c.execute("UPDATE grey_sessions SET delivery_status='superseded',reason='positive_official_schedule_revision',updated_at=? WHERE session_id=?",(now.isoformat(),old['session_id']))
            c.execute('INSERT INTO grey_sessions(session_id,trade_date,due_at,session_end,first_seen,payload,delivery_status,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                      (sid,d.isoformat(),due.isoformat(),session['session_end'],now.isoformat(),raw,state,now.isoformat()))
        return sid

    def rows(self):
        with self.connect() as c:rows=c.execute('SELECT * FROM grey_sessions ORDER BY due_at,session_id').fetchall()
        return [dict(r,payload=json.loads(r['payload']),snapshot=json.loads(r['snapshot']) if r['snapshot'] else None,
                     allotment=json.loads(r['allotment']) if r['allotment'] else None) for r in rows]

    def collection_due(self,now=None,limit=3):
        now=timestamp(now.isoformat()) if now else datetime.now(HK)
        rows=[]
        for row in self.rows():
            cutoff=timestamp(row['due_at'])-timedelta(minutes=1)
            if (row['delivery_status']=='pending' and row['data_status']!='verified' and row['attempts']<3
                    and timestamp(row['session_end'])+timedelta(seconds=30)<=now<cutoff
                    and (not row['retry_at'] or timestamp(row['retry_at'])<=now)):
                rows.append(row)
        return rows[:limit]

    def record_collection(self,sid,*,snapshot=None,reason=None,now=None):
        now=timestamp(now.isoformat()) if now else datetime.now(HK)
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE');row=c.execute('SELECT * FROM grey_sessions WHERE session_id=?',(sid,)).fetchone()
            if not row or row['delivery_status']!='pending':return
            if snapshot is not None:
                partial=snapshot.get('status')=='quote_only'
                (validate_quote if partial else validate_snapshot)(snapshot,now)
                expected=json.loads(row['payload'])
                for key in ('stock_code','provider','trade_date','session_start','session_end'):
                    if snapshot.get(key)!=expected.get(key):raise GreyDataError('snapshot_session_mismatch')
                state='quote_verified' if partial else 'verified';data=json.dumps(snapshot,ensure_ascii=False,sort_keys=True)
            elif row['data_status']=='quote_verified' and row['snapshot']:
                state='quote_verified';data=row['snapshot']
            else:state='needs_source';data=None
            c.execute('UPDATE grey_sessions SET data_status=?,snapshot=?,attempts=attempts+1,retry_at=?,reason=?,updated_at=? WHERE session_id=?',
                      (state,data,(now+timedelta(minutes=2)).isoformat(),reason,now.isoformat(),sid))

    def retain_allotment(self,source_event_id,stock_code,payload,now=None):
        now=timestamp(now.isoformat()) if now else datetime.now(HK)
        raw=json.dumps(payload,ensure_ascii=False,sort_keys=True)
        with self.connect() as c:
            c.execute('INSERT INTO grey_allotment_material VALUES(?,?,?,?) ON CONFLICT(source_event_id) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at',
                      (source_event_id,stock_code,raw,now.isoformat()))

    @staticmethod
    def material_sha(value):
        return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode()).hexdigest()

    def get(self,sid):
        return next((r for r in self.rows() if r['session_id']==sid),None)

    def attach_allotment(self,sid,material,expected_sha=None):
        if material.get('status') not in {'not_published','publication_unverified','published_pending_review','verified'}:raise GreyDataError('allotment_status')
        if material['status']=='verified' and not material.get('summary'):raise GreyDataError('allotment_summary_required')
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT allotment FROM grey_sessions WHERE session_id=?',(sid,)).fetchone()
            if not row:raise GreyDataError('unknown_session')
            current=json.loads(row[0]) if row[0] else None
            if expected_sha is not None and self.material_sha(current)!=expected_sha:return current or material
            ranks={'not_published':0,'publication_unverified':1,'published_pending_review':2,'verified':3}
            if expected_sha is None and current and ranks[current['status']]>ranks[material['status']]:return current
            c.execute('UPDATE grey_sessions SET allotment=? WHERE session_id=?',(json.dumps(material,ensure_ascii=False,sort_keys=True),sid))
        return material

    def due(self,now=None):
        now=timestamp(now.isoformat()) if now else datetime.now(HK)
        return [r for r in self.rows() if r['delivery_status']=='pending' and timestamp(r['due_at'])<=now]

    def reserve(self,sid,event_id):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            return c.execute("UPDATE grey_sessions SET delivery_status='unknown',event_id=? WHERE session_id=? AND delivery_status='pending'",(event_id,sid)).rowcount==1

    def finish(self,sid,message_id):
        with self.connect() as c:
            c.execute("UPDATE grey_sessions SET delivery_status='confirmed',message_id=? WHERE session_id=? AND delivery_status='unknown'",(message_id,sid))

    def reserve_group(self,ids,event_id,report_id):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            prior=c.execute('SELECT * FROM grey_reports WHERE report_id=?',(report_id,)).fetchone()
            if prior:
                if prior['status']=='confirmed':
                    for sid in ids:c.execute("UPDATE grey_sessions SET delivery_status='confirmed',event_id=?,message_id=? WHERE session_id=? AND delivery_status='pending'",(event_id,prior['message_id'],sid))
                return False
            for sid in ids:
                row=c.execute('SELECT delivery_status FROM grey_sessions WHERE session_id=?',(sid,)).fetchone()
                if not row or row[0]!='pending':return False
            c.execute("INSERT INTO grey_reports VALUES(?,?,'unknown',NULL)",(report_id,event_id))
            for sid in ids:c.execute("UPDATE grey_sessions SET delivery_status='unknown',event_id=? WHERE session_id=?",(event_id,sid))
            return True

    def finish_group(self,report_id,message_id):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT event_id FROM grey_reports WHERE report_id=?',(report_id,)).fetchone()
            if not row:raise GreyDataError('unknown_report_group')
            c.execute("UPDATE grey_reports SET status='confirmed',message_id=? WHERE report_id=?",(message_id,report_id))
            c.execute("UPDATE grey_sessions SET delivery_status='confirmed',message_id=? WHERE event_id=? AND delivery_status='unknown'",(message_id,row[0]))
