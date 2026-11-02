from __future__ import annotations
import hashlib
import fcntl
import json
import math
import sys
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from bm import records
from bm.agent.harness import extract_json
from bm.gateway.core import ModelRequest
from bm.picks import lines as L
from bm.score import picks as P
ROOT = Path(__file__).resolve().parents[2]
ET = ZoneInfo('America/New_York')
INSTANCES = 3
START_BANKROLL = 1000.0
MAX_STAKE_FRAC = 0.05
MAX_ATTEMPTS = 3
ATTEMPTS_PER_RUN = 2
SEATS_IN_FLIGHT = 6
PICKS_MODELS = ['opus-5.5', 'sonnet-5.5', 'sol-6.1', 'luna-5.6', 'gemini-3.1-pro', 'gemma-4-31b', 'nemotron-3-ultra', 'muse-spark-1.3', 'mimo-2.6-flash', 'grok-4.7', 'deepseek-v4-pro', 'kimi-k3', 'qwen-3.8-flash', 'muse-wonder', 'gemini-4-argon']
SYSTEM = 'You are a sports forecaster in the BenchedMark research series. You make your own forecasts for real NFL games that have not been played yet. Simulated books: no real money is wagered; your bankroll is in simulated dollars. Reply with ONE JSON object and nothing else.'

def store(slate: str):
    return records.store(kind='picks', key=slate)

def _now() -> datetime:
    return datetime.now(timezone.utc)

def _response_turns(response, gateway):
    routes = getattr(response, 'route_history', None)
    if not routes:
        fields = ('request_id', 'lane', 'model_requested', 'model_reported', 'usage', 'cost', 'latency_ms', 'raw_blob', 'attempts', 'error', 'error_detail', 'attempt_history', 'attempt_totals', 'identity_source', 'stop_reason', 'gateway_seq', 'prompt_blob', 'system_blob', 'text_blob')
        routes = [{key: getattr(response, key, None) for key in fields}]
    adapters = {name: lane.get('adapter') for name, lane in getattr(gateway, 'lanes', {}).items()}
    return [dict(records.turn(route, adapters), raw_blob=route.get('raw_blob'), gateway_log=route.get('gateway_log')) for route in routes]

def _age_s(at: datetime, snapshot: str | None) -> float | None:
    try:
        return (at - datetime.fromisoformat(snapshot)).total_seconds()
    except (TypeError, ValueError):
        return None

def receipt_status(row: dict, kickoff: str, receipt: dict | None=None) -> str:
    if receipt is not None and receipt.get('served_model_annotation'):
        from bm.ops.served_model import EXCLUDED_STATUS
        return EXCLUDED_STATUS
    if row.get('outcome') not in (None, 'own') or row.get('failure'):
        return 'excluded: failed attempt'
    if not row.get('received_at') and receipt is not None:
        if receipt.get('pick_record_id') != row.get('record_id') or not receipt.get('received_at') or (not receipt.get('prompt_matches')) or receipt.get('gateway_error'):
            return 'excluded: receipt does not match the record'
        if receipt.get('missing_games'):
            return 'excluded: incomplete slate answer'
        row = {**row, 'received_at': receipt['received_at'], 'deadline': receipt['deadline'], 'as_of': receipt['as_of']}
    try:
        received = datetime.fromisoformat(row['received_at'])
        deadline = datetime.fromisoformat(row['deadline'])
        asof = datetime.fromisoformat(row['as_of'])
        lock = datetime.fromisoformat(kickoff)
        if asof <= received < min(deadline, lock):
            return 'prospective receipt verified'
        return 'excluded: late or inconsistent receipt'
    except (KeyError, TypeError, ValueError):
        return 'legacy diagnostic: timeliness unverified'
_receipt_status = receipt_status

def _schedule_at(nv, at):
    from bm.league.context import SOURCE_PROVENANCE_FROM
    return nv.schedule(2026, as_of=at) if at >= SOURCE_PROVENANCE_FROM else nv.schedule(2026)

def slate_games(week: int, at: datetime) -> list[dict]:
    from bm.stats import nflverse as nv
    sch = _schedule_at(nv, at)
    lines = L.at(at, sch)
    return [dict(lines[g['game_id']], schedule=g) for g in sorted((g for g in sch if g['week'] == week), key=lambda g: (g['kickoff'], g['game_id'])) if g['game_id'] in lines and datetime.fromisoformat(g['kickoff']) > at]

