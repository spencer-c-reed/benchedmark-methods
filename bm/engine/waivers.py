from __future__ import annotations
import hashlib
import random
from .rules import Rules
from .state import Violation, World, apply_event

def resolve_waivers(rules: Rules, world: World, state: dict, bids: list[dict], worst_first: list[str], seed: str, ts: str) -> tuple[list[dict], list[dict]]:
    rng = random.Random(int(hashlib.sha256(seed.encode()).hexdigest()[:16], 16))
    coin = {t: rng.random() for t in sorted(state['teams'])}
    rank = {t: i for i, t in enumerate(worst_first)}
    pending = [dict(b, _i=i) for i, b in enumerate(bids)]
    results: dict[int, dict] = {}
    awards: list[dict] = []
    s = state
    while pending:
        firsts = {}
        for b in sorted(pending, key=lambda b: (b.get('priority', 99), b['_i'])):
            firsts.setdefault(b['team'], b)
        cands = sorted(firsts.values(), key=lambda b: (-int(b['amount']), rank.get(b['team'], 99), coin[b['team']]))
        top = cands[0]
        rivals = [b for b in pending if b['add'] == top['add']]
        rivals.sort(key=lambda b: (-int(b['amount']), rank.get(b['team'], 99), coin[b['team']]))
        won = False
        for b in rivals:
            ev = {'type': 'waiver_award', 'ts': ts, 'team': b['team'], 'add': b['add'], 'drop': b.get('drop'), 'amount': int(b['amount']), 'week': state['week']}
            if won:
                results[b['_i']] = {**_pub(b), 'result': 'lost', 'detail': 'outbid or tie-break'}
                continue
            try:
                s = apply_event(rules, world, s, ev)
            except Violation as e:
                results[b['_i']] = {**_pub(b), 'result': 'invalid', 'detail': e.code + ': ' + e.detail}
                continue
            awards.append(ev)
            results[b['_i']] = {**_pub(b), 'result': 'won'}
            won = True
        pending = [b for b in pending if b['add'] != top['add']]
        for b in list(pending):
            if b.get('drop') and b['drop'] not in s['teams'][b['team']]['roster']:
                results[b['_i']] = {**_pub(b), 'result': 'invalid', 'detail': 'drop player no longer on roster'}
                pending.remove(b)
    return (awards, [results[i] for i in sorted(results)])

def _pub(b: dict) -> dict:
    return {k: b.get(k) for k in ('team', 'add', 'drop', 'amount', 'priority')}
