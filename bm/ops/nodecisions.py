from __future__ import annotations
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
ROOT = Path(__file__).resolve().parents[2]
LEAGUES = ROOT / 'leagues'
ET = ZoneInfo('America/New_York')
GRACE = timedelta(hours=1, minutes=30)
DRAFT_STALL = timedelta(minutes=60)
IN_FLIGHT = timedelta(minutes=60)
LOOKBACK = timedelta(hours=24)
SILENT_HOURS = 26

def critical_windows(league, events: list[dict], now: datetime, *, schedule=None, steps=None) -> list[dict]:
    from bm.agent import bots
    from bm.league.season import week_steps
    from bm.cli import draft_week_trade_skipped
    from bm import succession
    models = sorted({c for s in league.seats.values() if s['entrant_id'] not in bots.ALL for c in [succession.controller(s['entrant_id'], league=(getattr(league, 'cfg', None) or {}).get('id'), at=now)] if c not in bots.ALL})
    service = 'benchedmark-agent-tick.service' if league.cfg.get('mode') == 'agent' else 'benchedmark-tick.service'
    drafted = any((e['type'] == 'draft_complete' for e in events))
    if not drafted:
        due = datetime.fromisoformat(league.cfg['draft_as_of'])
        if due > now + timedelta(hours=2):
            return []
        if schedule is None:
            from bm.stats import nflverse as nv
            schedule = nv.schedule(2026, as_of=now)
        kicks = [datetime.fromisoformat(g['kickoff']) for g in schedule if g['week'] == league.cfg['start_week'] and datetime.fromisoformat(g['kickoff']) >= due]
        cutoff = min(kicks) if kicks else None
        activity = next((e for e in reversed(events) if e['type'] == 'draft_activity' and e.get('status') in ('paused', 'on_clock')), None)
        committed = max((e.get('pick_no', 0) for e in events if e['type'] == 'draft_pick'), default=0)
        tid = activity.get('team') if activity and activity.get('pick_no', 0) > committed else None
        selected = league.seats.get(tid, {}).get('entrant_id')
        entrants = [selected] if selected and selected not in bots.ALL else models
        closed = cutoff is not None and now >= cutoff
        return [dict(type='critical_route_readiness', step='draft', due=due.isoformat(), deadline=cutoff.isoformat() if cutoff else None, entrants=entrants, unit=service, preflight_eligible=bool(cutoff and (not closed)), recovery_eligible=bool(selected and cutoff and (due <= now < cutoff)), status='closed original draft lock; no replay' if closed else 'draft cutoff unmeasured; no automatic dispatch' if cutoff is None else 'selected on-clock draft seat' if selected else 'planned draft routes; selected seat unmeasured')]
    if league.cfg.get('mode') == 'agent':
        return []
    week = league.cfg['start_week']
    for e in events:
        if e['type'] == 'week_advance':
            week = e.get('week', week)
    if steps is None:
        steps = []
        for w in (week, week + 1):
            if w <= int(league.cfg.get('end_week') or league.rules.last_week):
                steps.extend(week_steps(w))
    out = []
    offset = timedelta(minutes=int(league.cfg.get('step_offset_min') or 0))
    for key, scheduled in steps:
        if not key.startswith('trade:') or any((e['type'] == 'step_done' and e.get('step') == key for e in events)) or (hasattr(league, 'done') and league.done(key)):
            continue
        w = int(key.split(':')[1])
        due = scheduled + offset
        if w > league.rules.trades['deadline_week'] or draft_week_trade_skipped(league, w, key):
            continue
        if due > now + timedelta(hours=2) or due < now - timedelta(hours=24):
            continue
        cutoff = due + timedelta(hours=6)
        marker = f':w{w}-t{key.split(':')[2]}:'
        partial = any((e.get('window') == marker.strip(':') or marker in (e.get('decision_id') or '') for e in events))
        resumable = False
        if partial and hasattr(league, 'dir'):
            from bm.league import trade_context as tc
            original = tc.read(league, 'trade_original_context', marker.strip(':'))
            if original:
                tc.restore_view(league, original)
                cutoff = datetime.fromisoformat(original['deadline'])
                resumable = True
        pause = next((e for e in reversed(events) if e['type'] == 'trade_batch_paused' and e.get('window') == marker.strip(':')), None)
        paced = not pause or now >= datetime.fromisoformat(pause['next_try_at'])
        application_blocked = False
        out.append(dict(type='critical_route_readiness', step=key, due=due.isoformat(), deadline=cutoff.isoformat(), entrants=models, unit=service, preflight_eligible=now < cutoff, operator_blocked=application_blocked, window_open=bool(resumable and (not application_blocked) and (now < cutoff)), recovery_eligible=bool((not partial or resumable) and (not application_blocked) and paced and (w == week) and (due <= now < cutoff)), status='closed scheduled window; no automatic replay' if now >= cutoff else 'retained trade application gap; operator required' if application_blocked else 'retained original trade phase; guarded paced recovery' if resumable else 'partial trade batch lacks resumable context; operator required' if partial else 'existing unfinished trade step; no independent seat obligations inferred'))
    return out
