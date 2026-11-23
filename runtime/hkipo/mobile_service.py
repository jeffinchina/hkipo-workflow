"""Bounded local phone-mail worker. Monitoring and writing stay separate.

No model, network, mailbox read or delivery happens at import. Production remains
disabled until the actual mail→draft UI acceptance is recorded. Acceptance mode
is event-allowlisted; it is not a way to broadcast old historical samples.
"""
from dataclasses import dataclass,field
from datetime import datetime,timezone
import json
from pathlib import Path
from .delivery import DEFAULT_RECIPIENT,load_private_config
from .mail_reply import MailReplyStore
from .mail_transport import poll_replies
from .mobile import MobileWorkflow
from .mobile_layout import approve_layout
from .service import singleton_lock


@dataclass
class MobileSettings:
    data_dir:str
    private_config:str
    template_approval:str
    enable_deliveries:bool=False
    acceptance_mode:bool=False
    accepted_event_ids:list=field(default_factory=list)
    thumb_media_id:str|None=None
    table_cover_authorized:bool=False
    default_cover_path:str|None=None
    default_cover_sha256:str|None=None
    scan_seconds:int=60
    max_drafts:int=3
    official_state_db:str|None=None
    official_snapshot_dir:str|None=None
    research_root:str|None=None
    grey_settings:str|None=None
    def __post_init__(self):
        if any(type(getattr(self,k)) is not bool for k in ('enable_deliveries','acceptance_mode','table_cover_authorized')):
            raise ValueError('Delivery switches must be JSON booleans')
        if (not isinstance(self.accepted_event_ids,list) or
                any(not isinstance(e,str) or not e for e in self.accepted_event_ids)):
            raise ValueError('Acceptance events require an explicit string list')
        if self.scan_seconds<15 or not 1<=self.max_drafts<=10:raise ValueError('Invalid bounded mobile settings')


class MobileService:
    def __init__(self,settings,*,poller=None,layout_checker=None,flow_factory=None):
        self.settings=settings;self.root=Path(settings.data_dir).resolve()
        self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.poller=poller or poll_replies;self.layout_checker=layout_checker or approve_layout
        self.flow_factory=flow_factory or MobileWorkflow

    def cycle(self):
        with singleton_lock(self.root/'.mobile.lock') as acquired:
            if not acquired:return {'status':'busy'}
            settings=self.settings
            config=load_private_config(settings.private_config)
            replies=MailReplyStore(self.root/'intake.sqlite3',[DEFAULT_RECIPIENT])
            flow=self.flow_factory(replies,config)
            if settings.enable_deliveries:
                profile=json.loads(Path(settings.template_approval).read_text())
                if not settings.acceptance_mode and profile.get('production_accepted') is not True:
                    return {'status':'blocked','reason':'real_mail_and_wechat_acceptance_required'}
            grey=None;grey_result=None
            if settings.enable_deliveries and settings.grey_settings:
                from .grey_service import GreyService,GreySettings
                grey=GreyService(GreySettings(**json.loads(Path(settings.grey_settings).read_text())),flow)
                # Fixed-deadline reminders run before slower mailbox reads.
                grey_result=grey.cycle()
            # Receive only registered reminders; no inbox-wide content reads.
            incoming=self.poller(replies,config.get('email',{}))
            alerts=[];drafts=[]
            if settings.enable_deliveries:
                from .reminders import ReminderStore
                from .notices import NoticeBridge
                reminders=ReminderStore(self.root/'reminders.sqlite3')
                if settings.official_state_db:
                    from .notice_intake import dispatch_official,schedule_ready_offers
                    alerts.extend(dispatch_official(settings.official_state_db,flow,settings.official_snapshot_dir,
                                                   allotment_handler=grey.retain_notice if grey else None))
                    if settings.research_root:
                        alerts.extend(schedule_ready_offers(settings.official_state_db,settings.research_root,reminders,datetime.now(timezone.utc)))
                alerts.extend(NoticeBridge(flow,reminders).send_due())
                with replies.connect() as c:
                    events=[r[0] for r in c.execute('SELECT event_id FROM mobile_events ORDER BY rowid')]
                for eid in events:
                    event=flow.event(eid)
                    if event.get('notification_kind'):continue
                    if settings.acceptance_mode and eid not in settings.accepted_event_ids:continue
                    if event.get('replay') and eid not in settings.accepted_event_ids:continue
                    alerts.append(flow.send_reminder(eid,authorized=True))
            for row in replies.pending()[:settings.max_drafts]:
                eid=row['event_id']
                if settings.acceptance_mode and eid not in settings.accepted_event_ids:continue
                directory=self.root/'replies'/row['raw_hash']
                flow.preview(row,directory)
                if not settings.enable_deliveries:
                    drafts.append({'status':'preview_ready_delivery_disabled','raw_hash':row['raw_hash']});continue
                layout=self.layout_checker(directory,settings.template_approval)
                if layout.get('status')!='approved_local_layout':
                    drafts.append({'status':layout.get('status'),'raw_hash':row['raw_hash']});continue
                event=flow.event(eid)
                cover=(event['images'][0]['path'] if settings.table_cover_authorized and event['images']
                       else settings.default_cover_path)
                result=flow.process(row['raw_hash'],directory,thumb_media_id=settings.thumb_media_id,
                                    layout_approval=layout['approval'],cover_path=cover,
                                    cover_authorized=settings.table_cover_authorized or bool(settings.default_cover_path),
                                    cover_sha256=settings.default_cover_sha256)
                if result.get('status')=='confirmed':result['mail_receipt']=flow.send_receipt(row['raw_hash'],authorized=True)
                drafts.append(result)
            # A prior confirmed draft with an unsent receipt must be recoverable
            # after process exit; the receipt ledger itself blocks unknown retry.
            if settings.enable_deliveries:
                with replies.connect() as c:
                    unsent=[(r[0],r[1]) for r in c.execute("SELECT m.raw_hash,m.event_id FROM reply_messages m LEFT JOIN mobile_status_mail s USING(raw_hash) WHERE m.status='complete' AND s.raw_hash IS NULL LIMIT 10")]
                for raw,eid in unsent:
                    if not settings.acceptance_mode or eid in settings.accepted_event_ids:
                        flow.send_receipt(raw,authorized=True)
            result={'status':'ok','incoming':incoming,'reminders':alerts,'drafts':drafts,'delivery_enabled':settings.enable_deliveries}
            if grey_result is not None:result['grey_analysis']=grey_result
            return result

    def run_loop(self,stop,max_cycles=None):
        cycles=0;last=None
        while not stop.is_set():
            try:last=self.cycle()
            except Exception:last={'status':'failed','reason':'mobile_cycle_failed'}
            cycles+=1
            # Persist only safe summaries; source material and author originals
            # remain in the private database, outside health logs.
            (self.root/'health.json').write_text(json.dumps({'cycles':cycles,'last':last},ensure_ascii=False,indent=2))
            if max_cycles is not None and cycles>=max_cycles:break
            stop.wait(self.settings.scan_seconds)
        return {'status':'stopped','cycles':cycles,'last':last}
