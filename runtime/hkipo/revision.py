"""Targeted repair evidence and convergence audit; no readiness overrides."""
import json
import re
from pathlib import Path
from .contracts import obj, arr, S, REF
from .evidence import normalize_quote

RECOVERY_TACTICS = ('targeted', 'evidence_reconciliation', 'verified_scope')
MAX_REVIEW_ROUNDS = 24

SOURCE_REPAIR_SCHEMA = obj({
    'findings': arr(obj({'fact_key': S, 'finding': S,
        'state': {'type': 'string', 'enum': ['located', 'scope_checked_not_disclosed', 'unresolved']},
        'evidence': arr(REF), 'remaining_work': S})),
    'summary': S,
})


class InvalidSourceRepair(ValueError):
    pass


def stable_run_identity(identity):
    """Download timestamps are audit data, never a reason to discard a draft."""
    value = dict(identity)
    event = value['event']
    value['event'] = {key: event.get(key) for key in (
        'event_id', 'kind', 'stock_code', 'stock_name', 'episode_id', 'research_id',
        'title', 'url', 'published_at', 'language')}
    value['sources'] = sorted(tuple(row) for row in value['sources'])
    if value.get('source_mode') == 'text_first':
        # Additional authenticated page evidence triggers revalidation below,
        # not loss of the editable draft of unchanged official PDFs.
        value.pop('visual_review_hashes', None)
    return value


def source_repair(manifest, review, context, run_model, outdir, *, visual=None, quality=None, **options):
    """Re-investigate the full source, including pages never cited by the draft.

    Findings are research hints only. The final independent reviewer sees the
    actual complete PDFs/text and must judge every absence claim independently.
    """
    from .writer import COMMON, documents_context, save_json
    from .quality import _schema
    outdir = Path(outdir)
    prompt = COMMON + context + '''
你是补源研究员。这不是改稿或放行审核。只研究以下阻断项，重新检索并通读给出的全部原文。
先定位目录、定义、发行结构及相关专节，再检查正文交叉引用及条件条款。
若数字或条款矛盾，逐一列出原值的文件、页码、适用期间、币种、口径、本次角色和生效条件；
只有正式文件明确修订或取代时才用新版替换旧值，公告日期较新本身不构成替换证据。
如确定混淆了区间与定价、发售前后、各阶段分母或合并与经调整口径，写清应保留哪一值及其根源。
区分本次安排的实质披露与合同通用条件，不能由终止条款推断存在具体基石。
找不到具体信息时，明确已核查的全文范围、相关章节及真实引文；只能报告给定正式文件
没有具体披露，不能推断“没有基石”。若文字乱码、图表或跨页关系确实妨碍确认，
remaining_work写[visual_page:document_id:页码]及原因。原文之外确需其他正式文件则明确文档名。
不得为过审杜撰事实、引文、明确无基石的结论，也不得要求提供原文并未承诺的不存在证明。
每个finding必须有fact_key、状态、定位结果与真实连续引文；不作最终通过结论。
待研究问题：\n''' + json.dumps({'independent': [i for i in review.get('issues', []) if i['severity'] == 'blocking'],
        'mechanical': (quality or {}).get('blocking', [])}, ensure_ascii=False)
    prompt += '\n完整正式原文：\n' + documents_context(manifest, visual)
    result = run_model(prompt, SOURCE_REPAIR_SCHEMA, outdir, 'source-repair', **options)
    errors = []
    _schema(result, SOURCE_REPAIR_SCHEMA, 'source_repair', errors)
    if errors:
        raise InvalidSourceRepair('Source repair output schema invalid')
    docs = {d['document_id']: d for d in manifest['documents']}
    for finding in result['findings']:
        if finding['state'] != 'unresolved' and not finding['evidence']:
            raise InvalidSourceRepair('Source repair asserted a finding without quoted evidence')
        for ref in finding['evidence']:
            doc = docs.get(ref['document_id']); page = ref['pdf_page']
            quote = normalize_quote(ref['quote'])
            if not doc or not 1 <= page <= doc['pages_total'] or not quote:
                raise InvalidSourceRepair('Source repair quoted evidence not present on actual page')
            transcription = (visual or {}).get((ref['document_id'], page), {}).get('transcription', '')
            if not any(quote in normalize_quote(value) for value in (doc['pages'][page-1]['text'], transcription)):
                raise InvalidSourceRepair('Source repair quoted evidence not present on actual page')
    save_json(outdir / 'source-repair.verified.json', result)
    return result