CAPACITY = ('QUOTA_YIELD', 'SELF_CAP', 'RATE_LIMITED', 'PAID_REFUSED', 'LANE_DISABLED', 'LANE_BREAKER')
GATEWAY = ('TIMEOUT', 'TRANSIENT', 'AUTH', 'NO_ENTRANT', 'LANE_FAILED', 'WORKER_LOST')
OUR_PARSE_MARKERS = ('truncated', 'empty content', 'output budget exhausted')
HARNESS_OUTPUT_CAP = {'forecast_book': 16000}
WORKER_LOST_AFTER = timedelta(minutes=75)
RECONCILED = ('trade_decision_gap', 'draft_reconciliation', 'wake_lost', 'trade_offer_gap', 'league_not_created')

def _lane_floors() -> dict:
    import yaml
    try:
        return {l['lane']: int(l.get('min_output_tokens') or 0) for l in yaml.safe_load((ROOT / 'config' / 'lanes.yaml').read_text()) or []}
    except (OSError, ValueError, TypeError, KeyError):
        return {}

def failure_kind(code: str | None, detail: str='', outputs=(), cap: int | None=None) -> str | None:
    if not code:
        return None
    d = (detail or '').lower()
    if code in ('PARSE', 'ILLEGAL'):
        return 'our_parse' if any((c and (o or 0) >= c for o, c in outputs)) else 'model'
    if code == 'REFUSED':
        return 'refusal'
    if any((m in d for m in OUR_PARSE_MARKERS)):
        return 'our_parse'
    if code in CAPACITY:
        return 'capacity'
    return 'gateway'

def _session_outputs(league, *, floors=None) -> dict:
    path = getattr(league, 'dir', None)
    path = path / 'sessions.jsonl' if path else None
    out = {}
    if not path or not path.exists():
        return out
    floors = _lane_floors() if floors is None else floors
    for line in path.read_text().splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        p = row.get('payload', row)
        if p.get('decision_id'):
            cap = HARNESS_OUTPUT_CAP.get(p.get('decision_kind'), 3000)
            out[p['decision_id']] = [((t.get('usage') or {}).get('output') or 0, max(cap, floors.get(t.get('lane'), 0))) for t in p.get('turns') or []]
    return out

def trade_reconcile(league, events: list[dict], now: datetime, *, outputs=None, floors=None) -> list[dict]:
    expected, infra, answers = ({}, {}, {})
    for e in events:
        did = e.get('decision_id') or ''
        kind = e.get('kind')
        if kind not in ('trade_offer', 'trade_response'):
            continue
        if e['type'] == 'decision_expected':
            expected[did] = e
        elif e['type'] in ('decision_skipped_infra', 'decision_pending', 'decision_missed'):
            infra[did] = e
        elif e['type'] == 'decision':
            answers[did] = e
    done_steps = {e.get('step') for e in events if e['type'] == 'step_done'}
    closed_windows = {e.get('window') for e in events if e['type'] == 'trade_window_gap'}
    lazy = {}
    windows, issues = ({}, [])
    for did in sorted(set(expected) | set(infra) | set(answers)):
        parts = did.split(':')
        window = parts[1] if len(parts) > 1 else '?'
        try:
            week, n = (int(window[1:].split('-t')[0]), int(window.split('-t')[1]))
        except (ValueError, IndexError):
            continue
        src = expected.get(did) or infra.get(did) or answers.get(did)
        hold = (expected.get(did) or {}).get('hold_until')
        cutoff = datetime.fromisoformat(hold) if hold else None
        step_closed = f'trade:{week}:{n}' in done_steps or f'trade:{week}:{n}' in closed_windows
        closed = step_closed or (cutoff is not None and now >= cutoff) or (cutoff is None and did not in answers)
        at = datetime.fromisoformat(src.get('logged_at') or src['ts'])
        w = windows.setdefault(window, {'window': window, 'week': week, 'n': n, 'asked': 0, 'own': 0, 'model_default': 0, 'ours_open': 0, 'ours_missed': 0, 'by_kind': {}, 'deadline': hold, '_at': at})
        w['_at'] = max(w['_at'], at)
        w['asked'] += 1
        ans = answers.get(did)
        if ans and (not ans.get('forced_default')):
            w['own'] += 1
            continue
        if ans:
            cause = ans['forced_default'].get('cause')
            if outputs is None:
                outputs = lazy.setdefault('o', _session_outputs(league, floors=floors))
            fk = failure_kind(cause, '', outputs.get(did, ()))
            if fk == 'model':
                w['model_default'] += 1
                continue
            w['by_kind'][fk] = w['by_kind'].get(fk, 0) + 1
            w['ours_missed'] += 1
            issues.append(dict(type='trade_decision_gap', decision_id=did, team=ans.get('team'), entrant=ans.get('entrant'), _at=at, decision_kind=ans.get('kind'), cause=cause, failure_kind=fk, deadline=hold, recovery_eligible=False, status='forced default attributed to our infrastructure; terminal, excluded from model skill'))
            continue
        last = infra.get(did) or {}
        fk = failure_kind(last.get('cause'), last.get('detail', '')) if last else 'not_dispatched'
        w['by_kind'][fk] = w['by_kind'].get(fk, 0) + 1
        if closed:
            w['ours_missed'] += 1
        else:
            w['ours_open'] += 1
        issues.append(dict(type='trade_decision_gap', decision_id=did, team=src.get('team'), entrant=src.get('entrant'), _at=at, decision_kind=src.get('kind'), cause=last.get('cause'), failure_kind=fk, deadline=hold, recovery_eligible=not closed, unit='benchedmark-tick.service', status='closed original trade cutoff; infrastructure miss, no late replay' if closed else 'infrastructure gap open before original cutoff; ordinary tick resumes the retained window'))

    def when(x):
        return datetime.fromisoformat(x['deadline']) if x.get('deadline') else x.get('_at') or now
    keep = [w for w in windows.values() if when(w) >= now - timedelta(days=8) or w['ours_open']]
    issues = [i for i in issues if i['recovery_eligible'] or when(i) >= now - timedelta(hours=24)]
    for x in list(keep) + issues:
        x.pop('_at', None)
    return [dict(type='trade_reconciliation', **w) for w in sorted(keep, key=lambda w: (w['week'], w['n'])) if w['ours_open'] or w['ours_missed'] or w['model_default'] or w['asked']] + issues

