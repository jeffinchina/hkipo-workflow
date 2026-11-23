"""Stable, document-level event identities; no external side effects."""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from urllib.parse import urlsplit, urlunsplit

KINDS = frozenset({'offer', 'allotment', 'exception', 'preliminary', 'other'})

def utcnow():
    return datetime.now(timezone.utc).isoformat()

def canonical_url(url):
    p = urlsplit(url)
    if p.scheme != 'https' or not (p.hostname == 'hkexnews.hk' or (p.hostname or '').endswith('.hkexnews.hk')):
        raise ValueError('Only HTTPS HKEXnews document URLs are accepted')
    if p.username or p.password or p.port not in (None, 443):
        raise ValueError('Unexpected document URL authority')
    return urlunsplit(('https', p.hostname, p.path, p.query, ''))

@dataclass(frozen=True)
class DocumentEvent:
    event_id: str
    source: str
    source_id: str
    stock_code: str
    stock_name: str
    kind: str
    title: str
    url: str
    published_at: str
    discovered_at: str
    language: str = 'zh-HK'
    version: str = 'original'

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ValueError('Unknown event kind')
        if not self.stock_code.isdigit() or len(self.stock_code) > 5:
            raise ValueError('Invalid stock code')
        object.__setattr__(self, 'stock_code', self.stock_code.zfill(5))
        object.__setattr__(self, 'url', canonical_url(self.url))
        # Shared official document URL gives cross-source identity; source IDs are
        # retained as provenance. Kind and explicit version keep revisions apart.
        identity = [self.url, self.kind, self.version, self.language, self.stock_code]
        expected = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
        if self.event_id and self.event_id != expected:
            raise ValueError('Event ID does not match document identity')
        object.__setattr__(self, 'event_id', expected)

    def to_dict(self):
        return asdict(self)
