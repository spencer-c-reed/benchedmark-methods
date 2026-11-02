from __future__ import annotations
import json
import math
import subprocess
import sys
from collections import defaultdict
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from bm.paths import runtime_root
ROOT = runtime_root(Path(__file__).resolve().parents[2])
SAFETY = 1.5
DEFAULT_CALLS = 3.0
DEFAULT_USD_PER_CALL = 0.05
LOOKBACK_DAYS = 7
MIN_OBS = 3
TIMEOUT_HEADROOM = 0.8
TIMEOUT_CUT = 0.95
GATEWAY_TIMEOUT_S = 300
HANG_MIN_OK = 30
TOKEN_FIELDS = ('input', 'cached_input', 'output')
_GATEWAY_SCAN_CONTEXT: ContextVar[dict | None] = ContextVar('capacity_gateway_scan', default=None)

def kind_of(decision_id: str) -> str:
    d = decision_id or ''
    if d.startswith('picks:'):
        return 'picks'
    if d.startswith('probe:'):
        return 'probe'
    if ':draft:' in d:
        return 'draft'
    for k in ('lineup', 'waivers', 'offer', 'trade', 'book'):
        if f':{k}' in d or f'-{k}' in d:
            return k
    return 'other'

def cap_kind(lane: dict) -> str:
    k = lane.get('daily_call_cap_kind')
    if k:
        return str(k)
    if lane.get('pricing') in ('paid', 'sponsor'):
        return 'pacing' if lane.get('budget_usd') is not None and lane.get('model_daily_usd') else 'money'
    return 'pacing'

def _gateway_observations(log_path: Path | None=None, days: int=LOOKBACK_DAYS, now: datetime | None=None, *, need_observed: bool=True, need_latency: bool=True) -> tuple[dict, dict]:
    log_path = log_path or ROOT / 'logs' / 'gateway.jsonl'
    since = ((now or datetime.now(timezone.utc)) - timedelta(days=days)).isoformat()
    per_dec: dict = {} if need_observed else None
    lat_out: dict = {} if need_latency else None
    if not log_path.exists():
        return ({}, {})
    with open(log_path) as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            r = r.get('payload', r)
            if r.get('type') != 'model_call' or (r.get('ts') or '') < since:
                continue
            if need_observed and r.get('decision_id'):
                k = kind_of(r['decision_id'])
                if k != 'probe':
                    d = per_dec.setdefault(r['decision_id'], {'entrant': r.get('entrant'), 'kind': k, 'calls': defaultdict(int), 'usd': defaultdict(float), 'tok': {}})
                    d['calls'][r.get('lane')] += int(r.get('attempts') or 0)
                    tot = r.get('attempt_totals') if isinstance(r.get('attempt_totals'), dict) else {}
                    billed = tot.get('billed_usd') if tot.get('billed_usd') is not None else (r.get('cost') or {}).get('billed_usd')
                    lm = (r.get('lane'), r.get('model_requested'))
                    d['usd'][lm] += float(billed or 0)
                    use = tot.get('usage') if isinstance(tot.get('usage'), dict) else r.get('usage') or {}
                    t = d['tok'].setdefault(lm, defaultdict(float))
                    for x in TOKEN_FIELDS:
                        t[x] += float(use.get(x) or 0)
            if need_latency and r.get('lane'):
                hist = r.get('attempt_history') if isinstance(r.get('attempt_history'), list) and r.get('attempt_history') else [r]
                model = r.get('model_requested') or '?'
                for a in hist:
                    ms = a.get('latency_ms') if isinstance(a, dict) else None
                    if not isinstance(ms, (int, float)):
                        continue
                    o = lat_out.setdefault(r['lane'], {'ok': [], 'kinds': defaultdict(list), 'models': defaultdict(list), 'timeouts': [], 'model_timeouts': defaultdict(list)})
                    if a.get('error') is None:
                        o['ok'].append(ms)
                        o['kinds'][kind_of(r.get('decision_id') or '')].append(ms)
                        o['models'][model].append(ms)
                    elif a.get('error') == 'TIMEOUT':
                        o['timeouts'].append(ms)
                        o['model_timeouts'][model].append(ms)
    obs_out: dict = {}
    if need_observed:
        for d in per_dec.values():
            for key in ((d['entrant'], d['kind']), (d['entrant'], '*')):
                o = obs_out.setdefault(key, {'n': 0, 'calls': defaultdict(float), 'usd': defaultdict(float), 'tok': {}})
                o['n'] += 1
                for l, c in d['calls'].items():
                    o['calls'][l] += c
                for lm, u in d['usd'].items():
                    o['usd'][lm] += u
                for lm, t in d['tok'].items():
                    ot = o['tok'].setdefault(lm, defaultdict(float))
                    for x, v in t.items():
                        ot[x] += v
    return (obs_out, lat_out or {})