def draft_reconcile(league, events: list[dict], now: datetime, *, busy=None) -> list[dict]:
    if league.cfg.get('mode') != 'agent':
        return []
    due = datetime.fromisoformat(league.cfg['draft_as_of'])
    if now < due - timedelta(hours=2):
        return []
    picks = [e for e in events if e['type'] == 'draft_pick']
    by_source = {}
    for e in picks:
        by_source[e.get('source', '?')] = by_source.get(e.get('source', '?'), 0) + 1
    paused = {}
    for e in events:
        if e['type'] == 'draft_activity' and e.get('status') == 'paused':
            paused.setdefault((e.get('pick_no'), e.get('team')), failure_kind(e.get('cause') or 'LANE_FAILED', ''))
    pauses = {}
    for fk in paused.values():
        pauses[fk] = pauses.get(fk, 0) + 1
    done = any((e['type'] == 'step_done' and e.get('step') == 'draft' for e in events))
    gap = next((e for e in events if e['type'] == 'draft_window_gap'), None)
    deadline = gap.get('deadline') if gap else None
    last = max((datetime.fromisoformat(e.get('logged_at') or e['ts']) for e in events if e['type'] in ('draft_pick', 'draft_activity')), default=None)
    activity = next((e for e in reversed(events) if e['type'] == 'draft_activity'), None)
    if busy is None:
        from bm.agent.runtime import seat_busy
        busy = seat_busy(league, 'draft') if hasattr(league, 'dir') else False
    open_ = not done and now >= due and (gap is None)
    item = dict(type='draft_reconciliation', step='draft', due=due.isoformat(), deadline=deadline, picks=len(picks), total=league.rules.roster_size * len(league.seats), by_source=by_source, pauses_by_kind=pauses, infra_defaults=by_source.get('infra_default', 0), worker_alive=busy, current_pause=dict(team=activity.get('team'), entrant=league.entrant(activity['team']) if activity.get('team') in league.seats else None, cause=activity.get('cause'), failure_kind=failure_kind(activity.get('cause') or 'LANE_FAILED', ''), since=activity.get('ts'), retry_at=activity.get('retry_at')) if activity and activity.get('status') == 'paused' and (not done) else None, on_clock=(activity or {}).get('team'), on_clock_status=(activity or {}).get('status'), last_activity=last.isoformat() if last else None, unit='benchedmark-agent-tick.service', recovery_eligible=bool(open_ and (not busy)), status='draft complete' if done else f'closed: {gap.get('cause')}' if gap else 'not yet due' if now < due else 'draft running (worker holds the board)' if busy else 'draft due with no live worker; agent tick respawns it')
    return [item]

def wake_reconcile(league, events: list[dict], now: datetime, *, busy=None, repair: bool=False) -> list[dict]:
    if league.cfg.get('mode') != 'agent':
        return []
    closed = {e.get('wake_id') for e in events if e['type'] in ('wake_close', 'wake_failed')}
    out = []
    for e in events:
        if e['type'] != 'wake_open' or e.get('wake_id') in closed:
            continue
        started = datetime.fromisoformat(e['started_at'])
        if now - started < WORKER_LOST_AFTER:
            continue
        lock = 'draft' if e.get('kind') == 'draft' else e['team']
        alive = busy(lock) if busy else __import__('bm.agent.runtime', fromlist=['seat_busy']).seat_busy(league, lock)
        if alive:
            continue
        item = dict(type='wake_lost', team=e['team'], wake_id=e['wake_id'], wake_kind=e.get('kind'), started_at=e['started_at'], failure_kind='gateway', cause='WORKER_LOST', recovery_eligible=False, status='worker gone without closing its wake; our failure')
        if repair:
            from bm.league.season import iso
            league.append({'type': 'wake_failed', 'ts': iso(now), 'team': e['team'], 'wake_id': e['wake_id'], 'cause': 'WORKER_LOST', 'detected_by': 'commissioner', 'note': 'infrastructure: the worker died before closing its wake; excluded from timing and agentic scores (D-REL-1a)'})
            item['recorded'] = True
            if e.get('kind') != 'draft':
                from bm.agent.runtime import next_lock
                import hashlib
                lock_at = next_lock(league, e['team'], now) or now + timedelta(days=7)
                if now < lock_at:
                    sid = hashlib.sha256(f'retry:{e['wake_id']}'.encode()).hexdigest()[:10]
                    league.append({'type': 'wake_schedule', 'ts': iso(now), 'team': e['team'], 'wake_id': e['wake_id'], 'op': 'schedule', 'id': sid, 'at': now.isoformat(), 'reason': f'resume after an infrastructure failure (WORKER_LOST): {e.get('reason', '')}'[:300], 'retry': True})
                    item.update(recovery_eligible=True, unit='benchedmark-agent-tick.service', deadline=lock_at.isoformat(), status="worker lost; resume wake scheduled before the seat's next lock")
        out.append(item)
    return out

