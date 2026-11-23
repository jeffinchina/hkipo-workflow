"""Deterministic source/arithmetic gates plus mandatory independent semantic review.

A passing quote proves occurrence, never meaning, completeness of a table or
correct attribution. Caller must supply a genuinely independent review result;
this local function cannot authenticate reviewer identity or honesty.
"""
from decimal import Decimal, InvalidOperation, localcontext, ROUND_HALF_UP
from pathlib import Path
import re
from .contracts import ARTICLE_SCHEMA, REVIEW_SCHEMA, CORE_FIELDS, HARD_FIELDS
from .evidence import _sha, normalize_quote, read_pdf_pages, load_visual_reviews

_NUM = re.compile(r'^[+-]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)$')
_GAP_SCOPE = re.compile(r'公告|本招股书|招股书|原文')
_GAP_WORDS = re.compile(r'未(?:具体|明确)?披露|未公布|没有给出|未给出|尚未提供|未提供|不适用')
_ABSENCE_ASSERTION = re.compile(r'(?:无|没有|不存在|不设|不设置)(?:任何)?(?:基石(?:投资者)?|绿鞋|超额配股权)')


def _scoped_gap_text(text):
    # Syntax only: an independent reviewer must establish whether the source
    # really omits this information and the qualifier applies to this claim.
    return bool(_GAP_SCOPE.search(text) and _GAP_WORDS.search(text)
                and not _ABSENCE_ASSERTION.search(text))


def _gap_reference_allowed(blocks, index, fid):
    block = blocks[index]
    if _ABSENCE_ASSERTION.search(block['text']):
        return False
    if block['role'] != 'heading':
        return _scoped_gap_text(block['text'])
    # A gap heading can be colloquial; require its following explanation to
    # carry the same fact ID and the explicit source-scoped gap wording.
    for following in blocks[index + 1:]:
        if following['role'] == 'heading':
            return False
        if following['text'].strip():
            return fid in following['fact_ids'] and _scoped_gap_text(following['text'])
    return False



def _schema(value, schema, location, errors):
    kind = schema.get('type')
    valid = {'object': lambda: isinstance(value, dict), 'array': lambda: isinstance(value, list),
             'string': lambda: isinstance(value, str),
             'boolean': lambda: isinstance(value, bool),
             'integer': lambda: isinstance(value, int) and not isinstance(value, bool)}[kind]()
    if not valid:
        errors.append(f'{location}: expected {kind}')
        return
    if 'enum' in schema and value not in schema['enum']:
        errors.append(f'{location}: invalid enum')
    if kind == 'object':
        for key in schema['required']:
            if key not in value:
                errors.append(f'{location}.{key}: missing')
        for key in value:
            if key not in schema['properties']:
                errors.append(f'{location}.{key}: unknown field')
            else:
                _schema(value[key], schema['properties'][key], location + '.' + key, errors)
    elif kind == 'array':
        for i, item in enumerate(value):
            _schema(item, schema['items'], f'{location}[{i}]', errors)


def _number(raw):
    if not isinstance(raw, str) or not _NUM.fullmatch(raw) or len(raw) > 200:
        raise ValueError('expected plain finite numeric string (no commas/exponents)')
    value = Decimal(raw)
    if not value.is_finite():
        raise ValueError('non-finite number')
    return value


