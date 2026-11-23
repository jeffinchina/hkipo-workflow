"""Offline phone-reply intake. Never connects, sends, renders or publishes.

Adapter contract: register_reminder only after a real reminder send; supply a
trusted authenticated_sender from mailbox authentication policy (not From alone).
Submit UTF-8 author text as 标题: ... / 正文: ... / 【正文结束】 on own lines.
Only pending rows are executable. needs_clarification/rejected rows never queue.
Workers reserve() before draft effects; an unknown reservation requires manual
reconciliation through complete(), never an automatic second create. One event
has one persistent draft_id across author versions. Raw accepted/bounded mail is
stored for audit; callers must protect this SQLite file as private author data.
"""
import hashlib
import json
import os
import re
import sqlite3
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses
from html.parser import HTMLParser
from pathlib import Path
from .models import utcnow

_IDS = re.compile(r'<[^<>\s]+@[^<>\s]+>')
END = '【正文结束】'


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True); self.parts=[]; self.skip=0; self.quoted=0
    def handle_starttag(self, tag, attrs):
        if tag in {'script','style'}: self.skip += 1
        if tag == 'blockquote':
            self.quoted += 1; self.parts.append('\n')
        if not self.skip and tag in {'br','p','div','li'}: self.parts.append('\n')
    def handle_endtag(self, tag):
        if tag in {'script','style'} and self.skip: self.skip -= 1
        if tag == 'blockquote' and self.quoted:
            self.quoted -= 1; self.parts.append('\n')
        if not self.skip and tag in {'p','div','li'}: self.parts.append('\n')
    def handle_data(self, data):
        if not self.skip:
            # Keep quote context visible to the conservative submission parser.
            # A quoted template cannot become a new author block, and an author
            # quotation inside the block needs clarification rather than loss.
            self.parts.append('\n'.join('> '+line for line in data.split('\n')) if self.quoted else data)


def _author_text(msg):
    plain=[]; html=[]
    def walk(part):
        # Never descend attached messages or attached multipart containers.
        if part.get_content_disposition() == 'attachment' or part.get_filename(): return
        if part.get_content_type() == 'message/rfc822': return
        if part.is_multipart():
            for child in part.iter_parts(): walk(child)
        elif part.get_content_type() in {'text/plain','text/html'}:
            value=part.get_content()
            (plain if part.get_content_type()=='text/plain' else html).append(value)
    walk(msg)
    choices=plain or html
    if len(choices)!=1: raise ValueError('ambiguous_body_parts')
    value=choices[0]
    if not plain:
        parser=_HTMLText();parser.feed(value);value=''.join(parser.parts)
    return value.replace('\r\n','\n').replace('\r','\n')


def _image_attachments(msg, *, validate=True):
    """Only explicit new attachments; quoted inline CID images are excluded.

    Inspect byte signatures only. Never interpret EXIF, text, or executable data.
    """
    images=[]
    def candidates(part):
        if part.get_content_type() == 'message/rfc822': return
        if part.is_multipart():
            if part.get_content_disposition() == 'attachment': return
            for child in part.iter_parts(): yield from candidates(child)
        else: yield part
    for part in candidates(msg):
        if part.get_content_disposition() != 'attachment': continue
        if part.get_content_type() not in {'image/png','image/jpeg'}: continue
        data=part.get_payload(decode=True) or b''
        if validate and (not data or len(data)>=5_000_000): raise ValueError('needs_image_resize')
        mime=part.get_content_type()
        if validate and not (data.startswith(b'\x89PNG\r\n\x1a\n') if mime=='image/png' else data.startswith(b'\xff\xd8\xff')):
            raise ValueError('invalid_image_format')
        images.append({'sha256':hashlib.sha256(data).hexdigest(),'mime':mime,'data':data})
        if validate and len(images)>4: raise ValueError('too_many_images')
    return images


