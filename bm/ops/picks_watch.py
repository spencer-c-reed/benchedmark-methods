from __future__ import annotations
import json
import hashlib
import os
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from bm import records, succession
from bm.picks import run
ROOT = Path(__file__).resolve().parents[2]
INSTALLED_UNITS = Path.home() / '.config/systemd/user'

def _calendar(text: str):
    m = re.search('^OnCalendar=(\\d{4}-\\d\\d-\\d\\d) (\\d\\d:\\d\\d(?::\\d\\d)?)(?: (\\S+))?$', text, re.M)
    if not m:
        return None
    zone = m[3] or (Path('/etc/timezone').read_text().strip() if Path('/etc/timezone').exists() else None)
    if not zone:
        return None
    return datetime.fromisoformat(m[1] + 'T' + m[2]).replace(tzinfo=ZoneInfo(zone))

def configured() -> list[dict]:
    out = []
    services = {p.name: p for p in (ROOT / 'ops/systemd').glob('bm-picks-*.service')}
    services.update({p.name: p for p in INSTALLED_UNITS.glob('bm-picks-*.service')})
    for timer in INSTALLED_UNITS.glob('bm-picks-*.timer'):
        services.setdefault(timer.with_suffix('.service').name, timer.with_suffix('.service'))
    for service in sorted(services.values()):
        text = service.read_text() if service.exists() else ''
        exec_start = re.search('^ExecStart=(.+)$', text, re.M)
        command = exec_start[1] if exec_start else ''
        cmd = re.search('-m bm\\.picks\\.run slate (\\d+) --timing (early|prelock|london)', command)
        if not cmd:
            named = re.fullmatch('bm-picks-w(\\d+)-(early|prelock|london)\\.service', service.name)
            if named:
                out.append(dict(week=int(named[1]), timing=named[2], unit=service.name, timer=service.with_suffix('.timer').name, due=None, models=[], cohort_unknown=True, binding_valid=False, configuration_source=str(service), configuration_status='missing or unsupported ordinary service command; cohort unmeasured'))
            continue
        timer = service.with_suffix('.timer')
        try:
            due = _calendar(timer.read_text()) if timer.exists() else None
        except (ValueError, KeyError):
            due = None
        model = re.search('--models ([^\\s]+)', command)
        at = re.search('--at ([^\\s]+)', command)
        binding_valid = bool(due and at)
        closed_legacy_calendar = bool(not at and due and (datetime.now(due.tzinfo) >= due + timedelta(hours=6)))
        if at:
            try:
                original = datetime.fromisoformat(at[1])
                binding_valid = bool(due and original.tzinfo and (original == due))
            except ValueError:
                binding_valid = False
        out.append(dict(week=int(cmd[1]), timing=cmd[2], unit=service.name, timer=timer.name, due=due.isoformat() if binding_valid or closed_legacy_calendar else None, original_as_of=at[1] if at else None, binding_valid=binding_valid, configured_models_argument=model[1] if model else None, configuration_source=str(service), models=succession.picks_models(model[1].split(',') if model else list(run.PICKS_MODELS), due), configuration_status='scheduled' if binding_valid else 'missing/unsupported timer or mismatched original clock'))
    known = {(o['week'], o['timing']) for o in out}
    for path in sorted((ROOT / 'records/picks').glob('2026-w*-*.jsonl')):
        m = re.fullmatch('2026-w(\\d+)-(early|prelock|london)', path.stem)
        if m and (int(m[1]), m[2]) not in known:
            rows = [r['payload'] for r in records.ChainLog(path).rows()]
            ctx = next((r for r in rows if r.get('kind') == 'slate_context'), None)
            out.append(dict(week=int(m[1]), timing=m[2], unit=f'bm-picks-w{m[1]}-{m[2]}.service', timer=f'bm-picks-w{m[1]}-{m[2]}.timer', due=ctx.get('as_of') if ctx else None, models=ctx.get('models', list(run.PICKS_MODELS)) if ctx else succession.picks_models(list(run.PICKS_MODELS)), configuration_status='missing service and timer configuration'))
    return out