def _calculate(calc, facts):
    ids = calc['input_fact_ids']
    if not ids or any(i not in facts or facts[i]['status'] != 'supported' for i in ids):
        raise ValueError('inputs must reference supported facts')
    rows = [facts[i] for i in ids]
    op = calc['operation']
    if op not in {'divide', 'multiply', 'sum', 'subtract', 'percent_change', 'convert'}:
        raise ValueError('operation not allowed')
    currencies = {r['currency'] for r in rows if r['currency']}
    if len(currencies) > 1:
        raise ValueError('cross-currency inputs require an explicit conversion contract')
    units = {r['unit'] for r in rows if r['unit']}
    if op in {'sum', 'subtract', 'percent_change'} and (len({r['unit'] for r in rows}) != 1 or len({r['currency'] for r in rows}) != 1):
        raise ValueError('unit conflict')
    if op in {'sum', 'subtract'}:
        if calc['unit'] != rows[0]['unit'] or calc['currency'] != rows[0]['currency']:
            raise ValueError('output unit/currency must equal additive input')
    amount_scales = {'元': 1, 'amount': 1, '千元': 1000, '万元': 10000,
                     '百万元': 1000000, 'million': 1000000, '亿元': 100000000,
                     'billion': 1000000000}
    if op == 'convert':
        if (len(rows) != 1 or rows[0]['unit'] not in amount_scales or calc['unit'] not in amount_scales
                or not rows[0]['currency'] or calc['currency'] != rows[0]['currency']):
            raise ValueError('convert requires one explicit amount scale and unchanged currency')
    if op == 'divide':
        if len(rows) != 2:
            raise ValueError('divide needs two inputs')
        # No inferred conversion factors or free-form algebra of unit labels.
        if rows[0]['unit'] == rows[1]['unit'] and rows[0]['currency'] == rows[1]['currency']:
            if calc['unit'] not in {'', 'ratio', '倍', '概率', '%'} or calc['currency']:
                raise ValueError('same-unit divide must yield a ratio, probability fraction or percentage')
        elif rows[1]['unit'] == '' and rows[1]['currency'] == '':
            if calc['unit'] != rows[0]['unit'] or calc['currency'] != rows[0]['currency']:
                raise ValueError('dimensionless denominator must preserve unit')
        elif (rows[0]['unit'] == 'shares' and rows[1]['unit'] == 'shares_per_lot'
              and not rows[0]['currency'] and not rows[1]['currency']):
            if calc['unit'] != 'lots' or calc['currency']:
                raise ValueError('shares divided by shares_per_lot must yield lots')
        elif re.fullmatch(r'[A-Z]{3}_per_[A-Z]{3}', rows[1]['unit']) and not rows[1]['currency']:
            numerator, denominator = rows[1]['unit'].split('_per_')
            if (numerator == denominator or rows[0]['currency'] != numerator
                    or calc['currency'] != denominator or calc['unit'] != rows[0]['unit']
                    or rows[0]['unit'] not in {'元', 'amount', 'million', 'billion'}
                    or _number(rows[1]['value']) <= 0):
                raise ValueError('explicit FX divide currency direction or amount unit conflict')
        else:
            raise ValueError('unsupported divide unit relationship')
    if op == 'multiply':
        dimensional = [r for r in rows if r['unit'] or r['currency']]
        # Only this explicit canonical pair cancels shares into a per-lot amount.
        per_share = [r for r in rows if r['unit'] == 'per_share' and r['currency']]
        shares_per_lot = [r for r in rows if r['unit'] == 'shares_per_lot' and not r['currency']]
        if len(rows) == 2 and len(per_share) == len(shares_per_lot) == 1:
            expected = {'unit': 'per_lot', 'currency': per_share[0]['currency']}
        else:
            if len(dimensional) > 1:
                raise ValueError('multiplication of multiple dimensional inputs unsupported')
            expected = dimensional[0] if dimensional else {'unit': '', 'currency': ''}
        if calc['unit'] != expected['unit'] or calc['currency'] != expected['currency']:
            raise ValueError('multiply output unit/currency conflict')
    if op == 'percent_change':
        if len(rows) != 2 or calc['unit'] != '%' or calc['currency']:
            raise ValueError('percent_change needs old,new and percentage output')
    if op == 'subtract' and len(rows) != 2:
        raise ValueError('subtract needs two inputs')
    values = [_number(r['value']) for r in rows]
    with localcontext() as ctx:
        ctx.prec = 256
        if op == 'sum':
            expected = sum(values, Decimal(0))
        elif op == 'convert':
            expected = values[0] * amount_scales[rows[0]['unit']] / amount_scales[calc['unit']]
        elif op == 'subtract':
            expected = values[0] - values[1]
        elif op == 'multiply':
            expected = Decimal(1)
            for v in values:
                expected *= v
        elif op == 'divide':
            if values[1] == 0:
                raise ValueError('division by zero')
            expected = values[0] / values[1]
            if calc['unit'] == '%' and rows[0]['unit'] == rows[1]['unit'] and rows[0]['currency'] == rows[1]['currency']:
                expected *= 100
        else:
            if values[0] == 0:
                raise ValueError('percent_change old value zero')
            expected = (values[1] - values[0]) / values[0] * 100
        actual = _number(calc['value'])
        if actual != expected:
            # Rounding is opt-in and specifies decimal places in basis.
            match = re.search(r'round_half_up:(\d{1,2})(?:\b|$)', calc['basis'])
            if not match:
                # Bounded legacy wording declares the same exact rounding mode.
                match = re.search(r'四舍五入至小数点后([0-9]{1,2})位', calc['basis'])
            if not match or actual != expected.quantize(Decimal(1).scaleb(-int(match[1])), rounding=ROUND_HALF_UP):
                raise ValueError(f'arithmetic mismatch (expected {expected})')
    return str(expected)


