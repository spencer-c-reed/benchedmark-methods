"""Retained forecast readout and legacy unqualified market/forecast demo."""
from __future__ import annotations

import gzip
import json
import os
from statistics import mean, stdev
from typing import Any

from . import forecast, picks


def main():
    """Legacy demo only; not the production Picks collector or a scored comparison."""
    print("LEGACY UNQUALIFIED DEMO: not current production or a market/model comparison.")
    print("Raw implied means include vig; missing-reference 0.5 fallbacks are demo-only.")
    print("Price gaps are not closing-line value; outcomes are unknown; no forecast scores certified.")
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    # === Part A: Real odds snapshot ===
    print("=" * 80)
    print("A. LEGACY SNAPSHOT RAW IMPLIED MEANS (NOT NO-VIG)")
    print("=" * 80)

    manifest_path = os.path.join(repo_root, 'data/raw/market/manifest.jsonl')
    odds_files = []

    with open(manifest_path) as f:
        for line in f:
            entry = json.loads(line)
            if entry.get('source') == 'odds':
                odds_files.append(os.path.join(repo_root, entry['path']))

    if not odds_files:
        print("No odds files found in manifest.")
        return

    # Load the first odds file
    odds_path = odds_files[0]
    with gzip.open(odds_path) as f:
        odds_data = json.load(f)

    games = odds_data.get('games', [])
    print(f"Loaded {len(games)} games from odds snapshot.")
    print()

    # Legacy raw implied means, not matched-outcome no-vig consensus
    consensus_rows = []

    for game in games:
        away_team = game.get('away_team', 'Away')
        home_team = game.get('home_team', 'Home')
        game_id = game.get('id', '')

        bookmakers = game.get('bookmakers', [])

        # Collect all h2h, spreads, totals across bookmakers
        h2h_probs = []
        spread_probs = []
        total_probs = []

        for book in bookmakers:
            markets = book.get('markets', [])
            for market in markets:
                key = market.get('key')
                outcomes = market.get('outcomes', [])

                if key == 'h2h':
                    # Get home team h2h odds
                    for outcome in outcomes:
                        name = outcome.get('name')
                        price = outcome.get('price')
                        if name == home_team and price:
                            h2h_probs.append(picks.implied_prob(price))

                elif key == 'spreads':
                    # Get home team spread odds
                    for outcome in outcomes:
                        name = outcome.get('name')
                        point = outcome.get('point')
                        price = outcome.get('price')
                        if name == home_team and point and price:
                            spread_probs.append(picks.implied_prob(price))

                elif key == 'totals':
                    # Get over odds
                    for outcome in outcomes:
                        name = outcome.get('name')
                        price = outcome.get('price')
                        if name == 'Over' and price:
                            total_probs.append(picks.implied_prob(price))

        # Preserve historical raw means; no no-vig/closing-line interpretation.
        h2h_consensus = mean(h2h_probs) if h2h_probs else 0.5
        spread_consensus = mean(spread_probs) if spread_probs else 0.5
        total_consensus = mean(total_probs) if total_probs else 0.5

        consensus_rows.append({
            'game': f"{away_team} @ {home_team}",
            'p_home': h2h_consensus,
            'spread': spread_consensus,
            'total': total_consensus,
            'n_books': len(bookmakers)
        })

    # Print top rows
    print("Game                                   p(Home Win)  p(Spread)  p(Over)  Books")
    print("-" * 80)
    for row in consensus_rows[:10]:
        print(f"{row['game'][:35]:35s}  {row['p_home']:5.2f}       {row['spread']:5.2f}     {row['total']:5.2f}    {row['n_books']:2d}")

    if len(consensus_rows) > 10:
        print(f"... and {len(consensus_rows) - 10} more games")
    print()

    # === Part B: Picks scoring with market-follower picker ===
    print("=" * 80)
    print("B. UNSCORED LEGACY PRICE-GAP DEMO")
    print("=" * 80)

    # Market-follower strategy: 1% of bankroll on best CLV vs consensus
    follower_picks = []
    bankroll = 1000.0

    for game in games[:5]:  # Use first 5 games
        home_team = game.get('home_team', 'Home')
        away_team = game.get('away_team', 'Away')
        game_id = game.get('id', '')
        bookmakers = game.get('bookmakers', [])

        if not bookmakers:
            continue

        # Get best h2h odds
        best_h2h = None
        best_h2h_price = None

        for book in bookmakers:
            for market in book.get('markets', []):
                if market.get('key') == 'h2h':
                    for outcome in market.get('outcomes', []):
                        if outcome.get('name') == home_team:
                            price = outcome.get('price')
                            if price and (best_h2h_price is None or price > best_h2h_price):
                                best_h2h_price = price
                                best_h2h = outcome.get('name')

        if best_h2h_price:
            # Check if CLV is positive against consensus
            consensus_p = mean(h_p for row in consensus_rows if row['game'].endswith(home_team) for h_p in [row['p_home']])
            if not consensus_p:
                consensus_p = 0.5

            be = picks.break_even(best_h2h_price)
            if consensus_p > be:  # Positive CLV
                stake = bankroll * 0.01

                follower_picks.append({
                    'game_id': game_id,
                    'market': 'h2h',
                    'side': 'home',
                    'line': None,
                    'price': best_h2h_price,
                    'stake': stake,
                    'p_close_novig': consensus_p,
                    'outcome': 'unknown'  # Would need actual results
                })

    print(f"Market-follower picks: {len(follower_picks)} bets at 1% per pick")
    if follower_picks:
        avg_clv = mean([picks.clv_prob(p['p_close_novig'], p['price']) for p in follower_picks])
        print(f"Legacy raw-implied reference gap (NOT CLV): {avg_clv:.4f} prob")
    print()

    # === Part C: Forecast scoring ===
    print("=" * 80)
    print("C. LEGACY FORECAST INVENTORY (UNSCORED)")
    print("=" * 80)

    league_log_path = os.path.join(repo_root, 'leagues/nfl-x1/log.jsonl')

    forecast_count = 0
    forecast_scores = []

    if os.path.exists(league_log_path):
        with open(league_log_path) as f:
            for line in f:
                entry = json.loads(line)
                payload = entry.get('payload', {})

                # Look for decision events with forecasts
                if payload.get('type') == 'decision' and payload.get('kind') == 'start_sit':
                    forecast = payload.get('forecast')
                    if forecast:
                        forecast_count += 1
                        # Compute CRPS if we have quantiles
                        if isinstance(forecast, dict) and 'quantiles' in forecast:
                            # Example scoring (would need actual outcomes)
                            pass  # Quantile presence is not an observed forecast score.

                # Look for scoring events with actual outcomes
                if payload.get('type') == 'week_scored':
                    # Could score forecasts against actual outcomes here
                    pass

        print(f"League log loaded. Forecast events found: {forecast_count}")

        if forecast_scores:
            print(f"Sample forecast scores (if outcomes available): {forecast_scores[:5]}")
            print(f"Mean score: {mean(forecast_scores):.4f}")
        else:
            print("No complete forecast-outcome pairs available in this snapshot.")
    else:
        print("League log not found at expected path.")

    print()

    # === Summary ===
    print("=" * 80)
    print("LEGACY DEMO SUMMARY (NOT VALIDATION)")
    print("=" * 80)
    print(f"Odds consensus: {len(consensus_rows)} games with multi-book consensus")
    print(f"Picks demo: {len(follower_picks)} market-follower picks identified")
    print(f"Forecast demo: {forecast_count} decisions with forecast data")
    print()
    print("Legacy inventory only; no production, model ability, no-vig or scored comparison certified.")




