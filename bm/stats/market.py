from __future__ import annotations
import gzip
import fcntl
import tempfile
import hashlib
import json
import logging
import os
import math
import re
import shlex
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from functools import lru_cache
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / 'data' / 'raw' / 'market'
MANIFEST = RAW / 'manifest.jsonl'
UA = {'User-Agent': 'Mozilla/5.0 (BenchedMark-research/0.1; +benchedmark.com)'}
logger = logging.getLogger(__name__)

def _get(url: str, headers: dict | None=None, tries: int=3) -> bytes:
    err = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={**UA, **(headers or {})})
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.read()
        except Exception as e:
            err = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f'fetch failed {url}: {err}')

def _load_odds_api_key() -> str | None:
    if (key := os.getenv('ODDS_API_KEY')):
        return key
    config_path = Path.home() / '.config' / 'secrets' / 'odds-api.env'
    if config_path.exists():
        try:
            with open(config_path, 'r') as f:
                content = f.read().strip()
                if content:
                    return content
        except Exception as e:
            logger.warning(f'failed to read odds-api.env: {e}')
    return None

def fetch_sleeper_trending(lookback_hours: int=24, limit: int=50) -> dict[str, list[dict]]:
    adds_url = f'https://api.sleeper.app/v1/players/nfl/trending/add?lookback_hours={lookback_hours}&limit={limit}'
    drops_url = f'https://api.sleeper.app/v1/players/nfl/trending/drop?lookback_hours={lookback_hours}&limit={limit}'
    adds = json.loads(_get(adds_url))
    drops = json.loads(_get(drops_url))
    return {'adds': adds, 'drops': drops}
SLEEPER_PLAYERS_URL = 'https://api.sleeper.app/v1/players/nfl'
CONSENSUS_URL = 'https://raw.githubusercontent.com/dynastyprocess/data/master/files/values-players.csv'
NEWS_FIELDS = ('full_name', 'team', 'position', 'status', 'injury_status', 'injury_body_part', 'injury_notes', 'injury_start_date', 'news_updated', 'practice_participation', 'practice_description', 'depth_chart_position', 'depth_chart_order', 'search_rank', 'gsis_id')
_odds_quota: dict = {}

def fetch_sleeper_news() -> dict:
    allp = json.loads(_get(SLEEPER_PLAYERS_URL))
    out = {}
    for pid, p in allp.items():
        if not (p.get('team') or p.get('news_updated')):
            continue
        out[pid] = {k: p.get(k) for k in NEWS_FIELDS if p.get(k) is not None}
    return {'players': out}
CONSENSUS_RETIRED = 'FantasyPros-derived (DynastyProcess); retired 2026-10-13 for terms compliance'
CONSENSUS_ARCHIVE = '_archive/consensus-fantasypros-20261013'

def fetch_consensus() -> dict:
    raise RuntimeError(CONSENSUS_RETIRED)

def _fetch_consensus_retired_original() -> dict:
    import csv
    import io
    rows = list(csv.DictReader(io.StringIO(_get(CONSENSUS_URL).decode('utf-8'))))
    return {'rows': rows}
ODDS_KEY_FILE = Path.home() / '.config' / 'secrets' / 'benchedmark.env'
ODDS_POOL_PREFIX = 'BM_ODDS_API_KEY_'
ODDS_POOL_STATE = RAW / 'odds-keys.json'
ODDS_REST = timedelta(hours=24)
ODDS_FREE_CREDITS = 500.0

def _odds_api_keys() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if (k := _load_odds_api_key()):
        out.append(('ODDS_API_KEY' if os.getenv('ODDS_API_KEY') else 'odds-api.env', k))
    pool = {n: v.strip() for n, v in os.environ.items() if n.startswith(ODDS_POOL_PREFIX) and v.strip()}
    try:
        if ODDS_KEY_FILE.exists() and ODDS_KEY_FILE.stat().st_mode & 63 == 0:
            for line in ODDS_KEY_FILE.read_text().splitlines():
                n, _, v = line.strip().removeprefix('export ').partition('=')
                v = v.strip().strip('"').strip("'")
                if n.startswith(ODDS_POOL_PREFIX) and v:
                    pool.setdefault(n, v)
    except OSError as e:
        logger.warning(f'failed to read odds key pool file: {type(e).__name__}')
    seen = {k for _, k in out}
    for n in sorted(pool, key=lambda n: (len(n), n)):
        if pool[n] not in seen:
            out.append((n, pool[n]))
            seen.add(pool[n])
    return out

