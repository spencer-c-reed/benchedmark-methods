from __future__ import annotations
import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from bm.engine.state import TIME_RULE
MAX_LOGGED_LAG_S = 120
GENESIS_PREV = '0' * 64
MODEL_KINDS = ('draft_pick', 'start_sit', 'waiver_bid', 'trade_offer', 'trade_response', 'forecast_book')
REPORT_LIMIT = 200

def _t(s: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(s) if s else None
    except (TypeError, ValueError):
        return None

def logged_lag(events: list[dict], max_lag_s: float=MAX_LOGGED_LAG_S) -> list[dict]:
    bad = []
    for e in events:
        if e.get('time_rule') != TIME_RULE or not e.get('logged_at') or (not e.get('ts')):
            continue
        lag = (datetime.fromisoformat(e['logged_at']) - datetime.fromisoformat(e['ts'])).total_seconds()
        if lag > max_lag_s:
            bad.append({'seq': e.get('seq'), 'type': e['type'], 'ts': e['ts'], 'logged_at': e['logged_at'], 'lag_s': lag})
    return bad

def _decisions(events: list[dict]) -> list[dict]:
    return [e for e in events if e.get('type') == 'decision' and e.get('kind') in MODEL_KINDS]

def _in_ledger_regime(dec: dict) -> bool:
    from bm.league.season import RETRY_FROM_WEEK
    return (dec.get('week') or 0) >= RETRY_FROM_WEEK and dec.get('visibility') != 'reconstructed'

def expected_row(events: list[dict]) -> list[dict]:
    have = {e.get('decision_id') for e in events if e.get('type') == 'decision_expected'}
    return [{'seq': d.get('seq'), 'decision_id': d['decision_id'], 'kind': d.get('kind'), 'week': d.get('week')} for d in _decisions(events) if _in_ledger_regime(d) and d['decision_id'] not in have]

def locks(events: list[dict]) -> dict[str, str]:
    out: dict[str, str] = {}
    for e in events:
        if e.get('type') == 'decision_expected' and e.get('hold_until'):
            out[e['decision_id']] = e['hold_until']
    for e in events:
        if e.get('type') == 'decision_pending' and e.get('hold_until'):
            out.setdefault(e['decision_id'], e['hold_until'])
    return out

def after_lock(events: list[dict]) -> list[dict]:
    lk = locks(events)
    missed = {e.get('decision_id') for e in events if e.get('type') == 'decision_missed'}
    bad = []
    for d in _decisions(events):
        did = d['decision_id']
        lock, at = (_t(lk.get(did)), _t(d.get('logged_at') or d.get('ts')))
        if lock is None or at is None or at <= lock or (did in missed):
            continue
        bad.append({'seq': d.get('seq'), 'decision_id': did, 'kind': d.get('kind'), 'lock': lk[did], 'logged_at': d.get('logged_at') or d.get('ts'), 'late_s': (at - lock).total_seconds(), 'forced_default': (d.get('forced_default') or {}).get('cause')})
    return bad

def _model_seats(cfg: dict) -> set[str]:
    from bm.agent import bots
    return {s['team_id'] for s in cfg.get('seats') or [] if s.get('entrant_id') not in bots.ALL}

def engine_choice(events: list[dict], cfg: dict | None) -> list[dict]:
    if not cfg or not cfg.get('standing_lineup'):
        return []
    seats = _model_seats(cfg)
    forced = {d['decision_id']: d for d in _decisions(events) if d.get('forced_default')}
    forced_bids = {(d.get('team'), d.get('week')) for d in forced.values() if d.get('kind') == 'waiver_bid'}
    bad = []
    for e in events:
        typ, team = (e.get('type'), e.get('team') or e.get('from_team'))
        if team not in seats:
            continue
        if typ == 'lineup_set' and e.get('source') != 'model':
            bad.append({'seq': e.get('seq'), 'type': typ, 'team': team, 'source': e.get('source'), 'decision_id': e.get('decision_id')})
        elif typ == 'waiver_award' and 'source' not in e and ((team, e.get('week')) in forced_bids):
            bad.append({'seq': e.get('seq'), 'type': typ, 'team': team, 'week': e.get('week'), 'add': e.get('add'), 'why': 'award from a forced-default bid list'})
        elif typ == 'trade_msg' and (e.get('source') == 'default' or e.get('decision_id') in forced):
            bad.append({'seq': e.get('seq'), 'type': typ, 'team': team, 'action': e.get('action'), 'source': e.get('source'), 'decision_id': e.get('decision_id')})
    return bad

def step_once(events: list[dict]) -> list[dict]:
    seqs = defaultdict(list)
    for e in events:
        if e.get('type') == 'step_done':
            seqs[e.get('step')].append(e.get('seq'))
    return [{'step': k, 'count': len(v), 'seqs': v} for k, v in seqs.items() if len(v) != 1]

def week_scored_once(events: list[dict]) -> list[dict]:
    by = defaultdict(list)
    for e in events:
        if e.get('type') == 'week_scored':
            by[e.get('week')].append(e)
    bad = []
    for week, evs in by.items():
        revs = [e.get('revision') for e in evs]
        dup = [r for r, n in Counter(revs).items() if n > 1]
        if dup:
            bad.append({'week': week, 'count': len(evs), 'seqs': [e.get('seq') for e in evs], 'duplicate_revisions': dup})
    return bad

def chain(path: Path) -> list[dict]:
    from bm.log import canonical
    prev, bad = (GENESIS_PREV, [])
    if not path.exists():
        return [{'error': 'log missing', 'path': str(path)}]
    with open(path, encoding='utf-8') as f:
        for n, raw in enumerate(f, 1):
            if not raw.strip():
                continue
            try:
                r = json.loads(raw)
            except ValueError:
                bad.append({'line': n, 'error': 'unreadable line' + ('' if raw.endswith('\n') else ' (torn tail)')})
                break
            body = canonical(r['payload'])
            if hashlib.sha256(body).hexdigest() != r.get('payload_hash'):
                bad.append({'seq': r.get('seq'), 'error': 'payload_hash mismatch'})
            if r.get('prev_hash') != prev or hashlib.sha256(prev.encode() + body).hexdigest() != r.get('hash'):
                bad.append({'seq': r.get('seq'), 'error': 'chain mismatch'})
            prev = r.get('hash')
            if len(bad) >= REPORT_LIMIT:
                break
    return bad

def read_events(path: Path) -> list[dict]:
    out = []
    if not path.exists():
        return out
    with open(path, encoding='utf-8') as f:
        for raw in f:
            if not raw.strip():
                continue
            try:
                r = json.loads(raw)
            except ValueError:
                if raw.endswith('\n'):
                    raise
                break
            out.append(dict(r['payload'], seq=r['seq']))
    return out
CHECKS = ('expected_row', 'after_lock', 'logged_lag', 'engine_choice', 'step_once', 'week_scored_once')

def check(events: list[dict], cfg: dict | None=None) -> dict:
    res = {'expected_row': expected_row(events), 'after_lock': after_lock(events), 'logged_lag': logged_lag(events), 'engine_choice': engine_choice(events, cfg), 'step_once': step_once(events), 'week_scored_once': week_scored_once(events)}
    return {'ok': not any(res.values()), **res}

def check_league(league_id: str, leagues: Path | None=None) -> dict:
    if leagues is None:
        from bm.league.season import LEAGUES as leagues
    d = Path(leagues) / league_id
    cfg = json.loads((d / 'league.json').read_text()) if (d / 'league.json').exists() else None
    res = check(read_events(d / 'log.jsonl'), cfg)
    res['chain'] = chain(d / 'log.jsonl')
    res['ok'] = res['ok'] and (not res['chain'])
    return res

def report(league_ids: list[str], leagues: Path) -> dict:
    out = {'schema': 'league-invariants/1', 'leagues_dir': str(leagues), 'checks': list(CHECKS) + ['chain'], 'max_logged_lag_s': MAX_LOGGED_LAG_S, 'leagues': {}}
    for lid in league_ids:
        r = check_league(lid, leagues)
        out['leagues'][lid] = {'ok': r['ok'], 'counts': {k: len(r[k]) for k in out['checks']}, 'by_kind': {k: dict(Counter((v.get('kind') or v.get('type') for v in r[k]))) for k in ('expected_row', 'after_lock', 'engine_choice') if r[k]}, 'violations': {k: r[k][:REPORT_LIMIT] for k in out['checks'] if r[k]}}
    out['ok'] = all((r['ok'] for r in out['leagues'].values()))
    return out

def main(argv: list[str] | None=None) -> int:
    from bm.league.season import LEAGUES
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('leagues_ids', nargs='*')
    ap.add_argument('--leagues', type=Path, default=LEAGUES, help='directory of league dirs (default: the live leagues/)')
    ap.add_argument('--out', type=Path, help='write the JSON report here (never inside the leagues directory)')
    a = ap.parse_args(argv if argv is not None else sys.argv[1:])
    root = a.leagues.resolve()
    ids = a.leagues_ids or sorted((p.name for p in root.iterdir() if (p / 'log.jsonl').exists()))
    rep = report(ids, root)
    text = json.dumps(rep, indent=1, default=str)
    if a.out:
        out = a.out.resolve()
        if out.is_relative_to(root) or out.is_relative_to(LEAGUES.resolve()):
            ap.error('the report must be written outside the leagues directory')
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + '\n')
        print(json.dumps({lid: r['counts'] for lid, r in rep['leagues'].items()}))
    else:
        print(text)
    return 0 if rep['ok'] else 1
if __name__ == '__main__':
    sys.exit(main())
