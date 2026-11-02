from __future__ import annotations
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]

def _rows(path: Path):
    if not path.exists():
        return
    with open(path) as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            yield r.get('payload', r)

def _in(ts: str | None, since: str | None, until: str | None) -> bool:
    if not ts:
        return False
    t = datetime.fromisoformat(ts)
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    if since and t < datetime.fromisoformat(since):
        return False
    if until and t >= datetime.fromisoformat(until):
        return False
    return True

def measure(since: str | None=None, until: str | None=None, leagues: list[str] | None=None) -> dict:
    calls: dict = defaultdict(Counter)
    last_err: dict = {}
    for r in _rows(ROOT / 'logs' / 'gateway.jsonl'):
        if r.get('type') == 'model_call':
            last_err[r.get('request_id')] = f'{r.get('lane')}:{r.get('error') or 'OK'}'
        if r.get('type') != 'model_call' or not _in(r.get('ts'), since, until):
            continue
        if leagues and (not any((str(r.get('decision_id', '')).startswith(l + ':') for l in leagues))):
            continue
        k = f'{r.get('lane')}|{r.get('entrant')}'
        err = r.get('error')
        if err and 'no request sent' in (r.get('error_detail') or ''):
            err = err + '(not sent)'
        calls[k][err or 'OK'] += 1
    dec: dict = defaultdict(Counter)
    for lg in sorted((ROOT / 'leagues').glob('*/log.jsonl')):
        if leagues and lg.parent.name not in leagues:
            continue
        for r in _rows(lg):
            if r.get('type') != 'decision' or not r.get('model_call_ids'):
                continue
            if not _in(r.get('ts'), since, until):
                continue
            fd = r.get('forced_default')
            if not fd:
                dec[r.get('entrant')]['answered'] += 1
            else:
                real = last_err.get((r.get('model_call_ids') or [None])[-1], '?')
                dec[r.get('entrant')][f'default:{(fd.get('cause') or '?')[:20]}<{real}>'] += 1

    def pct(c: Counter, good: str) -> float | None:
        n = sum(c.values())
        return round(c[good] / n, 3) if n else None
    return {'measured_at': datetime.now(timezone.utc).isoformat(), 'since': since, 'until': until, 'leagues': leagues, 'calls': {k: {'n': sum(c.values()), 'ok_rate': pct(c, 'OK'), 'mix': dict(c)} for k, c in sorted(calls.items())}, 'decisions': {k: {'n': sum(c.values()), 'answered_rate': pct(c, 'answered'), 'mix': dict(c)} for k, c in sorted(dec.items())}}

def record(m: dict, label: str | None=None) -> None:
    p = ROOT / 'state' / 'lane-health.jsonl'
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, 'a') as f:
        f.write(json.dumps(dict(m, label=label)) + '\n')

def render(m: dict) -> str:
    out = [f'lane health {m['since'] or '-'} .. {m['until'] or 'now'}' + (f' leagues={m['leagues']}' if m['leagues'] else '')]
    out.append('CALLS (lane|entrant: ok/n, mix)')
    for k, v in m['calls'].items():
        out.append(f'  {k:48s} {v['mix'].get('OK', 0):4d}/{v['n']:<4d} {v['ok_rate']:.0%}  {v['mix']}')
    out.append('DECISIONS (entrant: answered/n, mix)')
    for k, v in m['decisions'].items():
        out.append(f'  {k:24s} {v['mix'].get('answered', 0):4d}/{v['n']:<4d} {v['answered_rate']:.0%}  {v['mix']}')
    return '\n'.join(out)
