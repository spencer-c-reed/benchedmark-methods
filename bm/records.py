from __future__ import annotations
import hashlib
import json
from pathlib import Path
from bm.log import ChainLog, canonical
ROOT = Path(__file__).resolve().parents[1]
SCHEMA = 'session-record/1'
INFRA = ('RATE_LIMITED', 'QUOTA_YIELD', 'SELF_CAP', 'LANE_DISABLED', 'LANE_BREAKER', 'AUTH', 'PAID_REFUSED', 'TIMEOUT', 'TRANSIENT', 'REFUSED', 'NO_ENTRANT', 'LANE_FAILED', 'PROVIDER_GONE', 'MANIFEST_REFUSED')
MODEL = ('PARSE', 'ILLEGAL')
CAUSES = INFRA + MODEL
INWORLD_OUT_PER_M = 100.0
INWORLD_IN_PER_M = 10.0
REASONING_IN_OUTPUT = {'claude_cli': True, 'anthropic_api': True, 'codex_cli': True, 'openai_compat': True, 'opencode': False, 'agy_cli': False}
_DECISION_KEYS = None

def inworld_usd(usage: dict, adapter: str | None) -> float:
    u = usage or {}
    out = u.get('output') or 0
    if not REASONING_IN_OUTPUT.get(adapter or '', True):
        out += u.get('reasoning') or 0
    return round((u.get('input') or 0) * INWORLD_IN_PER_M / 1000000.0 + out * INWORLD_OUT_PER_M / 1000000.0, 6)

def failure(code: str | None, detail: str='') -> dict | None:
    if not code:
        return None
    code = {'QUOTA_YIELD': 'QUOTA_YIELD', 'unparseable': 'PARSE'}.get(code, code)
    if code not in CAUSES:
        code = 'TRANSIENT'
    return {'code': code, 'layer': 'infra' if code in INFRA else 'model', 'detail': (detail or '')[:300]}

def decision_failure(dec: dict, calls: list[dict]) -> dict | None:
    fd = (dec.get('forced_default') or {}).get('cause')
    if not fd:
        return None
    if fd == 'PARSE':
        viol = (dec.get('validation') or {}).get('violations') or []
        code = 'PARSE' if any(('unparseable' in v for v in viol)) else 'ILLEGAL'
        return failure(code, '; '.join(viol)[:300])
    errs = [c for c in calls if c.get('error')]
    if errs:
        return failure(errs[-1]['error'], errs[-1].get('error_detail') or '')
    return failure(fd, '; '.join((dec.get('validation') or {}).get('violations') or []))

class GatewayIndex:

    def __init__(self, path: Path | None=None):
        from bm.paths import gateway_logs
        self.by_req: dict[str, list[dict]] = {}
        paths = [path] if path is not None else gateway_logs(ROOT)
        seen_rows = set()
        for log_path in paths:
            for r in ChainLog(log_path).rows():
                if r['hash'] in seen_rows:
                    continue
                seen_rows.add(r['hash'])
                p = r['payload']
                if p.get('type') == 'model_call':
                    self.by_req.setdefault(p['request_id'], []).append(dict(p, gateway_seq=r['seq'], gateway_log=str(log_path)))

    def calls(self, request_ids: list[str]) -> list[dict]:
        out = []
        for rid in request_ids:
            out.extend(self.by_req.get(rid, []))
        return out

class CallsIndex(GatewayIndex):

    def __init__(self, by_req: dict[str, list[dict]]):
        self.by_req = by_req

def _lane_adapters() -> dict:
    import yaml
    return {l['lane']: l.get('adapter') for l in yaml.safe_load((ROOT / 'config' / 'lanes.yaml').read_text())}