def repair_visual_pages(manifest, requests, registry_path, outdir, run_model, **options):
    """Render only explicitly requested pages; authenticate independent sidecars."""
    from datetime import datetime, timezone
    import hashlib
    from .writer import save_json, fingerprint
    from .evidence import render_page_bytes, _sha, load_visual_reviews
    from .visual import trusted_visuals
    from .visual_worker import VISUAL_OUTPUT_SCHEMA, _prompt
    from .quality import _schema
    if len(requests) > 8:
        raise ValueError('Targeted visual repair exceeds eight-page bound')
    registry_path, outdir = Path(registry_path), Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    approved = trusted_visuals(registry_path, manifest)[2] if registry_path.exists() else {}
    registry = json.loads(registry_path.read_text()) if registry_path.exists() else {'version': 1, 'approved_reviews': []}
    docs = {d['document_id']: d for d in manifest['documents']}
    batch = []
    for request in requests:
        did, page = request['document_id'], request['pdf_page']
        if (did, page) in approved:
            continue
        doc = docs[did]
        if _sha(doc['path']) != doc['sha256'] or not 1 <= page <= doc['pages_total']:
            raise ValueError('Targeted visual PDF changed or page invalid')
        data = render_page_bytes(doc['path'], page)
        path = outdir / f'{did}-page-{page}.png'; path.write_bytes(data)
        batch.append({'document_id': did, 'pdf_page': page, 'pdf_sha256': doc['sha256'],
            'image_sha256': hashlib.sha256(data).hexdigest(), 'image_path': str(path.resolve()),
            'extractable_text': doc['pages'][page-1]['text']})
    for start in range(0, len(batch), 4):
        group = batch[start:start+4]
        name = 'source-visual-' + fingerprint(group)[:16]
        result = run_model(prompt=_prompt(group), schema=VISUAL_OUTPUT_SCHEMA,
            outdir=outdir/'model', name=name, images=[p['image_path'] for p in group], **options)
        errors = []; _schema(result, VISUAL_OUTPUT_SCHEMA, 'source_visual', errors)
        if errors:
            raise ValueError('Targeted visual output schema invalid')
        expected = {(p['document_id'], p['pdf_page']): p for p in group}
        seen = set()
        for row in result['pages']:
            key = (row['document_id'], row['pdf_page'])
            if key not in expected or key in seen:
                raise ValueError('Targeted visual page coverage invalid')
            seen.add(key); p = expected[key]
            if row['pdf_sha256'] != p['pdf_sha256'] or row['image_sha256'] != p['image_sha256']:
                raise ValueError('Targeted visual source binding invalid')
            if row['decision'] == 'needs_source':
                continue
            sidecar = dict(row, reviewer='codex-cli:targeted-source-review',
                reviewed_at=datetime.now(timezone.utc).isoformat())
            path = outdir / (name+f'-{row["pdf_page"]}.json'); save_json(path, sidecar)
            digest = _sha(path)
            load_visual_reviews([path], [digest], manifest['documents'])
            registry['approved_reviews'].append({'path': str(path.resolve()), 'sha256': digest})
        if seen != set(expected):
            raise ValueError('Targeted visual missing page')
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        save_json(registry_path, registry)
    return registry_path if registry_path.exists() else None


def issue_snapshot(bundle, review, quality):
    """Keep exact issues and stable fact keys, so ID renumbering cannot hide them."""
    keys = {f['id']: f['key'] for f in bundle.get('facts', [])}
    rows = {}
    independent = set()
    for issue in review.get('issues', []):
        if issue['severity'] != 'blocking':
            continue
        key = 'review:' + keys.get(issue['fact_id'], issue['fact_id'] or str(issue['block_index']))
        rows.setdefault(key, []).append(issue['problem'])
        independent.add('review: ' + issue['problem'])
    for problem in quality.get('blocking', []):
        if problem in independent or (independent and problem == 'independent semantic review did not pass'):
            continue
        stable_problem = re.sub(r'^(fact )([^:]+)(:)',
            lambda match: match[1] + keys.get(match[2], match[2]) + match[3], problem)
        key = 'mechanical:' + stable_problem
        rows.setdefault(key, []).append(problem)
    return {key: list(dict.fromkeys(problems)) for key, problems in rows.items()}


def repair_feedback(review, quality, findings=None, history=None, tactic='targeted'):
    tactics = {
        'targeted': '定点核对阻断项的具体主张、证据和相应段落，尽量沿用已核实部分。',
        'evidence_reconciliation': '上一稿的定点修复未减少阻断项。重新用全部正式原文对照冲突数值、条件及其适用期，以逐项定位和口径比较收敛，勿重复旧的论证。',
        'verified_scope': '按原文能支持的最小准确范围修复尚有争议的句子。未核实的可选句可删除或明确限定；核心事实、必需段落、全文来源覆盖及会改变读者结论的限制必须保留。',
    }
    return '\n本轮修复范围：' + tactics[tactic] + '''
这是逐项修复，保留上一稿已核实的事实、ID、证据、结构及无关段落，禁止整篇自由重写。
先修补来源/核心事实，再修计算、字段、日期条件和对应正文。只改与阻断项有关的区域，
如必须联动修改其依赖值/段落则一并修改，不能删掉必需字段或将错误降成advisory来过关。
对于原文未具体披露的非hard字段，允许经过全文核查后诚实写not_disclosed，basis写明
全文范围和相关章节，evidence附真实范围定位引文，gaps与正文明确“给定招股书未具体披露”，
绝不写成“没有”。unresolved只用于真正仍有缺页、乱码、矛盾或未核实来源的问题。
每条阻断逐项解决；复核意见也可能混淆通用合同条件与实质安排，应以完整原文为准修正。
若上一轮产生新问题，恢复已核实段落并只修未解决项。任何通过都仍由新进程独立审核决定。
上一轮未通过的具体问题：\n''' + json.dumps({
        'mechanical': quality.get('blocking', []),
        'independent_review': [i for i in review.get('issues', []) if i['severity'] == 'blocking'],
        'source_research_hints_not_approval': findings, 'convergence': history,
    }, ensure_ascii=False)


def requested_pages(review, manifest, findings=None):
    known = {d['document_id']: d['pages_total'] for d in manifest['documents']}
    pages = {}
    reasons = [i['problem'] for i in review.get('issues', []) if i['severity'] == 'blocking']
    reasons += [f['remaining_work'] for f in (findings or {}).get('findings', []) if f['state'] == 'unresolved']
    for reason in reasons:
        for did, number in re.findall(r'\[visual_page:([a-f0-9]{12}):([0-9]{1,9})\]', reason):
            if did in known and 1 <= int(number) <= known[did]:
                pages.setdefault((did, int(number)), {'document_id': did, 'pdf_page': int(number), 'reason': reason})
    return list(pages.values())
