"""One-shot intake coordinator. Does not send or start a background service."""
from datetime import datetime, timedelta, timezone
from .sources import SourceError, TitleDataError

class Monitor:
    def __init__(self,store,sources,overlap_hours=72,initial_days=7,interval_seconds=60):
        if overlap_hours<0 or initial_days<1 or interval_seconds<1:
            raise ValueError('Invalid monitor window or interval')
        self.store,self.sources=store,list(sources)
        self.overlap=timedelta(hours=overlap_hours);self.initial=timedelta(days=initial_days)
        self.interval_seconds=interval_seconds # target only; scheduling external

    def _commit(self,source,result,now,*,partial=False):
        added,baseline=self.store.commit_scan(source.name,result.events,now.isoformat(),
                                              timestamp_precision=getattr(source,'timestamp_precision',None),
                                              partial=partial)
        return {'source':source.name,'ok':True,'baseline':baseline,'new_events':len(added),'total':result.count}

    def _record_failure(self,source,error,now):
        # HTTP bodies and URLs can include session parameters.
        safe=str(error) if isinstance(error,SourceError) else type(error).__name__
        retry_after=getattr(error,'retry_after',None)
        retry_at=(now+timedelta(seconds=retry_after)).isoformat() if retry_after is not None else None
        self.store.record_failure(source.name,safe,retry_at)
        return {'source':source.name,'ok':False,'error':safe}

    def scan(self,now=None):
        now=now or datetime.now(timezone.utc)
        if now.tzinfo is None:raise ValueError('Timezone-aware scan time required')
        now=now.astimezone(timezone.utc)
        self.store.ensure_monitor_start(now)
        results=[];recovery=[]
        for source in self.sources:
            state=self.store.source_state(source.name)
            if state and state.get('retry_not_before') and now<datetime.fromisoformat(state['retry_not_before']):
                results.append({'source':source.name,'ok':False,'deferred':True,'error':'Retry-After active'})
                continue
            start=datetime.fromisoformat(state['cutoff'])-self.overlap if state and state.get('cutoff') else now-self.initial
            try:
                result=source.fetch(start,now)
                if not result.complete:raise SourceError('incomplete source response')
                results.append(self._commit(source,result,now))
            except Exception as error:
                results.append(self._record_failure(source,error,now))
                if (isinstance(error,TitleDataError) and error.retry_after is None
                        and callable(getattr(type(source),'recover',None))):
                    recovery.append((len(results)-1,source,start))
        # Read independent sources before trying more queries on a faulty feed.
        for index,source,start in recovery:
            try:
                result=source.recover(start,now)
                observation=self._commit(source,result,now,partial=not result.complete)
                if result.complete:
                    results[index]=observation
                    results[index]['recovered']=True
                else:
                    results[index]['recovery']={'complete':False,'new_events':observation['new_events'],
                                                'total':observation['total']}
            except Exception as error:
                safe=str(error) if isinstance(error,SourceError) else type(error).__name__
                if getattr(error,'retry_after',None) is not None:
                    results[index]=self._record_failure(source,error,now)
                results[index]['recovery']={'complete':False,'error':safe}
        return {'ok':bool(results) and all(r['ok'] for r in results),'sources':results,'alerts':self.store.alerts(),'pending_classification':self.store.pending_classification()}
