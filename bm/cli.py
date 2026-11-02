from __future__ import annotations
import argparse
from zoneinfo import ZoneInfo
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
import yaml
from bm.gateway.core import Gateway
from bm.league.season import LEAGUES, League, iso, now_utc, week_steps
from bm.stats import nflverse as nv
ROOT = Path(__file__).resolve().parents[1]
CFG = ROOT / 'config' / 'leagues-nfl-proto.yaml'
STATUS = Path.home() / '.cache' / 'status' / 'benchedmark.json'
FILE_KEYS = ('draft_as_of', 'start_week', 'end_week', 'rules_file', 'format', 'status', 'draft_pick_window_s', 'observation_book_from', 'standing_lineup', 'prompt_template', 'freeze_manifest', 'quota_reserve')

def cfg():
    return yaml.safe_load(CFG.read_text())

def config_files() -> list[Path]:
    return [CFG] + sorted((p for p in (ROOT / 'config').glob('leagues-nfl-*.yaml') if p != CFG))

def league_cfgs() -> list[dict]:
    out = []
    for f in config_files():
        try:
            c = yaml.safe_load(f.read_text()) or {}
        except Exception as e:
            print(f'league config {f.name} unreadable, skipped: {e}', flush=True)
            continue
        for l in c.get('leagues', []) or []:
            if not isinstance(l, dict) or not l.get('id'):
                print(f'league config {f.name}: entry without an id skipped', flush=True)
                continue
            l = dict(l)
            if 'seats' not in l:
                if 'models' in l:
                    l['seats'] = list(l['models'] or []) + list(l.get('extra_models') or []) + list(l.get('bots') or [])
                else:
                    print(f'league config {f.name}: {l['id']} has no seats/models', flush=True)
                    l['seats'] = []
            out.append({**{k: c[k] for k in FILE_KEYS if k in c}, **l})
    return out

def seat_entrants(l: dict) -> list[str]:
    return [x if isinstance(x, str) else x.get('entrant') for x in l.get('seats') or [] if x]

def league_ids(only=None):
    ids = [l['id'] for l in league_cfgs()]
    ids += sorted((d.name for d in LEAGUES.glob('*') if (d / 'league.json').exists() and d.name not in ids))
    return [i for i in ids if not only or i in only]

def create(only=None):
    from bm.engine.rules import load_rules
    from bm.league.finals import draft_order
    from bm.league.season import template_module
    for l in league_cfgs():
        if only and l['id'] not in only:
            continue
        if l.get('mode') == 'agent':
            continue
        rules_file = l.get('rules_file', 'rules/nfl-s1.yaml')
        rules = load_rules(rules_file)
        d = LEAGUES / l['id']
        if (d / 'league.json').exists():
            print('exists', l['id'])
            continue
        if not l.get('seats'):
            print('not created (no seats in config):', l['id'], flush=True)
            continue
        seed = hashlib.sha256(l['id'].encode()).hexdigest()[:12]
        seats = []
        for i, e in enumerate(l['seats']):
            tid = f'{l['id']}-t{i + 1:02d}'
            clone = any((s['entrant_id'] == e for s in seats))
            seats.append({'team_id': tid, 'entrant_id': e, 'variant': 'standard', 'operator': 'entrant', **({'clone_of': next((s['team_id'] for s in seats if s['entrant_id'] == e))} if clone else {})})
        if 'draft_order' in l and l['draft_order'] is None:
            raise ValueError(f'{l['id']}: draft_order must list each league team ID exactly once')
        order = draft_order([s['team_id'] for s in seats], seed, l.get('draft_order'))
        tpl_version = template_module(l['prompt_template']).TEMPLATE_VERSION if l.get('prompt_template') else None
        d.mkdir(parents=True, exist_ok=True)
        lc = {'id': l['id'], 'label': l['label'], 'sport': 'nfl', 'division': l['division'], 'disclosure': l['disclosure'], 'block': l['block'], 'rules_file': rules_file, 'rules_hash': rules.rules_hash, 'season': 2026, 'start_week': l['start_week'], 'end_week': l.get('end_week', rules.last_week), 'format': l.get('format', 'season'), 'draft_as_of': l['draft_as_of'], 'seed': seed, 'seats': seats, 'draft_order': order, 'created_at': now_utc().isoformat(), 'status': l.get('status', 'private-prototype'), **({'step_offset_min': int(l['step_offset_min'])} if l.get('step_offset_min') else {}), **({'draft_pick_window_s': int(l['draft_pick_window_s'])} if l.get('draft_pick_window_s') else {}), **({'standing_lineup': True} if l.get('standing_lineup') else {}), **({'prompt_template': tpl_version} if tpl_version else {}), 'draft_week_trade_1': 'skip'}
        (d / 'league.json').write_text(json.dumps(lc, indent=1))
        lg = League(l['id'])
        lg.log.append({'type': 'genesis', 'ts': l['draft_as_of'], 'league_id': l['id'], 'team_ids': [s['team_id'] for s in seats], 'draft_order': order, 'week': l['start_week'], 'rules_hash': rules.rules_hash, 'rules_version': rules.version, 'seats': seats, 'data': nv.manifest()[-6:], 'logged_at': now_utc().isoformat()})
        print('created', l['id'], order)

