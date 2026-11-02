from __future__ import annotations
import gzip
import json
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from bm.gateway import identity
from bm.log import ChainLog
from bm.paths import runtime_root
CODE_ROOT = Path(__file__).resolve().parents[2]
CONVERSATIONS = Path.home() / '.gemini' / 'antigravity-cli' / 'conversations'
ANNOTATION = 'misserved'
ROW_KIND = 'served_model_annotation'
SOURCE = 'cli_generation_metadata'
FINDING = 'ba487cf'
EVIDENCE_FIELD = 'conversation DB gen_metadata.data protobuf field 1 -> 19 (bm.gateway.identity.agy_served)'
POLICY = "served-model provenance (coordinator, Oct 13 2026): an answer served by a model other than the one requested counts for no model: excluded from the requested model's metrics and attributed to neither model in rankings; team points and standings unchanged"
EXCLUDED_STATUS = 'excluded: served by another model'
KIND_LABEL = {'start_sit': 'lineup', 'waiver_bid': 'waiver', 'trade_offer': 'trade_offer', 'trade_response': 'trade_response', 'forecast_book': 'book', 'draft_pick': 'draft'}

def _label(kind: str | None) -> str:
    return KIND_LABEL.get(kind or '', kind or 'unknown')

def misserved_ids(events: list[dict]) -> set[str]:
    return {e['decision_id'] for e in events if e.get('type') == 'decision_annotation' and e.get('annotation') == ANNOTATION}

def misserved_team_weeks(events: list[dict]) -> set[tuple[str, int]]:
    gone = misserved_ids(events)
    return {(e['team'], e['week']) for e in events if e.get('type') == 'decision' and e.get('kind') == 'start_sit' and (e.get('decision_id') in gone)}

def annotated_records(rows: list[dict]) -> dict[str, dict]:
    return {r['target_record_id']: r for r in rows if r.get('kind') == ROW_KIND and r.get('target_record_id')}

def annotated_requests(rows: list[dict]) -> set[str]:
    return {rid for r in rows if r.get('kind') == ROW_KIND for rid in r.get('request_ids') or []}

def _blob(root: Path, h: str | None) -> dict:
    if not h:
        return {}
    for base in (root / 'blobs', CODE_ROOT / 'blobs'):
        p = base / h[:2] / f'{h}.gz'
        if p.exists():
            try:
                return json.loads(gzip.decompress(p.read_bytes()))
            except ValueError:
                return {}
    return {}

def _rows(path: Path) -> list[dict]:
    return [dict(r['payload'], seq=r['seq']) for r in ChainLog(path).rows()] if path.exists() else []

def agy_calls(root: Path, conversations: Path=CONVERSATIONS) -> tuple[list[dict], list[dict]]:
    bad, unresolved = ([], [])
    path = root / 'logs' / 'gateway.jsonl'
    if not path.exists():
        return (bad, unresolved)
    with open(path) as f:
        for line in f:
            if '"agy-cli"' not in line:
                continue
            row = json.loads(line)
            p = row.get('payload') or {}
            if p.get('type') != 'model_call' or p.get('surface') != 'agy-cli' or p.get('error'):
                continue
            raw = _blob(root, p.get('raw_blob'))
            conv = raw.get('conversation_id') or (raw.get('result') or {}).get('conversation_id')
            if not conv or not re.fullmatch('[0-9a-f-]{36}', str(conv)):
                continue
            served = identity.agy_served(conversations / f'{conv}.db')
            req = p.get('model_requested')
            other = [s for s in served if not identity.same_model(req, s)]
            if not other:
                continue
            item = {'request_id': p['request_id'], 'gateway_seq': row['seq'], 'gateway_hash': row['hash'], 'ts': p.get('ts'), 'decision_id': p.get('decision_id'), 'entrant': p.get('entrant'), 'lane': p.get('lane'), 'model_requested': req, 'model_reported_at_call': p.get('model_reported'), 'identity_source_at_call': p.get('identity_source'), 'served_models': served, 'served_resolved': [identity.SERVED_ALIASES.get(s, s) for s in other], 'conversation_id': conv, 'raw_blob': p.get('raw_blob')}
            (bad if all((s in identity.SERVED_ALIASES for s in other)) else unresolved).append(item)
    return (bad, unresolved)

