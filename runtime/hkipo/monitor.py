"""One-shot intake coordinator. Does not send or start a background service."""
from datetime import datetime, timedelta, timezone
from .sources import SourceError

class Monitor:
    def __init__(self,store,sources,overlap_hours=72,initial_days=7,interval_seconds=60):
        if overlap_hours<0 or initial_days<1 or interval_seconds<1:
            raise ValueError('Invalid monitor window or interval')
        self.store,self.sources=store,list(sources)
        self.overlap=timedelta(hours=overlap_hours);self.initial=timedelta(days=initial_days)
        self.interval_seconds=interval_seconds # target only; scheduling external

    def scan(self,now=None):
        now=now or datetime.now(timezone.utc)
        if now.tzinfo is None:raise ValueError('Timezone-aware scan time required')
        self.store.ensure_monitor_start(now)
        results=[]
        for source in self.sources:
            state=self.store.source_state(source.name)
            if state and state.get('retry_not_before') and now<datetime.fromisoformat(state['retry_not_before']):
                results.append({'source':source.name,'ok':False,'deferred':True,'error':'Retry-After active'})
                continue
            start=datetime.fromisoformat(state['cutoff'])-self.overlap if state and state.get('cutoff') else now-self.initial
            try:
                result=source.fetch(start,now)
                if not result.complete:raise SourceError('incomplete source response')
                added,baseline=self.store.commit_scan(source.name,result.events,now.astimezone(timezone.utc).isoformat(),timestamp_precision=getattr(source,'timestamp_precision',None))
                results.append({'source':source.name,'ok':True,'baseline':baseline,'new_events':len(added),'total':result.count})
            except Exception as error:
                # Deliberately do not persist raw error text: HTTP bodies and URLs
                # can include session parameters. Keep a bounded diagnostic class.
                safe=str(error) if isinstance(error,SourceError) else type(error).__name__
                retry_after=getattr(error,'retry_after',None)
                retry_at=(now+timedelta(seconds=retry_after)).astimezone(timezone.utc).isoformat() if retry_after is not None else None
                self.store.record_failure(source.name,safe,retry_at)
                results.append({'source':source.name,'ok':False,'error':safe})
        return {'ok':bool(results) and all(r['ok'] for r in results),'sources':results,'alerts':self.store.alerts(),'pending_classification':self.store.pending_classification()}