def unit_state(unit: str) -> dict:
    p = subprocess.run(['systemctl', '--user', 'show', unit, '-p', 'LoadState', '-p', 'ActiveState', '-p', 'UnitFileState', '-p', 'ExecStart'], capture_output=True, text=True, timeout=10)
    if p.returncode:
        return {'LoadState': 'unknown', 'check_error': p.returncode}
    return dict((l.split('=', 1) for l in p.stdout.splitlines() if '=' in l))

def _record_signature(path: Path):
    try:
        st = path.stat()
    except OSError:
        return None
    return dict(dev=st.st_dev, ino=st.st_ino, size=st.st_size, mtime_ns=st.st_mtime_ns)

def _configuration_signature(opportunity: dict) -> str:
    return hashlib.sha256(json.dumps(opportunity, sort_keys=True, default=str, separators=(',', ':')).encode()).hexdigest()

def _analysis_signature() -> str:
    release = ROOT / 'RELEASE'
    try:
        payload = json.loads(release.read_text())
        sha = payload.get('sha')
        if sha:
            return f'release:{sha}'
    except (OSError, ValueError, TypeError):
        pass
    from bm.picks import receipts as picks_receipts
    sources = (Path(__file__), Path(run.__file__), Path(picks_receipts.__file__))
    digest = hashlib.sha256()
    for path in sources:
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(os.fspath(path).encode())
    return f'source:{digest.hexdigest()}'

def _runtime_fields(o: dict, now: datetime, deadline: datetime | None, gaps: list[dict], *, timer_state=None, service_state=None) -> dict:
    due = datetime.fromisoformat(o['due']) if o.get('due') else None
    timer_state = unit_state(o['timer']) if timer_state is None else timer_state
    service_state = unit_state(o['unit']) if service_state is None else service_state
    timer_ready = timer_state.get('LoadState') == 'loaded' and timer_state.get('UnitFileState') in ('enabled', 'enabled-runtime', 'static') and (due is not None and now >= due or timer_state.get('ActiveState') == 'active')
    loaded_command = service_state.get('ExecStart', '')
    command_verified = f'bm.picks.run slate {o['week']} --timing {o['timing']}' in loaded_command and o.get('binding_valid', True)
    if 'original_as_of' in o:
        loaded_at = re.search('--at ([^\\s;]+)', loaded_command)
        loaded_models = re.search('--models ([^\\s;]+)', loaded_command)
        command_verified = command_verified and (loaded_at[1] if loaded_at else None) == o['original_as_of']
        command_verified = command_verified and (loaded_models[1] if loaded_models else None) == o.get('configured_models_argument')
    return dict(timer_state=timer_state, service_state=service_state, timer_ready=timer_ready, service_command_verified=command_verified, recovery_eligible=any((g.get('recovery_eligible') for g in gaps)) and service_state.get('LoadState') == 'loaded' and command_verified)