def _submission(value):
    # Only explicit fenced input is executable. No subject-based title guessing,
    # signature heuristics or quote stripping that might discard author prose.
    # iPhone/QQ can collapse template labels onto the same visible line.
    # Accept only an explicit leading title, body delimiter and end delimiter;
    # never infer them from a subject or quoted history.
    if re.match(r'^\s*标题[:：]', value):
        prefix, marker, rest = value.partition(END)
        if marker:
            match = re.match(r'^(\s*标题[:：].*?)[ \t]+正文[:：][ \t]*\n', prefix, re.S)
            if match and '\n' not in match.group(1).strip():
                prefix = match.group(1) + '\n正文：\n' + prefix[match.end():]
            if prefix and not prefix.endswith('\n'):
                prefix += '\n'
            value = prefix + marker + rest
    lines=value.split('\n')
    first=next((i for i,line in enumerate(lines) if line.strip()),len(lines))
    if first==len(lines): raise ValueError('missing_explicit_submission')
    title=re.fullmatch(r'标题[:：][ \t]*(.+)',lines[first])
    if not title: raise ValueError('missing_explicit_title')
    start=first+1
    while start<len(lines) and not lines[start].strip():start+=1
    if start>=len(lines) or not re.fullmatch(r'正文[:：][ \t]*',lines[start]):
        raise ValueError('missing_body_marker')
    # The first explicit end closes author input. Ordinary mobile clients may
    # quote the reminder template afterwards without prefixing every line with
    # '>'; those later template markers are outside this submission.
    end=next((i for i in range(start+1,len(lines)) if lines[i].strip()==END),None)
    if end is None: raise ValueError('missing_end_marker')
    body='\n'.join(lines[start+1:end])
    if not body.strip(): raise ValueError('empty_body')
    # Reject quoted history inside the explicit author block, instead of silently
    # changing its contents. Everything after END is intentionally outside it.
    if any(re.match(r'^\s*(>|On .+wrote:|在.+写道[:：]|-{2,}\s*Original Message)',s) for s in body.split('\n')):
        raise ValueError('quoted_history_inside_body')
    return title.group(1),body


