"""Read-only settled Picks calibration export; no collection, inference or settlement writes.

The headline pools early/prelock observations, as in the October 12 data review.
Games, rather than instances, books or repeated slate timings, are bootstrap units.
Legacy receipts remain descriptive; this export cannot certify prospective skill.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, pstdev

from bm.log import canonical
from bm.paths import runtime_root
from bm.score.forecast import calibration_bins, copying_index, log_score, skill_with_ci
from bm.score.picks import brier_vs_close, pooled_ensemble


def _prob(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1


def _mean(values):
    return mean(values) if values else None


def _metrics(rows, bootstrap_n):
    model_brier, market_brier, model_log, market_log = [], [], [], []
    for row in rows:
        scores = brier_vs_close(row['p_home'], row['p_close'], row['home_win'])
        model_brier.append(scores['model_brier'])
        market_brier.append(scores['close_brier'])
        model_log.append(log_score(row['p_home'], row['home_win']))
        market_log.append(log_score(row['p_close'], row['home_win']))
    clusters = [r['game_id'] for r in rows]
    result = dict(n=len(rows), n_games=len(set(clusters)), brier=_mean(model_brier),
                  log_score=_mean(model_log), market_brier=_mean(market_brier),
                  market_log_score=_mean(market_log),
                  calibration=calibration_bins([r['p_home'] for r in rows], [r['home_win'] for r in rows]))
    if bootstrap_n is not None:
        result.update(brier_skill=skill_with_ci(model_brier, market_brier, clusters=clusters, n=bootstrap_n),
                      log_skill=skill_with_ci(model_log, market_log, clusters=clusters, n=bootstrap_n))
    return result


def _step(rows, bootstrap_n):
    """Only complete three-instance groups contribute to pooling value."""
    groups = defaultdict(list)
    instances = defaultdict(list)
    for row in rows:
        groups[(row['slate'], row['game_id'])].append(row)
        instances[row['instance']].append(row)
    pooled, constituent, spreads = [], [], []
    incomplete = 0
    for group in groups.values():
        if {r['instance'] for r in group} != {1, 2, 3} or len(group) != 3:
            incomplete += 1
            continue
        probabilities = [r['p_home'] for r in group]
        pooled.append(dict(group[0], p_home=pooled_ensemble([[p] for p in probabilities])))
        constituent.extend(group)
        spreads.append(dict(slate=group[0]['slate'], game_id=group[0]['game_id'],
                            sd=pstdev(probabilities), range=max(probabilities)-min(probabilities)))
    result = _metrics(rows, bootstrap_n)
    ensemble = _metrics(pooled, bootstrap_n)
    paired = _metrics(constituent, None)
    ensemble.update(complete_three_instance_groups=len(pooled), incomplete_groups=incomplete,
                    constituent_brier=paired['brier'], constituent_log_score=paired['log_score'],
                    brier_improvement=None if not pooled else paired['brier']-ensemble['brier'],
                    log_improvement=None if not pooled else paired['log_score']-ensemble['log_score'])
    result.update(instances={str(i): _metrics(rs, None) for i, rs in sorted(instances.items())},
                  instance_spread=dict(n_groups=len(spreads), mean_sd=_mean([s['sd'] for s in spreads]),
                                       mean_range=_mean([s['range'] for s in spreads]), by_game=spreads),
                  pooled_ensemble=ensemble)
    return result


def _copying(blind, line):
    by_key = {(r['slate'], r['instance'], r['game_id']): r for r in blind}
    pairs = [(by_key[key], r) for r in line
             if (key := (r['slate'], r['instance'], r['game_id'])) in by_key]
    # Movement is toward the market shown in STEP 2, not a later unseen close.
    pairs = [(b, l) for b, l in pairs if _prob(l.get('p_shown'))]
    before = [abs(b['p_home']-l['p_shown']) for b, l in pairs]
    after = [abs(l['p_home']-l['p_shown']) for b, l in pairs]
    # copying_index's threshold is 0.5 POINTS: use percentage points for probabilities.
    similarity = copying_index([100*l['p_home'] for _, l in pairs],
                               [100*l['p_shown'] for _, l in pairs])
    nonzero = [(b, l) for b, l in pairs if abs(l['p_shown']-b['p_home']) > 1e-12]
    return dict(n_pairs=len(pairs), target='shown no-vig market at line step',
                mean_distance_blind=_mean(before), mean_distance_line=_mean(after),
                mean_distance_reduction=_mean([b-a for b, a in zip(before, after)]),
                fraction_distance_closed=None if not before or sum(before) == 0 else 1-sum(after)/sum(before),
                share_moving_closer=_mean([float(a < b) for b, a in zip(before, after)]),
                mean_signed_movement_fraction=_mean([(l['p_home']-b['p_home'])/(l['p_shown']-b['p_home'])
                                                     for b, l in nonzero]),
                blind_at_market_pairs=len(pairs)-len(nonzero),
                line_similarity_percentage_points=similarity)


def summarize(attempts, games, closes, *, week, season=2026, bootstrap_n=2000):
    """Pure calculation seam for retained records and the ordinary settlement inputs."""
    by_game = {g['game_id']: g for g in games}
    game_ids = set(by_game)
    selected, seen, failures, excluded = [], set(), [], Counter()
    attempt_counts = Counter()
    slate_games = set()
    models = set()
    for row in attempts:
        if row.get('kind') != 'pick' or row.get('week') != week:
            continue
        models.add(row['entrant'])
        if row.get('served_by_other_model'):  # bm.ops.served_model annotation: another model answered; no one's forecast
            excluded['served_by_other_model'] += 1
            continue
        attempt_counts[row.get('outcome', 'unknown')] += 1
        slate = row.get('slate') or row['key'].split(':')[0]
        expected = set(row.get('expected_games') or []) | set(row.get('answer') or {}) | set(row.get('lines') or {})
        game_ids.update(expected)
        slate_games.update((slate, gid) for gid in expected)
        if row.get('outcome') != 'own' or row.get('failure') or not isinstance(row.get('answer'), dict):
            failure = {k: (row.get('failure') or {}).get(k) for k in ('layer', 'code')}
            failures.append(dict({k: row.get(k) for k in ('record_id', 'key', 'entrant', 'instance', 'step', 'outcome')},
                                 failure=failure))
            continue
        seat = (slate, row['entrant'], row['instance'], row['step'])
        if seat in seen:
            excluded['duplicate_own_seat_step'] += 1
            continue
        seen.add(seat)  # first own attempt; no best-of-retries selection
        for gid, answer in row['answer'].items():
            g = by_game.get(gid, {})
            if g.get('home_score') is None or g.get('away_score') is None:
                excluded['unsettled_forecast'] += 1
                continue
            if g['home_score'] == g['away_score']:
                excluded['tie_forecast'] += 1
                continue  # two-way h2h is a push; never label a tie a model loss
            p_close = (closes.get(gid) or {}).get('consensus', {}).get('p_home')
            if not _prob(p_close):
                excluded['missing_close_forecast'] += 1
                continue
            if not isinstance(answer, dict) or not _prob(answer.get('p_home')):
                excluded['invalid_probability'] += 1
                continue
            receipt = 'legacy/unknown prospective receipt'
            if row.get('received_at'):
                try:
                    received = datetime.fromisoformat(row['received_at'])
                    cutoff = datetime.fromisoformat(g['kickoff'])
                    deadline = datetime.fromisoformat(row['deadline']) if row.get('deadline') else cutoff
                    if received >= min(cutoff, deadline):
                        excluded['late_forecast'] += 1
                        continue
                    receipt = 'received before game and recorded deadline' if row.get('deadline') else 'deadline unknown'
                except (ValueError, TypeError, KeyError):
                    excluded['invalid_receipt'] += 1
                    continue
            shown = (row.get('lines') or {}).get(gid, {}).get('consensus', {}).get('p_home')
            selected.append(dict(entrant=row['entrant'], instance=row['instance'], step=row['step'], slate=slate,
                                 game_id=gid, record_id=row.get('record_id'), p_home=answer['p_home'], p_close=p_close,
                                 p_shown=shown, home_win=g['home_score'] > g['away_score'], receipt_status=receipt))
    settled = [gid for gid in sorted(game_ids) if by_game.get(gid, {}).get('home_score') is not None
               and by_game[gid].get('away_score') is not None]
    ties = [gid for gid in settled if by_game[gid]['home_score'] == by_game[gid]['away_score']]
    scorable = [gid for gid in settled if gid not in ties and _prob((closes.get(gid) or {}).get('consensus', {}).get('p_home'))]
    def model_metrics(rows):
        blind = [r for r in rows if r['step'] == 'blind']
        line = [r for r in rows if r['step'] == 'line']
        return dict(blind=_step(blind, bootstrap_n), line=_step(line, bootstrap_n), copying_index=_copying(blind, line))
    by_model = {m: model_metrics([r for r in selected if r['entrant'] == m]) for m in sorted(models)}
    by_slate = {s: {m: model_metrics([r for r in selected if r['entrant'] == m and r['slate'] == s])
                    for m in sorted({r['entrant'] for r in attempts if (r.get('slate') or r.get('key', '').split(':')[0]) == s
                                     and r.get('kind') == 'pick'})} for s in sorted({s for s, _ in slate_games})}
    def market_metrics(ids):
        rows = [dict(game_id=gid, p_home=closes[gid]['consensus']['p_home'],
                     p_close=closes[gid]['consensus']['p_home'],
                     home_win=by_game[gid]['home_score'] > by_game[gid]['away_score']) for gid in ids]
        return dict(n=len(rows), n_games=len({r['game_id'] for r in rows}),
                    brier=_mean([brier_vs_close(r['p_home'], r['p_close'], r['home_win'])['close_brier'] for r in rows]),
                    log_score=_mean([log_score(r['p_home'], r['home_win']) for r in rows]))
    return dict(schema='picks-readout/1', season=season, week=week,
                measurement_status='descriptive retained-data readout; prospective certification not inferred',
                methods=dict(target='home win in non-tied games; two-way market excludes tie mass',
                             selection='first own answer per slate/model/instance/step; defaults excluded',
                             aggregation='all early/prelock forecast observations; slate timing retained separately',
                             market_weighting='one observation per slate/game; model skill uses its own matched baseline',
                             bootstrap=dict(unit='game_id', confidence=0.90, resamples=bootstrap_n, seed=0),
                             close_quality='last observed pre-kickoff quote; actual final close not certified',
                             score_quality='observed complete scores; schedule schema does not certify finality'),
                frame=dict(games=len(game_ids), settled_games=len(settled), settled_game_ids=settled,
                           unsettled_games=len(game_ids)-len(settled), unsettled_game_ids=sorted(game_ids-set(settled)),
                           tied_games=len(ties), tied_game_ids=ties, scored_games=len(scorable),
                           missing_close_games=sorted(set(settled)-set(ties)-set(scorable)),
                           attempt_outcomes=dict(attempt_counts), excluded_forecasts=dict(excluded),
                           selected_forecast_rows=len(selected), selected_seat_steps=len(seen),
                           receipt_status_counts=dict(Counter(r['receipt_status'] for r in selected))),
                market_close=market_metrics([gid for _, gid in sorted(slate_games) if gid in scorable]),
                market_close_unique_games=market_metrics(scorable), by_model=by_model, by_slate=by_slate,
                excluded_seat_steps=failures, observations=selected)


def retained_inputs(week, root=None, *, season=2026):
    """Retained schedule (with read-only settlement scores applied) and chained Picks records for one week.

    Shared by this readout and the expert-picks human baseline so both grade against the same results."""
    from bm.stats.nflverse import kickoff_iso
    root = Path(root).resolve() if root else runtime_root(Path(__file__).resolve().parents[2])
    def contained(path):
        result = (root / path).resolve()
        if not result.is_relative_to(root):
            raise ValueError('source path escapes runtime root')
        return result
    def manifest(path):
        raw = contained(path).read_bytes()
        return [json.loads(line) for line in raw.splitlines() if line.strip()], hashlib.sha256(raw).hexdigest()
    cache = {}
    def source(row):
        key = (row['path'], row['sha256'])
        if key not in cache:
            raw = gzip.decompress(contained(row['path']).read_bytes())
            if hashlib.sha256(raw).hexdigest() != row['sha256']:
                raise ValueError('source snapshot hash mismatch: '+row['path'])
            cache[key] = raw
        return cache[key]
    nfl, nfl_hash = manifest('data/raw/nfl/manifest.jsonl')
    schedule_source = next((m for m in reversed(nfl) if m.get('asset') == 'games'), None)
    if not schedule_source:
        raise ValueError('no retained games source')
    games = []
    for row in csv.DictReader(io.StringIO(source(schedule_source).decode())):
        if row['season'] != str(season) or row['game_type'] != 'REG' or int(row['week']) != week:
            continue
        score = lambda value: None if value in ('', 'NA') else int(value)
        games.append(dict(game_id=row['game_id'], home=row['home_team'], away=row['away_team'],
                          kickoff=kickoff_iso(row['gameday'], row['gametime']) if row['gametime'] else None,
                          home_score=score(row['home_score']), away_score=score(row['away_score'])))
    attempts, record_sources, settlements, misserved, contexts = [], {}, {}, set(), {}
    for path in sorted((root/'records/picks').glob(f'{season}-w{week}-*.jsonl')):
        if path.stem.endswith('-scripted'):
            continue
        raw, prev, count = path.read_bytes(), '0'*64, 0
        for count, line in enumerate((l for l in raw.splitlines() if l.strip()), 1):
            row = json.loads(line)
            body = canonical(row['payload'])
            if (row['seq'] != count or row['prev_hash'] != prev or
                    row['payload_hash'] != hashlib.sha256(body).hexdigest() or
                    row['hash'] != hashlib.sha256(prev.encode()+body).hexdigest()):
                raise ValueError('records source chain invalid: '+path.name)
            prev = row['hash']
            payload = row['payload']
            if payload.get('kind') in ('pick_settled', 'pick_settled_revision'):
                settlements[payload['key']] = payload
            elif payload.get('kind') == 'pick':
                attempts.append(payload)
            elif payload.get('kind') == 'served_model_annotation':
                misserved.add(payload.get('target_record_id'))
            elif payload.get('kind') == 'slate_context':
                contexts[payload.get('key') or path.stem] = payload
        record_sources[path.name] = dict(sha256=hashlib.sha256(raw).hexdigest(), rows=count, head=prev)
    # Settlement rows bind exact observed scores where available (latest revision per key).
    settled_scores = {}
    for row in settlements.values():
        scores = (row.get('score_provenance') or {}).get('observed_scores')
        if scores and all(type(scores.get(k)) in (int, float) and math.isfinite(scores[k]) for k in ('home_score', 'away_score')):
            gid = row['game_id']
            if gid in settled_scores and settled_scores[gid] != scores:
                raise ValueError('conflicting retained settlement scores: '+gid)
            settled_scores[gid] = scores
    for game in games:
        if game['game_id'] in settled_scores:
            game.update(settled_scores[game['game_id']])
    return dict(root=root, games=games, attempts=attempts, settlements=settlements, misserved=misserved,
                contexts=contexts, settled_scores=settled_scores, record_sources=record_sources, schedule_source=schedule_source,
                nfl_hash=nfl_hash, source=source, manifest=manifest)


def readout(week, root=None, *, season=2026, bootstrap_n=2000):
    """Read exact snapshots used by settle, without calling its append-only writer."""
    from bm.picks import lines
    inputs = retained_inputs(week, root, season=season)
    root, games, attempts, settlements, misserved, settled_scores, record_sources, schedule_source, nfl_hash, source, manifest = (
        inputs[k] for k in ('root', 'games', 'attempts', 'settlements', 'misserved', 'settled_scores', 'record_sources',
                            'schedule_source', 'nfl_hash', 'source', 'manifest'))
    market, market_hash = manifest('data/raw/market/manifest.jsonl')
    odds = sorted((m for m in market if m.get('source') == 'odds'), key=lambda m: m['retrieved_at'])
    closes, parsed, close_sources = {}, {}, {}
    for game in games:
        if game['home_score'] is None or game['away_score'] is None or not game['kickoff']:
            continue
        kickoff = datetime.fromisoformat(game['kickoff'])
        for m in reversed(odds):
            if datetime.fromisoformat(m['retrieved_at']) > kickoff:
                continue
            key = (m['path'], game['kickoff'])
            if key not in parsed:
                parsed[key] = lines.parse(dict(json.loads(source(m)), _meta=m), games, as_of=kickoff)
            close = parsed[key].get(game['game_id'])
            if close:
                closes[game['game_id']] = close
                close_sources[game['game_id']] = dict(snapshot=m, p_home=close['consensus']['p_home'],
                    home_score=game['home_score'], away_score=game['away_score'],
                    n_books=close['n_books'], source='retained settlement' if game['game_id'] in settled_scores else 'retained schedule input to settle')
                break
    attempts = [dict(a, served_by_other_model=True) if a.get('record_id') in misserved else a for a in attempts]
    result = summarize(attempts, games, closes, week=week, season=season, bootstrap_n=bootstrap_n)
    result.update(generated_at=datetime.now(timezone.utc).isoformat(),
                  sources=dict(runtime_root=str(root), schedule_snapshot=schedule_source,
                               nfl_manifest_sha256=nfl_hash, market_manifest_sha256=market_hash,
                               records=record_sources, close_by_game=close_sources,
                               retained_settlement_rows=len(settlements),
                               settlement_status='read-only retained settlement scores where present; otherwise complete scores from the ordinary settle schedule source'))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--week', type=int, required=True)
    parser.add_argument('--season', type=int, default=2026)
    parser.add_argument('--root', type=Path)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(argv)
    code_root = Path(__file__).resolve().parents[2]
    live = runtime_root(code_root).resolve()
    out = args.out.resolve()
    if live != code_root and out.is_relative_to(live):
        raise ValueError('output must be outside the shared live checkout')
    result = readout(args.week, args.root, season=args.season)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False)+'\n')
    print(json.dumps(dict(out=str(out), frame=result['frame'], market_close=result['market_close'],
                          blind_brier={m: r['blind']['brier'] for m, r in result['by_model'].items()}), indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
