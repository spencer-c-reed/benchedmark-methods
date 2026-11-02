"""Read-only top-half optimism diagnostics beside forecast calibration.

Decision ``win_prob_this_week`` is top-half; book ``week.p_win`` is
head-to-head and is never substituted. Explicit book top-half observations
may use ``self.p_top_half`` and ``teams[team_id].p_top_half`` (or the existing
``win_prob_this_week`` name). Sources are reported separately, never pooled.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from statistics import mean

from bm.paths import runtime_root
from bm.score import forecast, proof


def _prob(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1


def _top_half(obj):
    if not isinstance(obj, dict):
        return None
    return obj.get('p_top_half', obj.get('win_prob_this_week'))


def _metrics(rows, entrants):
    out = {}
    for entrant in sorted(entrants):
        rs = [r for r in rows if r['entrant'] == entrant]
        own = [r for r in rs if r['relation'] == 'self']
        valid = [r for r in own if r['base_rate'] is not None]
        rivals = [r for r in rs if r['relation'] == 'rival' and r['base_rate'] is not None]
        paired = []
        for r in valid:
            same = [o['p'] for o in rivals if o['league'] == r['league'] and o['team'] == r['team']]
            if same:
                paired.append(r['p'] - mean(same))
        settled = [r for r in valid if r['outcome'] is not None]
        ps = [r['p'] for r in settled]
        ys = [r['outcome'] for r in settled]
        settled_rivals = [r for r in rivals if r['outcome'] is not None]
        rival_ps = [r['p'] for r in settled_rivals]
        rival_ys = [r['outcome'] for r in settled_rivals]
        out[entrant] = dict(
            n_self=len(valid), n_self_retained=len(own),
            n_excluded_target=len(own) - len(valid), n_rival=len(rivals),
            mean_self_p=mean(r['p'] for r in valid) if valid else None,
            descriptive_mean_self_p=mean(r['p'] for r in own) if own else None,
            league_base_rate=mean(r['base_rate'] for r in valid) if valid else None,
            optimism_bias=mean(r['p'] - r['base_rate'] for r in valid) if valid else None,
            mean_rival_p=mean(r['p'] for r in rivals) if rivals else None,
            self_minus_rivals=mean(paired) if paired else None,
            n_paired_self_rival=len(paired), n_settled=len(settled),
            brier=mean(forecast.brier(p, y) for p, y in zip(ps, ys)) if ps else None,
            calibration=forecast.calibration_bins(ps, ys) if ps else None,
            n_rival_settled=len(settled_rivals),
            rival_brier=mean(forecast.brier(p, y) for p, y in zip(rival_ps, rival_ys)) if rival_ps else None,
            rival_calibration=forecast.calibration_bins(rival_ps, rival_ys) if rival_ps else None,
            sample_label='small sample; descriptive only',
            n_football_weeks=1 if rs else 0, n_independent_worlds=None,
            ranked=False, ci=None,
        )
    return out


def optimism(week: int, root=None, league_ids=None) -> dict:
    """Latest pre-kickoff own forecast per seat-week, with explicit exclusions.

    Qualification and cutoff come from the existing retained forecast reader.
    Pending even fields have a conditional .5 reference; settlement can later
    refuse it for a boundary tie. No odd-field or tie policy is invented.
    """
    root = Path(root) if root is not None else runtime_root(Path(__file__).resolve().parents[2])
    frame = proof.real_forecasts(root, league_ids)
    coverage = [r for r in frame['coverage'] if r['week'] == week]
    audit = {(r['league'], r['team']): r for r in coverage}
    rows, book_rows, excluded, diagnostics = [], [], [], []
    entrants = {r['entrant'] for r in coverage}
    raw_values = {ent: [] for ent in entrants}
    pre_values = {ent: [] for ent in entrants}
    for lid in frame['cohort_selection']['selected']:
        source = frame['source_receipts'][lid]
        directory = root / 'leagues' / lid
        # Match the verified prefix even if a live writer has since appended.
        lines = (directory / 'log.jsonl').read_bytes().splitlines(keepends=True)
        raw = b''.join(lines[:source['rows']])
        cfg_raw = (directory / 'league.json').read_bytes()
        if (hashlib.sha256(raw).hexdigest() != source['sha256'] or
                hashlib.sha256(cfg_raw).hexdigest() != source['config_sha256']):
            raise ValueError('forecast source changed during read: ' + lid)
        cfg = json.loads(cfg_raw)
        seats = {s['team_id']: s['entrant_id'] for s in cfg['seats']}
        chain = [json.loads(line) for line in raw.splitlines()]
        events = [dict(r['payload'], seq=r['seq']) for r in chain]
        from bm.ops.served_model import misserved_ids
        nobody = misserved_ids(events)    # served by another model (bm.ops.served_model): no model's forecast
        events = [e for e in events if e.get('week') == week]
        points = next((e['points'] for e in reversed(events) if e['type'] == 'week_scored'), None)
        candidates, books = {}, {}

        def reject(e, reason):
            excluded.append(dict(league=lid, team=e.get('team'), seq=e['seq'],
                                 source=e['type'], version=e.get('version'), reason=reason))

        def observation(e, target, p, source):
            a = audit[(lid, e['team'])]
            status = a['target_status']
            if len(seats) % 2:
                status = 'unqualified_odd_field'
            base = .5 if status in ('outcome_missing', 'defined_even_field_no_boundary_tie') else None
            y = None
            if status == 'defined_even_field_no_boundary_tie':
                y = sorted(points.values(), reverse=True).index(points[target]) < len(seats) // 2
            r = dict(league=lid, week=week, team=e['team'], entrant=seats[e['team']],
                     target_team=target, relation='self' if target == e['team'] else 'rival',
                     p=float(p), base_rate=base, outcome=y, target_status=status,
                     reference_status='conditional_on_even_field_no_boundary_tie' if status == 'outcome_missing' and base else status,
                     source=source, seq=e['seq'], version=e.get('version'),
                     filed_at=e.get('logged_at') if source == 'decision' else e.get('ts'))
            if base is None:
                reject(e, status)
            return r

        for e in events:
            if e['type'] not in ('decision', 'forecast_filed'):
                continue
            team = e.get('team')
            if (lid, team) not in audit:
                reject(e, 'unknown_team'); continue
            if e.get('forced_default'):
                reject(e, 'forced_default'); continue
            if e.get('decision_id') in nobody:
                reject(e, 'served_by_other_model'); continue
            if (e['type'] == 'decision' and e.get('entrant') != seats[team]) or (
                    e['type'] == 'forecast_filed' and e.get('entrant', seats[team]) != seats[team]):
                reject(e, 'entrant_mismatch'); continue
            p = _top_half(e.get('forecast')) if e['type'] == 'decision' else None
            if e['type'] == 'decision':
                if not _prob(p):
                    if isinstance(e.get('forecast'), dict) and 'win_prob_this_week' in e['forecast']:
                        reject(e, 'invalid_probability')
                    continue
                raw_values[seats[team]].append(p)
            a = audit[(lid, team)]
            cutoff = proof._filing_time(a['first_kickoff'])
            # Book scorer uses filing ts; decisions require actual logged_at.
            filed = proof._filing_time(e.get('ts') if e['type'] == 'forecast_filed' else e.get('logged_at'))
            actual = proof._filing_time(e.get('logged_at'))
            if cutoff is None or filed is None:
                reject(e, 'unknown_filing_or_cutoff'); continue
            if filed >= cutoff or (e['type'] == 'forecast_filed' and actual is not None and actual >= cutoff):
                reject(e, 'post_cutoff'); continue
            target = books if e['type'] == 'forecast_filed' else candidates
            target.setdefault(team, []).append((filed, e['seq'], e))
            if e['type'] == 'decision':
                pre_values[seats[team]].append(p)

        for team, versions in candidates.items():
            e = max(versions, key=lambda v: v[:2])[2]
            rows.append(observation(e, team, _top_half(e['forecast']), 'decision'))
        for team, versions in books.items():
            e = max(versions, key=lambda v: v[:2])[2]
            book = e.get('book') or {}
            own = _top_half(book.get('self'))
            targets = book.get('teams') or {}
            targets = targets if isinstance(targets, dict) else {}
            probs = {t: _top_half(obj) for t, obj in targets.items()}
            if own is not None:
                if team in probs and probs[team] != own:
                    reject(e, 'conflicting_self_probabilities'); continue
                probs[team] = own
            admitted = 0
            for t, p in probs.items():
                if t not in seats:
                    reject(e, 'unknown_target_team')
                elif not _prob(p):
                    reject(e, 'invalid_probability')
                else:
                    book_rows.append(observation(e, t, p, 'forecast_filed')); admitted += 1
            if not admitted:
                reject(e, 'no_explicit_top_half_probability')
            diagnostics.append(dict(league=lid, team=team, version=e.get('version'),
                                    selected_seq=e['seq'], prelock_versions=len(versions),
                                    matchup_p_win_ignored=isinstance(book.get('week'), dict) and 'p_win' in book['week']))

    metrics = _metrics(rows, entrants)
    comparison = {}
    for ent in sorted(entrants):
        ps, all_ps = pre_values[ent], raw_values[ent]
        selected = metrics[ent]['mean_self_p']
        comparison[ent] = dict(
            selected_seat_week_mean=selected, n_selected=metrics[ent]['n_self'],
            all_prelock_decision_mean=mean(ps) if ps else None, n_prelock_decisions=len(ps),
            retrospective_all_decision_mean=mean(all_ps) if all_ps else None,
            n_all_decisions=len(all_ps),
            outside_review_range=selected is not None and not .52 <= selected <= .59,
            selected_minus_retrospective_mean=selected - mean(all_ps) if selected is not None and all_ps else None,
            difference_reason='headline uses latest prelock forecast per seat-week; retrospective row means provide a diagnostic comparison to the rounded review range, whose aggregation method is not specified; post-cutoff rows and repeated decisions are not headline observations',
        )
    return dict(
        schema_version=1, week=week, by_entrant=metrics,
        books_by_entrant=_metrics(book_rows, entrants), rows=rows, book_rows=book_rows,
        excluded=excluded, book_version_diagnostics=diagnostics,
        coverage=coverage, cohort_selection=frame['cohort_selection'],
        source_receipts=frame['source_receipts'], data_review_comparison=comparison,
        target_definition=frame['target_definition'],
        reference_definition='Even-field top-half mass is .5 conditional on no boundary tie; not a specific team probability or empirical climatology.',
        selection='latest valid decision forecast / latest filed book strictly before first kickoff; one self observation per source and seat-week',
        inference_status='small sample; parallel leagues share the same football week; no CI, ranking, or population calibration claim; pending and excluded outcomes are null',
        self_rival_definition='paired self minus mean own rival forecasts in the same selected book; absent rival forecasts are null',
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--week', type=int, required=True)
    parser.add_argument('--root', type=Path)
    parser.add_argument('--league', action='append')
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    result = optimism(args.week, args.root, args.league)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + '\n')
    summary = {ent: {k: m[k] for k in ('n_self', 'mean_self_p', 'optimism_bias', 'n_settled')}
               for ent, m in result['by_entrant'].items() if m['n_self_retained']}
    print(json.dumps(dict(out=str(args.out), week=args.week, by_entrant=summary,
                          exclusions=len(result['excluded'])), sort_keys=True))


if __name__ == '__main__':
    main()