def inspect(opportunity: dict, now: datetime, *, payloads=None, schedule=None, timer_state=None, service_state=None) -> dict:
    o = dict(opportunity)
    slate = f'2026-w{o['week']}-{o['timing']}'
    path = ROOT / 'records/picks' / f'{slate}.jsonl'
    signature_before = _record_signature(path)
    rows = payloads if payloads is not None else [r['payload'] for r in records.ChainLog(path).rows()] if path.exists() else []
    signature_after = _record_signature(path)
    record_signature = signature_before if payloads is None and signature_before == signature_after else None
    ctx = next((r for r in rows if r.get('kind') == 'slate_context'), None)
    due = datetime.fromisoformat(o['due']) if o.get('due') else None
    legacy = list(dict.fromkeys((r['entrant'] for r in rows if r.get('kind') == 'pick' and r.get('entrant')))) if rows and (not ctx) else []
    models = ctx['models'] if ctx else legacy or o['models']
    n = ctx.get('instances', run.INSTANCES) if ctx else run.INSTANCES
    games = ctx['games'] if ctx else []
    if ctx:
        asof = datetime.fromisoformat(ctx['as_of'])
        deadline = min(asof + timedelta(hours=6), *(datetime.fromisoformat(g['kickoff']) for g in games)) if games else None
    elif due:
        if schedule is None:
            from bm.stats import nflverse as nv
            schedule = nv.schedule(2026, as_of=due)
        kicks = [datetime.fromisoformat(g['kickoff']) for g in schedule if g['week'] == o['week'] and datetime.fromisoformat(g['kickoff']) > due]
        deadline = min(due + timedelta(hours=6), *kicks) if kicks else None
    else:
        deadline = None
    closed = deadline is not None and now >= deadline
    from bm.picks.receipts import receipts_by_record
    receipts = receipts_by_record(rows) if not ctx else {}
    if receipts and deadline is None:
        deadline = min((datetime.fromisoformat(x['deadline']) for x in receipts.values()))
        closed = now >= deadline
    gaps, complete, historical_own = ([], 0, 0)
    for model in models:
        for inst in range(1, n + 1):
            for step in ('blind', 'line'):
                key = f'{slate}:{model}:i{inst}:{step}'
                prior = [r for r in rows if r.get('kind') == 'pick' and (r.get('seat_key') or r.get('key')) == key]
                seat = (ctx or {}).get('seat_prompts', {}).get(f'{model}:i{inst}', {})
                if ctx and any((run.completed_attempt(r, games, step, seat.get('bankroll', run.START_BANKROLL)) for r in prior)):
                    complete += 1
                    continue
                if not ctx and any((run.receipt_status(r, x['deadline'], x) == 'prospective receipt verified' for r in prior for x in [receipts.get(r.get('record_id'))] if x)):
                    complete += 1
                    continue
                if not ctx and any((r.get('outcome') == 'own' for r in prior)):
                    historical_own += 1
                last = prior[-1] if prior else {}
                receipt_excluded = next((st for r in prior for x in [receipts.get(r.get('record_id'))] if x for st in [run.receipt_status(r, x['deadline'], x)] if st.startswith('excluded')), None)
                reason = f'receipt-annotated answer {receipt_excluded}' if receipt_excluded else 'legacy unverified own answer' if any((r.get('outcome') == 'own' for r in prior)) and (not ctx) else 'invalid or partial claimed success' if last.get('outcome') == 'own' else 'infrastructure failure' if last.get('outcome') == 'infra_default' else 'model/parse failure' if prior else 'missing opportunity'
                eligible = not closed and deadline is not None and (due is not None) and (now >= due) and (not rows or ctx is not None) and (not ctx or run.retry_eligible(prior, games, step, seat.get('bankroll', run.START_BANKROLL), now, deadline))
                gaps.append(dict(seat_key=key, model=model, instance=inst, step=step, reason=reason, attempts=len(prior), failure=last.get('failure'), recovery_eligible=bool(eligible), status='closed gap; no late backfill' if closed else 'legacy frozen-input gap; repair ineligible' if rows and (not ctx) else 'model repair allowance exhausted' if ctx and (not eligible) and due and (now >= due) and deadline and (now < deadline) else 'open gap' if eligible else 'upcoming or deadline/configuration unknown'))
    runtime = _runtime_fields(o, now, deadline, gaps, timer_state=timer_state, service_state=service_state)
    o.update(slate=slate, observed_at=now.isoformat(), models=list(models), cohort_source='slate_context' if ctx else 'legacy pick records' if legacy else 'configured (no records yet)', deadline=deadline.isoformat() if deadline else None, expected_seat_steps=None if o.get('cohort_unknown') else len(models) * n * 2, complete_seat_steps=complete, gaps=gaps, historical_own_seat_steps=historical_own, true_gap_seat_steps=sum((g['reason'] != 'legacy unverified own answer' for g in gaps)), completeness_measurement='prospective receipt verified' if ctx else 'gateway receipt annotation (legacy records)' if receipts else 'legacy timeliness unmeasured', terminal=closed or bool(rows and ctx is None), record_signature=record_signature, configuration_signature=_configuration_signature(opportunity), analysis_signature=_analysis_signature(), status='unmeasured service/cohort configuration' if o.get('cohort_unknown') else 'complete' if not gaps else 'closed gaps' if closed else 'unmeasured deadline/configuration' if deadline is None else 'open/upcoming gaps')
    o.update(runtime)
    return o

