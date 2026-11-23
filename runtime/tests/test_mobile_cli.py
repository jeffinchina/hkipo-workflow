import json
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
from hkipo.delivery import DEFAULT_RECIPIENT, DeliveryError
from hkipo.mail_reply import MailReplyStore
from hkipo.mail_transport import IntakeError
from hkipo.mobile_cli import run


class MobileCLITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.config=self.root/'fake-config.json'
        self.config.write_text(json.dumps({'email':{'smtp_host':'smtp.example.test','smtp_port':465,'user':DEFAULT_RECIPIENT,'password':'synthetic-secret'}}))
        self.db=self.root/'state.db'
    def tearDown(self):self.temp.cleanup()
    def args(self,**kwargs):
        values={'private_config':str(self.config),'db':str(self.db),'action':'poll','event':None,'event_id':None,
                'authorize_send':False,'raw_hash':None,'out':None,'thumb_media_id':None,'layout_approval':None}
        return SimpleNamespace(**(values|kwargs))
    def test_poll_without_registered_reminders_idle_without_connection(self):
        with patch('hkipo.mail_transport.imaplib.IMAP4_SSL',side_effect=AssertionError('must not connect')) as client:
            self.assertEqual(run(self.args()),{'status':'idle','checked':0,'results':[]})
            client.assert_not_called()
    def test_reminder_without_authorization_never_sends(self):
        with patch('hkipo.mobile.smtp_send',side_effect=AssertionError('must not send')) as smtp:
            result=run(self.args(action='reminder',event_id='event-test'))
            self.assertEqual(result,{'status':'blocked','reason':'specific_reminder_send_authorization_required'})
            smtp.assert_not_called()
    def test_preview_exact_pending_hash_only(self):
        store=MailReplyStore(self.db,[DEFAULT_RECIPIENT]);store.register_reminder('<reminder@example.test>','event-test')
        msg=EmailMessage();msg['From']=DEFAULT_RECIPIENT;msg['Message-ID']='<reply@example.test>'
        msg['In-Reply-To']='<reminder@example.test>';msg.set_content('标题: 标题\n正文:\n正文\n【正文结束】')
        row=store.ingest(msg.as_bytes(),authenticated_sender=DEFAULT_RECIPIENT)
        with patch('hkipo.mobile.MobileWorkflow.preview',return_value={'raw_hash':row['raw_hash']}) as preview:
            self.assertEqual(run(self.args(action='preview',raw_hash='unknown',out=str(self.root/'preview')))['status'],'not_pending')
            preview.assert_not_called()
            result=run(self.args(action='preview',raw_hash=row['raw_hash'],out=str(self.root/'preview')))
            self.assertEqual(result['status'],'preview_ready');self.assertEqual(preview.call_args.args[0]['raw_hash'],row['raw_hash'])
            store.reserve(row['raw_hash'])
            self.assertEqual(run(self.args(action='preview',raw_hash=row['raw_hash'],out=str(self.root/'preview')))['status'],'not_pending')
            self.assertEqual(preview.call_count,1)
    def test_generic_exception_does_not_expose_secret(self):
        with patch('hkipo.mobile_cli.load_private_config',side_effect=RuntimeError('synthetic-secret')):
            result=run(self.args())
        self.assertEqual(result,{'status':'blocked','reason':'mobile_input_or_configuration_invalid'})
        self.assertNotIn('synthetic-secret',str(result))

    def test_typed_nonallowlisted_exception_does_not_expose_secret(self):
        for error in (DeliveryError('synthetic-secret'),IntakeError('synthetic-secret')):
            with self.subTest(error_type=type(error).__name__):
                with patch('hkipo.mobile_cli.load_private_config',side_effect=error):
                    result=run(self.args())
                self.assertEqual(result,{'status':'blocked','reason':'mobile_input_or_configuration_invalid'})
                self.assertNotIn('synthetic-secret',str(result))
