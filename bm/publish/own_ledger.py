from __future__ import annotations
from collections import defaultdict
FIELDS = ('own', 'forced_default', 'missed', 'engine_lineup', 'excluded', 'misserved', 'pending', 'served_unverified')
SERVED_UNVERIFIED = {'gemini-3.1-pro': ('2026-10-07', '2026-10-09')}

def own_ledger(lg, evs: list[dict], kinds: dict[str, str]) -> list[dict]:
    ann = {e['decision_id']: e.get('annotation') for e in evs if e.get('type') == 'decision_annotation' and e.get('annotation') in ('excluded', 'misserved')}
    decs = {e['decision_id']: e for e in evs if e.get('type') == 'decision'}
    rows: dict[tuple, dict] = defaultdict(lambda: dict.fromkeys(FIELDS, 0))
    start = int(lg.cfg.get('start_week') or 0)

    def model(eid: str) -> bool:
        return kinds.get(eid) == 'llm'

    def add(team: str, eid: str, week, kind: str, field: str) -> None:
        if model(eid):
            rows[team, eid, int(week), kind][field] += 1
    for e in decs.values():
        if e.get('kind') == 'draft_pick':
            continue
        eid = e.get('entrant') or lg.entrant(e['team'], e.get('week'))
        a = ann.get(e['decision_id'])
        add(e['team'], eid, e.get('week') or start, e['kind'], a or ('forced_default' if e.get('forced_default') else 'own'))
        win = SERVED_UNVERIFIED.get(eid)
        if not a and win and (win[0] <= str(e.get('ts') or '')[:10] <= win[1]):
            add(e['team'], eid, e.get('week') or start, e['kind'], 'served_unverified')
    missed = {}
    for e in evs:
        if e.get('type') in ('decision_missed', 'decision_skipped_infra') and e.get('kind') != 'draft_pick' and (e['decision_id'] not in decs):
            missed[e['decision_id']] = e
    scored = {e.get('week') for e in evs if e.get('type') == 'week_scored'}
    final = {e['decision_id'] for e in evs if e.get('type') == 'decision_missed'}
    for did, e in list(missed.items()):
        w = e.get('week') or start
        done = did in final or w in scored
        if not done:
            del missed[did]
        add(e['team'], e.get('entrant') or lg.entrant(e['team'], w), w, e.get('kind') or 'unknown', 'missed' if done else 'pending')
    for e in evs:
        if e.get('type') != 'draft_pick':
            continue
        eid = lg.entrant(e['team'], start)
        did = e.get('decision_id')
        src = e.get('source')
        if src == 'queue' and (decs.get(did) or {}).get('forced_default'):
            src = 'default'
        a = ann.get(did) if did else None
        add(e['team'], eid, start, 'draft_pick', a or ('own' if src in ('model', 'queue') else 'forced_default'))
        win = SERVED_UNVERIFIED.get(eid)
        if not a and src in ('model', 'queue') and win and (win[0] <= str(e.get('ts') or '')[:10] <= win[1]):
            add(e['team'], eid, start, 'draft_pick', 'served_unverified')
    lineup_weeks = {(e['team'], e.get('week')) for e in decs.values() if e.get('kind') == 'start_sit'}
    lineup_weeks |= {(e['team'], e.get('week')) for e in missed.values() if e.get('kind') == 'start_sit'}
    own_lineup_before: set[str] = set()
    superseded = _superseded(evs)
    for i, e in enumerate(evs):
        if e.get('type') == 'lineup_set' and e.get('source') == 'model':
            own_lineup_before.add(e['team'])
        if e.get('type') != 'week_scored' or i in superseded:
            continue
        w = int(e['week'])
        for t in e.get('points') or {}:
            if (t, w) in lineup_weeks or t in own_lineup_before:
                continue
            add(t, lg.entrant(t, w), w, 'start_sit', 'engine_lineup')
    out = []
    for (team, eid, week, kind), c in sorted(rows.items()):
        expected = c['own'] + c['forced_default'] + c['missed'] + c['engine_lineup']
        out.append({'league_id': lg.id, 'team_id': team, 'entrant_id': eid, 'week': week, 'kind': kind, 'expected': expected, **c})
    return out

def _superseded(evs: list[dict]) -> set[int]:
    last: dict = {}
    for i, e in enumerate(evs):
        if e.get('type') == 'week_scored':
            last[e['week']] = i
    return {i for i, e in enumerate(evs) if e.get('type') == 'week_scored' and last[e['week']] != i}
