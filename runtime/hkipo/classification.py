"""Narrow, auditable IPO classification; no model or network calls.

Positive decisions require official document-role evidence plus matching issuer
and code on the first three PDF pages. Unknown is a first-class outcome.
"""
from dataclasses import dataclass,asdict
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import unicodedata
from urllib.parse import urljoin
from .downloads import DownloadError, official_url
from .sources import TableParser, parse_title_row


def compact(text):return re.sub(r'\s+','',unicodedata.normalize('NFKC',text))
def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()

@dataclass(frozen=True)
class OfficialDocumentEvidence:
    url: str
    stock_code: str
    stock_name: str
    role: str
    category: str
    source_url: str
    source_sha256: str
    locator: str
    public_date: str


def aggregate_evidence(html,source_url):
    if not re.fullmatch(r'https://www2\.hkexnews\.hk/New-Listings/New-Listing-Information/(Main-Board|GEM)\?sc_lang=(zh-HK|en)',source_url):
        raise ValueError('Unverified aggregate source')
    parser=TableParser();parser.feed(html)
    header=next((r for r in parser.rows if len(r)==5 and re.search('股份代號|Stock Code',r[0]['text'],re.I)),None)
    if not header or not re.search('招股|Prospectus',header[3]['text'],re.I):raise ValueError('Official aggregate schema missing')
    sha=hashlib.sha256(html.encode()).hexdigest();out=[]
    for index,row in enumerate(parser.rows):
        if len(row)!=5 or not re.fullmatch(r'\s*\d{1,5}\s*',row[0]['text']):continue
        for col,role in [(2,'announcement'),(3,'prospectus'),(4,'allotment')]:
            for link in row[col]['links']:
                try:url=official_url(urljoin(source_url,link['url']))
                except Exception:continue
                match=re.search(r'/(20\d{2})/(\d{2})(\d{2})/',url)
                day=f'{match[1]}-{match[2]}-{match[3]}' if match else ''
                out.append(OfficialDocumentEvidence(url,row[0]['text'].strip().zfill(5),row[1]['text'].strip(),role,'official_new_listing_table',source_url,sha,f'row:{index};column:{col};header:{header[col]["text"].strip()}',day))
    return out


def title_evidence(raw,source_url='https://www1.hkexnews.hk/search/titleSearchServlet.do'):
    if source_url!='https://www1.hkexnews.hk/search/titleSearchServlet.do':raise ValueError('Unverified title source')
    payload=json.loads(raw);rows=json.loads(payload['result']) if isinstance(payload['result'],str) else payload['result']
    if not isinstance(rows,list):raise ValueError('Official title result rows missing')
    sha=hashlib.sha256(raw.encode()).hexdigest();out=[]
    for row in rows:
        try:
            events,_=parse_title_row(row)
        except (ValueError,TypeError,KeyError,AttributeError):
            # A verified recovery page may contain an explicitly quarantined
            # row. It must not establish document roles for any source.
            continue
        if not events:continue
        # LONG_TEXT is the official headline category, never guessed from the
        # document title (warrants also contain the words listing document).
        category=compact(row.get('LONG_TEXT',''))
        if re.search('上市文件.*發售以供認購|ListingDocuments.*OfferforSubscription',category,re.I):role='prospectus'
        # Leaf 15200 supplies a role only; matching equity PDF pairs are still required.
        elif re.fullmatch(r'(?:公告及通告-\[正式通告\]|AnnouncementsandNotices-\[FormalNotice\])',category,re.I):role='announcement'
        elif re.search('公告及通告.*新上市公告|新上市.*新上市公告|AnnouncementsandNotices.*NewListingAnnouncement|NewListing.*NewListingAnnouncement',category,re.I):role='announcement'
        elif re.search('配發結果|AllotmentResults',category,re.I):role='allotment'
        elif re.search('公告及通告|AnnouncementsandNotices',category,re.I) and re.search(r'全球發售|全球发售|global offering',row.get('TITLE',''),re.I) and re.search(r'延遲|延期|終止|取消|補充|更正|delay|postpon|cancel|supplement|correction',row.get('TITLE',''),re.I):role='exception'
        else:continue
        try:url=official_url(events[0].url)
        except (DownloadError,ValueError):continue
        for event in events:
            out.append(OfficialDocumentEvidence(url,event.stock_code,event.stock_name,role,category,source_url,sha,
                                                'NEWS_ID:'+event.source_id,event.published_at[:10]))
    return out