def transport_cost_fields(call: dict) -> dict:
    totals = call.get('attempt_totals') or {}
    cost = call.get('cost') or {}
    terminal_list = cost.get('usd_eq', call.get('terminal_list_usd', call.get('list_usd')))
    terminal_billed = cost.get('billed_usd', call.get('terminal_billed_usd', call.get('billed_usd')))
    known_attempts = (call.get('attempts') or 1) <= 1
    price = totals.get('usd_eq') if totals else terminal_list if known_attempts else None
    billed_complete = totals.get('billed_cost_complete', True) if totals else known_attempts and terminal_billed is not None
    billed = totals.get('billed_usd') if totals else terminal_billed
    history = call.get('attempt_history') or []
    observed_list = sum(((h.get('cost') or {}).get('usd_eq') or 0 for h in history)) if history else terminal_list or 0
    if totals:
        observed_list = totals.get('usd_eq_observed_lower_bound', totals.get('usd_eq')) or observed_list
    return {'terminal_list_usd': terminal_list, 'terminal_billed_usd': terminal_billed, 'list_usd': price, 'billed_usd': billed if billed_complete else None, 'list_usd_observed_lower_bound': observed_list, 'billed_usd_observed_lower_bound': billed or 0, 'transport_cost_coverage': 'complete' if price is not None and billed_complete and (totals or known_attempts) else 'incomplete'}

def cost_summary(turns: list[dict]) -> dict:
    return {'list_usd': round(sum((t['list_usd'] for t in turns)), 6) if all((t.get('list_usd') is not None for t in turns)) else None, 'billed_usd': round(sum((t['billed_usd'] for t in turns)), 6) if all((t.get('billed_usd') is not None for t in turns)) else None, 'list_usd_observed_lower_bound': round(sum((t.get('list_usd_observed_lower_bound', t.get('list_usd')) or 0 for t in turns)), 6), 'billed_usd_observed_lower_bound': round(sum((t.get('billed_usd_observed_lower_bound', t.get('billed_usd')) or 0 for t in turns)), 6)}

def turn(call: dict, adapters: dict) -> dict:
    ad = adapters.get(call.get('lane'))
    return {'request_id': call['request_id'], 'gateway_seq': call.get('gateway_seq'), 'lane': call.get('lane'), 'route': call.get('route', 0), 'adapter': ad, 'model_requested': call.get('model_requested'), 'identity_source': call.get('identity_source', 'legacy_unspecified'), 'stop_reason': call.get('stop_reason'), 'attempt_history': call.get('attempt_history') or [], 'attempt_totals': call.get('attempt_totals') or {}, 'served_model': call.get('model_reported'), 'effort': call.get('effort'), 'usage': call.get('usage') or {}, **transport_cost_fields(call), 'inworld_usd': inworld_usd(call.get('usage') or {}, ad), 'latency_ms': call.get('latency_ms'), 'attempts': call.get('attempts'), 'failure': failure(call.get('error'), call.get('error_detail') or ''), 'prompt_blob': call.get('prompt_blob'), 'system_blob': call.get('system_blob'), 'text_blob': call.get('text_blob')}

def from_decision(dec: dict, league: dict, gw: GatewayIndex, adapters: dict | None=None, harness_version: str='windowed-1') -> dict:
    adapters = adapters if adapters is not None else _lane_adapters()
    calls = gw.calls(dec.get('model_call_ids') or [])
    turns = [turn(c, adapters) for c in calls]
    seat = next((s for s in league.get('seats', []) if s['team_id'] == dec.get('team')), {})
    fail = decision_failure(dec, calls)
    served = sorted({t['served_model'] for t in turns if t['served_model']})
    rec = {'schema': SCHEMA, 'type': 'session', 'kind': 'decision', 'ts': dec['ts'], 'record_id': _rid('decision', dec['decision_id']), 'league': dec.get('league'), 'team': dec.get('team'), 'entrant': dec.get('entrant'), 'variant_id': f'{seat.get('variant', 'standard')}@{dec.get('prompt_template_hash', '')[:12]}', 'harness': 'windowed', 'harness_version': harness_version, 'decision_id': dec['decision_id'], 'decision_kind': dec.get('kind'), 'week': dec.get('week'), 'state_hash': dec.get('state_hash'), 'rendered_prompt_hash': dec.get('rendered_prompt_hash'), 'outcome': 'own' if not fail else 'refused' if fail['code'] == 'REFUSED' else 'infra_default' if fail['layer'] == 'infra' else 'model_default', 'failure': fail, 'served_models': served, 'turns': turns, 'tools': dec.get('research') or [], 'cost': {'inworld_usd': round(sum((t['inworld_usd'] for t in turns)), 6), **cost_summary(turns)}, 'action_hash': hashlib.sha256(canonical(dec.get('action'))).hexdigest(), 'decision': {k: v for k, v in dec.items() if k != 'seq'}}
    return rec