def _odds_pool_state() -> dict:
    try:
        return json.loads(ODDS_POOL_STATE.read_text())
    except (OSError, ValueError):
        return {}

def _odds_pool_save(state: dict) -> None:
    try:
        ODDS_POOL_STATE.parent.mkdir(parents=True, exist_ok=True)
        ODDS_POOL_STATE.write_text(json.dumps(state, indent=1, sort_keys=True))
    except OSError as e:
        logger.warning(f'failed to save odds key pool state: {type(e).__name__}')
ODDS_QUOTA_GROUP = {'BM_ODDS_API_KEY_2': 'odds-api.env'}

def odds_quota_label(label: str) -> str:
    return ODDS_QUOTA_GROUP.get(label, label)

def odds_unique_remaining(state: dict, labels: list[str] | None=None) -> dict:
    allowed = {odds_quota_label(x) for x in labels} if labels is not None else None
    groups = {}
    for label, record in state.items():
        group = odds_quota_label(label)
        if allowed is not None and group not in allowed:
            continue
        if not isinstance(record, dict):
            continue
        try:
            value = float(record.get('remaining'))
        except (ValueError, TypeError):
            continue
        stamp = record.get('seen_at') or ''
        if group not in groups or stamp > groups[group][0]:
            groups[group] = (stamp, value)
    return {label: value for label, (_, value) in groups.items()}

def odds_record_quota(label: str, quota: dict, seen_at: datetime, path: Path | None=None) -> None:
    path = path or ODDS_POOL_STATE
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + '.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            state = json.loads(path.read_text())
        except (OSError, ValueError):
            state = {}
        record = state.setdefault(label, {})
        stamp = seen_at.isoformat()
        if stamp >= (record.get('seen_at') or ''):
            record.update({k: quota[k] for k in ('remaining', 'used') if k in quota}, seen_at=stamp)
            record.pop('last_error', None)
            if quota.get('remaining') is not None and float(quota['remaining']) <= 0:
                record['exhausted_at'] = stamp
            else:
                record.pop('exhausted_at', None)
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as tmp:
            json.dump(state, tmp, indent=1, sort_keys=True)
            name = tmp.name
        os.replace(name, path)

def odds_record_error(label: str, code: int, seen_at: datetime) -> None:
    path = ODDS_POOL_STATE
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + '.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            state = json.loads(path.read_text())
        except (OSError, ValueError):
            state = {}
        state.setdefault(label, {}).update(exhausted_at=seen_at.isoformat(), last_error=code)
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as tmp:
            json.dump(state, tmp, indent=1, sort_keys=True)
            name = tmp.name
        os.replace(name, path)

def _odds_key_order(keys: list[tuple[str, str]], state: dict, now: datetime) -> list[tuple[str, str]]:

    def resting(label: str) -> bool:
        ex = state.get(label, {}).get('exhausted_at')
        return bool(ex) and now - datetime.fromisoformat(ex) < ODDS_REST

    def remaining(label: str) -> float:
        try:
            return float(state.get(label, {}).get('remaining'))
        except (TypeError, ValueError):
            return ODDS_FREE_CREDITS
    live = [kv for kv in keys if not resting(kv[0])]
    rest = sorted((kv for kv in keys if resting(kv[0])), key=lambda kv: state[kv[0]]['exhausted_at'])
    return sorted(live, key=lambda kv: -remaining(kv[0])) + rest

def _odds_get(url: str) -> tuple[bytes, dict]:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        body = r.read()
        quota = {h.replace('x-requests-', ''): r.headers.get(h) for h in ('x-requests-remaining', 'x-requests-used', 'x-requests-last') if r.headers.get(h) is not None}
    return (body, quota)
ODDS_5XX_RETRY_S = 5

