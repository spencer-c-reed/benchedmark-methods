from __future__ import annotations
import csv
import gzip
import hashlib
import io
import json
import os
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo
from bm.paths import runtime_root
ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / 'data' / 'raw' / 'nfl'
SNAP = RAW / 'snapshots'
MANIFEST = RAW / 'manifest.jsonl'
ET = ZoneInfo('America/New_York')
REL = 'https://github.com/nflverse/nflverse-data/releases/download'
UA = {'User-Agent': 'BenchedMark-research/0.1 (+benchedmark.com)'}

def assets(season: int) -> dict[str, tuple[str, str | None]]:
    return {'games': ('https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv', None), f'stats_player_week_{season}': (f'{REL}/stats_player/stats_player_week_{season}.csv', 'stats_player'), f'stats_team_week_{season}': (f'{REL}/stats_team/stats_team_week_{season}.csv', 'stats_team'), f'injuries_{season}': (f'{REL}/injuries/injuries_{season}.csv', 'injuries'), f'roster_weekly_{season}': (f'{REL}/weekly_rosters/roster_weekly_{season}.csv', 'weekly_rosters'), f'stats_player_week_{season - 1}': (f'{REL}/stats_player/stats_player_week_{season - 1}.csv', 'stats_player')}

def _get(url: str, tries: int=3) -> bytes:
    err = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.read()
        except Exception as e:
            err = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f'fetch failed {url}: {err}')

def _release_updated(tag: str, name: str) -> str | None:
    try:
        d = json.loads(_get(f'https://api.github.com/repos/nflverse/nflverse-data/releases/tags/{tag}'))
        for a in d.get('assets', []):
            if a['name'] == name:
                return a['updated_at']
    except Exception:
        return None
    return None

def _read_root() -> Path:
    return ROOT if MANIFEST.exists() else runtime_root(ROOT)

def manifest() -> list[dict]:
    path = MANIFEST if MANIFEST.exists() else _read_root() / 'data/raw/nfl/manifest.jsonl'
    if not path.exists():
        return []
    st = path.stat()
    return [dict(m) for m in _manifest_at(str(path.resolve()), st.st_mtime_ns, st.st_size)]

@lru_cache(maxsize=4)
def _manifest_at(path: str, mtime_ns: int, size: int) -> tuple[dict, ...]:
    return tuple((json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()))

def ingest(season: int=2026, only: list[str] | None=None) -> list[dict]:
    if (ROOT / '.git').is_file() or _read_root() != ROOT:
        raise RuntimeError('nflverse ingest requires a serving checkout; runtime snapshots are read-only from worktrees')
    SNAP.mkdir(parents=True, exist_ok=True)
    out = []
    last = {}
    for m in manifest():
        last[m['asset']] = m
    for name, (url, tag) in assets(season).items():
        if only and name not in only:
            continue
        now = datetime.now(timezone.utc).replace(microsecond=0)
        b = _get(url)
        sha = hashlib.sha256(b).hexdigest()
        row = {'asset': name, 'url': url, 'retrieved_at': now.isoformat(), 'sha256': sha, 'bytes': len(b), 'source_updated_at': _release_updated(tag, url.rsplit('/', 1)[1]) if tag else None}
        if name in last and last[name]['sha256'] == sha:
            row['path'] = last[name]['path']
            row['unchanged'] = True
        else:
            p = SNAP / name / f'{now.strftime('%Y%m%dT%H%M%SZ')}_{sha[:12]}.csv.gz'
            p.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(p, 'wb') as f:
                f.write(b)
            row['path'] = str(p.relative_to(ROOT))
        with open(MANIFEST, 'a') as f:
            f.write(json.dumps(row) + '\n')
        out.append(row)
    _read_snapshot_at.cache_clear()
    return out

def first_ingest_at() -> datetime | None:
    m = manifest()
    return datetime.fromisoformat(m[0]['retrieved_at']) if m else None

def snapshot_for(asset: str, as_of: datetime | None=None) -> dict | None:
    best = None
    for m in manifest():
        if m['asset'] != asset:
            continue
        if as_of is not None and datetime.fromisoformat(m['retrieved_at']) > as_of:
            continue
        best = m
    return best

def _read_snapshot(path: str) -> tuple[dict, ...]:
    return _read_snapshot_at((_read_root() / path).resolve())

@lru_cache(maxsize=32)
def _read_snapshot_at(path: Path) -> tuple[dict, ...]:
    with gzip.open(path, 'rt', encoding='utf-8', newline='') as f:
        return tuple(csv.DictReader(f))

def rows(asset: str, as_of: datetime | None=None, mode: str='observed') -> tuple[list[dict], dict]:
    m = snapshot_for(asset, None if mode == 'reconstructed' else as_of)
    if m is None:
        return ([], {})
    return (list(_read_snapshot(m['path'])), m)

