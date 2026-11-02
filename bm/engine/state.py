from __future__ import annotations
import copy
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping
from .rules import Rules

class Violation(Exception):

    def __init__(self, code: str, detail: str=''):
        super().__init__(f'{code}: {detail}')
        self.code = code
        self.detail = detail

    def as_dict(self) -> dict:
        return {'code': self.code, 'detail': self.detail}

@dataclass(frozen=True)
class World:
    players: Mapping[str, Mapping[str, Any]]
    kickoffs: Mapping[int, Mapping[str, str]]
    team_by_week: Mapping[int, Mapping[str, str]] = None

    def pos(self, pid: str) -> str:
        p = self.players.get(pid)
        if p is None:
            raise Violation('UNKNOWN_PLAYER', pid)
        return p['pos']

    def kickoff(self, week: int, pid: str) -> datetime | None:
        p = self.players.get(pid)
        if not p:
            return None
        team = ((self.team_by_week or {}).get(week) or {}).get(pid, p['team'])
        k = self.kickoffs.get(week, {}).get(team)
        return datetime.fromisoformat(k) if k else None

    def locked(self, week: int, pid: str, now: datetime) -> bool:
        k = self.kickoff(week, pid)
        return k is not None and now >= k

def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=False, default=str).encode()

def state_hash(state: dict) -> str:
    return hashlib.sha256(canonical(state)).hexdigest()

def genesis_state(rules: Rules, league_id: str, team_ids: list[str], draft_order: list[str], week: int) -> dict:
    return {'league_id': league_id, 'rules_hash': rules.rules_hash, 'week': week, 'phase': 'DRAFT', 'seq': 0, 'teams': {t: {'roster': [], 'lineup': {s: None for s in rules.starter_slots}, 'faab': rules.faab_budget} for t in team_ids}, 'draft': {'order': list(draft_order), 'pick': 0, 'rounds': rules.roster_size}, 'threads': {}, 'chatter': {}, 'scores': {}}

def owner_of(state: dict, pid: str) -> str | None:
    for tid, t in state['teams'].items():
        if pid in t['roster']:
            return tid
    return None

def draft_team_at(state: dict, pick: int) -> str:
    order = state['draft']['order']
    n = len(order)
    rnd, idx = divmod(pick, n)
    return order[idx] if rnd % 2 == 0 else order[n - 1 - idx]

def current_drafter(state: dict) -> str | None:
    d = state['draft']
    if d['pick'] >= d['rounds'] * len(d['order']):
        return None
    return draft_team_at(state, d['pick'])

def pos_counts(rules: Rules, world: World, roster: list[str]) -> dict[str, int]:
    c: dict[str, int] = {}
    for pid in roster:
        p = world.pos(pid)
        c[p] = c.get(p, 0) + 1
    return c

def can_fill_starters(rules: Rules, world: World, roster: list[str], picks_left: int) -> bool:
    unfilled = _unfilled_starters(rules, world, roster)
    return unfilled <= picks_left

def _unfilled_starters(rules: Rules, world: World, roster: list[str]) -> int:
    avail = pos_counts(rules, world, roster)
    unfilled = 0
    for slot in rules.starter_slots:
        st = rules.slot_type(slot)
        if st == 'FLEX':
            continue
        need_pos = rules.eligibility[st][0]
        if avail.get(need_pos, 0) > 0:
            avail[need_pos] -= 1
        else:
            unfilled += 1
    for slot in rules.starter_slots:
        if rules.slot_type(slot) != 'FLEX':
            continue
        for p in rules.eligibility['FLEX']:
            if avail.get(p, 0) > 0:
                avail[p] -= 1
                break
        else:
            unfilled += 1
    return unfilled

def lineup_violations(rules: Rules, world: World, state: dict, tid: str, lineup: Mapping[str, str | None], now: datetime) -> list[Violation]:
    v: list[Violation] = []
    team = state['teams'][tid]
    week = state['week']
    seen = set()
    for slot, pid in lineup.items():
        if slot not in team['lineup']:
            v.append(Violation('BAD_SLOT', slot))
            continue
        if pid is None:
            continue
        if pid in seen:
            v.append(Violation('DUPLICATE_PLAYER', pid))
        seen.add(pid)
        if pid not in team['roster']:
            v.append(Violation('NOT_ON_ROSTER', pid))
            continue
        if not rules.eligible(slot, world.pos(pid)):
            v.append(Violation('INELIGIBLE_SLOT', f'{pid} ({world.pos(pid)}) cannot play {slot}'))
    full = {s: lineup.get(s, team['lineup'].get(s)) for s in team['lineup']}
    for slot in team['lineup']:
        old, new = (team['lineup'][slot], full[slot])
        if old == new:
            continue
        if old is not None and world.locked(week, old, now):
            v.append(Violation('PLAYER_LOCKED', f'{old} in {slot} has kicked off and cannot be moved'))
        if new is not None and world.locked(week, new, now):
            v.append(Violation('PLAYER_LOCKED', f'{new} has kicked off and cannot be moved into {slot}'))
    return v

