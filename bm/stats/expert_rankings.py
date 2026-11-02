from __future__ import annotations
import argparse
import csv
import hashlib
import io
import json
import re
import subprocess
from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from bm.stats.platform import name_key
DATA = Path(__file__).resolve().parents[2] / 'data' / 'expert-rankings' / 'rankings.csv'
FIELDS = 'week source ranker scoring position pos_rank overall_rank player team proj_points pick_type published_at_quoted published_at_basis url live_or_archive read_date'.split()
EXTRA = ['raw_ranker', 'available_at', 'timing', 'input_sha256', 'input_row']
RULE = 'fantasypros-ecr-first-v2'
HISTORY_SCHEMA = 'expert-rankings-history/1'
SOURCES = {('FantasyPros', 'ECR consensus'): 0, ('FantasyPros', 'most accurate experts'): 1, ('Yahoo Sports', 'Hayden Winks'): 2, ('ESPN', 'Eric Karabell'): 3, ('RotoBaller', 'Nick Mariano'): 4, ('Athlon Sports', 'Cory Bonini'): 5}
WEEK8_NAME_ALIASES = {'Chigoziem Okonkwo': 'Chig Okonkwo', 'Drew Ogletree': 'Andrew Ogletree', 'Hollywood Brown': 'Marquise Brown', 'Joshua Palmer': 'Josh Palmer', 'Kenneth Gainwell': 'Kenny Gainwell', 'Matt Hibner': 'Matthew Hibner', 'Mitch Tinsley': 'Mitchell Tinsley', 'Zonovan Knight': 'Bam Knight'}

def verify_receipts(urls: list[str], input_hashes: set[str]) -> dict[str, list[dict]]:
    proofs = defaultdict(list)
    for url in sorted(set(urls)):
        m = re.fullmatch('https://github\\.com/([^/]+)/([^/]+)/issues/\\d+#issuecomment-(\\d+)', url)
        if not m:
            raise ValueError(f'unsupported capture receipt URL: {url}')
        result = subprocess.run(['gh', 'api', f'repos/{m[1]}/{m[2]}/issues/comments/{m[3]}'], check=True, capture_output=True, text=True, timeout=60)
        receipt = json.loads(result.stdout)
        if receipt['html_url'] != url:
            raise ValueError('capture receipt URL mismatch')
        created, updated = (datetime.fromisoformat(receipt[k].replace('Z', '+00:00')) for k in ('created_at', 'updated_at'))
        if created.tzinfo is None or updated.tzinfo is None or updated < created:
            raise ValueError('invalid capture receipt server timestamps')
        hashes = set(re.findall('(?<![0-9a-fA-F])[0-9a-fA-F]{64}(?![0-9a-fA-F])', receipt['body']))
        matched = input_hashes & {h.lower() for h in hashes}
        if not matched:
            raise ValueError(f'capture receipt SHA mismatch: {url}')
        for sha in sorted(matched):
            proofs[sha].append({'url': url, 'created_at': receipt['created_at'], 'updated_at': receipt['updated_at'], 'available_at': updated.astimezone(timezone.utc).isoformat(), 'body': receipt['body'], 'verified_via': 'gh api'})
    return dict(proofs)

def _timing(row, receipts=()):
    quoted = row['published_at_quoted']
    if 'POST-KICKOFF' in (quoted + ' ' + row['published_at_basis']).upper():
        return ('post_kickoff', '')
    m = re.search('archive capture (\\d{4}-\\d\\d-\\d\\d \\d\\d:\\d\\d) UTC', quoted)
    if row['live_or_archive'] == 'archive' and m:
        return ('archive_capture', datetime.strptime(m[1], '%Y-%m-%d %H:%M').replace(tzinfo=timezone.utc).isoformat())
    day_end = datetime.combine(datetime.fromisoformat(row['read_date']).date(), time(), timezone.utc) + timedelta(days=1)
    if row['live_or_archive'] == 'live' and receipts:
        earliest = min((datetime.fromisoformat(p['available_at']) for p in receipts))
        if earliest < day_end:
            return ('server_receipt', earliest.isoformat())
    return ('capture_day', day_end.isoformat())

def _identity(row):
    return (row['week'], row['source'], row['ranker'], row['position'], row['pick_type'], name_key(row['player'], ''))

def _preference(row):
    return (row['timing'] != 'post_kickoff', row['live_or_archive'] == 'live', row['read_date'], row['input_sha256'], int(row['input_row']))

def _available(row, as_of):
    return row['timing'] != 'post_kickoff' and row['available_at'] and (datetime.fromisoformat(row['available_at']) <= as_of)