def _gateway_reader(log_path: Path | None, days: int, now: datetime | None, *, need_observed: bool, need_latency: bool) -> tuple[dict, dict]:
    shared = _GATEWAY_SCAN_CONTEXT.get()
    if shared is not None and log_path is None and (days == LOOKBACK_DAYS) and (now is None):
        pair = shared.get('pair')
        if pair is None:
            pair = _gateway_observations(need_observed=True, need_latency=True)
            shared['pair'] = pair
        return pair
    return _gateway_observations(log_path, days, now, need_observed=need_observed, need_latency=need_latency)

def observed(log_path: Path | None=None, days: int=LOOKBACK_DAYS, now: datetime | None=None) -> dict:
    return _gateway_reader(log_path, days, now, need_observed=True, need_latency=False)[0]

def latency_observed(log_path: Path | None=None, days: int=LOOKBACK_DAYS, now: datetime | None=None) -> dict:
    return _gateway_reader(log_path, days, now, need_observed=False, need_latency=True)[1]

def _pct(xs: list, q: float) -> float:
    s = sorted(xs)
    return s[max(0, min(len(s) - 1, math.ceil(q * len(s)) - 1))]

def lane_timeout_s(lane: dict, model: str | None=None) -> float:
    from bm.gateway.core import model_timeout_s
    return model_timeout_s(lane, model, GATEWAY_TIMEOUT_S) if model else float(lane.get('timeout_s', GATEWAY_TIMEOUT_S))

def _hang_timeout_s(mx_ok: float, headroom: float=TIMEOUT_HEADROOM) -> int:
    return int(math.ceil(mx_ok / headroom / 60.0) * 60)

def _model_rows(lane: dict, o: dict):
    mt = o.get('model_timeouts') or {}
    for m in sorted(set(o.get('models') or {}) | set(mt)):
        yield (m, lane_timeout_s(lane, m), [x / 1000 for x in (o.get('models') or {}).get(m, [])], [x / 1000 for x in mt.get(m, [])])

