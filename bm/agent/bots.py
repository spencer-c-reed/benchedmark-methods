from __future__ import annotations
from datetime import datetime
import hashlib
import random
from bm.engine.defaults import best_lineup, default_draft_pick, legal_draft_choices

def _rng(*parts) -> random.Random:
    return random.Random(int(hashlib.sha256('|'.join(map(str, parts)).encode()).hexdigest()[:16], 16))

def draft(kind: str, rules, view, state, tid, seed) -> str:
    if kind in AUTOPILOTS:
        return autopilot_draft(kind, rules, view, state, tid, seed)
    if kind == 'projection-bot':
        vor = {p: v['vor'] if view.world.players.get(p, {}).get('pos') not in ('K', 'DEF') or not _bye(view, p) else -1000000.0 for p, v in view.proj.items()}
        return default_draft_pick(rules, view.world, state, tid, vor)
    if kind == 'adp-bot':
        return default_draft_pick(rules, view.world, state, tid, {p: v['ytd_vor'] + 0.01 * v['vor'] for p, v in view.proj.items()})
    top = sorted(view.proj, key=lambda p: -view.proj[p]['vor'])[:150]
    legal = legal_draft_choices(rules, view.world, state, tid, top)
    roster = state['teams'][tid]['roster']
    left = rules.roster_size - len(roster) - 1
    legal2 = [p for p in legal if view.world.pos(p) not in ('K', 'DEF') or left <= 2]
    pool = legal2 or legal or legal_draft_choices(rules, view.world, state, tid, list(view.proj))
    return _rng(seed, tid, state['draft']['pick']).choice(pool)

def lineup(kind: str, rules, view, state, tid, seed) -> dict:
    if kind == 'expert-bot':
        return expert_lineup(rules, view, state, tid)[0]
    if kind in AUTOPILOTS:
        return autopilot_lineup(kind, rules, view, state, tid, seed)
    if kind == 'adp-bot':
        score = {p: (v['ytd'] / v['games'] if v['games'] else 0) * (1 if v['week_proj'] > 0 else 0) for p, v in view.proj.items()}
        return best_lineup(rules, view.world, state, tid, score, view.as_of)
    if kind == 'random-bot':
        r = _rng(seed, tid, state['week'])
        score = {p: r.random() if v['week_proj'] > 0 else -1 for p, v in view.proj.items()}
        return best_lineup(rules, view.world, state, tid, score, view.as_of)
    return policy_lineup(rules, view, state, tid)

def expert_lineup(rules, view, state, tid, board=None) -> tuple[dict, dict]:
    match_report = None
    if board is None:
        from bm.stats import expert_rankings
        board, match_report = expert_rankings.board(state['week'], view.world, view.as_of)
    roster = state['teams'][tid]['roster']
    scores = policy_score(view, roster)
    for p in roster:
        if _bye(view, p) or _out(view, p):
            scores[p] = -1000000.0
        elif p in board:
            scores[p] = 1000000.0 - board[p]['choice'][0] * 10000 - board[p]['rank']
        else:
            scores[p] = min(1000, scores[p])
    lu = best_lineup(rules, view.world, state, tid, scores, view.as_of)
    locked = {s: p for s, p in lu.items() if p and view.world.locked(state['week'], p, view.as_of)}
    ranked = {s: {'pid': p, **{k: board[p][k] for k in ('rank', 'source', 'ranker', 'scoring')}} for s, p in lu.items() if p in board and s not in locked}
    fallback = {s: p for s, p in lu.items() if s not in locked and (p is None or p not in board)}
    return (lu, {'available': bool(ranked), 'ranked_slots': ranked, 'locked_slots': locked, 'projection_fallback_slots': fallback, 'unranked_roster': [p for p in roster if p not in board], 'match_report': match_report, 'coverage_rule': 'expert ranks with explicit projection fallback; locked slots unchanged'})
NO_PLAY_STATUS = frozenset({'Out', 'IR', 'FA', 'PS', 'RET'})

def policy_score(view, roster) -> dict[str, float]:
    return _week_score(view, roster)

def policy_lineup(rules, view, state, tid, now=None, score=None) -> dict:
    return best_lineup(rules, view.world, state, tid, policy_score(view, state['teams'][tid]['roster']) if score is None else score, view.as_of if now is None else now)

def lineup_value(rules, view, roster) -> float:
    from datetime import timedelta
    st = {'week': view.week, 'phase': 'SEASON', 'teams': {'x': {'roster': sorted(set(roster)), 'lineup': {s: None for s in rules.starter_slots}, 'faab': 0}}}
    val = {p: float(view.proj.get(p, {}).get('value', 0.0)) for p in st['teams']['x']['roster']}
    lu = policy_lineup(rules, view, st, 'x', now=view.as_of - timedelta(days=7), score=val)
    return round(sum((val.get(p, 0.0) for p in lu.values() if p)), 2)

