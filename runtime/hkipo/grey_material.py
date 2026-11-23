"""Retain a complete, independently checked allotment fact packet for grey notes."""
import hashlib
import json
from pathlib import Path
import re
from .grey_market import display


def _load(path):return json.loads(Path(path).read_text())


def verified_allotment(payload,job):
    event=payload['event'];wr=payload['writer_result'];status=_load(job/'status.json')
    if (event['kind']!='allotment' or status.get('status')!='ready_for_delivery'
            or not wr.get('request_id') or status.get('request_id')!=wr['request_id']):raise ValueError('stale allotment request')
    for key in ('bundle_path','review_path','quality_path'):
        if status.get(key)!=wr.get(key):raise ValueError('changed allotment output')
    paths=[Path(status[k]) for k in ('bundle_path','review_path','quality_path')]+[job/'sources/manifest.json',job/'status.json']
    before={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    bundle=_load(status['bundle_path']);review=_load(status['review_path']);manifest=_load(job/'sources/manifest.json')
    if bundle.get('event_id')!=event['event_id'] or bundle.get('kind')!='allotment' or _load(status['quality_path']).get('pass') is not True:
        raise ValueError('allotment quality pending')
    from .visual import trusted_visuals
    from .quality import validate_bundle
    visual_paths,visual_hashes,_=trusted_visuals(status.get('visual_registry'),manifest)
    quality=validate_bundle(bundle,manifest,review,visual_reviews=visual_paths,
        trusted_visual_review_sha256s=visual_hashes,source_mode=status.get('source_mode','legacy_strict'),
        scoped_visual_registry=status.get('scoped_visual_registry'),scope_approval_registry=status.get('scope_approval_registry'),
        bundle_path=status['bundle_path'],review_path=status['review_path'])
    if quality.get('pass') is not True:raise ValueError('current allotment revalidation failed')
    if before!={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}:raise ValueError('allotment changed during read')
    facts={f['key']:f for f in bundle['facts'] if f['status']=='supported'}
    listing=facts['listing_date']['value']
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',listing):raise ValueError('exact listing date required')
    labels={'final_offer_price':'最终定价','public_subscription':'公开认购','international_subscription':'国际认购',
            'final_public_allocation':'香港最终分配','final_international_allocation':'国际最终分配','reallocation':'回拨',
            'concentration':'集中度'}
    lines=[]
    for key,label in labels.items():
        fact=facts.get(key)
        if not fact:continue
        value=fact['value'] or '；'.join(fact.get('items',[]))
        if not value:continue
        if key=='concentration' and len(value)>120:continue
        # The full evidence/basis/units stay in the fact packet. Short e-mail
        # facts do not truncate a value mid-sentence or invent a unit conversion.
        value=re.sub(r'(?<![\w.])\d+\.\d{3,}(?![\w.])',lambda m:display(m[0]),value)
        unit=' / '.join(x for x in (fact.get('currency'),fact.get('unit')) if x)
        lines.append(label+'：'+value+('（'+unit+'）' if unit else ''))
    return {'status':'verified','summary':'\n'.join(lines),'listing_date':listing,
            'source_event_ids':event['source_event_ids'],'request_id':status['request_id'],
            'research_id':event['research_id'],'guard_files':[{'path':str(p),'sha256':before[str(p)]} for p in paths],
            'facts':bundle['facts'],'calculations':bundle['calculations'],
            'bundle_path':status['bundle_path'],'bundle_sha256':before[str(Path(status['bundle_path']))],
            'review_path':status['review_path'],'review_sha256':before[str(Path(status['review_path']))],
            'source_documents':[{'sha256':d['sha256'],'path':d['path']} for d in manifest['documents']]}