def timeout_check(lanes: dict, lat: dict, names=None, headroom: float=TIMEOUT_HEADROOM) -> tuple[list, list]:
    lines, flags = ([], [])
    for l in sorted(names if names is not None else lanes):
        lane = lanes.get(l)
        o = lat.get(l)
        if not lane or lane.get('enabled') is False or (not o) or (not (o['ok'] or o['timeouts'])):
            continue
        t = lane_timeout_s(lane)
        if o['ok']:
            mx, p99, p50 = (max(o['ok']) / 1000, _pct(o['ok'], 0.99) / 1000, _pct(o['ok'], 0.5) / 1000)
            head = f'{l}: timeout_s {t:.0f}; ok calls {len(o['ok'])}: p50 {p50:.0f}s p99 {p99:.0f}s max {mx:.0f}s ({mx / t:.0%} of timeout)'
        else:
            head = f'{l}: timeout_s {t:.0f}; no successful call in {LOOKBACK_DAYS} days'
        if o['timeouts']:
            cut = [x for x in o['timeouts'] if x / 1000 >= TIMEOUT_CUT * t]
            head += f'; TIMEOUTs {len(o['timeouts'])} (longest {max(o['timeouts']) / 1000:.0f}s, {len(cut)} at the current cap)'
        kinds = '; '.join((f'{k} n={len(v)} p99 {_pct(v, 0.99) / 1000:.0f}s max {max(v) / 1000:.0f}s' for k, v in sorted(o['kinds'].items(), key=lambda kv: -max(kv[1]))))
        per = list(_model_rows(lane, o))
        models = '; '.join((f'{m} n={len(ok)}' + (f' p99 {_pct(ok, 0.99):.0f}s max {max(ok):.0f}s' if ok else '') + (f' timeout_s {mt_:.0f}' if mt_ != t else '') + (f' no-answer {len(no)}' if no else '') for m, mt_, ok, no in sorted(per, key=lambda r: -max(r[2] or [0])))) if len(per) > 1 else ''
        lines.append(head + (f'\n    by kind: {kinds}' if kinds else '') + (f'\n    by model: {models}' if models else ''))
        why = []
        for m, mt_, ok, no in per:
            if not ok or max(ok) < headroom * mt_:
                continue
            slow = max(o['kinds'].items(), key=lambda kv: max(kv[1]))[0] if o['kinds'] else '?'
            cut = [x for x in no if x >= TIMEOUT_CUT * mt_]
            why.append((f'{m}: ' if len(per) > 1 else '') + f'max successful call {max(ok):.0f}s is {max(ok) / mt_:.0%} of timeout_s {mt_:.0f} (p99 {_pct(ok, 0.99):.0f}s, slowest kind {slow})' + (f'; {len(cut)} TIMEOUT(s) ran >= {TIMEOUT_CUT:.0%} of it without an answer (longest {max(cut):.0f}s), likely cut-off answers' if cut else ''))
        if why:
            flags.append(f'{l}: ' + '; '.join(why))
    return (lines, flags)

def hang_check(lanes: dict, lat: dict, names=None, headroom: float=TIMEOUT_HEADROOM) -> list:
    out = []
    for l in sorted(names if names is not None else lanes):
        lane = lanes.get(l)
        o = lat.get(l)
        if not lane or lane.get('enabled') is False or (not o):
            continue
        for m, t, ok, no in _model_rows(lane, o):
            hung = [x for x in no if x >= TIMEOUT_CUT * t]
            if not hung or (ok and max(ok) >= headroom * t):
                continue
            if ok:
                rec = _hang_timeout_s(max(ok), headroom)
                tail = f'; its {len(ok)} answered attempts: max {max(ok):.0f}s, p99 {_pct(ok, 0.99):.0f}s. Suggest model_timeout_s {{{m}: {rec}}} on {l} (slowest answer at {headroom:.0%}) so retries come sooner' + (f' (small sample: {len(ok)} answers < {HANG_MIN_OK}; recheck before applying)' if len(ok) < HANG_MIN_OK else '') if rec < t else f'; its answers max {max(ok):.0f}s'
            else:
                tail = f'; no answered attempt in {LOOKBACK_DAYS} days to size a timeout from'
            out.append(f'{l} {m}: {len(hung)} no-answer attempt(s) ran into timeout_s {t:.0f} ({', '.join((f'{x:.0f}s' for x in sorted(hung, reverse=True)))})' + tail)
    return out

def session_lanes(work, ents: dict, extra=()) -> set:
    out = set(extra)
    for ent, _k, n in work:
        e = ents.get(ent) or {}
        if n <= 0 or e.get('kind') == 'baseline':
            continue
        out |= {x for x in [e.get('lane')] + [f.get('lane') for k in ('spread', 'fallback') for f in e.get(k) or [] if isinstance(f, dict)] if x}
    return out

def alert_timeouts(rep: 'Report', label: str) -> bool:
    if not rep.timeout_flags and (not rep.hang_flags):
        return False
    from bm.ops.alert import wake
    if not rep.timeout_flags:
        return False
    lanes_ = sorted((f.split(':', 1)[0] for f in rep.timeout_flags))
    return wake('timeout|' + '|'.join(lanes_), f'BenchedMark timeout preflight ({label}): lane timeout_s at or under real successful latency, so good answers get cut off, retried and can end as infra defaults.\n' + '\n'.join(rep.timeout_flags) + '\nNothing was changed. Raise timeout_s in config/lanes.yaml (applies without a restart) above the observed max with room, keeping it under the decision window.' + ("\nHANGS (lower that model's timeout instead):\n" + '\n'.join(rep.hang_flags) if rep.hang_flags else ''))

