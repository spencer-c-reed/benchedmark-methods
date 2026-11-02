from __future__ import annotations
import json
import fcntl
import uuid
import os
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
WINDOW = Path(os.environ.get('BM_PRIORITY_FILE', Path.home() / '.cache' / 'status' / 'benchedmark-priority.json'))
PROVIDERS = ['openrouter']
ZEN_MODELS = ['opencode/muse-spark-1.3-contributor-free', 'opencode/nemotron-3-ultra-free', 'opencode/mimo-v2.6-flash-free', 'opencode/ling-3.1-flash-free']
SEATS = ['claude-main', 'codex-main']
LEAD_S = 15 * 60
TAIL_S = 10 * 60
HEARTBEAT_S = 120
_lock = threading.Lock()

def current_until() -> datetime | None:
    try:
        u = datetime.fromisoformat(json.loads(WINDOW.read_text())['until'].replace('Z', '+00:00'))
        return u if u.tzinfo else None
    except (OSError, ValueError, KeyError, TypeError):
        return None

def _read() -> dict:
    try:
        d = json.loads(WINDOW.read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}

def _ts(v) -> datetime | None:
    try:
        u = datetime.fromisoformat(str(v).replace('Z', '+00:00'))
        return u if u.tzinfo else None
    except (TypeError, ValueError):
        return None

def seats_until() -> datetime | None:
    d = _read()
    if not d.get('seats'):
        return None
    return _ts(d.get('seats_until')) or _ts(d.get('until'))

def extend(until: datetime, seats: bool=False, now: datetime | None=None) -> datetime:
    now = now or datetime.now(timezone.utc)
    with _window_lock():
        body = _read()
        cur = _ts(body.get('until'))
        new_until = max(cur, until) if cur else until
        body.update(until=new_until.astimezone().isoformat(timespec='seconds'), providers=PROVIDERS, models=ZEN_MODELS)
        _write(body, now)
        return new_until

@contextmanager
def _window_lock():
    WINDOW.parent.mkdir(parents=True, exist_ok=True)
    with _lock, WINDOW.with_suffix('.lock').open('a') as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield

def _write(body, now):
    leases = {k: v for k, v in body.get('seat_leases', {}).items() if _ts(v.get('until')) and _ts(v['until']) > now}
    body.setdefault('until', now.isoformat())
    body.setdefault('providers', PROVIDERS)
    body.setdefault('models', ZEN_MODELS)
    body['seat_leases'] = leases
    body['seats'] = sorted({v['seat'] for v in leases.values()})
    body.pop('seats_until', None)
    tmp = WINDOW.with_name(f'.{WINDOW.name}.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(body))
    os.replace(tmp, WINDOW)

@contextmanager
def active_call(lane, decision_id=''):
    pick = decision_id.startswith('picks:') or bool(re.search(':(?:draft|lineup\\d+|waivers):', decision_id))
    if not pick:
        yield
        return
    adapter = lane.get('adapter')
    seat = lane.get('account', 'claude-main') if adapter == 'claude_cli' else 'codex-' + lane.get('codex_profile', 'main') if adapter == 'codex_cli' else None
    if seat not in SEATS:
        yield
        return
    token, stop = (uuid.uuid4().hex, threading.Event())

    def update(remove=False):
        now = datetime.now(timezone.utc)
        with _window_lock():
            body = _read()
            leases = body.setdefault('seat_leases', {})
            if remove:
                leases.pop(token, None)
            else:
                leases[token] = {'seat': seat, 'until': (now + timedelta(seconds=120)).isoformat()}
            _write(body, now)

    def beat():
        while not stop.wait(30):
            try:
                update()
            except OSError as exc:
                print('priority seat lease write failed:', exc, flush=True)
    try:
        update()
    except OSError as exc:
        print('priority seat lease write failed:', exc, flush=True)
    thread = threading.Thread(target=beat, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=5)
        try:
            update(remove=True)
        except OSError as exc:
            print('priority seat lease release failed:', exc, flush=True)

def preopen(due_times, now: datetime | None=None, seats: bool=False) -> datetime | None:
    now = now or datetime.now(timezone.utc)
    soon = [d for d in due_times if d is not None and d - now <= timedelta(seconds=LEAD_S)]
    if not soon:
        return None
    return extend(max(now, max(soon)) + timedelta(seconds=TAIL_S), seats, now=now)

def reserve_windows(windows, now: datetime | None=None) -> datetime | None:
    now = now or datetime.now(timezone.utc)
    ends = []
    for w in windows or []:
        start, end = (_ts((w or {}).get('from')), _ts((w or {}).get('until')))
        if start and end and (start - timedelta(seconds=LEAD_S) <= now < end):
            ends.append(end)
    return extend(max(ends), True, now=now) if ends else None
SEAT_LANES = {'claude_max', 'codex_main', 'codex_credits'}
DONE = 'done'
_paused: dict[str, str] = {}

def draft_paused(league: str, lane: str | None) -> None:
    with _lock:
        if lane is None:
            _paused.pop(league, None)
        else:
            _paused[league] = lane

def seats_idle(leagues) -> bool:
    with _lock:
        lanes = [_paused.get(i) for i in leagues]
    return bool(lanes) and all((l and l not in SEAT_LANES for l in lanes))

@contextmanager
def hold(seats: bool=False, leagues=None):
    stop = threading.Event()

    def beat():
        while True:
            try:
                extend(datetime.now(timezone.utc) + timedelta(seconds=TAIL_S), seats and (not (leagues and seats_idle(leagues))))
            except OSError as e:
                print('priority window write failed:', e, flush=True)
            if stop.wait(HEARTBEAT_S):
                return
    try:
        extend(datetime.now(timezone.utc) + timedelta(seconds=TAIL_S), seats and (not (leagues and seats_idle(leagues))))
    except OSError as e:
        print('priority window write failed:', e, flush=True)
    t = threading.Thread(target=beat, daemon=True)
    t.start()
    try:
        yield
    finally:
        stop.set()
        t.join(timeout=5)
RESTART_CONFIG = 'leagues-nfl-restart.yaml'
PAUSE_LEAD_S = 15 * 60
ETA_LEAD_S = 30 * 60
CREATE_GRACE_S = 60 * 60
HARD_RESUME_AFTER_S = 5 * 3600

def _paths(root: Path | None, leagues_dir: Path | None) -> tuple[Path, Path]:
    if root is None or leagues_dir is None:
        from bm.league import season
        root = root or season.ROOT
        leagues_dir = leagues_dir or season.LEAGUES
    return (Path(root), Path(leagues_dir))

def restart_draft(root: Path | None=None) -> dict | None:
    import yaml
    root, _ = _paths(root, Path('.'))
    f = root / 'config' / RESTART_CONFIG
    try:
        c = yaml.safe_load(f.read_text()) or {}
    except (OSError, ValueError, yaml.YAMLError):
        return None
    at = _ts(c.get('draft_as_of')) if isinstance(c, dict) else None
    if at is None:
        return None
    dp = c.get('draft_priority') or {}
    if not isinstance(dp, dict) or dp.get('enabled') is False:
        return None
    return {'draft_at': at, 'pause_from': at - timedelta(seconds=float(dp.get('pause_lead_s', PAUSE_LEAD_S))), 'eta_bound': at - timedelta(seconds=float(dp.get('eta_lead_s', ETA_LEAD_S))), 'resume_by': _ts(dp.get('resume_by')), 'create_grace_s': float(dp.get('create_grace_s', CREATE_GRACE_S)), 'hard_resume': at + timedelta(seconds=float(dp.get('hard_resume_after_s', HARD_RESUME_AFTER_S))), 'leagues': [str(l['id']) for l in c.get('leagues') or [] if isinstance(l, dict) and l.get('id')], 'config': f'config/{RESTART_CONFIG}'}

def _draft_done(league_dir: Path) -> bool:
    try:
        with open(league_dir / 'log.jsonl', 'rb') as fh:
            for line in fh:
                if b'"step_done"' in line:
                    p = json.loads(line).get('payload') or {}
                    if p.get('type') == 'step_done' and p.get('step') == 'draft':
                        return True
    except (OSError, ValueError):
        return False
    return False

def draft_pause(league_id: str, now: datetime, own_cutoff: datetime | None=None, root: Path | None=None, leagues_dir: Path | None=None) -> dict | None:
    root, leagues_dir = _paths(root, leagues_dir)
    w = restart_draft(root)
    if w is None or league_id in w['leagues'] or now < w['pause_from']:
        return None
    resume_by = min([t for t in (w['resume_by'], own_cutoff) if t is not None], default=None)
    made = [l for l in w['leagues'] if (leagues_dir / l / 'league.json').exists()]
    info = {'restart_draft_at': w['draft_at'].isoformat(), 'pause_from': w['pause_from'].isoformat(), 'resume_by': resume_by.isoformat() if resume_by else None, 'hard_resume': w['hard_resume'].isoformat(), 'restart_leagues': w['leagues'], 'restart_leagues_created': made, 'config': w['config']}
    if made and all((_draft_done(leagues_dir / l) for l in made)):
        return dict(info, hold=False, reason='restart draft complete')
    if now >= w['hard_resume']:
        return dict(info, hold=False, reason='hard_resume')
    if resume_by is not None and now >= resume_by:
        return dict(info, hold=False, reason='resume_by reached (latest resume time)')
    if not made and now >= w['draft_at'] + timedelta(seconds=w['create_grace_s']):
        return dict(info, hold=False, reason='no restart league was created; nothing to yield to')
    return dict(info, hold=True, reason='restart draft priority: the restart leagues draft on the same lanes; new picks hold until their draft completes or the hard resume at ' + min(w['hard_resume'], resume_by or w['hard_resume']).isoformat())

def draft_eta_bound(league_cfg: dict, root: Path | None=None) -> datetime | None:
    b = _ts(league_cfg.get('draft_eta_bound'))
    if b is not None:
        return b
    w = restart_draft(root)
    start = _ts(league_cfg.get('draft_as_of'))
    if w is None or str(league_cfg.get('id')) in w['leagues'] or start is None or (start >= w['draft_at']):
        return None
    return w['eta_bound']