def decision_event(rec: dict) -> dict:
    return dict(rec['decision'])

def _rid(kind: str, key: str) -> str:
    return hashlib.sha256(f'{kind}:{key}'.encode()).hexdigest()[:24]

def make(kind: str, key: str, ts: str, **fields) -> dict:
    return {'schema': SCHEMA, 'type': 'session', 'kind': kind, 'ts': ts, 'record_id': _rid(kind, key), 'key': key, **fields}

def store(league_id: str | None=None, kind: str | None=None, key: str | None=None) -> ChainLog:
    if league_id:
        return ChainLog(ROOT / 'leagues' / league_id / 'sessions.jsonl')
    return ChainLog(ROOT / 'records' / kind / f'{key}.jsonl')

def write(rec: dict, log: ChainLog) -> dict:
    if any((r['payload'].get('record_id') == rec['record_id'] for r in log.rows())):
        return rec
    log.append(rec)
    return rec

def backfill(league_id: str, gw: GatewayIndex | None=None) -> list[dict]:
    lg = json.loads((ROOT / 'leagues' / league_id / 'league.json').read_text())
    gw = gw or GatewayIndex()
    ad = _lane_adapters()
    evs = ChainLog(ROOT / 'leagues' / league_id / 'log.jsonl').payloads()
    return [from_decision(e, lg, gw, ad) for e in evs if e['type'] == 'decision']

def stored_decisions(league_id: str) -> tuple[list[dict], dict[str, dict], list[str]]:
    directory = ROOT / 'leagues' / league_id
    events = ChainLog(directory / 'log.jsonl')
    sessions = ChainLog(directory / 'sessions.jsonl')
    errors = []
    for name, log in (('events', events), ('sessions', sessions)):
        ok, _, detail = log.verify()
        if not ok:
            errors.append(f'{name}: {detail}')
    evs = events.payloads()
    expected = {e['decision_id']: e for e in evs if e['type'] == 'decision'}
    recs = {}
    for row in sessions.rows():
        rec = row['payload']
        if rec.get('kind') != 'decision':
            continue
        did = rec.get('decision_id')
        if did in recs:
            errors.append(f'duplicate decision: {did}')
        recs[did] = rec
        if rec.get('schema') != SCHEMA or rec.get('record_id') != _rid('decision', str(did)):
            errors.append(f'invalid decision schema/id: {did}')
    for did in expected.keys() - recs.keys():
        errors.append(f'missing decision: {did}')
    for did in recs.keys() - expected.keys():
        errors.append(f'extra decision: {did}')
    for did in expected.keys() & recs.keys():
        dec = {k: v for k, v in expected[did].items() if k != 'seq'}
        rec = recs[did]
        if canonical(rec.get('decision')) != canonical(dec):
            errors.append(f'decision bytes differ: {did}')
        if rec.get('state_hash') != dec.get('state_hash'):
            errors.append(f'decision state hash differs: {did}')
        if rec.get('action_hash') != hashlib.sha256(canonical(dec.get('action'))).hexdigest():
            errors.append(f'decision action hash differs: {did}')
    return (evs, recs, errors)

def _seat_held(directory: Path, team: str) -> bool:
    import fcntl
    path = directory / f'seat-{team}.lock'
    if not path.exists():
        return False
    with open(path, 'a') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(f, fcntl.LOCK_UN)
    return False
_WAKE_OPEN_FIELDS = (('team', 'team'), ('ts', 'ts'), ('variant_id', 'variant'), ('prompt_hashes', 'prompt_hashes'), ('harness_version', 'harness_version'), ('data_as_of', 'data_as_of'), ('context_clock_source', 'context_clock_source'))
_WAKE_CLOSE_FIELDS = (('outcome', 'outcome'), ('ended_by', 'ended_by'))

def _worker_lost(open_: dict, fail: dict) -> bool:
    from datetime import datetime
    from bm.ops.nodecisions import WORKER_LOST_AFTER
    return fail.get('cause') == 'WORKER_LOST' and fail.get('detected_by') == 'commissioner' and (fail.get('team') == open_['team']) and (datetime.fromisoformat(fail['ts']) >= datetime.fromisoformat(open_['started_at']) + WORKER_LOST_AFTER)