def offer_violations(rules: Rules, world: World, state: dict, sender: str, receiver: str, give: list[str], get: list[str], now: datetime, faab_give: int=0, faab_get: int=0) -> list[Violation]:
    v: list[Violation] = []
    mx = rules.trades['max_players_per_side']
    if not give and (not get) and (not faab_give) and (not faab_get):
        v.append(Violation('EMPTY_OFFER', ''))
    if faab_give or faab_get:
        if not rules.trades.get('faab_tradeable'):
            v.append(Violation('FAAB_NOT_TRADEABLE', "this league's rules do not allow FAAB in trades"))
        for who, amt in ((sender, faab_give), (receiver, faab_get)):
            if not isinstance(amt, int) or isinstance(amt, bool) or amt < 0:
                v.append(Violation('BAD_FAAB', f'{amt!r} must be a whole number of dollars >= 0'))
            elif amt > state['teams'][who]['faab']:
                v.append(Violation('FAAB_SHORT', f'{who} has ${state['teams'][who]['faab']}, offer needs ${amt}'))
    if len(give) > mx or len(get) > mx:
        v.append(Violation('TOO_MANY_PLAYERS', f'max {mx} per side'))
    if state['week'] > rules.trades['deadline_week']:
        v.append(Violation('POST_DEADLINE', f'week {state['week']}'))
    for pid in give:
        if pid not in state['teams'][sender]['roster']:
            v.append(Violation('NOT_OWNED', f'{pid} is not on {sender}'))
        elif world.locked(state['week'], pid, now):
            v.append(Violation('PLAYER_LOCKED', pid))
    for pid in get:
        if pid not in state['teams'][receiver]['roster']:
            v.append(Violation('NOT_OWNED', f'{pid} is not on {receiver}'))
        elif world.locked(state['week'], pid, now):
            v.append(Violation('PLAYER_LOCKED', pid))
    size = rules.roster_size
    if len(state['teams'][sender]['roster']) - len(give) + len(get) > size:
        v.append(Violation('ROSTER_FULL', f'{sender} would exceed {size}'))
    if len(state['teams'][receiver]['roster']) - len(get) + len(give) > size:
        v.append(Violation('ROSTER_FULL', f'{receiver} would exceed {size}'))
    for tid, out, inn in ((sender, give, get), (receiver, get, give)):
        after = [p for p in state['teams'][tid]['roster'] if p not in out] + [p for p in inn if p in world.players]
        for pos, cap in rules.max_per_position.items():
            if sum((1 for p in after if world.pos(p) == pos)) > cap:
                v.append(Violation('POSITION_CAP', f'{tid} would have more than {cap} {pos}'))
    return v

def _faab(offer: dict) -> tuple[int, int]:
    return (offer.get('faab_give', 0), offer.get('faab_get', 0))

def _offer_state(frm: str, offer: dict) -> dict:
    lo = {'from': frm, 'give': list(offer['give']), 'get': list(offer['get'])}
    for k in ('faab_give', 'faab_get'):
        if offer.get(k):
            lo[k] = offer[k]
    return lo

def _remove_from_roster(team: dict, pid: str) -> None:
    team['roster'].remove(pid)
    for s, p in team['lineup'].items():
        if p == pid:
            team['lineup'][s] = None

def _add_to_roster(team: dict, pid: str) -> None:
    team['roster'].append(pid)
    team['roster'].sort()
STATEFUL = {'draft_pick', 'draft_complete', 'lineup_set', 'waiver_award', 'drop', 'trade_msg', 'chatter', 'week_scored', 'week_advance', 'offer_expired', 'wallet_convert'}
TIME_RULE = 'max_ts_logged_at'

def effective_time(ev: dict) -> datetime:
    t = datetime.fromisoformat(ev['ts'])
    if ev.get('time_rule') == TIME_RULE and ev.get('logged_at'):
        return max(t, datetime.fromisoformat(ev['logged_at']))
    return t

