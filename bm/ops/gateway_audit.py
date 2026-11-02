from __future__ import annotations
import argparse
import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from bm.paths import gateway_logs, runtime_root
ROOT = Path(__file__).resolve().parents[2]
ET = ZoneInfo('America/New_York')
FIELDS = ('model_reported', 'identity_source', 'billed_usd', 'usd_eq', 'usd_equivalent', 'cost_or_usd_equivalent', 'deadline', 'deadline_margin_s', 'observed_identity')
OBSERVED_SOURCES = {'provider_response', 'cli_usage_report', 'cli_response_stream', 'cli_generation_metadata'}

def timestamp(value: str) -> datetime:
    at = datetime.fromisoformat(value)
    return at.replace(tzinfo=ET) if at.tzinfo is None else at

def number(value) -> bool:
    return isinstance(value, (int, float)) and (not isinstance(value, bool)) and math.isfinite(value)

def text(value) -> bool:
    return isinstance(value, str) and bool(value.strip())

def coverage(row: dict) -> dict[str, bool]:
    cost = row.get('cost') or {}
    identity = row.get('identity_source')
    deadline = row.get('decision_deadline') or row.get('deadline')
    try:
        has_deadline = bool(deadline) and bool(timestamp(deadline))
    except (TypeError, ValueError):
        has_deadline = False
    return {'model_reported': text(row.get('model_reported')), 'identity_source': text(identity) and identity.strip().lower() != 'unknown', 'billed_usd': number(cost.get('billed_usd')), 'usd_eq': number(cost.get('usd_eq')), 'usd_equivalent': number(row.get('usd_equivalent')), 'cost_or_usd_equivalent': any((number(v) for v in (cost.get('billed_usd'), cost.get('usd_eq'), row.get('usd_equivalent')))), 'deadline': has_deadline, 'deadline_margin_s': number(row.get('deadline_margin_s')), 'observed_identity': text(row.get('model_reported')) and identity in OBSERVED_SOURCES}

def read_rows(path: Path, sources: list[dict]):
    if not path.exists():
        return
    size = path.stat().st_size
    source = {'path': str(path.resolve()), 'bytes': size, 'rows': 0, 'incomplete_tail': False}
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        remaining = size
        while remaining:
            line = stream.readline(remaining)
            if not line:
                raise ValueError(f'source shortened during audit: {path}')
            remaining -= len(line)
            digest.update(line)
            if not line.endswith(b'\n'):
                source['incomplete_tail'] = True
                continue
            if not line.strip():
                continue
            envelope = json.loads(line)
            row = envelope.get('payload', envelope)
            if not isinstance(row, dict):
                raise ValueError(f'non-object row in {path}')
            source['rows'] += 1
            key = envelope.get('hash') or (row.get('request_id'), row.get('route', 0), row.get('ts'))
            yield (row, key)
    source['sha256'] = digest.hexdigest()
    sources.append(source)

def live_seats(root: Path) -> tuple[dict, dict]:
    leagues, picks = ({}, {})
    for path in sorted((root / 'leagues').glob('*/league.json')):
        league = json.loads(path.read_text())
        if league.get('status') not in {'private-live-season', 'live-season'}:
            continue
        leagues[league['id']] = {'created_at': league['created_at'], 'entrants': sorted({s['entrant_id'] for s in league['seats']})}
    for path in sorted((root / 'records' / 'picks').glob('*.jsonl')):
        if not re.fullmatch('\\d{4}-w\\d+-(early|prelock|london)', path.stem):
            continue
        seats = set()
        for row, _ in read_rows(path, []):
            if row.get('kind') == 'pick' and text(row.get('entrant')):
                seats.add((row['entrant'], row.get('instance'), row.get('step')))
            elif row.get('kind') == 'slate_context':
                seats.update(((model, instance, step) for model in row['models'] for instance in range(1, int(row['instances']) + 1) for step in ('blind', 'line')))
        if seats:
            picks[path.stem] = sorted(seats)
    return (leagues, picks)

def cohort(row: dict, at: datetime, leagues: dict, picks: dict) -> str:
    parts = str(row.get('decision_id') or '').split(':')
    league = leagues.get(parts[0])
    if league:
        if at < timestamp(league['created_at']):
            return 'excluded_voided_draft'
        return 'live_season' if row.get('entrant') in league['entrants'] else 'excluded_unseated'
    if len(parts) in (5, 6) and parts[0] == 'picks' and (parts[1] in picks):
        instance = parts[3]
        if re.fullmatch('i\\d+', instance) and (parts[2], int(instance[1:]), parts[4]) in picks[parts[1]]:
            if row.get('entrant') == parts[2] and (len(parts) == 5 or re.fullmatch('attempt\\d+', parts[5])):
                return 'live_picks'
        return 'excluded_unseated'
    return 'excluded_other'

