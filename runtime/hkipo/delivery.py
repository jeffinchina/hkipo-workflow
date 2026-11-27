"""Frozen, revalidated deliveries: article email and private WeChat drafts only.

No sender is invoked on import. Tokens/config are memory-only. Unknown outcomes
must be reconciled; SMTP does not provide end-to-end exactly-once delivery.
"""
from email.message import EmailMessage
from email.policy import SMTP
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import smtplib
import uuid
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
from .quality import validate_bundle
from .render import article_text, article_html, legacy_article_text, legacy_article_html, qr_bytes
from .visual import trusted_visuals
from .render_profile import LEGACY, defaults, draft_metadata

DEFAULT_RECIPIENT=os.environ.get('HKIPO_RECIPIENT', '')

class DeliveryError(RuntimeError):
    """Messages contain only safe codes, never transport exception bodies."""


class BeforeArticleDelivery(DeliveryError):
    """The transport has not attempted to deliver the email or write a draft."""


def sha(data):return hashlib.sha256(data).hexdigest()
def fingerprint(value):return sha(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode())
def read_json(path):return json.loads(Path(path).read_text(encoding='utf-8'))


def personal_qr_source():
    import base64
    return 'data:image/jpeg;base64,'+base64.b64encode(qr_bytes()).decode('ascii')


def wechat_body_preview(body):
    """Check visible HTML size before reserving/uploading anything."""
    source=personal_qr_source()
    if 'data:image/' in body.replace(source,''):
        raise DeliveryError('unexpected_inline_image')
    return body.replace(source,'https://mmbiz.qpic.cn/approved-personal-qr.jpg')


def upload_personal_qr(body,client,token):
    source=personal_qr_source()
    if source not in body:return body
    return body.replace(source,client.inline_image(token,'personal-qr.jpg',qr_bytes()))

def load_private_config(path):
    """Explicit opt-in read; caller supplies a private configuration path.
    Do not serialize the returned object or log its repr."""
    try:
        value=read_json(Path(path).expanduser())
        if not isinstance(value,dict):raise ValueError()
        return value
    except (OSError,ValueError,TypeError):
        raise DeliveryError('private_config_unavailable') from None