def _reuse_terminal(opportunity: dict, prior: dict, now: datetime):
    if not prior or not prior.get('terminal'):
        return None
    slate = f'2026-w{opportunity['week']}-{opportunity['timing']}'
    if prior.get('slate') != slate:
        return None
    path = ROOT / 'records/picks' / f'{slate}.jsonl'
    signature = _record_signature(path)
    if not signature or signature != prior.get('record_signature'):
        return None
    config_signature = _configuration_signature(opportunity)
    if config_signature != prior.get('configuration_signature'):
        return None
    analysis_signature = _analysis_signature()
    if analysis_signature != prior.get('analysis_signature'):
        return None
    deadline = datetime.fromisoformat(prior['deadline']) if prior.get('deadline') else None
    if prior.get('cohort_source') == 'legacy pick records' and (deadline is None or now < deadline):
        return None
    runtime = _runtime_fields(opportunity, now, deadline, prior.get('gaps') or [])
    if _record_signature(path) != signature:
        return None
    row = dict(prior)
    row.update(observed_at=now.isoformat(), record_signature=signature, configuration_signature=config_signature, analysis_signature=analysis_signature)
    row.update(runtime)
    return row
EVIDENCE_H = 6
EVIDENCE_TAIL_BYTES = 40 << 20
ROUTE_ANSWERED = ('REFUSED', 'PARSE')
GONE = ('not recognized', 'not found', 'no such model', 'does not exist', 'unknown model', 'model_not_found', 'invalid model')
_GATEWAY_TAIL_CACHE = {}

def _gateway_tail_rows(log: Path) -> tuple[tuple, ...]:
    try:
        observed = log.stat()
    except OSError:
        return ()
    signature = (observed.st_dev, observed.st_ino, observed.st_size, observed.st_mtime_ns)
    cache_key = os.fspath(log.absolute())
    cached = _GATEWAY_TAIL_CACHE.get(cache_key)
    if cached and cached[0] == signature:
        return cached[1]
    with open(log, 'rb') as f:
        opened = os.fstat(f.fileno())
        opened_signature = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        f.seek(max(0, opened.st_size - EVIDENCE_TAIL_BYTES))
        if f.tell():
            f.readline()
        lines = f.readlines()
        finished = os.fstat(f.fileno())
    finished_signature = (finished.st_dev, finished.st_ino, finished.st_size, finished.st_mtime_ns)
    rows = []
    for ln in reversed(lines):
        if b'"model_call"' not in ln:
            continue
        try:
            p = json.loads(ln).get('payload') or {}
        except ValueError:
            continue
        rows.append(((p.get('lane'), p.get('model_requested')), 'ts' in p, p.get('ts'), p.get('error'), p.get('error_detail')))
    result = tuple(rows)
    if signature == opened_signature == finished_signature:
        if cache_key not in _GATEWAY_TAIL_CACHE and len(_GATEWAY_TAIL_CACHE) >= 4:
            _GATEWAY_TAIL_CACHE.clear()
        _GATEWAY_TAIL_CACHE[cache_key] = (signature, result)
    return result

def last_outcomes(routes: list[tuple[str, str]], since: datetime, log: Path | None=None) -> dict:
    log = log or ROOT / 'logs' / 'gateway.jsonl'
    want = set(routes)
    out = {r: [] for r in routes}
    if not log.exists():
        return out
    for key, has_ts, raw_ts, err, detail in _gateway_tail_rows(log):
        if key not in want:
            continue
        if not has_ts:
            raise KeyError('ts')
        ts = datetime.fromisoformat(raw_ts)
        if ts < since:
            break
        if err in ROUTE_ANSWERED:
            err = None
        out[key].append((ts, err, str(detail or '')[:200]))
    return out