def slate_exclusions(week: int, at: datetime, timing: str) -> list[dict]:
    from bm.stats import nflverse as nv
    reason = "kicked off at or before this slate's as_of; forecast in the week's early slate only" if timing == 'prelock' else "kicked off at or before this slate's as_of; not forecast in this slate"
    return [{'game_id': g['game_id'], 'kickoff': g['kickoff'], 'reason': reason} for g in sorted((g for g in _schedule_at(nv, at) if g['week'] == week), key=lambda g: (g['kickoff'], g['game_id'])) if datetime.fromisoformat(g['kickoff']) <= at]

def team_sheet(week: int, at: datetime) -> dict[str, dict]:
    from bm.stats import nflverse as nv
    sch = _schedule_at(nv, at)
    out = defaultdict(lambda: {'w': 0, 'l': 0, 't': 0, 'pf': 0, 'pa': 0, 'g': 0, 'injuries': []})
    for g in sch:
        if g['week'] < week and g.get('home_score') is not None and (datetime.fromisoformat(g['kickoff']) + timedelta(hours=4) <= at):
            for t, f, a in ((g['home'], g['home_score'], g['away_score']), (g['away'], g['away_score'], g['home_score'])):
                o = out[t]
                o['g'] += 1
                o['pf'] += f
                o['pa'] += a
                o['w' if f > a else 'l' if f < a else 't'] += 1
    for r in nv.injuries(2026, at, week):
        if r.get('report_status') in ('Out', 'Doubtful', 'Questionable') and r.get('position') in ('QB', 'RB', 'WR', 'TE'):
            out[r['team']]['injuries'].append(f'{r.get('full_name')} {r.get('position')} {r.get('report_status')}')
    return out

def blind_prompt(games: list[dict], sheet: dict) -> str:
    rows = []
    for g in games:
        h, a = (g['home'], g['away'])
        fmt = lambda t: f'{t} {sheet[t]['w']}-{sheet[t]['l']}' + (f'-{sheet[t]['t']}' if sheet[t]['t'] else '') + f', {sheet[t]['pf'] / max(sheet[t]['g'], 1):.1f} pts for / {sheet[t]['pa'] / max(sheet[t]['g'], 1):.1f} against per game' + (f'; injuries: {', '.join(sheet[t]['injuries'][:8])}' if sheet[t]['injuries'] else '')
        rows.append(f'- {g['game_id']}: {a} at {h}, kickoff {datetime.fromisoformat(g['kickoff']).astimezone(ET):%a %b %d %H:%M} ET\n    {fmt(a)}\n    {fmt(h)}')
    return 'STEP 1 (blind). Using only these stats and injury reports (no betting lines), for EACH game give: the median and 80% range (p10, p90) of total points; the median and 80% range of home margin (home minus away); P(home wins).\n' + '\n'.join(rows) + '\nReply: {"games": {"<game_id>": {"total": {"p10": x, "p50": x, "p90": x}, "margin": {"p10": x, "p50": x, "p90": x}, "p_home": p}, ...}}'

def line_prompt(games: list[dict], bankroll: float) -> str:
    rows = []
    for g in games:
        r = g['ref']
        sp, tt, ml = (r['spreads'], r['totals'], r['h2h'])
        rows.append(f'- {g['game_id']}: {g['away']} at {g['home']}: total {tt['over']['point']} (over {tt['over']['price']:+d} / under {tt['under']['price']:+d}); home {sp['home']['point']:+g} ({sp['home']['price']:+d}) / away {sp['away']['point']:+g} ({sp['away']['price']:+d}); moneyline home {ml['home']['price']:+d} / away {ml['away']['price']:+d}')
    cap = round(bankroll * MAX_STAKE_FRAC, 2)
    return f'STEP 2 (against the line). Lines at {games[0]['ref_book']}, snapshot {games[0]['snapshot']}:\n' + '\n'.join(rows) + f'\nBankroll: ${bankroll:.2f} of a $1,000 starting bankroll (simulated books; no real money is wagered). For each game give P(over), P(home covers), P(home wins), and optionally stakes in dollars at these prices: a stake of $0 is a pass; at most ${cap:.2f} per pick.\nReply: {{"games": {{"<game_id>": {{"p_over": p, "p_cover_home": p, "p_home": p, "stakes": [{{"market": "totals|spreads|h2h", "side": "over|under|home|away", "usd": x}}]}}, ...}}}}'

