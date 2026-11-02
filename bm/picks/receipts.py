from __future__ import annotations
import json
import sys
from datetime import datetime, timezone
from bm import paths, records
POLICY = "legacy-receipt-annotation/1: gateway model_call time; full slate; before the slate's first kickoff"

def receipts_by_record(rows: list[dict]) -> dict[str, dict]:
    from bm.ops.served_model import annotated_records
    out = {}
    for r in rows:
        if r.get('kind') == 'pick_receipt' and r.get('pick_record_id'):
            out[r['pick_record_id']] = r
    for rid, ann in annotated_records(rows).items():
        out[rid] = dict(out.get(rid) or {}, served_model_annotation=ann)
    return out

def slate_games(rows: list[dict]) -> list[str]:
    sets = {tuple(sorted(r['lines'])) for r in rows if r.get('kind') == 'pick' and r.get('step') == 'line' and isinstance(r.get('lines'), dict)}
    if len(sets) != 1:
        raise ValueError(f'slate game set is not unique across line-step records ({len(sets)} variants)')
    return list(next(iter(sets)))

def _gateway_calls(request_ids: set[str], decision_prefix: str) -> dict[str, list[dict]]:
    calls: dict[str, list[dict]] = {}
    for path in paths.gateway_logs(records.ROOT):
        if not path.exists():
            continue
        with open(path) as f:
            for line in f:
                if decision_prefix not in line:
                    continue
                r = json.loads(line)
                p = r.get('payload') or {}
                if p.get('type') == 'model_call' and p.get('request_id') in request_ids:
                    calls.setdefault(p['request_id'], []).append({'seq': r.get('seq'), 'hash': r.get('hash'), 'ts': p.get('ts') or r.get('ts'), 'decision_id': p.get('decision_id'), 'prompt_blob': p.get('prompt_blob'), 'error': p.get('error'), 'log': str(path)})
    return calls

def build(slate: str, rows: list[dict], schedule: list[dict]) -> list[dict]:
    have = {k: v for k, v in receipts_by_record(rows).items() if v.get('kind') == 'pick_receipt'}
    todo = [r for r in rows if r.get('kind') == 'pick' and r.get('outcome') == 'own' and isinstance(r.get('answer'), dict) and (not r.get('received_at')) and (r.get('record_id') not in have)]
    if not todo:
        return []
    games = slate_games(rows)
    kick = {g['game_id']: g['kickoff'] for g in schedule}
    if any((g not in kick for g in games)):
        raise ValueError('slate game missing from the schedule')
    deadline = min((datetime.fromisoformat(kick[g]) for g in games))
    calls = _gateway_calls({r['request_id'] for r in todo if r.get('request_id')}, f'"picks:{slate}:')
    out = []
    for r in todo:
        got = [c for c in calls.get(r.get('request_id'), []) if c['decision_id'] == f'picks:{r['key']}']
        last = max(got, key=lambda c: c['seq']) if got else None
        covered = sorted(set(r['answer']) & set(games))
        out.append(records.make('pick_receipt', f'receipt:{r['record_id']}', datetime.now(timezone.utc).isoformat(), pick_record_id=r['record_id'], pick_key=r['key'], seat_key=r.get('seat_key') or r['key'], slate=slate, entrant=r.get('entrant'), instance=r.get('instance'), step=r.get('step'), request_id=r.get('request_id'), received_at=last['ts'] if last else None, receipt_source='gateway model_call' if last else 'missing: no gateway model_call', gateway_log=last['log'] if last else None, gateway_seq=last['seq'] if last else None, gateway_hash=last['hash'] if last else None, gateway_error=last['error'] if last else None, prompt_matches=bool(last and last['prompt_blob'] == r.get('prompt_sha256')), as_of=r.get('snapshot'), deadline=deadline.isoformat(), expected_games=games, covered_games=len(covered), missing_games=sorted(set(games) - set(r['answer'])), policy=POLICY))
    return out

def annotate(slate: str, dry_run: bool=False) -> dict:
    from bm.stats import nflverse as nv
    log = records.store(kind='picks', key=slate)
    rows = [r['payload'] for r in log.rows()]
    new = build(slate, rows, nv.schedule(2026))
    if not dry_run:
        for rec in new:
            records.write(rec, log)
    from bm.picks.run import receipt_status
    return {'slate': slate, 'appended': 0 if dry_run else len(new), 'dry_run': dry_run, 'statuses': _count((receipt_status(next((p for p in rows if p.get('record_id') == x['pick_record_id'])), x['deadline'], x) for x in new))}

def _count(xs) -> dict:
    out: dict = {}
    for x in xs:
        out[x] = out.get(x, 0) + 1
    return out

def main(argv: list[str] | None=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if argv[:1] != ['annotate'] or len(argv) < 2:
        print(__doc__)
        return 2
    print(json.dumps(annotate(argv[1], dry_run='--dry-run' in argv), indent=1))
    return 0
if __name__ == '__main__':
    raise SystemExit(main())