def offer_reconcile(league, events: list[dict], now: datetime) -> list[dict]:
    if league.cfg.get('mode') != 'agent':
        return []
    from bm.agent import bots
    model_seat = {t for t, s in league.seats.items() if s.get('entrant_id') not in bots.ALL and (not s.get('house'))}
    latest, answered = ({}, set())
    for e in events:
        if e['type'] != 'trade_msg':
            continue
        if e.get('action') in ('PROPOSE', 'COUNTER') and e.get('expires_at'):
            latest[e['thread_id']] = e
        elif e['thread_id'] in latest and e.get('from_team') == latest[e['thread_id']].get('to_team'):
            answered.add((e['thread_id'], latest[e['thread_id']]['seq']))
    out = []
    for th, offer in sorted(latest.items()):
        to = offer.get('to_team')
        if to not in model_seat or (th, offer['seq']) in answered:
            continue
        exp = datetime.fromisoformat(offer['expires_at'])
        if exp < now - timedelta(hours=24):
            continue
        wakes = [e for e in events if e.get('team') == to and e.get('seq', 0) > offer['seq'] and (e['type'] in ('wake_close', 'wake_failed'))]
        own = any((e['type'] == 'wake_close' and e.get('outcome') in ('own', 'model_failed') for e in wakes))
        if own:
            continue
        failed = [e for e in wakes if e['type'] == 'wake_failed']
        fk = failure_kind(failed[-1].get('cause'), '') if failed else None
        if not failed:
            continue
        open_ = now < exp
        out.append(dict(type='trade_offer_gap', thread_id=th, team=to, entrant=league.entrant(to), deadline=exp.isoformat(), cause=failed[-1].get('cause'), failure_kind=fk, recovery_eligible=open_, unit='benchedmark-agent-tick.service', status='offer unanswered after our wake failures; resume wakes before expiry' if open_ else 'offer expired after our wake failures; infrastructure miss, no late reply'))
    return out

def pending_creation(now: datetime, cfgs=None, unit_state=None) -> list[dict]:
    if cfgs is None:
        from bm.cli import league_cfgs
        cfgs = league_cfgs()
    if unit_state is None:
        from bm.ops.picks_watch import unit_state
    out = []
    for c in cfgs:
        if c.get('mode') != 'agent' or not c.get('draft_as_of') or (LEAGUES / c['id'] / 'league.json').exists():
            continue
        due = datetime.fromisoformat(c['draft_as_of'])
        if not due - timedelta(hours=36) <= now:
            continue
        unit = _create_unit(c['id'])
        st = unit_state(unit) if unit else {'LoadState': 'not-found'}
        tst = unit_state(unit.replace('.service', '.timer')) if unit else {'LoadState': 'not-found'}
        armed = tst.get('ActiveState') == 'active' and st.get('LoadState') == 'loaded'
        late = now >= due
        out.append(dict(league=c['id'], issues=[dict(type='league_not_created', step='create', due=due.isoformat(), unit=unit, timer_armed=armed, service_state=st.get('ActiveState'), recovery_eligible=bool(late and unit and (st.get('LoadState') == 'loaded')), status='draft time passed and league absent; dispatch create' if late else 'create timer armed before draft' if armed else 'create unit/timer NOT armed before draft')]))
    return out

def _create_unit(league_id: str) -> str | None:
    from bm.ops.picks_watch import INSTALLED_UNITS
    for p in sorted(INSTALLED_UNITS.glob('benchedmark-create-*.service')):
        t = p.read_text()
        if 'agent-create' in t and f'--only {league_id}' in t:
            return p.name
    return None

