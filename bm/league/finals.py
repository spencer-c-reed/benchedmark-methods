from __future__ import annotations
import random

def draft_order(team_ids: list[str], seed: str, explicit: list[str] | None=None) -> list[str]:
    if explicit is not None:
        if not isinstance(explicit, list) or any((not isinstance(t, str) for t in explicit)) or len(explicit) != len(team_ids) or (set(explicit) != set(team_ids)):
            raise ValueError('draft_order must list each league team ID exactly once')
        return list(explicit)
    order = list(team_ids)
    random.Random(seed).shuffle(order)
    return order

def latin_draft_orders(league_ids: list[str], seats: list[str], seed: str) -> dict[str, list[str]]:
    if not isinstance(league_ids, list) or not league_ids or any((not isinstance(lid, str) or not lid for lid in league_ids)) or (len(set(league_ids)) != len(league_ids)):
        raise ValueError('league_ids must be a nonempty list of unique IDs')
    if not isinstance(seats, list) or not seats or any((not isinstance(seat, str) or not seat for seat in seats)):
        raise ValueError('seats must be a nonempty ordered list of entrant IDs')
    n = len(seats)
    base = list(range(n))
    random.Random(seed).shuffle(base)
    count = len(league_ids)
    out = {}
    for j, lid in enumerate(league_ids):
        offset = j * n // count if count < n else j % n
        row = base[offset:] + base[:offset]
        out[lid] = [f'{lid}-t{s + 1:02d}' for s in row]
    return out