def attempts_of(log, seat_key: str) -> list[dict]:
    return [r['payload'] for r in log.rows() if r['payload'].get('kind') == 'pick' and (r['payload'].get('seat_key') or r['payload'].get('key')) == seat_key]

def completed_attempt(row: dict, games: list[dict], step: str, bankroll: float, receipt: dict | None=None) -> bool:
    if row.get('outcome') != 'own' or row.get('failure') or (not isinstance(row.get('answer'), dict)):
        return False
    return bool(_clean(row['answer'], games, step, bankroll)) and all((receipt_status(row, g['kickoff'], receipt) == 'prospective receipt verified' for g in games))

def retry_eligible(prior: list[dict], games: list[dict], step: str, bankroll: float, now: datetime, deadline: datetime) -> bool:
    if now >= deadline or any((completed_attempt(p, games, step, bankroll) for p in prior)):
        return False
    model_failures = sum((p.get('outcome') != 'infra_default' for p in prior))
    return model_failures < MAX_ATTEMPTS

def bankroll_of(slate_prefix: str, model: str, inst: int, as_of: datetime | None=None) -> float:
    b = START_BANKROLL
    latest = {}
    for f in sorted((ROOT / 'records' / 'picks').glob('*.jsonl')) if (ROOT / 'records' / 'picks').exists() else []:
        if f.stem.endswith('-scripted') != slate_prefix.endswith('-scripted') or not f.stem.startswith(slate_prefix.split('-w')[0] + '-w'):
            continue
        for r in records.ChainLog(f).rows():
            p = r['payload']
            if p.get('kind') in ('pick_settled', 'pick_settled_revision') and p.get('entrant') == model and (p.get('instance') == inst) and (as_of is None or datetime.fromisoformat(p['ts']) <= as_of):
                latest[p['key']] = p['profit'] if p.get('measurement_status') == 'prospective receipt verified' else 0.0
    b += sum(latest.values())
    return round(b, 2)
PROMPT_VERSIONS = ('picks-restart-1',)

def prompt_module(version: str | None):
    if version is None:
        return sys.modules[__name__]
    if version not in PROMPT_VERSIONS:
        raise ValueError(f'unknown Picks prompt version {version!r}')
    from bm.picks import prompts_restart
    if prompts_restart.PROMPT_VERSION != version:
        raise ValueError(f'prompt module is {prompts_restart.PROMPT_VERSION}, not {version}')
    return prompts_restart

def run_slate(week: int, timing: str='early', models: list[str] | None=None, gw=None, at: datetime | None=None, tag: str='', prompt_version: str | None=None, until: datetime | None=None) -> dict:
    slate = f'2026-w{week}-{timing}{tag}'
    log = store(slate)
    with open(log.path.with_suffix('.run.lock'), 'w') as lf:
        try:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'slate': slate, 'status': 'busy', 'calls': []}
        return _run_slate(week, timing, models, gw, at, tag, prompt_version, until)