def visibility_mode(as_of: datetime) -> str:
    fi = first_ingest_at()
    return 'reconstructed' if fi is None or as_of < fi else 'observed'

def kickoff_iso(gameday: str, gametime: str) -> str:
    hh, mm = (gametime or '13:00').split(':')[:2]
    d = datetime.fromisoformat(gameday).replace(hour=int(hh), minute=int(mm), tzinfo=ET)
    return d.isoformat()
_TBD_WARNED: set = set()

def schedule(season: int, as_of: datetime | None=None, *, source_receipt: dict | None=None) -> list[dict]:
    mode = visibility_mode(as_of) if as_of else 'observed'
    rs, source = rows('games', as_of, mode) if as_of else rows('games')
    if source_receipt is not None:
        source_receipt.clear()
        source_receipt.update(source)
    out = []
    earliest: dict[int, str] = {}
    for r in rs:
        if r['season'] == str(season) and r['game_type'] == 'REG' and r.get('gametime'):
            wd = datetime.fromisoformat(r['gameday']).weekday()
            if wd not in earliest or r['gametime'] < earliest[wd]:
                earliest[wd] = r['gametime']
    for r in rs:
        if r['season'] != str(season) or r['game_type'] != 'REG':
            continue
        gt = r['gametime']
        if not gt:
            gt = earliest.get(datetime.fromisoformat(r['gameday']).weekday(), '09:30')
            if r['game_id'] not in _TBD_WARNED:
                _TBD_WARNED.add(r['game_id'])
                print(f'WARNING schedule: {r['game_id']} has no kickoff time; locking at the earliest slot that day ({gt} ET)', flush=True)
        out.append({'game_id': r['game_id'], 'week': int(r['week']), 'kickoff': kickoff_iso(r['gameday'], gt), 'away': r['away_team'], 'home': r['home_team'], 'away_score': int(r['away_score']) if r['away_score'] not in ('', 'NA') else None, 'home_score': int(r['home_score']) if r['home_score'] not in ('', 'NA') else None})
    return out

def kickoffs_by_week(season: int, as_of: datetime | None=None) -> dict[int, dict[str, str]]:
    k: dict[int, dict[str, str]] = {}
    for g in schedule(season, as_of) if as_of else schedule(season):
        k.setdefault(g['week'], {})[g['away']] = g['kickoff']
        k.setdefault(g['week'], {})[g['home']] = g['kickoff']
    return k

def game_end(kickoff: str) -> datetime:
    return datetime.fromisoformat(kickoff) + timedelta(hours=4)

def current_week(season: int, now: datetime) -> int:
    weeks: dict[int, datetime] = {}
    for g in schedule(season):
        e = game_end(g['kickoff'])
        weeks[g['week']] = max(weeks.get(g['week'], e), e)
    for w in sorted(weeks):
        if now < weeks[w]:
            return w
    return max(weeks)
OFFENSE = {'QB', 'RB', 'WR', 'TE', 'K'}
TEAMS = ['ARI', 'ATL', 'BAL', 'BUF', 'CAR', 'CHI', 'CIN', 'CLE', 'DAL', 'DEN', 'DET', 'GB', 'HOU', 'IND', 'JAX', 'KC', 'LA', 'LAC', 'LV', 'MIA', 'MIN', 'NE', 'NO', 'NYG', 'NYJ', 'PHI', 'PIT', 'SEA', 'SF', 'TB', 'TEN', 'WAS']

def universe(season: int, week: int, as_of: datetime | None=None) -> dict[str, dict]:
    mode = visibility_mode(as_of) if as_of else 'observed'
    rs, _ = rows(f'roster_weekly_{season}', as_of, mode)
    weeks = sorted({int(r['week']) for r in rs if r['week']})
    use = max([w for w in weeks if w <= week] or weeks[:1] or [week])
    out: dict[str, dict] = {}
    for r in rs:
        if r['week'] != str(use) or r['position'] not in OFFENSE or r['status'] not in ('ACT', 'RES', 'INA'):
            continue
        if not r['gsis_id']:
            continue
        out[r['gsis_id']] = {'name': r['full_name'], 'pos': r['position'], 'team': r['team'], 'roster_status': r['status'], 'years_exp': r.get('years_exp') or '', 'sleeper_id': r.get('sleeper_id') or '', 'espn_id': r.get('espn_id') or ''}
    for t in TEAMS:
        out[f'DEF-{t}'] = {'name': f'{t} Defense', 'pos': 'DEF', 'team': t, 'roster_status': 'ACT'}
    return out
PA_EXCLUDES_OFFENSE_TDS_FROM_WEEK = None
DEPARTED = {'CUT': 'FA', 'DEV': 'PS', 'RET': 'RET'}

