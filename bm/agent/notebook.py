from __future__ import annotations
import hashlib
SECTIONS = ('strategy', 'watchlist', 'trade_targets', 'opponent_notes', 'commitments')
LIMIT = 12000
SHOWN_EACH_WAKE = ('strategy', 'commitments')

def state(events: list[dict], team: str, until_seq: int | None=None) -> dict:
    nb = {s: '' for s in SECTIONS}
    v = 0
    for e in events:
        if until_seq is not None and e.get('seq', 0) > until_seq:
            break
        if e['type'] == 'notebook_edit' and e['team'] == team:
            nb[e['section']] = e['text']
            v = e['version']
    nb['_version'] = v
    return nb

def toc(nb: dict) -> str:
    return ' · '.join((f'{s} ({len(nb[s])} chars)' for s in SECTIONS)) + f' · total {sum((len(nb[s]) for s in SECTIONS))}/{LIMIT}'

def opening(nb: dict) -> str:
    parts = [f'[{s}]\n{nb[s] or '(empty)'}' for s in SHOWN_EACH_WAKE]
    return '\n'.join(parts) + '\nContents: ' + toc(nb)

def apply(nb: dict, op: str, section: str | None=None, text: str='', query: str='') -> tuple[dict | None, str]:
    if op == 'toc':
        return (None, toc(nb))
    if op == 'search':
        q = (query or text).lower()
        hits = [f'[{s}] …{nb[s][max(0, i - 80):i + 160]}…' for s in SECTIONS for i in [nb[s].lower().find(q)] if q and i >= 0]
        return (None, '\n'.join(hits) or 'no matches')
    if section not in SECTIONS:
        return (None, f'error: section must be one of {', '.join(SECTIONS)}')
    if op == 'read':
        return (None, nb[section] or '(empty)')
    if op not in ('append', 'edit'):
        return (None, 'error: op must be read, append, edit, search or toc')
    new = nb[section] + ('\n' if nb[section] and text else '') + text if op == 'append' else text
    total = sum((len(nb[s]) for s in SECTIONS if s != section)) + len(new)
    if total > LIMIT:
        return (None, f'error: notebook would be {total} chars; the limit is {LIMIT}. Edit something shorter first.')
    return ({'section': section, 'op': op, 'text': new}, f'ok: {section} now {len(new)} chars (v{nb['_version'] + 1})')

def edit_event(team: str, nb: dict, change: dict, ts: str, wake_id: str | None) -> dict:
    return {'type': 'notebook_edit', 'ts': ts, 'team': team, 'version': nb['_version'] + 1, 'section': change['section'], 'op': change['op'], 'text': change['text'], 'sha256': hashlib.sha256(change['text'].encode()).hexdigest(), 'wake_id': wake_id}
