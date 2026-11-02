from __future__ import annotations
import gzip
import hashlib
import json
import time
import urllib.request
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / 'data' / 'raw' / 'platform'
MANIFEST = RAW / 'manifest.jsonl'
UA = {'User-Agent': 'Mozilla/5.0 (BenchedMark-research/0.1; +benchedmark.com)'}
ESPN_BASE = 'https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{season}/segments/0/leaguedefaults/{sc}?view=kona_player_info'
ESPN_FILTER = '{"players":{"limit":1500,"sortDraftRanks":{"sortPriority":100,"sortAsc":true,"value":"PPR"}}}'
SLEEPER_POS = '&'.join((f'position[]={p}' for p in ('QB', 'RB', 'WR', 'TE', 'K', 'DEF')))
SLEEPER_SEASON = 'https://api.sleeper.app/projections/nfl/{season}?season_type=regular&' + SLEEPER_POS + '&order_by=adp_half_ppr'
SLEEPER_WEEK = 'https://api.sleeper.app/projections/nfl/{season}/{week}?season_type=regular&' + SLEEPER_POS
ESPN_TEAM = {1: 'ATL', 2: 'BUF', 3: 'CHI', 4: 'CIN', 5: 'CLE', 6: 'DAL', 7: 'DEN', 8: 'DET', 9: 'GB', 10: 'TEN', 11: 'IND', 12: 'KC', 13: 'LV', 14: 'LA', 15: 'MIA', 16: 'MIN', 17: 'NE', 18: 'NO', 19: 'NYG', 20: 'NYJ', 21: 'PHI', 22: 'ARI', 23: 'PIT', 24: 'LAC', 25: 'SF', 26: 'SEA', 27: 'TB', 28: 'WAS', 29: 'CAR', 30: 'JAX', 33: 'BAL', 34: 'HOU'}
SLEEPER_TEAM = {'LAR': 'LA', 'WSH': 'WAS', 'JAC': 'JAX'}
ESPN_STATUS = {'ACTIVE': '', 'NORMAL': '', 'QUESTIONABLE': 'Questionable', 'DOUBTFUL': 'Doubtful', 'OUT': 'Out', 'INJURY_RESERVE': 'IR', 'SUSPENSION': 'Out', 'DAY_TO_DAY': 'Questionable'}
SLEEPER_STATUS = {'Questionable': 'Questionable', 'Doubtful': 'Doubtful', 'Out': 'Out', 'IR': 'IR', 'PUP': 'IR', 'Sus': 'Out', 'NA': 'Out', 'COV': 'Out', 'DNR': 'Out'}
DP = 'https://github.com/dynastyprocess/data/raw/master/files/'
FP_TEAM = {'JAC': 'JAX', 'LAR': 'LA', 'WSH': 'WAS'}
FIRST_PARTY = False
FIRST_PARTY_SOURCES = ('espn', 'sleeper')
SOURCES = ('sleeper_api',)

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

def name_key(name: str, team: str, *, aliases: dict[str, str] | None=None) -> str:
    import re
    name = (aliases or {}).get(name, name)
    n = re.sub('\\b(jr|sr|ii|iii|iv|v)\\b\\.?', '', name.lower())
    return re.sub('[^a-z]', '', n) + '|' + FP_TEAM.get(team, team)

def crosswalk(season: int=2026) -> dict[str, dict[str, str]]:
    from bm.stats import nflverse as nv
    rs, _ = nv.rows(f'roster_weekly_{season}')
    out: dict[str, dict[str, str]] = {'espn': {}, 'sleeper': {}, 'yahoo': {}, 'name': {}}
    for r in sorted(rs, key=lambda r: int(r['week'] or 0)):
        g = r.get('gsis_id')
        if not g:
            continue
        out['name'][name_key(r.get('full_name', ''), r.get('team', ''))] = g
        for k in ('espn', 'sleeper', 'yahoo'):
            v = (r.get(f'{k}_id') or '').strip()
            if v:
                out[k][v.split('.')[0]] = g
    return out

def normalize_espn(std: dict, ppr: dict, xw: dict[str, str]) -> tuple[dict, list[str]]:

    def by_week(payload):
        out = {}
        for x in payload.get('players', []):
            p = x['player']
            wk = {}
            for s in p.get('stats', []):
                if s.get('statSourceId') == 1 and s.get('statSplitTypeId') == 1 and s.get('scoringPeriodId'):
                    wk[str(s['scoringPeriodId'])] = (s['seasonId'], float(s.get('appliedTotal') or 0.0))
            out[p['id']] = wk
        return out
    std_w = by_week(std)
    board, unmapped = ({}, [])
    season = None
    for x in ppr.get('players', []):
        p = x['player']
        pid = p['id']
        if p.get('defaultPositionId') == 16:
            g = f'DEF-{ESPN_TEAM.get(p.get('proTeamId'), '?')}'
        else:
            g = xw.get(str(pid))
        if not g or g.endswith('-?'):
            unmapped.append(f'{pid}:{p.get('fullName')}')
            continue
        ranks = p.get('draftRanksByRankType') or {}
        proj = {}
        for s in p.get('stats', []):
            if s.get('statSourceId') == 1 and s.get('statSplitTypeId') == 1 and s.get('scoringPeriodId'):
                season = season or s['seasonId']
                if s['seasonId'] != season:
                    continue
                w = str(s['scoringPeriodId'])
                a = float(s.get('appliedTotal') or 0.0)
                b = std_w.get(pid, {}).get(w, (season, a))[1]
                proj[w] = round((a + b) / 2, 2)
        own = p.get('ownership') or {}
        board[g] = {'rank': (ranks.get('PPR') or {}).get('rank') or None, 'rank_std': (ranks.get('STANDARD') or {}).get('rank') or None, 'adp': own.get('averageDraftPosition'), 'started': own.get('percentStarted'), 'status': ESPN_STATUS.get(p.get('injuryStatus') or 'ACTIVE', ''), 'proj': proj}
    return (board, unmapped)

