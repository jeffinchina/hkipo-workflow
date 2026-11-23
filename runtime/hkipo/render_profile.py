"""Validated, versioned defaults learned from the author's saved draft."""
import json
import os
import re
from pathlib import Path

PROFILE_PATH = Path(os.environ.get('HKIPO_PROFILE_PATH', str(Path(__file__).resolve().parents[2]/'private/draft-profile.json'))).expanduser()
LEGACY = {
    'title_color':'000000', 'heading_color':'2B77BF', 'body_color':'191919',
    'cta_color':'191919', 'risk_color':'737373', 'font_size_px':16,
    'line_height_px':32, 'paragraph_space_px':16, 'heading_space_px':16,
    'side_padding_px':8, 'text_align':'left',
    'body_font_weight':400,'heading_font_weight':600,'field_label_font_weight':700,
    'cta_highlight_color':'191919','cta_highlight_phrases':[],
    'qr_margin_px':0,'numbered_items':False,
}


def validate_style(value):
    if not isinstance(value,dict) or set(value)!=set(LEGACY):
        raise ValueError('Complete known render style required')
    for key in ('title_color','heading_color','body_color','cta_color','risk_color','cta_highlight_color'):
        css_color(value[key])
    for key,low,high in [('font_size_px',10,32),('line_height_px',12,96),
                         ('paragraph_space_px',0,80),('heading_space_px',0,80),('side_padding_px',0,48),('qr_margin_px',0,48)]:
        if type(value[key]) not in (int,float) or not low<=value[key]<=high:
            raise ValueError('Invalid render dimension')
    if value['line_height_px']<value['font_size_px'] or value['text_align'] not in {'left','justify'}:
        raise ValueError('Invalid line layout')
    if any(value[k] not in (300,400,500,600,700) for k in ('body_font_weight','heading_font_weight','field_label_font_weight')):
        raise ValueError('Invalid font weight')
    if (type(value['numbered_items']) is not bool or not isinstance(value['cta_highlight_phrases'],list)
            or any(p not in ('点个赞','点个关注') for p in value['cta_highlight_phrases'])
            or len(set(value['cta_highlight_phrases']))!=len(value['cta_highlight_phrases'])):
        raise ValueError('Invalid CTA or list presentation')
    return dict(value)


def css_color(value):
    if isinstance(value,str) and re.fullmatch(r'[A-Fa-f0-9]{6}',value):
        return '#'+value
    match=re.fullmatch(r'rgba\((\d{1,3}),(\d{1,3}),(\d{1,3}),(0(?:\.\d+)?|1(?:\.0+)?)\)',value) if isinstance(value,str) else None
    if match and all(int(c)<=255 for c in match.groups()[:3]):
        return value
    raise ValueError('Invalid render color')


def word_color(value):
    if css_color(value).startswith('#'):
        return value
    from decimal import Decimal,ROUND_HALF_UP
    r,g,b,alpha=re.fullmatch(r'rgba\((\d+),(\d+),(\d+),([.\d]+)\)',value).groups()
    a=Decimal(alpha)
    return ''.join(f'{int((Decimal(c)*a+255*(1-a)).quantize(Decimal(1),rounding=ROUND_HALF_UP)):02X}' for c in (r,g,b))


def defaults():
    if not PROFILE_PATH.exists():
        return {'version':1,'style':dict(LEGACY),'author':'','need_open_comment':1,
                'only_fans_can_comment':0,'thumb_media_id':None}
    value=json.loads(PROFILE_PATH.read_text())
    validate_style(value['style'])
    author=value.get('author','')
    if not isinstance(author,str) or len(author)>16 or any(c in author for c in '\r\n'):
        raise ValueError('Invalid draft author')
    if value.get('need_open_comment') not in (0,1) or value.get('only_fans_can_comment') not in (0,1):
        raise ValueError('Invalid comment defaults')
    cover=value.get('thumb_media_id')
    if cover is not None and (not isinstance(cover,str) or not cover.strip()):
        raise ValueError('Invalid default cover')
    return value


def draft_metadata(author=None, need_open_comment=None, only_fans_can_comment=None):
    value=defaults()
    result={'author':value['author'] if author is None else author,
        'need_open_comment':value['need_open_comment'] if need_open_comment is None else int(need_open_comment),
        'only_fans_can_comment':value['only_fans_can_comment'] if only_fans_can_comment is None else int(only_fans_can_comment)}
    if (not isinstance(result['author'],str) or len(result['author'])>16
            or any(c in result['author'] for c in '\r\n')
            or any(type(result[k]) is not int or result[k] not in (0,1) for k in ('need_open_comment','only_fans_can_comment'))):
        raise ValueError('Invalid draft metadata')
    return result