def apply_event(rules: Rules, world: World, state: dict, ev: dict) -> dict:
    t = ev['type']
    if t not in STATEFUL:
        return state
    s = copy.deepcopy(state)
    now = effective_time(ev)
    if t == 'draft_pick':
        if s['phase'] != 'DRAFT':
            raise Violation('NOT_DRAFTING', s['phase'])
        tid, pid = (ev['team'], ev['player'])
        if current_drafter(s) != tid:
            raise Violation('NOT_ON_CLOCK', f'{tid} picked but {current_drafter(s)} is on the clock')
        if pid not in world.players:
            raise Violation('UNKNOWN_PLAYER', pid)
        if owner_of(s, pid):
            raise Violation('ALREADY_OWNED', pid)
        roster = s['teams'][tid]['roster']
        if len(roster) >= rules.roster_size:
            raise Violation('ROSTER_FULL', tid)
        after = roster + [pid]
        picks_left = rules.roster_size - len(after)
        if not can_fill_starters(rules, world, after, picks_left):
            raise Violation('CANNOT_FILL_STARTERS', f'after {pid}, {tid} cannot fill its starting slots with {picks_left} picks left')
        pos = world.pos(pid)
        cap = rules.max_per_position.get(pos)
        if cap is not None and sum((1 for p in after if world.pos(p) == pos)) > cap:
            raise Violation('POSITION_CAP', f'max {cap} {pos}')
        _add_to_roster(s['teams'][tid], pid)
        s['draft']['pick'] += 1
    elif t == 'draft_complete':
        if current_drafter(s) is not None:
            raise Violation('DRAFT_INCOMPLETE', str(s['draft']['pick']))
        s['phase'] = 'SEASON'
    elif t == 'lineup_set':
        if s['phase'] != 'SEASON':
            raise Violation('NOT_IN_SEASON', s['phase'])
        vs = lineup_violations(rules, world, s, ev['team'], ev['lineup'], now)
        if vs:
            raise vs[0]
        for slot, pid in ev['lineup'].items():
            s['teams'][ev['team']]['lineup'][slot] = pid
    elif t == 'waiver_award':
        tid, add, drop, amt = (ev['team'], ev['add'], ev.get('drop'), int(ev['amount']))
        team = s['teams'][tid]
        if add not in world.players:
            raise Violation('UNKNOWN_PLAYER', add)
        if owner_of(s, add):
            raise Violation('ALREADY_OWNED', add)
        if amt < rules.faab_min_bid or amt > team['faab']:
            raise Violation('BAD_BID', f'{amt} with {team['faab']} left')
        if drop:
            if drop not in team['roster']:
                raise Violation('NOT_OWNED', drop)
            if world.locked(s['week'], drop, now):
                raise Violation('PLAYER_LOCKED', drop)
            _remove_from_roster(team, drop)
        if len(team['roster']) >= rules.roster_size:
            raise Violation('ROSTER_FULL', tid)
        pos = world.pos(add)
        cap = rules.max_per_position.get(pos)
        if cap is not None and sum((1 for p in team['roster'] if world.pos(p) == pos)) + 1 > cap:
            raise Violation('POSITION_CAP', f'max {cap} {pos}')
        _add_to_roster(team, add)
        team['faab'] -= amt
    elif t == 'drop':
        team = s['teams'][ev['team']]
        if ev['player'] not in team['roster']:
            raise Violation('NOT_OWNED', ev['player'])
        if world.locked(s['week'], ev['player'], now):
            raise Violation('PLAYER_LOCKED', ev['player'])
        _remove_from_roster(team, ev['player'])
    elif t == 'trade_msg':
        _apply_trade_msg(rules, world, s, ev, now)
    elif t == 'chatter':
        key = ev['window']
        c = s['chatter'].setdefault(key, {})
        if c.get(ev['team'], 0) >= rules.chatter_posts_per_window:
            raise Violation('CHATTER_QUOTA', key)
        if len(ev['text']) > rules.max_msg_chars:
            raise Violation('TOO_LONG', str(len(ev['text'])))
        c[ev['team']] = c.get(ev['team'], 0) + 1
    elif t == 'week_scored':
        s['scores'][str(ev['week'])] = dict(ev['points'])
    elif t == 'offer_expired':
        th = s['threads'].get(ev['thread_id'])
        if th is None or th['status'] != 'open':
            raise Violation('THREAD_CLOSED', ev['thread_id'])
        th['status'] = 'expired'
    elif t == 'wallet_convert':
        if not (rules.raw.get('wallet') or {}).get('convertible'):
            raise Violation('WALLET_NOT_CONVERTIBLE', 'this league has separate FAAB and token pools')
        team = s['teams'][ev['team']]
        d = int(ev['faab_delta'])
        if team['faab'] + d < 0:
            raise Violation('FAAB_SHORT', f'{ev['team']} has ${team['faab']}')
        team['faab'] += d
    elif t == 'week_advance':
        if ev['week'] != s['week'] + 1:
            raise Violation('BAD_WEEK', f'{s['week']} -> {ev['week']}')
        s['week'] = ev['week']
        for th in s['threads'].values():
            if th['status'] == 'open':
                th['status'] = 'expired'
    s['seq'] = state['seq'] + 1
    return s

