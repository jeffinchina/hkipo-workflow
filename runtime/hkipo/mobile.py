"""Author-owned phone replies to private WeChat drafts, without a Word hop.

Network effects are explicit methods, never import-time behavior. Events and
original images are registered separately from the author's prose. Unknown
draft mutations stay unknown until manually reconciled, including after crashes.
"""
from email.message import EmailMessage
from email.policy import SMTP
import hashlib
import html
import json
import re
from pathlib import Path
from .delivery import (DEFAULT_RECIPIENT, DeliveryError, WeChatClient,
                       fingerprint, smtp_send, upload_personal_qr, wechat_body_preview)
from .render import article_html, article_text
from .models import utcnow


class MobileWorkflow:
    def __init__(self, replies, config, *, smtp_sender=None, wechat_client=None):
        self.replies=replies; self.config=config
        self.smtp_sender=smtp_sender or smtp_send; self.wechat=wechat_client or WeChatClient()
        with replies.connect() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS mobile_events(event_id TEXT PRIMARY KEY,payload TEXT NOT NULL,payload_hash TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS mobile_alerts(delivery_key TEXT PRIMARY KEY,event_id TEXT NOT NULL,message_id TEXT NOT NULL,status TEXT NOT NULL,receipt TEXT);
            CREATE TABLE IF NOT EXISTS mobile_draft_receipts(raw_hash TEXT PRIMARY KEY,draft_id TEXT NOT NULL,receipt TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS mobile_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS mobile_status_mail(raw_hash TEXT PRIMARY KEY,message_id TEXT NOT NULL,status TEXT NOT NULL);
            ''')
            appid=config.get('wechat',{}).get('appid')
            if appid:
                account=hashlib.sha256(appid.encode()).hexdigest()
                prior=c.execute("SELECT value FROM mobile_settings WHERE key='wechat_account'").fetchone()
                if prior and prior[0]!=account:raise DeliveryError('mobile_draft_account_changed')
                c.execute("INSERT OR IGNORE INTO mobile_settings VALUES('wechat_account',?)",(account,))

    def register_event(self,event):
        """Register reviewed source material; changed images are a new event revision.

        An event id denotes issuer + actual IPO episode, not just a stock code.
        The caller must already have checked the table labels and core numbers.
        """
        required=('event_id','stock_code','issuer','episode','source_url','source_time','first_seen','brief','images')
        if not isinstance(event,dict) or any(k not in event or (k!='images' and not event[k]) for k in required):
            raise ValueError('Incomplete reviewed allotment event')
        kind=event.get('notification_kind')
        if kind is not None and kind not in {'allotment_writing','allotment_notice','exception_notice'}:
            raise ValueError('Unsupported notification kind')
        if (kind is None and event.get('source_reviewed') is not True) or not str(event['stock_code']).isdigit():
            raise ValueError('Source review and exact security identity required')
        if not isinstance(event['images'],list) or len(event['images'])>4 or (kind is None and not event['images']):raise ValueError('Expected one to four original source images')
        for item in event['images']:
            path=Path(item['path'])
            if path.suffix.lower() not in {'.jpg','.jpeg','.png'}:raise ValueError('Unsupported source image')
            data=path.read_bytes()
            if not data or len(data)>=5_000_000 or hashlib.sha256(data).hexdigest()!=item['sha256']:
                raise ValueError('Original source image changed or oversized')
        payload=json.dumps(event,ensure_ascii=False,sort_keys=True)
        digest=fingerprint(event)
        with self.replies.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            prior=c.execute('SELECT payload_hash FROM mobile_events WHERE event_id=?',(event['event_id'],)).fetchone()
            if prior and prior[0]!=digest:raise ValueError('Registered event is immutable; use a source revision')
            c.execute('INSERT OR IGNORE INTO mobile_events VALUES(?,?,?)',(event['event_id'],payload,digest))
        return digest

    def event(self,event_id):
        with self.replies.connect() as c:
            row=c.execute('SELECT payload FROM mobile_events WHERE event_id=?',(event_id,)).fetchone()
        if not row:raise ValueError('Unregistered source event')
        event=json.loads(row[0])
        # Recheck original files every time, including before any transport.
        self.register_event(event)
        return event

    def reminder(self,event_id):
        event=self.event(event_id)
        email=self.config.get('email',{})
        sender=email.get('user','')
        if sender.casefold() not in self.replies.allowed:raise DeliveryError('reply_mailbox_must_be_author_account')
        key=fingerprint(['allotment-reminder',event,email.get('user'),DEFAULT_RECIPIENT])
        mid=f'<hkipo-reply-{key}@local.invalid>'
        msg=EmailMessage(policy=SMTP)
        msg['From']=sender;msg['To']=DEFAULT_RECIPIENT;msg['Reply-To']=sender
        label='【流程验收·历史回放】' if event.get('replay') is True else {'allotment_writing':'【出签写作提醒】','allotment_notice':'【股份分配公告】','exception_notice':'【发行变动公告】'}.get(event.get('notification_kind'),'【出签资料】')
        msg['Subject']=f"{label}{event['issuer']} {event['stock_code']}"
        msg['Message-ID']=mid
        text=(f"{event['issuer']}（{event['stock_code']}）\n\n{event['brief']}\n\n"
              f"来源：{event['source_url']}\n来源时间：{event['source_time']}\n首次发现：{event['first_seen']}\n"+
              ('预计出签日写作提醒，请查看券商通知后回复。\n\n' if event.get('notification_kind')=='allotment_writing' else '请核对上述来源资料后回复。\n\n')+
              "在手机上回复本邮件，填写以下三处即可。正文保留你的原话，系统补文末和个人二维码，只生成公众号草稿。\n\n"
              "标题：填写标题\n正文：\n填写你想写的内容\n【正文结束】\n\n"
              "可附最多4张PNG/JPEG新图片，每张小于5MB；无图也可回复。请勿转发后新建邮件，以免丢失股票关联。")
        if event.get('replay') is True:
            text='这是历史资料的手机回复流程验收，不是当前新股出签提醒；回复后也仅生成未发表草稿。\n\n'+text
        msg.set_content(text)
        msg.add_alternative('<div style="font-size:16px;line-height:2;white-space:pre-wrap">'+html.escape(text)+'</div>',subtype='html')
        return key,mid,msg

    def send_reminder(self,event_id,*,authorized=False):
        if authorized is not True:raise DeliveryError('specific_reminder_send_authorization_required')
        if not all(self.config.get('email',{}).get(k) for k in ('smtp_host','smtp_port','user','password')):
            raise DeliveryError('email_configuration_incomplete')
        key,mid,msg=self.reminder(event_id)
        with self.replies.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            prior=c.execute('SELECT status FROM mobile_alerts WHERE delivery_key=?',(key,)).fetchone()
            if prior:
                if prior[0]=='confirmed':
                    c.execute('INSERT OR IGNORE INTO reply_reminders VALUES(?,?)',(mid,event_id))
                return {'status':'already_confirmed' if prior[0]=='confirmed' else 'needs_reconciliation','delivery_key':key}
            c.execute('INSERT INTO mobile_alerts VALUES(?,?,?,?,NULL)',(key,event_id,mid,'unknown'))
        self.replies.remember_reminder(msg.as_bytes(),event_id)
        try:
            receipt=self.smtp_sender(self.config['email'],msg,DEFAULT_RECIPIENT)
            if receipt.get('smtp_accepted') is not True or receipt.get('message_id')!=mid:raise DeliveryError('invalid_reminder_receipt')
            with self.replies.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                c.execute("UPDATE mobile_alerts SET status='confirmed',receipt=? WHERE delivery_key=?",(json.dumps({'smtp_accepted':True,'message_id':mid}),key))
                c.execute('INSERT OR IGNORE INTO reply_reminders VALUES(?,?)',(mid,event_id))
            return {'status':'confirmed','delivery_key':key,'message_id':mid}
        except Exception:
            return {'status':'needs_reconciliation','delivery_key':key}

    def _reply_event(self,row):
        event=dict(self.event(row['event_id']))
        attached=json.loads(row.get('attachments_json') or '[]')
        # Only newly submitted author attachments belong in the article.
        # Never inherit old reminder/source distribution pictures.
        event['images']=attached
        for item in event['images']:
            data=Path(item['path']).read_bytes()
            if hashlib.sha256(data).hexdigest()!=item['sha256'] or len(data)>=5_000_000:
                raise ValueError('Original source image changed or oversized')
        return event

    def preview(self,row,directory):
        event=self._reply_event(row)
        # Split at author paragraph boundaries only; single newlines stay in the
        # same paragraph. This permits exact trailing fixed-footer de-duplication
        # without altering the persisted author original.
        from .render import CTA,RISK
        prose=row['body']
        # Remove only a trailing exact canonical footer supplied by the author;
        # retain every other word, including quotations in the middle.
        trimmed=prose.rstrip()
        if trimmed.endswith(RISK):
            prefix=trimmed[:-len(RISK)].rstrip()
            if prefix.endswith(CTA):prose=prefix[:-len(CTA)].rstrip()
        def role_for(part):
            text=part.strip()
            if re.fullmatch(r'[一二三四五]、(?:基本情况|财务情况|发行情况|估值分析|最后总结)|(?:营收|净利润|净亏损|现金流)(?:[（(][^（）()]+[）)])?',text):return 'heading'
            if re.match(r'^(?:申购时间|发行价格|每手股数|入场费|发行数量|市值|绿鞋|发行方案|保荐人|基石)：',text):return 'field'
            return 'author'
        article={'title':row['title'],'blocks':[{'role':role_for(part),'text':part,'fact_ids':[]}
                 for part in prose.split('\n\n')]}
        body=article_html(article)
        # Tables sit immediately after the author's body, before the fixed footer.
        tables=''.join('<p style="margin:0 0 16px"><img alt="甲乙组原始分配图" src="hkipo-image:'+str(i)+'" style="width:100%;height:auto"/></p>' for i in range(len(event['images'])))
        # The canonical appended CTA is last; an author may quote the same words
        # earlier and that quote must not move tables into their paragraph.
        marker=(body.index('data-branding="article-cta"') if 'data-branding="article-cta"' in body
                else body.rindex(html.escape(CTA)))
        paragraph=body.rfind('<p ',0,marker)
        body=body[:paragraph]+tables+body[paragraph:]
        root=Path(directory);root.mkdir(parents=True,exist_ok=True)
        (root/'article.html').write_text(body,encoding='utf-8')
        (root/'author.txt').write_text(row['body'],encoding='utf-8')
        (root/'article.txt').write_text(article_text(article),encoding='utf-8')
        import shutil
        for i,item in enumerate(event['images']):
            shutil.copyfile(item['path'],root/(f'original-{i}'+Path(item['path']).suffix))
        manifest={'event_id':event['event_id'],'version':row['version'],'raw_hash':row['raw_hash'],
                  'author_sha256':hashlib.sha256(row['body'].encode()).hexdigest(),
                  'html_sha256':hashlib.sha256(body.encode()).hexdigest(),
                  'source_images':[i['sha256'] for i in event['images']],
                  'source_event_sha256':fingerprint(event),'requires_word':False,'created_at':utcnow()}
        try:
            from .render import write_docx
            write_docx(article,root/'article.docx')
            manifest['word_archive']='complete'
        except Exception as exc:
            manifest['word_archive']='background_failed:'+type(exc).__name__
        # Standalone local preview keeps source originals. Never submitted as an
        # author entry UI, and no public service is exposed by this helper.
        import base64
        visible=body
        for i,item in enumerate(event['images']):
            mime='png' if Path(item['path']).suffix.lower()=='.png' else 'jpeg'
            visible=visible.replace('hkipo-image:'+str(i),'data:image/'+mime+';base64,'+base64.b64encode(Path(item['path']).read_bytes()).decode())
        (root/'preview.html').write_text('<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><body style="max-width:660px;margin:24px auto;padding:0 12px;"><h1 style="font-size:22px">'+html.escape(row['title'])+'</h1>'+visible+'</body>',encoding='utf-8')
        manifest['preview_sha256']=hashlib.sha256((root/'preview.html').read_bytes()).hexdigest()
        (root/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
        return manifest

    def process(self,raw_hash,directory,*,thumb_media_id=None,layout_approval=None,
                cover_path=None,cover_authorized=False,cover_sha256=None):
        pending=next((r for r in self.replies.pending() if r['raw_hash']==raw_hash),None)
        if not pending:return {'status':'not_pending'}
        manifest=self.preview(pending,directory)
        if isinstance(layout_approval,(str,Path)):
            try:layout_approval=json.loads(Path(layout_approval).read_text())
            except (OSError,ValueError):layout_approval=None
        keys=('raw_hash','version','html_sha256','preview_sha256','author_sha256','source_event_sha256')
        if (not isinstance(layout_approval,dict) or layout_approval.get('confirmed') is not True
                or any(layout_approval.get(k)!=manifest[k] for k in keys)):
            return {'status':'needs_layout_acceptance'}
        event=self._reply_event(pending)
        cover=None
        if thumb_media_id is None:
            if cover_authorized is not True or cover_path is None:return {'status':'needs_cover'}
            path=Path(cover_path).resolve()
            # The default mobile path can reuse only a previously reviewed
            # original table, never upload an arbitrary local file from mail.
            approved=next((i for i in event['images'] if Path(i['path']).resolve()==path),None)
            if approved is None and (not isinstance(cover_sha256,str) or
                    hashlib.sha256(path.read_bytes()).hexdigest()!=cover_sha256):return {'status':'needs_cover'}
            cover=(path.name,path.read_bytes())
        elif not isinstance(thumb_media_id,str) or not thumb_media_id:return {'status':'needs_cover'}
        body=(Path(directory)/'article.html').read_text()
        preview=wechat_body_preview(body)
        if len(pending['title'])>32 or len(preview)>=20000:return {'status':'content_limits_exceeded'}
        cfg=self.config.get('wechat',{})
        if not all(cfg.get(k) for k in ('appid','appsecret')):raise DeliveryError('wechat_configuration_incomplete')
        with self.replies.connect() as c:
            if c.execute("SELECT 1 FROM reply_messages WHERE event_id=? AND status='unknown'",(pending['event_id'],)).fetchone():
                return {'status':'needs_reconciliation_or_superseded'}
        # Token acquisition is read-only. Configuration/IP failures must not
        # reserve an uncertain draft mutation and strand an otherwise valid reply.
        try:
            token=self.wechat.token(cfg)
        except Exception as exc:
            code=str(exc)
            safe=code if re.fullmatch(r'wechat_api_error_[0-9]{1,8}',code) else 'wechat_token_unavailable'
            return {'status':'needs_configuration','reason':safe}
        row=self.replies.reserve(raw_hash)
        if row is None:return {'status':'needs_reconciliation_or_superseded'}
        try:
            cover_id=thumb_media_id or self.wechat.cover(token,*cover)
            body=upload_personal_qr(body,self.wechat,token)
            for i,item in enumerate(event['images']):
                url=self.wechat.inline_image(token,Path(item['path']).name,Path(item['path']).read_bytes())
                body=body.replace('hkipo-image:'+str(i),url)
            if len(body)>=20000 or len(body.encode())>=1_000_000:
                raise DeliveryError('wechat_content_limits_exceeded')
            from .render_profile import draft_metadata
            article={'article_type':'news','title':row['title'],'content':body,'thumb_media_id':cover_id,**draft_metadata()}
            if row['draft_id']:
                response=self.wechat.call('/cgi-bin/draft/update',{'access_token':token},{'media_id':row['draft_id'],'index':0,'articles':article})
                if response.get('errcode') not in (0,'0'):raise DeliveryError('draft_update_receipt_missing')
                draft_id=row['draft_id'];operation='update'
            else:
                response=self.wechat.call('/cgi-bin/draft/add',{'access_token':token},{'articles':[article]})
                draft_id=response.get('media_id');operation='add'
                if not isinstance(draft_id,str) or not draft_id:raise DeliveryError('draft_add_receipt_missing')
            receipt={'draft_id':draft_id,'operation':operation,'version':row['version'],'raw_hash':raw_hash,'html_sha256':hashlib.sha256(body.encode()).hexdigest(),'ui_verified':False}
            with self.replies.connect() as c:
                c.execute('INSERT INTO mobile_draft_receipts VALUES(?,?,?)',(raw_hash,draft_id,json.dumps(receipt)))
            if not self.replies.complete(raw_hash,draft_id):raise DeliveryError('draft_completion_fence_failed')
            return {'status':'confirmed','receipt':receipt}
        except Exception:
            return {'status':'needs_reconciliation','raw_hash':raw_hash}

    def send_receipt(self,raw_hash,*,authorized=False):
        """One status receipt for a confirmed author draft, linked to its thread.

        The caller invokes this only as part of the requested reply→draft flow.
        A transport timeout is persisted and is never blindly resent.
        """
        if authorized is not True:raise DeliveryError('status_receipt_authorization_required')
        with self.replies.connect() as c:
            row=c.execute("SELECT m.*,r.receipt FROM reply_messages m JOIN mobile_draft_receipts r USING(raw_hash) WHERE m.raw_hash=? AND m.status='complete'",(raw_hash,)).fetchone()
        if row is None:return {'status':'draft_not_confirmed'}
        cfg=self.config.get('email',{})
        if not all(cfg.get(k) for k in ('smtp_host','smtp_port','user','password')):raise DeliveryError('email_configuration_incomplete')
        mid=f'<hkipo-status-{raw_hash}@local.invalid>'
        msg=EmailMessage(policy=SMTP);msg['From']=cfg['user'];msg['To']=DEFAULT_RECIPIENT;msg['Reply-To']=cfg['user']
        msg['Message-ID']=mid;msg['In-Reply-To']=row['message_id']
        with self.replies.connect() as c:
            roots=[r[0] for r in c.execute('SELECT message_id FROM reply_reminders WHERE event_id=? ORDER BY rowid DESC LIMIT 1',(row['event_id'],))]
        msg['References']=' '.join(roots+[row['message_id']])
        msg['Subject']='【公众号草稿已更新】'+row['title']
        msg.set_content(f"《{row['title']}》第{row['version']}版已存入公众号草稿箱，未公开发表。\n\n请在公众号草稿箱预览后发布。正文按你的原文保留，已补固定文末和个人微信二维码。\n\n要修改时可继续回复本邮件：\n标题：填写标题\n正文：\n填写完整新正文\n【正文结束】\n")
        with self.replies.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            prior=c.execute('SELECT status FROM mobile_status_mail WHERE raw_hash=?',(raw_hash,)).fetchone()
            if prior:return {'status':'already_confirmed' if prior[0]=='confirmed' else 'needs_reconciliation'}
            c.execute('INSERT INTO mobile_status_mail VALUES(?,?,?)',(raw_hash,mid,'unknown'))
        self.replies.remember_reminder(msg.as_bytes(),row['event_id'])
        try:
            receipt=self.smtp_sender(cfg,msg,DEFAULT_RECIPIENT)
            if receipt.get('smtp_accepted') is not True or receipt.get('message_id')!=mid:raise DeliveryError('invalid_status_receipt')
            with self.replies.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                c.execute("UPDATE mobile_status_mail SET status='confirmed' WHERE raw_hash=?",(raw_hash,))
                c.execute('INSERT OR IGNORE INTO reply_reminders VALUES(?,?)',(mid,row['event_id']))
            return {'status':'confirmed','message_id':mid}
        except Exception:return {'status':'needs_reconciliation'}