def _run_slate(week: int, timing: str, models, gw, at, tag: str, prompt_version: str | None=None, until: datetime | None=None) -> dict:
    from bm.gateway.core import Gateway
    from bm.league.season import now_utc
    at = at or now_utc()
    gw = gw or Gateway()
    slate = f'2026-w{week}-{timing}{tag}'
    log = store(slate)
    prior_rows = [r['payload'] for r in log.rows()]
    contexts = [r for r in prior_rows if r.get('kind') == 'slate_context']
    if contexts:
        ctx = contexts[0]
        if prompt_version is not None and ctx.get('prompt_version') != prompt_version:
            return {'slate': slate, 'error': f'slate frozen with prompt version {ctx.get('prompt_version')}, not {prompt_version}', 'calls': []}
        at = datetime.fromisoformat(ctx['as_of'])
        games, sheet = (ctx['games'], ctx['team_sheet'])
    elif prior_rows:
        return {'slate': slate, 'error': 'legacy slate has no frozen input context; retained only', 'calls': []}
    else:
        games = slate_games(week, at)
        if until is not None:
            games = [g for g in games if datetime.fromisoformat(g['kickoff']) < until]
        pm0 = prompt_module(prompt_version)
        sheet = (pm0.build_sheet(week, at, games) if hasattr(pm0, 'build_sheet') else team_sheet(week, at)) if games else {}
    if not games:
        return {'error': f'no lined games for week {week} at {at}'}
    incomplete = [g['game_id'] for g in games if any((type((g.get('ref', {}).get(m, {}).get(s) or {}).get('price')) is not int or not (g.get('ref', {}).get(m, {}).get(s) or {}).get('price') for m, sides in (('h2h', ('home', 'away')), ('spreads', ('home', 'away')), ('totals', ('over', 'under'))) for s in sides))]
    incomplete += [g['game_id'] for g in games if any((not isinstance((g.get('ref', {}).get(m, {}).get(side) or {}).get('point'), (int, float)) or not math.isfinite(g['ref'][m][side]['point']) for m, sides in (('spreads', ('home', 'away')), ('totals', ('over', 'under'))) for side in sides))]
    if incomplete:
        return {'slate': slate, 'error': 'incomplete market input; no forecasts requested', 'games_missing_markets': incomplete, 'calls': []}
    if not contexts:
        from bm import succession
        models = succession.picks_models(list(models or PICKS_MODELS), at)
        frozen_prompts = {}
        pm = prompt_module(prompt_version)
        for model in models:
            for inst in range(1, INSTANCES + 1):
                bankroll = bankroll_of(slate, model, inst, at)
                frozen_prompts[f'{model}:i{inst}'] = {'bankroll': bankroll, 'open_stakes': open_stakes_of(slate, model, inst, at), 'blind': pm.blind_prompt(games, sheet), 'line': pm.line_prompt(games, bankroll)}
        versioned = {} if prompt_version is None else {'prompt_version': prompt_version, 'template_hash': pm.template_hash()}
        if until is not None:
            versioned['games_before'] = until.isoformat()
        if hasattr(pm, 'leak_scan'):
            found = sorted({f for fp in frozen_prompts.values() for f in pm.leak_scan(fp['blind'], at)})
            if found:
                return {'slate': slate, 'error': 'briefing leak_scan failed; no forecasts requested', 'findings': found[:20], 'calls': []}
        try:
            excluded = slate_exclusions(week, at, timing)
        except Exception as e:
            excluded = {'unmeasured': f'{type(e).__name__}'}
        context_record = records.make('slate_context', slate, _now().isoformat(), as_of=at.isoformat(), games=games, team_sheet=sheet, system=SYSTEM, excluded_games=excluded, models=models, instances=INSTANCES, seat_prompts=frozen_prompts, policy='prospective-picks/2; full slate; pre-first-kickoff receipt', **versioned)
        log.append(context_record)
        ctx = context_record
    from bm import succession
    models = succession.picks_models(list(models), at) if models else list(ctx['models'])
    if any((m not in ctx['models'] for m in models)):
        return {'slate': slate, 'error': 'models outside frozen slate cohort', 'calls': []}
    permit = ROOT / 'state/commissioner-picks-dispatch.json'
    if permit.exists() and (not tag):
        try:
            permits = json.loads(permit.read_text())
            if not isinstance(permits, dict):
                raise ValueError('authorization map required')
            if slate in permits:
                auth = permits[slate]
                if not isinstance(auth, dict) or not isinstance(auth.get('eligible_models'), list):
                    raise ValueError('matching authorization shape invalid')
                if any((not isinstance(m, str) or m not in ctx['models'] for m in auth['eligible_models'])):
                    raise ValueError('matching authorization outside frozen cohort')
                stamp = datetime.fromisoformat(auth['observed_at'])
                if timedelta(0) <= _now() - stamp <= timedelta(minutes=5):
                    models = [m for m in models if m in auth['eligible_models']]
        except (OSError, ValueError, KeyError, TypeError):
            return {'slate': slate, 'error': 'malformed commissioner recovery authorization', 'calls': []}
    out, lock = ([], threading.Lock())
    deadline = min(at + timedelta(hours=6), *(datetime.fromisoformat(g['kickoff']) for g in games))

    def one(model: str, inst: int):
        seat = ctx['seat_prompts'][f'{model}:i{inst}']
        for step in ('blind', 'line'):
            prompt = seat[step]
            seat_key = f'{slate}:{model}:i{inst}:{step}'
            for _ in range(ATTEMPTS_PER_RUN):
                prior = attempts_of(log, seat_key)
                if not retry_eligible(prior, games, step, seat['bankroll'], _now(), deadline):
                    break
                n = len(prior) + 1
                key = seat_key if n == 1 else f'{seat_key}:attempt{n}'
                started = _now()
                if started >= deadline:
                    with lock:
                        out.append({'model': model, 'instance': inst, 'step': step, 'outcome': 'closed', 'reason': 'slate deadline passed'})
                    break
                r = gw.call(ModelRequest(entrant_id=model, decision_id=f'picks:{key}', system=ctx['system'], prompt=prompt, max_output_tokens=16000, deadline=deadline))
                received = _now()
                d = extract_json(r.text) if not r.error else None
                parsed = (d or {}).get('games') if isinstance((d or {}).get('games'), dict) else None
                answer = _clean(parsed, games, step, seat['bankroll'], seat.get('open_stakes', 0.0)) if parsed else None
                error = r.error or ('TIMEOUT' if received >= deadline else None)
                detail = r.error_detail or ('response received after the slate deadline' if received >= deadline else '')
                valid = bool(answer) and (not error)
                turns = _response_turns(r, gw)
                evidence_complete = all((t.get('gateway_seq') is not None and all((t.get(k) for k in ('prompt_blob', 'system_blob', 'text_blob'))) for t in turns))
                rec = records.make('pick', key, received.isoformat(), entrant=model, instance=inst, step=step, timing=timing, week=week, slate=slate, seat_key=seat_key, attempt=n, retry_of=prior[-1]['record_id'] if prior else None, as_of=at.isoformat(), started_at=started.isoformat(), received_at=received.isoformat(), deadline=deadline.isoformat(), expected_games=[g['game_id'] for g in games], snapshot=games[0]['snapshot'], prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(), snapshot_age_s=_age_s(at, games[0].get('snapshot')), outcome='own' if valid else 'infra_default' if error else 'model_default', failure=records.failure(error or (None if valid else 'PARSE'), detail), request_id=r.request_id, served_model=r.model_reported, usage=r.usage, turns=turns, cost=records.cost_summary(turns), context_record_id=ctx.get('record_id'), source_clock_policy='frozen_slate_context', transport_evidence_coverage='complete' if evidence_complete else 'incomplete', **{'prompt_version': ctx['prompt_version']} if ctx.get('prompt_version') else {}, lines={g['game_id']: {k: g.get(k) for k in ('ref', 'ref_book', 'consensus', 'consensus_threshold_uniform', 'n_books', 'snapshot', 'market_provenance')} for g in games} if step == 'line' else None, answer=answer if valid else None)
                with lock:
                    records.write(rec, log)
                    out.append({'model': model, 'instance': inst, 'step': step, 'attempt': n, 'outcome': rec['outcome']})
    worker_errors = []

    def guarded_one(model, inst):
        try:
            one(model, inst)
        except Exception as exc:
            with lock:
                worker_errors.append({'model': model, 'instance': inst, 'error': type(exc).__name__, 'detail': str(exc)[:300]})
    with ThreadPoolExecutor(max_workers=SEATS_IN_FLIGHT) as pool:
        list(pool.map(lambda seat: guarded_one(*seat), [(m, i) for m in models for i in range(1, INSTANCES + 1)]))
    retained = [r['payload'] for r in log.rows()]
    complete = sum((any((completed_attempt(r, games, step, ctx['seat_prompts'][f'{m}:i{i}']['bankroll']) for r in retained if r.get('seat_key', r.get('key')) == f'{slate}:{m}:i{i}:{step}')) for m in ctx['models'] for i in range(1, INSTANCES + 1) for step in ('blind', 'line')))
    expected = len(ctx['models']) * INSTANCES * 2
    return {'slate': slate, 'games': len(games), 'calls': out, 'worker_errors': worker_errors, 'expected_seat_steps': expected, 'complete_seat_steps': complete, 'status': 'complete' if complete == expected and (not worker_errors) else 'incomplete'}

