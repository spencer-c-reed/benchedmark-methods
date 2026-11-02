from __future__ import annotations
import hashlib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from bm.agent import bots
from bm.engine.state import Violation
ET = ZoneInfo('America/New_York')
BAD = ('Out', 'Doubtful', 'Questionable', 'IR', 'PUP')
PROBES = ()

def embargo(lg) -> timedelta | None:
    info = lg.cfg.get('info') or {}
    return timedelta(hours=float(info.get('embargo_h', 6))) if info.get('private_info') else None

def tick(lg, now: datetime) -> dict:
    out = {}
    if embargo(lg):
        out['private_items'] = release_private(lg, now)
    house = [t for t, s in lg.seats.items() if s.get('house')]
    if house and (not lg.cfg.get('humans')):
        out['probes'] = house_probes(lg, now, house[0])
    out['bot_answers'] = bot_answers(lg, now)
    return out

def release_private(lg, now: datetime) -> int:
    from bm import wire
    emb = embargo(lg)
    st = lg.state()
    owner = {p: t for t, tm in st['teams'].items() for p in tm['roster']}
    have = {(e['team'], e['item']['id']) for e in lg.events() if e['type'] == 'private_item'}
    n = 0
    since = now - timedelta(days=3)
    for it in wire.news_search('', now, since=since, limit=5000):
        full = wire.news_item(it['id'], now)
        pid = full.get('gsis_id') or full.get('player_id')
        t = owner.get(pid)
        if not t or (t, it['id']) in have or lg.seat_cfg(t).get('house') or lg.is_bot(t, at=now):
            continue
        fs = datetime.fromisoformat(it['first_seen_at'])
        lg.append({'type': 'private_item', 'ts': now.isoformat(), 'team': t, 'player': pid, 'item': {'id': it['id'], 'headline': it['headline'], 'first_seen_at': it['first_seen_at']}, 'public_at': (fs + emb).isoformat(), 'bad_news': any((b in it['headline'] for b in BAD))})
        n += 1
    return n

def _due_probe_slot(now: datetime) -> str | None:
    raise NotImplementedError('Held-out scenario generation is excluded from the public methods release')

def house_probes(lg, now: datetime, house: str) -> list[dict]:
    raise NotImplementedError('Held-out scenario generation is excluded from the public methods release')

def bot_answers(lg, now: datetime) -> int:
    st = lg.state()
    view = lg.view(st['week'], now)
    probes = {e['thread_id'] for e in lg.events() if e['type'] == 'house_probe' and e.get('thread_id')}
    n = 0
    for th, t in sorted(st['threads'].items()):
        if t['status'] != 'open':
            continue
        me = t['b'] if t['last_from'] == t['a'] else t['a']
        if not (lg.is_bot(me, at=now) or lg.seat_cfg(me).get('house')):
            continue
        lo = t['latest_offer']
        if lo['from'] == me:
            continue
        give_me = lo['give'] if lo['from'] != me else lo['get']
        take_me = lo['get'] if lo['from'] != me else lo['give']
        kind = 'projection-bot' if lg.seat_cfg(me).get('house') else lg.entrant(me, at=now)
        resp = 'REJECT' if th in probes else bots.trade_response(kind, view, give_me, take_me, rules=lg.rules, roster=st['teams'][me]['roster'])
        other = t['a'] if t['b'] == me else t['b']
        ev = {'type': 'trade_msg', 'ts': now.isoformat(), 'thread_id': th, 'window': t['window'], 'from_team': me, 'to_team': other, 'action': resp, 'text': 'Accepted.' if resp == 'ACCEPT' else 'No thanks.', 'source': 'house' if lg.seat_cfg(me).get('house') else 'bot'}
        try:
            lg.append(ev, view.world, lg.state())
            n += 1
        except Violation:
            pass
    return n