def _bye(view, pid) -> bool:
    return bool(getattr(view, 'opp', None)) and (not _has_game(view, pid))

def _out(view, pid) -> bool:
    return (view.proj.get(pid, {}).get('status') or '') in NO_PLAY_STATUS

def _week_score(view, roster) -> dict[str, float]:
    return {p: -1.0 if _bye(view, p) or _out(view, p) else view.proj.get(p, {}).get('week_proj', 0.0) for p in roster}

def waiver_bids(kind: str, rules, view, state, tid) -> list[dict]:
    if kind != 'projection-bot':
        return []
    team = state['teams'][tid]
    week, world, now = (state['week'], view.world, view.as_of)
    owned = {p for t in state['teams'].values() for p in t['roster']}
    val = lambda p: view.proj.get(p, {}).get('value', 0)
    wk = lambda p: 0.0 if _bye(view, p) else view.proj.get(p, {}).get('week_proj', 0.0)
    budget = team['faab']
    roster = list(team['roster'])
    taken: set = set()
    cover, upgrades = ([], [])

    def free_agents(pos, key):
        return sorted([p for p, x in world.players.items() if x['pos'] == pos and p not in owned and (p not in taken) and (not world.locked(week, p, now))], key=lambda p: (-key(p), p))

    def droppable(keep):
        return sorted([p for p in roster if p not in keep and (not world.locked(week, p, now))], key=lambda p: (val(p), p))

    def cap_ok(add, drop):
        cap = rules.max_per_position.get(world.pos(add))
        n = sum((1 for p in roster if p != drop and world.pos(p) == world.pos(add)))
        return cap is None or n + 1 <= cap
    for _ in range(len(rules.starter_slots)):
        lu = best_lineup(rules, world, {**state, 'teams': {**state['teams'], tid: {**team, 'roster': roster}}}, tid, _week_score(view, roster), now)
        holes = [s for s in rules.starter_slots if (lu.get(s) is None or _bye(view, lu[s])) and (not (lu.get(s) and world.locked(week, lu[s], now)))]
        if not holes:
            break
        slot = holes[0]
        pos_ok = rules.eligibility.get(rules.slot_type(slot), [])
        cands = [p for pos in pos_ok for p in free_agents(pos, wk) if wk(p) > 0]
        cands.sort(key=lambda p: (-wk(p), p))
        keep = {p for s, p in lu.items() if p and (not _bye(view, p))}
        add = drop = None
        for c in cands:
            if len(roster) < rules.roster_size and cap_ok(c, None):
                add, drop = (c, None)
                break
            d = next((p for p in droppable(keep) if cap_ok(c, p)), None)
            if d is not None:
                add, drop = (c, d)
                break
        if add is None:
            break
        cover.append({'add': add, 'drop': drop, 'amount': min(budget, max(int(rules.faab_min_bid), int(round(wk(add) / 2))))})
        taken.add(add)
        if drop:
            roster.remove(drop)
        roster.append(add)
    for pos in ('RB', 'WR', 'TE', 'QB', 'K', 'DEF'):
        mine = sorted([p for p in roster if world.pos(p) == pos and p not in taken], key=lambda p: (val(p), p))
        if not mine:
            continue
        worst = mine[0]
        fa = free_agents(pos, val)
        if not fa or world.locked(week, worst, now):
            continue
        gain = val(fa[0]) - val(worst)
        if gain >= 1.5:
            upgrades.append({'add': fa[0], 'drop': worst, 'amount': min(budget, int(round(gain * 3)))})
            taken.add(fa[0])
            roster.remove(worst)
            roster.append(fa[0])
    return cover + sorted(upgrades, key=lambda b: -b['amount'])[:3]
TRADE_LINEUP_FROM_WEEK = 7
TRADE_MIN_GAIN = 1.0
TRADE_MARGIN = 0.03

def trade_response(kind: str, view, give_to_me: list[str], take_from_me: list[str], rules=None, roster=None) -> str:
    if kind != 'projection-bot':
        return 'REJECT'
    if rules is not None and roster is not None and (getattr(view, 'week', 0) >= TRADE_LINEUP_FROM_WEEK):
        if not take_from_me and (not give_to_me):
            return 'REJECT'
        before = lineup_value(rules, view, roster)
        after = lineup_value(rules, view, [p for p in roster if p not in set(take_from_me)] + list(give_to_me))
        return 'ACCEPT' if after >= before + max(TRADE_MIN_GAIN, TRADE_MARGIN * before) else 'REJECT'
    get = sum((view.proj.get(p, {}).get('value', 0) for p in give_to_me))
    give = sum((view.proj.get(p, {}).get('value', 0) for p in take_from_me))
    return 'ACCEPT' if give > 0 and get >= 1.05 * give else 'REJECT'