def _clean(parsed: dict, games: list[dict], step: str, bankroll: float, open_stakes: float=0.0) -> dict:
    ok = {}
    number = lambda x: isinstance(x, (int, float)) and (not isinstance(x, bool)) and math.isfinite(x)
    prob = lambda x: number(x) and 0 <= x <= 1
    cap = max(0.0, bankroll * MAX_STAKE_FRAC)
    for g in games:
        a = parsed.get(g['game_id'])
        if not isinstance(a, dict):
            return {}
        if step == 'blind':
            if not prob(a.get('p_home')):
                return {}
            for k in ('total', 'margin'):
                q = a.get(k)
                if not isinstance(q, dict) or not all((number(q.get(p)) for p in ('p10', 'p50', 'p90'))):
                    return {}
                if not q['p10'] <= q['p50'] <= q['p90']:
                    return {}
            ok[g['game_id']] = {k: {p: a[k][p] for p in ('p10', 'p50', 'p90')} for k in ('total', 'margin')} | {'p_home': a['p_home']}
        else:
            if not all((prob(a.get(k)) for k in ('p_over', 'p_cover_home', 'p_home'))):
                return {}
            st = []
            seen = set()
            stakes = a.get('stakes', [])
            if not isinstance(stakes, list):
                return {}
            for s in stakes:
                if not isinstance(s, dict) or s.get('market') not in ('totals', 'spreads', 'h2h') or (not number(s.get('usd'))) or (s['usd'] < 0):
                    return {}
                sides = ('over', 'under') if s['market'] == 'totals' else ('home', 'away')
                if s.get('side') not in sides:
                    return {}
                if s['usd'] > 0:
                    stake_key = (s['market'], s['side'])
                    if stake_key in seen:
                        return {}
                    seen.add(stake_key)
                    if round(min(float(s['usd']), cap), 2) > 0:
                        st.append({'market': s['market'], 'side': s.get('side'), 'usd': round(min(float(s['usd']), cap), 2), 'capped': s['usd'] > cap})
            ok[g['game_id']] = {k: a[k] for k in ('p_over', 'p_cover_home', 'p_home') if prob(a.get(k))} | {'stakes': st}
    if step == 'line':
        room = max(0.0, round(bankroll - open_stakes, 2))
        total = sum((x['usd'] for v in ok.values() for x in v['stakes']))
        if total > room + 1e-09:
            f = room / total if total else 0.0
            for v in ok.values():
                for x in v['stakes']:
                    x['usd'] = math.floor(x['usd'] * f * 100) / 100
                    x['capped'] = True
                v['stakes'] = [x for x in v['stakes'] if x['usd'] > 0]
    return ok