def _odds_get_retry_5xx(url: str) -> tuple[bytes, dict]:
    try:
        return _odds_get(url)
    except urllib.error.HTTPError as e:
        if e.code < 500:
            raise
        logger.warning(f'odds api HTTP {e.code}; retrying once in {ODDS_5XX_RETRY_S}s')
    time.sleep(ODDS_5XX_RETRY_S)
    return _odds_get(url)

def odds_api_call(path_and_query: str) -> tuple[bytes, dict] | None:
    keys = _odds_api_keys()
    if not keys:
        logger.info(f'Odds API key not found (env var ODDS_API_KEY, ~/.config/secrets/odds-api.env, or {ODDS_POOL_PREFIX}<n>); skipping')
        return None
    now = datetime.now(timezone.utc)
    state = _odds_pool_state()
    sep = '&' if '?' in path_and_query else '?'
    result = None
    for label, key in _odds_key_order(keys, state, now):
        st = state.setdefault(label, {})
        try:
            body, quota = _odds_get_retry_5xx(f'https://api.the-odds-api.com/v4/{path_and_query}{sep}apiKey={key}')
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 429):
                st.update(exhausted_at=now.isoformat(), last_error=e.code)
                odds_record_error(label, e.code, now)
                logger.warning(f'odds api key {label}: HTTP {e.code}; failing over')
                continue
            logger.error(f'failed to fetch odds api with {label}: HTTP {e.code}')
            break
        except Exception as e:
            logger.error(f'failed to fetch odds api with {label}: {type(e).__name__}')
            break
        st.update({k: quota[k] for k in ('remaining', 'used') if k in quota}, seen_at=now.isoformat())
        st.pop('last_error', None)
        if 'remaining' in quota and float(quota['remaining']) <= 0:
            st['exhausted_at'] = now.isoformat()
        else:
            st.pop('exhausted_at', None)
        result = (body, {**quota, 'key': label})
        break
    if result is not None:
        odds_record_quota(result[1]['key'], result[1], datetime.now(timezone.utc))
    return result
SHARP_BOOKS = ('pinnacle',)
US_SHARP_FALLBACK = ('lowvig', 'betonlineag')

def fetch_odds_sharp() -> dict[str, list[dict]] | None:
    got = odds_api_call('sports/americanfootball_nfl/odds?bookmakers=' + ','.join(SHARP_BOOKS) + '&markets=h2h,spreads,totals&oddsFormat=american')
    if got is None:
        return None
    body, quota = got
    _odds_quota.clear()
    _odds_quota.update(quota)
    try:
        data = json.loads(body)
    except ValueError as e:
        logger.error(f'failed to parse odds api response: {type(e).__name__}')
        return None
    games = data if isinstance(data, list) else data.get('data', [])
    return {'games': games, 'books': sorted({b.get('key') for g in games for b in g.get('bookmakers', [])})}

def fetch_odds_api() -> dict[str, list[dict]] | None:
    got = odds_api_call('sports/americanfootball_nfl/odds?regions=us&markets=h2h,spreads,totals&oddsFormat=american')
    if got is None:
        return None
    body, quota = got
    _odds_quota.clear()
    _odds_quota.update(quota)
    try:
        data = json.loads(body)
    except ValueError as e:
        logger.error(f'failed to parse odds api response: {type(e).__name__}')
        return None
    return {'games': data if isinstance(data, list) else data.get('data', [])}
ET = ZoneInfo('America/New_York')
KICKOFFS = [(3, 20, 15), (6, 9, 30), (6, 13, 0), (6, 16, 5), (6, 16, 25), (6, 20, 20), (0, 20, 15)]
ODDS_CALL_COST = 3
ODDS_DECISION_LEAD = timedelta(minutes=5)
ODDS_RAMP_START = timedelta(minutes=60)
ODDS_RAMP_STEP = timedelta(minutes=15)
ODDS_CLOSE_LEAD = timedelta(minutes=5)
ODDS_DAILY = timedelta(hours=23)
ODDS_EARLY = timedelta(seconds=30)
PICKS_UNIT_GLOB = 'bm-picks-w*.service'

def _tick_minutes(value: str) -> timedelta | None:
    try:
        minutes = float(value)
        if math.isfinite(minutes) and minutes > 0:
            return timedelta(minutes=max(1.0, minutes))
    except (ValueError, OverflowError):
        pass
    return None

