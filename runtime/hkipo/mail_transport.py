"""Bounded, read-only IMAP intake for registered phone replies only.

Authentication is a matching message in the same account's Sent folder. From
and user-supplied Authentication-Results are never treated as proof of authorship.
No new mail is sent, no flag is changed, and no mailbox-wide body scan occurs.
"""
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
import hashlib
from datetime import datetime, timedelta, timezone
import imaplib
import re
import ssl
from .mail_reply import _author_text, _submission, _IDS, _image_attachments


class IntakeError(RuntimeError):
    pass


def _quoted(value):
    if not isinstance(value, str) or any(c in value for c in '\r\n\x00'):
        raise IntakeError('unsafe_imap_search_value')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def _fetch_bytes(client, uid, fields, max_bytes):
    status, result = client.uid('FETCH', uid, fields)
    if status != 'OK': raise IntakeError('imap_fetch_failed')
    chunks = [r[1] for r in result or [] if isinstance(r, tuple) and isinstance(r[1], bytes)]
    if len(chunks) != 1 or len(chunks[0]) > max_bytes:
        raise IntakeError('imap_body_missing_or_oversized')
    return chunks[0]


def _search(client, *criteria, charset=None):
    status, data = client.uid('SEARCH', charset, *criteria)
    if status != 'OK' or len(data or []) != 1 or not isinstance(data[0], bytes):
        raise IntakeError('imap_search_failed')
    ids = data[0].split()
    if any(not uid.isdigit() for uid in ids): raise IntakeError('invalid_imap_uid')
    return ids


def _select(client, folder):
    status, _ = client.select(_quoted(folder), readonly=True)
    if status != 'OK': raise IntakeError('imap_readonly_folder_unavailable')


def _identity(msg, sender):
    addresses = getaddresses(msg.get_all('From', []))
    return len(addresses) == 1 and addresses[0][1].casefold() == sender.casefold()


def _thread_ids(msg):
    return set(_IDS.findall(' '.join(str(v) for h in ('In-Reply-To','References') for v in msg.get_all(h, []))))


def _headers(client, uid, max_bytes, *, date_window=None):
    fields='(RFC822.SIZE '+('INTERNALDATE ' if date_window else '')+'BODY.PEEK[HEADER.FIELDS (FROM TO REPLY-TO SUBJECT DATE MESSAGE-ID IN-REPLY-TO REFERENCES)])'
    status, data = client.uid('FETCH', uid, fields)
    if status != 'OK': raise IntakeError('imap_header_fetch_failed')
    parts = [r for r in data or [] if isinstance(r, tuple)]
    if len(parts) != 1 or len(parts[0]) != 2 or not all(isinstance(v, bytes) for v in parts[0]):
        raise IntakeError('imap_header_missing')
    match = re.search(rb'RFC822.SIZE\s+(\d+)', parts[0][0], re.I)
    if not match or int(match[1]) > max_bytes or len(parts[0][1]) > min(max_bytes, 65536):
        return None
    if date_window:
        value=re.search(rb'INTERNALDATE\s+"([^"]+)"',parts[0][0],re.I)
        try:day=datetime.strptime(value[1].decode('ascii'),'%d-%b-%Y %H:%M:%S %z').date()
        except (ValueError,UnicodeError,TypeError):return None
        if not date_window[0]<=day<date_window[1]:return None
    return BytesParser(policy=policy.default).parsebytes(parts[0][1])


