from __future__ import annotations
import hashlib
from typing import Mapping

def round_robin(team_ids: list[str], weeks: list[int], seed: str) -> dict[int, list[tuple[str, str]]]:
    n = len(team_ids)
    if n % 2 == 1:
        team_ids = team_ids + ['BYE']
        n += 1
    perm = sorted(team_ids, key=lambda t: hashlib.sha256(f'{seed}:{t}'.encode()).hexdigest())
    schedule_cycle = {}
    for rd in range(n - 1):
        games = []
        for i in range(n // 2):
            h = perm[i]
            a = perm[n - 1 - i]
            if rd % 2 == 1:
                h, a = (a, h)
            if h != 'BYE' and a != 'BYE':
                games.append((h, a))
        if games:
            schedule_cycle[rd] = games
        perm = perm[:1] + perm[-1:] + perm[1:-1]
    out = {}
    for wk in weeks:
        rd = (wk - min(weeks)) % (n - 1)
        out[wk] = schedule_cycle[rd]
    return out

def h2h_results(schedule: dict[int, list[tuple[str, str]]], scores: dict[str | int, dict[str, float]]) -> dict:
    scores_int = {}
    for wk, teams in scores.items():
        scores_int[int(wk)] = teams
    all_teams = set()
    for games in schedule.values():
        for h, a in games:
            all_teams.add(h)
            all_teams.add(a)
    stats = {t: {'w': 0, 'l': 0, 't': 0, 'pf': 0.0, 'pa': 0.0} for t in all_teams}
    week_results = []
    for wk in sorted(schedule.keys()):
        if wk not in scores_int:
            continue
        wk_scores = scores_int[wk]
        for home, away in schedule[wk]:
            h_pts = wk_scores.get(home, 0.0)
            a_pts = wk_scores.get(away, 0.0)
            stats[home]['pf'] += h_pts
            stats[home]['pa'] += a_pts
            stats[away]['pf'] += a_pts
            stats[away]['pa'] += h_pts
            if h_pts > a_pts:
                stats[home]['w'] += 1
                stats[away]['l'] += 1
                winner = home
            elif a_pts > h_pts:
                stats[away]['w'] += 1
                stats[home]['l'] += 1
                winner = away
            else:
                stats[home]['t'] += 1
                stats[away]['t'] += 1
                winner = None
            week_results.append({'week': wk, 'home': home, 'away': away, 'h_pts': h_pts, 'a_pts': a_pts, 'winner': winner})
    for t in stats:
        stats[t]['pf'] = round(stats[t]['pf'], 2)
        stats[t]['pa'] = round(stats[t]['pa'], 2)
    return {'stats': stats, 'week_results': week_results}

def h2h_standings(team_ids: list[str], schedule: dict[int, list[tuple[str, str]]], scores: dict[str | int, dict[str, float]], seed: str) -> list[dict]:
    results = h2h_results(schedule, scores)
    stats = results['stats']
    apw = {t: 0 for t in team_ids}
    apl = {t: 0 for t in team_ids}
    scores_int = {}
    for wk, teams in scores.items():
        scores_int[int(wk)] = teams
    for wk in sorted(scores_int.keys()):
        if wk not in schedule:
            continue
        sc = scores_int[wk]
        for t in team_ids:
            if t not in sc:
                continue
            for o in team_ids:
                if o == t or o not in sc:
                    continue
                if sc[t] > sc[o]:
                    apw[t] += 1
                elif sc[t] < sc[o]:
                    apl[t] += 1
    draw = lambda t: hashlib.sha256(f'{seed}:{t}'.encode()).hexdigest()
    ranked = sorted(team_ids, key=lambda t: (-(stats[t]['w'] + 0.5 * stats[t]['t']), -stats[t]['pf'], -apw[t], draw(t)))
    return [{'team_id': t, 'rank': i + 1, 'w': stats[t]['w'], 'l': stats[t]['l'], 't': stats[t]['t'], 'pf': stats[t]['pf'], 'pa': stats[t]['pa'], 'all_play_w': apw[t], 'all_play_l': apl[t]} for i, t in enumerate(ranked)]

def _winner(g: dict, scores: dict[int, dict[str, float]]) -> str | None:
    sc = scores.get(g['week'])
    if sc is None or not g['home'] or (not g['away']) or (g['home'] not in sc) or (g['away'] not in sc):
        return None
    h, a = (sc[g['home']], sc[g['away']])
    if h != a:
        return g['home'] if h > a else g['away']
    return g['home'] if g['home_seed'] < g['away_seed'] else g['away']

def _game(week, rnd, hs, as_, seeds):
    return {'week': week, 'round': rnd, 'home_seed': hs, 'away_seed': as_, 'home': seeds[hs - 1] if hs else None, 'away': seeds[as_ - 1] if as_ else None, 'winner': None}

def playoff_bracket(seeds: list[str], n_playoff: int=6, weeks: tuple[int, int, int]=(15, 16, 17)) -> dict:
    assert n_playoff == 6, 'the bracket is defined for 6 playoff teams'
    po = [_game(weeks[0], 'QF', 3, 6, seeds), _game(weeks[0], 'QF', 4, 5, seeds)]
    cons = list(range(n_playoff + 1, len(seeds) + 1))
    r1 = [_game(weeks[0], 'C1', cons[i], cons[-1 - i], seeds) for i in range(len(cons) // 2)]
    return {'seeds': list(seeds), 'weeks': list(weeks), 'n_playoff': n_playoff, 'playoff': po, 'consolation': r1}

def advance_bracket(bracket: dict, scores: dict) -> dict:
    sc = {int(k): v for k, v in scores.items()}
    seeds, weeks = (bracket['seeds'], bracket['weeks'])
    seed_of = {t: i + 1 for i, t in enumerate(seeds)}
    po = bracket['playoff']
    for g in po + bracket['consolation']:
        if g['winner'] is None:
            g['winner'] = _winner(g, sc)
    rounds = {r: [g for g in po if g['round'] == r] for r in ('QF', 'SF', 'P5', 'F', 'P3')}
    if all((g['winner'] for g in rounds['QF'])) and (not rounds['SF']):
        qw = sorted((seed_of[g['winner']] for g in rounds['QF']))
        ql = sorted((seed_of[g['home'] if g['winner'] == g['away'] else g['away']] for g in rounds['QF']))
        po += [_game(weeks[1], 'SF', 1, qw[1], seeds), _game(weeks[1], 'SF', 2, qw[0], seeds), _game(weeks[1], 'P5', ql[0], ql[1], seeds)]
        return advance_bracket(bracket, scores)
    if rounds['SF'] and all((g['winner'] for g in rounds['SF'])) and (not rounds['F']):
        sw = sorted((seed_of[g['winner']] for g in rounds['SF']))
        sl = sorted((seed_of[g['home'] if g['winner'] == g['away'] else g['away']] for g in rounds['SF']))
        po += [_game(weeks[2], 'F', sw[0], sw[1], seeds), _game(weeks[2], 'P3', sl[0], sl[1], seeds)]
        return advance_bracket(bracket, scores)
    cons_teams = seeds[bracket['n_playoff']:]
    played = {g['round'] for g in bracket['consolation']}
    for k in (2, 3):
        prev = [g for g in bracket['consolation'] if g['round'] == f'C{k - 1}']
        if f'C{k}' in played or not prev or (not all((g['winner'] for g in prev))):
            continue
        res = _cons_record(bracket, sc)
        prior = {frozenset((g['home'], g['away'])) for g in bracket['consolation']}
        pairs = swiss_pairings(cons_teams, res, k, 'consolation:' + ','.join(seeds), prior)
        for a, b in pairs:
            h, aw = sorted((a, b), key=lambda t: seed_of[t])
            bracket['consolation'].append(_game(weeks[k - 1], f'C{k}', seed_of[h], seed_of[aw], seeds))
        return advance_bracket(bracket, scores)
    return bracket

def _cons_record(bracket: dict, sc: dict[int, dict[str, float]]) -> dict[str, dict]:
    res = {t: {'wins': 0, 'points': 0.0} for t in bracket['seeds'][bracket['n_playoff']:]}
    for g in bracket['consolation']:
        if g['winner']:
            res[g['winner']]['wins'] += 1
            for t in (g['home'], g['away']):
                res[t]['points'] += sc[g['week']][t]
    return res

def final_placements(bracket: dict, scores: dict | None=None) -> list[str]:
    seeds = bracket['seeds']
    by = {g['round']: g for g in bracket['playoff'] if g['round'] in ('F', 'P3', 'P5')}
    out = []
    for r in ('F', 'P3', 'P5'):
        g = by.get(r)
        if not g or not g['winner']:
            return out
        out += [g['winner'], g['home'] if g['winner'] == g['away'] else g['away']]
    if scores is not None and len([g for g in bracket['consolation'] if g['winner']]) == 3 * ((len(seeds) - bracket['n_playoff']) // 2):
        res = _cons_record(bracket, {int(k): v for k, v in scores.items()})
        out += sorted(res, key=lambda t: (-res[t]['wins'], -res[t]['points'], seeds.index(t)))
    return out

def swiss_pairings(team_ids: list[str], results_so_far: dict[str, dict], round_no: int, seed: str, prior_pairs: set[frozenset] | None=None) -> list[tuple[str, str]]:
    prior = set(prior_pairs or ())
    draw = lambda t: hashlib.sha256(f'{seed}:{round_no}:{t}'.encode()).hexdigest()
    if round_no == 1 or not results_so_far:
        order = sorted(team_ids, key=draw)
        h = len(order) // 2
        return [(order[i], order[h + i]) for i in range(h)]
    rec = lambda t: results_so_far.get(t, {})
    order = sorted(team_ids, key=lambda t: (-rec(t).get('wins', 0), -rec(t).get('points', 0.0), draw(t)))
    if len(order) % 2:
        order = order[:-1]

    def search(rest: list[str], allow_rematch: bool, budget: list[int]):
        if not rest:
            return []
        t, others = (rest[0], rest[1:])
        cands = sorted(others, key=lambda o: (abs(rec(o).get('wins', 0) - rec(t).get('wins', 0)), order.index(o)))
        for o in cands:
            if frozenset((t, o)) in prior and (not allow_rematch):
                continue
            budget[0] -= 1
            if budget[0] < 0:
                return None
            sub = search([x for x in others if x != o], allow_rematch, budget)
            if sub is not None:
                return [(t, o)] + sub
        return None
    for allow in (False, True):
        got = search(order, allow, [200000])
        if got is not None:
            return got
    return [(order[i], order[i + 1]) for i in range(0, len(order), 2)]

def swiss_standings(team_ids: list[str], rounds: list[dict], scores: dict[str | int, dict[str, float]], seed: str) -> list[dict]:
    wins = {t: 0 for t in team_ids}
    pf = {t: 0.0 for t in team_ids}
    sc = {int(k): v for k, v in scores.items()}
    for rd in rounds:
        wk = sc.get(rd.get('week'))
        if wk is None:
            continue
        for g in rd.get('games', []):
            h, a = (g['home'], g['away'])
            pf[h] += wk.get(h, 0.0)
            pf[a] += wk.get(a, 0.0)
            if wk.get(h, 0.0) > wk.get(a, 0.0):
                wins[h] += 1
            elif wk.get(a, 0.0) > wk.get(h, 0.0):
                wins[a] += 1
    draw = lambda t: hashlib.sha256(f'{seed}:{t}'.encode()).hexdigest()
    ranked = sorted(team_ids, key=lambda t: (-wins[t], -pf[t], draw(t)))
    return [{'team_id': t, 'rank': i + 1, 'wins': wins[t], 'points': round(pf[t], 2)} for i, t in enumerate(ranked)]
