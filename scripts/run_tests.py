"""Offline regression tests; synthetic branding, mocked model and transports."""
import hashlib
import os
from pathlib import Path
import sys
import unittest
ROOT=Path(__file__).resolve().parents[1]
image=ROOT/'runtime/tests/fixtures/test-qr.jpg'
os.environ['HKIPO_RECIPIENT']='recipient@example.invalid'
os.environ['HKIPO_QR_PATH']=str(image)
os.environ['HKIPO_QR_SHA256']=hashlib.sha256(image.read_bytes()).hexdigest()
os.environ['HKIPO_PROFILE_PATH']=str(ROOT/'examples/draft-profile.example.json')
sys.path.insert(0,str(ROOT/'runtime'))
suite=unittest.defaultTestLoader.discover(str(ROOT/'runtime/tests'))
result=unittest.TextTestRunner(verbosity=1).run(suite)
raise SystemExit(not result.wasSuccessful())