def _forecast_metrics(rows, entrants):
    """Point estimates and explicitly diagnostic shared-world-week block bands."""
    from bm.score import forecast as F
    out = {}
    for ent in sorted(entrants):
        rs = [r for r in rows if r['entrant'] == ent]
        m = [F.brier(r['p'], r['y']) for r in rs]
        c = [F.brier(.5, r['y']) for r in rs]
        blocks = [r['world_week'] for r in rs]
        unknown = any(b is None for b in blocks)
        sk = F.skill_with_ci(m, c, clusters=blocks, seed=0) if not unknown else dict(skill=F.skill(m,c), ci_lower=None, ci_upper=None, ci_status='unknown shared-world provenance')
        out[ent] = dict(n=len(rs), brier=round(mean(m),4) if m else None,
                        log=round(mean(F.log_score(r['p'],r['y']) for r in rs),4) if rs else None,
                        mean_p=round(mean(r['p'] for r in rs),3) if rs else None,
                        hit=sum(r['y'] for r in rs) if rs else None,
                        n_world_week_blocks=None if unknown else len(set(blocks)),
                        n_independent_worlds=None, ranked=False,
                        ci_kind='pointwise shared-world-week diagnostic only; weeks not established independent, no simultaneous/population inference',
                        inference_status='descriptive only; model/harness/default exposure, missingness and prospective power unqualified')
        out[ent]['skill_vs_0.5']=sk
    return out