def route_models(ent: dict) -> dict:
    out: dict = {}
    for ln, m in [(ent.get('lane'), ent.get('model'))] + [(f.get('lane'), f.get('model')) for k in ('spread', 'fallback') for f in ent.get(k) or [] if isinstance(f, dict)]:
        if ln and m and (m not in out.setdefault(ln, [])):
            out[ln].append(m)
    return out

def list_usd(model: str, usage: dict) -> float | None:
    from bm.gateway import core
    usd = core.usd_eq(model, usage)
    if usd is None:
        return None
    return usd * float((core._PRICES or {}).get(model, {}).get('peak_multiplier') or 1)

def priced_on_route(usd: dict, tok: dict, ent: dict) -> dict:
    routes = route_models(ent)
    out: dict = defaultdict(float)
    for (ln, m), u in usd.items():
        cur = routes.get(ln) or []
        if not cur or m in cur:
            out[ln, m] += u
            continue
        t = tok.get((ln, m))
        p = list_usd(cur[0], {'input': t.get('input', 0), 'cached_input': t.get('cached_input', 0), 'output': t.get('output', 0)}) if t and any(t.values()) else None
        out[ln, cur[0]] += u if p is None else p
    return dict(out)

def per_decision(entrant: str, kind: str, obs: dict, ents: dict) -> tuple[dict, dict]:
    o = obs.get((entrant, kind))
    if not o or o['n'] < MIN_OBS:
        o = obs.get((entrant, '*')) if (obs.get((entrant, '*')) or {}).get('n', 0) >= MIN_OBS else o
    if o and o['n']:
        usd = priced_on_route(o['usd'], o.get('tok') or {}, ents.get(entrant) or {})
        return ({l: c / o['n'] for l, c in o['calls'].items() if l}, {lm: u / o['n'] for lm, u in usd.items() if lm[0]})
    e = ents.get(entrant) or {}
    if not e.get('lane'):
        return ({}, {})
    return ({e['lane']: DEFAULT_CALLS}, {})

@dataclass
class Report:
    label: str
    need_calls: dict = field(default_factory=dict)
    need_usd: dict = field(default_factory=dict)
    raised: list = field(default_factory=list)
    short: list = field(default_factory=list)
    lines: list = field(default_factory=list)
    timeout_lines: list = field(default_factory=list)
    timeout_flags: list = field(default_factory=list)
    hang_flags: list = field(default_factory=list)
    odds_flags: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.short

    def summary(self) -> str:
        head = f'BenchedMark capacity preflight ({self.label}): ' + ('OK' if self.ok else 'SHORT')
        parts = [head]
        if self.raised:
            parts.append('raised for today (our pacing caps): ' + '; '.join(self.raised))
        if self.short:
            parts.append('NOT ENOUGH HEADROOM (provider/money caps, never raised automatically): ' + '; '.join(self.short))
        if self.timeout_flags:
            parts.append('TIMEOUT RISK (config not changed): ' + '; '.join(self.timeout_flags))
        if self.odds_flags:
            parts.append('ODDS PLAN (lines for the Picks CLV endpoint; not blocking): ' + '; '.join(self.odds_flags))
        if self.hang_flags:
            parts.append("HANGS (config not changed; lower that model's timeout so retries come sooner): " + '; '.join(self.hang_flags))
        return '\n'.join(parts)