def _proof(client, raw, sender, sent_folder, max_bytes):
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    mids = msg.get_all('Message-ID', [])
    if len(mids) != 1 or not _IDS.fullmatch(str(mids[0]).strip()) or not _identity(msg, sender):
        return False
    try:
        expected = _submission(_author_text(msg))
    except (ValueError, TypeError, LookupError, UnicodeError):
        # Still authenticate malformed author input so intake can return a
        # useful clarification, without accepting an injected body as author.
        expected = None
    _select(client, sent_folder)
    ids = _search(client, 'HEADER', 'Message-ID', _quoted(str(mids[0]).strip()))
    date_window=None
    if not ids:
        # A server may silently fail even an exact Message-ID search. Bound
        # same-account Sent headers by server date, then require exact ID,
        # thread, complete author text and attachments as in the fast path.
        try:day=parsedate_to_datetime(str(msg.get('Date',''))).date()
        except (ValueError,TypeError,AttributeError):day=datetime.now(timezone.utc).date()
        date_window=(day-timedelta(days=1),day+timedelta(days=3))
        ids=_search(client,'SINCE',date_window[0].strftime('%d-%b-%Y'),'FROM',_quoted(sender))
    if len(ids) > 100: return False
    exact = []
    for uid in ids:
        head = _headers(client, uid, max_bytes,date_window=date_window)
        if head is not None and len(head.get_all('Message-ID', [])) == 1 and str(head['Message-ID']).strip() == str(mids[0]).strip():
            exact.append((uid, head))
    if len(exact) != 1: return False
    uid, mirror_head = exact[0]
    if mirror_head is None: return False
    if len(mirror_head.get_all('Message-ID', [])) != 1 or str(mirror_head.get('Message-ID', '')).strip() != str(mids[0]).strip() or not _identity(mirror_head, sender):
        return False
    for header in ('In-Reply-To', 'References'):
        if set(_IDS.findall(' '.join(map(str, msg.get_all(header, []))))) != set(
                _IDS.findall(' '.join(map(str, mirror_head.get_all(header, []))))):
            return False
    mirror = BytesParser(policy=policy.default).parsebytes(
        _fetch_bytes(client, uid, '(BODY.PEEK[])', max_bytes))
    if len(mirror.get_all('Message-ID', [])) != 1 or str(mirror.get('Message-ID', '')).strip() != str(mids[0]).strip() or not _identity(mirror, sender):
        return False
    # Require the same thread too; a reused Message-ID must not transplant a
    # legitimate author draft into another security/IPO episode.
    for header in ('In-Reply-To', 'References'):
        if set(_IDS.findall(' '.join(map(str, msg.get_all(header, []))))) != set(
                _IDS.findall(' '.join(map(str, mirror.get_all(header, []))))):
            return False
    try:
        if _image_attachments(msg, validate=False) != _image_attachments(mirror, validate=False): return False
        if expected is None: return _author_text(msg) == _author_text(mirror)
        return expected == _submission(_author_text(mirror))
    except (ValueError, TypeError, LookupError, UnicodeError):
        return False


def _reminder_content(msg):
    """Complete MIME leaves, including HTML, inline images and other attachments.

    Ignore transfer encoding and boundary changes, but normalize only text line
    endings. Never reduce a reminder to its subject, extracted author text, or
    selected images: every original body/attachment must be identical.
    """
    if any(part.defects for part in msg.walk()):
        raise ValueError('malformed_reminder')
    parts = []
    for part in msg.walk():
        if part.get_content_type() == 'message/rfc822':
            raise ValueError('nested_reminder_message_not_supported')
        if part.is_multipart():
            parts.append((part.get_content_type(), part.get_content_disposition(),
                          part.get_filename(), str(part.get('Content-ID', ''))))
            continue
        data = part.get_payload(decode=True)
        if data is None:
            raise ValueError('missing_reminder_payload')
        if part.get_content_maintype() == 'text':
            data = part.get_content().replace('\r\n', '\n').replace('\r', '\n')
        parts.append((part.get_content_type(), part.get_content_disposition(),
                      part.get_filename(), str(part.get('Content-ID', '')), data))
    return tuple(parts)


def _same_reminder_headers(original, mirror, sender):
    if not _identity(original, sender) or not _identity(mirror, sender):
        return False
    return all(original.get_all(h, []) == mirror.get_all(h, [])
               for h in ('Subject', 'To', 'Reply-To', 'In-Reply-To', 'References'))