def verify_release(release):
    """Return frozen bytes after re-reading all hashes and actual PDF evidence."""
    try:
        if not isinstance(release,dict):release=read_json(release)
        required={'event_id','bundle_path','review_path','quality_path','output_dir','files_sha256','sourcesmanifest_path','bundle_sha256','review_sha256','visual_qa_path'}
        if not required.issubset(release):raise DeliveryError('release_fields_missing')
        if release.get('status_path'):
            current=read_json(release['status_path'])
            if current.get('status')!='ready_for_delivery' or current.get('request_id')!=release.get('request_id'):
                raise DeliveryError('release_is_not_current_request')
        bundle_bytes=Path(release['bundle_path']).read_bytes();review_bytes=Path(release['review_path']).read_bytes()
        if sha(bundle_bytes)!=release['bundle_sha256'] or sha(review_bytes)!=release['review_sha256']:
            raise DeliveryError('release_fact_hash_mismatch')
        bundle=json.loads(bundle_bytes);review=json.loads(review_bytes)
        if bundle['event_id']!=release['event_id']:raise DeliveryError('release_event_mismatch')
        if read_json(release['quality_path']).get('pass') is not True:raise DeliveryError('stored_quality_blocked')
        sources=read_json(release['sourcesmanifest_path'])
        if release.get('visual_registry') and sha(Path(release['visual_registry']).read_bytes())!=release.get('visual_registry_sha256'):
            raise DeliveryError('visual_approval_registry_changed')
        visual_paths,visual_hashes,_=trusted_visuals(release.get('visual_registry'),sources)
        for key in ('scoped_visual_registry', 'scope_approval_registry'):
            if release.get(key) and sha(Path(release[key]).read_bytes()) != release.get(key + '_sha256'):
                raise DeliveryError('scoped_approval_registry_changed')
        quality=validate_bundle(bundle,sources,review,visual_reviews=visual_paths,
                                trusted_visual_review_sha256s=visual_hashes,
                                source_mode=release.get('source_mode','legacy_strict'),
                                scoped_visual_registry=release.get('scoped_visual_registry'),
                                scope_approval_registry=release.get('scope_approval_registry'),
                                bundle_path=release['bundle_path'], review_path=release['review_path'])
        if release.get('status_path') and current.get('source_mode','legacy_strict')!=release.get('source_mode','legacy_strict'):
            raise DeliveryError('release_source_mode_changed')
        if quality.get('pass') is not True:raise DeliveryError('current_quality_blocked')
        out=Path(release['output_dir']).resolve(strict=True)
        files={}
        for name,digest in release['files_sha256'].items():
            if Path(name).name!=name or name in {'.','..'}:raise DeliveryError('unsafe_output_filename')
            path=(out/name).resolve(strict=True)
            if path.parent!=out:raise DeliveryError('output_symlink_escape')
            data=path.read_bytes()
            if sha(data)!=digest:raise DeliveryError('release_output_hash_mismatch')
            files[name]=data
        if not {'article.txt','article.html','article.docx'}.issubset(files):raise DeliveryError('article_or_attachment_missing')
        version=release.get('render_version',1)
        if version not in (1,2,3):raise DeliveryError('unknown_render_version')
        expected_text=(article_text if version in (2,3) else legacy_article_text)(bundle['article'])
        expected_html=(article_html(bundle['article'],profile=release['render_profile']) if version==3 else
                       article_html(bundle['article'],profile=LEGACY) if version==2 else legacy_article_html(bundle['article']))
        if files['article.txt'].decode()!=expected_text or files['article.html'].decode()!=expected_html:
            raise DeliveryError('rendered_body_does_not_match_bundle')
        if release.get('scoped_visual_registry') and sha(Path(release['visual_qa_path']).read_bytes()) != release.get('visual_qa_sha256'):
            raise DeliveryError('scoped_word_visual_qa_changed')
        qa=read_json(release['visual_qa_path'])
        if qa.get('confirmed') is not True or qa.get('docx_sha256')!=sha(files['article.docx']):
            raise DeliveryError('word_visual_qa_required')
        return {'event_id':release['event_id'],'article':bundle['article'],'text':expected_text,'html':expected_html,'files':files}
    except DeliveryError:raise
    except Exception:
        raise DeliveryError('release_invalid_or_unreadable') from None


def smtp_send(config,message,recipient):
    """Distinguish a connection/login failure from uncertainty after sendmail."""
    attempted=False;accepted=False
    try:
        with smtplib.SMTP_SSL(config['smtp_host'],int(config['smtp_port']),timeout=30) as smtp:
            smtp.login(config['user'],config['password'])
            attempted=True
            refused=smtp.sendmail(config['user'],[recipient],message.as_bytes(policy=SMTP))
            if refused:raise DeliveryError('smtp_recipient_refused')
            accepted=True
    except DeliveryError:raise
    except (smtplib.SMTPRecipientsRefused,smtplib.SMTPSenderRefused):
        raise BeforeArticleDelivery('smtp_envelope_refused') from None
    except Exception:
        if not attempted:raise BeforeArticleDelivery('smtp_connection_or_login_failed') from None
        if not accepted:raise DeliveryError('smtp_outcome_unconfirmed') from None
    return {'message_id':message['Message-ID'],'smtp_accepted':True}

class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise DeliveryError('wechat_redirect_rejected')