def stored_wakes(league_id: str, now=None) -> tuple[dict, list[str]]:
    from datetime import datetime, timezone
    now = now or datetime.now(timezone.utc)
    directory = ROOT / 'leagues' / league_id
    evs = ChainLog(directory / 'log.jsonl').payloads()
    errors: list[str] = []
    opens: dict[str, dict] = {}
    closes: dict[str, list[dict]] = {}
    failed: dict[str, list[dict]] = {}
    subs: dict[str, dict] = {}
    resolved: dict[str, list[dict]] = {}
    for e in evs:
        t = e['type']
        if t == 'wake_open':
            if e['wake_id'] in opens:
                errors.append(f'duplicate wake open: {e['wake_id']}')
            opens[e['wake_id']] = e
        elif t == 'wake_close':
            closes.setdefault(e['wake_id'], []).append(e)
        elif t == 'wake_failed':
            failed.setdefault(e['wake_id'], []).append(e)
        elif t == 'action_submitted':
            if e['action_id'] in subs:
                errors.append(f'duplicate action: {e['action_id']}')
            subs[e['action_id']] = e
        elif t == 'action_resolved':
            resolved.setdefault(e['action_id'], []).append(e)
    recs: dict[str, dict] = {}
    for row in ChainLog(directory / 'sessions.jsonl').rows():
        rec = row['payload']
        if rec.get('kind') != 'wake':
            continue
        wid = rec.get('key')
        if wid in recs:
            errors.append(f'duplicate wake record: {wid}')
        recs[wid] = rec
        if rec.get('schema') != SCHEMA or rec.get('record_id') != _rid('wake', str(wid)):
            errors.append(f'invalid wake schema/id: {wid}')
    last_open = {}
    for wid, o in opens.items():
        last_open[o['team']] = wid
    in_flight = set()
    lost: dict[str, dict] = {}
    for wid in (closes.keys() | failed.keys()) - opens.keys():
        errors.append(f'wake terminal without open: {wid}')
    for wid, o in opens.items():
        cl = closes.get(wid, [])
        if not cl:
            fl = failed.get(wid, [])
            if len(fl) == 1 and _worker_lost(o, fl[0]):
                lost[wid] = fl[0]
                if wid in recs:
                    errors.append(f'wake record for lost wake: {wid}')
            elif last_open.get(o['team']) == wid and _seat_held(directory, 'draft' if o.get('kind') == 'draft' else o['team']):
                in_flight.add(wid)
            else:
                errors.append(f'unclosed wake: {wid}')
            continue
        if len(cl) > 1:
            errors.append(f'duplicate wake close: {wid}')
        c = cl[0]
        if (c['outcome'] == 'lane_failed') != (len(failed.get(wid, [])) == 1) or len(failed.get(wid, [])) > 1:
            errors.append(f'wake_failed does not match outcome: {wid}')
        if c['team'] != o['team'] or datetime.fromisoformat(c['ts']) < datetime.fromisoformat(o['started_at']):
            errors.append(f'wake close inconsistent with open: {wid}')
        rec = recs.get(wid)
        if rec is None:
            errors.append(f'missing wake record: {wid}')
            continue
        diffs = [rf for rf, ef in _WAKE_OPEN_FIELDS if rec.get(rf) != o.get(ef)]
        diffs += [rf for rf, ef in _WAKE_CLOSE_FIELDS if rec.get(rf) != c.get(ef)]
        reason = rec.get('reason') or {}
        diffs += [f'reason.{k}' for k, ef in (('kind', 'kind'), ('text', 'reason')) if reason.get(k) != o.get(ef)]
        if rec.get('league') != league_id:
            diffs.append('league')
        turns = rec.get('turns') or []
        if len(turns) != c.get('turns'):
            diffs.append('turns')
        cost = rec.get('cost') or {}
        if cost.get('inworld_usd') != c.get('inworld_usd') or round(sum((t.get('inworld_usd', 0) for t in turns)), 6) != c.get('inworld_usd'):
            diffs.append('cost.inworld_usd')
        if cost.get('fees_usd') != c.get('fees_usd'):
            diffs.append('cost.fees_usd')
        if (rec.get('failure') is None) != (c['outcome'] == 'own'):
            diffs.append('failure')
        errors.extend((f'wake field differs: {wid} {f}' for f in diffs))
    for wid in recs.keys() - opens.keys():
        errors.append(f'extra wake record: {wid}')
    pending = 0
    for aid, s in subs.items():
        o = opens.get(s['wake_id'])
        if o is None:
            errors.append(f'action from unknown wake: {aid}')
        elif s['wake_started_at'] != o['started_at'] or s['team'] != o['team']:
            errors.append(f'action inconsistent with its wake: {aid}')
        else:
            end = (closes.get(s['wake_id']) or [lost.get(s['wake_id'])])[0]
            if end and datetime.fromisoformat(s['ts']) > datetime.fromisoformat(end['ts']):
                errors.append(f'action after its wake closed: {aid}')
        if hashlib.sha256(f'{s['wake_id']}:{s['action']['kind']}:{canonical(s['action']).decode()}'.encode()).hexdigest()[:16] != aid:
            errors.append(f'action id mismatch: {aid}')
        rs = resolved.get(aid, [])
        if len(rs) > 1:
            errors.append(f'duplicate action resolution: {aid}')
        if rs and (rs[0]['team'] != s['team'] or rs[0]['kind'] != s['action']['kind']):
            errors.append(f'action resolution inconsistent: {aid}')
        if not rs:
            if datetime.fromisoformat(s['resolves_at']) <= now and (not _seat_held(directory, 'draft')):
                errors.append(f'unresolved action: {aid}')
            else:
                pending += 1
    for aid in resolved.keys() - subs.keys():
        errors.append(f'action resolution without submission: {aid}')
    summary = {'wakes': len(opens), 'wake_records': len(recs), 'wakes_in_flight': len(in_flight), 'wakes_lost': len(lost), 'wakes_failed_infra': sum((len(v) for v in failed.values())), 'actions': len(subs), 'actions_resolved': sum((1 for a in subs if resolved.get(a))), 'actions_pending': pending}
    return (summary, errors)