def normalize_sleeper(season_rows: list, week_rows: dict[int, list], xw: dict[str, str], by_name: dict[str, str] | None=None) -> tuple[dict, list[str]]:

    def gid(r):
        pid = str(r['player_id'])
        pl = r.get('player') or {}
        if pl.get('position') == 'DEF' or pid.isalpha():
            return f'DEF-{SLEEPER_TEAM.get(pid, pid)}'
        return xw.get(pid) or (by_name or {}).get(name_key(f'{pl.get('first_name', '')} {pl.get('last_name', '')}', r.get('team') or ''))
    board, unmapped = ({}, [])
    for r in season_rows:
        g = gid(r)
        st = r.get('stats') or {}
        adp = st.get('adp_half_ppr')
        if not g:
            if adp and adp < 400:
                unmapped.append(f'{r['player_id']}:{(r.get('player') or {}).get('last_name')}')
            continue
        status = SLEEPER_STATUS.get((r.get('player') or {}).get('injury_status') or '', '')
        board[g] = {'rank': None, 'adp': adp if adp and adp < 999 else None, 'started': None, 'status': status, 'proj': {}}
    for w, rows in week_rows.items():
        for r in rows:
            g = gid(r)
            if not g:
                continue
            e = board.setdefault(g, {'rank': None, 'adp': None, 'started': None, 'status': '', 'proj': {}})
            e['proj'][str(w)] = round(float((r.get('stats') or {}).get('pts_half_ppr') or 0.0), 2)
            if r.get('player') and r['player'].get('injury_status') is not None:
                e['status'] = SLEEPER_STATUS.get(r['player']['injury_status'], '')
    ordered = sorted((g for g, e in board.items() if e['adp']), key=lambda g: (board[g]['adp'], g))
    for i, g in enumerate(ordered, 1):
        board[g]['rank'] = i
    return (board, unmapped)

def normalize_fpecr(ecr_rows: list[dict], fp_to_gsis: dict[str, str], by_name: dict[str, str] | None=None) -> tuple[dict, list[str]]:
    pages = [('/nfl/rankings/ros-ppr-overall.php', 0), ('/nfl/rankings/ros-k.php', 10000), ('/nfl/rankings/ros-dst.php', 20000)]
    keyed = []
    unmapped = []
    for page, off in pages:
        for r in ecr_rows:
            if r['fp_page'] != page:
                continue
            if r['pos'] == 'DST':
                g = f'DEF-{FP_TEAM.get(r['tm'] or r['team'], r['tm'] or r['team'])}'
            else:
                g = fp_to_gsis.get(r['id'], 'NA')
                if g in ('', 'NA'):
                    g = (by_name or {}).get(name_key(r['player'], r['tm'] or r['team']), 'NA')
                if g in ('', 'NA'):
                    unmapped.append(f'{r['id']}:{r['player']}')
                    continue
            keyed.append((off + float(r['ecr']), g, r.get('scrape_date')))
    board = {}
    for _, g, sd in sorted(keyed):
        if g not in board:
            board[g] = {'rank': len(board) + 1, 'adp': None, 'started': None, 'status': '', 'proj': {}, 'scrape_date': sd}
    return (board, unmapped)

def normalize_sleeper_api(players: dict, xw: dict[str, str], by_name: dict[str, str] | None=None) -> tuple[dict, list[str]]:
    board, unmapped = ({}, [])
    keyed = []
    for sid, v in players.items():
        if v.get('position') not in ('QB', 'RB', 'WR', 'TE', 'K', 'DEF') or not v.get('search_rank') or v['search_rank'] >= 9999999:
            continue
        if v.get('position') == 'DEF':
            g = f'DEF-{SLEEPER_TEAM.get(sid, sid)}'
        else:
            g = (v.get('gsis_id') or '').strip() or xw.get(str(sid)) or (by_name or {}).get(name_key(v.get('full_name') or '', v.get('team') or ''))
        if not g:
            if v['search_rank'] < 400:
                unmapped.append(f'{sid}:{v.get('full_name')}')
            continue
        keyed.append((v['search_rank'], g, SLEEPER_STATUS.get(v.get('injury_status') or '', '')))
    for r, g, st in sorted(keyed):
        if g not in board:
            board[g] = {'rank': len(board) + 1, 'search_rank': r, 'adp': None, 'started': None, 'status': st, 'proj': {}}
    return (board, unmapped)

