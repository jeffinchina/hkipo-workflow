"""Offer-only editorial checks; never substitutes for source/fact review."""
import re
from .contracts import CORE_FIELDS

SECTIONS=['一、基本情况','二、财务情况','三、发行情况','四、估值分析','五、最后总结']
FIELDS=['申购时间','发行价格','每手股数','入场费','发行数量','市值','绿鞋','发行方案','保荐人','基石']

READER_POLICY = '''
成稿显示与内部事实账本分开验收：原文精确值、引文、单位/期间/币种、完整名单与条件必须完整核实；
正文金额、百分比和倍数最多两位小数，通常一位或两位，按显示单位正确四舍五入即可。
不要把正确舍入判成数据错误，不要求正文展现所有精确值、计算参数、查证过程、借股/终止/稳价等一般条件。
底稿遗漏仍须修复；正文未复述无实质影响的标准条件不是遗漏，不得据此blocking或强行加长正文。
只有真正影响读者理解业务、利润质量、价格成本、货量、参与条件或核心结论的遗漏才阻断；每项指出具体误导后果。
财务优先营收/未调整利润和一至两个关键问题，简短交代；陌生业务缩写首次用普通话说明实际产品或用途，能不用就不用。
绿鞋默认只写有或无；基石默认有/无，有则机构与合计认购占比。完整适用条件、名单、分母在底稿核验。
没有依据不能把未知写成无；真正的资料缺口继续后台补源，不把异常核验链路放进正文来充当成稿。
独立核验不得读作者自评通过结论；全部事实和数字要准确，表达简洁不降低底稿核验要求。
'''


def public_presentation_issues(blocks):
    errors=[]
    for index,block in enumerate(blocks):
        text=re.sub(r'https?://\S+','',block['text'])
        if re.search(r'(?<![A-Za-z0-9_.])\d+(?:,\d{3})*\.\d{3,}(?![A-Za-z0-9_.])',text):
            errors.append(f'editorial: body block {index} exceeds two decimal places; round display, preserve exact audit values')
        if re.search(r'PDF\s*第\s*\d+\s*页|not_disclosed|unresolved|source_refs|fact_ids?|逐页核验|核验链路|已通读.*逐页',text):
            errors.append(f'editorial: body block {index} contains internal audit wording')
    return errors

def apply_offer_editorial(report,bundle):
    if bundle.get('kind')!='offer':return report
    blocks=bundle['article']['blocks'];errors=public_presentation_issues(blocks)
    indices=[]
    for title in SECTIONS:
        found=[i for i,b in enumerate(blocks) if b['role']=='heading' and b['text'].strip()==title]
        if len(found)!=1:errors.append('editorial: missing/duplicate section '+title)
        else:indices.append(found[0])
    if len(indices)==5 and indices!=sorted(indices):errors.append('editorial: section order')
    if len(indices)==5 and indices==sorted(indices):
        intro=[b for b in blocks[:indices[0]] if b['text'].strip()]
        if len(intro)!=1 or intro[0]['role']!='hook':errors.append('editorial: exactly one short hook before first section')
        financial=blocks[indices[1]+1:indices[2]]
        if not any(b['role']=='heading' and re.fullmatch(r'营收(?:[（(][^（）()]+[）)])?',b['text'].strip()) for b in financial):errors.append('editorial: separate revenue subheading required')
        # Profit yearly groups are a default, not a compulsory shape for every issuer.
        # The independent reviewer must still verify material profit/loss disclosure.
        if not any(re.search(r'利润|亏损|盈利',b['text']) for b in blocks[indices[1]+1:indices[4]]):errors.append('editorial: profit/loss disclosure missing')
        offering=blocks[indices[2]+1:indices[3]]
        labels=[b['text'].split('：',1)[0].strip() for b in offering if b['role']=='field']
        actual=[s for s in labels if s in FIELDS]
        if actual!=FIELDS:errors.append('editorial: ten issuance labels required once in canonical order')
        if any(s in labels for s in ('独家保荐人','联席保荐人','联合保荐人')):errors.append('editorial: label must be 保荐人')
        quantity=[b['text'] for b in offering if b['role']=='field' and b['text'].startswith('发行数量：')]
        if quantity and not re.search(r'\d[^：]*手',quantity[0]):errors.append('editorial: public offer quantity must include lots')
    facts={f['key']:f for f in bundle['facts']}
    for key in CORE_FIELDS['offer']:
        if key not in facts or facts[key]['status']=='unresolved':errors.append('editorial: unresolved publication-critical field '+key)
        elif facts[key]['status'] in {'not_disclosed','not_applicable'} and (not facts[key].get('basis','').strip() or not facts[key].get('evidence')):
            errors.append('editorial: absence/non-applicability needs source scope evidence '+key)
    # Preserve every existing fact, source, arithmetic and independent-review error.
    report['blocking'].extend(errors)
    report['pass']=not report['blocking']
    report['editorial_policy']='offer-v3.0'
    return report