def inspect_cover(path,expected_code):
    """Evidence only from labelled cover blocks on pages 1..3; no全文 search."""
    import fitz
    result={'path':str(Path(path).resolve()),'sha256':digest(path),'pass':False,'evidence':[],'reasons':[]}
    with fitz.open(path) as pdf:
        if pdf.needs_pass:result['reasons'].append('encrypted');return result
        pages=[pdf[i].get_text() for i in range(min(3,len(pdf)))]
    joined='\n'.join(pages);short=compact(joined)
    # These terms must be in a cover title/entity block. Generic risk boilerplate
    # about rights or REIT peers elsewhere must not exclude a valid IPO.
    first_lines='\n'.join('\n'.join(p.splitlines()[:25]) for p in pages)
    exclude=re.search(r'申請版本|申请版本|聆訊後資料集|聆讯后资料集|APPLICATION\s+PROOF|POST.HEARING|交易所買賣基金|交易所买卖基金|房地產投資信託基金|房地產投資信託|認股權證|可贖回牛熊證|供股章程|供股股份|基金單位|基金单位|\bETF\b|\bREIT\b|unit\s+trust|rights\s+issue|real\s+estate\s+investment\s+trust|exchange.traded\s+fund',first_lines,re.I)
    if exclude:result['reasons'].append('excluded_cover_type:'+exclude[0]);return result
    codes=[]
    for i,text in enumerate(pages,1):
        for match in re.finditer(r'(?:股份代號|股份代号|Stock\s+Code)\s*[:：]?\s*(\d(?:[ \t\u00a0]*\d){0,4})(?![ \t\u00a0]*\d)',text,re.I):
            codes.append(re.sub(r'\s+','',match[1]).zfill(5));result['evidence'].append({'rule':'labelled_stock_code','pdf_page':i,'quote':match[0]})
    if set(codes)!={str(expected_code).zfill(5)}:result['reasons'].append('cover_code_missing_or_mismatch');return result
    issuers=[]
    for i,text in enumerate(pages,1):
        lines=text.splitlines()
        for n,line in enumerate(lines):
            line=line.strip()
            if re.fullmatch(r'[\u3400-\u9fffA-Za-z0-9 .,&()（）－\-]{2,100}(?:股份有限公司|有限公司|LIMITED|Limited)',line) and re.search(r'註冊成立|注册成立|incorporated', ''.join(lines[n+1:n+4]),re.I):
                issuers.append(line);result['evidence'].append({'rule':'issuer_incorporation_block','pdf_page':i,'quote':'\n'.join(lines[n:n+4])[:450]})
    if not issuers:result['reasons'].append('company_issuer_block_missing');return result
    # Registration/incorporation alone is insufficient: cover must describe an
    # equity global offering, labelled offer shares, and the application route.
    tests={'global_offer':r'全球發售|全球发售|GLOBAL\s*OFFERING','equity_offer_shares':r'全球發售項下.*?股份|香港發售股份數目|香港发售股份数目|NumberofOfferShares','applicant_route':r'如何申請香港發售股份|如何申请香港发售股份|申請時|申請時繳足|申請程序|HowtoApplyforHongKongOfferShares|payableinfullonapplication'}
    for rule,pattern in tests.items():
        hit=next(((i,re.search(pattern,compact(text),re.I)) for i,text in enumerate(pages,1) if re.search(pattern,compact(text),re.I)),None)
        if hit is None:result['reasons'].append(rule+'_missing');continue
        result['evidence'].append({'rule':rule,'pdf_page':hit[0],'quote':hit[1][0]})
    if result['reasons']:return result
    announcement=bool(re.search(r'本公告並非招股章程|Thisannouncementisnotaprospectus',short,re.I))
    prospectus=not announcement and bool(re.search(r'重要提示|IMPORTANTNOTICE',short,re.I)) and bool(re.search(r'送呈香港公司註冊處|342C|deliveredtotheRegistrarofCompanies',short,re.I))
    if announcement or prospectus:
        result['evidence'].append({'rule':'front_matter_document_role','pdf_pages':list(range(1,len(pages)+1)),'observed_markers':[m[0] for m in re.finditer(r'本公告並非招股章程|Thisannouncementisnotaprospectus|重要提示|IMPORTANTNOTICE|送呈香港公司註冊處|342C|deliveredtotheRegistrarofCompanies',short,re.I)]})
    publication_dates=[]
    for index,text in enumerate(pages,1):
        for line in text.splitlines():
            match=re.fullmatch(r'\s*(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*',line)
            if match:
                publication_dates.append(f'{match[1]}-{int(match[2]):02d}-{int(match[3]):02d}')
                result['evidence'].append({'rule':'standalone_publication_date','pdf_page':index,'quote':line})
    result.update(pass_=True,issuer=compact(issuers[-1]),codes=sorted(set(codes)),document_role='announcement' if announcement else 'prospectus' if prospectus else 'unconfirmed',publication_dates=publication_dates)
    result['pass']=True;result.pop('pass_',None)
    return result