def preflight(work: list[tuple[str, str, int]], label: str, *, apply: bool=True, gw=None, obs: dict | None=None, safety: float=SAFETY, lat: dict | None=None, day: str | None=None) -> Report:
    if obs is None and lat is None:
        token = _GATEWAY_SCAN_CONTEXT.set({})
        try:
            obs, lat = (observed(), latency_observed())
        finally:
            _GATEWAY_SCAN_CONTEXT.reset(token)
    rep = _preflight(work, label, apply=apply, gw=gw, obs=obs, safety=safety, day=day)
    try:
        from bm.gateway.core import load_yaml
        lanes = gw.lanes if gw is not None else {l['lane']: l for l in load_yaml('lanes.yaml')}
        ents = gw.entrants if gw is not None else {e['id']: e for e in load_yaml('entrants.yaml')}
        names = session_lanes(work, ents, extra=rep.need_calls)
        lat = latency_observed() if lat is None else lat
        rep.timeout_lines, rep.timeout_flags = timeout_check(lanes, lat, names)
        rep.hang_flags = hang_check(lanes, lat, names)
        rep.lines += rep.timeout_lines
    except Exception as e:
        rep.lines.append(f'timeout check failed: {e}')
    if label.startswith('picks'):
        try:
            from datetime import datetime, timezone
            from bm.stats import market
            lines, rep.odds_flags = market.odds_preflight_lines(datetime.now(timezone.utc))
            rep.lines += lines
        except Exception as e:
            rep.lines.append(f'odds budget check failed: {type(e).__name__}')
    return rep

def alert_odds(rep: 'Report', label: str) -> bool:
    if not rep.odds_flags:
        return False
    from bm.ops.alert import wake
    return wake('odds-plan|' + label.split()[0], f'BenchedMark Picks preflight ({label}): the odds pull plan is short.\n' + '\n'.join(rep.odds_flags))

def _claude_credit_estimate(model: str, entrant: str, kind: str) -> float:
    from bm.gateway.adapters import AnthropicAPI
    from bm.gateway.core import ModelRequest
    from bm.picks.run import SYSTEM
    req = ModelRequest(entrant_id=entrant, decision_id=f'{kind}:capacity-preflight', system=SYSTEM if kind == 'picks' else '', prompt='', max_output_tokens=16000)
    return AnthropicAPI()._est(model, req)

def _claude_route(lane: dict, model: str, entrant: str, kind: str, calls: int, ledger, credit_need: dict) -> str:
    from bm.gateway import adapters
    from bm.gateway.core import Gateway, GatewayError
    guard = Gateway.__new__(Gateway)
    guard.ledger = ledger
    Gateway.spend_guard(guard, lane, model)
    Gateway.money_guard(guard, lane, model)
    if lane.get('adapter') == 'claude_cli' and lane.get('yield_at_weekly_pct'):
        cache = Path.home() / '.cache/status/claude-weekly.json'
        quota = json.loads(cache.read_text())
        utilization = quota.get('utilization')
        if isinstance(utilization, bool) or not isinstance(utilization, (int, float)) or (not math.isfinite(utilization)) or (not 0 <= utilization <= 1):
            raise ValueError('quota utilization unknown')
        adapters.ClaudeCLI().preflight(lane, model)
        age = datetime.now(timezone.utc) - datetime.fromisoformat(quota['observedAt'])
        if not timedelta(0) <= age <= timedelta(minutes=30):
            raise ValueError('under-cap quota observation stale')
        return f'Claude weekly utilization {utilization:.0%} below yield {lane['yield_at_weekly_pct']}%'
    if lane.get('adapter') == 'anthropic_api':
        who = lane.get('credits_consumer', 'benchedmark')
        need = _claude_credit_estimate(model, entrant, kind) * calls
        total = credit_need.get(who, 0.0) + need
        result = subprocess.run([str(adapters.CREDITS_LEDGER), 'can-spend', who, f'{total:.4f}'], capture_output=True, text=True, timeout=10)
        if result.returncode == 75:
            raise GatewayError('QUOTA_YIELD', f'Claude API credit allocation refused ~${total:.2f} for {who}')
        if result.returncode:
            raise ValueError(f'Claude API credit check failed (exit {result.returncode})')
        return f'Claude API credit allocation available for ~${total:.2f} ({who})'
    return 'configured route guards passed; inference untested'