class WeChatClient:
    """Official endpoint allowlist. No request retries for mutations, no cache."""
    PATHS={'/cgi-bin/token','/cgi-bin/media/uploadimg','/cgi-bin/material/add_material','/cgi-bin/material/batchget_material','/cgi-bin/draft/add','/cgi-bin/draft/update','/cgi-bin/draft/get','/cgi-bin/draft/batchget','/cgi-bin/freepublish/batchget'}
    def __init__(self,requester=None):
        self.requester=requester or self._request
    def _request(self,method,url,body,headers):
        try:
            with build_opener(_NoRedirect()).open(Request(url,data=body,headers=headers,method=method),timeout=30) as response:
                data=response.read(2_000_001)
                if len(data)>2_000_000:raise DeliveryError('wechat_response_oversized')
                return json.loads(data)
        except DeliveryError:raise
        except Exception:raise DeliveryError('wechat_transport_outcome_unconfirmed') from None
    def call(self,path,params=None,payload=None,upload=None):
        if path not in self.PATHS:raise DeliveryError('wechat_endpoint_not_allowed')
        params=params or {};headers={};body=None;method='GET'
        if upload is not None:
            name,data=upload;boundary='hkipo-'+uuid.uuid4().hex
            # Only our approved safe filename, never arbitrary header text.
            safe='image'+Path(name).suffix.lower()
            if Path(safe).suffix not in {'.jpg','.jpeg','.png','.gif','.bmp'}:raise DeliveryError('image_type_unsupported')
            mime=mimetypes.guess_type(safe)[0] or 'application/octet-stream'
            body=(f'--{boundary}\r\nContent-Disposition: form-data; name="media"; filename="{safe}"\r\nContent-Type: {mime}\r\n\r\n'.encode()+data+f'\r\n--{boundary}--\r\n'.encode())
            headers['Content-Type']='multipart/form-data; boundary='+boundary;method='POST'
        elif payload is not None:
            body=json.dumps(payload,ensure_ascii=False).encode();headers['Content-Type']='application/json; charset=utf-8';method='POST'
        url='https://api.weixin.qq.com'+path+'?'+urlencode(params)
        try:result=self.requester(method,url,body,headers)
        except Exception:raise DeliveryError('wechat_transport_outcome_unconfirmed') from None
        if not isinstance(result,dict):raise DeliveryError('wechat_invalid_response')
        code=result.get('errcode',0)
        if code not in (0,'0',None):
            safe_code=str(code) if isinstance(code,(int,str)) and str(code).lstrip('-').isdigit() else 'unknown'
            raise DeliveryError('wechat_api_error_'+safe_code[:12])
        return result
    def token(self,config):
        result=self.call('/cgi-bin/token',{'grant_type':'client_credential','appid':config['appid'],'secret':config['appsecret']})
        token=result.get('access_token')
        if not isinstance(token,str) or not token:raise DeliveryError('wechat_token_missing')
        return token
    def cover(self,token,name,data):
        result=self.call('/cgi-bin/material/add_material',{'access_token':token,'type':'image'},upload=(name,data))
        media=result.get('media_id')
        if not isinstance(media,str) or not media:raise DeliveryError('wechat_cover_receipt_missing')
        return media
    def inline_image(self,token,name,data):
        if Path(name).suffix.lower() not in {'.png','.jpg','.jpeg'} or not data or len(data)>=5_000_000:raise DeliveryError('inline_image_invalid')
        if len(data)>=1_000_000:
            # Preserve the author's original file; generate only an upload copy.
            from io import BytesIO
            from PIL import Image, ImageOps
            try:
                with Image.open(BytesIO(data)) as source:
                    if source.width*source.height>40_000_000:raise ValueError('image dimensions')
                    image=ImageOps.exif_transpose(source).convert('RGB')
                    for scale in range(8):
                        for quality in (95,90,85):
                            out=BytesIO();image.save(out,format='JPEG',quality=quality,optimize=True)
                            candidate=out.getvalue()
                            if len(candidate)<1_000_000:break
                        if len(candidate)<1_000_000:break
                        image.thumbnail((max(1,int(image.width*.85)),max(1,int(image.height*.85))))
                    if len(candidate)>=1_000_000:raise ValueError('upload copy too large')
                    name='upload-copy.jpg';data=candidate
            except Exception:raise DeliveryError('inline_image_conversion_failed') from None
        result=self.call('/cgi-bin/media/uploadimg',{'access_token':token},upload=(name,data))
        url=result.get('url','');host=urlsplit(url).hostname or ''
        if urlsplit(url).scheme not in {'http','https'} or not (host=='mmbiz.qpic.cn' or host.endswith('.qpic.cn')):raise DeliveryError('wechat_image_receipt_invalid')
        return url

