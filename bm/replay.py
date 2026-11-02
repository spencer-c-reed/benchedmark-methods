from __future__ import annotations
import hashlib
import json
import random
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
import yaml
from bm import records
from bm.agent.harness import extract_json
from bm.gateway.core import ModelRequest
from bm.log import ChainLog, get_blob
ROOT = Path(__file__).resolve().parents[1]
RUNNER = 'panel-v2'
KINDS = {'start_sit': 10, 'waiver_bid': 10, 'trade_response': 10, 'draft_pick': 10}
CANARIES = ROOT / 'config' / 'canaries.yaml'
TOKEN = re.compile('^(\\s+)(?:[A-Z0-9]+\\s+)?(\\S+ .+ (?:QB|RB|WR|TE|K|DEF) \\S+ .*wk=.*)$')

def frozen_states(n_per_kind: dict | None=None, seed: str='panel-v1', leagues: list[str] | None=None) -> list[dict]:
    n_per_kind = n_per_kind or KINDS
    gw = records.GatewayIndex()
    pool = defaultdict(list)
    for d in sorted((ROOT / 'leagues').iterdir()):
        if not (d / 'log.jsonl').exists() or d.name.startswith(('smoke', 'bookproof')) or (leagues and d.name not in leagues):
            continue
        for e in ChainLog(d / 'log.jsonl').payloads():
            if e['type'] != 'decision' or e.get('forced_default') or e.get('kind') not in n_per_kind:
                continue
            calls = [c for c in gw.calls(e.get('model_call_ids') or []) if not c.get('error') and c.get('prompt_blob')]
            if not calls:
                continue
            c = calls[0]
            pool[e['kind']].append({'id': e['decision_id'], 'kind': e['kind'], 'league': e['league'], 'team': e['team'], 'week': e['week'], 'entrant': e['entrant'], 'system_blob': c['system_blob'], 'prompt_blob': c['prompt_blob'], 'original_action': e.get('action'), 'rendered_prompt_hash': e.get('rendered_prompt_hash')})
    out = []
    for k, n in n_per_kind.items():
        xs = sorted(pool[k], key=lambda s: hashlib.sha256(f'{seed}:{s['id']}'.encode()).hexdigest())
        out += xs[:n]
    return out

def transform(prompt: str, variant: str, seed: str) -> str:
    if variant == 'P0':
        return prompt
    lines = prompt.split('\n')
    if variant == 'O1':
        out, block = ([], [])

        def flush():
            if block:
                rng = random.Random(hashlib.sha256((seed + ''.join(block)).encode()).hexdigest())
                b = block[:]
                rng.shuffle(b)
                out.extend(b)
                block.clear()
        for ln in lines:
            if TOKEN.match(ln):
                block.append(ln)
            else:
                flush()
                out.append(ln)
        flush()
        return '\n'.join(out)
    if variant == 'F1':
        out = []
        for ln in lines:
            m = re.match('^(\\s+)(?:([A-Z]+\\d*|BN)\\s+)?(00-\\d+|DEF-\\w+)\\s+(.+?)\\s+(QB|RB|WR|TE|K|DEF)\\s+(\\S+)\\s+(.*)$', ln)
            if m and 'wk=' in ln:
                ind, slot, pid, name, pos, team, rest = m.groups()
                out.append(f'{ind}- {(slot + ': ' if slot else '')}{name} ({pos}, {team}): id {pid}; {rest}')
            else:
                out.append(ln)
        return '\n'.join(out)
    if variant in ('P1', 'P2', 'P3'):
        raise ValueError('P1-P3 rewrite the system prompt: use transform_system()')
    raise ValueError(f'unknown variant {variant}')
REWRITES = ROOT / 'config' / 'panel-rewrites.yaml'
PASSAGES = {'P1': ('a private 10-team research league', 'Nobody will answer questions.'), 'P2': ('DATA\n', 'bye=team has no game this week.'), 'P3': ('- One public league chat post', 'the others are AI models.')}

def _passage(system: str, v: str) -> str | None:
    a, b = PASSAGES[v]
    i = system.find(a)
    j = system.find(b, i)
    return system[i:j + len(b)] if i >= 0 and j >= 0 else None