def _claude_capacity(ent: dict, kind: str, n: int, cpd: dict, upd: dict, lanes: dict, ledger, need_c: dict, need_u: dict, credit_need: dict, rep: Report, safety: float, day: str | None=None):
    from bm.gateway.core import Gateway, GatewayError, model_daily_cap
    routes = [(ent.get('lane'), ent.get('model'))] + [(f['lane'], f['model']) for key in ('spread', 'fallback') for f in ent.get(key) or []]
    reasons = []
    used, overrides = (ledger.today(), ledger.cap_overrides())
    for ln, model in dict.fromkeys(routes):
        lane = lanes.get(ln)
        calls = cpd.get(ln) or DEFAULT_CALLS
        projected = math.ceil(n * calls * safety)
        usd = upd.get((ln, model), calls * DEFAULT_USD_PER_CALL if (lane or {}).get('pricing') in ('paid', 'sponsor') else 0)
        try:
            if not lane:
                raise GatewayError('LANE_DISABLED', 'missing configured lane')
            detail = _claude_route(lane, model, ent['id'], kind, projected, ledger, credit_need)
            if cap_kind(lane) != 'pacing':
                left = max(int(lane.get('daily_call_cap', 0)), overrides.get(ln, 0)) - used.get(ln, 0)
                if math.ceil(need_c.get(ln, 0) + n * calls * safety) > left:
                    raise GatewayError('SELF_CAP', f'{cap_kind(lane)} call cap cannot carry projected session')
            for key, cap, spent, need in (('model_daily_usd', model_daily_cap(lane, model, day), ledger.spent(f'{ln}:{model}', day), need_u.get((ln, model), 0) + n * usd * safety), ('budget_usd', lane.get('budget_usd'), ledger.spent(ln, 'ALL'), sum((v for (l, _), v in need_u.items() if l == ln)) + n * usd * safety)):
                if cap is not None and need > float(cap) - spent:
                    raise GatewayError('SELF_CAP', f'money cap {key} cannot carry projected session')
        except GatewayError as exc:
            reasons.append(f'{ln} {model}: {exc.code} ({exc.detail})')
            if exc.code not in Gateway.FALLBACK_ON:
                break
        except Exception as exc:
            reasons.append(f'{ln} {model}: capacity unknown ({type(exc).__name__}: {exc})')
        else:
            role = 'primary' if ln == ent.get('lane') else 'fallback'
            rep.lines.append(f'{ent['id']}: OK via {role} {ln} {model} ({detail})' + ('; unavailable: ' + '; '.join(reasons) if reasons else ''))
            if lane.get('adapter') == 'anthropic_api':
                who = lane.get('credits_consumer', 'benchedmark')
                credit_need[who] = credit_need.get(who, 0.0) + _claude_credit_estimate(model, ent['id'], kind) * projected
            return ({ln: calls}, {(ln, model): usd} if usd else {})
    msg = f'{ent['id']}: no usable configured fallback; ' + '; '.join(reasons)
    rep.lines.append(msg)
    rep.short.append(msg)
    return None