def _filing_time(value):
    from datetime import datetime
    try:
        t = datetime.fromisoformat(value)
        return t if t.tzinfo else None
    except (ValueError, TypeError):
        return None


def real_forecasts(root=None, league_ids=None) -> dict:
    """Retained settled forecast readout, not a prospective capability proof.

    Default selection matches the live-season metadata cohort. Explicit historical
    cohorts remain available as descriptive diagnostics. Last actually filed own
    forecast before first kickoff is qualified separately from legacy last-anytime
    forecasts. Parallel leagues share football outcomes; route attempts, model
    calls and repeated league instances are not independent task/world samples.
    """
    import hashlib
    import math
    from pathlib import Path
    from bm.log import canonical
    root = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    config_bytes = {p.name: (p/'league.json').read_bytes() for p in sorted((root/'leagues').iterdir())
                    if (p/'league.json').is_file() and (p/'log.jsonl').is_file()}
    available = {lid: json.loads(raw) for lid,raw in config_bytes.items()}
    ids = list(league_ids) if league_ids is not None else [lid for lid,cfg in available.items()
           if cfg.get('status') == 'private-live-season' and cfg.get('format') == 'season']
    if len(set(ids)) != len(ids) or any(lid not in available for lid in ids):
        raise ValueError('explicit cohort requires unique retained league IDs')
    qualified, descriptive, audit, sources, entrants = [], [], [], {}, set()
    for lid in ids:
        cfg = available[lid]; path = root/'leagues'/lid/'log.jsonl'
        raw = path.read_bytes()  # one coherent prefix; original logs are read-only
        chain = [json.loads(line) for line in raw.splitlines() if line.strip()]
        prev = '0'*64
        for seq,row in enumerate(chain,1):
            body = canonical(row['payload'])
            if (row['seq'] != seq or row['prev_hash'] != prev or
                    row['payload_hash'] != hashlib.sha256(body).hexdigest() or
                    row['hash'] != hashlib.sha256(prev.encode()+body).hexdigest()):
                raise ValueError('forecast source chain invalid: '+lid)
            prev = row['hash']
        evs = [r['payload'] for r in chain]
        sources[lid] = dict(sha256=hashlib.sha256(raw).hexdigest(), rows=len(chain), head=prev,
                            config_sha256=hashlib.sha256(config_bytes[lid]).hexdigest())
        seats = {s['team_id']:s['entrant_id'] for s in cfg.get('seats',[])}
        entrants.update(seats.values())
        scored, cutoffs, decisions, defaults = {}, {}, {}, {}
        weeks = set()
        from bm.ops.served_model import misserved_ids
        nobody = misserved_ids(evs)       # served by another model (bm.ops.served_model): no model's forecast
        for e in evs:
            w = e.get('week')
            if type(w) is not int: continue
            if cfg.get('end_week') is not None and w > cfg['end_week']: continue
            weeks.add(w)
            if e['type'] == 'week_scored': scored[w] = e['points']
            if e['type'] == 'lineup_lock':
                t = _filing_time(e.get('kickoff'))
                if t: cutoffs[w] = min(t,cutoffs.get(w,t))
            if e['type'] == 'decision' and e.get('decision_id') not in nobody:
                key=(e.get('team'),w)
                if e.get('forced_default'): defaults[key]=defaults.get(key,0)+1
                else: decisions.setdefault(key,[]).append(e)
        for w in sorted(weeks):
            for team,seat_ent in seats.items():
                from bm import succession   # a bot operator's team-week (rules/policies.md seat_operators) is no model's
                if succession.operated(seat_ent,league=cfg.get('id',lid),week=w): continue
                key=(team,w); candidates=[];valid=[];late=unknown=invalid=mismatch=0
                for e in decisions.get(key,[]):
                    if e.get('entrant') != seat_ent:
                        mismatch+=1;continue
                    p=(e.get('forecast') or {}).get('win_prob_this_week') if isinstance(e.get('forecast'),dict) else None
                    if type(p) not in (int,float) or not math.isfinite(p) or not 0<=p<=1:
                        invalid+=1;continue
                    entrants.add(e['entrant']); candidates.append(e)
                    filed=_filing_time(e.get('logged_at'))
                    if filed is None or w not in cutoffs: unknown+=1
                    elif filed>=cutoffs[w]: late+=1
                    else: valid.append(e)
                pts=scored.get(w)
                complete=(isinstance(pts,dict) and bool(seats) and set(pts)==set(seats) and all(type(pts.get(t)) in (int,float) and math.isfinite(pts[t]) for t in seats))
                # The elicitation specifies top half, but no boundary-tie/odd-field policy.
                ordered = sorted(pts.values(), reverse=True) if complete else []
                target_status = ('outcome_missing' if not complete else
                                 'unqualified_odd_field' if len(ordered) % 2 else
                                 'unqualified_boundary_tie' if ordered[len(ordered)//2-1] == ordered[len(ordered)//2] else
                                 'defined_even_field_no_boundary_tie')
                target_qualified = target_status == 'defined_even_field_no_boundary_tie'
                # Preserve the previous descriptive tie convention explicitly.
                y=(sorted(pts.values(),reverse=True).index(pts[team])+1<=len(pts)/2) if complete else None
                block=(cfg.get('sport'),cfg.get('season'),w) if cfg.get('sport') and type(cfg.get('season')) is int and cfg.get('status') in ('private-live-season','private-finals-rehearsal','private-prototype-ended-wk4') else None
                def add(target,e):
                    target.append(dict(league=lid,team=team,entrant=e['entrant'],week=w,p=float(e['forecast']['win_prob_this_week']),y=y,world_week=block))
                if complete and candidates: add(descriptive,candidates[-1])
                if complete and target_qualified and valid: add(qualified,max(valid,key=lambda e:_filing_time(e['logged_at'])))
                audit.append(dict(league=lid,team=team,entrant=seat_ent,week=w,
                                  target_status=target_status,
                                  outcome_status='settled_event' if complete else 'incomplete_settled_points' if pts is not None else 'not_settled',
                                  own_forecast_rows=len(candidates), prelock_forecast_rows=len(valid), late_forecast_rows=late,
                                  retrospective_selected_logged_at=candidates[-1].get('logged_at') if candidates else None,
                                  first_kickoff=cutoffs[w].isoformat() if w in cutoffs else None,
                                  unknown_filing_or_cutoff_rows=unknown, missing_or_invalid_probability_rows=invalid,
                                  forced_default_decision_rows=defaults.get(key,0), entrant_mismatch_rows=mismatch,
                                  missing_prelock_forecast=not bool(valid), pending_outcome=not complete,
                                  scored_prelock_pair=complete and target_qualified and bool(valid)))
    return dict(n=len(qualified),by_entrant=_forecast_metrics(qualified,entrants),
                descriptive_n=len(descriptive),descriptive_by_entrant=_forecast_metrics(descriptive,entrants),
                descriptive_status='retrospective last-anytime forecasts; post-kickoff information may be visible; not prospective ability',
                cohort_selection=dict(basis='explicit retained league IDs' if league_ids is not None else 'private-live-season / season metadata',selected=ids,excluded=[lid for lid in available if lid not in ids]),
                frame=dict(league_seat_weeks=len(audit), meaning='retained seat-week frame, not proof every elicitation/provider attempt occurred',
                           missing_prelock_forecasts=sum(r['missing_prelock_forecast'] for r in audit),pending_outcomes=sum(r['pending_outcome'] for r in audit),
                           unqualified_target_outcomes=sum(not r['pending_outcome'] and r['target_status'] != 'defined_even_field_no_boundary_tie' for r in audit),qualified_settled_pairs=len(qualified)),
                coverage=audit, source_receipts=sources,
                prelock_condition_status='actual event filing time only; Gateway/prompt/input/deadline attribution not certified',
                target_definition='win_prob_this_week: team scores in the league top half this week, not head-to-head matchup win; prelock scoring requires even field with no boundary tie',
                reference_definition='0.5 flat probability reference only; not empirical climatology, calibrated skill, market comparator or chance of this specific team winning',
                tie_convention='legacy competition rank; ties at boundary can label more than half true, 0.5 climatology not certified',
                inference_status='descriptive settled-event scores only; no calibrated independent-world effect, population CI or capability ranking; defaults and provider failures are not model zeros')


def real_markets(root=None) -> dict:
    """Readonly actual retained quote/forecast coverage; not a paired skill estimate."""
    import csv
    import hashlib
    import io
    import math
    from collections import Counter
    from pathlib import Path
    from bm.picks import lines as L
    from bm.stats.nflverse import kickoff_iso
    from bm.log import canonical
    root = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    def contained(path):
        p = (root / path).resolve()
        if not p.is_relative_to(root.resolve()):
            raise ValueError('market source path escapes retained root')
        return p
    def manifest(path):
        raw = contained(path).read_bytes()
        return [json.loads(line) for line in raw.splitlines() if line.strip()], hashlib.sha256(raw).hexdigest()
    def source(row):
        raw = gzip.decompress(contained(row['path']).read_bytes())
        if hashlib.sha256(raw).hexdigest() != row['sha256']:
            raise ValueError('market source snapshot hash mismatch: '+row['path'])
        return raw
    market_rows, market_hash = manifest('data/raw/market/manifest.jsonl')
    nfl_rows, nfl_hash = manifest('data/raw/nfl/manifest.jsonl')
    schedules = [r for r in nfl_rows if r.get('asset') == 'games']
    if not schedules:
        raise ValueError('no retained games source for market event mapping')
    schedule_source = schedules[-1]
    schedule = []
    for r in csv.DictReader(io.StringIO(source(schedule_source).decode('utf-8'))):
        if r['season'] == '2026' and r['game_type'] == 'REG' and r.get('gametime'):
            schedule.append(dict(game_id=r['game_id'], home=r['home_team'], away=r['away_team'],
                                 kickoff=kickoff_iso(r['gameday'], r['gametime'])))
    snapshots, details, book_keys, games = [], [], set(), set()
    for row in market_rows:
        if row.get('source') != 'odds': continue
        raw = source(row)
        parsed = L.parse(dict(json.loads(raw), _meta=row), schedule)
        snapshots.append(dict(path=row['path'], sha256=row['sha256'], retrieved_at=row.get('retrieved_at'),
                              retrieval_time_semantics=row.get('retrieval_time_semantics'), mapped_games=len(parsed)))
        for gid,g in parsed.items():
            games.add(gid)
            book_keys.update(q['book_key'] for qs in g['market_provenance']['consensus_inputs'].values() for q in qs if q.get('book_key'))
            for kind,side in [('h2h','home'),('spreads','home'),('totals','over')]:
                details.append(dict(snapshot=row['retrieved_at'], **L.comparator_at_reference(g, kind, side, source_bytes=raw)))
    forecasts, records_sources, attempts, seen_own = [], {}, Counter(), set()
    for path in sorted((root/'records/picks').glob('*.jsonl')):
        if path.stem.endswith('-scripted'): continue
        raw = path.read_bytes();prev='0'*64;chain=[json.loads(line) for line in raw.splitlines() if line.strip()]
        for seq,row in enumerate(chain,1):
            body=canonical(row['payload'])
            if row['seq'] != seq or row['prev_hash'] != prev or row['payload_hash'] != hashlib.sha256(body).hexdigest() or row['hash'] != hashlib.sha256(prev.encode()+body).hexdigest():
                raise ValueError('market forecast source chain invalid: '+path.name)
            prev=row['hash']
            e=row['payload']
            if e.get('kind') != 'pick': continue
            attempts[e.get('outcome','unknown')]+=1
            if e.get('outcome') != 'own' or not isinstance(e.get('answer'),dict): continue
            seat=e.get('seat_key') or e['key'].split(':attempt')[0]
            if seat in seen_own: continue  # first retained own answer; never winner-select retries
            seen_own.add(seat)
            for gid,a in e['answer'].items():
                if not isinstance(a,dict): continue
                finite=lambda p:type(p) in (int,float) and math.isfinite(p) and 0<=p<=1
                forecasts.append(dict(record_id=e['record_id'],seat_step=seat,entrant=e['entrant'],instance=e.get('instance'),
                                      step=e.get('step'),game_id=gid,slate=e.get('slate'),
                                      valid_probability_fields=[k for k in ('p_home','p_cover_home','p_over') if finite(a.get(k))],
                                      has_stake=any(s.get('usd',0)>0 for s in a.get('stakes',[]) if isinstance(s,dict)),
                                      receipt_context_present=all(e.get(k) for k in ('received_at','deadline','as_of')),
                                      quote_provenance_present=bool((e.get('lines') or {}).get(gid,{}).get('market_provenance'))))
        records_sources[path.name]=dict(sha256=hashlib.sha256(raw).hexdigest(),rows=len(chain),head=prev)
    return dict(schema='market-coverage-readout/1', snapshots=snapshots, quote_comparators=details,
                quote_frame=dict(snapshot_game_markets=len(details),distinct_mapped_games=len(games),book_keys=sorted(book_keys),
                    matched_reference_diagnostics=sum(d['diagnostic_no_vig_probability'] is not None for d in details),
                    time_source_qualified_quote_diagnostics=sum(d['time_source_qualified_quote_probability'] is not None for d in details)),
                forecast_frame=dict(attempt_rows=dict(attempts),observed_own_seat_steps=len(seen_own),forecast_game_rows=len(forecasts),
                    receipt_context_missing=sum(not f['receipt_context_present'] for f in forecasts),quote_provenance_missing_all_record_games=sum(not f['quote_provenance_present'] for f in forecasts),
                    line_step_game_rows=sum(f['step']=='line' for f in forecasts),
                    line_quote_provenance_missing=sum(f['step']=='line' and not f['quote_provenance_present'] for f in forecasts),
                    assigned_opportunity_denominator=None,meaning='all observed own forecast games, including unstaked/pass games; retries/routes are not independent samples'),
                forecasts=forecasts, sources=dict(market_manifest_sha256=market_hash,nfl_manifest_sha256=nfl_hash,schedule_snapshot=schedule_source,records=records_sources),
                pairing_status='not scored: full prospective original model/request/source/target/outcome pairing not established; absent receipts are unknown, not zero or failures',
                model_market_scores=None, independent_worlds=None,ranked=False,
                inference_status='archival coverage diagnostic only; latest schedule mapping not historical as-of certified; no liquidity/exchange/execution/calibration or independence established; fantasy top-half is a separate target')


if __name__ == "__main__":
    import json as _j
    import sys as _s
    if _s.argv[1:2] in (["real"], ["markets"]):
        import argparse as _a
        _p = _a.ArgumentParser()
        _p.add_argument("--root")
        _p.add_argument("--league", action="append")
        _args = _p.parse_args(_s.argv[2:])
        if _s.argv[1] == "markets" and _args.league:
            _p.error("--league applies only to fantasy real readout")
        result = real_markets(_args.root) if _s.argv[1] == "markets" else real_forecasts(_args.root, _args.league)
        print(_j.dumps(result, indent=1, default=str))
    else:
        main()