def _capture_unit_property(unit: str, prop: str) -> str:
    try:
        r = subprocess.run(['systemctl', '--user', 'show', unit, '-p', prop, '--value'], capture_output=True, text=True, timeout=2)
        return r.stdout.strip() if r.returncode == 0 else ''
    except (OSError, subprocess.TimeoutExpired):
        return ''

def capture_tick_info() -> tuple[timedelta, str]:
    if (tick := _tick_minutes(os.environ.get('BM_CAPTURE_TICK_MIN', ''))):
        return (tick, 'process Environment BM_CAPTURE_TICK_MIN')
    env = _capture_unit_property('benchedmark-capture.service', 'Environment')
    try:
        values = dict((v.split('=', 1) for v in shlex.split(env) if '=' in v))
    except ValueError:
        values = {}
    if (tick := _tick_minutes(values.get('BM_CAPTURE_TICK_MIN', ''))):
        return (tick, 'benchedmark-capture.service Environment BM_CAPTURE_TICK_MIN')
    calendar = _capture_unit_property('benchedmark-capture.timer', 'TimersCalendar')
    match = re.fullmatch('\\{ OnCalendar=\\*-\\*-\\* \\*:(?:\\*|\\d+)/(\\d+):\\d+ ; next_elapse=.* \\}', calendar)
    if match and 0 < int(match[1]) < 60:
        return (timedelta(minutes=int(match[1])), 'benchedmark-capture.timer OnCalendar (TimersCalendar)')
    monotonic = _capture_unit_property('benchedmark-capture.timer', 'TimersMonotonic')
    match = re.search('\\{ OnUnit(?:Active|Inactive)USec=([^;]+) ;', monotonic)
    if match:
        duration = match[1].strip()
        parts = re.findall('(\\d+(?:\\.\\d+)?)(us|ms|s|min|h|d)', duration)
        if parts and ''.join((n + u for n, u in parts)) == duration.replace(' ', ''):
            seconds = sum((float(n) * {'us': 1e-06, 'ms': 0.001, 's': 1, 'min': 60, 'h': 3600, 'd': 86400}[u] for n, u in parts))
            if seconds > 0:
                return (timedelta(seconds=seconds), 'benchedmark-capture.timer TimersMonotonic')
    return (timedelta(minutes=15), 'fallback (capture cadence unavailable)')

def capture_tick() -> timedelta:
    return capture_tick_info()[0]

def _season(now: datetime) -> int:
    n = now.astimezone(ET)
    return n.year if n.month >= 3 else n.year - 1

def schedule_kickoffs(start: datetime, end: datetime) -> list[datetime]:
    out: set[datetime] = set()
    try:
        from bm.stats import nflverse as nv
        for season in sorted({_season(start), _season(end)}):
            for g in nv.schedule(season):
                k = datetime.fromisoformat(g['kickoff'])
                if start <= k <= end:
                    out.add(k)
    except Exception as e:
        logger.warning('odds plan: schedule unavailable (%s); using fixed kickoff slots', type(e).__name__)
    if not out:
        day = start.astimezone(ET).replace(hour=0, minute=0, second=0, microsecond=0)
        while day <= end:
            for wd, h, m in KICKOFFS:
                k = day.replace(hour=h, minute=m)
                if day.weekday() == wd and start <= k <= end:
                    out.add(k)
            day = (day + timedelta(days=1)).replace(hour=0)
    return sorted(out)

def _unit_dirs() -> list[Path]:
    env = os.environ.get('BM_PICKS_UNIT_DIRS')
    if env is not None:
        return [Path(p) for p in env.split(os.pathsep) if p]
    return [Path.home() / '.config' / 'systemd' / 'user']

def picks_slate_times(start: datetime, end: datetime) -> list[datetime]:
    import re
    out: set[datetime] = set()
    for d in _unit_dirs():
        try:
            files = sorted(d.glob(PICKS_UNIT_GLOB))
        except OSError:
            continue
        for f in files:
            try:
                text = f.read_text()
            except OSError:
                continue
            m = re.search('bm\\.picks\\.run slate \\d+\\b[^\\n]*--at (\\S+)', text)
            if not m:
                continue
            try:
                t = datetime.fromisoformat(m.group(1))
            except ValueError:
                continue
            if t.tzinfo is not None and start <= t <= end:
                out.add(t)
    return sorted(out)