def repair(league_id: str) -> dict:
    import fcntl
    import os
    import tempfile
    from contextlib import ExitStack
    directory = ROOT / 'leagues' / league_id
    with ExitStack() as stack:
        for name in ('run.lock', 'log.jsonl.lock', 'sessions.jsonl.lock'):
            lf = stack.enter_context(open(directory / name, 'a'))
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        evs, recs, errors = stored_decisions(league_id)
        expected = {e['decision_id']: {k: v for k, v in e.items() if k != 'seq'} for e in evs if e['type'] == 'decision'}
        allowed = {f'missing decision: {did}' for did in expected.keys() - recs.keys()}
        replacements = {}
        for did in expected.keys() & recs.keys():
            old = recs[did].get('decision')
            dec = expected[did]
            if isinstance(old, dict) and 'logged_at' not in old and ('logged_at' in dec):
                if canonical(dict(old, logged_at=dec['logged_at'])) == canonical(dec):
                    replacements[did] = dict(recs[did], decision=dec)
                    allowed.add(f'decision bytes differ: {did}')
        unexpected = [e for e in errors if e not in allowed]
        if unexpected:
            raise ValueError('Refusing non-legacy record drift: ' + '; '.join(unexpected))
        missing = expected.keys() - recs.keys()
        if not replacements and (not missing):
            return {'league': league_id, 'repaired': 0, 'backup': None}
        path = directory / 'sessions.jsonl'
        rows = [r['payload'] for r in ChainLog(path).rows()]
        rows = [replacements.get(r.get('decision_id'), r) if r.get('kind') == 'decision' else r for r in rows]
        if missing:
            lg = json.loads((directory / 'league.json').read_text())
            gw = GatewayIndex()
            ad = _lane_adapters()
            rows.extend((from_decision(dec, lg, gw, ad) for did, dec in expected.items() if did in missing))
        fd, temporary = tempfile.mkstemp(prefix='.sessions-repair-', dir=directory)
        os.close(fd)
        temp = Path(temporary)
        try:
            log = ChainLog(temp)
            for rec in rows:
                log.append(rec)
            assert log.verify()[0]
            backup_fd, backup = tempfile.mkstemp(prefix='sessions.jsonl.before-repair-', dir=directory)
            with os.fdopen(backup_fd, 'wb') as f:
                f.write(path.read_bytes() if path.exists() else b'')
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, path)
            dir_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            temp.unlink(missing_ok=True)
            temp.with_suffix(temp.suffix + '.lock').unlink(missing_ok=True)
        return {'league': league_id, 'repaired': len(replacements) + len(missing), 'backup': backup}

