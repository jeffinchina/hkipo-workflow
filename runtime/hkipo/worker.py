"""Bounded queue worker: verified PDFs -> existing writer -> local pending delivery.

Issuer/event/document-role confirmations are explicit, never model guesses.
No mail, draft API, scheduler installation or model calls during import.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import threading
import uuid
from .downloads import DownloadError
from .quota import quota_deferral

class Worker:
    def __init__(self,store,downloader,job_root,skill_paths,writer=None,worker_id=None,lease_seconds=1800,max_attempts=3,auto_article_kinds=None):
        self.store=store;self.downloader=downloader;self.root=Path(job_root).resolve();self.root.mkdir(parents=True,exist_ok=True)
        self.skill_paths=dict(skill_paths);self.writer=writer;self.id=worker_id or uuid.uuid4().hex
        self.lease_seconds=lease_seconds;self.max_attempts=max_attempts
        self.auto_article_kinds=set(auto_article_kinds) if auto_article_kinds is not None else {'offer','allotment','exception'}
    def _finish(self,claim,status,reason=None,retry=0):
        changed=self.store.finish_claim(claim['token'],status,reason,retry_seconds=retry)
        return {'status':status if changed else 'lease_lost','event_ids':claim['event_ids'],'reason':reason}
    def run_one(self,author_inputs=None,**writer_options):
        # Injected writer recommended for offline validation. Real writer is
        # imported/called only when this explicit method passes every local gate.
        claim=self.store.claim_job_group(self.id,self.lease_seconds,self.max_attempts)
        if claim is None:return {'status':'idle'}
        owned=set(claim['event_ids']);classes=claim['classifications']
        seed=next(e for e in claim['events'] if e['event_id'] in owned)
        classification=classes.get(seed['event_id'])
        for event in claim['events']:
            if event['event_id'] in owned:self.store.record_stage([event['event_id']],'first_seen',{'discovered_at':event['discovered_at'],'published_at':event['published_at'],'published_precision':'date' if event['source'].startswith('new-listings-') else 'source_value'})
        if not classification:
            return self._finish(claim,'needs_classification','issuer_event_and_document_role_unconfirmed')
        expected={'offer':'ipo_offer','allotment':'ipo_allotment','exception':'ipo_exception'}.get(seed['kind'])
        if classification['issuer_type']!='common_equity' or classification['event_type']!=expected:
            return self._finish(claim,'needs_classification','not_confirmed_ordinary_equity_ipo_event')
        episode=classification['episode_id']
        selected=[e for e in claim['events'] if classes.get(e['event_id'],{}).get('episode_id')==episode and classes[e['event_id']]['issuer_type']=='common_equity' and classes[e['event_id']]['event_type']==expected]
        roles={classes[e['event_id']]['document_role'] for e in selected}
        if seed['kind']=='offer' and not {'announcement','prospectus'}.issubset(roles):
            return self._finish(claim,'waiting_sources','formal_announcement_and_prospectus_required')
        if seed['kind']!='offer' and seed['kind'] not in roles:
            return self._finish(claim,'needs_classification','document_role_not_confirmed')
        skill=self.skill_paths.get(seed['kind'])
        if not skill:return self._finish(claim,'blocked','writing_skill_missing')
        stop=threading.Event();lost=threading.Event()
        def heartbeat():
            while not stop.wait(max(.2,self.lease_seconds/3)):
                if not self.store.renew_claim(claim['token'],self.lease_seconds):lost.set();return
        thread=threading.Thread(target=heartbeat,daemon=True);thread.start()
        try:
            downloads=[]
            for event in sorted(selected,key=lambda e:e['event_id']):
                download=self.downloader.download(event['url'])
                downloads.append((event,download))
                self.store.record_stage([event['event_id']],'downloaded',download.to_dict())
            identity={'stock_code':seed['stock_code'],'kind':seed['kind'],'episode_id':episode,'sources':sorted((e['event_id'],d.sha256) for e,d in downloads)}
            research_id=hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()
            previous=self.store.research_run(research_id)
            if previous and previous['status']=='ready_for_delivery':
                return self._finish(claim,'ready_for_delivery','existing_identical_source_hashes')
            if lost.is_set() or not self.store.renew_claim(claim['token'],self.lease_seconds):return {'status':'lease_lost','event_ids':claim['event_ids']}
            logical_event_id=hashlib.sha256(json.dumps({'stock_code':seed['stock_code'],'kind':seed['kind'],'episode_id':episode},sort_keys=True).encode()).hexdigest()
            event=dict(seed,event_id=logical_event_id,research_id=research_id,source_event_ids=[e['event_id'] for e,_ in downloads],source_events=[e for e,_ in downloads],source_hashes=[d.to_dict() for _,d in downloads],episode_id=episode)
            if seed['kind'] not in self.auto_article_kinds:
                committed=self.store.finish_research_claim(claim['token'],research_id,{'event':event,'source_identity':identity},'material_only','author_reply_route')
                return {'status':'material_only' if committed else 'lease_lost','event_ids':claim['event_ids'],'research_id':research_id,'reason':'author_reply_route'}
            writer=self.writer
            if writer is None:
                from .writer import write_article
                writer=write_article
            directory=self.root/research_id
            self.store.research_run(research_id,{'event':event,'source_identity':identity},'writing')
            result=writer(event=event,pdf_paths=list(dict.fromkeys(d.path for _,d in downloads)),job_dir=directory,skill_path=skill,author_inputs=author_inputs or {},**writer_options)
            if lost.is_set() or not self.store.renew_claim(claim['token'],self.lease_seconds):return {'status':'lease_lost','event_ids':claim['event_ids']}
            defer=result.get('quota_defer') if result.get('status')=='deferred' else None
            verified=quota_deferral(defer.get('observation')) if isinstance(defer,dict) else None
            if verified:
                committed=self.store.defer_research_claim(claim['token'],research_id,{'event':event,'writer_result':result,'source_identity':identity},verified)
                return {'status':'deferred' if committed else 'lease_lost','event_ids':claim['event_ids'],'research_id':research_id,'reason':verified['reason'],'retry_at':verified['retry_at']}
            status=result['status'] if result.get('status') in {'ready_for_delivery','revision_pending'} else 'blocked'
            reason=None if status=='ready_for_delivery' else (result.get('reason') or 'writer_requires_attention')
            committed=self.store.finish_research_claim(claim['token'],research_id,{'event':event,'writer_result':result,'source_identity':identity},status,reason)
            answer={'status':status if committed else 'lease_lost','event_ids':claim['event_ids'],'reason':reason}
            if not committed:return answer
            self.store.record_stage(claim['event_ids'],'written',{'research_id':research_id,'status':status,'written_at':datetime.now(timezone.utc).isoformat()})
            answer.update(research_id=research_id,logical_event_id=logical_event_id,writer_result=result)
            return answer
        except DownloadError as error:
            self.store.record_stage(claim['event_ids'],'blocked',{'safe_error':str(error)})
            return self._finish(claim,'failed','download_failed',retry=max(300,int(getattr(error,'retry_after',None) or 0)+1))
        except Exception as error:
            # Never persist raw model/HTTP error messages, which may contain
            # headers, private configuration or prompt contents.
            self.store.record_stage(claim['event_ids'],'blocked',{'error_type':type(error).__name__})
            return self._finish(claim,'failed','worker_failed_'+type(error).__name__,retry=300)
        finally:
            stop.set();thread.join(timeout=2)
    def run_batch(self,max_groups=10,**options):
        if not 1<=max_groups<=100:raise ValueError('Batch must be bounded')
        results=[]
        for _ in range(max_groups):
            result=self.run_one(**options)
            if result['status']=='idle':break
            results.append(result)
        return results
