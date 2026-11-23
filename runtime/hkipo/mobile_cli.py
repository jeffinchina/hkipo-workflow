"""Explicit command entry for local phone-mail intake and private drafts."""
from pathlib import Path
import json
from .delivery import DEFAULT_RECIPIENT, DeliveryError, load_private_config
from .mail_reply import MailReplyStore
from .mail_transport import IntakeError,poll_replies
from .mobile import MobileWorkflow

SAFE_CODES={'private_config_unavailable','mobile_draft_account_changed','reply_mailbox_must_be_author_account',
            'specific_reminder_send_authorization_required','email_configuration_incomplete',
            'status_receipt_authorization_required','wechat_configuration_incomplete',
            'explicit_mailbox_allowlist_required','imap_intake_failed','imap_login_failed',
            'imap_search_failed','imap_readonly_folder_unavailable','imap_fetch_failed',
            'imap_body_missing_or_oversized','imap_header_fetch_failed','imap_header_missing',
            'unsafe_imap_search_value','invalid_imap_uid'}


def run(args):
    try:
        config=load_private_config(args.private_config)
        replies=MailReplyStore(args.db,[DEFAULT_RECIPIENT])
        flow=MobileWorkflow(replies,config)
        if args.action=='register':
            if not args.event:return {'status':'invalid_arguments','reason':'event_required'}
            return {'status':'registered','event_sha256':flow.register_event(json.loads(Path(args.event).read_text()))}
        if args.action=='poll':return poll_replies(replies,config.get('email',{}))
        if args.action=='reminder':
            if not args.event_id:return {'status':'invalid_arguments','reason':'event_id_required'}
            return flow.send_reminder(args.event_id,authorized=args.authorize_send)
        if not args.raw_hash:return {'status':'invalid_arguments','reason':'raw_hash_required'}
        if args.action=='receipt':return flow.send_receipt(args.raw_hash,authorized=args.authorize_send)
        if not args.out:return {'status':'invalid_arguments','reason':'out_required'}
        if args.action=='draft':
            return flow.process(args.raw_hash,args.out,thumb_media_id=args.thumb_media_id,layout_approval=args.layout_approval,
                                cover_path=getattr(args,'cover_path',None),cover_authorized=getattr(args,'authorize_cover',False))
        row=next((r for r in replies.pending() if r['raw_hash']==args.raw_hash),None)
        if row is None:return {'status':'not_pending'}
        return {'status':'preview_ready','manifest':flow.preview(row,args.out)}
    except (DeliveryError,IntakeError) as exc:
        # Both exception classes are safe code-only boundaries.
        code=str(exc)
        return {'status':'blocked','reason':code if code in SAFE_CODES else 'mobile_input_or_configuration_invalid'}
    except Exception:
        return {'status':'blocked','reason':'mobile_input_or_configuration_invalid'}