def scan(now: datetime, lookback_hours: int=24, repair: bool=False) -> list[dict]:
    from bm.league.season import League, week_steps, now_utc
    from bm.agent import bots
    if not now.tzinfo:
        now = now.replace(tzinfo=timezone.utc)
    lookback = timedelta(hours=lookback_hours)
    window_start = now - lookback
    findings = []
    schedule = None
    step_cache: dict[int, list[tuple[str, datetime]]] = {}
    lane_floors = _lane_floors()

    def steps_for(week: int):
        if week not in step_cache:
            step_cache[week] = week_steps(week)
        return step_cache[week]
    for league_dir in sorted(LEAGUES.glob('*/')):
        if not (league_dir / 'league.json').exists():
            continue
        league_id = league_dir.name
        try:
            league = League(league_id)
        except Exception as exc:
            findings.append({'league': league_id, 'issues': [{'type': 'league_scan_failed', 'error': type(exc).__name__, 'status': 'unknown; not healthy'}]})
            continue
        if league.cfg.get('status') in ('ended', 'archived') or 'ended' in str(league.cfg.get('status', '')):
            continue
        events = league.events()
        issues = seat_gaps(events, now, lookback_hours)
        current_week = int(league.cfg.get('start_week', 1))
        for e in events:
            if e['type'] == 'week_advance':
                current_week = int(e.get('week', current_week))
        last_week = int(league.cfg.get('end_week') or league.rules.last_week)
        agent_mode = league.cfg.get('mode') == 'agent'
        try:
            has_draft = any((e['type'] == 'draft_complete' for e in events))
            if not has_draft:
                if schedule is None:
                    from bm.stats import nflverse as nv
                    schedule = nv.schedule(2026, as_of=now)
                critical_steps = []
            elif agent_mode:
                critical_steps = []
            else:
                critical_steps = []
                for week in (current_week, current_week + 1):
                    if week <= last_week:
                        critical_steps.extend(steps_for(week))
            issues.extend(critical_windows(league, events, now, schedule=schedule, steps=critical_steps))
        except Exception as exc:
            issues.append({'type': 'critical_readiness_unmeasured', 'error': type(exc).__name__, 'status': 'unknown; not healthy'})
        try:
            if league.cfg.get('mode') == 'agent':
                from bm.league.agentleague import AgentLeague
                alg = AgentLeague(league_id)
                issues.extend(draft_reconcile(alg, events, now))
                issues.extend(wake_reconcile(alg, events, now, repair=repair))
                issues.extend(offer_reconcile(alg, events, now))
            else:
                issues.extend(trade_reconcile(league, events, now, floors=lane_floors))
        except Exception as exc:
            issues.append({'type': 'reconciliation_unmeasured', 'error': f'{type(exc).__name__}: {exc}'[:200], 'status': 'unknown; not healthy'})
        last_event_time = None
        for e in reversed(events):
            if e.get('ts'):
                try:
                    last_event_time = datetime.fromisoformat(e['ts'].replace('Z', '+00:00'))
                    break
                except Exception:
                    pass
        has_draft = any((e['type'] == 'draft_complete' for e in events))
        last_scored = max([int(w) for e in events if e['type'] == 'week_scored' for w in [e.get('week')] if w is not None], default=0)
        end_week = league.cfg.get('end_week', 18)
        in_season = has_draft and (last_scored < end_week or last_scored == 0)
        draft_at = league.cfg.get('draft_as_of')
        if not has_draft and draft_at and (now > datetime.fromisoformat(draft_at) + GRACE):
            picks = [datetime.fromisoformat(e.get('logged_at') or e['ts']) for e in events if e['type'] == 'draft_pick']
            last_pick = max(picks) if picks else datetime.fromisoformat(draft_at)
            if now - last_pick > DRAFT_STALL:
                issues.append({'type': 'draft_stalled', 'picks': len(picks), 'last_pick': last_pick.isoformat(), 'hours': round((now - last_pick).total_seconds() / 3600, 1)})
        if in_season and last_event_time:
            silence_duration = now - last_event_time
            stalled = True
            try:
                if agent_mode:
                    from bm.agent.runtime import schedule_of
                    from bm.league.agentleague import faab_run_at
                    if schedule is None:
                        from bm.stats import nflverse as nv
                        schedule = nv.schedule(2026, as_of=now)
                    weeks = range(int(league.cfg.get('start_week', 1)), last_week + 1)
                    due = [datetime.fromisoformat(w['at']) for t in league.seats for w in schedule_of(events, t)['wakes']]
                    due += [faab_run_at(w) for w in weeks]
                    due += [datetime.fromisoformat(g['kickoff']) for g in schedule if g['week'] in weeks]
                else:
                    due = [d for w in (current_week, current_week + 1) if w <= last_week for _, d in steps_for(w)]
                stalled = any((last_event_time < d and d + GRACE <= now for d in due))
            except Exception:
                stalled = True
            if stalled and silence_duration > timedelta(hours=SILENT_HOURS):
                issues.append({'type': 'league_silent', 'hours': round(silence_duration.total_seconds() / 3600, 1), 'last_event_time': last_event_time.isoformat()})
        for week in [w for w in (current_week, max(1, current_week - 1)) if w <= last_week and (not agent_mode)]:
            try:
                steps = steps_for(week)
            except Exception:
                continue
            open_windows = {i['step'] for i in issues if i.get('type') == 'critical_route_readiness' and i.get('window_open')}
            for step_key, scheduled_time in steps:
                if not has_draft or not any((step_key.startswith(p) for p in ['trade:', 'waiver_bids:', 'lineup:'])):
                    continue
                if step_key in open_windows:
                    continue
                if step_key.startswith('lineup:'):
                    parts = step_key.split(':')
                    if len(parts) >= 3:
                        try:
                            n = int(parts[2])
                            if n >= 2:
                                continue
                        except (ValueError, IndexError):
                            pass
                if scheduled_time < window_start:
                    continue
                if scheduled_time > now:
                    continue
                drafted = next((datetime.fromisoformat(e['logged_at']) for e in events if e['type'] == 'draft_complete' and e.get('logged_at')), None)
                start = max(scheduled_time, drafted) if drafted else scheduled_time
                past_due = now > start + GRACE
                step_done = any((e['type'] == 'step_done' and e.get('step') == step_key for e in events))
                if not past_due and (not step_done):
                    continue
                decision_markers = _decision_markers(step_key, league_id, week, league.seats)
                if not step_done and _step_in_progress(step_key, scheduled_time, steps, decision_markers, events, now):
                    continue
                step_done = any((e['type'] == 'step_done' and e.get('step') == step_key for e in events))
                if not step_done:
                    issues.append({'type': 'step_past_due_no_done', 'step': step_key, 'scheduled_time': scheduled_time.isoformat(), 'grace_expired': (now - (scheduled_time + GRACE)).total_seconds() / 3600, 'hours_overdue': round((now - scheduled_time).total_seconds() / 3600, 1)})
                else:
                    from bm import succession
                    model_seats = [tid for tid, seat in league.seats.items() if seat.get('entrant_id') not in bots.ALL and succession.controller(seat.get('entrant_id'), league=league_id, week=week) not in bots.ALL]
                    if model_seats and decision_markers:
                        recorded = {e.get('decision_id') for e in events if e['type'] in ('decision', 'decision_pending', 'decision_missed', 'decision_expected', 'decision_skipped_infra')}
                        missing = [tid for tid in model_seats if not any((f'{marker}:{tid}' in recorded for marker in decision_markers))]
                        if missing:
                            issues.append({'type': 'step_done_partial_seats', 'step': step_key, 'scheduled_time': scheduled_time.isoformat(), 'missing_teams': missing, 'model_seats': len(model_seats), 'recovery_eligible': False, 'status': 'missing original seat eligibility receipt; operator review, not inferred replay'})
        if issues:
            findings.append({'league': league_id, 'issues': issues})
    try:
        findings.extend(pending_creation(now))
    except Exception as exc:
        findings.append({'league': '_full-game-creation', 'issues': [{'type': 'reconciliation_unmeasured', 'error': type(exc).__name__, 'status': 'unknown; not healthy'}]})
    return findings