def _reconcile_reminder_aliases(client, store, sender, sent_folder, limit):
    with store.connect() as c:
        roots = c.execute("""SELECT o.* FROM reply_reminder_originals o
            JOIN reply_reminders r ON r.message_id=o.message_id AND r.event_id=o.event_id
            LEFT JOIN reply_reminder_aliases a ON a.original_id=o.message_id
            WHERE a.message_id IS NULL ORDER BY o.rowid DESC LIMIT ?""", (limit,)).fetchall()
    matches = []
    for root in roots:
        raw = bytes(root['raw'])
        if hashlib.sha256(raw).hexdigest() != root['raw_hash']:
            continue
        original = BytesParser(policy=policy.default).parsebytes(raw)
        subject = str(original.get('Subject', ''))
        if not subject or len(original.get_all('Subject', [])) != 1:
            continue
        try:
            content = _reminder_content(original)
            day = datetime.fromisoformat(root['created_at'].replace('Z', '+00:00'))
        except (ValueError, TypeError, LookupError, UnicodeError):
            continue
        _select(client, sent_folder)
        # Restrict to this account's Sent and a short delivery window. A search
        # server may overmatch; exact headers gate every bounded body fetch.
        since=(day-timedelta(days=1)).strftime('%d-%b-%Y')
        before=(day+timedelta(days=3)).strftime('%d-%b-%Y')
        ids = _search(client, 'SINCE', since, 'BEFORE', before,
                      'HEADER', 'Subject', _quoted(subject).encode('utf-8'), charset='UTF-8')
        fallback=not ids
        if fallback:
            # Some IMAP servers return an empty successful result for a UTF-8
            # subject that is visibly present. Use only this account's Sent
            # headers in the same short window, then require the exact headers
            # and complete original MIME below before accepting any alias.
            # The same servers can also silently fail BEFORE. Enforce its
            # upper bound using server INTERNALDATE before any body fetch.
            ids=_search(client,'SINCE',since,'FROM',_quoted(sender))
        if len(ids) > 100:
            continue
        candidates = []
        for uid in ids:
            head = _headers(client, uid, store.max_bytes,date_window=((day-timedelta(days=1)).date(),(day+timedelta(days=3)).date()) if fallback else None)
            if head is None or not _same_reminder_headers(original, head, sender):
                continue
            mids = head.get_all('Message-ID', [])
            if len(mids) != 1 or not _IDS.fullmatch(str(mids[0]).strip()):
                continue
            candidates.append((uid, str(mids[0]).strip(), mids))
        if len(candidates) > 10:
            continue
        found = []
        for uid, alias, mids in candidates:
            mirror = BytesParser(policy=policy.default).parsebytes(
                _fetch_bytes(client, uid, '(BODY.PEEK[])', store.max_bytes))
            try:
                if (mirror.get_all('Message-ID', []) == mids
                        and _same_reminder_headers(original, mirror, sender)
                        and _reminder_content(mirror) == content):
                    found.append(alias)
            except (ValueError, TypeError, LookupError, UnicodeError):
                continue
        if len(found) == 1:
            matches.append((found[0], root['message_id'], root['event_id']))
    with store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        for alias, original_id, event_id in matches:
            # A shared mirror cannot choose between distinct original events.
            if len({e for a, _, e in matches if a == alias}) != 1:
                continue
            prior = c.execute('SELECT event_id FROM reply_reminders WHERE message_id=?', (alias,)).fetchone()
            if prior and prior[0] != event_id:
                continue
            c.execute('INSERT OR IGNORE INTO reply_reminders VALUES(?,?)', (alias, event_id))
            c.execute('INSERT OR IGNORE INTO reply_reminder_aliases VALUES(?,?,?)', (alias, original_id, event_id))


