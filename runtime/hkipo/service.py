"""Local cycle orchestration. Discovery and slow writing have separate processes.

No scheduler, model call or external delivery is started on import. Safe defaults
only scan/classify; work requires explicit enable_writer or an injected worker.
"""
from contextlib import contextmanager
from dataclasses import dataclass,field,asdict
from datetime import datetime,timezone,timedelta
import fcntl
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urlsplit,parse_qs
from .classification import aggregate_evidence,title_evidence,classify_offer,classify_followup,apply_classification,OfficialDocumentEvidence
from .downloads import PDFDownloader,DownloadError
from .models import DocumentEvent
from .monitor import Monitor
from .sources import SourceError,TitleSearchSource,AggregateSource
from .store import StateStore
from .worker import Worker


def _now():return datetime.now(timezone.utc)
def _hash(data):return hashlib.sha256(data).hexdigest()

def private_write(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd,temp=tempfile.mkstemp(prefix='.'+path.name+'-',dir=path.parent)
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,'wb') as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())
        os.replace(temp,path)
    finally:
        if os.path.exists(temp):os.unlink(temp)

def write_json(path,value):private_write(path,json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2).encode())

@contextmanager
def singleton_lock(path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    handle=path.open('a+b');os.chmod(path,0o600)
    try:
        try:fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            yield False;return
        yield True
    finally:
        fcntl.flock(handle.fileno(),fcntl.LOCK_UN);handle.close()


def _sanitize(body,format):
    """Keep public response data, remove ephemeral session/cookie/token values.
    The manifest records both original and stored SHA, making changes explicit."""
    if not isinstance(body,str):raise SourceError('snapshot body must be text')
    value=body
    if format in {'json','legacy_aggregate'}:
        parsed=json.loads(body)
        def clean(o):
            if isinstance(o,dict):return {k:('[REDACTED]' if re.search(r'token|cookie|session|secret|password|viewstate',k,re.I) else clean(v)) for k,v in o.items()}
            if isinstance(o,list):return [clean(x) for x in o]
            return o
        # Preserve original response byte-for-byte if no sensitive keys occur.
        cleaned=clean(parsed)
        if cleaned!=parsed:value=json.dumps(cleaned,ensure_ascii=False)
    value=re.sub(r'(?i)(;jsessionid=)[^\s"\'<>?;]+',r'\1[REDACTED]',value)
    value=re.sub(r'(?i)((?:access_token|auth_token|sessionid|csrf_token|cookie|secret)=)[^&\s"\'<>]+',r'\1[REDACTED]',value)
    def hidden(match):
        tag=match[0]
        if re.search(r'type\s*=\s*["\']?hidden|ViewState|csrf|session',tag,re.I):
            return re.sub(r'(?i)(value\s*=\s*)(["\']).*?\2',r'\1"[REDACTED]"',tag)
        return tag
    value=re.sub(r'<input\b[^>]*>',hidden,value,flags=re.I)
    if format=='html':
        # Public listing tables need no executable scripts or account/session
        # metadata. Strip these from the retained body and record that its hash
        # differs from the original response, never pretend it is byte-exact.
        value=re.sub(r'<script\b[^>]*>.*?</script>', '[SCRIPT OMITTED]',value,flags=re.I|re.S)
        value=re.sub(r'<meta\b(?=[^>]*(?:csrf|token|session|cookie))[^>]*>', '[SESSION META OMITTED]',value,flags=re.I)
    return value

class SnapshotArchive:
    def __init__(self,root,allow_legacy_replay=False):
        self.root=Path(root).resolve();self.root.mkdir(parents=True,exist_ok=True,mode=0o700);os.chmod(self.root,0o700)
        self.allow_legacy_replay=allow_legacy_replay
    def _url(self,url,format):
        p=urlsplit(url)
        if p.scheme!='https' or p.username or p.password or p.port not in (None,443):raise SourceError('snapshot endpoint rejected')
        aggregate=p.hostname=='www2.hkexnews.hk' and re.fullmatch(r'/New-Listings/New-Listing-Information/(Main-Board|GEM)',p.path) is not None
        title=p.hostname=='www1.hkexnews.hk' and p.path=='/search/titleSearchServlet.do'
        if not (aggregate or title):raise SourceError('snapshot endpoint rejected')
        allowed={'sc_lang'} if aggregate else {'sortDir','sortByOptions','category','market','stockId','documentType','fromDate','toDate','title','searchType','t1code','t2Gcode','t2code','rowRange','lang'}
        if set(parse_qs(p.query))-allowed:raise SourceError('snapshot query fields rejected')
        if format=='legacy_aggregate' and not (aggregate and self.allow_legacy_replay):raise SourceError('legacy snapshot replay not enabled')
        if format not in {'html','json','legacy_aggregate'}:raise SourceError('snapshot format rejected')
        return url
    def save_result(self,result):
        if not result.complete or not result.responses:raise SourceError('complete raw response audit required')
        records=[]
        for response in result.responses:
            format=response['format'];url=self._url(response['url'],format);raw=response['body'];stored=_sanitize(raw,format)
            original_sha=_hash(raw.encode());stored_sha=_hash(stored.encode());ext='html' if format=='html' else 'json'
            path=self.root/'bodies'/(stored_sha+'.'+ext)
            if not path.is_file() or _hash(path.read_bytes())!=stored_sha:private_write(path,stored.encode())
            records.append({'url':url,'format':format,'path':str(path),'original_sha256':original_sha,'sha256':stored_sha,'sanitized':original_sha!=stored_sha,'captured_at':_now().isoformat()})
        safe_name=_hash(result.source.encode())[:20]
        manifest={'source':result.source,'complete':True,'count':result.count,'parse_audit':result.evidence,'responses':records,'saved_at':_now().isoformat()}
        write_json(self.root/'latest'/(safe_name+'.json'),manifest)
        return records
    def manifests(self):
        for path in sorted((self.root/'latest').glob('*.json')):yield json.loads(path.read_text())
    def evidence(self):
        evidence=[];errors=[]
        for manifest in self.manifests():
            for record in manifest['responses']:
                try:
                    data=Path(record['path']).read_bytes()
                    if _hash(data)!=record['sha256']:raise ValueError()
                    raw=data.decode();format=record['format']
                    if format=='html':evidence.extend(aggregate_evidence(raw,record['url']))
                    elif format=='json':evidence.extend(title_evidence(raw))
                    elif self.allow_legacy_replay:evidence.extend(self._legacy(raw,record))
                except Exception:errors.append({'source':manifest['source'],'error':'snapshot_hash_or_schema_invalid'})
        return evidence,errors
    def _legacy(self,raw,record):
        """Replay existing parsed snapshots honestly; never label as raw HTML."""
        data=json.loads(raw);out=[]
        for stock in data['stocks']:
            for key,role in [('announcement','announcement'),('prospectus','prospectus'),('allotment','allotment')]:
                if stock.get(key):out.append(OfficialDocumentEvidence(stock[key],str(stock['code']).zfill(5),stock['name'],role,'legacy_parsed_aggregate_replay',record['url'],record['sha256'],'legacy snapshot field:'+key,data['date']))
        return out

class _AuditedSource:
    def __init__(self,source,archive):self.source=source;self.archive=archive;self.name=source.name;self.timestamp_precision=getattr(source,'timestamp_precision',None)
    def fetch(self,start,end):
        result=self.source.fetch(start,end)
        self.archive.save_result(result) # before Monitor commits successful cutoff
        return result

@dataclass
class ServiceSettings:
    data_dir: str
    scan_seconds: float = 60
    overlap_hours: float = .25
    initial_days: int = 3
    aggregate_seconds: float = 900
    max_classification_groups: int = 10
    max_worker_groups: int = 2
    enable_writer: bool = False
    include_gem: bool = False
    allow_legacy_replay: bool = False
    skill_paths: dict = field(default_factory=dict)
    writer_options: dict = field(default_factory=dict)
    author_inputs: dict = field(default_factory=dict)
    auto_article_kinds: tuple = ('offer','allotment')
    grey_state_db: str|None = None
    def __post_init__(self):
        if self.scan_seconds<1 or self.aggregate_seconds<1 or self.overlap_hours<0 or self.initial_days<1:raise ValueError('Invalid service timing')
        if not 1<=self.max_classification_groups<=100 or not 1<=self.max_worker_groups<=100:raise ValueError('Unbounded service batch')

class LocalService:
    def __init__(self,settings,sources=None,downloader=None,worker=None,process_factory=None):
        self.settings=settings if isinstance(settings,ServiceSettings) else ServiceSettings(**settings)
        self.root=Path(self.settings.data_dir).resolve();self.root.mkdir(parents=True,exist_ok=True,mode=0o700);os.chmod(self.root,0o700)
        self.store=StateStore(self.root/'state.sqlite3');os.chmod(self.root/'state.sqlite3',0o600)
        self.archive=SnapshotArchive(self.root/'snapshots',self.settings.allow_legacy_replay)
        self.sources=list(sources) if sources is not None else [TitleSearchSource(),AggregateSource('Main-Board')]+([AggregateSource('GEM')] if self.settings.include_gem else [])
        self.downloader=downloader or PDFDownloader(self.root/'pdf',self.store)
        self.worker=worker
        self.process_factory=process_factory or multiprocessing.get_context('spawn').Process
        self._process=None
    def _state(self,part='scan'):
        path=self.root/(part+'-state.json')
        try:return json.loads(path.read_text())
        except FileNotFoundError:return {'last_source_scans':{},'classification_inputs':{},'download_failures':{}}
    def _persist(self,state,part='scan'):write_json(self.root/(part+'-state.json'),state)
    def scan_cycle(self,now=None):
        now=now or _now();state=self._state();due=[]
        for source in self.sources:
            interval=self.settings.aggregate_seconds if source.name.startswith('new-listings-') else self.settings.scan_seconds
            last=state['last_source_scans'].get(source.name)
            if last and (now-datetime.fromisoformat(last)).total_seconds()<interval:continue
            due.append(_AuditedSource(source,self.archive));state['last_source_scans'][source.name]=now.isoformat()
        if due:
            result=Monitor(self.store,due,overlap_hours=self.settings.overlap_hours,initial_days=self.settings.initial_days,interval_seconds=self.settings.scan_seconds).scan(now)
        else:result={'ok':True,'sources':[],'alerts':self.store.alerts(),'skipped':'not_due'}
        self._persist(state);return result
    def _events(self):
        with self.store.connect() as c:
            return [(json.loads(r['payload']),r['baseline']) for r in c.execute('SELECT payload,baseline FROM events')]
    def classify_cycle(self,now=None):
        now=now or _now();state=self._state('classification');evidence,errors=self.archive.evidence()
        all_events=self._events();active=[e for e,b in all_events if b in (0,2) and e['kind'] in {'offer','allotment','exception'}]
        if self.settings.grey_state_db and Path(self.settings.grey_state_db).is_file():
            # A current/future broker session may belong to an offer present at
            # startup. Confirm its IPO episode from complete official covers,
            # without promoting baseline documents or writing historical offers.
            import sqlite3
            with sqlite3.connect(self.settings.grey_state_db) as db:
                current={code for code,day,status in db.execute("SELECT json_extract(payload,'$.stock_code'),trade_date,delivery_status FROM grey_sessions")
                         if status not in ('skipped_history','superseded') and -7<=(datetime.fromisoformat(day).date()-now.date()).days<=60}
            existing={e['event_id'] for e in active}
            active += [e for e,b in all_events if b==1 and e['kind']=='offer' and e['stock_code'] in current and e['event_id'] not in existing]
        # Only actual official role-linked candidates are downloaded. Preliminary,
        # routine listings and random title keywords never spend model budget.
        official_urls={x.url for x in evidence}
        unconfirmed=[e['event_id'] for e in active if e['url'] not in official_urls]
        with self.store.connect() as c:
            for eid in unconfirmed:c.execute("UPDATE jobs SET status='needs_classification',reason='official_role_missing' WHERE event_id=? AND status='pending'",(eid,))
        active=[e for e in active if e['url'] in official_urls]
        groups={}
        for event in active:
            dates=sorted({x.public_date for x in evidence if x.url==event['url'] and x.stock_code==event['stock_code']})
            key=(event['stock_code'],event['language'],event['kind'],tuple(dates))
            groups.setdefault(key,[]).append(event)
        decisions=[]
        processed=0
        for key,events in sorted(groups.items()):
            code,language,kind,dates=key
            material=[e for e,_ in all_events if e['stock_code']==code and e['language']==language and e['kind']==kind and any(x.url==e['url'] and x.public_date in dates for x in evidence)]
            prior_context=[]
            if kind!='offer':
                for file in (self.root/'classification').glob('*.json'):
                    prior=json.loads(file.read_text())
                    if prior.get('decision')=='confirmed' and prior.get('prospectus_public_date') and prior.get('stock_code')==code:
                        prior_context.append((prior.get('episode_id'),prior.get('prospectus_public_date')))
            identity=_hash(json.dumps({'ids':sorted(e['event_id'] for e in material),'evidence':sorted({(x.url,x.role,x.public_date,x.stock_code,x.locator) for x in evidence if x.url in {e['url'] for e in material}}), 'classifier_rules':4, 'prior_ipo_episodes':sorted(set(prior_context))},sort_keys=True).encode())
            # Already processed input is not repeatedly downloaded every minute.
            if identity in state['classification_inputs']:continue
            if any(state['download_failures'].get(e['event_id']) and now<datetime.fromisoformat(state['download_failures'][e['event_id']]['retry_at']) for e in material):continue
            if processed>=self.settings.max_classification_groups:break
            processed+=1
            paths={};failed=False
            for event in material:
                old_failure=state['download_failures'].get(event['event_id'])
                if old_failure and now<datetime.fromisoformat(old_failure['retry_at']):failed=True;continue
                try:
                    downloaded=self.downloader.download(event['url']);paths[event['event_id']]=downloaded.path
                    state['download_failures'].pop(event['event_id'],None)
                    self.store.record_stage([event['event_id']],'downloaded',downloaded.to_dict())
                except Exception as error:
                    seconds=max(300,int(getattr(error,'retry_after',None) or 0)+1)
                    state['download_failures'][event['event_id']]={'retry_at':(now+timedelta(seconds=seconds)).isoformat(),'error':type(error).__name__}
                    failed=True
            if failed:continue
            if kind=='offer':decision=classify_offer(material,paths,evidence)
            else:
                offers=[]
                for file in (self.root/'classification').glob('*.json'):
                    previous=json.loads(file.read_text())
                    if previous.get('decision')=='confirmed' and previous.get('prospectus_public_date') and previous.get('stock_code')==code:offers.append(previous)
                matching=[]
                for event in material:
                    for offer in offers:
                        possible=classify_followup(event,paths[event['event_id']],evidence,offer)
                        if possible['decision']=='confirmed':matching.append(possible)
                if matching and len({d['episode_id'] for d in matching})==1:
                    decision=dict(matching[0])
                    decision['assignments']=list({a['event_id']:a for d in matching for a in d['assignments']}.values())
                    decision['audit']=[a for d in matching for a in d['audit']]
                else:decision={'decision':'needs_classification','reason':'prior_episode_missing_or_ambiguous','audit':[],'assignments':[]}
            decision['service_input_sha256']=identity
            write_json(self.root/'classification'/(identity+'.json'),decision)
            # Classification proves document/issuer type, not whether a date-only
            # late-recovery record was published after service startup. Keep
            # baseline=2 until the precise title source promotes it (or an
            # operator explicitly authorizes historical replay elsewhere).
            apply_classification(self.store,decision,promote_candidates=False)
            state['classification_inputs'][identity]={'decision':decision['decision'],'at':now.isoformat()}
            decisions.append({'decision':decision['decision'],'reason':decision['reason'],'input_sha256':identity})
        self._persist(state,'classification')
        return {'decisions':decisions,'snapshot_errors':errors,'download_failures':state['download_failures']}
    def work_cycle(self):
        if self.worker is None and not self.settings.enable_writer:return {'status':'disabled','results':[]}
        worker=self.worker or Worker(self.store,self.downloader,self.root/'research',self.settings.skill_paths,auto_article_kinds=self.settings.auto_article_kinds)
        results=worker.run_batch(self.settings.max_worker_groups,author_inputs=self.settings.author_inputs,**self.settings.writer_options)
        return {'status':'completed','results':results}
    def _health(self,result):
        for name,value in result.items():write_json(self.root/'health-components'/(name+'.json'),{'at':_now().isoformat(),'result':value})
        components={p.stem:json.loads(p.read_text()) for p in (self.root/'health-components').glob('*.json')}
        pipeline=self._state('classification')
        health={'at':_now().isoformat(),'alerts':self.store.alerts(),'external_delivery_enabled':False,'writer_enabled':self.settings.enable_writer,'components':components,'download_failures':pipeline.get('download_failures',{})}
        classification=components.get('classification',{}).get('result',{})
        health['pipeline_process_alive']=bool(self._process is not None and self._process.is_alive())
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE');self.store._worker_schema(c)
            attention=[dict(r) for r in c.execute("SELECT j.event_id,j.status,j.reason,j.updated_at FROM jobs j WHERE j.status IN ('blocked','failed')")]
            counts={r['status']:r['count'] for r in c.execute('SELECT status,COUNT(*) AS count FROM jobs GROUP BY status')}
        health['queue_counts']=counts
        health['queue_attention']=attention
        health['ok']=not health['alerts'] and not classification.get('snapshot_errors') and not health['download_failures'] and not attention
        write_json(self.root/'health.json',health);return health
    def cycle(self,scan=True,classify=False,work=False,now=None):
        # Scan and pipeline have different locks, so a slow writer never owns the
        # discovery lock. Combined mode is for bounded foreground/offline runs.
        result={}
        if scan:
            with singleton_lock(self.root/'locks/scan.lock') as acquired:
                if acquired:result['scan']=self.scan_cycle(now)
                else:result['scan']={'status':'busy'}
        if classify or work:
            with singleton_lock(self.root/'locks/pipeline.lock') as acquired:
                if acquired:
                    if classify:result['classification']=self.classify_cycle(now)
                    if work:result['work']=self.work_cycle()
                else:result['pipeline']={'status':'busy'}
        result['health']=self._health({k:v for k,v in result.items()})
        return result
    def dispatch_pipeline(self):
        if self._process is not None and self._process.is_alive():return {'status':'already_running'}
        if self._process is not None:self._process.join(timeout=0)
        self._process=self.process_factory(target=_pipeline_process,args=(asdict(self.settings),),daemon=False)
        self._process.start();return {'status':'started'}
    def run_loop(self,stop,max_cycles=None,dispatch=True):
        count=0
        with singleton_lock(self.root/'locks/monitor-loop.lock') as acquired:
            if not acquired:return {'status':'already_running','cycles':0}
            while not stop.is_set() and (max_cycles is None or count<max_cycles):
                self.cycle(scan=True,classify=False,work=False)
                if dispatch:self.dispatch_pipeline()
                count+=1
                if max_cycles is not None and count>=max_cycles:break
                stop.wait(self.settings.scan_seconds)
        return {'status':'stopped','cycles':count}


def _pipeline_process(settings):
    service=LocalService(settings)
    service.cycle(scan=False,classify=True,work=service.settings.enable_writer)