def metrics(counts: Counter) -> dict:
    n = counts['rows']
    return {'rows': n, 'success_rows': counts['success_rows'], 'fields': {field: {'filled': counts[field], 'missing': n - counts[field], 'fill_rate': round(counts[field] / n, 6) if n else None} for field in FIELDS}, 'deadline_rows': counts['deadline'], 'margin_on_deadline_rate': round(counts['margin_with_deadline'] / counts['deadline'], 6) if counts['deadline'] else None}

def audit(since: str, *, code_root: Path=ROOT) -> dict:
    start = timestamp(since)
    root = runtime_root(code_root)
    leagues, picks = live_seats(root)
    groups = {name: defaultdict(Counter) for name in ('by_lane', 'by_lane_model_day', 'by_identity_day', 'by_cohort')}
    total, skipped = (Counter(), Counter())
    seen, sources = (set(), [])
    for path in gateway_logs(code_root):
        for row, key in read_rows(path, sources):
            if row.get('type') != 'model_call':
                continue
            if key in seen:
                skipped['duplicate_rows'] += 1
                continue
            seen.add(key)
            try:
                at = timestamp(row['ts'])
            except (KeyError, TypeError, ValueError):
                skipped['invalid_timestamp'] += 1
                continue
            if at < start:
                skipped['before_since'] += 1
                continue
            scope = cohort(row, at, leagues, picks)
            if scope.startswith('excluded'):
                skipped[scope] += 1
                continue
            filled = coverage(row)
            counts = Counter({'rows': 1, 'success_rows': int(not row.get('error')), **{k: int(v) for k, v in filled.items()}, 'margin_with_deadline': int(filled['deadline'] and filled['deadline_margin_s'])})
            total.update(counts)
            lane, model = (row.get('lane') or '<missing>', row.get('model_requested') or '<missing>')
            day = at.astimezone(ET).date().isoformat()
            groups['by_lane'][lane,].update(counts)
            groups['by_lane_model_day'][lane, model, day].update(counts)
            groups['by_identity_day'][lane, model, row.get('model_reported') or '<missing>', row.get('identity_source') or '<missing>', day].update(counts)
            groups['by_cohort'][scope,].update(counts)
    dimensions = {'by_lane': ('lane',), 'by_lane_model_day': ('lane', 'model_requested', 'day'), 'by_identity_day': ('lane', 'model_requested', 'model_reported', 'identity_source', 'day'), 'by_cohort': ('cohort',)}
    return {'schema': 1, 'measured_at': datetime.now(timezone.utc).isoformat(), 'since': start.isoformat(), 'timezone': str(ET), 'runtime_root': str(root), 'scope': 'current live season seats since league creation; untagged recorded Picks seats', 'unit': 'gateway route rows, including errors and all retained retries', 'notes': ['Historical rows are not rewritten or inferred from current adapters/prices.', 'unknown identity_source is missing; requested_cli_configuration is a declaration, not observed identity.', 'usd_eq is legacy terminal cost; usd_equivalent sums retries on one route for new rows.', 'Sum usd_equivalent across distinct route rows for a decision; unknown prices stay null.'], 'live_leagues': leagues, 'live_picks_slates': sorted(picks), 'sources': sources, 'excluded': dict(skipped), 'total': metrics(total), **{name: [dict(zip(dimensions[name], key), **metrics(counts)) for key, counts in sorted(group.items())] for name, group in groups.items()}}

def render(report: dict) -> str:
    columns = ('model_reported', 'identity_source', 'observed_identity', 'billed_usd', 'usd_eq', 'usd_equivalent', 'deadline', 'deadline_margin_s')
    lines = [f'Live gateway route rows since {report['since']} (days: {report['timezone']})', 'Fill: filled/rows (percent); errors included; zero is filled; unknown stays missing.', '| lane | rows | ' + ' | '.join(columns) + ' |', '|---|---:|' + '---:|' * len(columns)]
    for row in report['by_lane'] + [dict(report['total'], lane='TOTAL')]:
        cells = [row['lane'], str(row['rows'])]
        for field in columns:
            value = row['fields'][field]
            rate = f'{value['fill_rate']:.1%}' if value['fill_rate'] is not None else 'n/a'
            cells.append(f'{value['filled']}/{row['rows']} ({rate})')
        lines.append('| ' + ' | '.join(cells) + ' |')
    lines.append('Excluded: ' + json.dumps(report['excluded'], sort_keys=True))
    return '\n'.join(lines)

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--since', required=True, help='ISO date/time; naive inputs use America/New_York')
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args(argv)
    output = args.out.resolve()
    protected = runtime_root(ROOT)
    if any((output.is_relative_to(protected / name) for name in ('leagues', 'records', 'logs', 'state', 'export', 'blobs'))):
        parser.error('--out must be outside live data directories')
    report = audit(args.since)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, allow_nan=False) + '\n')
    print(render(report))
    return 0
if __name__ == '__main__':
    raise SystemExit(main())