def _preflight(work: list[tuple[str, str, int]], label: str, *, apply: bool=True, gw=None, obs: dict | None=None, safety: float=SAFETY, day: str | None=None) -> Report:
    from bm.gateway.core import Ledger, load_yaml, model_daily_cap
    lanes = gw.lanes if gw is not None else {l['lane']: l for l in load_yaml('lanes.yaml')}
    ents = gw.entrants if gw is not None else {e['id']: e for e in load_yaml('entrants.yaml')}
    ledger = gw.ledger if gw is not None else Ledger()
    obs = observed() if obs is None else obs
    rep = Report(label)
    need_c: dict = defaultdict(float)
    need_u: dict = defaultdict(float)
    credit_need: dict = {}
    primary_of: dict = defaultdict(set)
    for ent, kind, n in work:
        if n <= 0 or (ents.get(ent) or {}).get('kind') == 'baseline' or ent not in ents:
            continue
        cpd, upd = per_decision(ent, kind, obs, ents)
        lane = lanes.get((ents[ent] or {}).get('lane')) or {}
        routes = session_lanes([(ent, kind, n)], ents)
        if kind == 'picks' and any(((lanes.get(l) or {}).get('adapter') in ('claude_cli', 'anthropic_api') for l in routes)):
            selected = _claude_capacity(ents[ent], kind, n, cpd, upd, lanes, ledger, need_c, need_u, credit_need, rep, safety, day)
            if selected is None:
                continue
            cpd, upd = selected
            lane = lanes.get(next(iter(cpd))) or {}
        for l, c in cpd.items():
            need_c[l] += n * c * safety
            if l == (ents[ent] or {}).get('lane'):
                primary_of[l].add(ent)
        for lm, u in upd.items():
            need_u[lm] += n * u * safety
        if lane.get('pricing') in ('paid', 'sponsor') and (not any((lm[0] == lane['lane'] for lm in upd))):
            need_u[lane['lane'], ents[ent]['model']] += n * cpd.get(lane['lane'], DEFAULT_CALLS) * DEFAULT_USD_PER_CALL * safety
    rep.need_calls = {l: math.ceil(v) for l, v in need_c.items()}
    rep.need_usd = {lm: round(v, 4) for lm, v in need_u.items()}
    used = ledger.today()
    ovr = ledger.cap_overrides()
    for l, need in sorted(rep.need_calls.items()):
        lane = lanes.get(l)
        if not lane or lane.get('enabled') is False:
            continue
        cap = int(lane.get('daily_call_cap', 0))
        eff = max(cap, ovr.get(l, 0))
        left = eff - used.get(l, 0)
        rep.lines.append(f'{l}: need ~{need} calls, {left} left of {eff}/day ({used.get(l, 0)} used)')
        if need <= left:
            continue
        kind = cap_kind(lane)
        if kind == 'pacing' and str(label).startswith(('replay', 'canary', 'panel')):
            rep.short.append(f'{l}: offline work needs ~{need} calls, {left} left of {eff}/day; cap NOT raised for offline work')
            continue
        if kind == 'pacing':
            new = used.get(l, 0) + need
            msg = f'{l} daily_call_cap {eff} -> {new} (need ~{need}, {left} left)'
            if apply:
                ledger.set_cap_override(l, new, f'capacity preflight {label}: {msg}')
            rep.raised.append(msg + ('' if apply else ' [dry run]'))
        elif lane.get('pricing') == 'free' and (not primary_of.get(l)):
            rep.lines.append(f'{l}: fallback only for this session; ~{need} projected, {left} left (not blocking)')
        else:
            rep.short.append(f'{l} ({kind} call cap): need ~{need} calls, only {left} left of {eff}/day')
    by_lane: dict = defaultdict(float)
    for (l, m), need in sorted(rep.need_usd.items()):
        lane = lanes.get(l) or {}
        by_lane[l] += need
        cap = model_daily_cap(lane, m, day)
        if cap is not None:
            left = float(cap) - ledger.spent(f'{l}:{m}', day)
            on = f' on {day}' if day else ''
            rep.lines.append(f'{l} {m}: need ~${need:.2f}, ${left:.2f} left of ${float(cap):.2f}/day{on}')
            if need > left:
                rep.short.append(f'{l} {m} money cap model_daily_usd: need ~${need:.2f}, only ${max(left, 0):.2f} left of ${float(cap):.2f}/day{on}')
    for l, need in sorted(by_lane.items()):
        b = (lanes.get(l) or {}).get('budget_usd')
        if b is not None:
            left = float(b) - ledger.spent(l, 'ALL')
            rep.lines.append(f'{l}: need ~${need:.2f}, ${left:.2f} left of lifetime ${float(b):.2f}')
            if need > left:
                rep.short.append(f'{l} money cap budget_usd: need ~${need:.2f}, only ${max(left, 0):.2f} left of lifetime ${float(b):.2f}')
    return rep

def draft_work(league_ids: list[str]) -> list[tuple[str, str, int]]:
    from bm.league.season import LEAGUES, League
    out = []
    for i in league_ids:
        if not (LEAGUES / i / 'league.json').exists():
            continue
        lg = League(i)
        if lg.done('draft'):
            continue
        made = defaultdict(int)
        for e in lg.events():
            if e['type'] == 'draft_pick':
                made[e['team']] += 1
        for tid in lg.seats:
            if not lg.is_bot(tid):
                out.append((lg.entrant(tid), 'draft', max(0, lg.rules.roster_size - made[tid])))
    return out