def plan_targets(kickoffs: list[datetime], slates: list[datetime]) -> list[tuple[datetime, str, datetime]]:
    out: dict[datetime, tuple[str, datetime]] = {}
    closes = sorted({k - ODDS_CLOSE_LEAD for k in kickoffs})
    spans: list[list[datetime]] = []
    for k in sorted(kickoffs):
        s, e = (k - ODDS_RAMP_START, k - ODDS_CLOSE_LEAD)
        if spans and s <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], e)
        else:
            spans.append([s, e])
    for s, e in spans:
        t = s
        while True:
            if t in closes:
                out[t] = ('close', t + ODDS_CLOSE_LEAD - timedelta(minutes=1))
            else:
                out.setdefault(t, ('ramp', t + ODDS_RAMP_STEP))
            if t >= e:
                break
            nxt = [c for c in closes if t < c <= t + ODDS_RAMP_STEP]
            t = nxt[0] if nxt else t + ODDS_RAMP_STEP
    for a in slates:
        t = a - ODDS_DECISION_LEAD
        kind, dl = out.get(t, ('decision', a - timedelta(minutes=1)))
        out[t] = ('decision' if kind == 'ramp' else kind, min(dl, a - timedelta(minutes=1)))
    return sorted(((t, k, dl) for t, (k, dl) in out.items()))

def odds_targets(start: datetime, end: datetime) -> list[tuple[datetime, str, datetime]]:
    ks = schedule_kickoffs(start, end + ODDS_RAMP_START)
    return [x for x in plan_targets(ks, picks_slate_times(start, end + ODDS_DECISION_LEAD)) if start <= x[0] <= end]

def _odds_due(now: datetime, last_at: datetime | None, tick: timedelta | None=None, only: tuple | None=None) -> str | None:
    tick = tick or capture_tick()
    for t, kind, deadline in odds_targets(now - 2 * tick - ODDS_RAMP_STEP, now + 2 * tick):
        if only and kind not in only:
            continue
        served = last_at is not None and last_at >= t - tick + timedelta(minutes=1)
        if served or now >= deadline:
            continue
        if now >= t - ODDS_EARLY or now + tick > deadline:
            return kind
    if (not only or 'daily' in only) and (last_at is None or now - last_at >= ODDS_DAILY):
        return 'daily'
    return None

def planned_odds_pulls(start: datetime, end: datetime) -> list[tuple[datetime, str]]:
    out, last = ([], start)
    for t, kind, _ in odds_targets(start, end):
        while t - last >= ODDS_DAILY:
            last += ODDS_DAILY
            out.append((last, 'daily'))
        out.append((t, kind))
        last = t
    while end - last >= ODDS_DAILY:
        last += ODDS_DAILY
        out.append((last, 'daily'))
    return out

def odds_budget(now: datetime, state: dict | None=None) -> dict:
    from calendar import monthrange
    n = now.astimezone(timezone.utc)
    month_end = n.replace(day=monthrange(n.year, n.month)[1], hour=23, minute=59, second=59, microsecond=0) + timedelta(days=2)
    week = planned_odds_pulls(now, now + timedelta(days=7))
    month = planned_odds_pulls(now, month_end)
    state = _odds_pool_state() if state is None else state
    rem = odds_unique_remaining(state)
    need = planned_odds_credits(month)
    kinds = {}
    for _, k in week:
        kinds[k] = kinds.get(k, 0) + 1
    tick, tick_source = capture_tick_info()
    return {'week_pulls': len(week), 'week_credits': planned_odds_credits(week), 'week_by_kind': kinds, 'through': month_end.isoformat(), 'month_pulls': len(month), 'month_credits': need, 'pool_remaining': rem, 'pool_total': sum(rem.values()), 'short': bool(rem) and sum(rem.values()) < need, 'tick_min': tick.total_seconds() / 60, 'tick_source': tick_source}