def seat_gaps(events: list[dict], now: datetime, lookback_hours: int=24) -> list[dict]:
    expected, last, answers, holds = ({}, {}, {}, {})
    for e in events:
        did = e.get('decision_id')
        if not did:
            continue
        if e.get('hold_until'):
            holds[did] = e['hold_until']
        if e['type'] == 'decision_expected':
            expected[did] = e
        elif e['type'] in ('decision_pending', 'decision_missed', 'decision_skipped_infra'):
            last[did] = e
        elif e['type'] == 'decision':
            answers[did] = e
    voided = {e.get('decision_id') for e in events if e['type'] == 'decision_voided'}
    gaps = []
    for did in sorted(set(expected) | set(last) | {k for k, v in answers.items() if v.get('forced_default')}):
        if did in voided:
            continue
        e = last.get(did) or expected.get(did) or answers[did]
        answer = answers.get(did)
        if answer and (not answer.get('forced_default')):
            continue
        deadline = (expected.get(did) or {}).get('hold_until') or holds.get(did)
        cutoff = datetime.fromisoformat(deadline) if deadline else None
        stamp = e.get('logged_at') or e.get('ts')
        if cutoff and cutoff < now - timedelta(hours=lookback_hours):
            continue
        if not cutoff and stamp and (datetime.fromisoformat(stamp) < now - timedelta(hours=lookback_hours)):
            continue
        closed = cutoff is not None and now >= cutoff
        protocol = (expected.get(did) or e).get('retry') or {}
        supported = protocol.get('kind') in ('lineup', 'waiver_bid', 'book')
        trade_closed = protocol.get('kind') == 'trade' and any((x['type'] == 'step_done' and x.get('step') == f'trade:{protocol['week']}:{protocol['n']}' for x in events))
        closed = closed or trade_closed
        if not closed and did not in last and (not answer) and stamp and (now - datetime.fromisoformat(stamp) < IN_FLIGHT):
            continue
        repairable = supported and (not answer) and (cutoff is not None) and (not closed) and (e['type'] != 'decision_missed')
        gaps.append({'type': 'seat_decision_gap', 'decision_id': did, 'team': e.get('team'), 'entrant': e.get('entrant'), 'cause': answer.get('forced_default') if answer else e.get('cause'), 'deadline': deadline, 'recovery_eligible': repairable, 'last_attempt': (last.get(did) or {}).get('logged_at'), 'status': ('infra default (gateway failure, not a model action); terminal, no automatic replacement' if any((str(v).startswith('gateway:') for v in (answer.get('validation') or {}).get('violations') or [])) else 'model default terminal; no automatic replacement') if answer else 'closed gap; no late backfill' if closed or e['type'] == 'decision_missed' else 'held/missing before lock; ordinary scheduler retry' if repairable else 'draft pick open before the first lock; the draft loop retries it' if protocol.get('kind') == 'draft' and cutoff is not None else 'unmeasured retry deadline; operator repair required'})
    return gaps

def _decision_markers(step_key: str, league_id: str, week: int, seats: dict) -> list[str]:
    markers = []
    if step_key.startswith('draft'):
        markers = [f'{league_id}:w{week}:draft']
    elif step_key.startswith('waiver_bids:'):
        markers = [f'{league_id}:w{week}:waivers']
    elif step_key.startswith('trade:'):
        parts = step_key.split(':')
        if len(parts) >= 3:
            markers = [f'{league_id}:w{week}-t{parts[2]}:offer']
    elif step_key.startswith('lineup:'):
        parts = step_key.split(':')
        if len(parts) >= 3:
            n = parts[2]
            markers = [f'{league_id}:w{week}:lineup{n}']
    return markers

def _step_in_progress(step_key: str, scheduled_time: datetime, steps, markers: list[str], events: list[dict], now: datetime) -> bool:
    following = [t for k, t in steps if t > scheduled_time and k != step_key]
    if not markers or not following or now >= min(following):
        return False
    for e in reversed(events):
        if not str(e.get('type', '')).startswith(('decision', 'memory')) or not e.get('logged_at'):
            continue
        if not any((str(e.get('decision_id') or '').startswith(m + ':') for m in markers)):
            continue
        try:
            return now - datetime.fromisoformat(e['logged_at']) < IN_FLIGHT
        except (TypeError, ValueError):
            continue
    return False

