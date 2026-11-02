from __future__ import annotations
from datetime import datetime
from typing import Mapping
from bm.engine.rules import Rules
from bm.engine.scoring import score_player_line
from bm.engine.state import World

def optimal_lineup(rules: Rules, world: World, roster: list[str], week: int, player_points: Mapping[str, float], lock_time: datetime | None=None) -> dict[str, str | None]:
    if lock_time is None:
        return _optimal_without_locks(rules, world, roster, player_points)
    else:
        return _optimal_with_locks(rules, world, roster, week, player_points, lock_time)

def _get_player_pos(world: World, pid: str) -> str:
    try:
        return world.pos(pid)
    except:
        return None

def _optimal_without_locks(rules: Rules, world: World, roster: list[str], player_points: Mapping[str, float]) -> dict[str, str | None]:
    lineup: dict[str, str | None] = {s: None for s in rules.starter_slots}
    used: set[str] = set()
    ranked = sorted(roster, key=lambda p: player_points.get(p, 0.0), reverse=True)
    for slot in rules.starter_slots:
        if rules.slot_type(slot) == 'FLEX':
            continue
        for player in ranked:
            if player not in used:
                pos = _get_player_pos(world, player)
                if pos and rules.eligible(slot, pos):
                    lineup[slot] = player
                    used.add(player)
                    break
    flex_slots = [s for s in rules.starter_slots if rules.slot_type(s) == 'FLEX']
    for slot in flex_slots:
        for player in ranked:
            if player not in used:
                pos = _get_player_pos(world, player)
                if pos and rules.eligible(slot, pos):
                    lineup[slot] = player
                    used.add(player)
                    break
    return lineup

def _optimal_with_locks(rules: Rules, world: World, roster: list[str], week: int, player_points: Mapping[str, float], lock_time: datetime) -> dict[str, str | None]:
    locked: dict[str, str | None] = {}
    unlocked: set[str] = set()

    def is_locked(pid: str) -> bool:
        ko = world.kickoff(week, pid)
        return ko is not None and ko <= lock_time
    locked_players = [p for p in roster if is_locked(p)]
    unlocked_players = [p for p in roster if not is_locked(p)]
    lineup: dict[str, str | None] = {s: None for s in rules.starter_slots}
    used: set[str] = set()
    ranked = sorted(roster, key=lambda p: player_points.get(p, 0.0), reverse=True)
    for slot in rules.starter_slots:
        if rules.slot_type(slot) == 'FLEX':
            continue
        for player in ranked:
            if player not in used:
                pos = _get_player_pos(world, player)
                if pos and rules.eligible(slot, pos):
                    lineup[slot] = player
                    used.add(player)
                    break
    flex_slots = [s for s in rules.starter_slots if rules.slot_type(s) == 'FLEX']
    for slot in flex_slots:
        for player in ranked:
            if player not in used:
                pos = _get_player_pos(world, player)
                if pos and rules.eligible(slot, pos):
                    lineup[slot] = player
                    used.add(player)
                    break
    return lineup

def compute_ceiling(rules: Rules, world: World, tid: str, week: int, state: dict, player_points: Mapping[str, float]) -> dict:
    team = state['teams'][tid]
    roster = team['roster']
    actual_lineup = team['lineup']

    def lineup_points(lineup: dict[str, str | None]) -> float:
        total = 0.0
        for slot in rules.starter_slots:
            pid = lineup.get(slot)
            if pid:
                total += player_points.get(pid, 0.0)
        return total
    actual_points = lineup_points(actual_lineup)
    optimal_lu = _optimal_without_locks(rules, world, roster, player_points)
    optimal_points = lineup_points(optimal_lu)
    share = actual_points / optimal_points * 100.0 if optimal_points > 0 else 100.0
    mistakes = []
    for slot in rules.starter_slots:
        actual_player = actual_lineup.get(slot)
        optimal_player = optimal_lu.get(slot)
        if actual_player != optimal_player:
            actual_pts = player_points.get(actual_player, 0.0) if actual_player else 0.0
            optimal_pts = player_points.get(optimal_player, 0.0) if optimal_player else 0.0
            delta = optimal_pts - actual_pts
            if delta > 0:
                mistakes.append({'slot': slot, 'benched': actual_player, 'benched_points': actual_pts, 'for': optimal_player, 'for_points': optimal_pts, 'delta': round(delta, 2)})
    mistakes.sort(key=lambda m: m['delta'], reverse=True)
    return {'team_id': tid, 'week': week, 'actual_points': round(actual_points, 2), 'optimal_points': round(optimal_points, 2), 'share_captured': round(share, 1), 'actual_lineup': dict(actual_lineup), 'optimal_lineup': optimal_lu, 'mistakes': mistakes}