AUTOPILOT_DOC = 'What a casual player gets by pressing the big platforms\' buttons and walking away. Mechanics follow each\nplatform\'s own help pages (quoted in plan/REAL-TOURNAMENTS.md §A.1); undocumented details are marked ASSUMPTION.\n\nespn-auto-bot     Draft: ESPN Autopick, "Best Available" default with no custom list: "the highest-ranked available\n                  player" that "fit[s] an open roster slot and not exceed roster position limits" (so K and D/ST come\n                  only when they are the last open slots). Lineup: ESPN Quick Lineup at every lineup window: "placing\n                  available players into their eligible slots if they have a game scheduled", by projection\n                  (ASSUMPTION: ESPN says "rankings" without naming them); Out/IR sit.\nyahoo-auto-bot    Draft: Yahoo Autopick: "All starter positions are filled before bench positions", highest-ranked at a\n                  position of need, then a balanced bench (ASSUMPTION for the ratio: BENCH_CAP). Lineup: Yahoo "Start\n                  Active Players": swaps a starter out only if he has no game, or is injured while a healthy bench\n                  player has a game; never re-optimizes by projection.\nsleeper-auto-bot  Draft: Sleeper CPU autopick with an empty queue: "what positions my team needs ... one of the\n                  higher-ranked players available" (ASSUMPTION: "one of" = seeded pick among the top 3 at a need).\n                  Lineup: Sleeper has no lineup optimizer (AutoSubs are manual), so the post-draft lineup is never touched.\nconsensus-bot     Draft by a consensus of two rank sources, ESPN-style slot logic; lineup by projection, benching\n                  Out/IR/Doubtful. The diligent casual: a cheat sheet and the app\'s projections.\nAll four: no waivers, reject every trade, never propose.\n\nDATA. Open mode (default, platform.FIRST_PARTY False), from Week 7 (OPEN_FROM_WEEK; rules/policies.md baseline_bots,\n2026-10-13): espn-auto-bot ranks by our own nflverse projection ("own": rest-of-season value) and yahoo-auto-bot by\nSleeper\'s documented-API search_rank (non-commercial use); consensus = mean of those two; Sleeper rank = search_rank;\nprojections and injury status = the engine\'s own (nflverse). Neither is ESPN\'s or Yahoo\'s data, so their public names\nare "Best-Available Bot" and "Starters-First Bot" (config/entrants.yaml); the ids stay for seat assignment and history.\nWeeks 5-6 (logged) used FantasyPros consensus ranks via DynastyProcess ("fpecr"), which FantasyPros\' terms do not permit\nus to use; those weeks replay with it unchanged and no new decision uses it. First-party mode: ESPN\'s own default rank, projections and status; Sleeper\nADP; consensus = mean ESPN/Sleeper ADP and projections. Terms-restricted, so it waits for the operator (D-1).\nAny source with no snapshot at or before the decision (simulated seasons, backtests) falls back to the internal proxy:\nrank by last season\'s points per game. bots.provenance() records which data a decision used.\n'

def _specs(first_party: bool) -> dict:
    if first_party:
        return {'espn-auto-bot': {'rank': 'espn', 'draft': 'best_available', 'lineup': 'optimize', 'proj': ('espn',), 'status': ('espn',), 'sit': ('Out', 'IR')}, 'yahoo-auto-bot': {'rank': 'fpecr', 'draft': 'starters_first', 'lineup': 'start_active'}, 'sleeper-auto-bot': {'rank': 'sleeper', 'draft': 'needs_top3', 'lineup': 'untouched'}, 'consensus-bot': {'rank': (('espn', 'adp'), ('sleeper', 'adp')), 'draft': 'best_available', 'lineup': 'optimize', 'proj': ('espn', 'sleeper'), 'status': ('espn', 'sleeper'), 'sit': ('Out', 'IR', 'Doubtful')}}
    return {'espn-auto-bot': {'rank': 'fpecr', 'draft': 'best_available', 'lineup': 'optimize', 'proj': (), 'status': (), 'sit': ('Out', 'IR')}, 'yahoo-auto-bot': {'rank': 'fpecr', 'draft': 'starters_first', 'lineup': 'start_active'}, 'sleeper-auto-bot': {'rank': 'sleeper_api', 'draft': 'needs_top3', 'lineup': 'untouched'}, 'consensus-bot': {'rank': (('fpecr', 'rank'), ('sleeper_api', 'rank')), 'draft': 'best_available', 'lineup': 'optimize', 'proj': (), 'status': (), 'sit': ('Out', 'IR', 'Doubtful')}}
