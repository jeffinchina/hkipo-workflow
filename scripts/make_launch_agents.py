"""Generate macOS LaunchAgent files locally; never install or start them."""
import argparse
import os
from pathlib import Path
import plistlib
import shutil

ROOT=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser()
parser.add_argument('--enable-writer',action='store_true')
args=parser.parse_args()
python=ROOT/'.venv/bin/python'
if not python.is_file():raise SystemExit('Create .venv and install the project first')
out=ROOT/'data/deployment';out.mkdir(parents=True,exist_ok=True,mode=0o700)
env={k:v for k,v in os.environ.items() if k.startswith('HKIPO_')}
env['PATH']=os.environ.get('PATH',os.defpath)
commands={
    'scan':['watch','--config',str(ROOT/'config/service.json'),'--classify'],
    'publisher':['publisher','--config',str(ROOT/'config/publisher.json'),'--watch'],
    'mobile':['mobile-service','--config',str(ROOT/'config/mobile.json'),'--watch'],
}
if args.enable_writer:commands['scan']+=['--work','--enable-writer']
for name,command in commands.items():
    label='org.hkipo.workflow.'+name
    value={'Label':label,'ProgramArguments':[str(python),'-m','hkipo',*command],
           'WorkingDirectory':str(ROOT),'RunAtLoad':True,'KeepAlive':True,
           'ThrottleInterval':60,'EnvironmentVariables':env,
           'StandardOutPath':str(out/(name+'.log')),'StandardErrorPath':str(out/(name+'.error.log'))}
    target=out/(label+'.plist')
    target.write_bytes(plistlib.dumps(value));target.chmod(0o600)
print('Generated three local LaunchAgents. Review them before installing with launchctl.')
