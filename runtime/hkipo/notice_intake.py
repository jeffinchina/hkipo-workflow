"""Small offline adapters from existing official intake/research to notice ledgers."""
from dataclasses import asdict
from datetime import date
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from .models import canonical_url
from .notices import NoticeBridge, make_official_event
from .evidence import normalize_quote


def _connect(value):
    path = getattr(value, 'dbpath', value)
    db = sqlite3.connect(str(path)); db.row_factory = sqlite3.Row
    return db


def _live(db):
    return [json.loads(r['payload']) for r in db.execute('SELECT payload FROM events WHERE baseline=0')]


def dispatch_official(store_state_db, flow, snapshot_dir=None, *, allotment_handler=None):
    evidence = []
    if snapshot_dir:
        from .service import SnapshotArchive
        evidence, _ = SnapshotArchive(snapshot_dir).evidence()
    results = []
    with _connect(store_state_db) as db:
        events = _live(db)
    bridge = NoticeBridge(flow)
    for original in events:
        if original.get('language') != 'zh-HK' or original.get('kind') not in ('allotment', 'exception'):
            continue
        d = dict(original)
        for item in evidence:
            item = asdict(item) if hasattr(item, '__dataclass_fields__') else item
            if (item.get('category') == 'official_new_listing_table' and item.get('role') == d['kind']
                    and item.get('url') == d.get('url') and item.get('stock_code') == d.get('stock_code')):
                d['official_role_evidence'] = item
                break
        try:
            source_hash = hashlib.sha256(json.dumps(original,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            event = make_official_event(d, episode_id=d.get('episode_id'), source_hash=source_hash)
        except (KeyError, ValueError, TypeError):
            results.append(dict(event_id=d.get('event_id'),status='skipped_unassociated'))
            continue
        if d['kind']=='allotment':
            # The source event stays allotment. Its facts feed the fixed-time
            # grey-market analysis; no duplicate immediate allotment e-mail.
            result=(allotment_handler(event,d) if allotment_handler else
                    {'status':'retained_for_grey_analysis'})
        else:
            result = bridge.send_notice(event, d['event_id'], '【官方公告】' + d['title'], event['brief'] + '\n' + event['source_url'])
        results.append(dict(result,source_event_id=d['event_id']))
    return results


def _load(path):
    return json.loads(Path(path).read_text())


def _date_in_quote(value, quote):
    parsed = date.fromisoformat(value)
    quote = normalize_quote(quote)
    return value in quote or f'{parsed.year}年{parsed.month}月{parsed.day}日' in quote


def schedule_ready_offers(state_db, research_root, reminders, now):
    results = []
    with _connect(state_db) as db:
        live = _live(db)
        try:
            runs = [dict(r) for r in db.execute("SELECT * FROM research_runs WHERE status='ready_for_delivery'")]
        except sqlite3.OperationalError:
            runs = []
    live_ids = {e['event_id'] for e in live}
    # Explicit postponement/withdrawal cancels only an unambiguous active issue.
    for e in live:
        if e.get('kind') != 'exception' or not re.search(r'延[期遲迟]|撤[回銷销]|withdraw|postpon',e.get('title',''),re.I):
            continue
        try:
            make_official_event(e,source_hash='eligibility-only')
        except (ValueError,KeyError):
            continue
        pending = [r for r in reminders.rows() if r['status']=='pending' and r['payload'].get('stock_code')==e.get('stock_code')]
        issues = {r['issue_id'] for r in pending}
        if len(issues)==1:
            p=pending[0]['payload']
            reminders.schedule(p['issue_id'],p['official_close'],source_hash=hashlib.sha256(e['url'].encode()).hexdigest(),evidence={'source_url':e['url']},stock_code=p['stock_code'],stock_name=p['stock_name'],status='withdrawn' if re.search(r'撤|withdraw',e['title'],re.I) else 'delayed',now=now)
            results.append(dict(issue_id=p['issue_id'],status='cancelled'))
    for run in runs:
        payload=json.loads(run['payload']); event=payload.get('event',{}); wr=payload.get('writer_result',{})
        if event.get('kind')!='offer' or not set(event.get('source_event_ids',[])).intersection(live_ids):
            continue
        result=dict(research_id=run['research_id'],status='needs_timetable')
        try:
            current=_load(Path(research_root)/run['research_id']/'status.json')
            if not wr.get('request_id') or current.get('request_id')!=wr['request_id'] or current.get('status')!='ready_for_delivery':
                raise ValueError('stale request')
            if any(current.get(k)!=wr.get(k) for k in ('bundle_path','quality_path')):
                raise ValueError('stale output')
            if _load(current['quality_path']).get('pass') is not True:
                raise ValueError('quality pending')
            bundle=_load(current['bundle_path'])
            if bundle.get('event_id')!=event['event_id'] or bundle.get('kind')!='offer':
                raise ValueError('wrong bundle')
            manifest=_load(Path(research_root)/run['research_id']/'sources/manifest.json')
            docs={d['document_id']:d for d in manifest['documents']}
            facts=[f for f in bundle['facts'] if f.get('key')=='subscription_period' and f.get('status')=='supported']
            closes=set(); pricings=set(); refs=[]
            for f in facts:
                for item in f.get('items',[]):
                    for key, collection in (('close_date',closes),('pricing_date',pricings)):
                        if key not in item: continue
                        value=item[key]
                        valid=[]
                        for ref in f.get('evidence',[]):
                            d=docs[ref['document_id']]
                            if hashlib.sha256(Path(d['path']).read_bytes()).hexdigest()!=d['sha256']: raise ValueError('changed PDF')
                            page=next(p for p in d['pages'] if p['pdf_page']==ref['pdf_page'])
                            if normalize_quote(ref['quote']) in normalize_quote(page['text']) and _date_in_quote(value,ref['quote']): valid.append((ref,d))
                        if not valid: raise ValueError('date quote absent')
                        collection.add(value); refs.extend(valid)
            if len(closes)!=1 or len(pricings)>1: raise ValueError('missing or conflicting dates')
            close=next(iter(closes)); expected=reminders.calendar.next_business_day(date.fromisoformat(close))
            if pricings and next(iter(pricings))!=expected.isoformat(): raise ValueError('nonstandard timetable')
            sources=[s for s in event.get('source_events',[]) if s.get('kind')=='offer' and s.get('event_id') in live_ids]
            if not sources or not refs or not event.get('episode_id'): raise ValueError('missing official source')
            doc_shas={d['sha256'] for _,d in refs}
            matching=[s for s in sources if any(h.get('sha256') in doc_shas and h.get('url')==s.get('url') for h in event.get('source_hashes',[]))]
            if not matching: raise ValueError('source document mismatch')
            url=canonical_url(matching[0]['url'])
            sha=hashlib.sha256(json.dumps(sorted(doc_shas)).encode()).hexdigest()
            issue=event['episode_id']; evidence={'source_url':url,'quote_sha256':hashlib.sha256(json.dumps([r for r,_ in refs],sort_keys=True).encode()).hexdigest()}
            existing=reminders.rows(issue)
            if existing and all(r['payload'].get('source_hash')==sha and r['payload'].get('official_close')==close and r['payload'].get('schedule_status')=='active' for r in existing):
                result.update(status='already_scheduled',issue_id=issue)
            else:
                # Do not resurrect a cancelled issue from an older ready run.
                if existing and any(r['payload'].get('schedule_status') in ('delayed','withdrawn') for r in existing): raise ValueError('cancelled timetable')
                reminders.schedule(issue,close,source_hash=sha,evidence=evidence,stock_code=event['stock_code'],stock_name=event['stock_name'],now=now)
                result.update(status='scheduled',issue_id=issue)
        except (OSError,ValueError,KeyError,TypeError,StopIteration):
            pass
        results.append(result)
    return results