def classify_offer(events,pdf_paths,evidence):
    """pdf_paths maps source event_id -> verified local PDF path.
    Returns decision/episode_id/assignments/audit, without mutating the store."""
    audit=[];candidates=[]
    by_url={}
    for item in evidence:by_url.setdefault(item.url,[]).append(item)
    for event in events:
        e=event.to_dict() if hasattr(event,'to_dict') else dict(event)
        if e.get('kind')!='offer':continue
        if re.search('申請版本|聆訊後|application proof|post.hearing',e.get('title',''),re.I):
            audit.append({'event_id':e['event_id'],'rule':'preliminary_excluded'});continue
        official=[x for x in by_url.get(e['url'],[]) if x.stock_code==e['stock_code'] and x.role in {'announcement','prospectus'}]
        if len({x.role for x in official})!=1:
            audit.append({'event_id':e['event_id'],'rule':'official_role_missing_or_conflicting'});continue
        path=pdf_paths.get(e['event_id'])
        if path is None:audit.append({'event_id':e['event_id'],'rule':'local_pdf_missing'});continue
        try:cover=inspect_cover(path,e['stock_code'])
        except Exception:cover={'pass':False,'reasons':['cover_unreadable']}
        audit.append({'event_id':e['event_id'],'official_evidence':[asdict(x) for x in official],'cover':cover})
        if cover['pass'] and cover.get('document_role')==official[0].role:
            candidates.append((e,official[0],cover))
        elif cover['pass']:audit[-1]['role_conflict']='official_role_and_pdf_front_matter_disagree'
    roles={item[1].role for item in candidates}
    if not {'announcement','prospectus'}.issubset(roles):return {'decision':'needs_classification','reason':'confirmed_announcement_and_prospectus_required','audit':audit,'assignments':[]}
    if len({e['stock_code'] for e,_,_ in candidates})!=1 or len({c['issuer'] for _,_,c in candidates})!=1:
        return {'decision':'needs_classification','reason':'issuer_or_code_conflict','audit':audit,'assignments':[]}
    dates={o.public_date for _,o,_ in candidates if o.role=='prospectus'}
    if len(dates)!=1 or not next(iter(dates)):
        return {'decision':'needs_classification','reason':'prospectus_round_date_ambiguous','audit':audit,'assignments':[]}
    day=next(iter(dates))
    if any(day not in c.get('publication_dates',[]) for _,o,c in candidates if o.role=='prospectus'):
        return {'decision':'needs_classification','reason':'prospectus_cover_public_date_not_confirmed','audit':audit,'assignments':[]}
    if len({c['sha256'] for _,_,c in candidates})<2:
        return {'decision':'needs_classification','reason':'distinct_announcement_and_prospectus_required','audit':audit,'assignments':[]}
    issuer=candidates[0][2]['issuer'];code=candidates[0][0]['stock_code']
    # Formal announcement and prospectus must share the public issue date; a
    # different-date supplement is not silently merged into an earlier round.
    if any(o.public_date!=day for _,o,_ in candidates):
        return {'decision':'needs_classification','reason':'document_round_date_conflict','audit':audit,'assignments':[]}
    episode='ipo-'+code+'-'+day+'-'+hashlib.sha256(issuer.encode()).hexdigest()[:12]
    return {'decision':'confirmed','reason':'official_roles_and_matching_equity_cover_blocks','episode_id':episode,'issuer':issuer,'stock_code':code,'prospectus_public_date':day,'audit':audit,'assignments':[{'event_id':e['event_id'],'issuer_type':'common_equity','event_type':'ipo_offer','document_role':o.role,'episode_id':episode} for e,o,_ in candidates]}