def poll_replies(store, config, *, sent_folder='Sent Messages', client_factory=None,
                 max_messages=20, max_reminders=30):
    """Search only registered reminder threads. Return safe summaries, no bodies.

    Config is an explicitly supplied in-memory email mapping. Never log it.
    A missing Sent mirror is deferred (phone sync may lag), never guessed valid.
    Header search matches References for subsequent replies to the same thread.
    """
    sender = config.get('user', '')
    if sender.casefold() not in store.allowed or max_messages < 1 or max_reminders < 1:
        raise IntakeError('explicit_mailbox_allowlist_required')
    with store.connect() as c:
        reminders = [r[0] for r in c.execute('SELECT message_id FROM reply_reminders ORDER BY rowid DESC LIMIT ?', (max_reminders,))]
    if not reminders: return {'status': 'idle', 'checked': 0, 'results': []}
    factory = client_factory or (lambda: imaplib.IMAP4_SSL(
        config.get('imap_host', 'imap.qq.com'), int(config.get('imap_port', 993)),
        ssl_context=ssl.create_default_context(), timeout=30))
    client = None
    try:
        client = factory()
        if client.login(sender, config['password'])[0] != 'OK': raise IntakeError('imap_login_failed')
        _reconcile_reminder_aliases(client, store, sender, sent_folder, max_reminders)
        with store.connect() as c:
            # Alias rows share the original event, including after restarts.
            reminders = [r[0] for r in c.execute('SELECT message_id FROM reply_reminders ORDER BY rowid DESC LIMIT ?', (max_reminders * 2,))]
        _select(client, 'INBOX')
        ids = set()
        for mid in reminders:
            ids.update(_search(client, 'FROM', _quoted(sender), 'OR', 'HEADER', 'In-Reply-To', _quoted(mid),
                               'HEADER', 'References', _quoted(mid)))
        # Old accepted messages must not consume the new-body budget. Filter
        # candidates using headers before selecting a bounded, rotating page.
        results = []; candidates = []
        for uid in sorted(ids, key=int):
            _select(client, 'INBOX')
            head = _headers(client, uid, store.max_bytes)
            if head is None:
                results.append({'status': 'rejected', 'reason': 'message_too_large'}); continue
            if not _identity(head, sender): continue
            if len(head.get_all('Message-ID', [])) != 1: continue
            mid = str(head['Message-ID']).strip()
            if not _IDS.fullmatch(mid): continue
            # Registered originals/verified aliases are our outbound roots,
            # never a new author reply even if a status receipt has References.
            if mid in reminders: continue
            # IMAP HEADER SEARCH is a substring search. Validate exact registered
            # identities before downloading any author's private body.
            if not _thread_ids(head).intersection(reminders): continue
            with store.connect() as c:
                events = {r[0] for ref in _thread_ids(head) for r in c.execute('SELECT event_id FROM reply_reminders WHERE message_id=?', (ref,))}
                if len(events) != 1: continue
                known = c.execute('SELECT status FROM reply_messages WHERE message_id=?', (mid,)).fetchone()
            if known: continue
            candidates.append(uid)
        # Persist a fair cursor even for missing Sent mirrors. Otherwise a page
        # of deferred phone-sync copies would permanently starve later replies.
        # UID resets are harmless: the page wraps to lower values each cycle.
        mailbox = sender.casefold() + '\0' + config.get('imap_host', 'imap.qq.com')
        with store.connect() as c:
            c.execute('CREATE TABLE IF NOT EXISTS reply_intake_cursor(mailbox TEXT PRIMARY KEY,last_uid INTEGER NOT NULL)')
            prior = c.execute('SELECT last_uid FROM reply_intake_cursor WHERE mailbox=?', (mailbox,)).fetchone()
        cursor = prior[0] if prior else 0
        page = ([uid for uid in candidates if int(uid) > cursor] +
                [uid for uid in candidates if int(uid) <= cursor])[:max_messages]
        for uid in page:
            with store.connect() as c:
                c.execute('INSERT INTO reply_intake_cursor VALUES(?,?) ON CONFLICT(mailbox) DO UPDATE SET last_uid=excluded.last_uid', (mailbox, int(uid)))
            _select(client, 'INBOX')
            raw = _fetch_bytes(client, uid, '(BODY.PEEK[])', store.max_bytes)
            if not _proof(client, raw, sender, sent_folder, store.max_bytes):
                results.append({'status': 'deferred', 'reason': 'sent_copy_not_verified'}); continue
            row = store.ingest(raw, authenticated_sender=sender)
            results.append({k: row.get(k) for k in ('status', 'reason', 'event_id', 'version', 'raw_hash')})
        return {'status': 'ok', 'checked': len(ids), 'has_more': len(candidates) > len(page), 'results': results}
    except IntakeError as exc:
        safe = {'unsafe_imap_search_value', 'imap_fetch_failed', 'imap_body_missing_or_oversized', 'imap_search_failed', 'invalid_imap_uid', 'imap_readonly_folder_unavailable', 'imap_header_fetch_failed', 'imap_header_missing', 'imap_login_failed'}
        raise IntakeError(str(exc) if str(exc) in safe else 'imap_intake_failed') from None
    except Exception:
        raise IntakeError('imap_intake_failed') from None
    finally:
        if client:
            try: client.logout()
            except Exception: pass