OPEN_FROM_WEEK = 7
OPEN_SPECS = {'espn-auto-bot': {'rank': 'own', 'draft': 'best_available', 'lineup': 'optimize', 'proj': (), 'status': (), 'sit': ('Out', 'IR')}, 'yahoo-auto-bot': {'rank': 'sleeper_api', 'draft': 'starters_first', 'lineup': 'start_active'}, 'sleeper-auto-bot': {'rank': 'sleeper_api', 'draft': 'needs_top3', 'lineup': 'untouched'}, 'consensus-bot': {'rank': (('own', 'rank'), ('sleeper_api', 'rank')), 'draft': 'best_available', 'lineup': 'optimize', 'proj': (), 'status': (), 'sit': ('Out', 'IR', 'Doubtful')}}
from bm.stats.platform import FIRST_PARTY
AUTOPILOTS = _specs(FIRST_PARTY)
FP_OFF_FROM_DEFAULT = '2026-10-14T15:00:00-04:00'

def fp_off_from() -> datetime:
    try:
        from bm.gateway.core import load_yaml
        v = (load_yaml('bots.yaml') or {}).get('fp_off_from')
        if v:
            return datetime.fromisoformat(str(v))
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return datetime.fromisoformat(FP_OFF_FROM_DEFAULT)

def spec_for(kind: str, view) -> dict | None:
    as_of = getattr(view, 'as_of', None)
    if not FIRST_PARTY and (getattr(view, 'week', 0) >= OPEN_FROM_WEEK or (as_of is not None and as_of >= fp_off_from())):
        return OPEN_SPECS.get(kind)
    return AUTOPILOTS.get(kind)
BENCH_CAP = {'QB': 2, 'RB': 5, 'WR': 5, 'TE': 2, 'K': 1, 'DEF': 1}
ALL = {'projection-bot', 'adp-bot', 'random-bot', *AUTOPILOTS}

def _own_board(view) -> dict:
    order = sorted(view.proj, key=lambda p: (-view.proj[p].get('value', 0.0), p))
    return {p: {'rank': i + 1, 'adp': None, 'started': None, 'status': view.proj[p].get('status') or '', 'proj': {}} for i, p in enumerate(order)}

def _board(source, view):
    if source == 'own':
        return (_own_board(view), {'retrieved_at': None, 'sha256': None, 'source': 'engine projection'})
    from bm.stats import platform
    try:
        return platform.board(source, view.as_of)
    except Exception:
        return (None, None)

def provenance(kind: str, view) -> dict:
    spec = spec_for(kind, view)
    if not spec:
        return {'kind': kind, 'data': 'engine'}
    rank = spec['rank'] if isinstance(spec['rank'], tuple) else ((spec['rank'], 'rank'),)
    srcs = tuple((s for s, _ in rank)) + tuple(spec.get('proj', ())) + tuple(spec.get('status', ()))
    out = {'first_party': FIRST_PARTY}
    for s in dict.fromkeys(srcs):
        b, m = _board(s, view)
        out[s] = 'engine projection' if s == 'own' else {'retrieved_at': m['retrieved_at'], 'sha256': m['sha256']} if m else 'proxy'
    return {'kind': kind, 'data': out}

def _proxy_order(view) -> list[str]:
    return sorted(view.proj, key=lambda p: (-view.proj[p].get('ros_ppg_prior', 0.0), -view.proj[p].get('value', 0.0), p))

def _rank_order(spec, view) -> list[str]:
    if isinstance(spec['rank'], tuple):
        vals: dict[str, list[float]] = {}
        for s, field in spec['rank']:
            b, _ = _board(s, view)
            for p, e in (b or {}).items():
                if e.get(field) and p in view.proj:
                    vals.setdefault(p, []).append(float(e[field]))
        key = {p: sum(v) / len(v) for p, v in vals.items()}
    else:
        b, _ = _board(spec['rank'], view)
        key = {p: float(e['rank']) for p, e in (b or {}).items() if e.get('rank') and p in view.proj}
    ranked = sorted(key, key=lambda p: (key[p], p))
    seen = set(ranked)
    return ranked + [p for p in _proxy_order(view) if p not in seen]