def _gw():
    return Gateway()

def capacity_gate(work, label: str, gw, force: bool=False) -> bool:
    from bm.ops import capacity
    from bm.ops.alert import wake
    try:
        rep = capacity.preflight(work, label, gw=gw)
    except Exception as e:
        print(f'capacity preflight failed ({e}); continuing', flush=True)
        return True
    print('\n'.join(rep.lines), flush=True)
    print(rep.summary(), flush=True)
    capacity.alert_odds(rep, label)
    if rep.ok:
        capacity.alert_timeouts(rep, label)
        return True
    wake('capacity|' + label.split()[0] + '|' + '|'.join(sorted((' '.join(x.split()[:2]) for x in rep.short))), rep.summary() + ('\nStarting anyway (--force).' if force else "\nNot started. Raise the money cap with the operator OK (config/lanes.yaml applies without a restart), then rerun; or rerun with --force to start and let that seat pause when the cap is hit."))
    capacity.alert_timeouts(rep, label)
    return force

def seat_reserve(l: dict) -> bool:
    q = l.get('quota_reserve')
    return bool(isinstance(q, dict) and q.get('seats'))

def missing_manifests(ids) -> list[str]:
    from bm.gateway.core import ROOT as RUNTIME
    out = []
    for l in league_cfgs():
        path = l.get('freeze_manifest')
        if l['id'] not in ids or not path or (not (LEAGUES / l['id'] / 'league.json').exists()) or League(l['id']).done('draft'):
            continue
        if not (Path(path) if Path(path).is_absolute() else RUNTIME / path).exists():
            out.append(f'{l['id']}: {path}')
    return out

def draft(only=None, force: bool=False):
    ids = league_ids(only)
    missing = missing_manifests(ids)
    if missing:
        print('draft not started: no freeze manifest for ' + '; '.join(missing) + ' (record it: python3 -m bm.ops.freeze record --leagues ... --out <path>)', flush=True)
        sys.exit(4)
    gw = _gw()
    from bm.ops import capacity
    if not capacity_gate(capacity.draft_work(league_ids(only)), f'draft {','.join(only or ['all'])}', gw, force):
        sys.exit(3)

    def go(i):
        lg = League(i, gw)
        as_of = datetime.fromisoformat(lg.cfg['draft_as_of'])
        if as_of > now_utc():
            print(i, 'draft not due until', lg.cfg['draft_as_of'], flush=True)
            return
        lg.clock = now_utc
        with lg.exclusive():
            try:
                lg.run_draft(as_of)
            finally:
                priority.draft_paused(i, priority.DONE)
    from bm import priority
    ths = [threading.Thread(target=go, args=(i,)) for i in league_ids(only) if (LEAGUES / i / 'league.json').exists()]
    seats = any((seat_reserve(l) for l in league_cfgs() if l['id'] in ids))
    with priority.hold(seats=seats, leagues=ids):
        [t.start() for t in ths]
        [t.join() for t in ths]

def end_week(lg: League) -> int:
    return int(lg.cfg.get('end_week') or lg.rules.last_week)

def run_league_steps(lg: League, now: datetime, replay_week: int | None=None, clock=None) -> list[str]:
    if replay_week is not None:
        if not lg.cfg.get('replay'):
            raise RuntimeError(f'{lg.id}: replay refused; only a league created for replay (league.json "replay": true) may be replayed, never a live league')
        return _run_steps(lg, now, replay_week)
    if not lg.done('draft'):
        return []
    ran = [f'rescore:{w}' for w in lg.rescore_due(now)]
    return ran + _run_live(lg, now, clock or now_utc)

def _book_week(lg: League, now: datetime) -> int | None:
    from bm.agent.book import slate_time
    frm = lg.cfg.get('observation_book_from') or next((l.get('observation_book_from') for l in league_cfgs() if l['id'] == lg.id), None)
    if not frm:
        return None
    week = lg.state()['week']
    first = min((datetime.fromisoformat(g['kickoff']) for g in nv.schedule(2026) if g['week'] == week))
    if week < int(frm) or week > end_week(lg) or lg.done(f'book:{week}') or (not slate_time(week) <= now < first):
        return None
    if lg.book_backoff_until(week, now) is not None:
        return None
    return week

def _observation_book(lg: League, now: datetime) -> list[str]:
    week = _book_week(lg, now)
    if week is None:
        return []
    lg.observation_book(week, now)
    return [f'book:{week}']

def draft_week_trade_skipped(lg: League, week: int, key: str) -> bool:
    if not (key.endswith(':1') and week == lg.cfg['start_week']) or lg.cfg.get('draft_week_trade_1') != 'skip':
        return False
    window = f'w{week}-t1'
    return not any((e.get('window') == window or f':{window}:' in str(e.get('decision_id') or '') for e in lg.events()))
_PRIO = {'waiver_run': 0, 'score': 0, 'lineup': 1, 'retry': 2, 'waiver_bids': 3, 'book': 4, 'trade': 5}
_FAR = datetime.max.replace(tzinfo=timezone.utc)