def validate_bundle(bundle, documents, review=None, *, visual_reviews=None, trusted_visual_review_sha256s=(),
                    scoped_visual_registry=None, scope_approval_registry=None,
                    bundle_path=None, review_path=None, source_mode="legacy_strict"):
    """Validate ARTICLE_SCHEMA and REVIEW_SCHEMA; return pass/blocking/advisory.

    documents accepts prepare_documents() manifest or its documents list. Reads
    each supplied PDF afresh to verify its SHA, page count and quote occurrence.
    Independent semantic review is required even if all mechanical checks pass.
    """
    if source_mode not in {"legacy_strict", "text_first"}:
        raise ValueError("Unknown source_mode")
    blocking, advisory = [], []
    report = {'pass': False, 'blocking': blocking, 'advisory': advisory,
              'gaps': [], 'calculations_checked': {}, 'source_mode': source_mode,
              'unreviewed_visual_pages': [],
              'limitations': ['Quote occurrence does not prove semantic attribution, financial basis, table completeness or actual reallocation.',
                              'Caller must authenticate independent reviewer provenance; a model all-pass claim alone is insufficient.',
                              'Only explicit same-scale BASE_per_QUOTE FX division and canonical share/lot conversion; no inferred FX, arbitrary unit conversion, OCR or natural-language claim inference.',
                              'Gap wording checks are syntactic only; independent review must verify source scope, non-disclosure/non-applicability and absence of unsupported positive claims.',
                              'Visual sidecar signatures authenticate caller trust anchors, not the truth of a transcription; independent visual reviewer provenance is caller responsibility.']}
    _schema(bundle, ARTICLE_SCHEMA, 'bundle', blocking)
    if blocking:
        return report
    report['gaps'] = list(bundle['gaps'])
    docs = documents.get('documents', []) if isinstance(documents, dict) else documents
    if not isinstance(docs, list) or not docs:
        blocking.append('documents: at least one actual PDF required')
        return report
    real = {}
    for d in docs:
        try:
            did = d['document_id']
            if did in real:
                raise ValueError('duplicate document_id')
            actual_sha = _sha(Path(d['path']))
            if actual_sha != d['sha256'] or did != actual_sha[:12]:
                raise ValueError('actual PDF SHA/document_id mismatch')
            pages = read_pdf_pages(d['path'])
            if len(pages) != d['pages_total']:
                raise ValueError('actual PDF page count mismatch')
            real[did] = {'sha256': actual_sha, 'pages_total': len(pages), 'pages': pages,
                         'unreadable_pages': [p['pdf_page'] for p in pages if p['status'] == 'unreadable']}
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
            blocking.append(f'document: {exc}')
    visual = {}
    try:
        visual = load_visual_reviews(visual_reviews or [], trusted_visual_review_sha256s, docs)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError) as exc:
        blocking.append(f'visual review: {exc}')
    scopes = {}
    if source_mode == "legacy_strict" and scoped_visual_registry is not None:
        try:
            from .scoped_visual import load_scoped_pages, dependency_binding, load_scoped_dependency_review
            scopes = load_scoped_pages(scoped_visual_registry, docs, visual)
            if scopes:
                report['source_visual_gaps'] = [{'document_id': k[0], 'pdf_page': k[1],
                    'status': 'needs_source', 'scope_sha256': r['_sha256'],
                    'regions': [g['region_id'] for g in r['regions']]} for k, r in scopes.items()]
                if not bundle['gaps'] or any(not any(r['regions'][0]['region_id'] in gap for gap in bundle['gaps']) for r in scopes.values()):
                    blocking.append('scoped source gap must appear in bundle.gaps with region_id')
                if scope_approval_registry is not None and bundle_path and review_path:
                    import json
                    if json.loads(Path(bundle_path).read_text()) != bundle or json.loads(Path(review_path).read_text()) != review:
                        raise ValueError('in-memory bundle/review differs from approved files')
                    binding = dependency_binding(bundle_path, review_path, docs, scopes, visual, scoped_visual_registry)
                    approved = load_scoped_dependency_review(scope_approval_registry, binding, scopes)
                    report['scope_dependency_approval_sha256'] = approved['_sha256']
                else:
                    blocking.append('scoped dependency approval missing; delivery forbidden')
        except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError) as exc:
            blocking.append(f'scoped visual review: {exc}')
    report['scoped_research_eligible'] = bool(scopes)
    report['visual_reviews_used'] = []
    for did, d in real.items():
        for page in d['pages']:
            key = (did, page['pdf_page'])
            if page['status'] == 'visual_required' and key not in visual and key not in scopes:
                report["unreviewed_visual_pages"].append({"document_id": did, "pdf_page": page["pdf_page"], "reasons": page["visual_reasons"]})
                if source_mode == "legacy_strict":
                    blocking.append(f"visual review required: {did} PDF page {page['pdf_page']} ({', '.join(page['visual_reasons'])})")
            if key in visual:
                sidecar = visual[key]
                report['visual_reviews_used'].append({'document_id': did, 'pdf_page': page['pdf_page'],
                    'sidecar_sha256': sidecar['sidecar_sha256'], 'decision': sidecar['decision'],
                    'reviewer': sidecar['reviewer'], 'reviewed_at': sidecar['reviewed_at']})
                if sidecar['decision'] == 'transcribed':
                    page['visual_transcription'] = sidecar['transcription']

    def coverage(rows, label):
        seen = set()
        for c in rows:
            did = c['document_id']
            if did in seen:
                blocking.append(f'{label}: duplicate document {did}')
            seen.add(did)
            d = real.get(did)
            if d is None:
                blocking.append(f'{label}: unknown document {did}')
                continue
            if c['sha256'] != d['sha256'] or c['pages_total'] != d['pages_total']:
                blocking.append(f'{label}: SHA/page count mismatch {did}')
            expected = set(range(1, d['pages_total'] + 1))
            readable = c['pages_reviewed']; unreadable = c['unreadable_pages']
            if (len(readable) != len(set(readable)) or len(unreadable) != len(set(unreadable)) or
                set(readable) & set(unreadable) or set(readable) | set(unreadable) != expected or
                set(unreadable) != set(d['unreadable_pages'])):
                blocking.append(f'{label}: incomplete/invalid page coverage {did}')
            if unreadable:
                blocking.append(f'{label}: unreadable PDF pages {did}: {unreadable}; obtain readable source/OCR review')
        if seen != set(real):
            blocking.append(f'{label}: must cover every supplied document')
    coverage(bundle['sources_seen'], 'sources_seen')
    def refcheck(ref, label):
        d = real.get(ref['document_id']); n = ref['pdf_page']; quote = normalize_quote(ref['quote'])
        if d is None or not 1 <= n <= d['pages_total']:
            blocking.append(f'{label}: unknown document or invalid PDF page')
        elif not quote or not any(quote in normalize_quote(t) for t in (scopes[(ref['document_id'], n)]['trusted_transcription_segments'] if (ref['document_id'], n) in scopes else [d['pages'][n-1]['text'], d['pages'][n-1].get('visual_transcription', '')])):
            blocking.append(f'{label}: quote absent on cited PDF page')
    facts = {}; keys = {}
    for f in bundle['facts']:
        if not f['id'] or f['id'] in facts:
            blocking.append('facts: empty/duplicate id ' + f['id'])
        facts[f['id']] = f
        keys.setdefault(f['key'], []).append(f)
        if f['status'] == 'supported' and (not f['evidence'] or not (f['value'].strip() or f['items'])):
            blocking.append(f"fact {f['id']}: supported requires value/items and quoted evidence")
        if f['status'] in {'not_disclosed', 'not_applicable'} and not f['basis'].strip():
            blocking.append(f"fact {f['id']}: absent/non-applicable requires reason in basis")
        for ref in f['evidence']:
            refcheck(ref, 'fact ' + f['id'])
    for key in CORE_FIELDS[bundle['kind']]:
        rows = keys.get(key, [])
        if len(rows) != 1:
            blocking.append('core: exactly one explicit fact required for ' + key)
        elif rows[0]['status'] != 'supported':
            advisory.append('core optional/unresolved: ' + key + ' (' + rows[0]['status'] + ')')
    hard_ids = set()
    for key in HARD_FIELDS[bundle['kind']]:
        for f in keys.get(key, []):
            if f['status'] != 'supported':
                blocking.append('hard field unsupported: ' + key)
            else:
                hard_ids.add(f['id'])
    calcs = {}; candidates = {}; available = dict(facts)
    for c in bundle['calculations']:
        if not c['id'] or c['id'] in facts or c['id'] in candidates:
            blocking.append('calculation: empty/duplicate id ' + c['id'])
            continue
        candidates[c['id']] = c
    def calculate_dependency(cid, visiting):
        if cid in calcs:
            return
        if cid in visiting or len(visiting) >= 32:
            raise ValueError('cyclic or excessive calculation dependencies')
        c = candidates[cid]
        for dependency in c['input_fact_ids']:
            if dependency in candidates:
                calculate_dependency(dependency, visiting | {cid})
        exact = _calculate(c, available)
        calcs[cid] = c
        # The declared rounded value has just passed the exact Decimal check;
        # use that explicit precision when it is an input to another calculation.
        available[cid] = dict(c, status='supported')
        report['calculations_checked'][cid] = exact
    for cid in candidates:
        try:
            calculate_dependency(cid, set())
        except (ValueError, InvalidOperation, ArithmeticError) as exc:
            blocking.append(f"calculation {cid}: {exc}")
    referenced = set()
    article = bundle['article']
    if not article['title'].strip():
        blocking.append('article: empty title')
    if not article['blocks'] or not any(b['text'].strip() for b in article['blocks']):
        blocking.append('article: empty body')
    for fid in article['title_fact_ids']:
        referenced.add(fid)
        if fid not in facts or facts[fid]['status'] != 'supported':
            blocking.append(f'title: unsupported/unknown fact reference {fid}')
    if re.search(r'\d', article['title']) and not article['title_fact_ids']:
        blocking.append('title: numeric claim without fact references')
    for i, block in enumerate(bundle['article']['blocks']):
        for fid in block['fact_ids']:
            referenced.add(fid)
            if fid in calcs or (fid in facts and facts[fid]['status'] == 'supported'):
                continue
            fact = facts.get(fid)
            if (fact and fact['status'] in {'not_disclosed', 'not_applicable'} and
                    fact['basis'].strip() and _gap_reference_allowed(article['blocks'], i, fid)):
                advisory.append(f'block {i}: source-scoped gap reference {fid}; independent reviewer must verify the qualifier and source omission/applicability')
            else:
                blocking.append(f'block {i}: unsupported/unknown or unqualified gap fact reference {fid}')
        text = re.sub(r'^\s*\d+\s*[、.．)）]\s*', '', block['text'])
        if block['role'] in {'paragraph', 'field', 'hook', 'item'} and not block['fact_ids']:
            blocking.append(f'block {i}: factual role requires fact references; pure opinion must use opinion role')
        if block['role'] == 'opinion' and re.search(r'\d', text) and not block['fact_ids']:
            blocking.append(f'block {i}: numeric opinion without fact references')
    required_review = referenced | hard_ids
    # Reviewer checks the actual supported inputs as well as displayed derived IDs.
    to_check = list(referenced); expanded = set()
    while to_check:
        fid = to_check.pop()
        if fid in calcs:
            if fid in expanded:
                continue
            expanded.add(fid)
            required_review.update(calcs[fid]['input_fact_ids'])
            to_check.extend(calcs[fid]['input_fact_ids'])
    if review is None:
        blocking.append('independent semantic review missing; delivery forbidden')
    else:
        review_errors = []
        _schema(review, REVIEW_SCHEMA, 'review', review_errors)
        blocking.extend(review_errors)
        if not review_errors:
            if review['title_checked'] is not True:
                blocking.append('review: title not explicitly checked')
            if review['unreferenced_claims_checked'] is not True:
                blocking.append('review: unreferenced claims not explicitly checked')
            if review['decision'] != 'pass':
                blocking.append('independent semantic review did not pass')
            if not required_review <= set(review['checked_fact_ids']):
                blocking.append('review: checked_fact_ids missing article/hard/input facts')
            coverage(review['reviewed_documents'], 'reviewed_documents')
            for issue in review['issues']:
                for ref in issue['source_refs']:
                    refcheck(ref, 'review issue')
                (blocking if issue['severity'] == 'blocking' else advisory).append('review: ' + issue['problem'])
    advisory.append('Mechanical source matching is not semantic verification; allocation percentages/financial basis require independent review.')
    report['pass'] = not blocking
    return report