def apply_classification(store,decision,promote_candidates=False):
    if decision.get('decision')!='confirmed':return 0
    for item in decision['assignments']:store.confirm_classification(**item,promote_candidate=promote_candidates)
    return len(decision['assignments'])


def classify_followup(event,path,evidence,confirmed_offer):
    """Only attach a formal follow-up to an already proven offer episode.
    The PDF must reference that exact prospectus date close to 招股章程, not
    merely carry a date or ticker somewhere in the document."""
    e=event.to_dict() if hasattr(event,'to_dict') else dict(event)
    audit=[]
    def unknown(reason):return {'decision':'needs_classification','reason':reason,'assignments':[],'audit':audit}
    if confirmed_offer.get('decision')!='confirmed' or not confirmed_offer.get('episode_id') or not confirmed_offer.get('prospectus_public_date') or not confirmed_offer.get('issuer'):
        return unknown('previous_confirmed_ipo_episode_required')
    if e.get('kind') not in {'allotment','exception'} or e['stock_code']!=confirmed_offer['stock_code']:return unknown('kind_or_issuer_episode_mismatch')
    official=[x for x in evidence if x.url==e['url'] and x.stock_code==e['stock_code'] and x.role==e['kind']]
    if not official:return unknown('official_followup_role_required')
    import fitz
    try:
        with fitz.open(path) as pdf:
            pages=[pdf[i].get_text() for i in range(min(3,len(pdf)))]
        text=compact('\n'.join(pages));sha=digest(path)
    except Exception:return unknown('followup_pdf_unreadable')
    codes={m.zfill(5) for m in re.findall(r'(?:股份代號|股份代号|StockCode)[:：]?(\d{1,5})(?!\d)',text,re.I)}
    if codes!={e['stock_code']} or confirmed_offer['issuer'] not in text:return unknown('followup_labelled_issuer_code_mismatch')
    day=datetime.fromisoformat(confirmed_offer['prospectus_public_date']).date()
    date_phrase=f'{day.year}年{day.month}月{day.day}日'
    reference=re.search(re.escape(date_phrase)+r'.{0,35}招股章程|招股章程.{0,35}'+re.escape(date_phrase),text)
    if reference is None:return unknown('exact_prior_prospectus_reference_required')
    title_pattern=r'(?:全球發售.{0,80})?(?:配發結果|分配結果|AllotmentResults)' if e['kind']=='allotment' else r'(?:延遲|延期|終止|取消|補充|更正).{0,20}(?:全球發售|上市)|全球發售.{0,20}(?:延遲|延期|終止|取消|補充|更正)'
    match=re.search(title_pattern,text,re.I)
    if match is None:return unknown('followup_cover_action_not_confirmed')
    public=datetime.fromisoformat(official[0].public_date).date()
    if not 0<=(public-day).days<=90:return unknown('followup_outside_confirmed_ipo_window')
    audit.append({'event_id':e['event_id'],'official_evidence':[asdict(x) for x in official],'pdf_sha256':sha,'pdf_pages':list(range(1,len(pages)+1)),'issuer':confirmed_offer['issuer'],'labelled_codes':sorted(codes),'prior_prospectus_quote':reference[0],'cover_action_quote':match[0]})
    return {'decision':'confirmed','reason':'official_followup_and_exact_prior_ipo_prospectus_reference','episode_id':confirmed_offer['episode_id'],'stock_code':e['stock_code'],'issuer':confirmed_offer['issuer'],'audit':audit,'assignments':[{'event_id':e['event_id'],'issuer_type':'common_equity','event_type':'ipo_'+e['kind'],'document_role':e['kind'],'episode_id':confirmed_offer['episode_id']}]}
