from __future__ import annotations
import argparse
import fcntl
import json
import os
import tempfile
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from bm.log import ChainLog
ROOT = Path(__file__).resolve().parents[2]

def runner_busy(directory: Path, lock_name: str='run.lock') -> bool:
    try:
        f = (directory / lock_name).open('rb')
    except FileNotFoundError:
        return False
    with f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(f, fcntl.LOCK_UN)
        return False

def snapshot(config: dict, events: list[dict], *, busy: bool, log_head: dict | None=None) -> dict:
    picks = []
    activity = None
    completed_at = None
    complete = False
    decisions = {e.get('decision_id'): e for e in events if e.get('type') == 'decision'}
    for event in events:
        kind = event.get('type')
        if kind == 'draft_pick':
            actual = event.get('logged_at')
            source = event.get('source')
            if source == 'queue' and decisions.get(event.get('decision_id'), {}).get('forced_default'):
                source = 'default'
            picks.append({'pick_no': event.get('pick_no', len(picks) + 1), 'round': event.get('round'), 'team_id': event['team'], 'player_id': event['player'], 'source': source, 'decision_id': event.get('decision_id'), 'at': actual, 'time_basis': 'logged_at' if actual else 'unknown', 'event_ts': event.get('ts'), 'event_seq': event.get('seq'), 'data_as_of': event.get('data_as_of')})
            activity = None
        elif kind == 'draft_activity':
            activity = {k: event[k] for k in ('status', 'team', 'pick_no', 'retry_at', 'cause', 'lane', 'data_as_of') if k in event}
            activity['at'] = event.get('logged_at') or event.get('ts')
        elif kind == 'decision_skipped_infra' and event.get('kind') == 'draft_pick':
            activity = {'status': 'paused', 'team': event.get('team'), 'at': event.get('logged_at'), 'cause': event.get('cause'), 'lane': event.get('lane')}
        elif kind == 'draft_complete':
            complete = True
            completed_at = event.get('logged_at')
    order = list(config['draft_order'])
    total = len(order) * int(config['draft_rounds'])
    next_pick = len(picks)
    on_clock = None
    if not complete and next_pick < total and order:
        round_index, slot = divmod(next_pick, len(order))
        on_clock = order[slot if round_index % 2 == 0 else len(order) - 1 - slot]
    status = 'complete' if complete else 'not_started' if not picks and activity is None else 'waiting'
    if not complete and activity:
        status = activity['status']
    if not complete and (not busy) and (status in ('on_clock', 'paused')):
        status = 'interrupted'
    return {'schema_version': 'draft-1', 'league_id': config['id'], 'draft_as_of': config['draft_as_of'], 'draft_order': order, 'status': status, 'runner_busy': busy, 'picks_completed': len(picks), 'picks_total': total, 'on_clock_team': on_clock, 'activity': activity, 'completed_at': completed_at, 'picks': picks, 'log_head': log_head}

def publish(directory: Path, output_root: Path | None=None) -> Path:
    directory = Path(directory)
    config = json.loads((directory / 'league.json').read_text())
    lid = config['id']
    if not isinstance(lid, str) or not lid or Path(lid).name != lid or (lid in ('.', '..')):
        raise ValueError('invalid league ID')
    from bm.engine.rules import load_rules
    config = {**config, 'draft_rounds': load_rules(config['rules_file']).roster_size}
    lock = directory / 'log.jsonl.lock'
    with lock.open('rb') if lock.exists() else nullcontext() as handle:
        if handle is not None:
            fcntl.flock(handle, fcntl.LOCK_SH)
        rows = list(ChainLog(directory / 'log.jsonl').rows())
        events = [dict(row['payload'], seq=row['seq']) for row in rows]
        head = {'seq': rows[-1]['seq'], 'hash': rows[-1]['hash']} if rows else None
        lock_name = 'seat-draft.lock' if config.get('mode') == 'agent' else 'run.lock'
        data = snapshot(config, events, busy=runner_busy(directory, lock_name), log_head=head)
        data['generated_at'] = datetime.now(timezone.utc).isoformat()
        dest = Path(output_root) if output_root is not None else ROOT / 'export' / 'drafts'
        dest.mkdir(parents=True, exist_ok=True, mode=448)
        fd, name = tempfile.mkstemp(prefix=f'.{lid}.', suffix='.tmp', dir=dest)
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(data, f, separators=(',', ':'))
                f.write('\n')
                f.flush()
                os.fsync(f.fileno())
            path = dest / f'{lid}.json'
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
    return path

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('league')
    parser.add_argument('--output-root', type=Path)
    args = parser.parse_args()
    if Path(args.league).name != args.league or args.league in ('.', '..'):
        parser.error('league must be a league ID')
    print(publish(ROOT / 'leagues' / args.league, args.output_root))
if __name__ == '__main__':
    main()