def _evidence(c: dict) -> dict:
    return {'gateway_log': 'logs/gateway.jsonl', 'gateway_seq': c['gateway_seq'], 'gateway_hash': c['gateway_hash'], 'raw_blob': c['raw_blob'], 'conversation_db': f'~/.gemini/antigravity-cli/conversations/{c['conversation_id']}.db', 'field': EVIDENCE_FIELD, 'served_models': c['served_models'], 'finding_commit': FINDING}

def items(root: Path, calls: list[dict]) -> list[dict]:
    by_rid = {c['request_id']: c for c in calls}
    out = []
    for lg_dir in sorted((root / 'leagues').glob('*/log.jsonl')):
        for e in _rows(lg_dir):
            if e.get('type') != 'decision':
                continue
            hit = [by_rid[r] for r in e.get('model_call_ids') or [] if r in by_rid]
            if hit:
                out.append({'domain': 'league', 'league': lg_dir.parent.name, 'decision_id': e['decision_id'], 'team': e.get('team'), 'entrant': e.get('entrant'), 'kind': _label(e.get('kind')), 'decision_kind': e.get('kind'), 'week': e.get('week'), 'decision_seq': e['seq'], 'decision_logged_at': e.get('logged_at'), 'forced_default': bool(e.get('forced_default')), 'calls': hit})
    for sub, domain in (('picks', 'picks'), ('canary', 'replay'), ('replay', 'replay')):
        for path in sorted((root / 'records' / sub).glob('*.jsonl')):
            for r in _rows(path):
                if r.get('kind') == ROW_KIND:
                    continue
                turns = r.get('turns') if isinstance(r.get('turns'), list) else []
                rids = [r.get('request_id')] + [t.get('request_id') for t in turns if isinstance(t, dict)]
                hit = [by_rid[x] for x in dict.fromkeys(rids) if x in by_rid]
                if not hit or r.get('kind') in ('pick_receipt', 'pick_settled', 'pick_settled_revision'):
                    continue
                out.append({'domain': domain, 'league': None, 'log': f'records/{sub}/{path.name}', 'record_id': r.get('record_id'), 'record_key': r.get('key'), 'record_kind': r.get('kind'), 'entrant': r.get('entrant'), 'kind': 'picks' if domain == 'picks' else 'replay', 'replayed_kind': _label(r.get('decision_kind')) if domain == 'replay' else None, 'step': r.get('step'), 'slate': path.stem if domain == 'picks' else None, 'replayed_state': r.get('state'), 'record_seq': r['seq'], 'calls': hit})
    return out

def counts(found: list[dict]) -> dict:
    by = Counter()
    for i in found:
        where = i.get('league') or (f'picks:{i['slate']}' if i['domain'] == 'picks' else i['log'][len('records/'):-len('.jsonl')])
        by[where, i['kind']] += 1
    table = {}
    for (where, kind), n in sorted(by.items()):
        table.setdefault(where, {})[kind] = n
    return {'by_where_and_kind': table, 'by_kind': dict(Counter((i['kind'] for i in found))), 'by_domain': dict(Counter((i['domain'] for i in found))), 'items': len(found), 'replay_by_replayed_kind': dict(Counter((i['replayed_kind'] for i in found if i['domain'] == 'replay'))), 'calls': len({c['request_id'] for i in found for c in i['calls']})}

def scan(root: Path | None=None, conversations: Path=CONVERSATIONS) -> dict:
    root = Path(root) if root else runtime_root(CODE_ROOT)
    bad, unresolved = agy_calls(root, conversations)
    found = items(root, bad)
    covered = {c['request_id'] for i in found for c in i['calls']}
    return {'generated_at': datetime.now(timezone.utc).isoformat(), 'root': str(root), 'finding_commit': FINDING, 'served_model_source': SOURCE, 'evidence_field': EVIDENCE_FIELD, 'policy': POLICY, 'misserved_calls': len(bad), 'calls_without_retained_record': [c['request_id'] for c in bad if c['request_id'] not in covered], 'counts': counts(found), 'items': found, 'unresolved_labels': dict(Counter((f'{c['model_requested']} -> {','.join(c['served_models'])} on {c['ts'][:10]}' for c in unresolved)))}