def odds_preflight_lines(now: datetime) -> tuple[list[str], list[str]]:
    b = odds_budget(now)
    lines = [f'odds pulls: next 7 days {b['week_pulls']} ({b['week_credits']} credits; {b['week_by_kind']}); through {b['through'][:10]} {b['month_pulls']} ({b['month_credits']} credits); pool {b['pool_total']:.0f} left {b['pool_remaining']}', f'capture tick: {b['tick_min']:g} min (source: {b['tick_source']})']
    flags = []
    if b['short']:
        flags.append(f'odds pool {b['pool_total']:.0f} credits < {b['month_credits']} planned through {b['through'][:10]}')
    if b['tick_min'] > ODDS_CLOSE_LEAD.total_seconds() / 60:
        flags.append(f'capture tick {b['tick_min']:.0f} min > {ODDS_CLOSE_LEAD.total_seconds() / 60:.0f}: close and decision pulls land up to a tick early (source: {b['tick_source']}; install ops/systemd/benchedmark-capture.*)')
    return (lines, flags)

def min_interval(src: str, now: datetime) -> timedelta:
    n = now.astimezone(ET)
    sunday = n.weekday() == 6
    if src == 'sleeper_trending':
        return timedelta(minutes=14 if sunday and 10 <= n.hour < 21 else 55)
    if src == 'sleeper_news':
        return timedelta(days=1)
    if src == 'consensus':
        return timedelta(hours=12)
    if src == 'odds':
        return ODDS_DAILY
    return timedelta(0)

def planned_odds_credits(pulls: list[tuple[datetime, str]]) -> int:
    return ODDS_CALL_COST * (len(pulls) + sum((1 for _, k in pulls if k == 'close')))
SLEEPER_MAP_RULE = 'at most once per ET day and per 24 h (docs.sleeper.com, Fetch all players)'

def map_pull_allowed(now: datetime, last: dict | None) -> bool:
    if last is None:
        return True
    at = datetime.fromisoformat(last['retrieved_at'])
    return at.astimezone(ET).date() < now.astimezone(ET).date() and now - at >= timedelta(days=1)

def is_due(src: str, now: datetime, last: dict | None) -> bool:
    if src == 'consensus':
        return False
    if src == 'sleeper_news':
        return map_pull_allowed(now, last)
    if src == 'odds_sharp':
        return _odds_due(now, datetime.fromisoformat(last['retrieved_at']) if last else None, only=('close',)) == 'close'
    if src == 'odds':
        return _odds_due(now, datetime.fromisoformat(last['retrieved_at']) if last else None) is not None
    if last is None:
        return True
    at = datetime.fromisoformat(last['retrieved_at'])
    return now - at >= min_interval(src, now)

def manifest() -> list[dict]:
    if not MANIFEST.exists():
        return []
    return [json.loads(l) for l in MANIFEST.read_text().splitlines() if l.strip()]

