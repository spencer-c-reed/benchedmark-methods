from __future__ import annotations
from datetime import datetime
from typing import Mapping
from .rules import Rules
from .state import World, can_fill_starters, owner_of

def legal_draft_choices(rules: Rules, world: World, state: dict, tid: str, candidates: list[str]) -> list[str]:
    roster = state['teams'][tid]['roster']
    owned = {p for t in state['teams'].values() for p in t['roster']}
    out = []
    for pid in candidates:
        if pid in owned or pid not in world.players:
            continue
        after = roster + [pid]
        if not can_fill_starters(rules, world, after, rules.roster_size - len(after)):
            continue
        pos = world.pos(pid)
        cap = rules.max_per_position.get(pos)
        if cap is not None and sum((1 for p in after if world.pos(p) == pos)) > cap:
            continue
        out.append(pid)
    return out

def default_draft_pick(rules: Rules, world: World, state: dict, tid: str, value: Mapping[str, float]) -> str:
    ranked = sorted(world.players, key=lambda p: (-value.get(p, 0.0), p))
    legal = legal_draft_choices(rules, world, state, tid, ranked)
    roster = state['teams'][tid]['roster']
    picks_left_after = rules.roster_size - len(roster) - 1
    have = {world.pos(p) for p in roster}
    for pid in legal:
        pos = world.pos(pid)
        if pos in ('K', 'DEF') and (picks_left_after > 2 or pos in have):
            continue
        if pos in ('QB', 'TE') and sum((1 for p in roster if world.pos(p) == pos)) >= 2:
            continue
        return pid
    return legal[0]

def best_lineup(rules: Rules, world: World, state: dict, tid: str, proj: Mapping[str, float], now: datetime) -> dict:
    team = state['teams'][tid]
    week = state['week']
    lineup: dict[str, str | None] = {}
    used = set()
    for slot, pid in team['lineup'].items():
        if pid is not None and world.locked(week, pid, now):
            lineup[slot] = pid
            used.add(pid)
    free = [p for p in team['roster'] if p not in used and (not world.locked(week, p, now))]
    free.sort(key=lambda p: (-proj.get(p, 0.0), p))
    for slot in rules.starter_slots:
        if slot in lineup or rules.slot_type(slot) == 'FLEX':
            continue
        for p in free:
            if p not in used and rules.eligible(slot, world.pos(p)):
                lineup[slot] = p
                used.add(p)
                break
        else:
            cur = team['lineup'].get(slot)
            lineup[slot] = cur if cur and world.locked(week, cur, now) else None
    for slot in rules.starter_slots:
        if slot in lineup:
            continue
        for p in free:
            if p not in used and rules.eligible(slot, world.pos(p)):
                lineup[slot] = p
                used.add(p)
                break
        else:
            lineup[slot] = None
    return {s: lineup.get(s) for s in rules.starter_slots}