def _open_starters(rules, world, roster) -> dict[str, int]:
    left = list(roster)
    open_ = {}
    for slot in rules.starter_slots:
        t = rules.slot_type(slot)
        if t == 'FLEX':
            continue
        hit = next((p for p in left if rules.eligible(slot, world.pos(p))), None)
        if hit:
            left.remove(hit)
        else:
            open_[t] = open_.get(t, 0) + 1
    for slot in rules.starter_slots:
        if rules.slot_type(slot) != 'FLEX':
            continue
        hit = next((p for p in left if rules.eligible(slot, world.pos(p))), None)
        if hit:
            left.remove(hit)
        else:
            open_['FLEX'] = open_.get('FLEX', 0) + 1
    return open_

def _fills_need(rules, world, pos, open_) -> bool:
    return open_.get(pos, 0) > 0 or (open_.get('FLEX', 0) > 0 and pos in rules.eligibility.get('FLEX', []))

def _legal(rules, world, state, tid, pid) -> bool:
    return bool(legal_draft_choices(rules, world, state, tid, [pid]))

def autopilot_draft(kind, rules, view, state, tid, seed) -> str:
    spec = spec_for(kind, view)
    world = view.world
    roster = state['teams'][tid]['roster']
    order = [p for p in _rank_order(spec, view) if p in world.players]
    if spec['draft'] == 'best_available':
        for p in order:
            if _legal(rules, world, state, tid, p):
                return p
    else:
        open_ = _open_starters(rules, world, roster)
        need = any(open_.values())
        cands = []
        for p in order:
            pos = world.pos(p)
            if need and (not _fills_need(rules, world, pos, open_)):
                continue
            if not need and spec['draft'] == 'starters_first' and (sum((1 for q in roster if world.pos(q) == pos)) >= BENCH_CAP.get(pos, 99)):
                continue
            if _legal(rules, world, state, tid, p):
                cands.append(p)
                if spec['draft'] == 'starters_first' or len(cands) == 3:
                    break
        if cands:
            if spec['draft'] == 'needs_top3':
                return _rng(seed, tid, state['draft']['pick'], kind).choice(cands)
            return cands[0]
    return default_draft_pick(rules, world, state, tid, {p: v['vor'] for p, v in view.proj.items()})

def _status(view, pid, sources=()) -> str:
    for s in sources:
        b, _ = _board(s, view)
        if b and pid in b:
            return b[pid].get('status') or ''
    return view.proj.get(pid, {}).get('status') or ''

def _week_proj(spec, view) -> dict[str, float]:
    w = str(view.week)
    vals: dict[str, list[float]] = {}
    for s in spec.get('proj', ()):
        b, _ = _board(s, view)
        for p, e in (b or {}).items():
            if w in e.get('proj', {}):
                vals.setdefault(p, []).append(float(e['proj'][w]))
    return {p: sum(vals[p]) / len(vals[p]) if p in vals else v.get('week_proj', 0.0) for p, v in view.proj.items()}

def _has_game(view, pid) -> bool:
    p = view.world.players.get(pid)
    return bool(p) and p['team'] in view.opp

def autopilot_lineup(kind, rules, view, state, tid, seed) -> dict:
    spec = spec_for(kind, view)
    team = state['teams'][tid]
    if spec['lineup'] == 'untouched':
        return {s: team['lineup'].get(s) for s in rules.starter_slots}
    if spec['lineup'] == 'optimize':
        proj = _week_proj(spec, view)
        srcs = spec.get('status', ())
        score = {p: proj.get(p, 0.0) if _has_game(view, p) and _status(view, p, srcs) not in spec['sit'] else -1.0 for p in team['roster']}
        return best_lineup(rules, view.world, state, tid, score, view.as_of)
    world, week, now = (view.world, state['week'], view.as_of)
    rank = {p: i for i, p in enumerate(_rank_order(spec, view))}
    injured = lambda p: _status(view, p) not in ('', None)
    lineup = {s: team['lineup'].get(s) for s in rules.starter_slots}
    lineup = {s: p if p in team['roster'] else None for s, p in lineup.items()}
    for slot in rules.starter_slots:
        cur = lineup[slot]
        if cur is not None and world.locked(week, cur, now):
            continue
        no_game = cur is None or not _has_game(view, cur)
        hurt = cur is not None and (not no_game) and injured(cur)
        if not (no_game or hurt):
            continue
        used = set(lineup.values())
        bench = [p for p in team['roster'] if p not in used and (not world.locked(week, p, now)) and rules.eligible(slot, world.pos(p)) and _has_game(view, p) and (no_game or not injured(p))]
        if bench:
            lineup[slot] = min(bench, key=lambda p: (rank.get(p, 10 ** 6), p))
    return lineup