def ingest(paths: list[Path], out: Path=DATA, receipt_urls: list[str]=()) -> dict:
    raw_inputs = [(path, path.read_bytes()) for path in paths]
    receipts = verify_receipts(receipt_urls, {hashlib.sha256(raw).hexdigest() for _, raw in raw_inputs})
    groups, inputs, seen = (defaultdict(list), [], set())
    for path, raw in raw_inputs:
        sha = hashlib.sha256(raw).hexdigest()
        if sha in seen:
            continue
        seen.add(sha)
        with io.StringIO(raw.decode('utf-8-sig'), newline='') as f:
            reader = csv.DictReader(f)
            if reader.fieldnames != FIELDS:
                raise ValueError(f'{path}: expected the original 16-column schema')
            count = 0
            for line, original in enumerate(reader, 2):
                if None in original or any((v is None for v in original.values())):
                    raise ValueError(f'{path}:{line}: malformed row')
                r = {k: v.strip() for k, v in original.items()}
                if not 1 <= int(r['week']) <= 18 or not r['player']:
                    raise ValueError(f'{path}:{line}: invalid week/player')
                for k in ('pos_rank', 'overall_rank'):
                    if r[k] and (not r[k].isdigit() or int(r[k]) < 1):
                        raise ValueError(f'{path}:{line}: invalid {k}')
                r['raw_ranker'] = r['ranker']
                if (r['source'], r['ranker']) == ('Yahoo', 'Hayden Winks'):
                    r['source'] = 'Yahoo Sports'
                if (r['source'], r['ranker']) == ('FantasyPros', 'consensus'):
                    r['ranker'] = 'ECR consensus'
                r['position'] = {'DST': 'DEF', 'D/ST': 'DEF'}.get(r['position'], r['position'])
                if r['pick_type'] in ('', 'ranking') and r['pos_rank']:
                    r['pick_type'] = 'rank'
                r['timing'], r['available_at'] = _timing(r, receipts.get(sha, ()))
                r.update(input_sha256=sha, input_row=str(line))
                groups[_identity(r)].append(r)
                count += 1
        inputs.append({'sha256': sha, 'rows': count, **({'capture_receipts': receipts[sha]} if sha in receipts else {})})
    preference = _preference
    selected = [max(rs, key=preference) for rs in groups.values()]
    selected.sort(key=lambda r: (int(r['week']), r['source'], r['ranker'], r['position'], int(r['pos_rank'] or 99999), r['player'], r['pick_type']))
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, FIELDS + EXTRA, lineterminator='\n')
        writer.writeheader()
        writer.writerows(selected)
    history = [r for _, rs in sorted(groups.items()) for r in sorted(rs, key=preference)[:-1]]
    manifest = {'rule': RULE, 'history_schema': HISTORY_SCHEMA, 'history': history, 'inputs': sorted({x['sha256']: x for x in inputs}.values(), key=lambda x: x['sha256']), 'rows': len(selected), 'sha256': hashlib.sha256(out.read_bytes()).hexdigest(), 'superseded': [{'week': rs[0]['week'], 'source': rs[0]['source'], 'ranker': rs[0]['ranker'], 'player': rs[0]['player'], 'kept': {k: max(rs, key=preference)[k] for k in ('input_sha256', 'input_row')}, 'replaced': [{k: r[k] for k in ('input_sha256', 'input_row')} for r in sorted(rs, key=preference)[:-1]]} for _, rs in sorted(groups.items()) if len(rs) > 1]}
    out.with_suffix('.manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return {k: v for k, v in manifest.items() if k not in ('superseded', 'history')} | {'history_rows': len(history)}

@lru_cache(maxsize=4)
def _read(path: str, mtime: int) -> tuple[dict, ...]:
    with Path(path).open(newline='', encoding='utf-8') as f:
        return tuple(csv.DictReader(f))

def rows(path: Path | None=None) -> tuple[dict, ...]:
    path = path or DATA
    return _read(str(path), path.stat().st_mtime_ns) if path.exists() else ()

@lru_cache(maxsize=4)
def _dataset_sha256(path: str, mtime: int, size: int) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

@lru_cache(maxsize=4)
def _history(path: str, mtime: int, size: int):
    raw = Path(path).read_bytes()
    return (json.loads(raw), hashlib.sha256(raw).hexdigest())

def _as_of_rows(path: Path, as_of: datetime, digest: str | None, week: int):
    current = tuple((r for r in rows(path) if int(r['week']) == week))
    manifest_path = path.with_suffix('.manifest.json')
    report = {'status': 'unavailable', 'manifest_sha256': None, 'retained_versions': 0, 'as_of_replacements': 0}
    if not manifest_path.exists():
        return (current, report)
    stat = manifest_path.stat()
    manifest, sha = _history(str(manifest_path), stat.st_mtime_ns, stat.st_size)
    report['manifest_sha256'] = sha
    if not manifest.get('history_schema'):
        return (current, report)
    if manifest['history_schema'] != HISTORY_SCHEMA:
        raise ValueError('unsupported expert rankings history schema')
    if manifest.get('sha256') != digest:
        raise ValueError('expert rankings manifest CSV hash mismatch')
    history = [r for r in manifest['history'] if int(r['week']) == week]
    report.update(status='retained', retained_versions=len(current) + len(history))
    groups = defaultdict(list)
    for r in (*current, *history):
        groups[_identity(r)].append(r)
    selected = []
    for r in current:
        visible = [v for v in groups[_identity(r)] if _available(v, as_of)]
        chosen = max(visible, key=_preference) if visible else r
        selected.append(chosen)
        report['as_of_replacements'] += chosen != r
    return (selected, report)

def _name_index(world, week):
    names, exact = (defaultdict(list), defaultdict(list))
    for pid, v in world.players.items():
        key = name_key(v['name'], '').split('|')[0]
        names[key, v['pos']].append(pid)
        team = ((world.team_by_week or {}).get(week) or {}).get(pid, v['team'])
        exact[name_key(v['name'], team), v['pos']].append(pid)
    return (names, exact)

def match_player(row: dict, world, week: int, index=None) -> tuple[str | None, str]:
    position = row['position']
    team = row['team']
    if position == 'DEF':
        pid = 'DEF-' + name_key('', team).split('|')[1]
        return (pid, 'team') if pid in world.players else (None, 'unmatched')
    aliases = WEEK8_NAME_ALIASES if week >= 8 else None
    name = name_key(row['player'], '', aliases=aliases).split('|')[0]
    names, exact_names = index if index is not None else _name_index(world, week)
    same = names.get((name, position), [])
    exact = exact_names.get((name_key(row['player'], team, aliases=aliases), position), [])
    if len(exact) == 1:
        return (exact[0], 'name_team')
    if len(same) == 1:
        return (same[0], 'unique_name_position')
    return (None, 'ambiguous' if same else 'unmatched')

def board(week: int, world, as_of: datetime, path: Path | None=None) -> tuple[dict, dict]:
    path = path or DATA
    stat = path.stat() if path.exists() else None
    digest = _dataset_sha256(str(path), stat.st_mtime_ns, stat.st_size) if stat else None
    selected, unmatched, fallback_matches = ({}, [], [])
    versions, history_report = _as_of_rows(path, as_of, digest, week)
    ranked = [r for r in versions if int(r['week']) == week and r['pick_type'] == 'rank' and r['pos_rank'] and ((r['source'], r['ranker']) in SOURCES)]
    eligible = [r for r in ranked if _available(r, as_of)]
    matched = 0
    index = _name_index(world, week)
    for r in eligible:
        pid, method = match_player(r, world, week, index)
        if pid is None:
            unmatched.append({k: r[k] for k in ('player', 'team', 'position', 'source', 'ranker')} | {'reason': method})
            continue
        matched += 1
        if method == 'unique_name_position':
            fallback_matches.append({'player': r['player'], 'printed_team': r['team'], 'pid': pid})
        choice = (SOURCES[r['source'], r['ranker']], int(r['pos_rank']), r['source'], r['ranker'])
        if pid not in selected or choice < selected[pid]['choice']:
            selected[pid] = {'choice': choice, 'rank': int(r['pos_rank']), 'source': r['source'], 'ranker': r['ranker'], 'scoring': r['scoring'], 'input_sha256': r['input_sha256'], 'input_row': r['input_row']}
    return (selected, {'week': week, 'rule': RULE, 'dataset_sha256': digest, 'revision_history': history_report, 'rank_rows': len(ranked), 'eligible_rows': len(eligible), 'excluded_timing_rows': len(ranked) - len(eligible), 'matched_rows': matched, 'match_rate': round(matched / len(eligible), 6) if eligible else None, 'unmatched': unmatched, 'unique_name_position_matches': fallback_matches, 'matched_players': len(selected), 'covered_positions': sorted({world.pos(p) for p in selected})})

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('csvs', type=Path, nargs='+')
    parser.add_argument('--out', type=Path, default=DATA)
    parser.add_argument('--receipt', action='append', default=[], help='GitHub SHA-bearing receipt URL; verified with gh api at ingest')
    args = parser.parse_args()
    print(json.dumps(ingest(args.csvs, args.out, args.receipt), indent=2))
    return 0
if __name__ == '__main__':
    raise SystemExit(main())