def config_draft_work(prefix: str='nfl-l') -> list[tuple[str, str, int]]:
    from bm.cli import league_cfgs
    from bm.engine.rules import load_rules
    out = []
    for l in league_cfgs():
        if not l['id'].startswith(prefix):
            continue
        size = load_rules(l.get('rules_file', 'rules/nfl-s1.yaml')).roster_size
        out += [(e, 'draft', size) for e in l.get('seats') or []]
    return out

def steps_work(leagues, now: datetime, horizon: timedelta=timedelta(minutes=60)) -> list[tuple[str, str, int]]:
    from bm.league.season import week_steps
    kinds = {'lineup': 'lineup', 'waiver_bids': 'waivers', 'trade': 'offer'}
    out = []
    for lg in leagues:
        if not lg.done('draft'):
            continue
        try:
            week = lg.state()['week']
            steps = week_steps(week)
        except Exception:
            continue
        for key, due in steps:
            k = kinds.get(key.split(':')[0])
            if k and (not lg.done(key)) and (due <= now + horizon):
                w = int(key.split(':')[1])
                out += [(lg.entrant(t, w), k, 1) for t in lg.seats if not lg.is_bot(t, w)]
    return out

def picks_work(models: list[str], instances: int, steps: int=2) -> list[tuple[str, str, int]]:
    return [(m, 'picks', instances * steps) for m in models]

def replay_work(entrant: str, n: int=40, variants: tuple[str, ...]=('P0', 'O1', 'F1'), samples: int=2, sample_start: int=1) -> list[tuple[str, str, int]]:
    from bm import records
    from bm.replay import KINDS, RUNNER, frozen_states, sample_numbers
    per = {k: max(1, n * v // 40) for k, v in KINDS.items()}
    states = frozen_states(per)
    log = records.store(kind='replay', key=f'panel-{entrant}-{RUNNER}')
    done = {r['payload'].get('key') for r in log.rows()}
    pending = 0
    for st in states:
        for variant in variants:
            for sample in sample_numbers(samples, sample_start):
                key = f'{st['id']}:{entrant}:{variant}:s{sample}'
                if key not in done:
                    pending += 1
    return [(entrant, '*', pending)]

def main(argv: list[str]) -> int:
    only = [argv[i + 1] for i, a in enumerate(argv) if a == '--only']
    if argv[:1] == ['draft']:
        from bm.cli import league_ids
        rep = preflight(draft_work(league_ids(only or None)), 'draft (dry run)', apply='--apply' in argv)
    elif argv[:1] == ['season-draft']:
        rep = preflight(config_draft_work(), 'season draft (dry run)', apply='--apply' in argv)
    elif argv[:1] == ['replay'] and len(argv) >= 2:
        entrants = tuple((x for x in argv[1].split(',') if x))
        n = int(argv[argv.index('--n') + 1]) if '--n' in argv else 40
        variants = tuple(argv[argv.index('--variants') + 1].split(',')) if '--variants' in argv else ('P0', 'O1', 'F1')
        samples = int(argv[argv.index('--samples') + 1]) if '--samples' in argv else 2
        sample_start = int(argv[argv.index('--sample-start') + 1]) if '--sample-start' in argv else 1
        work = []
        for entrant in entrants:
            work += replay_work(entrant, n, variants, samples, sample_start)
        label = f'replay {','.join(entrants)} n={n} variants={','.join(variants)} samples={sample_start}-{sample_start + samples - 1}'
        rep = preflight(work, label, apply='--apply' in argv)
    elif argv[:1] == ['timeouts']:
        from bm.gateway.core import load_yaml
        lanes, lat = ({l['lane']: l for l in load_yaml('lanes.yaml')}, latency_observed())
        lines, flags = timeout_check(lanes, lat)
        hangs = hang_check(lanes, lat)
        print('\n'.join(lines))
        print('TIMEOUT RISK:\n' + '\n'.join(flags) if flags else 'timeouts OK')
        if hangs:
            print("HANGS (lower that model's timeout so retries come sooner):\n" + '\n'.join(hangs))
        return 4 if flags else 0
    else:
        print(__doc__)
        return 2
    print('\n'.join(rep.lines))
    print(rep.summary())
    return 0 if rep.ok else 3
if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
