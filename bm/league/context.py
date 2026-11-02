from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from bm.engine.rules import Rules
from bm.engine.state import World
from bm.stats import nflverse as nv
from bm.stats import projections
SEASON = 2026
SOURCE_PROVENANCE_FROM = datetime.fromisoformat('2026-10-12T00:00:00-04:00')

@dataclass
class View:
    rules: Rules
    world: World
    week: int
    as_of: datetime
    mode: str
    proj: dict
    lines: dict
    injuries: list
    games: list
    opp: dict = field(default_factory=dict)
    data_refs: list = field(default_factory=list)

    def ptoken(self, pid: str, with_lock: bool=False) -> str:
        p = self.world.players.get(pid, {'name': pid, 'pos': '?', 'team': '?'})
        pr = self.proj.get(pid, {})
        st = pr.get('status')
        bye = p['team'] not in self.opp and p['pos'] != '?'
        parts = [pid, p['name'], p['pos'], p['team']]
        if st:
            parts.append(f'[{st}]')
        parts.append(f'wk={pr.get('week_proj', 0):.1f}')
        parts.append(f'ros={pr.get('ros_ppg', 0):.1f}')
        parts.append(f'ytd={pr.get('ytd', 0):.0f}/{pr.get('games', 0)}g')
        if bye:
            parts.append('bye')
        elif with_lock:
            o, k = self.opp[p['team']]
            locked = self.as_of >= datetime.fromisoformat(k)
            parts.append(f'vs {o} {datetime.fromisoformat(k).strftime('%a %H:%M')}' + (' LOCKED' if locked else ''))
        return ' '.join(parts)
FRESH_STATUS_FROM = datetime.fromisoformat('2026-10-08T00:00:00-04:00')

@lru_cache(maxsize=4)
def _world_players(season: int, week: int, as_iso: str) -> tuple:
    as_of = datetime.fromisoformat(as_iso)
    players = {}
    team_by_week: dict[int, dict[str, str]] = {}
    for w in range(1, week + 1):
        u = nv.universe(season, w, as_of)
        team_by_week[w] = {pid: p['team'] for pid, p in u.items()}
        for pid, p in u.items():
            players[pid] = p
    if as_of >= FRESH_STATUS_FROM:
        for pid, d in nv.departed(season, week, as_of).items():
            if pid in players:
                players[pid] = {**players[pid], 'roster_status': d['code'], 'team': players[pid]['team'] if d['code'] == 'DEV' else 'FA'}
    return (players, team_by_week)

def build_view(rules: Rules, week: int, as_of: datetime, extra_players: set[str]=frozenset(), strict_sources: bool=False) -> View:
    mode = nv.visibility_mode(as_of)
    players, tbw = _world_players(SEASON, week, as_of.isoformat())
    players = dict(players)
    if extra_players:
        for pid in extra_players:
            if pid not in players:
                players[pid] = {'name': pid, 'pos': 'WR', 'team': 'FA', 'roster_status': 'CUT'}
    source_as_of = as_of if strict_sources or as_of >= SOURCE_PROVENANCE_FROM else None
    kick = nv.kickoffs_by_week(SEASON, source_as_of) if source_as_of else nv.kickoffs_by_week(SEASON)
    world = World(players=players, kickoffs=kick, team_by_week=tbw)
    lines, refs = nv.weekly_lines(SEASON, as_of, schedule_as_of=source_as_of) if source_as_of else nv.weekly_lines(SEASON, as_of)
    inj = nv.injuries(SEASON, as_of, week)
    schedule = nv.schedule(SEASON, source_as_of) if source_as_of else nv.schedule(SEASON)
    games = [g for g in schedule if g['week'] == week]
    opp = {}
    for g in games:
        opp[g['away']] = ('@' + g['home'], g['kickoff'])
        opp[g['home']] = (g['away'], g['kickoff'])
    fresh = as_of >= FRESH_STATUS_FROM
    extra_status = {}
    if fresh:
        from bm.stats import platform
        b, status_ref = platform.board('sleeper_api', as_of)
        extra_status = {pid: v['status'] for pid, v in (b or {}).items() if v.get('status')}
    prior = nv.prior_lines(SEASON, source_as_of) if source_as_of else nv.prior_lines(SEASON)
    proj = projections.build(rules, players, lines, prior, inj, week, set(opp), extra_status=extra_status, fresh=fresh)
    if source_as_of is not None:
        for asset in (f'roster_weekly_{SEASON}', 'games', f'injuries_{SEASON}', f'stats_player_week_{SEASON - 1}'):
            m = nv.snapshot_for(asset, None if mode == 'reconstructed' else as_of)
            if m:
                refs.append(m)
        if fresh and status_ref:
            refs.append(dict(status_ref, asset='platform:sleeper_api'))
    return View(rules=rules, world=world, week=week, as_of=as_of, mode=mode, proj=proj, lines=lines, injuries=inj, games=games, opp=opp, data_refs=[{k: r.get(k) for k in ('asset', 'retrieved_at', 'sha256')} for r in refs])
_ENGINE_WORLD: dict = {}

def engine_world() -> World:
    m = nv.snapshot_for(f'roster_weekly_{SEASON}')
    games = nv.snapshot_for('games')
    key = (m['sha256'] if m else None, games['sha256'] if games else None)
    if key in _ENGINE_WORLD:
        return _ENGINE_WORLD[key]
    rs, _ = nv.rows(f'roster_weekly_{SEASON}')
    weeks = sorted({int(r['week']) for r in rs if r['week']})
    players, tbw = ({}, {})
    for w in weeks:
        u = nv.universe(SEASON, w)
        tbw[w] = {pid: p['team'] for pid, p in u.items()}
        players.update(u)
    w = World(players=players, kickoffs=nv.kickoffs_by_week(SEASON), team_by_week=tbw)
    _ENGINE_WORLD.clear()
    _ENGINE_WORLD[key] = w
    return w
