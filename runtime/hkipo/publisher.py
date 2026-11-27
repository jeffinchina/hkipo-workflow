"""Local ready-job → Word QA → frozen email/private draft worker.

Separate from discovery so a long model/renderer call cannot delay source scans.
Historical acceptance is event-allowlisted. No public publishing API exists here.
"""
from dataclasses import dataclass,field
from datetime import datetime,timezone
import json
from pathlib import Path
import re
from .delivery import Deliverer,DeliveryError
from .draft_covers import OfferDraftCovers
from .quota import QuotaDeferred,QuotaUnavailable,quota_deferral
from .release_worker import prepare_release,RenderUnavailable,WordLayoutRejected
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
    offer_cover_paths:list=field(default_factory=list)
    def __post_init__(self):
        for key in ('enable_deliveries','production_accepted','acceptance_mode'):
            if type(getattr(self,key)) is not bool:raise ValueError('Explicit JSON booleans required')
        if not isinstance(self.accepted_event_ids,list) or any(not isinstance(x,str) or not x for x in self.accepted_event_ids):raise ValueError('Explicit acceptance event list required')
        if not isinstance(self.offer_cover_paths,list) or any(not isinstance(path,str) or not path for path in self.offer_cover_paths):raise ValueError('Offer cover paths must be a list of local files')
        if not 1<=self.max_jobs<=10 or self.poll_seconds<15:raise ValueError('Invalid bounded publisher settings')


class Publisher:
    def __init__(self,settings,*,preparer=None,deliverer=None):
        self.settings=settings;self.root=Path(settings.data_dir).resolve()
        self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.store=StateStore(settings.state_db)
        self.preparer=preparer or prepare_release;self.deliverer=deliverer
        self.offer_covers=OfferDraftCovers(self.store,settings.offer_cover_paths) if settings.offer_cover_paths else None
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE');self.store._worker_schema(c)
            c.execute('CREATE TABLE IF NOT EXISTS publication_runs(research_id TEXT PRIMARY KEY,status TEXT NOT NULL,report TEXT NOT NULL,retry_at REAL)')

    def _record(self,rid,status,report,retry=None):
        with self.store.connect() as c:
            c.execute('INSERT OR REPLACE INTO publication_runs VALUES(?,?,?,?)',(rid,status,json.dumps(report,ensure_ascii=False),retry))
        return {'research_id':rid,'status':status,**report}

    def _retry(self,row,reason,report):
        previous=json.loads(row['previous_report']) if row['previous_report'] else {}
        delays=(60,300,900,1800)
        attempt=min(previous.get('recovery_attempt',0)+1,len(delays)) if previous.get('reason')==reason else 1
        retry=datetime.now(timezone.utc).timestamp()+delays[attempt-1]
        return self._record(row['research_id'],'deferred',{**report,'reason':reason,'recovery_attempt':attempt,'retry_at':retry},retry)

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
                rows=c.execute("SELECT r.*,p.report AS previous_report FROM research_runs r LEFT JOIN publication_runs p USING(research_id) WHERE r.status='ready_for_delivery' AND (p.status IS NULL OR (p.status='deferred' AND (p.retry_at IS NULL OR p.retry_at<=?)) OR (p.status='prepared' AND ?)) ORDER BY r.updated_at LIMIT ?",(datetime.now(timezone.utc).timestamp(),cfg.enable_deliveries,cfg.max_jobs)).fetchall()
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
                    selected=self.store.research_run(rid)
                    if not selected or selected['status']!='ready_for_delivery':
                        results.append(self._record(rid,'superseded',{'reason':'newer_research_ready'}));continue
                    if not cfg.enable_deliveries:
                        results.append(self._record(rid,'prepared',{'release_path':str(job/'release.json'),'delivery_enabled':False}));continue
                    sender=self.deliverer or Deliverer(self.store,config_path=cfg.private_config)
                    try:email=sender.email(release)
                    except DeliveryError:email={'status':'blocked','error':'email_preparation_failed'}
                    try:
                        cover={'thumb_media_id':cfg.thumb_media_id}
                        if self.offer_covers is not None and not cfg.acceptance_mode:
                            cover=self.offer_covers.select(release,payload['event'],cfg.thumb_media_id)
                        draft=sender.draft(release,update_existing=True,**cover)
                    except DeliveryError as exc:
                        code=str(exc)
                        draft={'status':'retryable','error':code} if code.startswith('offer_cover_') else {'status':'blocked','error':'draft_preparation_failed'}
                    confirmed={'confirmed','already_confirmed'}
                    report={'email':email,'draft':draft,'release_path':str(job/'release.json')}
                    outcomes={email.get('status'),draft.get('status')}
                    if 'retryable' in outcomes:
                        draft_reason=draft.get('error','')
                        reason=draft_reason if draft_reason.startswith('offer_cover_') else 'delivery_preflight_retry'
                        results.append(self._retry(row,reason,report))
                    else:
                        results.append(self._record(rid,'delivered' if outcomes<=confirmed else 'needs_attention',report))
                except RenderUnavailable as exc:
                    results.append(self._retry(row,'word_render_unavailable',{'failures':exc.failures}))
                except WordLayoutRejected:
                    results.append(self._record(rid,'needs_attention',{'reason':'word_layout_rejected'}))
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
                recovery=[{'research_id':r['research_id'],'reason':json.loads(r['report']).get('reason'),'retry_at':r['retry_at']}
                    for r in c.execute("SELECT research_id,report,retry_at FROM publication_runs WHERE status='deferred' ORDER BY retry_at")]
            return {'status':'needs_attention' if attention else ('recovering' if recovery else 'ok'),'ok':not attention,
                    'delivery_enabled':cfg.enable_deliveries,'attention':attention,'recovery':recovery,'results':results}

    def run_loop(self,stop,max_cycles=None):
        cycles=0;last=None
        while not stop.is_set():
            try:last=self.cycle()
            except Exception:last={'status':'failed','reason':'publisher_cycle_failed'}
            cycles+=1;write_json(self.root/'health.json',{'cycles':cycles,'last':last,'at':datetime.now(timezone.utc).isoformat()})
            if max_cycles is not None and cycles>=max_cycles:break
            stop.wait(self.settings.poll_seconds)
        return {'status':'stopped','cycles':cycles,'last':last}
