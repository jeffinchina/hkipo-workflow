import io,unittest,random,hashlib
from PIL import Image
from hkipo.delivery import WeChatClient

class UploadCopyTests(unittest.TestCase):
    def test_large_photo_upload_copy_preserves_original(self):
        image=Image.frombytes('RGB',(900,900),random.Random(1).randbytes(900*900*3))
        stream=io.BytesIO();image.save(stream,format='PNG');original=stream.getvalue()
        before=hashlib.sha256(original).hexdigest();uploads=[]
        client=WeChatClient()
        def call(path,params,upload=None):
            uploads.append(upload);return {'url':'https://mmbiz.qpic.cn/test.jpg'}
        client.call=call
        self.assertGreater(len(original),1_000_000)
        client.inline_image('fake','phone.png',original)
        self.assertLess(len(uploads[0][1]),1_000_000)
        self.assertEqual(hashlib.sha256(original).hexdigest(),before)
