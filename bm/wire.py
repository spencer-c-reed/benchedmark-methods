from __future__ import annotations
import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from bm.stats import market, nflverse
ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / 'data' / 'cache'
WIRE_INDEX = CACHE / 'wire.jsonl'
logger = logging.getLogger(__name__)

def _short_id(item_dict: dict) -> str:
    s = json.dumps({'player_id': item_dict.get('player_id'), 'kind': item_dict.get('kind'), 'first_seen_at': item_dict.get('first_seen_at'), 'headline': item_dict.get('headline')}, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(s.encode()).hexdigest()[:12]
FANTASY_POS = ('QB', 'RB', 'WR', 'TE', 'K', 'DEF')

def _sleeper_headline(player_name: str, team: str, pos: str, status: str, body_part: str, practice: str) -> str:
    h = f'{player_name} ({team or 'FA'} {pos})'
    if status and status != 'Active':
        h += f' listed {status}' + (f' ({body_part.lower()})' if body_part else '')
    if practice and practice != 'Full Participation in Practice':
        h += f'; practice: {practice}'
    return h

def build_wire(until: Optional[datetime]=None) -> list[dict]:
    items = {}
    sleeper_snapshots = []
    for m in market.manifest():
        if m['source'] == 'sleeper_news':
            retrieved_at = datetime.fromisoformat(m['retrieved_at'])
            if until is None or retrieved_at <= until:
                sleeper_snapshots.append((retrieved_at, m))
    sleeper_snapshots.sort(key=lambda x: x[0])
    try:
        from bm.stats.platform import crosswalk
        s2g = crosswalk(2026)['sleeper']
    except Exception:
        s2g = {}
    prev_state = {}
    for retrieved_at, m in sleeper_snapshots:
        data = market._read(m['path'])
        players = data.get('players', {})
        for sleeper_id, p in players.items():
            if p.get('position') not in FANTASY_POS or not (p.get('team') or p.get('injury_status') or p.get('practice_participation')):
                continue
            state = (p.get('injury_status'), p.get('injury_body_part'), p.get('practice_participation'), p.get('depth_chart_order'), p.get('team'))
            prev = prev_state.get(sleeper_id)
            if prev != state and (p.get('injury_status') or p.get('practice_participation')):
                headline = _sleeper_headline(p.get('full_name', ''), p.get('team') or '', p.get('position') or '', p.get('injury_status') or '', p.get('injury_body_part') or '', p.get('practice_participation') or '')
                item = {'kind': 'sleeper_status', 'player_id': sleeper_id, 'sleeper_id': sleeper_id, 'gsis_id': s2g.get(str(sleeper_id)), 'player_name': p.get('full_name', ''), 'team': p.get('team') or '', 'pos': p.get('position') or '', 'headline': headline, 'body': json.dumps({'injury_status': p.get('injury_status'), 'injury_body_part': p.get('injury_body_part'), 'injury_notes': p.get('injury_notes'), 'practice_participation': p.get('practice_participation'), 'depth_chart_order': p.get('depth_chart_order'), 'depth_chart_position': p.get('depth_chart_position')}, separators=(',', ':')), 'source': 'sleeper_status', 'published_at': datetime.fromtimestamp(p.get('news_updated', 0) / 1000, tz=timezone.utc).isoformat() if p.get('news_updated') else retrieved_at.isoformat(), 'first_seen_at': retrieved_at.isoformat()}
                item_id = _short_id(item)
                if item_id not in items:
                    items[item_id] = item
                    items[item_id]['id'] = item_id
            prev_state[sleeper_id] = state
    for year in sorted({int(m['asset'].split('_')[1]) for m in nflverse.manifest() if str(m.get('asset', '')).startswith('injuries_')}):
        snapshots_by_time = {}
        nfl_manifest = nflverse.manifest()
        for m in nfl_manifest:
            if m.get('asset') == f'injuries_{year}':
                retrieved_at = datetime.fromisoformat(m['retrieved_at'])
                if until is None or retrieved_at <= until:
                    snapshots_by_time[retrieved_at] = m
        if not snapshots_by_time:
            continue
        seen = {}
        for retrieved_at in sorted(snapshots_by_time):
            m = snapshots_by_time[retrieved_at]
            rows = nflverse._read_snapshot(m['path'])
            for r in rows:
                key = (r.get('gsis_id'), r.get('week'), r.get('report_status'), r.get('practice_status'))
                if key not in seen:
                    seen[key] = retrieved_at.isoformat()
        if not snapshots_by_time:
            continue
        latest_m = snapshots_by_time[max(snapshots_by_time)]
        latest_rows = nflverse._read_snapshot(latest_m['path'])
        for r in latest_rows:
            gsis = r.get('gsis_id')
            week = r.get('week')
            pos = r.get('position')
            status = r.get('report_status')
            practice = r.get('practice_status')
            if not gsis or not week or pos not in ('QB', 'RB', 'WR', 'TE', 'K'):
                continue
            key = (gsis, week, status, practice)
            first_seen = seen.get(key, latest_m['retrieved_at'])
            headline = f'{r.get('full_name', '')} ({r.get('team', '')} {pos})'
            if status and status != 'Active':
                headline += f' listed {status}'
                if r.get('report_primary_injury'):
                    headline += f' ({r['report_primary_injury'].lower()})'
            if practice:
                headline += f'; practice: {practice}' + (f' ({r['practice_primary_injury'].lower()})' if r.get('practice_primary_injury') and (not status) else '')
            headline += f' (week {week} report)'
            item = {'kind': 'injury_report', 'player_id': gsis, 'gsis_id': gsis, 'player_name': r.get('full_name', ''), 'team': r.get('team', ''), 'pos': pos, 'headline': headline, 'body': json.dumps({'week': week, 'report_status': status, 'report_primary_injury': r.get('report_primary_injury'), 'practice_status': practice}, separators=(',', ':')), 'source': 'nflverse_injuries', 'published_at': first_seen, 'first_seen_at': first_seen}
            item_id = _short_id(item)
            if item_id not in items:
                items[item_id] = item
                items[item_id]['id'] = item_id
    out = sorted(items.values(), key=lambda x: (x['first_seen_at'], x['id']))
    return out

def refresh_wire(until: Optional[datetime]=None) -> None:
    items = build_wire(until)
    existing_ids = set()
    if WIRE_INDEX.exists():
        for line in WIRE_INDEX.read_text().splitlines():
            if line.strip():
                try:
                    r = json.loads(line)
                    existing_ids.add(r['id'])
                except Exception:
                    pass
    CACHE.mkdir(parents=True, exist_ok=True)
    with open(WIRE_INDEX, 'a') as f:
        for item in items:
            if item['id'] not in existing_ids:
                f.write(json.dumps(item) + '\n')

def news_search(query: str, as_of: datetime, since: Optional[datetime]=None, players: Optional[list[str]]=None, limit: int=20) -> list[dict]:
    items = []
    if WIRE_INDEX.exists():
        for line in WIRE_INDEX.read_text().splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
                first_seen = datetime.fromisoformat(item['first_seen_at'])
                if first_seen > as_of:
                    continue
                if since and first_seen < since:
                    continue
                if players and item.get('player_name') not in players and (item.get('player_id') not in players) and (item.get('gsis_id') not in players):
                    continue
                search_text = f'{item.get('headline', '')} {item.get('player_name', '')} {item.get('team', '')}'.lower()
                if all((tok in search_text for tok in query.lower().split())):
                    items.append(item)
            except Exception:
                pass
    items.sort(key=lambda x: (x['first_seen_at'], x['id']), reverse=True)
    out = []
    for item in items[:limit]:
        out.append({'id': item['id'], 'headline': item.get('headline', ''), 'source': item.get('source', ''), 'published_at': item.get('published_at', ''), 'first_seen_at': item['first_seen_at']})
    return out

def news_item(item_id: str, as_of: datetime) -> dict:
    if not WIRE_INDEX.exists():
        return {'error': 'item not found'}
    for line in WIRE_INDEX.read_text().splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
            if item['id'] != item_id:
                continue
            first_seen = datetime.fromisoformat(item['first_seen_at'])
            if first_seen > as_of:
                return {'error': f'item not visible at {as_of}'}
            return item
        except Exception:
            pass
    return {'error': 'item not found'}