def _apply_trade_msg(rules: Rules, world: World, s: dict, ev: dict, now: datetime) -> None:
    th_id, frm, to, action = (ev['thread_id'], ev['from_team'], ev['to_team'], ev['action'])
    text = ev.get('text') or ''
    if len(text) > rules.max_msg_chars:
        raise Violation('TOO_LONG', str(len(text)))
    if frm == to or frm not in s['teams'] or to not in s['teams']:
        raise Violation('BAD_PARTIES', f'{frm}->{to}')
    th = s['threads'].get(th_id)
    if action == 'PROPOSE':
        if th is not None:
            raise Violation('THREAD_EXISTS', th_id)
        window = ev['window']
        opened = sum((1 for x in s['threads'].values() if x['a'] == frm and x['window'] == window))
        if opened >= rules.trades['max_threads_per_team_per_window']:
            raise Violation('THREAD_QUOTA', f'{frm} already opened {opened} threads in {window}')
        offer = ev['offer']
        vs = offer_violations(rules, world, s, frm, to, offer['give'], offer['get'], now, *_faab(offer))
        if vs:
            raise vs[0]
        s['threads'][th_id] = {'a': frm, 'b': to, 'window': window, 'week': s['week'], 'msgs': 1, 'latest_offer': _offer_state(frm, offer), 'status': 'open', 'last_from': frm}
        return
    if th is None:
        raise Violation('NO_THREAD', th_id)
    if th['status'] != 'open':
        raise Violation('THREAD_CLOSED', th['status'])
    if {frm, to} != {th['a'], th['b']}:
        raise Violation('BAD_PARTIES', f'{frm}->{to} not in thread')
    if th['last_from'] == frm:
        raise Violation('NOT_YOUR_TURN', frm)
    if th['msgs'] >= rules.trades['max_msgs_per_thread']:
        raise Violation('THREAD_MSG_CAP', str(th['msgs']))
    th['msgs'] += 1
    th['last_from'] = frm
    if action == 'COUNTER':
        offer = ev['offer']
        vs = offer_violations(rules, world, s, frm, to, offer['give'], offer['get'], now, *_faab(offer))
        if vs:
            raise vs[0]
        th['latest_offer'] = _offer_state(frm, offer)
    elif action == 'ACCEPT':
        lo = th['latest_offer']
        if lo is None or lo['from'] == frm:
            raise Violation('NOTHING_TO_ACCEPT', 'the latest offer is your own')
        vs = offer_violations(rules, world, s, lo['from'], frm, lo['give'], lo['get'], now, *_faab(lo))
        if vs:
            raise vs[0]
        a_team, b_team = (s['teams'][lo['from']], s['teams'][frm])
        for pid in lo['give']:
            _remove_from_roster(a_team, pid)
            _add_to_roster(b_team, pid)
        for pid in lo['get']:
            _remove_from_roster(b_team, pid)
            _add_to_roster(a_team, pid)
        fg, fr = _faab(lo)
        a_team['faab'] += fr - fg
        b_team['faab'] += fg - fr
        th['status'] = 'accepted'
        moved = set(lo['give']) | set(lo['get'])
        for oid, o in s['threads'].items():
            if oid != th_id and o['status'] == 'open' and o['latest_offer'] and moved & (set(o['latest_offer']['give']) | set(o['latest_offer']['get'])):
                o['status'] = 'expired'
    elif action == 'REJECT':
        th['status'] = 'rejected'
    elif action == 'MESSAGE':
        pass
    else:
        raise Violation('BAD_ACTION', action)

def fold(rules: Rules, world: World, events: list[dict], until_seq: int | None=None) -> dict:
    state: dict | None = None
    for ev in events:
        if until_seq is not None and ev.get('seq', 0) > until_seq:
            break
        if ev['type'] == 'genesis':
            g = ev
            state = genesis_state(rules, g['league_id'], g['team_ids'], g['draft_order'], g['week'])
            continue
        assert state is not None, 'log must start with genesis'
        state = apply_event(rules, world, state, ev)
    assert state is not None
    return state