def ingest(sources: list[str] | None=None, only_due: bool=False, *, report: dict | None=None) -> list[dict]:
    if report is not None:
        report.clear()
    if sources is None:
        sources = ['odds', 'odds_sharp', 'sleeper_trending', 'sleeper_news']
    RAW.mkdir(parents=True, exist_ok=True)
    last = {m['source']: m for m in manifest()}
    out = []
    for src in sources:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        observation = {'checked_at': now.isoformat(), 'status': 'fetching'}
        if report is not None:
            report[src] = observation
        plan = None
        if src == 'odds':
            plan = _odds_due(now, datetime.fromisoformat(last['odds']['retrieved_at']) if last.get('odds') else None)
            observation['plan'] = plan
        if only_due and (not is_due(src, now, last.get(src))) or (src == 'sleeper_news' and (not map_pull_allowed(now, last.get(src)))):
            observation.update(status='not due', last_snapshot=last.get(src), **{'rule': SLEEPER_MAP_RULE} if src == 'sleeper_news' else {})
            continue
        fetchers = {'sleeper_trending': fetch_sleeper_trending, 'sleeper_news': fetch_sleeper_news, 'consensus': fetch_consensus, 'odds': fetch_odds_api, 'odds_sharp': fetch_odds_sharp}
        if src not in fetchers:
            observation.update(status='failed', error_type='UnknownSource')
            logger.warning('unknown market source: %s', src)
            continue
        try:
            raw_data = fetchers[src]()
        except Exception as exc:
            observation.update(status='failed', error_type=type(exc).__name__)
            logger.error('failed to fetch %s: %s', src, type(exc).__name__)
            continue
        if raw_data is None:
            observation.update(status='unavailable', detail='no snapshot returned; configuration/quota/provider cause unverified')
            continue
        fetch_started_at = now
        now = datetime.now(timezone.utc)
        body = json.dumps(raw_data, sort_keys=True, separators=(',', ':')).encode()
        sha = hashlib.sha256(body).hexdigest()
        row = {'source': src, 'retrieved_at': now.isoformat(), 'fetch_started_at': fetch_started_at.isoformat(), 'retrieval_time_semantics': 'fetch completed; local clock', 'sha256': sha, 'bytes': len(body)}
        counts = {k: len(v) for k, v in raw_data.items() if hasattr(v, '__len__')}
        row['counts'] = counts
        if src in ('odds', 'odds_sharp') and _odds_quota:
            row['quota'] = dict(_odds_quota)
        if src == 'odds':
            row['plan'] = plan if only_due else 'manual'
        if src in last and last[src]['sha256'] == sha:
            row['path'] = last[src]['path']
            row['unchanged'] = True
        else:
            stamp = f'{now.strftime('%Y%m%dT%H%M%SZ')}_{sha[:12]}'
            p = RAW / src / f'{stamp}.json.gz'
            p.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(p, 'wb') as f:
                f.write(body)
            row['path'] = str(p.relative_to(ROOT))
        with open(MANIFEST, 'a') as f:
            f.write(json.dumps(row) + '\n')
        out.append(row)
        observation.update(status='captured', snapshot=row)
        last[src] = row
        _read.cache_clear()
    return out

@lru_cache(maxsize=16)
def _read(path: str) -> dict:
    with gzip.open(ROOT / path, 'rt') as f:
        return json.load(f)

def snapshot_for(source: str, as_of: datetime) -> dict | None:
    best = None
    for m in manifest():
        if m['source'] == source and datetime.fromisoformat(m['retrieved_at']) <= as_of:
            best = m
    return best

def data(source: str, as_of: datetime) -> tuple[dict | None, dict | None]:
    m = snapshot_for(source, as_of)
    if m is None:
        return (None, None)
    return (_read(m['path']), m)

def trending_adds_as_of(as_of: datetime) -> list[dict]:
    d, _ = data('sleeper_trending', as_of)
    return (d or {}).get('adds', [])

def trending_drops_as_of(as_of: datetime) -> list[dict]:
    d, _ = data('sleeper_trending', as_of)
    return (d or {}).get('drops', [])

def odds_as_of(as_of: datetime) -> list[dict]:
    d, _ = data('odds', as_of)
    return (d or {}).get('games', [])

def consensus_as_of(as_of: datetime) -> list[dict]:
    return []

def archive_consensus(root: Path | None=None) -> dict:
    import shutil
    raw = root / 'data' / 'raw' / 'market' if root else RAW
    src, dst = (raw / 'consensus', raw / CONSENSUS_ARCHIVE)
    moved = []
    if src.exists():
        dst.mkdir(parents=True, exist_ok=True)
        for f in sorted(src.iterdir()):
            target = dst / f.name
            if target.exists():
                raise FileExistsError(f'archive already holds {f.name}; nothing moved over it')
            shutil.move(str(f), str(target))
            moved.append(f.name)
        src.rmdir()
    if moved:
        with open(dst / 'MOVED.jsonl', 'a') as fh:
            fh.write(json.dumps({'moved_at': datetime.now(timezone.utc).isoformat(), 'files': moved, 'reason': CONSENSUS_RETIRED, 'published': False}) + '\n')
    return {'moved': len(moved), 'archive': str(dst)}
if __name__ == '__main__':
    import sys
    if sys.argv[1:2] == ['archive-consensus']:
        print(json.dumps(archive_consensus()))

def news_as_of(as_of: datetime) -> dict[str, dict]:
    d, _ = data('sleeper_news', as_of)
    return (d or {}).get('players', {})