def proof(league_ids: list[str]) -> dict:
    from bm.engine.rules import load_rules
    from bm.engine.state import apply_event, genesis_state, state_hash
    from bm.league.context import engine_world
    world = engine_world()
    out = {}
    for lid in league_ids:
        lg = json.loads((ROOT / 'leagues' / lid / 'league.json').read_text())
        rules = load_rules(lg['rules_file'])
        evs, recs, errors = stored_decisions(lid)
        wakes, wake_errors = stored_wakes(lid)
        wake_out = {**wakes, 'wake_errors': len(wake_errors), 'wake_error_detail': wake_errors[:25]}
        if errors:
            out[lid] = {'events': len(evs), 'decisions': len(recs), 'events_byte_identical': 0, 'state_hash_mismatches': len(errors), 'final_state_hash': None, 'rebuilt_final_hash': None, 'stored_errors': errors, **wake_out}
            continue
        rebuilt = [dict(decision_event(recs[e['decision_id']]), seq=e['seq']) if e['type'] == 'decision' else e for e in evs]
        same_bytes = sum((canonical(a) == canonical(b) for a, b in zip(evs, rebuilt)))
        st_a = st_b = None
        mism = 0
        seen_ok = seen_n = 0
        history: set[str] = set()
        for a, b in zip(evs, rebuilt):
            if a['type'] == 'genesis':
                st_a = genesis_state(rules, a['league_id'], a['team_ids'], a['draft_order'], a['week'])
                st_b = genesis_state(rules, b['league_id'], b['team_ids'], b['draft_order'], b['week'])
                history.add(state_hash(st_b))
                continue
            if b['type'] == 'decision':
                seen_n += 1
                seen_ok += recs[b['decision_id']]['state_hash'] in history
            st_a, st_b = (apply_event(rules, world, st_a, a), apply_event(rules, world, st_b, b))
            history.add(state_hash(st_b))
            mism += state_hash(st_a) != state_hash(st_b)
        fails = {}
        for r in recs.values():
            if r['failure']:
                k = f'{r['failure']['layer']}:{r['failure']['code']}'
                fails[k] = fails.get(k, 0) + 1
        out[lid] = {'events': len(evs), 'decisions': len(recs), 'events_byte_identical': same_bytes, 'state_hash_mismatches': mism, 'final_state_hash': state_hash(st_a), 'rebuilt_final_hash': state_hash(st_b), 'decision_state_hash_carried': f'{seen_ok}/{seen_n}', 'turns': sum((len(r['turns']) for r in recs.values())), 'decisions_without_gateway_rows': sum((1 for r in recs.values() if r['decision'].get('model_call_ids') and (not r['turns']))), 'own': sum((r['outcome'] == 'own' for r in recs.values())), 'failures': fails, 'inworld_usd': round(sum((r['cost']['inworld_usd'] for r in recs.values())), 4), **cost_summary([r['cost'] for r in recs.values()]), **wake_out}
    return out

def main(argv: list[str] | None=None) -> int:
    import sys
    argv = argv if argv is not None else sys.argv[1:]
    if argv and argv[0] == 'proof':
        ids = argv[1:] or sorted((p.name for p in (ROOT / 'leagues').iterdir() if (p / 'log.jsonl').exists()))
        res = proof(ids)
        print(json.dumps(res, indent=1))
        return 0 if all((r['state_hash_mismatches'] == 0 and r['final_state_hash'] == r['rebuilt_final_hash'] and (r['events_byte_identical'] == r['events']) and (r['wake_errors'] == 0) for r in res.values())) else 1
    if argv and argv[0] == 'repair':
        if not argv[1:]:
            print('repair requires explicit league IDs')
            return 2
        for lid in argv[1:]:
            print(json.dumps(repair(lid)))
        return 0
    if argv and argv[0] == 'backfill':
        for lid in argv[1:]:
            log = store(lid)
            n = 0
            for rec in backfill(lid):
                write(rec, log)
                n += 1
            print(f'{lid}: {n} records -> {log.path}')
        return 0
    print('usage: python3 -m bm.records proof [league ...] | backfill league ... | repair league ...')
    return 2
if __name__ == '__main__':
    raise SystemExit(main())