def _live_work(lg: League, now: datetime) -> list[tuple]:
    items = []
    week = lg.state()['week']
    if week <= end_week(lg):
        kicks = sorted({datetime.fromisoformat(g['kickoff']) for g in nv.schedule(2026) if g['week'] == week})
        nxt = lambda t: next((k for k in kicks if k > t), None)
        off = timedelta(minutes=int(lg.cfg.get('step_offset_min') or 0))
        steps = week_steps(week)
        held = False
        for i, (key, due) in enumerate(steps):
            if lg.done(key):
                continue
            eff = due + off
            if eff > now:
                break
            kind = key.split(':')[0]
            if kind != 'lineup':
                if held or (kind == 'score' and any((not lg.done(k) for k, _ in steps[:i]))):
                    held = True
                    continue
                held = True
            if kind == 'score':
                dl = eff
            elif kind == 'waiver_bids':
                run_at = next((d for k, d in steps if k == f'waiver_run:{week}')) + off
                dl = run_at if run_at > now else nxt(now)
            else:
                dl = nxt(max(eff, now))
            items.append((dl or _FAR, _PRIO[kind], eff, key, kind, week))
        bw = _book_week(lg, now)
        if bw is not None:
            items.append((kicks[0] if kicks else _FAR, _PRIO['book'], now, f'book:{bw}', 'book', bw))
    _, due = lg.pending_due(now)
    for p in due + lg.expected_missing(now):
        items.append((datetime.fromisoformat(p['hold_until']), _PRIO['retry'], datetime.fromisoformat(p.get('next_try_at') or p['logged_at']), f'retry:{p['decision_id']}', 'retry', p))
    return sorted(items, key=lambda x: x[:4])

def _run_item(lg: League, item: tuple, at: datetime) -> bool:
    _, _, _, key, kind, arg = item
    if kind == 'retry':
        lg.retry_one(arg, at)
        return True
    if kind == 'book':
        lg.observation_book(arg, at)
        return True
    week = arg
    if kind == 'trade':
        if draft_week_trade_skipped(lg, week, key):
            lg.append({'type': 'step_done', 'ts': iso(at), 'step': key, 'note': 'skipped: draft day'})
        else:
            lg.trade_window(week, int(key.split(':')[2]), at)
            return lg.done(key)
    elif kind == 'waiver_bids':
        lg.waiver_bids(week, at)
    elif kind == 'waiver_run':
        lg.waiver_run(week, at)
    elif kind == 'lineup':
        lg.lineup_window(week, int(key.split(':')[2]), at)
    elif kind == 'score':
        lg.record_locks(week, at)
        if not lg.score(week, at):
            return False
    lg.record_locks(week, at)
    return True
BOOK_POLL_S = 30
STEP_MAX_S = {'trade': 3 * 3600, 'book': 3600}
STEP_MAX_DEFAULT_S = 3600
TRADE_WINDOW_H = 6
TRADE_PAST_CUTOFF_S = 1800
TICK_UNIT_TIMEOUT_S = 6 * 3600
TICK_ENDS_AT: datetime | None = None
HUNG: list = []
HUNG_EXIT = 3
HANDED_OFF: list = []
TICK_UNIT = 'benchedmark-tick.service'
TICK_ALERTED = False

class StepHung(Exception):
    pass

class StepHandedOff(Exception):
    pass

def _abandon(lg: League, t: threading.Thread, key: str, limit: float) -> None:
    lg._hung = t
    HUNG.append(f'{lg.id}:{key}')
    raise StepHung(f'{lg.id} {key} still running after {limit:.0f}s: abandoned (hang guard). The league stays held by the hung step this tick; the tick exits without waiting for it and the next tick resumes the league.')

def _hand_off(lg: League, t: threading.Thread, key: str, limit: float, own: float) -> None:
    lg._hung = t
    HANDED_OFF.append(f'{lg.id}:{key}')
    raise StepHandedOff(f'{lg.id} {key} still running when the tick ended after {limit:.0f}s (its own limit {own:.0f}s): handed off, not hung. The league stays held by it until this tick exits; the next tick resumes it.')

def _step_limit(lg: League, item: tuple, at: datetime, tick_end: bool=True) -> float:
    key, kind = (item[3], item[4])
    limit = STEP_MAX_S.get(kind, STEP_MAX_DEFAULT_S)
    if kind == 'trade':
        due = next((d for k, d in week_steps(item[5]) if k == key), None)
        if due is not None:
            cutoff = due + timedelta(minutes=int(lg.cfg.get('step_offset_min') or 0), hours=TRADE_WINDOW_H)
            limit = max(limit, (cutoff - at).total_seconds() + TRADE_PAST_CUTOFF_S)
    if tick_end and TICK_ENDS_AT is not None:
        limit = min(limit, max(60.0, (TICK_ENDS_AT - at).total_seconds()))
    return limit

def _guarded(lg: League, item: tuple, at: datetime) -> bool:
    key, kind = (item[3], item[4])
    limit = _step_limit(lg, item, at)
    box: dict = {}

    def run():
        try:
            box['r'] = _run_item(lg, item, at)
        except BaseException as e:
            box['e'] = e
    t = threading.Thread(target=run, name=f'{lg.id}-{key}', daemon=True)
    t.start()
    t.join(limit)
    if t.is_alive():
        own = _step_limit(lg, item, at, tick_end=False)
        if limit < own:
            _hand_off(lg, t, key, limit, own)
        _abandon(lg, t, key, limit)
    if 'e' in box:
        raise box['e']
    return box.get('r')