def transform_system(system: str, variant: str) -> str:
    if variant in ('P0', 'O1', 'F1'):
        return system
    rws = (yaml.safe_load(REWRITES.read_text()) or {}).get(variant) if REWRITES.exists() else None
    old = _passage(system, variant)
    rw = next((r for r in rws or [] if r['original'] == old), None)
    if not rw or not rw.get('checked'):
        raise ValueError(f"{variant}: no person-checked rewrite of this prompt's passage in {REWRITES.name}")
    return system.replace(old, rw['rewrite'], 1)

def _save_rewrites(out: dict) -> None:
    REWRITES.write_text('# P1-P3 robustness-panel rewrites (§4.4). A person compares each rewrite with the original and sets\n# checked: true (and checked_by) only if the meaning is identical. Unchecked rewrites never run.\n' + yaml.safe_dump(out, sort_keys=False, width=140, allow_unicode=True))

def draft_rewrites(writer: str, gw=None) -> dict:
    from bm.gateway.core import Gateway
    gw = gw or Gateway()
    passages = {v: sorted({_passage(get_blob(st['system_blob']).decode(), v) for st in frozen_states()} - {None}) for v in ('P1', 'P2', 'P3')}
    out = {}
    for v, olds in passages.items():
        out[v] = []
        for old in olds:
            r = gw.call(ModelRequest(entrant_id=writer, decision_id=f'panel-rewrite:{v}', system='You rewrite text. Reply with the rewritten text only.', prompt="Rewrite the passage below so that it means exactly the same thing: same facts, same rules, same numbers, same instructions, same names and ids, nothing added or removed. Change the wording and sentence structure only. Keep it about the same length. Keep any line that starts with '- ' as a line starting with '- '.\n\nPASSAGE:\n" + old, max_output_tokens=4000))
            out[v].append({'original': old, 'rewrite': (r.text or '').strip(), 'writer': writer, 'served_model': r.model_reported, 'error': r.error, 'checked': False, 'checked_by': None})
            _save_rewrites(out)
    _save_rewrites(out)
    return {v: [{'chars': len(x['rewrite']), 'error': x['error']} for x in xs] for v, xs in out.items()}

def decision_key(kind: str, action: dict | None) -> str | None:
    if not isinstance(action, dict):
        return None
    if kind == 'draft_pick':
        return action.get('player_id')
    if kind == 'start_sit':
        lu = action.get('lineup') or {}
        return ','.join(sorted((str(v) for v in lu.values() if v)))
    if kind == 'waiver_bid':
        return ','.join(sorted((str(b.get('add')) for b in action.get('bids') or [] if isinstance(b, dict))))
    if kind == 'trade_response':
        return action.get('response')
    return json.dumps(action, sort_keys=True)[:200]

def _first_option(prompt: str) -> str | None:
    m = re.search('BEST AVAILABLE[^\\n]*\\n\\s+(?:- [^:]+: id )?(00-\\d+|DEF-\\w+)', prompt)
    return m.group(1) if m else None

def first_listed(kind: str, prompt: str, action: dict | None) -> bool | None:
    if kind != 'draft_pick' or not isinstance(action, dict):
        return None
    f = _first_option(prompt)
    return None if not f else f == action.get('player_id')