def live_evidence(rows: list[tuple], now: datetime) -> dict:
    if not rows:
        return {'evidence': 'unmeasured'}
    ts, err, detail = rows[0]
    base = {'last_call_at': ts.isoformat(), 'last_error': err, 'calls_in_window': len(rows)}
    if not err:
        return dict(base, evidence='ok')
    if any((g in detail.lower() for g in GONE)) or (len(rows) > 1 and rows[1][1]):
        return dict(base, evidence='blocked', last_detail=detail)
    return dict(base, evidence='mixed', last_detail=detail)
PREFLIGHT_BUDGET = timedelta(minutes=5)

class _Unmeasured(Exception):

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code, self.detail = (code, detail)

def readiness(models: list[str], deadline: datetime, *, probe: bool | None=None, now: datetime | None=None, memo: dict | None=None) -> dict:
    from bm.gateway.core import Gateway, GatewayError, ModelRequest
    from bm.gateway import adapters
    from datetime import timezone
    gw = Gateway()
    now = now or datetime.now(timezone.utc)
    allroutes = []
    for name in models:
        ent = gw.entrants.get(name) or {}
        allroutes += [(ent.get('lane'), ent.get('model'))] + [(f['lane'], f['model']) for f in (ent.get('spread') or []) + (ent.get('fallback') or [])]
    evidence = last_outcomes(list(dict.fromkeys(allroutes)), now - timedelta(hours=EVIDENCE_H))
    if probe is None:
        import os
        probe = 'PYTEST_CURRENT_TEST' not in os.environ
    if probe:
        unmeasured = [n for n in models if (gw.entrants.get(n) or {}).get('lane') and (not evidence.get((gw.entrants[n].get('lane'), gw.entrants[n].get('model'))))]
        if memo is not None:
            probed = memo.setdefault('probed', set())
            unmeasured = [n for n in unmeasured if n not in probed]
            probed.update(unmeasured)
        if unmeasured:
            try:
                from bm.ops.probe import probe as live_probe
                live_probe(unmeasured, 'readiness')
                evidence = last_outcomes(list(dict.fromkeys(allroutes)), now - timedelta(hours=EVIDENCE_H))
            except Exception as exc:
                print('readiness probe failed:', repr(exc)[:200], flush=True)
    result = {}
    for name in models:
        ent = gw.entrants.get(name) or {}
        routes = [(ent.get('lane'), ent.get('model'))] + [(f['lane'], f['model']) for f in (ent.get('spread') or []) + (ent.get('fallback') or [])]
        observed = []
        for ln, model in dict.fromkeys(routes):
            lane = gw.lanes.get(ln)
            row = dict(lane=ln, requested_model=model, status='unknown')
            try:
                if not lane:
                    raise GatewayError('LANE_DISABLED', 'missing configured lane')
                gw.spend_guard(lane, model)
                gw.money_guard(lane, model)
                cooling = gw.ledger.cooldown_left(f'{ln}:{model}')
                if cooling > 0:
                    raise GatewayError('RATE_LIMITED', 'existing provider backoff active; no retry dispatch')
                if lane['adapter'] == 'claude_cli' and lane.get('yield_at_weekly_pct'):
                    cache = Path.home() / '.cache/status/claude-weekly.json'
                    quota = json.loads(cache.read_text())
                    if not isinstance(quota.get('utilization'), (int, float)):
                        raise ValueError('quota utilization unknown')
                    row['quota_source'] = str(cache)
                    row['quota_utilization'] = quota['utilization']
                    row['quota_observed_at'] = quota.get('observedAt')
                    row['quota_resets_at'] = quota.get('resetsAt')
                    quota_observed = datetime.fromisoformat(quota['observedAt'])
                    from datetime import timezone
                    age = datetime.now(timezone.utc) - quota_observed
                    if quota['utilization'] * 100 < float(lane['yield_at_weekly_pct']) and (not timedelta(0) <= age <= timedelta(minutes=30)):
                        raise ValueError('under-cap quota observation stale; capacity unknown')
                ev = live_evidence(evidence.get((ln, model), []), now)
                budget = min(deadline, datetime.now(timezone.utc) + PREFLIGHT_BUDGET)
                seen = memo.setdefault('preflight', {}) if memo is not None else None
                try:
                    if seen is not None and (ln, model) in seen:
                        if seen[ln, model] is not None:
                            raise seen[ln, model]
                    else:
                        try:
                            gw.adapters[lane['adapter']].preflight(dict(lane, _preflight_deadline=budget), model)
                        except GatewayError as exc:
                            if seen is not None and (not (exc.code == 'TIMEOUT' and 'during preflight' in (exc.detail or ''))):
                                seen[ln, model] = exc
                            raise
                        if seen is not None:
                            seen[ln, model] = None
                except (GatewayError, subprocess.TimeoutExpired) as exc:
                    if isinstance(exc, GatewayError) and (not (exc.code == 'TIMEOUT' and 'during preflight' in (exc.detail or ''))):
                        raise
                    passed = datetime.now(timezone.utc) >= deadline
                    row['preflight'] = 'decision deadline passed' if passed else f'preflight did not finish inside its {int(PREFLIGHT_BUDGET.total_seconds() // 60)}-minute budget'
                    if passed:
                        row['deadline_passed'] = True
                    if ev['evidence'] != 'ok':
                        row.update(ev)
                        raise _Unmeasured('DEADLINE_PASSED' if passed else 'PREFLIGHT_TIMEOUT', row['preflight'])
                if lane['adapter'] == 'anthropic_api':
                    req = ModelRequest(entrant_id=name, decision_id='commissioner-readiness', system=run.SYSTEM, prompt='', max_output_tokens=16000, deadline=deadline)
                    est = adapters.AnthropicAPI()._est(model, req)
                    p = subprocess.run([str(adapters.CREDITS_LEDGER), 'can-spend', lane.get('credits_consumer', 'benchedmark'), f'{est:.4f}'], capture_output=True, text=True, timeout=10)
                    if p.returncode:
                        raise GatewayError('QUOTA_YIELD', 'included-credit ledger refused conservative request estimate')
                row['status'] = 'preflight eligible; inference untested'
                row.update(ev)
                if ev['evidence'] == 'blocked':
                    raise GatewayError(ev.get('last_error') or 'TRANSIENT', 'live evidence: ' + (ev.get('last_detail') or 'last calls failed'))
                if ev['evidence'] == 'ok':
                    row['status'] = 'ready; live call answered'
            except _Unmeasured as exc:
                row.update(status='unknown', cause=exc.code, detail=exc.detail)
            except GatewayError as exc:
                row.update(status='blocked', cause=exc.code, detail=exc.detail)
            except Exception as exc:
                row.update(status='unknown', cause=type(exc).__name__)
            observed.append(row)
        result[name] = dict(routes=observed, eligible=any((r['status'] in ('preflight eligible; inference untested', 'ready; live call answered') for r in observed)))
    return result