def open_stakes_of(slate: str, model: str, inst: int, as_of: datetime) -> float:
    total = 0.0
    d = ROOT / 'records' / 'picks'
    for f in sorted(d.glob('*.jsonl')) if d.exists() else []:
        if f.stem == slate or f.stem.endswith('-scripted') != slate.endswith('-scripted'):
            continue
        rows = [r['payload'] for r in records.ChainLog(f).rows()]
        settled = {r['key'] for r in rows if r.get('kind') in ('pick_settled', 'pick_settled_revision') and datetime.fromisoformat(r['ts']) <= as_of}
        for r in rows:
            if r.get('kind') != 'pick' or r.get('step') != 'line' or r.get('entrant') != model or (r.get('instance') != inst) or (r.get('outcome') != 'own') or (not isinstance(r.get('answer'), dict)) or (datetime.fromisoformat(r.get('received_at') or r['ts']) > as_of):
                continue
            for gid, a in r['answer'].items():
                for x in a.get('stakes', []):
                    if f'{r['key']}:{gid}:{x['market']}:{x['side']}' not in settled:
                        total += x['usd']
    return round(total, 2)

def settle(week: int, scripted: bool=False) -> dict:
    from bm.stats import nflverse as nv
    score_source = {}
    sch = nv.schedule(2026, source_receipt=score_source)
    observed_at = _now()
    by_id = {g['game_id']: g for g in sch}
    out = defaultdict(list)
    for f in sorted((ROOT / 'records' / 'picks').glob(f'2026-w{week}-*.jsonl')):
        if f.stem.endswith('-scripted') != scripted:
            continue
        log = records.ChainLog(f)
        rows = [r['payload'] for r in log.rows()]
        done = {r['key']: r for r in rows if r.get('kind') in ('pick_settled', 'pick_settled_revision')}
        from bm.picks.receipts import receipts_by_record
        receipts = receipts_by_record(rows)
        seen_keys = set()
        for r in rows:
            if r.get('kind') != 'pick' or r.get('step') != 'line' or (not r.get('answer')):
                continue
            for gid, a in r['answer'].items():
                g = by_id.get(gid)
                if not g or g.get('home_score') is None or g.get('away_score') is None:
                    continue
                cl = L.close(gid, sch)
                ref = r['lines'][gid]['ref']
                for s in a.get('stakes', []):
                    key = f'{r['key']}:{gid}:{s['market']}:{s['side']}'
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                    side = {'totals': ref['totals'].get(s['side']), 'spreads': ref['spreads'].get(s['side']), 'h2h': ref['h2h'].get(s['side'])}[s['market']]
                    if not side:
                        continue
                    pick = {'market': s['market'], 'side': s['side'], 'line': side.get('point'), 'price': side['price'], 'stake': s['usd']}
                    res = P.settle(pick, {'home_score': g['home_score'], 'away_score': g['away_score']})
                    close_line = (cl or {}).get('consensus', {}).get('total' if s['market'] == 'totals' else 'spread_home')
                    if s['market'] == 'spreads' and s['side'] == 'away' and (close_line is not None):
                        close_line = -close_line
                    comparable = s['market'] == 'h2h' or (close_line is not None and close_line == pick['line'] and (cl or {}).get('consensus_threshold_uniform', {}).get(s['market'], False))
                    p_close = _close_prob(cl, s['market'], s['side']) if cl and comparable else None
                    p_close_status = 'same proposition' if p_close is not None else 'unavailable: missing close or changed/mixed threshold'
                    if p_close is None and cl and (s['market'] != 'h2h') and (pick['line'] is not None):
                        from bm.score import picks_clv
                        home_t = -pick['line'] if s['market'] == 'spreads' and s['side'] == 'away' else pick['line']
                        ph, how = picks_clv.close_prob(cl, s['market'], home_t)
                        if ph is not None:
                            p_close = ph if s['side'] in ('home', 'over') else 1 - ph
                            p_close_status = 'close books at the taken number' if how == 'exact' else f'translated to the taken number (normal, SD {picks_clv.SIGMA[s['market']]} pts)'
                    prob = {'totals': a.get('p_over'), 'spreads': a.get('p_cover_home'), 'h2h': a.get('p_home')}[s['market']]
                    if s['side'] in ('under', 'away') and prob is not None:
                        prob = 1 - prob
                    row = {'key': key, 'entrant': r['entrant'], 'instance': r['instance'], 'week': week, 'game_id': gid, **pick, **res, 'score_provenance': {'source_snapshot': score_source or None, 'observed_scores': {'home_score': g['home_score'], 'away_score': g['away_score']}, 'observed_at': observed_at.isoformat(), 'source_age_s': _age_s(observed_at, score_source.get('retrieved_at')), 'source_status': 'exact consumed schedule snapshot retained' if all((score_source.get(k) for k in ('asset', 'path', 'sha256', 'retrieved_at'))) else 'missing/incomplete schedule source receipt', 'finality_status': 'score observed; finality not certified by schedule schema'}, 'p_model': prob, 'close_line': close_line, 'measurement_status': receipt_status(r, g['kickoff'], receipts.get(r.get('record_id'))), 'clv_points': P.clv_points(pick['line'], close_line, s['side']) if s['market'] != 'h2h' and close_line is not None else None, 'probability_clv_status': p_close_status, 'p_close_novig': p_close, 'clv_prob': None if p_close is None else round(P.clv_prob(p_close, side['price']), 4), 'close_snapshot_age_s': _age_s(datetime.fromisoformat(g['kickoff']), (cl or {}).get('snapshot')), 'close_stale': (_age_s(datetime.fromisoformat(g['kickoff']), (cl or {}).get('snapshot')) or 1000000000.0) > 30 * 60, 'close_quality': 'last observed pre-kickoff quote; actual final closing quote not certified', 'close_snapshot': (cl or {}).get('snapshot'), 'pick_reference_book': r['lines'][gid].get('ref_book'), 'pick_market_provenance': r['lines'][gid].get('market_provenance'), 'close_market_provenance': (cl or {}).get('market_provenance'), 'close_provenance_status': 'missing last-observed quote' if not cl else 'retained last-observed source quotes; actual close unverified' if cl.get('market_provenance') else 'legacy: source quote provenance missing', 'market_provenance_status': 'retained source quotes' if r['lines'][gid].get('market_provenance') else 'legacy diagnostic: source quote provenance missing'}
                    out[r['entrant']].append(row)
                    if key not in done:
                        log.append(records.make('pick_settled', key, datetime.now(ET).isoformat(), **{k: v for k, v in row.items() if k != 'key'}))
                        done[key] = row
                    elif any((done[key].get(k) != row.get(k) for k in ('outcome', 'profit', 'p_model', 'p_close_novig', 'clv_prob', 'close_snapshot', 'measurement_status'))):
                        revision = records.make('pick_settled_revision', key, datetime.now(ET).isoformat(), supersedes_record_id=done[key].get('record_id'), **{k: v for k, v in row.items() if k != 'key'})
                        log.append(revision)
                        done[key] = revision
    summary = {}
    for m, rows in out.items():
        clv = [x['clv_prob'] for x in rows if x['clv_prob'] is not None]
        summary[m] = {'prospective_picks': sum((x['measurement_status'] == 'prospective receipt verified' for x in rows)), 'measurement_status': 'deployment diagnostic; only receipt-verified picks are prospective', 'picks': len(rows), 'profit': round(sum((x['profit'] for x in rows)), 2), 'mean_clv_prob': round(sum(clv) / len(clv), 4) if clv else None, 'beat_close': round(sum((1 for c in clv if c > 0)) / len(clv), 3) if clv else None}
    return summary

