from __future__ import annotations
import json
import re
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
import yaml
from bm.gateway.core import Gateway, ModelRequest
from bm.log import ChainLog, put_blob
from .harness import extract_json, sha
ROOT = Path(__file__).resolve().parents[2]
IDENT = ROOT / 'identities'
LOG = ChainLog(ROOT / 'identities' / 'log.jsonl')
MAX_SVG = 20000
SVGNS = 'http://www.w3.org/2000/svg'
SYSTEM = 'You are an AI model entered as a team manager in BenchedMark, a research benchmark where AI models run fantasy sports teams against each other. Before your first season you create your team identity. It will be used for every team you manage, in every league and sport, and shown publicly with your model name.'
TASK = 'Create your team identity. Reply with ONE JSON object and nothing else:\n{"team_name": "<max 30 chars>", "tagline": "<max 80 chars>", "design_note": "<one sentence on the idea>", "svg": "<the logo>"}\n\nLogo rules:\n- A complete standalone SVG: <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256">...</svg>, under 20 KB.\n- Only basic shapes, paths, gradients, groups and text. No scripts, no images, no external links, no CSS url(), no fonts beyond generic families (sans-serif, serif, monospace).\n- It must read clearly at 64x64 pixels.\n- Original work: no real team, league or brand names, logos, marks or mascots; nothing offensive.\nName rules: original; not the name of a real sports team or league; no trademarks; nothing offensive.'
ALLOWED = {'svg', 'g', 'defs', 'title', 'desc', 'path', 'rect', 'circle', 'ellipse', 'line', 'polyline', 'polygon', 'text', 'tspan', 'lineargradient', 'radialgradient', 'stop', 'clippath', 'mask', 'use', 'symbol'}
ALLOWED_ATTR = {'xmlns', 'viewbox', 'width', 'height', 'x', 'y', 'x1', 'y1', 'x2', 'y2', 'cx', 'cy', 'r', 'rx', 'ry', 'fx', 'fy', 'd', 'points', 'fill', 'fill-opacity', 'fill-rule', 'stroke', 'stroke-width', 'stroke-opacity', 'stroke-linecap', 'stroke-linejoin', 'stroke-dasharray', 'stroke-miterlimit', 'opacity', 'transform', 'id', 'class', 'offset', 'stop-color', 'stop-opacity', 'gradientunits', 'gradienttransform', 'spreadmethod', 'clip-path', 'clippathunits', 'mask', 'maskunits', 'font-family', 'font-size', 'font-weight', 'font-style', 'text-anchor', 'dominant-baseline', 'letter-spacing', 'dx', 'dy', 'href', 'preserveaspectratio', 'style', 'version', 'paint-order', 'vector-effect'}
BAD_VALUE = re.compile('(url\\s*\\(\\s*[\'\\"]?\\s*(?!#)|javascript:|data:|expression\\s*\\(|@import|<|&#)', re.I)
STYLE_OK = re.compile('^[\\w\\s\\-:;.,#%()]*$')

def sanitize(svg: str) -> tuple[str | None, list[str]]:
    problems = []
    if not isinstance(svg, str) or not svg.strip():
        return (None, ['empty svg'])
    if len(svg.encode()) > MAX_SVG:
        return (None, [f'svg is {len(svg.encode())} bytes; max {MAX_SVG}'])
    if re.search('<!DOCTYPE|<!ENTITY', svg, re.I):
        return (None, ['DOCTYPE/ENTITY not allowed'])
    try:
        root = ET.fromstring(svg.strip())
    except ET.ParseError as e:
        return (None, [f'not well-formed XML: {e}'])

    def local(tag: str) -> str:
        return tag.split('}', 1)[-1].lower()
    if local(root.tag) != 'svg':
        return (None, ['root element must be <svg>'])

    def clean(el):
        for child in list(el):
            if not isinstance(child.tag, str) or local(child.tag) not in ALLOWED:
                problems.append(f'removed <{(local(child.tag) if isinstance(child.tag, str) else 'comment')}>')
                el.remove(child)
                continue
            clean(child)
        for k in list(el.attrib):
            lk = local(k)
            v = el.attrib[k]
            if lk.startswith('on') or lk not in ALLOWED_ATTR:
                problems.append(f'removed attribute {lk}')
                del el.attrib[k]
                continue
            if lk == 'href' and (not v.startswith('#')):
                problems.append('removed external href')
                del el.attrib[k]
                continue
            if BAD_VALUE.search(v) or (lk == 'style' and (not STYLE_OK.match(v))):
                problems.append(f'removed unsafe value in {lk}')
                del el.attrib[k]
        if el.text and BAD_VALUE.search(el.text):
            el.text = re.sub(BAD_VALUE, '', el.text)
    clean(root)
    if not any((local(k) == 'viewbox' for k in root.attrib)):
        root.set('viewBox', '0 0 256 256')
    for k in ('width', 'height'):
        root.attrib.pop(k, None)
    ET.register_namespace('', SVGNS)
    out = ET.tostring(root, encoding='unicode')
    if 'xmlns="http://www.w3.org/2000/svg"' not in out:
        out = out.replace('<svg', '<svg xmlns="http://www.w3.org/2000/svg"', 1)
    if not any((local(e.tag) in ('path', 'rect', 'circle', 'ellipse', 'polygon', 'polyline', 'line', 'text') for e in root.iter())):
        return (None, problems + ['no visible shapes'])
    return (out, problems)