class MapNotYet(Exception):
    pass

def fetch(source: str, season: int=2026, weeks: range | None=None) -> tuple[dict, dict, list[str]]:
    xw = crosswalk(season)
    if source == 'espn':
        hdr = {'X-Fantasy-Filter': ESPN_FILTER}
        u1, u3 = (ESPN_BASE.format(season=season, sc=1), ESPN_BASE.format(season=season, sc=3))
        std, ppr = (json.loads(_get(u1, hdr)), json.loads(_get(u3, hdr)))
        board, un = normalize_espn(std, ppr, xw['espn'])
        return ({u1: std, u3: ppr}, board, un)
    if source == 'sleeper':
        raise PermissionError('undocumented Sleeper projections endpoint: not used (source_terms 2026-10-13)')
        if weeks is None:
            st = json.loads(_get('https://api.sleeper.app/v1/state/nfl'))
            weeks = range(int(st['week']), 19)
        us = SLEEPER_SEASON.format(season=season)
        raw = {us: json.loads(_get(us))}
        wk = {}
        for w in weeks:
            u = SLEEPER_WEEK.format(season=season, week=w)
            raw[u] = json.loads(_get(u))
            wk[w] = raw[u]
        board, un = normalize_sleeper(raw[us], wk, xw['sleeper'], xw['name'])
        return (raw, board, un)
    if source == 'sleeper_api':
        from bm.stats import market
        d, m = market.data('sleeper_news', datetime.now(timezone.utc))
        players = (d or {}).get('players') or {}
        if not any((v.get('search_rank') for v in players.values())):
            raise MapNotYet('newest sleeper_news snapshot has no search_rank yet (next daily map pull)')
        board, un = normalize_sleeper_api(players, xw['sleeper'], xw['name'])
        key = f'market:sleeper_news:{m['retrieved_at']}:{m['sha256'][:12]}'
        return ({key: {k: {f: v.get(f) for f in ('position', 'team', 'full_name', 'search_rank', 'injury_status', 'gsis_id')} for k, v in players.items() if v.get('search_rank')}}, board, un)
    if source == 'fpecr':
        import csv
        import io
        ue, ui = (DP + 'db_fpecr_latest.csv', DP + 'db_playerids.csv')
        ecr = list(csv.DictReader(io.StringIO(_get(ue).decode())))
        ids = {r['fantasypros_id']: r['gsis_id'] for r in csv.DictReader(io.StringIO(_get(ui).decode()))}
        board, un = normalize_fpecr(ecr, ids, xw['name'])
        raw = {ue: [r for r in ecr if '/ros-' in r['fp_page'] and 'dynasty' not in r['fp_page']]}
        return (raw, board, un)
    raise ValueError(f'unknown platform source {source!r}')

def manifest() -> list[dict]:
    if not MANIFEST.exists():
        return []
    return [json.loads(l) for l in MANIFEST.read_text().splitlines() if l.strip()]

def ingest(sources=SOURCES, season: int=2026, keep_raw: bool=True) -> list[dict]:
    blocked = [s for s in sources if s in FIRST_PARTY_SOURCES and (not FIRST_PARTY)]
    if blocked:
        raise PermissionError(f"first-party platform sources {blocked} are off (platform.FIRST_PARTY, the operator decision D-1)")
    RAW.mkdir(parents=True, exist_ok=True)
    last = {m['source']: m for m in manifest()}
    out = []
    for src in sources:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        try:
            raw, board, unmapped = fetch(src, season)
        except MapNotYet:
            continue
        body = json.dumps(board, sort_keys=True, separators=(',', ':')).encode()
        sha = hashlib.sha256(body).hexdigest()
        row = {'source': src, 'season': season, 'retrieved_at': now.isoformat(), 'sha256': sha, 'n_players': len(board), 'n_unmapped': len(unmapped), 'unmapped_sample': unmapped[:10], 'urls': sorted(raw)}
        if src in last and last[src]['sha256'] == sha:
            row['path'], row['unchanged'] = (last[src]['path'], True)
        else:
            stamp = f'{now.strftime('%Y%m%dT%H%M%SZ')}_{sha[:12]}'
            p = RAW / src / f'{stamp}.json.gz'
            p.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(p, 'wb') as f:
                f.write(body)
            row['path'] = str(p.relative_to(ROOT))
            if keep_raw:
                rp = RAW / src / 'raw' / f'{stamp}.json.gz'
                rp.parent.mkdir(parents=True, exist_ok=True)
                with gzip.open(rp, 'wt') as f:
                    json.dump(raw, f)
                row['raw_path'] = str(rp.relative_to(ROOT))
        with open(MANIFEST, 'a') as f:
            f.write(json.dumps(row) + '\n')
        out.append(row)
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

def board(source: str, as_of: datetime) -> tuple[dict | None, dict | None]:
    m = snapshot_for(source, as_of)
    if m is None:
        return (None, None)
    return (_read(m['path']), m)