def _close_prob(cl: dict, market: str, side: str) -> float | None:
    c = cl['consensus']
    if market == 'h2h':
        return c['p_home'] if side == 'home' else None if c['p_home'] is None else 1 - c['p_home']
    if market == 'spreads':
        return c['p_cover_home'] if side == 'home' else None if c['p_cover_home'] is None else 1 - c['p_cover_home']
    return c['p_over'] if side == 'over' else None if c['p_over'] is None else 1 - c['p_over']

class ScriptedPicker:
    lanes: dict = {}

    def call(self, req):
        import re
        import uuid
        from bm.gateway.core import ModelResponse
        ids = re.findall('- (2026_\\d\\d_\\w+_\\w+):', req.prompt)
        if 'STEP 1' in req.prompt:
            ans = {'games': {g: {'total': {'p10': 34, 'p50': 44, 'p90': 55}, 'margin': {'p10': -10, 'p50': 2, 'p90': 14}, 'p_home': 0.55} for g in ids}}
        else:
            ans = {'games': {g: {'p_over': 0.5, 'p_cover_home': 0.53, 'p_home': 0.56, 'stakes': [{'market': 'spreads', 'side': 'home', 'usd': 10}]} for g in ids}}
        return ModelResponse(uuid.uuid4().hex, req.entrant_id, 'scripted', 'scripted', 'scripted', 'scripted-picker', json.dumps(ans), {'kind': 'none'}, {'input': len(req.prompt) // 3, 'output': 400}, {'billed_usd': 0.0}, 5, None)

def main(argv: list[str] | None=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if argv[:1] == ['slate']:
        week = int(argv[1])
        timing = argv[argv.index('--timing') + 1] if '--timing' in argv else 'early'
        models = argv[argv.index('--models') + 1].split(',') if '--models' in argv else None
        at = datetime.fromisoformat(argv[argv.index('--at') + 1]) if '--at' in argv else None
        if '--scripted' not in argv and at is not None and (at.tzinfo is None or _now() >= at + timedelta(hours=6)):
            print(json.dumps({'slate': f'2026-w{week}-{timing}', 'status': 'closed gap', 'error': 'original scheduled window expired or clock invalid; no dispatch', 'calls': []}))
            return 4
        gw = ScriptedPicker() if '--scripted' in argv else None
        if gw is None:
            from bm.cli import capacity_gate
            from bm.gateway.core import Gateway
            from bm.ops import capacity
            gw = Gateway()
            from bm import succession
            at0 = datetime.fromisoformat(argv[argv.index('--at') + 1]) if '--at' in argv else None
            if not capacity_gate(capacity.picks_work(succession.picks_models(models or PICKS_MODELS, at0), INSTANCES), f'picks slate w{week} {timing}', gw, force='--force' in argv):
                return 3
        result = run_slate(week, timing, models, gw, at, tag='-scripted' if '--scripted' in argv else '', prompt_version=argv[argv.index('--prompt') + 1] if '--prompt' in argv else None, until=datetime.fromisoformat(argv[argv.index('--until') + 1]) if '--until' in argv else None)
        print(json.dumps(result, indent=1, default=str))
        return 0 if result.get('status') == 'complete' and (not result.get('error')) else 4
    elif argv[:1] == ['settle']:
        print(json.dumps(settle(int(argv[1]), scripted='--scripted' in argv), indent=1))
    return 0
if __name__ == '__main__':
    raise SystemExit(main())