def _run_live(lg: League, now: datetime, clock, sleep=None) -> list[str]:
    import time
    sleep = sleep or time.sleep
    lg.clock = clock
    ran, tried = ([], set())
    at = now
    book, book_err = (None, [])

    def run_book(it, t):
        try:
            _run_item(lg, it, t)
        except BaseException as e:
            book_err.append(e)
    try:
        for _ in range(5000):
            if drain_requested(clock()):
                print(f'{lg.id}: release {drain_requested(clock())} staged; draining (no new work this pass)', flush=True)
                break
            items = [it for it in _live_work(lg, at) if it[3] not in tried]
            if not items:
                if book is None or not book.is_alive():
                    break
                sleep(BOOK_POLL_S)
                at = max(at, clock())
                continue
            it = items[0]
            tried.add(it[3])
            if it[4] == 'book':
                book = threading.Thread(target=run_book, args=(it, at), name=f'{lg.id}-book', daemon=True)
                book.start()
                ran.append(it[3])
            elif _guarded(lg, it, at):
                ran.append(it[3])
            at = max(at, clock())
    finally:
        if book is not None:
            book.join(STEP_MAX_S['book'])
            if book.is_alive():
                _abandon(lg, book, 'book', STEP_MAX_S['book'])
    if book_err:
        raise book_err[0]
    return ran

def _run_steps(lg: League, now: datetime, replay_week: int | None=None) -> list[str]:
    ran = []
    for _ in range(3):
        if not lg.done('draft'):
            return ran
        week = lg.state()['week']
        if week > end_week(lg):
            return ran
        if replay_week is not None and week != replay_week:
            return ran
        for key, due in week_steps(week):
            if lg.done(key):
                continue
            if replay_week is None and due + timedelta(minutes=int(lg.cfg.get('step_offset_min') or 0)) > now:
                return ran
            at = now if replay_week is None or key.startswith('score') else due
            kind = key.split(':')[0]
            if kind == 'trade':
                if replay_week is not None and key.endswith(':1') and (week == lg.cfg['start_week']):
                    lg.append({'type': 'step_done', 'ts': iso(at), 'step': key, 'note': 'skipped: draft day'})
                    continue
                lg.trade_window(week, int(key.split(':')[2]), at)
            elif kind == 'waiver_bids':
                lg.waiver_bids(week, at)
            elif kind == 'waiver_run':
                lg.waiver_run(week, at)
            elif kind == 'lineup':
                lg.lineup_window(week, int(key.split(':')[2]), at)
            elif kind == 'score':
                lg.record_locks(week, now)
                if not lg.score(week, now):
                    return ran
            ran.append(key)
            lg.record_locks(week, at)
        if replay_week is not None:
            return ran
    return ran

def replay_week(week: int, only=None):
    gw = _gw()
    now = now_utc()

    def go(i):
        lg = League(i, gw)
        with lg.exclusive():
            print(i, run_league_steps(lg, now, replay_week=week), flush=True)
    ths = [threading.Thread(target=go, args=(i,)) for i in league_ids(only)]
    [t.start() for t in ths]
    [t.join() for t in ths]

def _run_lock_held(league_dir: Path) -> bool:
    import fcntl
    try:
        with open(league_dir / 'run.lock', 'a') as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(f, fcntl.LOCK_UN)
            return False
    except OSError:
        return False

def _due_model_scan(now: datetime, only=None, gw=None, include_nonseat: bool=True) -> tuple[list[datetime], list[datetime], list[League]]:
    out, seat_out, leagues = ([], [], [])
    for l in league_cfgs():
        if only and l['id'] not in only:
            continue
        reserved = seat_reserve(l)
        if not include_nonseat and (not reserved):
            continue
        try:
            if not (LEAGUES / l['id'] / 'league.json').exists():
                due = datetime.fromisoformat(l['draft_as_of'])
                out.append(due)
                if reserved:
                    seat_out.append(due)
                continue
            if l.get('mode') == 'agent':
                continue
            lg = League(l['id']) if gw is None else League(l['id'], gw)
            leagues.append(lg)
            events = lg.events()
            done = {e['step'] for e in events if e['type'] == 'step_done'}
            due = None
            if 'draft' not in done:
                if not _run_lock_held(LEAGUES / l['id']):
                    due = datetime.fromisoformat(lg.cfg['draft_as_of'])
            else:
                week = int(lg.cfg['start_week'])
                for event in events:
                    if event['type'] == 'week_advance':
                        nxt = int(event['week'])
                        if nxt != week + 1:
                            raise ValueError(f'BAD_WEEK {week} -> {nxt}')
                        week = nxt
                if week <= end_week(lg):
                    off = timedelta(minutes=int(lg.cfg.get('step_offset_min') or 0))
                    for key, scheduled in week_steps(week):
                        if key not in done:
                            if not key.startswith('score'):
                                due = scheduled + off
                            break
            if due is not None:
                out.append(due)
                if reserved:
                    seat_out.append(due)
        except Exception as e:
            print('priority: due-time read failed for', l['id'], e)
    return (out, seat_out, leagues)