def _ann_fields(i: dict, on: str) -> dict:
    cs = i['calls']
    return {'served_model': ','.join(dict.fromkeys((s for c in cs for s in c['served_models']))), 'served_model_resolved': ','.join(dict.fromkeys((s for c in cs for s in c['served_resolved']))), 'served_model_source': SOURCE, 'model_requested': cs[0]['model_requested'], 'request_ids': [c['request_id'] for c in cs], 'gateway_seqs': [c['gateway_seq'] for c in cs], 'evidence': [_evidence(c) for c in cs], 'annotated_on': on, 'policy': POLICY}

def _with_run_lock(lg_dir: Path, fn, tries: int=60, wait_s: float=5.0):
    import fcntl
    for _ in range(tries):
        with open(lg_dir / 'run.lock', 'a') as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                time.sleep(wait_s)
                continue
            try:
                return fn()
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    raise RuntimeError(f'{lg_dir.name} stayed busy; nothing appended')

def apply(root: Path | None=None, found: list[dict] | None=None, now: datetime | None=None, conversations: Path=CONVERSATIONS) -> dict:
    from bm import records
    root = Path(root) if root else runtime_root(CODE_ROOT)
    found = scan(root, conversations)['items'] if found is None else found
    at = now or datetime.now(timezone.utc)
    on = at.date().isoformat()
    done = Counter()
    for lid in sorted({i['league'] for i in found if i['domain'] == 'league'}):
        lg_dir = root / 'leagues' / lid
        mine = [i for i in found if i.get('league') == lid]

        def write(lg_dir=lg_dir, mine=mine):
            log = ChainLog(lg_dir / 'log.jsonl')
            have = misserved_ids(log.payloads())
            for i in mine:
                if i['decision_id'] in have:
                    continue
                log.append({'type': 'decision_annotation', 'ts': at.astimezone().isoformat(timespec='seconds'), 'decision_id': i['decision_id'], 'annotation': ANNOTATION, 'team': i['team'], 'entrant': i['entrant'], 'kind': i['decision_kind'], 'decision_week': i['week'], 'decision_logged_at': i['decision_logged_at'], **_ann_fields(i, on), 'logged_at': at.isoformat(timespec='seconds')})
                done['league'] += 1
        _with_run_lock(lg_dir, write)
    for i in (x for x in found if x['domain'] != 'league'):
        log = ChainLog(root / i['log'])
        rows = [r['payload'] for r in log.rows()]
        if i['record_id'] not in annotated_records(rows):
            records.write(records.make(ROW_KIND, f'{ANNOTATION}:{i['record_id']}', at.isoformat(), target_record_id=i['record_id'], target_key=i['record_key'], target_kind=i['record_kind'], entrant=i['entrant'], annotation=ANNOTATION, **_ann_fields(i, on)), log)
            done[i['domain']] += 1
        if i['domain'] == 'picks':
            n = _revise_settled(log, i, at)
            if n:
                done['pick_settled_revision'] += n
    return {'applied': dict(done), 'items': len(found)}

def _revise_settled(log: ChainLog, i: dict, at: datetime) -> int:
    from bm import records
    latest = {}
    for r in (r['payload'] for r in log.rows()):
        if r.get('kind') in ('pick_settled', 'pick_settled_revision') and str(r.get('key', '')).startswith(i['record_key'] + ':'):
            latest[r['key']] = r
    n = 0
    for key, r in latest.items():
        if r.get('measurement_status') == EXCLUDED_STATUS:
            continue
        keep = {k: v for k, v in r.items() if k not in ('schema', 'type', 'kind', 'ts', 'record_id', 'key', 'supersedes_record_id', 'measurement_status')}
        log.append(records.make('pick_settled_revision', key, at.isoformat(), supersedes_record_id=r.get('record_id'), **keep, measurement_status=EXCLUDED_STATUS))
        n += 1
    return n

def main(argv: list[str] | None=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ('scan', 'apply'):
        print(__doc__)
        return 2
    root = Path(argv[argv.index('--root') + 1]).expanduser() if '--root' in argv else None
    res = scan(root)
    if argv[0] == 'apply':
        res['apply'] = apply(root, res['items'])
    if '--out' in argv:
        out = Path(argv[argv.index('--out') + 1]).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(res, indent=1) + '\n')
    print(json.dumps({k: v for k, v in res.items() if k != 'items'}, indent=1))
    return 0
if __name__ == '__main__':
    raise SystemExit(main())
