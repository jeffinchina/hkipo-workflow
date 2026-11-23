"""Generate private local files without overwriting existing configuration."""
import json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def absolutize(value):
    if isinstance(value,dict):return {k:absolutize(v) for k,v in value.items()}
    if isinstance(value,list):return [absolutize(v) for v in value]
    if isinstance(value,str) and value.startswith(('data/','config/','private/','skills/')):
        return str(ROOT/value)
    return value

for name in ('local','service','publisher','mobile'):
    target=ROOT/'config'/f'{name}.json'
    target.parent.mkdir(exist_ok=True,mode=0o700)
    if not target.exists():
        data=json.loads((ROOT/'examples'/f'{name}-settings.example.json').read_text())
        target.write_text(json.dumps(absolutize(data),ensure_ascii=False,indent=2)+'\n')
        target.chmod(0o600)
for name,example in [('config.json','private-config'),('draft-profile.json','draft-profile')]:
    target=ROOT/'private'/name
    target.parent.mkdir(exist_ok=True,mode=0o700)
    if not target.exists():
        target.write_bytes((ROOT/'examples'/f'{example}.example.json').read_bytes())
        target.chmod(0o600)
print('Created missing local configuration. Writer and deliveries remain disabled.')