def _due_model_steps(now: datetime, only=None, seats_only: bool=False) -> list[datetime]:
    out, seat_out, _ = _due_model_scan(now, only, include_nonseat=not seats_only)
    return seat_out if seats_only else out
TICK_POLL_S = 60
TICK_SHARED_S = 600
TICK_MAX_S = 5 * 3600
STAGED = ROOT / 'state' / 'release-staged.json'
DRAIN_TTL = timedelta(minutes=20)

def running_release() -> str | None:
    try:
        return json.loads((ROOT / 'RELEASE').read_text())['sha'][:12]
    except (OSError, ValueError, KeyError, TypeError):
        return None

def drain_requested(now: datetime | None=None) -> str | None:
    try:
        d = json.loads(STAGED.read_text())
        rel, at = (str(d['release'])[:12], datetime.fromisoformat(d['at']))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if rel == running_release() or (now or now_utc()) - at > DRAIN_TTL:
        return None
    return rel

def tick(only=None, *, clock=None, sleep=None, poll_s: float | None=None, max_s: float | None=None):
    import time
    clock, sleep = (clock or now_utc, sleep or time.sleep)
    poll_s = TICK_POLL_S if poll_s is None else poll_s
    max_s = TICK_MAX_S if max_s is None else max_s
    lock = open(ROOT / 'state' / 'tick.lock', 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        shared = True
    except BlockingIOError:
        shared = False
        print('another tick holds the shared pass; running due league work only (each league under its own lock)', flush=True)
    now = clock()
    staged = drain_requested(now)
    if staged:
        print(json.dumps({'at': now.isoformat(), 'ran': {}, 'draining_for_release': staged}), flush=True)
        return
    gw = _gw()
    from bm import priority
    if shared:
        dues = _shared_pass(now, only, gw)
        try:
            _onboard_missing(gw, now)
        except Exception as e:
            print('onboarding pass failed:', e)
    else:
        dues = _due_model_steps(now, only)
    running = any((d <= now for d in dues))
    seat_running = running and any((d <= now for d in _due_model_steps(now, only, seats_only=True)))
    out: dict = {}
    from contextlib import nullcontext
    with priority.hold(seats=seat_running) if running else nullcontext():
        _run_leagues(league_ids(only), gw, out, clock, sleep, poll_s, max_s, shared_pass=(lambda t: _shared_pass(t, only, gw)) if shared else None)
    print(json.dumps({'at': now.isoformat(), 'ran': out}))
    bad = {i: r[0][:300] for i, r in out.items() if r and str(r[0]).startswith('ERROR')}
    global TICK_ALERTED
    TICK_ALERTED = False
    if bad:
        from bm.ops.alert import alert
        TICK_ALERTED = alert('tick-league-error', 'BenchedMark tick: league step failed in ' + ', '.join(sorted(bad)) + '. ' + next(iter(bad.values()))[-300:])
    if shared:
        try:
            from bm.publish.export import export
            export()
        except Exception as e:
            print('export failed:', e)
        write_status(now, out)

def _busy_lineup_deadlines(lg: League, now: datetime) -> list[dict]:
    if lg.cfg.get('status') in ('ended', 'archived'):
        return []
    events = lg.events()
    week = int(lg.cfg['start_week'])
    for event in events:
        if event['type'] == 'week_advance':
            week = int(event['week'])
    if week > end_week(lg):
        return []
    done = {e['step'] for e in events if e['type'] == 'step_done'}
    kicks = sorted({datetime.fromisoformat(g['kickoff']) for g in nv.schedule(2026) if g['week'] == week})
    off = timedelta(minutes=int(lg.cfg.get('step_offset_min') or 0))
    late = []
    for key, scheduled in week_steps(week):
        if not key.startswith('lineup:') or key in done or scheduled + off > now:
            continue
        deadline = next((k for k in kicks if k > scheduled), None)
        if deadline is not None and now >= deadline:
            late.append({'step': key, 'deadline': deadline.isoformat()})
    return late

def _league_pass(i: str, gw, clock) -> tuple[list[str], str | None]:
    if not (LEAGUES / i / 'league.json').exists():
        return ([], 'not created')
    if json.loads((LEAGUES / i / 'league.json').read_text()).get('mode') == 'agent':
        return ([], 'agent league: run by bm.cli agent-tick')
    import traceback
    try:
        lg = League(i, gw)
        with lg.exclusive():
            t = clock()
            ran = run_league_steps(lg, t, clock=clock)
            t = clock()
            wk = lg.state()['week']
            if wk <= end_week(lg):
                lg.record_locks(wk, t)
        return (ran, None)
    except RuntimeError as e:
        if 'being run by another process' in str(e):
            try:
                from bm.ops.alert import alert
                for missed in _busy_lineup_deadlines(lg, clock()):
                    alert('busy-past-lineup-lock', f'BenchedMark: {i} is busy with unfinished {missed['step']} past kickoff {missed['deadline']}; another runner holds its league lock.')
            except Exception as exc:
                print(f'busy deadline check failed for {i}: {exc}', flush=True)
            return ([], 'busy: held by another bm process (e.g. a draft); steps run on a later pass')
        return ([], 'ERROR ' + traceback.format_exc()[-800:])
    except StepHandedOff as e:
        return ([], f'held: {e}')
    except Exception:
        return ([], 'ERROR ' + traceback.format_exc()[-800:])

def _run_leagues(ids: list[str], gw, out: dict, clock, sleep, poll_s: float, max_s: float, shared_pass=None) -> None:
    start = last_shared = clock()
    ran: dict = {i: [] for i in ids}
    note: dict = {}
    done_for_good: set = set()
    threads: dict = {}

    def go(i):
        r, why = _league_pass(i, gw, clock)
        ran[i] += r
        if why:
            note[i] = why
            if why.startswith(('ERROR', 'held:', 'not created', 'agent league')):
                done_for_good.add(i)
        else:
            note.pop(i, None)

    def launch(i):
        threads[i] = threading.Thread(target=go, args=(i,), name=f'tick-{i}', daemon=True)
        threads[i].start()
    for i in ids:
        launch(i)
    while True:
        waited = 0.0
        while waited < poll_s and any((t.is_alive() for t in threads.values())):
            step = min(1.0, poll_s - waited)
            sleep(step)
            waited += step
        alive = [i for i, t in threads.items() if t.is_alive()]
        if not alive:
            break
        if (clock() - start).total_seconds() >= max_s:
            print(f'tick: {max_s:.0f}s reached; waiting for {', '.join(alive)}, no new passes', flush=True)
            [threads[i].join() for i in alive]
            break
        if shared_pass is not None and (clock() - last_shared).total_seconds() >= TICK_SHARED_S:
            last_shared = clock()
            try:
                shared_pass(last_shared)
            except Exception as e:
                print('shared pass failed:', e, flush=True)
        if drain_requested(clock()):
            continue
        for i in ids:
            if i not in alive and i not in done_for_good:
                launch(i)
    for i in ids:
        r = ran[i]
        out[i] = [note[i]] + r if note.get(i, '').startswith(('ERROR', 'held:')) else r or ([note[i]] if i in note else [])

def _shared_pass(now: datetime, only, gw) -> list[datetime]:
    m = nv.manifest()
    last = max((datetime.fromisoformat(r['retrieved_at']) for r in m), default=None)
    if last is None or now - last > timedelta(minutes=55):
        try:
            nv.ingest(2026)
        except Exception as e:
            print('ingest failed:', e)
    try:
        from bm.stats import platform
        last_sl = max((datetime.fromisoformat(r['retrieved_at']) for r in platform.manifest() if r['source'] == 'sleeper_api'), default=None)
        loc = now.astimezone(ZoneInfo('America/New_York'))
        gameday = loc.weekday() in (0, 3, 5, 6) and 10 <= loc.hour < 21
        if last_sl is None or now - last_sl > (timedelta(minutes=50) if gameday else timedelta(hours=20)):
            platform.ingest(sources=('sleeper_api',))
    except Exception as e:
        print('sleeper status pull failed:', e)
    from bm import priority
    dues, seat_dues, scanned_leagues = _due_model_scan(now, only, gw)
    try:
        priority.preopen(dues, now)
        priority.preopen(seat_dues, now, seats=True)
        for w in {json.dumps(l['quota_reserve'].get('windows') or [], sort_keys=True) for l in league_cfgs() if seat_reserve(l) and (not only or l['id'] in only)}:
            priority.reserve_windows(json.loads(w), now)
    except OSError as e:
        print('priority window write failed:', e)
    try:
        from bm.ops import capacity
        soon = any((d <= now + timedelta(minutes=60) for d in dues))
        due = []
        if soon:
            scanned = {lg.id: lg for lg in scanned_leagues}
            for i in league_ids(only):
                path = LEAGUES / i / 'league.json'
                if path.exists() and json.loads(path.read_text()).get('mode') != 'agent':
                    due.append(scanned.get(i) or League(i, gw))
        work = capacity.steps_work(due, now)
        if work:
            capacity_gate(work, f'tick {now.astimezone(ZoneInfo('America/New_York')):%a %H:%M} ET due steps', gw, force=True)
    except Exception as e:
        print('capacity preflight skipped:', e)
    return dues

def _onboard_missing(gw, now) -> None:
    from bm.agent.onboard import IDENT, load, onboard
    for e in sorted({e for l in league_cfgs() for e in seat_entrants(l)}):
        ident = load(e)
        if ident and ident.get('status') == 'ok':
            continue
        if ident and now - datetime.fromisoformat(ident['created_at']) < timedelta(hours=6):
            continue
        try:
            onboard(gw, e)
        except Exception as ex:
            print('onboard failed', e, ex)

def next_steps() -> list[dict]:
    res = []
    now = now_utc()
    for i in league_ids():
        if not (LEAGUES / i / 'league.json').exists():
            res.append({'league': i, 'next': 'create'})
            continue
        lg = League(i)
        if not lg.done('draft'):
            res.append({'league': i, 'next': 'draft', 'due': lg.cfg['draft_as_of']})
            continue
        week = lg.state()['week']
        if week > end_week(lg):
            res.append({'league': i, 'next': 'season complete'})
            continue
        for key, due in week_steps(week):
            if not lg.done(key):
                res.append({'league': i, 'week': week, 'next': key, 'due': iso(due)})
                break
    return res

def write_status(now, ran):
    from bm.gateway.core import Ledger
    STATUS.parent.mkdir(parents=True, exist_ok=True)
    STATUS.write_text(json.dumps({'observed_at': now.isoformat(), 'ran': ran, 'next': next_steps(), 'calls_today': Ledger().today()}, indent=1))

def market_pull(only_due: bool=False):
    from bm.stats import market
    report = {}
    for r in market.ingest(only_due=only_due, report=report):
        print(r['source'], r['retrieved_at'], r.get('unchanged', False), f'{r.get('bytes', 0)} bytes')
    try:
        from bm import wire
        wire.refresh_wire()
    except Exception as e:
        print('wire refresh failed:', e)
    incomplete = any((r.get('status') not in ('captured', 'not due') for r in report.values()))
    receipt = {'observed_at': datetime.now(timezone.utc).isoformat(), 'status': 'incomplete' if incomplete else 'complete', 'sources': report, 'meaning': 'capture delivery only; no quote coverage, freshness, liquidity or forecast success claim'}
    path = ROOT / 'state/capture-market.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(receipt, indent=2))
    temp.replace(path)
    print('capture delivery:', receipt['status'])
    return 4 if incomplete else 0

def ceiling(week: int, only=None):
    from bm.league.ceiling import compute_ceiling
    from bm.stats import nflverse as nv
    from bm.engine.scoring import score_player_line
    import os
    lines, _ = nv.weekly_lines(2026, through_week=week)
    week_lines = lines.get(week, {})
    if not week_lines:
        print(f'No stats found for week {week}')
        return
    master_leagues = Path(os.path.expanduser('~/projects/fantasy-bench/leagues'))
    if not master_leagues.exists():
        print(f'Master leagues directory not found at {master_leagues}')
        return
    for i in league_ids(only):
        league_dir = master_leagues / i
        if not (league_dir / 'league.json').exists():
            continue
        try:
            import bm.league.season as season_mod
            old_leagues = season_mod.LEAGUES
            season_mod.LEAGUES = master_leagues
            try:
                lg = League(i)
            finally:
                season_mod.LEAGUES = old_leagues
        except Exception as e:
            print(f'Failed to load league {i}: {e}', flush=True)
            continue
        score_event = None
        for event in reversed(lg.events()):
            if event.get('type') == 'step_done' and event.get('step') == f'score:{week}':
                score_event = event
                break
        if not score_event:
            continue
        from datetime import datetime
        score_ts = datetime.fromisoformat(score_event.get('ts') or score_event.get('logged_at'))
        view = lg.view(lg.cfg['start_week'], score_ts)
        state = lg.state(view.world)
        if str(week) not in state['scores']:
            continue
        league_player_points = {}
        for pid, line in week_lines.items():
            try:
                if pid in view.world.players:
                    pos = view.world.pos(pid)
                elif pid.startswith('DEF-'):
                    pos = 'DEF'
                else:
                    continue
                if pos:
                    pts = score_player_line(lg.rules, pos, line)
                    league_player_points[pid] = pts
            except Exception:
                pass
        results = []
        for tid in sorted(state['teams']):
            result = compute_ceiling(lg.rules, view.world, tid, week, state, league_player_points)
            results.append(result)
        print(f'{i} week {week}')
        for r in results:
            name = lg.names().get(r['team_id'], r['team_id'])
            print(f'  {r['team_id']} {name}: {r['actual_points']:.1f}/{r['optimal_points']:.1f} ' + f'({r['share_captured']:.0f}%)')
            if r['mistakes']:
                for m in r['mistakes'][:3]:
                    benched_str = m['benched'] if m['benched'] else '(empty)'
                    for_str = m['for'] if m['for'] else '(empty)'
                    print(f'    -{m['delta']:.1f}: {benched_str} for {for_str}')

def _agent_worker_unit(league_id: str, team: str) -> str:
    kind = 'draft' if team == 'draft' else 'wake'
    raw = f'{league_id}-{team}'
    slug = ''.join((ch if ch.isalnum() or ch in '_.-' else '-' for ch in raw))
    slug = slug.strip('-._')[:96] or 'worker'
    digest = hashlib.sha256(raw.encode()).hexdigest()[:10]
    return f'benchedmark-agent-{kind}-{slug}-{digest}.service'

def _spawn_agent_worker(league_id: str, team: str, reason: dict | None, runner=subprocess.run) -> bool:
    command = [sys.executable, '-m', 'bm.cli', 'agent-draft', league_id] if team == 'draft' else [sys.executable, '-m', 'bm.cli', 'agent-wake', league_id, '--only', team, '--label', json.dumps(reason)]
    log = ROOT / 'logs' / 'agent-workers.log'
    path = os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin')
    argv = ['systemd-run', '--user', '--quiet', '--collect', '--no-ask-password', '--service-type=exec', '--expand-environment=no', '--slice=background.slice', f'--unit={_agent_worker_unit(league_id, team)}', f'--working-directory={ROOT}', f'--setenv=PYTHONPATH={ROOT}', '--setenv=PYTHONUNBUFFERED=1', f'--setenv=PATH={path}', f'--property=StandardOutput=append:{log}', f'--property=StandardError=append:{log}', '--nice=10', '--', *command]
    runtime_root = os.environ.get('BM_RUNTIME_ROOT')
    if runtime_root:
        argv.insert(argv.index('--'), f'--setenv=BM_RUNTIME_ROOT={runtime_root}')
    result = runner(argv, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or 'unknown systemd-run error').strip()[-500:]
        raise RuntimeError(f'transient worker launch failed: {detail}')
    return True

def agent_tick(only=None):
    from bm.agent import runtime
    from bm.league.agentleague import AgentLeague
    now = now_utc()
    gw = _gw()

    def spawn(league_id, team, reason):
        return _spawn_agent_worker(league_id, team, reason)
    out = {}
    for i in league_ids(only):
        d = LEAGUES / i / 'league.json'
        if not d.exists() or json.loads(d.read_text()).get('mode') != 'agent':
            continue
        lg = AgentLeague(i, gw)
        try:
            with lg.exclusive():
                out[i] = runtime.tick(lg, gw, now, spawn=spawn)
        except RuntimeError as e:
            out[i] = str(e)
        except Exception:
            import traceback
            out[i] = 'ERROR ' + traceback.format_exc()[-800:]
            from bm.ops.alert import alert
            alert('agent-tick', f'BenchedMark agent tick failed in {i}: {out[i][-300:]}')
    print(json.dumps({'at': now.isoformat(), 'ran': out}, default=str))

def verify():
    ok = True
    for i in league_ids():
        if not (LEAGUES / i / 'league.json').exists():
            continue
        lg = League(i)
        good, n, head = lg.log.verify()
        st = lg.state()
        print(i, 'chain', 'OK' if good else 'BROKEN', n, head[:16], 'replay week', st['week'])
        ok &= good
    from bm.log import ChainLog
    g = ChainLog(ROOT / 'logs' / 'gateway.jsonl').verify()
    print('gateway chain', g[0], g[1])
    return ok

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd')
    ap.add_argument('arg', nargs='?')
    ap.add_argument('--only', action='append')
    ap.add_argument('--since')
    ap.add_argument('--until')
    ap.add_argument('--label')
    ap.add_argument('--due', action='store_true')
    ap.add_argument('--force', action='store_true', help='draft: start even if the capacity preflight is short')
    a = ap.parse_args()
    if a.cmd == 'ingest':
        for r in nv.ingest(2026):
            print(r['asset'], r['retrieved_at'], r.get('unchanged', False))
    elif a.cmd == 'market-pull':
        sys.exit(market_pull(only_due=a.due))
    elif a.cmd == 'create':
        create(a.only)
    elif a.cmd == 'draft':
        draft(a.only, a.force)
    elif a.cmd == 'replay-week':
        replay_week(int(a.arg), a.only)
    elif a.cmd == 'ceiling':
        ceiling(int(a.arg), a.only)
    elif a.cmd == 'tick':
        global TICK_ENDS_AT
        TICK_ENDS_AT = now_utc() + timedelta(seconds=TICK_UNIT_TIMEOUT_S - 600)
        tick(a.only)
        if HUNG or HANDED_OFF:
            print(json.dumps({'hung_abandoned': HUNG, 'handed_off': HANDED_OFF}), flush=True)
            if HUNG and TICK_ALERTED:
                from bm.ops import alert as A
                A.stamp([A.covered_key(TICK_UNIT)])
            os._exit(HUNG_EXIT if HUNG else 0)
    elif a.cmd == 'export':
        from bm.publish.export import export
        export()
    elif a.cmd == 'onboard':
        from bm.agent.onboard import onboard_all
        ents = sorted({e for l in league_cfgs() for e in seat_entrants(l)})
        for r in onboard_all(ents):
            print(r['entrant_id'], r.get('status'), r.get('team_name'), r.get('tagline'), r.get('usage'))
    elif a.cmd == 'status':
        print(json.dumps(next_steps(), indent=1))
    elif a.cmd == 'lane-health':
        from bm.publish import lanehealth
        m = lanehealth.measure(a.since, a.until, a.only)
        print(lanehealth.render(m))
        if a.label:
            lanehealth.record(m, a.label)
    elif a.cmd == 'verify':
        sys.exit(0 if verify() else 1)
    elif a.cmd == 'agent-create':
        from bm.league import agentleague as al
        for l in league_cfgs():
            if l.get('mode') == 'agent' and (not a.only or l['id'] in a.only) and l.get('seats'):
                lg = al.create(l)
                print('agent league', lg.id, len(lg.seats), 'seats, draft', lg.cfg['draft_as_of'])
    elif a.cmd == 'agent-tick':
        agent_tick(a.only)
    elif a.cmd == 'agent-wake':
        from bm.agent import runtime
        print(json.dumps(runtime.worker(a.arg, a.only[0], json.loads(a.label or 'null')), default=str))
    elif a.cmd == 'agent-draft':
        from bm.agent import runtime
        print(json.dumps(runtime.worker(a.arg, 'draft', None), default=str))
    else:
        ap.error('unknown command')
if __name__ == '__main__':
    main()
