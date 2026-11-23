"""Bounded public-source snapshot ingestion. Never sends or writes articles."""
from __future__ import annotations
import hashlib
import json
import re
import sqlite3
import uuid
from pathlib import Path

SOURCE = 'futu_public_12147652'
ENTRY = 'https://q.futunn.com/profile/12147652'
SCOPE = 'broker_customer_allotment'


def linked_codes(body: str) -> list[str]:
    """Only stocks in the explicit allotment announcement clause, before any preview."""
    prefix = re.split(r'预告|預告', body, maxsplit=1)[0]
    match = re.search(r'([^。！？\n]*?)(?:已经公布中签|已經公佈中籤)', prefix)
    return sorted(set(re.findall(r'\((\d{5})\.HK\)', match[1]))) if match else []


def validate_snapshot(snapshot: dict) -> list[dict]:
    if snapshot.get('schema_version') != 1 or snapshot.get('source') != SOURCE or snapshot.get('entry') != ENTRY:
        raise ValueError('unsupported public-source schema')
    if snapshot.get('status') != 'success' or not isinstance(snapshot.get('posts'), list) or not snapshot['posts']:
        raise ValueError('failed or empty public discovery')
    seen = set()
    for post in snapshot['posts']:
        fid = post.get('feed_id', '')
        if not isinstance(fid, str) or not fid.isdigit() or fid in seen:
            raise ValueError('invalid or repeated feed ID')
        seen.add(fid)
        if post.get('url') != f'https://q.futunn.com/feed/{fid}' or post.get('author_id') != '12147652':
            raise ValueError('unverified public feed identity')
        if type(post.get('published_timestamp')) is not int or post['published_timestamp'] <= 0:
            raise ValueError('missing current-feed timestamp')
        if post.get('data_scope') != SCOPE or not isinstance(post.get('title'), str) or not post['title'] or not isinstance(post.get('body'), str) or not post['body']:
            raise ValueError('missing broker content or scope')
        if not isinstance(post.get('images'), list) or not post['images'] or any(not isinstance(u, str) or not u.startswith('https://nnqimage.futunn.com/') for u in post['images']):
            raise ValueError('missing public image evidence')
    return snapshot['posts']


def ingest_snapshot(snapshot: dict, db_path: str | Path, archive_dir: str | Path) -> dict:
    """Archive every attempt; successful nonempty snapshots alone advance source state.

    First successful snapshot establishes a silent baseline. Later new/revised posts
    return durable review candidates, never notifications. Multiple stock identity
    requires mapping; images are never assigned to a stock automatically.
    """
    raw = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    digest = hashlib.sha256(raw).hexdigest()
    archive = Path(archive_dir); archive.mkdir(parents=True, exist_ok=True)
    target = archive / f'{digest}.json'
    if not target.exists():
        temp = archive / f'{digest}.{uuid.uuid4().hex}.tmp'; temp.write_bytes(raw); temp.replace(target)
    try:
        posts = validate_snapshot(snapshot)
    except ValueError as exc:
        return {'status': 'rejected', 'reason': str(exc), 'snapshot_hash': digest, 'candidates': []}
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS broker_source_state(source TEXT PRIMARY KEY,last_snapshot TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS broker_source_versions(source TEXT,feed_id TEXT,content_hash TEXT,payload TEXT,snapshot_hash TEXT,baseline INTEGER NOT NULL,PRIMARY KEY(source,feed_id,content_hash));
        CREATE TABLE IF NOT EXISTS broker_source_latest(source TEXT,feed_id TEXT,content_hash TEXT,PRIMARY KEY(source,feed_id));
        CREATE TABLE IF NOT EXISTS broker_review_queue(revision_id INTEGER PRIMARY KEY AUTOINCREMENT,source TEXT NOT NULL,feed_id TEXT NOT NULL,content_hash TEXT NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending');
        ''')
        db.execute('BEGIN IMMEDIATE')
        baseline = db.execute('SELECT 1 FROM broker_source_state WHERE source=?', (SOURCE,)).fetchone() is None
        candidates = []
        for post in posts:
            material = {k: post[k] for k in ('feed_id','url','author_id','published_timestamp','title','body','images','data_scope')}
            material['images'] = sorted(set(material['images']))
            content_hash = hashlib.sha256(json.dumps(material, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            prior = db.execute('SELECT content_hash FROM broker_source_latest WHERE source=? AND feed_id=?', (SOURCE,post['feed_id'])).fetchone()
            known = db.execute('SELECT 1 FROM broker_source_versions WHERE source=? AND feed_id=? AND content_hash=?', (SOURCE,post['feed_id'],content_hash)).fetchone()
            codes = linked_codes(post['body'])
            payload = {**material, 'stock_codes': codes, 'identity_status': 'single_stock_candidate' if len(codes)==1 else 'needs_identity_mapping', 'notification_eligible': False, 'review_status':'needs_review', 'change_type':'revised' if prior else 'new', 'content_hash':content_hash}
            if not known:
                db.execute('INSERT INTO broker_source_versions VALUES(?,?,?,?,?,?)', (SOURCE,post['feed_id'],content_hash,json.dumps(payload,ensure_ascii=False),digest,int(baseline)))
            if not baseline and (prior is None or prior[0] != content_hash):
                cursor = db.execute('INSERT INTO broker_review_queue(source,feed_id,content_hash,payload,status) VALUES(?,?,?,?,?)', (SOURCE,post['feed_id'],content_hash,json.dumps(payload,ensure_ascii=False),'pending'))
                payload['revision_id'] = cursor.lastrowid
                candidates.append(payload)
            db.execute('INSERT OR REPLACE INTO broker_source_latest VALUES(?,?,?)', (SOURCE,post['feed_id'],content_hash))
        db.execute('INSERT OR REPLACE INTO broker_source_state VALUES(?,?)', (SOURCE,digest))
    return {'status':'baseline' if baseline else 'success','snapshot_hash':digest,'candidates':candidates}