def departed(season: int, week: int, as_of: datetime | None=None) -> dict[str, dict]:
    mode = visibility_mode(as_of) if as_of else 'observed'
    rs, _ = rows(f'roster_weekly_{season}', as_of, mode)
    newest: dict[str, tuple[int, set, str]] = {}
    for r in rs:
        g, w = (r.get('gsis_id'), r.get('week'))
        if not g or not w or r.get('position') not in OFFENSE or (int(w) > week):
            continue
        w = int(w)
        cur = newest.get(g)
        if cur is None or w > cur[0]:
            newest[g] = (w, {r['status']}, r['team'])
        elif w == cur[0]:
            cur[1].add(r['status'])
    out = {}
    for g, (w, sts, team) in newest.items():
        if sts & {'ACT', 'RES', 'INA'}:
            continue
        code = next((c for c in ('RET', 'CUT', 'DEV') if c in sts), None)
        if code:
            out[g] = {'code': code, 'status': DEPARTED[code], 'team': team, 'week': w}
    return out

def weekly_lines(season: int, as_of: datetime | None=None, through_week: int | None=None, schedule_as_of: datetime | None=None) -> tuple[dict[int, dict[str, dict]], list[dict]]:
    mode = visibility_mode(as_of) if as_of else 'observed'
    ps, m1 = rows(f'stats_player_week_{season}', as_of, mode)
    ts, m2 = rows(f'stats_team_week_{season}', as_of, mode)
    sched = {g['game_id']: g for g in (schedule(season, schedule_as_of) if schedule_as_of else schedule(season))}

    def visible(game_id: str) -> bool:
        g = sched.get(game_id)
        if g is None:
            return False
        return as_of is None or game_end(g['kickoff']) <= as_of
    out: dict[int, dict[str, dict]] = {}
    for r in ps:
        if r.get('season_type') != 'REG':
            continue
        w = int(r['week'])
        if through_week is not None and w > through_week or not visible(r['game_id']):
            continue
        out.setdefault(w, {})[r['player_id']] = r
    for r in ts:
        if r.get('season_type') != 'REG':
            continue
        w = int(r['week'])
        if through_week is not None and w > through_week or not visible(r['game_id']):
            continue
        g = sched[r['game_id']]
        opp_score = g['home_score'] if r['team'] == g['away'] else g['away_score']
        if opp_score is None:
            continue
        line = dict(r)
        line['points_allowed'] = opp_score
        if PA_EXCLUDES_OFFENSE_TDS_FROM_WEEK is not None and w >= PA_EXCLUDES_OFFENSE_TDS_FROM_WEEK:
            opp = next((x for x in ts if x['game_id'] == r['game_id'] and x['team'] != r['team']), None)
            n = int(float((opp or {}).get('def_tds') or 0))
            line['points_allowed'] = max(0, opp_score - 7 * n)
            line['points_allowed_raw'] = opp_score
        out.setdefault(w, {})[f'DEF-{r['team']}'] = line
    return (out, [m for m in (m1, m2) if m])

def prior_lines(season: int, as_of: datetime | None=None) -> dict[str, list[dict]]:
    mode = visibility_mode(as_of) if as_of else 'observed'
    ps, _ = rows(f'stats_player_week_{season - 1}', as_of, mode) if as_of else rows(f'stats_player_week_{season - 1}')
    out: dict[str, list[dict]] = {}
    for r in ps:
        if r.get('season_type') == 'REG':
            out.setdefault(r['player_id'], []).append(r)
    return out

def injuries(season: int, as_of: datetime, week: int) -> list[dict]:
    mode = visibility_mode(as_of)
    if mode == 'observed':
        seen: dict[tuple, str] = {}
        latest: list[dict] = []
        scanned: set[str] = set()
        for m in manifest():
            if m['asset'] != f'injuries_{season}' or datetime.fromisoformat(m['retrieved_at']) > as_of:
                continue
            rs = _read_snapshot(m['path'])
            latest = list(rs)
            if m['path'] in scanned:
                continue
            scanned.add(m['path'])
            for r in rs:
                key = (r['gsis_id'], r['week'], r['report_status'], r['practice_status'])
                seen.setdefault(key, m['retrieved_at'])
        out = []
        for r in latest:
            if r['week'] != str(week):
                continue
            key = (r['gsis_id'], r['week'], r['report_status'], r['practice_status'])
            out.append(dict(r, first_seen_at=seen[key], visibility='observed'))
        return out
    rs, _ = rows(f'injuries_{season}', as_of, 'reconstructed')
    ko = kickoffs_by_week(season).get(week, {})
    out = []
    for r in rs:
        if r['week'] != str(week) or r['team'] not in ko:
            continue
        vis = datetime.fromisoformat(ko[r['team']]) - timedelta(hours=40)
        if as_of >= vis:
            out.append(dict(r, first_seen_at=vis.isoformat(), visibility='reconstructed'))
    return out
