from __future__ import annotations
import hashlib
import math
from bm.engine.h2h import h2h_results, swiss_pairings
from bm.engine.rules import Rules
from bm.engine.scoring import standings as points_standings
from bm.log import canonical

def finals_view(config: dict, rules: Rules, events: list[dict]) -> dict | None:
    if rules.raw.get('season', {}).get('standings') != 'swiss':
        return None
    if config.get('format') != 'finals':
        raise ValueError('Swiss finals rules require format: finals')
    first, last = (int(config['start_week']), int(config['end_week']))
    if last - first != 2:
        raise ValueError('Swiss finals require exactly three consecutive weeks')
    weeks = list(range(first, last + 1))
    teams = [s['team_id'] for s in config['seats']]
    if any((not isinstance(t, str) or not t for t in teams)):
        raise ValueError('Swiss finals require nonempty team IDs')
    if len(teams) < 2 or len(teams) % 2 or len(set(teams)) != len(teams):
        raise ValueError('Swiss finals require unique teams and an even field')
    seed = config['seed']
    rounds = []
    scores, score_seqs, score_statuses, incomplete_games = ({}, {}, {}, {})
    for event in events:
        if event.get('type') == 'swiss_round_scheduled':
            number = event.get('round')
            if isinstance(number, bool) or not isinstance(number, int) or number != len(rounds) + 1 or (number not in (1, 2, 3)) or (event.get('week') != weeks[number - 1]):
                raise ValueError('retained Swiss rounds must be unique, consecutive and in the finals window')
            pairs = event.get('pairings')
            if not isinstance(pairs, list) or len(pairs) != len(teams) // 2 or any((not isinstance(p, (list, tuple)) or len(p) != 2 for p in pairs)):
                raise ValueError('every Swiss round must pair each team exactly once')
            flat = [t for pair in pairs for t in pair]
            if len(flat) != len(teams) or any((not isinstance(t, str) for t in flat)) or set(flat) != set(teams):
                raise ValueError('every Swiss round must pair each team exactly once')
            rounds.append({'round': number, 'week': event['week'], 'pairings': [list(p) for p in pairs], 'event_seq': event.get('seq'), 'source_score_seqs': event.get('source_score_seqs'), 'source_scores_sha256': event.get('source_scores_sha256')})
        elif event.get('type') == 'week_scored' and event.get('week') in weeks:
            week = event['week']
            points = event.get('points')
            if not isinstance(points, dict) or set(points) - set(teams):
                raise ValueError("Swiss scores must identify only this league's teams")
            if any((isinstance(v, bool) or not isinstance(v, (int, float)) or (not math.isfinite(v)) for v in points.values())):
                raise ValueError('Swiss scores must be finite numbers')
            scores[week], score_seqs[week] = (dict(points), event.get('seq'))
            score_statuses[week] = event.get('status', 'unknown')
            incomplete_games[week] = event.get('incomplete_games') or []
    complete_scores = {w: p for w, p in scores.items() if set(p) == set(teams) and (not incomplete_games[w])}
    schedule = {r['week']: [tuple(p) for p in r['pairings']] for r in rounds}
    used_scores = {w: complete_scores[w] for w in schedule if w in complete_scores}
    results = h2h_results(schedule, used_scores)['stats'] if schedule else {}
    stats = {t: results.get(t, {'w': 0, 'l': 0, 't': 0, 'pf': 0.0, 'pa': 0.0}) for t in teams}
    draw = lambda t: hashlib.sha256(f'{seed}:{t}'.encode()).hexdigest()
    ordered = sorted(teams, key=lambda t: (-(stats[t]['w'] + 0.5 * stats[t]['t']), -stats[t]['pf'], draw(t)))
    all_play = {r['team_id']: r for r in points_standings(teams, used_scores, seed)}
    table = [{'team_id': t, 'rank': i + 1, 'w': stats[t]['w'], 'l': stats[t]['l'], 't': stats[t]['t'], 'record_points': stats[t]['w'] + 0.5 * stats[t]['t'], 'points': stats[t]['pf'], 'all_play_w': all_play[t]['all_play_w'], 'all_play_l': all_play[t]['all_play_l']} for i, t in enumerate(ordered)]
    proposed = None
    next_index = len(rounds)
    if next_index < 3 and all((w in complete_scores for w in weeks[:next_index])):
        prior = {frozenset(p) for r in rounds for p in r['pairings']}
        records = {t: {'wins': stats[t]['w'] + 0.5 * stats[t]['t'], 'points': stats[t]['pf']} for t in teams}
        pairs = swiss_pairings(teams, records if next_index else {}, next_index + 1, seed, prior)
        inputs = {str(w): complete_scores[w] for w in weeks[:next_index]}
        proposed = {'type': 'swiss_round_scheduled', 'round': next_index + 1, 'week': weeks[next_index], 'pairings': [list(p) for p in pairs], 'source_score_seqs': {str(w): score_seqs[w] for w in weeks[:next_index]}, 'source_scores_sha256': hashlib.sha256(canonical(inputs)).hexdigest()}
    complete = len(rounds) == 3 and all((w in complete_scores for w in weeks))
    return {'rounds': rounds, 'proposed_round': proposed, 'standings': table, 'weekly_scores': {str(w): p for w, p in used_scores.items()}, 'scored_rounds': len(used_scores), 'complete': complete, 'champion': table[0]['team_id'] if complete else None, 'tie_policy': 'half_win', 'score_statuses': {str(w): score_statuses[w] for w in used_scores}, 'final': complete and all((score_statuses[w] == 'final' for w in weeks))}