def _retrying_early(issue: dict, now: datetime) -> bool:
    from bm.league.season import LOCK_WAKE, RETRY_CAP_S
    cycle = timedelta(seconds=RETRY_CAP_S) + timedelta(minutes=10) + IN_FLIGHT
    try:
        return bool(issue.get('last_attempt') and now - datetime.fromisoformat(issue['last_attempt']) < cycle and (datetime.fromisoformat(issue['deadline']) - now > LOCK_WAKE))
    except (TypeError, ValueError):
        return False

def alert_summary(findings: list[dict], now: datetime) -> str | None:
    issues_by_type = {}
    for f in findings:
        for issue in f['issues']:
            if issue['type'] == 'critical_route_readiness' or (issue['type'] == 'seat_decision_gap' and (not issue.get('recovery_eligible') or _retrying_early(issue, now))):
                continue
            if issue['type'] in RECONCILED or issue['type'] in ('trade_reconciliation', 'draft_reconciliation'):
                continue
            t = issue['type']
            issues_by_type[t] = issues_by_type.get(t, 0) + 1
    summary_parts = [f'{v} {k.replace('_', ' ')}' for k, v in sorted(issues_by_type.items())]
    return 'BenchedMark no-decisions: ' + ', '.join(summary_parts) if summary_parts else None

