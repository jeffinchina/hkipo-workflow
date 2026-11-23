"""After-close collection and fixed-time mail over the existing reply ledger.

The broker calendar schedules jobs before an allotment announcement exists.
At 18:40 missing official material is reported honestly, never waited for.
"""
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from .grey_market import HK, GreyDataError, report_text, timestamp, validate_snapshot, decimal, display
from .grey_store import GreyStore
from .notices import NoticeBridge


@dataclass
class GreySettings:
    data_dir:str
    official_state_db:str
    research_root:str
    enabled:bool=False
    provider:str|None=None
    providers:tuple=('辉立暗盘','富途暗盘')
    calendar_refresh_seconds:int=1800
    proxy_url:str|None=None
    private_config:str|None=None
    reply_db:str|None=None
    background_collection:bool=False
    scan_seconds:int=15
    def __post_init__(self):
        if (type(self.enabled) is not bool or type(self.background_collection) is not bool
                or not 5<=self.scan_seconds<=60 or not 300<=self.calendar_refresh_seconds<=86400
                or not isinstance(self.providers,(tuple,list)) or not self.providers
                or len(self.providers)>2 or any(not isinstance(p,str) or not p for p in self.providers)):
            raise ValueError('Explicit grey-analysis settings required')


class GreyService:
    def __init__(self,settings,flow,*,calendar_fetcher=None,collector=None,clock=None):
        self.settings=settings;self.flow=flow;self.root=Path(settings.data_dir)
        self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.store=GreyStore(self.root/'grey.sqlite3')
        self.calendar_fetcher=calendar_fetcher;self.collector=collector
        self.clock=clock or (lambda:datetime.now(HK))
        self.executor=ThreadPoolExecutor(max_workers=2) if settings.background_collection else None
        self.inflight={}

    def _calendar(self,now):
        if self.calendar_fetcher is None:
            from .grey_calendar import fetch_calendar
            fetcher=lambda:fetch_calendar(self.root/'calendar',proxy_url=self.settings.proxy_url)
        else:fetcher=self.calendar_fetcher
        with self.store.connect() as c:
            previous=c.execute('SELECT * FROM grey_source_refresh WHERE source=?',('calendar',)).fetchone()
            interval=min(60,self.settings.calendar_refresh_seconds) if previous and previous['reason'] else self.settings.calendar_refresh_seconds
            if previous and (now-timestamp(previous['last_attempt'])).total_seconds()<interval:return
            c.execute('INSERT INTO grey_source_refresh(source,last_attempt) VALUES(?,?) ON CONFLICT(source) DO UPDATE SET last_attempt=excluded.last_attempt',('calendar',now.isoformat()))
        try:
            for item in fetcher():
                # One official calendar supplies candidate dates; each venue's
                # actual same-day quotes and series must independently confirm
                # its session before any prices/analysis can be reported.
                for provider in ((self.settings.provider,) if self.settings.provider else self.settings.providers):
                    candidate=dict(item,provider=provider,market='grey')
                    episode=self._episode(candidate)
                    if episode:candidate['episode_id']=episode
                    self.store.register(candidate,now)
            with self.store.connect() as c:
                c.execute("UPDATE grey_source_refresh SET last_success=?,reason=NULL WHERE source='calendar'",(now.isoformat(),))
        except Exception as error:
            with self.store.connect() as c:c.execute("UPDATE grey_source_refresh SET reason=? WHERE source='calendar'",('calendar_'+type(error).__name__,))

    def retain_notice(self,event,original):
        material=dict(status='published_pending_review',source_url=original['url'],source_time=original['published_at'],
                      source_event_id=original['event_id'],episode=original.get('episode_id'))
        self.store.retain_allotment(original['event_id'],original['stock_code'],material)
        return {'status':'retained_for_grey_analysis','source_event_id':original['event_id']}

    def _episode(self,session):
        if session.get('episode_id'):return session['episode_id']
        with sqlite3.connect(self.settings.official_state_db) as c:
            c.row_factory=sqlite3.Row
            try:rows=c.execute("SELECT e.payload,w.episode_id FROM worker_classifications w JOIN events e USING(event_id) WHERE w.issuer_type='common_equity' AND w.event_type='ipo_offer'").fetchall()
            except sqlite3.OperationalError:return None
        candidates=[]
        for r in rows:
            e=json.loads(r['payload'])
            if e.get('stock_code')!=session['stock_code']:continue
            published=datetime.fromisoformat(e['published_at']).date()
            delta=(datetime.fromisoformat(session['trade_date']).date()-published).days
            if 0<=delta<=180:candidates.append((published,r['episode_id']))
        if not candidates:return None
        latest=max(d for d,_ in candidates)
        ids={eid for d,eid in candidates if d==latest}
        return next(iter(ids)) if len(ids)==1 else None

    def _allotment(self,row,verify=True):
        """Reuse only current independently passed facts; raw notice remains pending."""
        # Refresh stale caller rows and use compare-and-swap on completion.
        row=self.store.get(row['session_id']) or row
        expected_sha=self.store.material_sha(row.get('allotment'))
        material={'status':'not_published'};session=row['payload'];episode=self._episode(session)
        cached=row.get('allotment') or {}
        if cached.get('status') in ('published_pending_review','publication_unverified'):material=cached
        if cached.get('status')=='verified':
            try:
                checks=[(cached['bundle_path'],cached['bundle_sha256']),(cached['review_path'],cached['review_sha256'])]
                checks += [(d['path'],d['sha256']) for d in cached['source_documents']]
                checks += [(d['path'],d['sha256']) for d in cached['guard_files']]
                if all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==sha for p,sha in checks):material=cached
            except (OSError,KeyError,TypeError):pass
        with sqlite3.connect(self.settings.official_state_db) as c:
            c.row_factory=sqlite3.Row
            try:classes={r['event_id']:dict(r) for r in c.execute("SELECT * FROM worker_classifications WHERE issuer_type='common_equity' AND event_type='ipo_allotment' AND document_role='allotment'")}
            except sqlite3.OperationalError:classes={}
            for r in c.execute('SELECT payload FROM events WHERE baseline=0'):
                event=json.loads(r[0])
                if event.get('kind')!='allotment' or event.get('stock_code')!=session['stock_code']:continue
                classification=classes.get(event['event_id'])
                if not episode or not classification or classification['episode_id']!=episode:
                    # A same-code raw row does not prove the current IPO issue.
                    if material['status']=='not_published':material={'status':'publication_unverified','source_url':event['url']}
                    continue
                if material.get('status')!='verified' or event['event_id'] not in material.get('source_event_ids',[]):
                    material={'status':'published_pending_review','source_url':event['url'],'source_event_id':event['event_id'],'episode_id':episode}
                self.store.retain_allotment(event['event_id'],session['stock_code'],material)
            if material['status']=='not_published':
                source=c.execute("SELECT last_success,failure_count FROM sources WHERE source='title-search'").fetchone()
                age=(self.clock()-timestamp(source['last_success'])).total_seconds() if source and source['last_success'] else None
                if source is None or source['failure_count'] or age is None or not 0<=age<=180:
                    material={'status':'publication_unverified','reason':'official_intake_not_fresh'}
            try:records=c.execute("SELECT * FROM research_runs ORDER BY updated_at,rowid").fetchall()
            except sqlite3.OperationalError:records=[]
        relevant=[]
        for record in records:
            candidate=json.loads(record['payload']);event=candidate.get('event',{})
            if event.get('kind')=='allotment' and event.get('stock_code')==session['stock_code'] and episode and event.get('episode_id')==episode:
                relevant.append(record)
        latest=relevant[-1] if relevant else None
        if material.get('status')=='verified':
            live_payload=json.loads(latest['payload']) if latest else {}
            if (not latest or latest['status']!='ready_for_delivery' or cached.get('research_id')!=latest['research_id']
                    or cached.get('request_id')!=live_payload.get('writer_result',{}).get('request_id')):
                material={'status':'published_pending_review','reason':'current_research_changed','episode_id':episode}
        ready=[latest] if latest and latest['status']=='ready_for_delivery' else []
        for r in (ready if verify and material.get('status')!='verified' else []):
            payload=json.loads(r['payload']);event=payload.get('event',{});wr=payload.get('writer_result',{})
            if (event.get('kind')!='allotment' or event.get('stock_code')!=session['stock_code']
                    or not episode or event.get('episode_id')!=episode):continue
            try:
                from .grey_material import verified_allotment
                result=verified_allotment(payload,Path(self.settings.research_root)/event['research_id'])
                # Same verified IPO episode survives a later listing postponement.
                result['episode_id']=episode
                material=result
            except (OSError,ValueError,KeyError,TypeError):continue
        return self.store.attach_allotment(row['session_id'],material,expected_sha)

    def _collect(self,row,now):
        if self.collector is None:
            def collector(session):
                if session['provider']=='辉立暗盘':
                    from .grey_phillip import collect_session
                else:
                    from .grey_source import collect_session
                return collect_session(session,self.root/'sources',proxy_url=self.settings.proxy_url)
        else:collector=self.collector
        try:
            snapshot=collector(row['payload'])
            completed=max(now,self.clock())
            self.store.record_collection(row['session_id'],snapshot=snapshot,now=completed)
            # Raw quote and complete minute series are retained by the source
            # adapter; normalized facts have a separate immutable local archive.
            normalized=self.root/'snapshots'/row['session_id'];normalized.mkdir(parents=True,exist_ok=True)
            raw=json.dumps(snapshot,ensure_ascii=False,sort_keys=True,indent=2).encode()
            (normalized/(hashlib.sha256(raw).hexdigest()+'.json')).write_bytes(raw)
        except Exception as error:
            self.store.record_collection(row['session_id'],reason='grey_source_'+type(error).__name__,now=max(now,self.clock()))

    @staticmethod
    def report_id(row):
        return hashlib.sha256(json.dumps(['grey-analysis-v1',row['payload']['stock_code'],row['trade_date']]).encode()).hexdigest()

    def _send_group(self,rows,now):
        row=rows[0];session=row['payload'];material=self._allotment(row,verify=False)
        report_id=self.report_id(row);sections=[]
        available=[r['snapshot'] for r in rows if r['data_status'] in ('verified','quote_verified') and r['snapshot']]
        baselines={(decimal(s['issue_price']),s['lot_size'],s['currency']) for s in available}
        reference_conflict=len(baselines)>1 or any(s.get('reference_conflict') for s in available)
        if material.get('status')=='verified':
            for fact in material.get('facts',[]):
                if fact.get('key')=='final_offer_price' and fact.get('status')=='supported':
                    try:reference_conflict|=any(decimal(s['issue_price'])!=decimal(fact['value']) for s in available)
                    except GreyDataError:pass
        for item in rows:
            venue=item['payload']['provider']
            if item['data_status'] in ('verified','quote_verified') and item['snapshot']:
                if reference_conflict:
                    quote=item['snapshot'];part=venue+'\n'
                    part+='；'.join(label+'：'+display(quote['ohlc'][key]) for key,label in [('open','开盘'),('close','收盘'),('high','最高'),('low','最低')])
                    part+='\n行情来源：'+quote['source_url']
                    sections.append(part)
                    continue
                body=report_text(item['snapshot'],material,now).split('\n配发概要：',1)[0].strip()
                # Shared issuer/date/allotment appear once; platform facts and
                # commentary stay separate and retain their own source URL.
                part=venue+'\n'+'\n'.join(body.splitlines()[2:])
                part+='\n行情来源：'+item['snapshot']['source_url']
                if item['snapshot'].get('series_kind')=='sampled_price_line':
                    part+='\n分时为AAStocks官方辉立数据（5分钟采样），日行情与辉立官方报价交叉核验。'
            else:
                part=venue+'\n官方完整收盘数据尚未取得或尚未通过校验；暂不提供价格与走势判断。'
            sections.append(part)
        if reference_conflict:sections.append('发行价或每手股数存在来源分歧，统一基准尚待核实；保留各平台已核实开收高低，暂不列涨跌幅及一手盈亏。')
        allocation=(material['summary'] if material['status']=='verified' else
                    '正式配发公告尚未公布。' if material['status']=='not_published' else
                    '当前发行期的配发公告发布状态尚在核验。' if material['status']=='publication_unverified' else
                    '正式配发公告已发布，概要数据尚在核验。')
        text=(session['issuer']+'（'+session['stock_code']+'）暗盘收盘点评\n'+row['trade_date']+' 18:40\n\n'+
              '\n\n'.join(sections)+'\n\n配发概要：'+allocation+
              '\n\n盈亏按发行价中签一手计算，未扣费用，不代表实际成交收益。不同暗盘分别统计。')
        eid='grey_analysis:'+report_id
        event=dict(event_id=eid,notification_kind='grey_analysis',stock_code=session['stock_code'],
                   issuer=session['issuer'],episode=session.get('episode_id') or session['listing_date'],
                   source_url=session['schedule_source_url'],source_time=session['session_end'],first_seen=row['first_seen'],
                   brief='暗盘收盘点评与配发概要',images=[],source_hash=hashlib.sha256(text.encode()).hexdigest(),
                   grey_data_status={r['payload']['provider']:r['data_status'] for r in rows},allotment_status=material['status'])
        if not self.store.reserve_group([r['session_id'] for r in rows],eid,report_id):
            return {'status':'already_reserved','report_id':report_id}
        result=NoticeBridge(self.flow).send_notice(event,report_id,'【暗盘分析】'+session['issuer']+' '+session['stock_code'],text)
        if result['status'] in ('confirmed','already_confirmed'):self.store.finish_group(report_id,result['message_id'])
        return dict(result,report_id=report_id)

    def _reconcile(self):
        # A confirmed mail receipt repairs a crash after SMTP without resending.
        for row in self.store.rows():
            if row['delivery_status']!='unknown' or not row['event_id']:continue
            key=NoticeBridge.delivery_key(row['event_id'],self.report_id(row))
            with self.flow.replies.connect() as c:
                result=c.execute("SELECT message_id FROM mobile_alerts WHERE delivery_key=? AND status='confirmed'",(key,)).fetchone()
            if result:self.store.finish_group(self.report_id(row),result['message_id'])

    def cycle(self,now=None):
        now=timestamp(now.isoformat()) if now else datetime.now(HK)
        if not self.settings.enabled:return {'status':'disabled','results':[]}
        self._reconcile();results=[]
        # At the deadline send already scheduled work before any source/network
        # refresh. Formal allotment never gates the 18:40 notification.
        groups={}
        due={self.report_id(r) for r in self.store.due(now)}
        for row in self.store.rows():
            key=self.report_id(row)
            if key in due and row['delivery_status']=='pending':groups.setdefault(key,[]).append(row)
        for rows in groups.values():results.append(self._send_group(rows,now))
        # A routine refresh is deferred near the fixed deadline; its network
        # latency must not hold the mail scheduler across 18:40.
        if not (now.hour==18 and 38<=now.minute<=40):self._calendar(now)
        for sid,future in list(self.inflight.items()):
            if future.done():
                try:future.result()
                except Exception:pass
                del self.inflight[sid]
        for row in self.store.collection_due(now):
            if row['session_id'] in self.inflight:continue
            if self.executor:
                self.inflight[row['session_id']]=self.executor.submit(self._collect,row,now)
            else:self._collect(row,now)
        # Keep late official allotment summaries independently, without another
        # reminder or replacement of the frozen mail body.
        for row in self.store.rows():
            key='allotment:'+row['session_id']
            if key in self.inflight:continue
            if self.executor:self.inflight[key]=self.executor.submit(self._allotment,row)
            else:self._allotment(row)
        with self.store.connect() as c:calendar=c.execute("SELECT * FROM grey_source_refresh WHERE source='calendar'").fetchone()
        return {'status':'degraded' if calendar and calendar['reason'] else 'ok','results':results,
                'calendar':dict(calendar) if calendar else None,
                'sessions':[{'session_id':r['session_id'],'trade_date':r['trade_date'],'due_at':r['due_at'],
                             'data_status':r['data_status'],'delivery_status':r['delivery_status'],'reason':r['reason']} for r in self.store.rows()]}

    def run_loop(self,stop,max_cycles=None):
        from .service import singleton_lock,write_json
        count=0;last=None
        # A dedicated process keeps IMAP/PDF/model work out of the deadline path.
        with singleton_lock(self.root/'.grey.lock') as acquired:
            if not acquired:return {'status':'busy'}
            while not stop.is_set():
                try:last=self.cycle()
                except Exception as error:last={'status':'failed','reason':'grey_cycle_'+type(error).__name__}
                count+=1
                write_json(self.root/'health.json',{'cycles':count,'last':last,'checked_at':datetime.now(HK).isoformat()})
                if max_cycles is not None and count>=max_cycles:break
                wait=self.settings.scan_seconds
                current=datetime.now(HK)
                future=[(timestamp(r['due_at'])-current).total_seconds() for r in self.store.rows() if r['delivery_status']=='pending']
                if any(0<n<wait for n in future):wait=min(n for n in future if n>0)
                stop.wait(max(.05,wait))
        if self.executor:self.executor.shutdown(wait=False,cancel_futures=True)
        return {'status':'stopped','cycles':count,'last':last}


def configured_service(settings):
    from .delivery import DEFAULT_RECIPIENT,load_private_config
    from .mail_reply import MailReplyStore
    from .mobile import MobileWorkflow
    if not settings.private_config or not settings.reply_db:raise ValueError('Private config and shared reply ledger required')
    replies=MailReplyStore(settings.reply_db,[DEFAULT_RECIPIENT])
    flow=MobileWorkflow(replies,load_private_config(settings.private_config))
    return GreyService(settings,flow)
