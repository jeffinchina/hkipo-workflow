"""Check tracked/staged blobs without printing any secret values."""
import argparse
from pathlib import Path
import re
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
FORBIDDEN_DIRS={'private','config','research','evaluation','artifacts','logs','node_modules','vendor','.venv','__pycache__'}
FORBIDDEN_SUFFIXES={'.sqlite','.sqlite3','.db','.eml','.pdf','.docx','.pem','.key','.p12','.pfx','.zip'}
RULES={
    'private-home-path':rb'/(?:Users|home)/[A-Za-z0-9_.-]+/',
    'private-key':rb'-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----',
    'github-token':rb'(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,})',
    'api-key':rb'\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{25,}',
    'wechat-appid':rb'\bwx[0-9a-f]{16}\b',
    'account-media-id':rb'\b2A0i1_[A-Za-z0-9_-]{16,}',
    'secret-value':rb'''(?i)["'](?:password|appsecret|client_secret|api_key|access_token)["']\s*:\s*["']([A-Za-z0-9_+/=-]{20,})["']''',
    'temporary-share-link':rb'(?i)[?&](?:tempkey|ticket|token)=[A-Za-z0-9_%+/=-]{24,}',
}
EMAIL=re.compile(rb'[\w.+-]+@([\w.-]+\.[A-Za-z]{2,})')

def files(staged=False):
    if staged:
        result=subprocess.run(['git','ls-files','--stage','-z'],cwd=ROOT,check=True,capture_output=True)
        for entry in result.stdout.split(b'\0'):
            if not entry:continue
            meta,name=entry.split(b'\t',1);mode,oid,stage=meta.split()
            if mode!=b'100644' and mode!=b'100755':
                yield name.decode(),b'', 'unsupported-git-entry';continue
            data=subprocess.run(['git','cat-file','blob',oid.decode()],cwd=ROOT,check=True,capture_output=True).stdout
            yield name.decode(),data,None
    else:
        result=subprocess.run(['git','ls-files','-z'],cwd=ROOT,capture_output=True)
        paths=[ROOT/p.decode() for p in result.stdout.split(b'\0') if p] if result.returncode==0 else [p for p in ROOT.rglob('*') if p.is_file() and '.git' not in p.parts]
        for p in paths:
            if p.is_symlink():yield p.relative_to(ROOT).as_posix(),b'','symlink';continue
            if p.is_file():yield p.relative_to(ROOT).as_posix(),p.read_bytes(),None

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--staged',action='store_true');args=parser.parse_args()
    failures=[];count=0
    for name,data,error in files(args.staged):
        count+=1;p=Path(name)
        if error:failures.append((name,error));continue
        if set(p.parts)&FORBIDDEN_DIRS or p.suffix in FORBIDDEN_SUFFIXES or p.name.startswith('.env'):
            failures.append((name,'private-file-class'))
        for code,pattern in RULES.items():
            if re.search(pattern,data):failures.append((name,code))
        for match in EMAIL.finditer(data):
            domain=match.group(1).lower()
            if domain.endswith((b'.invalid',b'.example',b'.test')) or domain in {b'example.com',b'example.org',b'example.net'}:continue
            failures.append((name,'personal-email'));break
    for name,code in sorted(set(failures)):print(f'{code}: {name}',file=sys.stderr)
    print(f'Privacy scan: {count} files, {len(set(failures))} issues')
    return bool(failures)
if __name__=='__main__':raise SystemExit(main())