def render_png(svg_path: Path, size: int) -> Path:
    out = svg_path.with_name(f'logo-{size}.png')
    subprocess.run(['convert', '-limit', 'memory', '256MiB', '-limit', 'time', '20', '-background', 'none', '-density', '192', str(svg_path), '-resize', f'{size}x{size}', str(out)], check=True, timeout=60, capture_output=True)
    return out

def identity_id(entrant_id: str, variant: str='standard') -> str:
    return entrant_id if variant == 'standard' else f'{entrant_id}+{variant}'

def load(entrant_id: str, variant: str='standard') -> dict | None:
    p = IDENT / identity_id(entrant_id, variant) / 'identity.json'
    return json.loads(p.read_text()) if p.exists() else None
BOT_IDENTITIES = {'projection-bot': ('Projection Bot', 'Fifty lines of arithmetic. Beat it.'), 'adp-bot': ('ADP Bot', 'Drafts the box score, sets it and forgets it.'), 'random-bot': ('Random Bot', 'Every lineup a coin flip. The floor.'), 'espn-auto-bot': ('Best-Available Bot', 'Drafts the best player left on our projection, starts by projection every week.'), 'yahoo-auto-bot': ('Starters-First Bot', "Fills starters first on Sleeper's ranks, then only swaps out byes and injuries."), 'sleeper-auto-bot': ('Sleeper Autopilot', 'Sleeper CPU autopick, then never opened the app again.'), 'consensus-bot': ('Consensus Bot', "Drafts the consensus cheat sheet, starts the app's projections.")}

def onboard(gw: Gateway, entrant_id: str, variant: str='standard') -> dict:
    iid = identity_id(entrant_id, variant)
    have = load(entrant_id, variant)
    if have and (have.get('status') == 'ok' or have.get('kind') == 'baseline'):
        return have
    d = IDENT / iid
    d.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    if entrant_id in BOT_IDENTITIES:
        name, tag = BOT_IDENTITIES[entrant_id]
        rec = {'identity_id': iid, 'entrant_id': entrant_id, 'variant': variant, 'kind': 'baseline', 'team_name': name, 'tagline': tag, 'logo': None, 'created_at': now, 'status': 'ok'}
        (d / 'identity.json').write_text(json.dumps(rec, indent=1))
        return rec
    prompt = TASK
    calls, problems_all, parsed, clean = ([], [], None, None)
    for attempt in range(2):
        r = gw.call(ModelRequest(entrant_id=entrant_id, decision_id=f'onboard:{iid}:a{attempt + 1}', system=SYSTEM, prompt=prompt, max_output_tokens=8000))
        calls.append({'request_id': r.request_id, 'error': r.error, 'usage': r.usage, 'cost': r.cost})
        if r.error:
            problems_all.append(f'gateway {r.error}: {(r.error_detail or '')[:200]}')
            break
        parsed = extract_json(r.text)
        if not parsed:
            problems_all.append('unparseable reply')
            prompt = TASK + '\n\nYour previous reply was not one valid JSON object. Try again.'
            continue
        name = str(parsed.get('team_name') or '').strip()[:30]
        tag = str(parsed.get('tagline') or '').strip()[:80]
        clean, probs = sanitize(parsed.get('svg') or '')
        problems_all += probs
        if clean and name:
            break
        prompt = TASK + '\n\nYour previous logo was rejected: ' + '; '.join(probs[:6] or ['missing team_name']) + '. Fix it and reply again.'
        clean = None
    rec = {'identity_id': iid, 'entrant_id': entrant_id, 'variant': variant, 'kind': 'llm', 'created_at': now, 'team_name': (parsed or {}).get('team_name', '')[:30] if parsed else None, 'tagline': str((parsed or {}).get('tagline') or '')[:80] if parsed else None, 'design_note': str((parsed or {}).get('design_note') or '')[:300] if parsed else None, 'sanitizer_notes': problems_all[:20], 'model_call_ids': [c['request_id'] for c in calls], 'usage': {'calls': len(calls), 'input': sum(((c['usage'] or {}).get('input', 0) or 0 for c in calls)), 'output': sum(((c['usage'] or {}).get('output', 0) or 0 for c in calls)), 'usd_eq': round(sum(((c['cost'] or {}).get('usd_eq') or 0 for c in calls)), 5)}, 'raw_svg_blob': put_blob((parsed or {}).get('svg') or '') if parsed else None}
    if clean and rec['team_name']:
        (d / 'logo.svg').write_text(clean)
        try:
            render_png(d / 'logo.svg', 64)
            render_png(d / 'logo.svg', 256)
            rec['logo'] = {'svg': f'identities/{iid}/logo.svg', 'png_64': f'identities/{iid}/logo-64.png', 'png_256': f'identities/{iid}/logo-256.png', 'sha256': sha(clean), 'bytes': len(clean.encode())}
            rec['status'] = 'ok'
        except Exception as e:
            rec['status'] = 'render_failed'
            rec['sanitizer_notes'].append(f'render failed: {e}')
    else:
        rec['status'] = 'failed'
    (d / 'identity.json').write_text(json.dumps(rec, indent=1))
    LOG.append({'type': 'onboarding', 'ts': now, **{k: v for k, v in rec.items()}})
    return rec

def onboard_all(entrant_ids: list[str]) -> list[dict]:
    from concurrent.futures import ThreadPoolExecutor
    gw = Gateway()
    with ThreadPoolExecutor(6) as ex:
        return list(ex.map(lambda e: onboard(gw, e), entrant_ids))