def replay(state: dict, entrant: str, variant: str, sample: int, gw, allowed_pricing: tuple | None=None, deadline: datetime | None=None) -> dict:
    system = transform_system(get_blob(state['system_blob']).decode(), variant)
    prompt = get_blob(state['prompt_blob']).decode()
    prompt = prompt if variant.startswith('P') else transform(prompt, variant, f'{state['id']}:{variant}')
    key = f'{state['id']}:{entrant}:{variant}:s{sample}'
    r = gw.call(ModelRequest(entrant_id=entrant, decision_id=f'replay:{key}', system=system, prompt=prompt, max_output_tokens=16000, allowed_pricing=allowed_pricing, deadline=deadline))
    d = extract_json(r.text) if not r.error else None
    turns = 1
    if d is not None and 'tool_calls' in d and ('action' not in d):
        r = gw.call(ModelRequest(entrant_id=entrant, decision_id=f'replay:{key}:final', system=system, prompt=prompt + '\n\nResearch is closed for this decision. Give your final JSON answer now.', max_output_tokens=16000, allowed_pricing=allowed_pricing, deadline=deadline))
        d = extract_json(r.text) if not r.error else None
        turns = 2
    act = (d or {}).get('action')
    return {'key': key, 'state': state['id'], 'decision_kind': state['kind'], 'entrant': entrant, 'variant': variant, 'sample': sample, 'error': r.error, 'served_model': r.model_reported, 'decision': decision_key(state['kind'], act), 'first_listed': first_listed(state['kind'], prompt, act), 'first_option': _first_option(prompt), 'request_id': r.request_id, 'turns': turns, 'runner': RUNNER, 'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest()}

def sample_numbers(samples: int, sample_start: int=1) -> range:
    if samples < 1:
        raise ValueError('samples must be >= 1')
    if sample_start < 1:
        raise ValueError('sample_start must be >= 1')
    return range(sample_start, sample_start + samples)

def panel(entrant: str, variants=('P0', 'O1', 'F1'), samples: int=2, states: list[dict] | None=None, gw=None, sample_start: int=1) -> dict:
    from bm.gateway.core import Gateway
    gw = gw or Gateway()
    states = states if states is not None else frozen_states()
    log = records.store(kind='replay', key=f'panel-{entrant}-{RUNNER}')
    latest = {}
    from bm.ops.served_model import ROW_KIND, annotated_records
    rows_ = [rec['payload'] for rec in log.rows()]
    misserved = annotated_records(rows_)
    for payload in rows_:
        if payload.get('key') and payload.get('kind') != ROW_KIND and (payload.get('record_id') not in misserved):
            latest[payload['key']] = payload
    done = {key for key, payload in latest.items() if not payload.get('error')}
    for st in states:
        for v in variants:
            for s in sample_numbers(samples, sample_start):
                k = f'{st['id']}:{entrant}:{v}:s{s}'
                if k in done:
                    continue
                row = replay(st, entrant, v, s, gw)
                records.write(records.make('panel_call', k, datetime.now(timezone.utc).isoformat(), **{x: y for x, y in row.items() if x != 'key'}), log)
                latest[k] = dict(row, kind_of_record='panel_call')
                if row.get('error') in STOP_ON:
                    print(f'panel {entrant}: stopped at {k} on {row['error']} (quota/cap); resume later', flush=True)
                    return dict(measures(list(latest.values())), stopped_on=row['error'], stopped_at=k)
    return measures(list(latest.values()))
STOP_ON = ('QUOTA_YIELD', 'SELF_CAP', 'LANE_DISABLED')

def measures(rows: list[dict]) -> dict:
    by = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if r.get('decision') is not None:
            by[r['state']][r['variant']].append(r['decision'])
    within, across = ([], [])
    for st, vs in by.items():
        for v, ds in vs.items():
            if len(ds) >= 2:
                within.append(ds[0] == ds[1])
        p0 = vs.get('P0', [])
        for v, ds in vs.items():
            if v != 'P0' and p0 and ds:
                across.append(ds[0] == p0[0])
    fl = defaultdict(list)
    for r in rows:
        if r.get('first_listed') is not None:
            fl[r['variant']].append(r['first_listed'])
    p0 = {r['state']: r['decision'] for r in rows if r['variant'] == 'P0' and r.get('sample') == 1 and r.get('decision')}
    expected = [p0[r['state']] == r.get('first_option') for r in rows if r['variant'] == 'O1' and r.get('first_listed') is not None and (r['state'] in p0)]
    rate = lambda xs: round(sum(xs) / len(xs), 3) if xs else None
    bias = round(100 * (rate(fl['O1']) - rate(expected)), 1) if len(fl.get('O1', [])) >= 10 and len(expected) >= 10 else None
    return {'states': len(by), 'calls': len(rows), 'errors': sum((1 for r in rows if r.get('error'))), 'within_variant_agreement': rate(within), 'across_variant_agreement_vs_P0': rate(across), 'first_listed_share': {v: rate(x) for v, x in fl.items()}, 'first_listed_expected_if_order_blind': rate(expected), 'position_bias_pts': bias, 'position_bias_flag': bias is not None and bias > 10, 'n_position': len(fl.get('O1', [])), 'undecided': sum((1 for r in rows if r.get('decision') is None)), 'n_within': len(within), 'n_across': len(across), 'served_models': dict(Counter((r.get('served_model') for r in rows)))}

def kendall_tau(a: list[str], b: list[str]) -> float:
    pos = {x: i for i, x in enumerate(b)}
    xs = [x for x in a if x in pos]
    n = len(xs)
    if n < 2:
        return 1.0
    c = d = 0
    for i in range(n):
        for j in range(i + 1, n):
            c, d = (c + 1, d) if pos[xs[i]] < pos[xs[j]] else (c, d + 1)
    return round((c - d) / (n * (n - 1) / 2), 4)

def gate(variance_share: float, tau: float) -> dict:
    ok = variance_share < 0.25 and tau >= 0.8
    return {'ranked': ok, 'label': 'ranked' if ok else 'wording-sensitive', 'variance_share': variance_share, 'tau': tau}

def canary_set(n: int=10) -> list[dict]:
    if CANARIES.exists():
        return yaml.safe_load(CANARIES.read_text())['states']
    sts = frozen_states({'start_sit': 4, 'draft_pick': 3, 'waiver_bid': 2, 'trade_response': 1}, seed='canary-v1')[:n]
    body = {'version': 'canary-v1', 'frozen_at': datetime.now(timezone.utc).isoformat(), 'states': sts, 'sha256': hashlib.sha256(json.dumps(sts, sort_keys=True).encode()).hexdigest()}
    CANARIES.write_text('# Canary states (D-CAL-7): frozen; a change is a new version, never an edit.\n' + yaml.safe_dump(body, sort_keys=False, width=140))
    return sts
UNPAID = ('free', 'subscription')

def canary_plan(seats: list[str], entrants: dict, lanes: dict, cadence: str) -> dict:

    def pricings(e):
        ent = entrants.get(e) or {}
        routes = [ent.get('lane')] + [f.get('lane') for f in (ent.get('spread') or []) + (ent.get('fallback') or [])]
        return {(lanes.get(ln) or {}).get('pricing') for ln in routes if ln}
    if cadence == 'all':
        return {e: None for e in seats}
    unpaid = [e for e in seats if pricings(e) & set(UNPAID)]
    if cadence == 'daily':
        return {e: UNPAID for e in unpaid}
    if cadence == 'paid':
        return {e: None for e in seats if e not in unpaid}
    raise ValueError(f'unknown canary cadence {cadence!r}')

def canary(entrant: str, gw=None, stop_at: datetime | None=None, allowed_pricing: tuple | None=None) -> dict:
    from bm.gateway.core import Gateway
    gw = gw or Gateway()
    log = records.store(kind='canary', key=entrant)
    rows_ = [r['payload'] for r in log.rows()]
    from bm.ops.served_model import annotated_records
    misserved = annotated_records(rows_)
    prior = [p for p in rows_ if p.get('kind', 'canary') == 'canary' and p.get('record_id') not in misserved]
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M')
    rows = []
    states = canary_set()
    for st in states:
        if stop_at and datetime.now(timezone.utc) >= stop_at:
            break
        row = replay(st, entrant, 'P0', 1, gw, allowed_pricing, deadline=stop_at)
        rows.append(row)
        records.write(records.make('canary', f'{run_id}:{st['id']}', datetime.now(timezone.utc).isoformat(), run=run_id, **{x: y for x, y in row.items() if x != 'key'}), log)
        if row.get('error') in STOP_ON:
            break
    return dict({'entrant': entrant, 'run': run_id, 'states': len(rows), 'states_skipped_for_time': len(states) - len(rows), 'errors': sum((1 for r in rows if r['error']))}, **canary_verdict(entrant, prior, rows))
DRIFT = 0.6

def _answered(r: dict) -> bool:
    return not r.get('error') and r.get('decision') is not None

def _ids(rows: list[dict]) -> set[str]:
    return {r['served_model'].removesuffix(':free') for r in rows if r.get('served_model')}

def canary_verdict(entrant: str, prior: list[dict], rows: list[dict]) -> dict:
    base = {}
    for p in prior:
        if _answered(p):
            base.setdefault(p['state'], p)

    def agreement(rs):
        same = [r['decision'] == base[r['state']]['decision'] for r in rs if _answered(r) and r['state'] in base and (base[r['state']] is not r)]
        return (round(sum(same) / len(same), 3) if same else None, len(same))
    agree, n = agreement(rows)
    runs = {}
    for p in prior:
        runs.setdefault(p.get('run'), []).append(p)
    earlier = [a for a, k in (agreement(rs) for rs in runs.values()) if a is not None and k >= 3]
    floor = min(earlier) if earlier else None
    ids = _ids(rows)
    last_ids = next((_ids(runs[r]) for r in sorted(runs, key=str, reverse=True) if _ids(runs[r])), set())
    out = {'agreement_with_first_run': agree, 'states_compared': n, 'resampling_floor': floor, 'drifted': agree is not None and agree < min(DRIFT, floor if floor is not None else DRIFT), 'served_ids': sorted(ids), 'served_id_unknown': not ids, 'served_id_changed': bool(ids) and bool(last_ids) and (ids != last_ids)}
    if out['served_id_changed']:
        out['env_event'] = {'type': 'env', 'entrant': entrant, 'change': 'served model id', 'from': sorted(last_ids), 'to': sorted(ids)}
    return out

def main(argv: list[str] | None=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    n = int(argv[argv.index('--n') + 1]) if '--n' in argv else 40
    per = {k: max(1, n * v // 40) for k, v in KINDS.items()}
    if argv[:1] == ['states']:
        sts = frozen_states(per)
        print(json.dumps({'n': len(sts), 'by_kind': dict(Counter((s['kind'] for s in sts))), 'by_entrant': dict(Counter((s['entrant'] for s in sts)))}, indent=1))
    elif argv[:1] == ['panel']:
        vs = argv[argv.index('--variants') + 1].split(',') if '--variants' in argv else ['P0', 'O1', 'F1']
        smp = int(argv[argv.index('--samples') + 1]) if '--samples' in argv else 2
        sample_start = int(argv[argv.index('--sample-start') + 1]) if '--sample-start' in argv else 1
        print(json.dumps(panel(argv[1], vs, smp, frozen_states(per), sample_start=sample_start), indent=1))
    elif argv[:1] == ['rewrites']:
        print(json.dumps(draft_rewrites(argv[1]), indent=1))
    elif argv[:1] == ['canary']:
        print(json.dumps(canary(argv[1]), indent=1))
    elif argv[:1] == ['canary-all']:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from bm.gateway.core import Gateway
        from bm.ops.probe import model_seats
        gw = Gateway()
        stop_at = datetime.now(timezone.utc) + timedelta(hours=3, minutes=30)
        cadence = 'daily' if '--daily' in argv else 'paid' if '--paid' in argv else 'all'
        plan = canary_plan(list(model_seats()), gw.entrants, gw.lanes, cadence)
        print(json.dumps({'cadence': cadence, 'plan': {e: list(p) if p else 'unrestricted' for e, p in plan.items()}}), flush=True)
        out = {}
        with ThreadPoolExecutor(max_workers=max(1, len(plan))) as pool:
            futs = {pool.submit(canary, e, gw, stop_at, p): e for e, p in plan.items()}
            for f in as_completed(futs):
                e = futs[f]
                try:
                    out[e] = f.result()
                except Exception as ex:
                    out[e] = {'error': str(ex)[:200]}
                print(json.dumps({'at': datetime.now(timezone.utc).isoformat(), 'entrant': e, 'result': out[e]}), flush=True)
        changed = [e for e, r in out.items() if r.get('served_id_changed')]
        drift = [e for e, r in out.items() if r.get('drifted')]
        if changed or drift:
            from bm.ops.alert import alert
            alert('canary', f'BenchedMark canaries: served model changed for {changed or 'none'}; answers drifted for {drift or 'none'} (D-CAL-7: env event, row splits)')
        print(json.dumps(out, indent=1))
    return 0
if __name__ == '__main__':
    raise SystemExit(main())