def main() -> int:
    import argparse
    import sys
    parser = argparse.ArgumentParser()
    parser.add_argument('--now', help='ISO datetime (default: now UTC)')
    parser.add_argument('--repair', action='store_true', help='Dispatch cutoff-eligible existing services; no probes')
    parser.add_argument('--alert', action='store_true', help='Send alert if issues found')
    parser.add_argument('--compact-output', action='store_true', help='Print a bounded receipt; full commissioner evidence remains in state files')
    args = parser.parse_args()
    if args.now:
        try:
            now = datetime.fromisoformat(args.now.replace('Z', '+00:00'))
        except Exception as e:
            print(f'Invalid --now: {e}', file=sys.stderr)
            return 1
    else:
        now = datetime.now(timezone.utc)
    findings = scan(now, 24, args.repair)
    from bm.ops import picks_watch
    memo = {}
    picks = picks_watch.check(now, repair=args.repair, notify=args.alert, memo=memo)
    services = set()
    if args.repair:
        from bm.ops.picks_watch import readiness
        for f in findings:
            gaps = [i for i in f['issues'] if i.get('type') == 'seat_decision_gap' and i.get('recovery_eligible') and i.get('entrant')]
            if gaps:
                f['readiness'] = readiness(sorted({i['entrant'] for i in gaps}), min((datetime.fromisoformat(i['deadline']) for i in gaps)), memo=memo)
                if any((v.get('eligible') for v in f['readiness'].values())):
                    services.add('benchedmark-tick.service')
            for item in f['issues']:
                if item.get('type') == 'critical_route_readiness' and item.get('preflight_eligible'):
                    item['readiness'] = readiness(item['entrants'], datetime.fromisoformat(item['deadline']), memo=memo)
                    item['service_state'] = picks_watch.unit_state(item['unit'])
                    item['service_ready'] = item['service_state'].get('LoadState') == 'loaded'
                    if item['service_ready'] and item.get('recovery_eligible') and any((v.get('eligible') for v in item['readiness'].values())):
                        services.add(item['unit'])
                if item.get('type') in RECONCILED and item.get('recovery_eligible') and item.get('unit'):
                    if item.get('entrant') and item.get('deadline'):
                        item['readiness'] = readiness([item['entrant']], datetime.fromisoformat(item['deadline']), memo=memo)
                        if not any((v.get('eligible') for v in item['readiness'].values())):
                            item['status'] += '; every route blocked by preflight (capacity/config): dispatch withheld, owner notified'
                            continue
                    services.add(item['unit'])
    dispatch = []
    for service in sorted(services):
        import subprocess
        status = picks_watch.unit_state(service)
        if status.get('LoadState') != 'loaded' or status.get('ActiveState') in ('active', 'activating'):
            dispatch.append({'service': service, 'status': 'not dispatched; unavailable or already running', 'unit_state': status})
            continue
        p = subprocess.run(['systemctl', '--user', 'start', '--no-block', service], capture_output=True, text=True, timeout=10)
        dispatch.append({'service': service, 'status': 'requested, not completed' if p.returncode == 0 else 'failed'})
    state = ROOT / 'state/commissioner-leagues.json'
    try:
        previous = {f['league']: f for f in json.loads(state.read_text()).get('findings', [])}
    except (OSError, ValueError, KeyError, TypeError):
        previous = {}
    for finding in findings:
        prior = previous.get(finding['league'], {})
        for key in ('notification_fingerprint', 'notification_delivered'):
            if key in prior:
                finding[key] = prior[key]
        for item in finding['issues']:
            old_item = next((i for i in prior.get('issues', []) if i.get('step') == item.get('step') and i.get('type') == item.get('type')), {})
            for key in ('notification_fingerprint', 'notification_delivered'):
                if key in old_item:
                    item[key] = old_item[key]
    if args.alert:
        from bm.ops.alert import wake
        for finding in findings:
            for item in finding['issues']:
                if item.get('type') != 'critical_route_readiness':
                    continue
                blocked = [m for m, v in item.get('readiness', {}).items() if not v.get('eligible')]
                terminal = not item.get('preflight_eligible') or item.get('operator_blocked') or item['status'].startswith('partial trade batch')
                if not blocked and (not terminal) and (item.get('service_ready') is not False):
                    continue
                old_item = next((i for i in previous.get(finding['league'], {}).get('issues', []) if i.get('step') == item['step'] and i.get('type') == item['type']), {})
                picks_watch.notify_changed(item, old_item, 'commissioner-critical|' + finding['league'] + '|' + item['step'], f'BenchedMark critical readiness {finding['league']} {item['step']}: {item['status']}; deadline {item['deadline']}; blocked/unknown routes {blocked}. Existing service {item['unit']} ready {item.get('service_ready', 'unmeasured')}; recovery eligible {item['recovery_eligible']}. Inspect state/commissioner-leagues.json. Repair before ORIGINAL cutoff; no late/partial trade replay.', terminal=terminal, evidence=dict(step=item['step'], deadline=item['deadline'], status=item['status'], blocked=blocked, service_ready=item.get('service_ready')))
            ours = [i for i in finding['issues'] if i.get('type') in RECONCILED and i.get('type') != 'draft_reconciliation' and (not (i.get('type') == 'league_not_created' and i.get('timer_armed') and (not i.get('recovery_eligible'))))]
            draft = [i for i in finding['issues'] if i.get('type') == 'draft_reconciliation' and (i.get('infra_defaults') or i.get('recovery_eligible') or i['status'].startswith('closed') or ((i.get('current_pause') or {}).get('since') and now - datetime.fromisoformat(i['current_pause']['since']) >= timedelta(minutes=30)))]
            if ours or draft:
                active = any((i.get('recovery_eligible') for i in ours + draft))
                kinds = {}
                for i in ours:
                    kinds[i.get('failure_kind')] = kinds.get(i.get('failure_kind'), 0) + 1
                picks_watch.notify_changed(finding.setdefault('reconciliation_notice', {}), previous.get(finding['league'], {}).get('reconciliation_notice', {}), 'commissioner-reconcile|' + finding['league'], f'BenchedMark commissioner {finding['league']}: {len(ours)} trade/wake/create gaps on our side by kind {kinds}, {sum((bool(i.get('recovery_eligible')) for i in ours + draft))} open before their original cutoff and dispatched; ' + (f'draft {draft[0]['status']}, {draft[0]['picks']}/{draft[0]['total']} picks, infra defaults {draft[0]['infra_defaults']}, board paused on {draft[0]['current_pause']}; ' if draft else '') + 'Inspect state/commissioner-leagues.json. ' + ('Fix the failing route before the cutoff.' if active else 'Closed: no late replay.'), terminal=not active, evidence=[(i.get('decision_id') or i.get('thread_id') or i.get('wake_id') or i.get('step'), i.get('failure_kind'), i.get('recovery_eligible')) for i in ours + draft])

            def _model_cause(g):
                c = g.get('cause')
                c = c.get('cause') if isinstance(c, dict) else c
                return c in ('PARSE', 'REFUSED', 'ILLEGAL') or str(g.get('status', '')).startswith('model default')
            gaps = [i for i in finding['issues'] if i.get('type') == 'seat_decision_gap' and (not i.get('recovery_eligible')) and (not _model_cause(i)) and (not str(i.get('status', '')).startswith('draft pick open'))]
            if gaps:
                active = any((g['recovery_eligible'] for g in gaps))
                picks_watch.notify_changed(finding, previous.get(finding['league'], {}), 'commissioner-league|' + finding['league'], f'BenchedMark commissioner {finding['league']}: {len(gaps)} seat gaps, {sum((bool(g['recovery_eligible']) for g in gaps))} cutoff-eligible for ordinary scheduler. Inspect state/commissioner-leagues.json; ' + ('fix routes before recorded locks. No retrospective decisions.' if active else 'closed/unsupported recovery diagnostic retained; no retrospective decisions.'), terminal=not active, evidence=gaps)
    if args.alert and findings:
        from bm.ops.alert import alert
        summary = alert_summary(findings, now)
        if summary:
            alert('no-decisions', summary)
    state.parent.mkdir(parents=True, exist_ok=True)
    temp = state.with_suffix('.tmp')
    temp.write_text(json.dumps({'observed_at': now.isoformat(), 'findings': findings, 'dispatch': dispatch}, indent=2))
    temp.replace(state)
    successor = None
    if args.alert and (not args.now):
        try:
            from bm.ops import successorwatch
            successor = successorwatch.check(now)
        except Exception as e:
            successor = {'error': repr(e)[:200]}
    if args.compact_output:
        successor_status = 'not_checked' if successor is None else 'error' if isinstance(successor, dict) and successor.get('error') else 'checked'
        print(json.dumps({'status': 'complete', 'commissioner_leagues': str(state), 'commissioner_picks': str(ROOT / 'state/commissioner-picks.json'), 'league_findings': len(findings), 'league_issues': sum((len(f.get('issues', ())) for f in findings)), 'picks_opportunities': len(picks), 'picks_gaps': sum((len(p.get('gaps', ())) for p in picks)), 'dispatch_requests': len(dispatch), 'successor_watch': successor_status}, sort_keys=True))
    else:
        print(json.dumps({'leagues': findings, 'picks': picks, 'league_dispatch': dispatch, 'successor_watch': successor}, indent=2, default=str))
    return 0
if __name__ == '__main__':
    import sys
    sys.exit(main())