class MailReplyStore:
    def __init__(self, dbpath, allowed_senders, max_bytes=30_000_000):
        self.dbpath=str(dbpath);self.allowed={s.strip().casefold() for s in allowed_senders}
        if not self.allowed or not 1<=max_bytes<=30_000_000: raise ValueError('Explicit sender allowlist and positive size limit required')
        self.max_bytes=max_bytes;Path(self.dbpath).parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS reply_reminders(message_id TEXT PRIMARY KEY,event_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS reply_reminder_originals(message_id TEXT PRIMARY KEY,event_id TEXT NOT NULL,raw BLOB NOT NULL,raw_hash TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS reply_reminder_aliases(message_id TEXT PRIMARY KEY,original_id TEXT NOT NULL UNIQUE,event_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS reply_drafts(event_id TEXT PRIMARY KEY,draft_id TEXT);
            CREATE TABLE IF NOT EXISTS reply_messages(raw_hash TEXT PRIMARY KEY,message_id TEXT UNIQUE,raw BLOB NOT NULL,event_id TEXT,version INTEGER,status TEXT NOT NULL,reason TEXT,title TEXT,body TEXT,content_hash TEXT,created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS reply_reparse_audit(id INTEGER PRIMARY KEY,raw_hash TEXT NOT NULL,previous_reason TEXT,new_status TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE UNIQUE INDEX IF NOT EXISTS reply_versions ON reply_messages(event_id,version);
            ''')
            columns={r[1] for r in c.execute('PRAGMA table_info(reply_messages)')}
            if 'attachments_json' not in columns:
                c.execute("ALTER TABLE reply_messages ADD COLUMN attachments_json TEXT NOT NULL DEFAULT '[]'")
    def connect(self):
        c=sqlite3.connect(self.dbpath,timeout=30)
        os.chmod(self.dbpath,0o600)
        c.row_factory=sqlite3.Row;return c
    def register_reminder(self,message_id,event_id):
        if not _IDS.fullmatch(message_id) or not event_id: raise ValueError('Invalid reminder identity')
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('SELECT event_id FROM reply_reminders WHERE message_id=?',(message_id,)).fetchone()
            if old and old[0]!=event_id: raise ValueError('Reminder already bound to another event')
            c.execute('INSERT OR IGNORE INTO reply_reminders VALUES(?,?)',(message_id,event_id))
    def remember_reminder(self, raw, event_id):
        """Retain the exact outbound MIME before SMTP; only confirmed roots reconcile."""
        if not isinstance(raw, bytes) or len(raw) > self.max_bytes:
            raise ValueError('Invalid reminder original')
        msg = BytesParser(policy=policy.default).parsebytes(raw)
        mids = msg.get_all('Message-ID', [])
        if len(mids) != 1 or not _IDS.fullmatch(str(mids[0]).strip()) or not event_id:
            raise ValueError('Invalid reminder identity')
        mid = str(mids[0]).strip()
        digest = hashlib.sha256(raw).hexdigest()
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            old = c.execute('SELECT event_id FROM reply_reminder_originals WHERE message_id=?', (mid,)).fetchone()
            if old and old[0] != event_id:
                raise ValueError('Reminder original already bound to another event')
            # MIME boundaries may change on reconstruction; retain first attempt.
            c.execute('INSERT OR IGNORE INTO reply_reminder_originals VALUES(?,?,?,?,?)',
                      (mid, event_id, raw, digest, utcnow()))

    def ingest(self,raw,*,authenticated_sender=None):
        if not isinstance(raw,bytes): raise TypeError('raw must be bytes')
        if len(raw)>self.max_bytes: return {'status':'rejected','reason':'message_too_large'}
        digest=hashlib.sha256(raw).hexdigest()
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            prior=c.execute('SELECT * FROM reply_messages WHERE raw_hash=?',(digest,)).fetchone()
            if prior:return dict(prior)|{'duplicate':True}
            attachments=[];status='rejected';reason=None;eid=None;version=None;title=None;body=None;content_hash=None;mid=None
            try:
                msg=BytesParser(policy=policy.default).parsebytes(raw)
                if any(part.defects for part in msg.walk()): raise ValueError('malformed_mime')
                senders=getaddresses(msg.get_all('From',[]))
                identity=(authenticated_sender or '').strip().casefold()
                if len(senders)!=1 or senders[0][1].casefold() not in self.allowed or identity!=senders[0][1].casefold():
                    raise ValueError('sender_not_authenticated_or_allowed')
                mids=msg.get_all('Message-ID',[])
                if len(mids)!=1 or not _IDS.fullmatch(str(mids[0]).strip()): raise ValueError('invalid_message_id')
                mid=str(mids[0]).strip()
                existing=c.execute('SELECT * FROM reply_messages WHERE message_id=?',(mid,)).fetchone()
                if existing:return {'status':'rejected','reason':'message_id_collision','raw_hash':digest}
                refs=' '.join(str(v) for h in ('In-Reply-To','References') for v in msg.get_all(h,[]))
                ids=set(_IDS.findall(refs))
                events={r[0] for ident in ids for r in c.execute('SELECT event_id FROM reply_reminders WHERE message_id=?',(ident,))}
                if len(events)!=1: raise ValueError('unmatched_or_ambiguous_thread')
                eid=events.pop();status='needs_clarification'
                title,body=_submission(_author_text(msg))
                attachments=_image_attachments(msg)
                content_hash=hashlib.sha256((title+'\0'+body+('\0'+','.join(i['sha256'] for i in attachments) if attachments else '')).encode()).hexdigest()
                if attachments:
                    root=Path(self.dbpath).parent/'reply-originals'/digest
                    root.mkdir(parents=True,exist_ok=True,mode=0o700);os.chmod(root,0o700)
                    for i,item in enumerate(attachments):
                        path=root/(str(i)+('.png' if item['mime']=='image/png' else '.jpg'))
                        path.write_bytes(item.pop('data'));os.chmod(path,0o600)
                        item['path']=str(path.resolve())
                same=c.execute('SELECT * FROM reply_messages WHERE event_id=? AND version IS NOT NULL ORDER BY version DESC LIMIT 1',(eid,)).fetchone()
                if same and same['content_hash']==content_hash:
                    c.execute('INSERT INTO reply_messages(raw_hash,message_id,raw,event_id,version,status,reason,title,body,content_hash,created_at,attachments_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(digest,mid,raw,eid,None,'duplicate',None,title,body,content_hash,utcnow(),json.dumps(attachments)))
                    return dict(c.execute('SELECT * FROM reply_messages WHERE raw_hash=?',(digest,)).fetchone())|{'duplicate':True}
                version=c.execute('SELECT COALESCE(MAX(version),0)+1 FROM reply_messages WHERE event_id=?',(eid,)).fetchone()[0]
                # A newer explicit version replaces only unstarted work.
                c.execute("UPDATE reply_messages SET status='superseded' WHERE event_id=? AND status='pending'",(eid,))
                c.execute('INSERT OR IGNORE INTO reply_drafts(event_id) VALUES(?)',(eid,));status='pending'
            except (ValueError,LookupError,UnicodeError,TypeError,RecursionError) as exc:
                reason=str(exc)
            c.execute('INSERT INTO reply_messages(raw_hash,message_id,raw,event_id,version,status,reason,title,body,content_hash,created_at,attachments_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(digest,mid,raw,eid,version,status,reason,title,body,content_hash,utcnow(),json.dumps(attachments)))
            row=dict(c.execute('SELECT * FROM reply_messages WHERE raw_hash=?',(digest,)).fetchone())
            return row
    def reparse_clarification(self, raw_hash, *, authenticated_sender=None):
        """Reparse retained authenticated input after a parser fix, with audit.

        No replacement raw mail, sender, event, or completed/unknown work may be
        supplied. The caller must provide the same trusted mailbox identity.
        """
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT * FROM reply_messages WHERE raw_hash=?', (raw_hash,)).fetchone()
            if not row or row['status'] != 'needs_clarification' or not row['event_id']:
                raise ValueError('Only authenticated clarification rows can be reparsed')
            raw = bytes(row['raw'])
            if hashlib.sha256(raw).hexdigest() != raw_hash:
                raise ValueError('Retained raw hash changed')
            msg = BytesParser(policy=policy.default).parsebytes(raw)
            senders = getaddresses(msg.get_all('From', []))
            identity = (authenticated_sender or '').strip().casefold()
            if (len(senders) != 1 or identity not in self.allowed
                    or senders[0][1].casefold() != identity):
                raise ValueError('sender_not_authenticated_or_allowed')
            if (any(part.defects for part in msg.walk())
                    or len(msg.get_all('Message-ID', [])) != 1
                    or str(msg['Message-ID']).strip() != row['message_id']):
                raise ValueError('Retained message identity changed')
            refs = set(_IDS.findall(' '.join(str(v) for h in ('In-Reply-To', 'References') for v in msg.get_all(h, []))))
            events = {r[0] for ref in refs for r in c.execute('SELECT event_id FROM reply_reminders WHERE message_id=?', (ref,))}
            if events != {row['event_id']}:
                raise ValueError('Retained thread association changed')
            title, body = _submission(_author_text(msg))
            attachments = _image_attachments(msg)
            content_hash = hashlib.sha256((title+'\0'+body+('\0'+','.join(i['sha256'] for i in attachments) if attachments else '')).encode()).hexdigest()
            if attachments:
                root = Path(self.dbpath).parent/'reply-originals'/raw_hash
                root.mkdir(parents=True, exist_ok=True, mode=0o700); os.chmod(root, 0o700)
                for i, item in enumerate(attachments):
                    path = root/(str(i)+('.png' if item['mime']=='image/png' else '.jpg'))
                    path.write_bytes(item.pop('data')); os.chmod(path, 0o600)
                    item['path'] = str(path.resolve())
            same = c.execute('SELECT content_hash FROM reply_messages WHERE event_id=? AND version IS NOT NULL ORDER BY version DESC LIMIT 1', (row['event_id'],)).fetchone()
            status = 'duplicate' if same and same[0] == content_hash else 'pending'
            version = None
            if status == 'pending':
                version = c.execute('SELECT COALESCE(MAX(version),0)+1 FROM reply_messages WHERE event_id=?', (row['event_id'],)).fetchone()[0]
                c.execute("UPDATE reply_messages SET status='superseded' WHERE event_id=? AND status='pending'", (row['event_id'],))
                c.execute('INSERT OR IGNORE INTO reply_drafts(event_id) VALUES(?)', (row['event_id'],))
            c.execute('INSERT INTO reply_reparse_audit(raw_hash,previous_reason,new_status,created_at) VALUES(?,?,?,?)', (raw_hash, row['reason'], status, utcnow()))
            c.execute('UPDATE reply_messages SET version=?,status=?,reason=NULL,title=?,body=?,content_hash=?,attachments_json=? WHERE raw_hash=?', (version, status, title, body, content_hash, json.dumps(attachments), raw_hash))
            return dict(c.execute('SELECT * FROM reply_messages WHERE raw_hash=?', (raw_hash,)).fetchone())

    def pending(self):
        with self.connect() as c:
            return [dict(r) for r in c.execute("SELECT m.*,d.draft_id FROM reply_messages m JOIN reply_drafts d USING(event_id) WHERE m.status='pending' ORDER BY m.created_at,m.version")]
    def reserve(self,raw_hash):
        """Return work with draft_id. Unknown event blocks all other versions."""
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute("SELECT m.*,d.draft_id FROM reply_messages m JOIN reply_drafts d USING(event_id) WHERE raw_hash=? AND status='pending'",(raw_hash,)).fetchone()
            if not row:return None
            if c.execute("SELECT 1 FROM reply_messages WHERE event_id=? AND status='unknown'",(row['event_id'],)).fetchone():return None
            c.execute("UPDATE reply_messages SET status='unknown' WHERE raw_hash=?",(raw_hash,))
            return dict(row)|{'status':'unknown'}
    def complete(self,raw_hash,draft_id):
        if not draft_id:raise ValueError('draft_id required')
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute("SELECT event_id FROM reply_messages WHERE raw_hash=? AND status='unknown'",(raw_hash,)).fetchone()
            if not row:return False
            prior=c.execute('SELECT draft_id FROM reply_drafts WHERE event_id=?',(row[0],)).fetchone()[0]
            if prior and prior!=draft_id:raise ValueError('Cannot replace event draft identity')
            c.execute('UPDATE reply_drafts SET draft_id=? WHERE event_id=?',(draft_id,row[0]))
            c.execute("UPDATE reply_messages SET status='complete' WHERE raw_hash=?",(raw_hash,));return True
