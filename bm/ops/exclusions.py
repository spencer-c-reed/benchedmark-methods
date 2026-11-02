from __future__ import annotations
import json
import sys
from datetime import datetime, timezone
from bm.records import INFRA
POLICY_AT = datetime(2026, 10, 8, 22, 40, tzinfo=timezone.utc)
LEAGUES = tuple((f'nfl-l{i}' for i in range(1, 7)))
REASON = 'forced default from an infrastructure failure before the Oct 8 18:40 ET no-autopilot rule (D-REL-1a); under the current rule it would have been held and retried, never charged to the model'

def candidates(events: list[dict]) -> list[dict]:
    out = []
    for e in events:
        fd = e.get('forced_default') if e.get('type') == 'decision' else None
        if not fd or fd.get('cause') not in INFRA or (not e.get('logged_at')):
            continue
        if datetime.fromisoformat(e['logged_at']) < POLICY_AT:
            out.append(e)
    return out
UNATTRIBUTED = ('excluded', 'misserved')

def excluded_ids(events: list[dict]) -> set[str]:
    return {e['decision_id'] for e in events if e.get('type') == 'decision_annotation' and e.get('annotation') in UNATTRIBUTED}

def annotate(lg, apply: bool=False, now: datetime | None=None) -> list[str]:
    from bm.league.season import iso, now_utc
    evs = lg.events()
    done = {e['decision_id'] for e in evs if e.get('type') == 'decision_annotation' and e.get('annotation') == 'excluded'}
    todo = [e for e in candidates(evs) if e['decision_id'] not in done]
    if apply:
        at = now or now_utc()
        for e in todo:
            lg.append({'type': 'decision_annotation', 'ts': iso(at), 'decision_id': e['decision_id'], 'annotation': 'excluded', 'team': e.get('team'), 'entrant': e.get('entrant'), 'kind': e.get('kind'), 'cause': e['forced_default']['cause'], 'decision_logged_at': e['logged_at'], 'policy': 'D-REL-1a no autopilot from our failures (Oct 8 2026 18:40 ET)', 'reason': REASON})
    return [e['decision_id'] for e in todo]

def main(argv: list[str] | None=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    apply = '--apply' in argv
    ids = [a for a in argv if not a.startswith('--')] or list(LEAGUES)
    from bm.league.season import League
    out = {}
    for i in ids:
        lg = League(i)
        if apply:
            with lg.exclusive():
                out[i] = annotate(lg, apply=True)
        else:
            out[i] = annotate(lg)
    print(json.dumps({'applied': apply, 'marked': out, 'total': sum((len(v) for v in out.values()))}, indent=1))
    return 0
if __name__ == '__main__':
    raise SystemExit(main())
