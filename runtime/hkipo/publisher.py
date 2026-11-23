"""Local ready-job → Word QA → frozen email/private draft worker.

Separate from discovery so a long model/renderer call cannot delay source scans.
Historical acceptance is event-allowlisted. No public publishing API exists here.
"""
from dataclasses import dataclass,field
from datetime import datetime,timezone
import json
from pathlib import Path
import re
from .delivery import Deliverer
from .quota import QuotaDeferred,QuotaUnavailable,quota_deferral
from .release_worker import prepare_release
from .service import singleton_lock,write_json
from .store import StateStore


@dataclass
class PublisherSettings:
    data_dir:str
    state_db:str
    research_root:str
    private_config:str
    settings_path:str|None=None
    enable_deliveries:bool=False
    production_accepted:bool=False
    acceptance_mode:bool=False
    accepted_event_ids:list=field(default_factory=list)
    thumb_media_id:str|None=None
    poll_seconds:int=60
    max_jobs:int=2
    def __post_init__(self):
        for key in ('enable_deliveries','production_accepted','acceptance_mode'):
            if type(getattr(self,key)) is not bool:raise ValueError('Explicit JSON booleans required')
        if not isinstance(self.accepted_event_ids,list) or any(not isinstance(x,str) or not x for x in self.accepted_event_ids):raise ValueError('Explicit acceptance event list required')
        if not 1<=self.max_jobs<=10 or self.poll_seconds<15:raise ValueError('Invalid bounded publisher settings')


class Publisher:
    def __init__(self,settings,*,preparer=None,deliverer=None):
        self.settings=settings;self.root=Path(settings.data_dir).resolve()
        self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.store=StateStore(settings.state_db)
        self.preparer=preparer or prepare_release;self.deliverer=deliverer
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE');self.store._worker_schema(c)
            c.execute('CREATE TABLE IF NOT EXISTS publication_runs(research_id TEXT PRIMARY KEY,status TEXT NOT NULL,report TEXT NOT NULL,retry_at REAL)')

    def _record(self,rid,status,report,retry=None):
        with self.store.connect() as c:
            c.execute('INSERT OR REPLACE INTO publication_runs VALUES(?,?,?,?)',(rid,status,json.dumps(report,ensure_ascii=False),retry))
        return {'research_id':rid,'status':status,**report}

    def _eligible(self,payload):
        event=payload.get('event',{});eid=event.get('event_id')
        if self.settings.acceptance_mode:
            return eid in self.settings.accepted_event_ids
        ids=event.get('source_event_ids',[])
        if not ids:return False
        with self.store.connect() as c:
            # A real queued non-baseline trigger is mandatory. Merely finding an
            # old release or a downloaded PDF never makes it publishable.
            return any(c.execute("SELECT 1 FROM events e JOIN jobs j USING(event_id) WHERE e.event_id=? AND e.baseline=0 AND j.status='ready_for_delivery'",(i,)).fetchone() for i in ids)

    def cycle(self):
        with singleton_lock(self.root/'.publisher.lock') as acquired:
            if not acquired:return {'status':'busy','results':[]}
            cfg=self.settings
            if cfg.enable_deliveries and not (cfg.production_accepted or cfg.acceptance_mode):
                return {'status':'blocked','reason':'real_delivery_acceptance_required','results':[]}
            with self.store.connect() as c:
                rows=c.execute("SELECT r.* FROM research_runs r LEFT JOIN publication_runs p USING(research_id) WHERE r.status='ready_for_delivery' AND (p.status IS NULL OR (p.status='deferred' AND (p.retry_at IS NULL OR p.retry_at<=?)) OR (p.status='prepared' AND ?)) ORDER BY r.updated_at LIMIT ?",(datetime.now(timezone.utc).timestamp(),cfg.enable_deliveries,cfg.max_jobs)).fetchall()
            results=[]
            for row in rows:
                rid=row['research_id']
                try:
                    payload=json.loads(row['payload'])
                    if not cfg.acceptance_mode and payload.get('event',{}).get('kind','offer')!='offer':
                        results.append(self._record(rid,'material_only',{'reason':'v06_author_reply_route'}));continue
                    if not re.fullmatch(r'[0-9a-f]{64}',rid) or not self._eligible(payload):
                        results.append(self._record(rid,'blocked',{'reason':'not_a_live_queued_event_or_authorized_acceptance'}));continue
                    job=(Path(cfg.research_root).resolve()/rid)
                    status=json.loads((job/'status.json').read_text())
                    if (status.get('status')!='ready_for_delivery' or not status.get('request_id')
                            or payload.get('writer_result',{}).get('request_id')!=status.get('request_id')):
                        results.append(self._record(rid,'blocked',{'reason':'queued_writer_request_changed'}));continue
                    release=self.preparer(job,settings_path=cfg.settings_path)
                    current=json.loads((job/'status.json').read_text())
                    if (current.get('status')!='ready_for_delivery' or current.get('request_id')!=status.get('request_id')
                            or release.get('request_id')!=status.get('request_id')):
                        results.append(self._record(rid,'blocked',{'reason':'queued_writer_request_changed'}));continue
                    if release['event_id']!=payload['event']['event_id']:
                        raise ValueError('Release identity differs from queued event')
                    if not cfg.enable_deliveries:
                        results.append(self._record(rid,'prepared',{'release_path':str(job/'release.json'),'delivery_enabled':False}));continue
                    sender=self.deliverer or Deliverer(self.store,config_path=cfg.private_config)
                    email=sender.email(release)
                    draft=sender.draft(release,thumb_media_id=cfg.thumb_media_id,update_existing=True)
                    confirmed={'confirmed','already_confirmed'}
                    state='delivered' if email.get('status') in confirmed and draft.get('status') in confirmed else 'needs_attention'
                    report={'email':email,'draft':draft,'release_path':str(job/'release.json')}
                    results.append(self._record(rid,state,report))
                except QuotaDeferred as exc:
                    deferred=quota_deferral(exc.observation)
                    results.append(self._record(rid,'deferred' if deferred else 'blocked',{'reason':'quota_deferred' if deferred else 'quota_unverified_or_hard_limit','observation':exc.observation},deferred['retry_at'] if deferred else None))
                    break
                except QuotaUnavailable:
                    results.append(self._record(rid,'blocked',{'reason':'quota_unverified_or_hard_limit'}));break
                except Exception as exc:
                    # Original diagnostics stay in controller-owned QA files.
                    # Health files never contain private model/article bodies.
                    results.append(self._record(rid,'blocked',{'reason':'release_preparation_failed','error_class':type(exc).__name__}))
            with self.store.connect() as c:
                attention=[{'research_id':r['research_id'],'status':r['status']}
                    for r in c.execute("SELECT research_id,status FROM publication_runs WHERE status IN ('blocked','needs_attention')")]
            return {'status':'needs_attention' if attention else 'ok','ok':not attention,
                    'delivery_enabled':cfg.enable_deliveries,'attention':attention,'results':results}

    def run_loop(self,stop,max_cycles=None):
        cycles=0;last=None
        while not stop.is_set():
            try:last=self.cycle()
            except Exception:last={'status':'failed','reason':'publisher_cycle_failed'}
            cycles+=1;write_json(self.root/'health.json',{'cycles':cycles,'last':last,'at':datetime.now(timezone.utc).isoformat()})
            if max_cycles is not None and cycles>=max_cycles:break
            stop.wait(self.settings.poll_seconds)
        return {'status':'stopped','cycles':cycles,'last':last}