class Deliverer:
    def __init__(self,store,config_path=None,config=None,smtp_sender=None,wechat_client=None):
        if config is None and config_path is None:raise DeliveryError('explicit_private_config_required')
        self.store=store;self.config=config if config is not None else load_private_config(config_path)
        self.smtp_sender=smtp_sender or smtp_send;self.wechat=wechat_client or WeChatClient()

    def _reserve(self,key,event_id,channel):
        from .models import utcnow
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            uncertain=c.execute("SELECT delivery_key FROM deliveries WHERE event_id=? AND channel=? AND status='unknown' LIMIT 1",(event_id,channel)).fetchone()
            if uncertain:return {'status':'needs_reconciliation','delivery_key':uncertain['delivery_key'],'receipt':None}
            prior=c.execute('SELECT * FROM deliveries WHERE delivery_key=?',(key,)).fetchone()
            if prior:
                return {'status':'already_confirmed' if prior['status']=='confirmed' else 'needs_reconciliation','delivery_key':key,'receipt':json.loads(prior['receipt']) if prior['receipt'] else None}
            c.execute("INSERT INTO deliveries VALUES(?,?,?,'unknown',NULL,?)",(key,event_id,channel,utcnow()))
        return None

    def _retry_unsent(self,key,reason):
        # Only the reservation owner may call before attempting the side effect.
        # Other workers see an unknown reservation until this transaction ends.
        removed=self.store.release_unsent_delivery(key)
        return {'status':'retryable' if removed else 'needs_reconciliation','delivery_key':key,'error':reason}

    def email(self,release,recipient=DEFAULT_RECIPIENT,*,subject_prefix=''):
        frozen=verify_release(release)
        if not isinstance(recipient,str) or '\n' in recipient or '\r' in recipient or recipient.count('@')!=1:raise DeliveryError('invalid_recipient')
        if not isinstance(subject_prefix,str) or len(subject_prefix)>60 or '\n' in subject_prefix or '\r' in subject_prefix:raise DeliveryError('invalid_subject_prefix')
        cfg=self.config.get('email',{})
        if not all(cfg.get(k) for k in ('smtp_host','smtp_port','user','password')):raise DeliveryError('email_configuration_incomplete')
        subject=subject_prefix+frozen['article']['title']
        payload={'title':subject,'text':frozen['text'],'html':frozen['html'],'docx_sha256':sha(frozen['files']['article.docx'])}
        key=fingerprint([frozen['event_id'],'email',recipient,payload])
        message=EmailMessage(policy=SMTP);message['From']=cfg['user'];message['To']=recipient
        message['Subject']=subject;message['Message-ID']=f'<hkipo-{key}@local.invalid>'
        message.set_content(frozen['text'])
        body=frozen['html'];qr_source=personal_qr_source()
        if qr_source in body:
            # Mail clients commonly suppress data URLs. Embed the same frozen
            # original as a related CID image, without altering local artifacts.
            body=body.replace(qr_source,'cid:hkipo-personal-qr')
        message.add_alternative(body,subtype='html')
        if 'cid:hkipo-personal-qr' in body:
            message.get_payload()[-1].add_related(qr_bytes(),maintype='image',subtype='jpeg',
                cid='<hkipo-personal-qr>',filename='personal-qr.jpg',disposition='inline')
        message.add_attachment(frozen['files']['article.docx'],maintype='application',subtype='vnd.openxmlformats-officedocument.wordprocessingml.document',filename='article.docx')
        prior=self._reserve(key,frozen['event_id'],'email')
        if prior:return prior
        try:
            receipt=self.smtp_sender(cfg,message,recipient)
            if not isinstance(receipt,dict) or receipt.get('smtp_accepted') is not True or not receipt.get('message_id'):raise DeliveryError('smtp_receipt_invalid')
            safe_receipt={'message_id':str(receipt['message_id']),'smtp_accepted':True,'payload_sha256':fingerprint(payload)}
            self.store.finish_delivery(key,'confirmed',safe_receipt)
            return {'status':'confirmed','delivery_key':key,'receipt':safe_receipt}
        except BeforeArticleDelivery:
            return self._retry_unsent(key,'smtp_delivery_not_started')
        except Exception:
            # Reservation remains unknown even on an explicit rejection; no
            # automatic retry is safe once a transport has been entered.
            return {'status':'needs_reconciliation','delivery_key':key,'error':'email_outcome_unconfirmed'}

    def _existing_draft(self,event_id,account):
        with self.store.connect() as c:
            rows=c.execute("SELECT receipt FROM deliveries WHERE event_id=? AND channel='wechat_draft' AND status='confirmed' ORDER BY updated_at DESC",(event_id,)).fetchall()
        for row in rows:
            receipt=json.loads(row['receipt']) if row['receipt'] else {}
            if receipt.get('account_sha256')==account and receipt.get('media_id'):return receipt
        return None

    def draft(self,release,thumb_media_id=None,cover_path=None,cover_authorized=False,update_existing=False,
              author=None,need_open_comment=None,only_fans_can_comment=None):
        frozen=verify_release(release)
        cfg=self.config.get('wechat',{})
        if not all(cfg.get(k) for k in ('appid','appsecret')):raise DeliveryError('wechat_configuration_incomplete')
        metadata=draft_metadata(author,need_open_comment,only_fans_can_comment)
        if thumb_media_id is None and cover_path is None:thumb_media_id=defaults().get('thumb_media_id')
        cover=None
        if thumb_media_id is None:
            if cover_path is None or cover_authorized is not True:return {'status':'needs_cover'}
            try:
                path=Path(cover_path);cover=(path.name,path.read_bytes())
                if path.suffix.lower() not in {'.png','.jpg','.jpeg','.gif','.bmp'} or len(cover[1])>10*1024*1024:raise ValueError()
            except Exception:raise DeliveryError('cover_invalid') from None
        elif not isinstance(thumb_media_id,str) or not thumb_media_id:raise DeliveryError('cover_id_invalid')
        title=frozen['article']['title'];body=frozen['html']
        preview=wechat_body_preview(body)
        if len(title)>32 or len(preview)>=20000 or len(preview.encode())>=1_000_000:raise DeliveryError('wechat_content_limits_exceeded')
        account=sha(cfg['appid'].encode())
        existing=self._existing_draft(frozen['event_id'],account)
        payload={'title':title,'content':body,'cover':thumb_media_id if cover is None else sha(cover[1]),**metadata}
        key=fingerprint([frozen['event_id'],'wechat_draft',account,payload])
        # Compare the current confirmed draft, not any historical receipt: A→B→A
        # is another update, not a no-op just because A once succeeded.
        payload_hash=fingerprint(payload)
        if existing and existing.get('payload_sha256')==payload_hash:
            return {'status':'already_confirmed','delivery_key':key,'receipt':existing}
        if existing and not update_existing:return {'status':'update_requires_explicit_request','media_id':existing['media_id']}
        if existing:
            key=fingerprint([frozen['event_id'],'wechat_draft',account,payload,'replaces',existing['payload_sha256']])
        prior=self._reserve(key,frozen['event_id'],'wechat_draft')
        if prior:return prior
        try:
            token=self.wechat.token(cfg)
        except Exception:
            return self._retry_unsent(key,'wechat_token_unavailable')
        try:
            media_cover=thumb_media_id or self.wechat.cover(token,*cover)
            body=upload_personal_qr(body,self.wechat,token)
            article={'article_type':'news','title':title,'content':body,'thumb_media_id':media_cover,**metadata}
            if existing:
                media=existing['media_id']
                response=self.wechat.call('/cgi-bin/draft/update',{'access_token':token},{'media_id':media,'index':0,'articles':article})
                if response.get('errcode') not in (0,'0'):raise DeliveryError('wechat_update_receipt_missing')
                operation='update'
            else:
                response=self.wechat.call('/cgi-bin/draft/add',{'access_token':token},{'articles':[article]})
                media=response.get('media_id')
                if not isinstance(media,str) or not media:raise DeliveryError('wechat_draft_receipt_missing')
                operation='add'
            receipt={'media_id':media,'operation':operation,'account_sha256':account,'payload_sha256':fingerprint(payload),'format_ui_verified':False}
            self.store.finish_delivery(key,'confirmed',receipt)
            return {'status':'confirmed','delivery_key':key,'receipt':receipt}
        except Exception:
            return {'status':'needs_reconciliation','delivery_key':key,'error':'wechat_outcome_unconfirmed'}
