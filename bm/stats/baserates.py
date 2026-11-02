from __future__ import annotations
import csv
import gzip
import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path
from statistics import mean, stdev
from dataclasses import asdict
from bm.engine.rules import load_rules
from bm.engine.scoring import score_player_line
from . import market, nflverse
ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / 'data' / 'cache'
CACHE_FILE = CACHE / 'baserates-v1.json'
logger = logging.getLogger(__name__)

def _read_history_csv(year: int, name: str) -> list[dict]:
    p = ROOT / 'data' / 'raw' / 'nfl-history' / str(year) / f'{name}_{year}.csv.gz'
    if not p.exists():
        return []
    with gzip.open(p, 'rt', encoding='utf-8', newline='') as f:
        return list(csv.DictReader(f))

def _sha256_files(*names: str) -> str:
    h = hashlib.sha256()
    for year in range(2012, 2026):
        for name in names:
            p = ROOT / 'data' / 'raw' / 'nfl-history' / str(year) / f'{name}_{year}.csv.gz'
            if p.exists():
                h.update(p.read_bytes())
    return h.hexdigest()

def _cache_sources() -> dict:
    history = ROOT / 'data' / 'raw' / 'nfl-history'
    paths = [f'{year}/{name}_{year}.csv.gz' for year in range(2012, 2026) for name in ('injuries', 'stats_player_week', 'roster_weekly', 'snap_counts')]
    paths.append('global/players.csv.gz')
    inputs = [{'path': rel, 'sha256': hashlib.sha256((history / rel).read_bytes()).hexdigest() if (history / rel).exists() else None} for rel in paths]
    fingerprint = hashlib.sha256(json.dumps(inputs, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {'schema': 'baserates-source-v3', 'inputs': inputs, 'fingerprint': fingerprint + ':v3', 'original_observation_time_certified': False}

def _points_bucket(pts: float) -> str:
    if pts < 5:
        return '0-5'
    elif pts < 10:
        return '5-10'
    elif pts < 15:
        return '10-15'
    elif pts < 20:
        return '15-20'
    else:
        return '20+'

def build_baserates() -> dict:
    rules = load_rules()
    injuries_by_status: dict[str, dict[str, list[bool]]] = {}
    basis_counts: dict[str, dict[str, dict[str, int]]] = {}
    pfr_to_gsis = {}
    gp = ROOT / 'data' / 'raw' / 'nfl-history' / 'global' / 'players.csv.gz'
    if gp.exists():
        with gzip.open(gp, 'rt', encoding='utf-8', newline='') as f:
            pfr_to_gsis = {r['pfr_id']: r['gsis_id'] for r in csv.DictReader(f) if r.get('pfr_id') and r.get('gsis_id')}
    for year in range(2012, 2026):
        injuries = _read_history_csv(year, 'injuries')
        snaps = _read_history_csv(year, 'snap_counts')
        played = set()
        basis = 'positive_offense_or_special_teams_snap' if snaps else 'regular_season_stat_row_presence'
        if snaps:
            for r in snaps:
                g = pfr_to_gsis.get(r.get('pfr_player_id'))
                if g and r.get('game_type', 'REG') == 'REG' and (float(r.get('offense_snaps') or 0) > 0 or float(r.get('st_snaps') or 0) > 0):
                    played.add((g, str(int(r['week']))))
        else:
            for r in _read_history_csv(year, 'stats_player_week'):
                if r.get('season_type') == 'REG':
                    played.add((r['player_id'], str(int(r['week']))))
        for r in injuries:
            if (r.get('game_type') or r.get('season_type') or 'REG') != 'REG':
                continue
            gsis, week, pos = (r.get('gsis_id'), r.get('week'), r.get('position'))
            if not gsis or not week or pos not in ('QB', 'RB', 'WR', 'TE', 'K'):
                continue
            status = r.get('report_status') or 'none'
            key = f'{pos}|{status}'
            positive = (gsis, str(int(week))) in played
            injuries_by_status.setdefault(key, {}).setdefault(pos, []).append(positive)
            counts = basis_counts.setdefault(key, {}).setdefault(basis, {'n': 0, 'positive': 0})
            counts['n'] += 1
            counts['positive'] += int(positive)
    p_plays_data = []
    for key in sorted(injuries_by_status):
        pos, status = key.split('|')
        by_pos = injuries_by_status[key]
        if pos not in by_pos:
            continue
        plays = by_pos[pos]
        if not plays:
            continue
        n = len(plays)
        rate = sum(plays) / n
        p_plays_data.append({'position': pos, 'report_status': status if status != 'none' else None, 'n': n, 'rate': round(rate, 4), 'basis': next(iter(basis_counts[key])) if len(basis_counts[key]) == 1 else 'mixed', 'basis_counts': basis_counts[key]})
    points_by_bucket: dict[str, dict[str, list[float]]] = {}
    for year in range(2012, 2026):
        stats = _read_history_csv(year, 'stats_player_week')
        roster = _read_history_csv(year, 'roster_weekly')
        pos_map = {}
        for r in roster:
            gsis = r.get('gsis_id')
            if gsis and r.get('position') in ('QB', 'RB', 'WR', 'TE', 'K'):
                pos_map[gsis] = r['position']
        stats_by_player_week = {}
        for r in stats:
            if r.get('season_type') != 'REG':
                continue
            k = (r['player_id'], int(r['week']))
            stats_by_player_week[k] = r
        player_weeks = {}
        for (pid, week), line in sorted(stats_by_player_week.items()):
            if pid not in player_weeks:
                player_weeks[pid] = []
            player_weeks[pid].append((week, line))
        for pid, weeks_lines in player_weeks.items():
            pos = pos_map.get(pid)
            if not pos:
                continue
            for i, (week, line) in enumerate(weeks_lines):
                prior_pts = []
                for j in range(max(0, i - 4), i):
                    prior_pts.append(score_player_line(rules, pos, weeks_lines[j][1]))
                if not prior_pts:
                    avg_pts = 0.0
                else:
                    avg_pts = mean(prior_pts)
                bucket = _points_bucket(avg_pts)
                pts = score_player_line(rules, pos, line)
                key = f'{pos}|{bucket}'
                if key not in points_by_bucket:
                    points_by_bucket[key] = {}
                if pos not in points_by_bucket[key]:
                    points_by_bucket[key][pos] = []
                points_by_bucket[key][pos].append(pts)
    points_data = []
    for key in sorted(points_by_bucket):
        parts = key.split('|')
        if len(parts) != 2:
            continue
        pos, bucket = parts
        by_pos = points_by_bucket[key]
        if pos not in by_pos:
            continue
        pts_list = by_pos[pos]
        if not pts_list or len(pts_list) < 2:
            continue
        pts_sorted = sorted(pts_list)
        n = len(pts_sorted)
        points_data.append({'position': pos, 'prior_form_bucket': bucket, 'n': n, 'mean': round(mean(pts_list), 2), 'sd': round(stdev(pts_list), 2), 'p10': round(pts_sorted[max(0, int(n * 0.1) - 1)], 2), 'p25': round(pts_sorted[max(0, int(n * 0.25) - 1)], 2), 'p50': round(pts_sorted[max(0, int(n * 0.5) - 1)], 2), 'p75': round(pts_sorted[max(0, int(n * 0.75) - 1)], 2), 'p90': round(pts_sorted[max(0, int(n * 0.9) - 1)], 2)})
    error_by_bucket: dict[str, list[float]] = {}
    for year in range(2012, 2026):
        stats = _read_history_csv(year, 'stats_player_week')
        roster = _read_history_csv(year, 'roster_weekly')
        pos_map = {}
        for r in roster:
            gsis = r.get('gsis_id')
            if gsis and r.get('position') in ('QB', 'RB', 'WR', 'TE', 'K'):
                pos_map[gsis] = r['position']
        stats_by_player_week = {}
        for r in stats:
            if r.get('season_type') != 'REG':
                continue
            k = (r['player_id'], int(r['week']))
            stats_by_player_week[k] = r
        player_weeks = {}
        for (pid, week), line in sorted(stats_by_player_week.items()):
            if pid not in player_weeks:
                player_weeks[pid] = []
            player_weeks[pid].append((week, line))
        for pid, weeks_lines in player_weeks.items():
            pos = pos_map.get(pid)
            if not pos:
                continue
            for i, (week, line) in enumerate(weeks_lines):
                prior_pts = []
                for j in range(max(0, i - 4), i):
                    prior_pts.append(score_player_line(rules, pos, weeks_lines[j][1]))
                if not prior_pts:
                    continue
                avg_pts = mean(prior_pts)
                bucket = _points_bucket(avg_pts)
                actual = score_player_line(rules, pos, line)
                error = actual - avg_pts
                key = f'{pos}|{bucket}'
                if key not in error_by_bucket:
                    error_by_bucket[key] = []
                error_by_bucket[key].append(error)
    error_data = []
    for key in sorted(error_by_bucket):
        parts = key.split('|')
        if len(parts) != 2:
            continue
        pos, bucket = parts
        errors = error_by_bucket[key]
        if not errors or len(errors) < 2:
            continue
        error_data.append({'position': pos, 'prior_form_bucket': bucket, 'n': len(errors), 'error_sd': round(stdev(errors), 2)})
    return {'p_plays_given_status': p_plays_data, 'points_distribution': points_data, 'forecast_error_sd': error_data}

def base_rates(kind: str, **filters) -> list[dict]:
    data = _load_cache()
    if kind not in data:
        return []
    rows = data[kind]
    if kind == 'p_plays_given_status':
        rows = [{k: r[k] for k in ('position', 'report_status', 'n', 'rate')} for r in rows]
    for k, v in filters.items():
        rows = [r for r in rows if r.get(k) == v]
    return rows

def base_rate_provenance(kind: str, **filters) -> list[dict]:
    if kind != 'p_plays_given_status':
        return []
    try:
        data = json.loads(CACHE_FILE.read_text())
    except (OSError, ValueError):
        return []
    rows = data.get(kind, [])
    for k, v in filters.items():
        rows = [r for r in rows if r.get(k) == v]
    return [{**r, 'basis': r.get('basis', 'legacy_unknown'), 'basis_counts': r.get('basis_counts'), 'medical_availability_certified': False, 'negative_semantics': 'absence_from_selected_membership_set_not_certified_dnp', 'source_provenance': data.get('_source_provenance'), 'source_currentness': 'not_validated_by_diagnostic_reader'} for r in rows]

def consensus_as_of(as_of: datetime) -> dict[str, dict]:
    from . import platform
    rows = market.consensus_as_of(as_of)
    if not rows:
        return {}
    names = platform.crosswalk()['name']
    out = {}
    for r in rows:
        g = names.get(platform.name_key(r.get('player', ''), r.get('team', '')))
        if not g:
            continue
        f = lambda k: float(r[k]) if r.get(k) not in (None, '', 'NA') else None
        out[g] = {'ecr': f('ecr_1qb'), 'pos_rank': f('ecr_pos'), 'value': int(f('value_1qb') or 0), 'name': r.get('player', ''), 'pos': r.get('pos', ''), 'team': r.get('team', ''), 'scrape_date': r.get('scrape_date')}
    return out

def consensus_coverage(as_of: datetime) -> dict:
    rows = market.consensus_as_of(as_of)
    got = consensus_as_of(as_of)
    return {'rows': len(rows), 'mapped': len(got), 'share': round(len(got) / len(rows), 3) if rows else None}

def market_distribution(pos: str, point: float, levels: tuple[float, ...]=(0.1, 0.25, 0.5, 0.75, 0.9)) -> dict[str, float]:
    import statistics
    bucket = _points_bucket(point)
    errors = base_rates('forecast_error_sd', position=pos, prior_form_bucket=bucket)
    if not errors:
        sd = 3.0
    else:
        sd = errors[0].get('error_sd', 3.0)
    from scipy import stats
    out = {}
    for level in levels:
        z = stats.norm.ppf(level)
        q = point + z * sd
        out[str(level)] = max(0, round(q, 2))
    return out

def _load_cache() -> dict:
    sources = _cache_sources()
    if CACHE_FILE.exists():
        try:
            cached = json.loads(CACHE_FILE.read_text())
            current_sha = sources['fingerprint']
            cached_sha = cached.get('_input_sha256', '')
            if cached_sha == current_sha:
                return cached
        except Exception as e:
            logger.debug(f'cache load failed: {e}')
    logger.info('Building base-rate tables from 2012-2025 history...')
    data = build_baserates()
    data['_input_sha256'] = sources['fingerprint']
    data['_source_provenance'] = sources
    CACHE.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(data, indent=2))
    return data

def crosswalk(season: int=2026) -> dict[str, dict[str, str]]:
    from . import platform
    try:
        return platform.crosswalk(season)
    except Exception:
        return {}
