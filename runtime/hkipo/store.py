"""SQLite transaction boundary for intake, watermarks, jobs and delivery receipts."""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from .models import utcnow

class StateStore:
    def __init__(self, dbpath):
        self.dbpath = str(dbpath)
        Path(self.dbpath).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(event_id TEXT PRIMARY KEY,payload TEXT NOT NULL,baseline INTEGER NOT NULL,created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs(event_id TEXT PRIMARY KEY REFERENCES events(event_id),status TEXT NOT NULL DEFAULT 'pending',updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sources(source TEXT PRIMARY KEY,last_success TEXT,cutoff TEXT,last_failure TEXT,failure_count INTEGER NOT NULL DEFAULT 0,error TEXT,retry_not_before TEXT);
            CREATE TABLE IF NOT EXISTS deliveries(delivery_key TEXT PRIMARY KEY,event_id TEXT NOT NULL,channel TEXT NOT NULL,status TEXT NOT NULL,receipt TEXT,updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS alerts(source TEXT PRIMARY KEY,opened_at TEXT NOT NULL,error TEXT NOT NULL,resolved_at TEXT);
            ''')
            if 'retry_not_before' not in {r[1] for r in c.execute('PRAGMA table_info(sources)')}:
                c.execute('ALTER TABLE sources ADD COLUMN retry_not_before TEXT')

    def connect(self):
        c = sqlite3.connect(self.dbpath, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA foreign_keys=ON')
        c.execute('PRAGMA busy_timeout=30000')
        return c

    def _ingest(self, c, events, baseline, promote_candidates=False):
        added = []
        for event in events:
            payload=json.dumps(event.to_dict(), ensure_ascii=False)
            cur = c.execute('INSERT OR IGNORE INTO events VALUES(?,?,?,?)', (event.event_id,payload,int(baseline),utcnow()))
            inserted=bool(cur.rowcount)
            promoted=False
            if not inserted and promote_candidates and not baseline:
                promoted=bool(c.execute('UPDATE events SET baseline=0,payload=? WHERE event_id=? AND baseline=2',(payload,event.event_id)).rowcount)
            if inserted or promoted:
                added.append(event)
                if not baseline and event.kind in {'offer','allotment','exception'}:
                    c.execute('INSERT OR IGNORE INTO jobs(event_id,updated_at) VALUES(?,?)', (event.event_id, utcnow()))
        return added

    def ingest(self, events, baseline=False):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            return self._ingest(c, events, baseline)

    def ensure_monitor_start(self, when):
        if when.tzinfo is None:raise ValueError('Timezone-aware start required')
        with self.connect() as c:
            c.execute('INSERT OR IGNORE INTO metadata VALUES(?,?)',('monitor_started',when.astimezone(timezone.utc).isoformat()))

    def pending_classification(self):
        with self.connect() as c:
            return [json.loads(r['payload']) for r in c.execute('SELECT payload FROM events WHERE baseline=2')]

    def commit_scan(self, source, events, cutoff, baseline=False, timestamp_precision=None):
        parsed = datetime.fromisoformat(cutoff)
        if parsed.tzinfo is None: raise ValueError("Timezone-aware cutoff required")
        parsed=parsed.astimezone(timezone.utc);cutoff=parsed.isoformat()
        precision=timestamp_precision or ('date' if source.startswith('new-listings-') or source=='aggregate' else 'minute')
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('INSERT OR IGNORE INTO metadata VALUES(?,?)',('monitor_started',cutoff))
            anchor=datetime.fromisoformat(c.execute('SELECT value FROM metadata WHERE key=?',('monitor_started',)).fetchone()['value'])
            prior=c.execute('SELECT last_success FROM sources WHERE source=?',(source,)).fetchone()
            first_source=prior is None or not prior['last_success']
            initial_scan=parsed<=anchor
            added=[]
            for event in events:
                level=0
                if baseline or initial_scan:level=1
                elif first_source:
                    try:
                        published=datetime.fromisoformat(event.published_at)
                        if published.tzinfo is None:raise ValueError()
                        if precision=='date':
                            # Midnight on a document-path date is not a release
                            # time. Same-day late-recovery records wait for an
                            # exact source rather than being silently discarded.
                            level=1 if published.astimezone(ZoneInfo('Asia/Hong_Kong')).date()<anchor.astimezone(ZoneInfo('Asia/Hong_Kong')).date() else 2
                        else:level=0 if published>=anchor else 1
                    except ValueError:level=2
                added.extend(self._ingest(c,[event],level,promote_candidates=(precision!='date')))
            c.execute('''INSERT INTO sources(source,last_success,cutoff) VALUES(?,?,?)
              ON CONFLICT(source) DO UPDATE SET last_success=excluded.last_success,
              cutoff=CASE WHEN sources.cutoff IS NULL OR excluded.cutoff>sources.cutoff THEN excluded.cutoff ELSE sources.cutoff END,
              failure_count=0,error=NULL,retry_not_before=NULL''', (source, utcnow(), cutoff))
            c.execute('UPDATE alerts SET resolved_at=? WHERE source=? AND resolved_at IS NULL',(utcnow(),source))
            return added, bool(baseline or initial_scan)

    def source_state(self, source):
        with self.connect() as c:
            row=c.execute('SELECT * FROM sources WHERE source=?',(source,)).fetchone()
            return dict(row) if row else None

    def record_failure(self, source, error, retry_not_before=None):
        # Error strings are supplied by our sanitized exception boundary, never
        # raw HTTP headers or credential-bearing URLs.
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('''INSERT INTO sources(source,last_failure,failure_count,error) VALUES(?,?,1,?)
              ON CONFLICT(source) DO UPDATE SET last_failure=excluded.last_failure,
              failure_count=sources.failure_count+1,error=excluded.error''',(source,utcnow(),error))
            c.execute('UPDATE sources SET retry_not_before=? WHERE source=?',(retry_not_before,source))
            c.execute('''INSERT INTO alerts(source,opened_at,error) VALUES(?,?,?)
              ON CONFLICT(source) DO UPDATE SET opened_at=CASE WHEN alerts.resolved_at IS NOT NULL THEN excluded.opened_at ELSE alerts.opened_at END,error=excluded.error,resolved_at=NULL''',(source,utcnow(),error))

    def jobs(self, status=None):
        with self.connect() as c:
            rows=c.execute('SELECT * FROM jobs'+(' WHERE status=?' if status else ''), (status,) if status else ()).fetchall()
            return [dict(r) for r in rows]

    def set_job_status(self,event_id,status):
        if status not in {'pending','running','blocked','complete','failed'}:
            raise ValueError('Unknown job status')
        with self.connect() as c:
            c.execute('UPDATE jobs SET status=?,updated_at=? WHERE event_id=?',(status,utcnow(),event_id))

    def begin_delivery(self,delivery_key,event_id,channel):
        """Reserve unknown before an external side effect. Existing unknown must
        be reconciled, not retried automatically. No sender is implemented here."""
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            cur=c.execute('INSERT OR IGNORE INTO deliveries VALUES(?,?,?,\'unknown\',NULL,?)',(delivery_key,event_id,channel,utcnow()))
            return bool(cur.rowcount)

    def finish_delivery(self,delivery_key,status,receipt=None):
        if status not in {'unknown','confirmed','failed'}:
            raise ValueError('Unknown delivery status')
        with self.connect() as c:
            c.execute('UPDATE deliveries SET status=?,receipt=?,updated_at=? WHERE delivery_key=?',(status,json.dumps(receipt) if receipt is not None else None,utcnow(),delivery_key))

    def delivery(self,key):
        with self.connect() as c:
            r=c.execute('SELECT * FROM deliveries WHERE delivery_key=?',(key,)).fetchone()
            return dict(r) if r else None

    def alerts(self):
        with self.connect() as c:
            return [dict(r) for r in c.execute('SELECT * FROM alerts WHERE resolved_at IS NULL')]

    def _worker_schema(self,c):
        # Migration is serialized by the caller's BEGIN IMMEDIATE.
        columns={r[1] for r in c.execute('PRAGMA table_info(jobs)')}
        for name,sqltype in [('claim_token','TEXT'),('worker_id','TEXT'),('lease_until','TEXT'),('attempts','INTEGER NOT NULL DEFAULT 0'),('reason','TEXT'),('retry_after','TEXT')]:
            if name not in columns:c.execute('ALTER TABLE jobs ADD COLUMN '+name+' '+sqltype)
        c.execute('CREATE TABLE IF NOT EXISTS worker_classifications(event_id TEXT PRIMARY KEY REFERENCES events(event_id),issuer_type TEXT NOT NULL,event_type TEXT NOT NULL,document_role TEXT NOT NULL,episode_id TEXT NOT NULL,confirmed_at TEXT NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS worker_stages(event_id TEXT NOT NULL,stage TEXT NOT NULL,at TEXT NOT NULL,details TEXT NOT NULL,PRIMARY KEY(event_id,stage,at))')
        c.execute('CREATE TABLE IF NOT EXISTS research_runs(research_id TEXT PRIMARY KEY,payload TEXT NOT NULL,status TEXT NOT NULL,updated_at TEXT NOT NULL)')

    def confirm_classification(self,event_id,issuer_type,event_type,document_role,episode_id,promote_candidate=False):
        if issuer_type not in {'common_equity','etf','reit','other'} or event_type not in {'ipo_offer','ipo_allotment','ipo_exception','refinancing','preliminary','other'} or document_role not in {'announcement','prospectus','allotment','exception','other'} or not episode_id:
            raise ValueError('Invalid explicit classification')
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE');self._worker_schema(c)
            c.execute('INSERT INTO worker_classifications VALUES(?,?,?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET issuer_type=excluded.issuer_type,event_type=excluded.event_type,document_role=excluded.document_role,episode_id=excluded.episode_id,confirmed_at=excluded.confirmed_at',(event_id,issuer_type,event_type,document_role,episode_id,utcnow()))
            c.execute("UPDATE jobs SET status='pending',reason=NULL,retry_after=NULL WHERE event_id=? AND status IN ('blocked','needs_classification','waiting_sources')",(event_id,))
            if promote_candidate:
                event_row=c.execute('SELECT baseline,payload FROM events WHERE event_id=?',(event_id,)).fetchone()
                if event_row and event_row['baseline']==2:
                    payload=json.loads(event_row['payload'])
                    if issuer_type!='common_equity' or event_type!={'offer':'ipo_offer','allotment':'ipo_allotment','exception':'ipo_exception'}.get(payload['kind']):
                        raise ValueError('Candidate not confirmed ordinary IPO')
                    c.execute('UPDATE events SET baseline=0 WHERE event_id=?',(event_id,))
                    c.execute("INSERT OR IGNORE INTO jobs(event_id,updated_at) VALUES(?,?)",(event_id,utcnow()))

    def claim_job_group(self,worker_id,lease_seconds=1800,max_attempts=3,now=None):
        """Claim one stock/kind/language group under one write lock. Fence all
        completions by an unguessable claim token. Expired groups retry finitely."""
        import uuid
        from datetime import timedelta
        if (now is not None and now.tzinfo is None) or lease_seconds<1 or max_attempts<1:raise ValueError('Invalid lease parameters')
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE');self._worker_schema(c)
            # Lock waits must not consume a new lease or delay expiry recovery.
            current=now if now is not None else datetime.now(timezone.utc)
            stamp=current.astimezone(timezone.utc).isoformat();until=(current+timedelta(seconds=lease_seconds)).astimezone(timezone.utc).isoformat()
            c.execute("UPDATE jobs SET status=CASE WHEN attempts>=? THEN 'blocked' ELSE 'pending' END,reason='lease_expired',claim_token=NULL,worker_id=NULL WHERE status='running' AND lease_until<=?",(max_attempts,stamp))
            rows=c.execute("SELECT j.*,e.payload FROM jobs j JOIN events e USING(event_id) WHERE j.status IN ('pending','failed') AND j.attempts<? AND (j.retry_after IS NULL OR j.retry_after<=?) ORDER BY j.updated_at,j.event_id",(max_attempts,stamp)).fetchall()
            classes={r['event_id']:dict(r) for r in c.execute('SELECT * FROM worker_classifications')}
            def groupkey(e):return (e['stock_code'],e['kind'],e['language'],classes.get(e['event_id'],{}).get('episode_id',e['event_id']))
            running=c.execute("SELECT e.payload FROM jobs j JOIN events e USING(event_id) WHERE j.status='running' OR (j.status='pending' AND j.reason IN ('quota_reserve','quota_window_exhausted') AND j.retry_after>?)",(stamp,)).fetchall()
            busy={groupkey(json.loads(r['payload'])) for r in running}
            selected=None
            for row in rows:
                e=json.loads(row['payload']);key=groupkey(e)
                if key not in busy:selected=(key,row);break
            if selected is None:return None
            key,_=selected;group=[]
            waiting=c.execute("SELECT j.*,e.payload FROM jobs j JOIN events e USING(event_id) WHERE j.status='waiting_sources'").fetchall()
            for row in list(rows)+list(waiting):
                e=json.loads(row['payload'])
                if groupkey(e)==key:group.append(dict(row))
            token=uuid.uuid4().hex
            for row in group:
                c.execute("UPDATE jobs SET status='running',claim_token=?,worker_id=?,lease_until=?,attempts=attempts+1,updated_at=? WHERE event_id=?",(token,worker_id,until,stamp,row['event_id']))
            all_events=[json.loads(r['payload']) for r in c.execute('SELECT payload FROM events') if (lambda e:(e['stock_code'],e['kind'],e['language'])==key[:3])(json.loads(r['payload']))]
            classes={r['event_id']:dict(r) for r in c.execute('SELECT * FROM worker_classifications')}
            return {'token':token,'event_ids':[r['event_id'] for r in group],'events':all_events,'classifications':classes,'lease_until':until}

    def finish_claim(self,claim_token,status,reason=None,retry_seconds=0,now=None):
        from datetime import timedelta
        if status not in {'pending','failed','blocked','needs_classification','waiting_sources','ready_for_delivery','complete'}:raise ValueError('Invalid worker status')
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE');self._worker_schema(c)
            current=now if now is not None else datetime.now(timezone.utc)
            stamp=current.astimezone(timezone.utc).isoformat()
            retry=(current+timedelta(seconds=retry_seconds)).astimezone(timezone.utc).isoformat() if retry_seconds else None
            return c.execute("UPDATE jobs SET status=?,reason=?,retry_after=?,claim_token=NULL,worker_id=NULL,lease_until=NULL,updated_at=? WHERE claim_token=? AND status='running' AND lease_until>?",(status,reason,retry,stamp,claim_token,stamp)).rowcount

    def renew_claim(self,token,lease_seconds=1800):
        from datetime import timedelta
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE');self._worker_schema(c)
            now=datetime.now(timezone.utc)
            return bool(c.execute("UPDATE jobs SET lease_until=? WHERE claim_token=? AND status='running' AND lease_until>?",((now+timedelta(seconds=lease_seconds)).isoformat(),token,now.isoformat())).rowcount)

    def record_stage(self,event_ids,stage,details=None):
        if stage not in {'first_seen','downloaded','written','blocked'}:raise ValueError('Unknown pipeline stage')
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE');self._worker_schema(c)
            for eid in event_ids:
                c.execute('INSERT OR IGNORE INTO worker_stages VALUES(?,?,?,?)',(eid,stage,utcnow(),json.dumps(details or {},ensure_ascii=False)))

    def research_run(self,research_id,payload=None,status=None):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE');self._worker_schema(c)
            if payload is not None:
                c.execute('INSERT INTO research_runs VALUES(?,?,?,?) ON CONFLICT(research_id) DO UPDATE SET payload=excluded.payload,status=excluded.status,updated_at=excluded.updated_at',(research_id,json.dumps(payload,ensure_ascii=False),status,utcnow()))
            r=c.execute('SELECT * FROM research_runs WHERE research_id=?',(research_id,)).fetchone()
            return dict(r) if r else None

    def finish_research_claim(self,token,research_id,payload,status,reason=None):
        """Publish local research and release its job lease atomically."""
        if status not in {'ready_for_delivery','blocked','material_only','revision_pending'}:raise ValueError('Invalid research outcome')
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE');self._worker_schema(c)
            stamp=utcnow()
            from datetime import timedelta
            continuing=status=='revision_pending'
            job_status='pending' if continuing else ('complete' if status=='material_only' else status)
            retry=(datetime.fromisoformat(stamp)+timedelta(seconds=60)).isoformat() if continuing else None
            count=c.execute("UPDATE jobs SET status=?,reason=?,claim_token=NULL,worker_id=NULL,lease_until=NULL,retry_after=?,attempts=MAX(0,attempts-?),updated_at=? WHERE claim_token=? AND status='running' AND lease_until>?",(job_status,reason,retry,int(continuing),stamp,token,stamp)).rowcount
            if not count:return 0
            c.execute('INSERT INTO research_runs VALUES(?,?,?,?) ON CONFLICT(research_id) DO UPDATE SET payload=excluded.payload,status=excluded.status,updated_at=excluded.updated_at',(research_id,json.dumps(payload,ensure_ascii=False),status,stamp))
            return count

    def defer_research_claim(self,token,research_id,payload,deferred,now=None):
        """Fence timed quota deferral and restore only this claim's attempt."""
        from .quota import quota_deferral
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE');self._worker_schema(c)
            # Lock acquisition may outlast the lease or observation freshness.
            # Explicit test clocks remain deterministic; real clocks sample only
            # after obtaining the write lock.
            current=now if now is not None else datetime.now(timezone.utc)
            verified=quota_deferral(deferred.get('observation'),current.timestamp())
            if not verified:return 0
            stamp=current.astimezone(timezone.utc).isoformat()
            retry=datetime.fromtimestamp(verified['retry_at'],timezone.utc).isoformat()
            count=c.execute("UPDATE jobs SET status='pending',reason=?,retry_after=?,attempts=MAX(0,attempts-1),claim_token=NULL,worker_id=NULL,lease_until=NULL,updated_at=? WHERE claim_token=? AND status='running' AND lease_until>?",(verified['reason'],retry,stamp,token,stamp)).rowcount
            if not count:return 0
            c.execute('INSERT INTO research_runs VALUES(?,?,?,?) ON CONFLICT(research_id) DO UPDATE SET payload=excluded.payload,status=excluded.status,updated_at=excluded.updated_at',(research_id,json.dumps(payload,ensure_ascii=False),'deferred',stamp))
            return count