def notification_fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:20]

def notify_changed(row: dict, previous: dict, key: str, message: str, *, terminal: bool, evidence) -> bool:
    from bm.ops.alert import wake
    fingerprint = notification_fingerprint(evidence)
    unchanged = previous.get('notification_fingerprint') == fingerprint
    delivered = bool(previous.get('notification_delivered')) and unchanged
    row['notification_fingerprint'] = fingerprint
    if not (terminal and delivered):
        delivered = bool(wake(key + '|' + fingerprint, message)) or delivered
    row['notification_delivered'] = delivered
    return delivered

def check(now: datetime, *, repair=False, notify=False, memo: dict | None=None) -> list[dict]:
    from bm.ops.alert import wake
    out = []
    state = ROOT / 'state/commissioner-picks.json'
    try:
        old = json.loads(state.read_text()).get('opportunities', [])
    except (OSError, ValueError):
        old = []
    previous = {r.get('slate'): r for r in old}
    for o in configured():
        try:
            slate = f'2026-w{o['week']}-{o['timing']}' if 'week' in o and 'timing' in o else None
            r = (_reuse_terminal(o, previous.get(slate, {}), now) if slate else None) or inspect(o, now)
            prior = previous.get(r['slate'], {})
            for key in ('notification_fingerprint', 'notification_delivered'):
                if key in prior:
                    r[key] = prior[key]
            due = datetime.fromisoformat(r['due']) if r.get('due') else None
            deadline = datetime.fromisoformat(r['deadline']) if r.get('deadline') else None
            genuine = [g for g in r['gaps'] if g.get('reason') != 'legacy unverified own answer']
            if genuine and (not r.get('terminal')) and deadline and (now < deadline) and due and (due - now <= timedelta(hours=2)):
                missing = sorted({g['model'] for g in genuine})
                r['readiness'] = readiness(missing, deadline, memo=memo) if repair else {'status': 'not checked in read-only scan'}
                eligible = [m for m in missing if (r['readiness'].get(m) or {}).get('eligible')]
                if repair and r['recovery_eligible'] and eligible and (r['service_state'].get('ActiveState') not in ('active', 'activating')):
                    permit = ROOT / 'state/commissioner-picks-dispatch.json'
                    permit.parent.mkdir(parents=True, exist_ok=True)
                    auth = json.loads(permit.read_text()) if permit.exists() else {}
                    auth[r['slate']] = {'observed_at': now.isoformat(), 'eligible_models': eligible}
                    tmp = permit.with_suffix('.tmp')
                    tmp.write_text(json.dumps(auth))
                    tmp.replace(permit)
                    p = subprocess.run(['systemctl', '--user', 'start', '--no-block', r['unit']], capture_output=True, text=True, timeout=10)
                    r['dispatch'] = {'status': 'requested, not completed' if p.returncode == 0 else 'failed', 'returncode': p.returncode}
            needs_attention = (genuine or r.get('cohort_unknown')) and (not r['timer_ready'] or due is None or now >= due or (due - now <= timedelta(hours=2)))
            if notify and needs_attention:
                blocked = [m for m, v in r.get('readiness', {}).items() if isinstance(v, dict) and (not v.get('eligible'))]
                historical = r.get('historical_own_seat_steps', 0)
                coverage = f'{historical} historical own seat-steps (timeliness unverified), {len(genuine)} genuine gaps; prospective completion unmeasured' if r.get('completeness_measurement') == 'legacy timeliness unmeasured' else f'{r['complete_seat_steps']}/{r['expected_seat_steps']} prospective seat-steps verified; {len(genuine)} genuine gaps'
                if r.get('cohort_unknown'):
                    coverage = 'service/cohort configuration unmeasured; no seat denominator invented'
                notify_changed(r, previous.get(r['slate'], {}), 'commissioner|' + r['slate'], f'BenchedMark commissioner {r['slate']}: {coverage}; deadline {r['deadline']}; {r['status']}; timer ready {r['timer_ready']}; blocked/unknown routes {blocked}. Inspect state/commissioner-picks.json. ' + ('Historical diagnostic retained; no replay or late backfill.' if r.get('terminal') else 'Repair configured routes/timer before cutoff; never backfill after deadline.'), terminal=r.get('terminal', False), evidence=dict(gaps=genuine, historical_own=historical, deadline=r['deadline'], terminal=r.get('terminal', False), readiness=r.get('readiness') if not r.get('terminal') else None, **{'cohort_unknown': True} if r.get('cohort_unknown') else {}))
            out.append(r)
        except Exception as exc:
            out.append(dict(o, status='unmeasured commissioner scan failure', error=type(exc).__name__))
            if notify:
                wake('commissioner-scan|' + o['unit'], f'BenchedMark commissioner cannot measure {o['unit']}: {type(exc).__name__}. Investigate before its deadline.')
    state = ROOT / 'state/commissioner-picks.json'
    state.parent.mkdir(parents=True, exist_ok=True)
    temp = state.with_suffix('.tmp')
    temp.write_text(json.dumps({'observed_at': now.isoformat(), 'opportunities': out}, indent=2))
    temp.replace(state)
    return out
