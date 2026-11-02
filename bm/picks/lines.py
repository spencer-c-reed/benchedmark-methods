from __future__ import annotations
import copy
import math
import statistics
from datetime import datetime, timedelta
from bm.score.picks import no_vig_probs
TEAMS = {'Arizona Cardinals': 'ARI', 'Atlanta Falcons': 'ATL', 'Baltimore Ravens': 'BAL', 'Buffalo Bills': 'BUF', 'Carolina Panthers': 'CAR', 'Chicago Bears': 'CHI', 'Cincinnati Bengals': 'CIN', 'Cleveland Browns': 'CLE', 'Dallas Cowboys': 'DAL', 'Denver Broncos': 'DEN', 'Detroit Lions': 'DET', 'Green Bay Packers': 'GB', 'Houston Texans': 'HOU', 'Indianapolis Colts': 'IND', 'Jacksonville Jaguars': 'JAX', 'Kansas City Chiefs': 'KC', 'Las Vegas Raiders': 'LV', 'Los Angeles Chargers': 'LAC', 'Los Angeles Rams': 'LA', 'Miami Dolphins': 'MIA', 'Minnesota Vikings': 'MIN', 'New England Patriots': 'NE', 'New Orleans Saints': 'NO', 'New York Giants': 'NYG', 'New York Jets': 'NYJ', 'Philadelphia Eagles': 'PHI', 'Pittsburgh Steelers': 'PIT', 'San Francisco 49ers': 'SF', 'Seattle Seahawks': 'SEA', 'Tampa Bay Buccaneers': 'TB', 'Tennessee Titans': 'TEN', 'Washington Commanders': 'WAS'}
REF_BOOKS = ('draftkings', 'fanduel', 'betmgm')

def _snapshots(source: str='odds') -> list[dict]:
    from bm.stats import market
    return sorted((m for m in market.manifest() if m['source'] == source), key=lambda m: m['retrieved_at'])

def snapshot_at(t: datetime) -> dict | None:
    from bm.stats import market
    ms = [m for m in _snapshots() if datetime.fromisoformat(m['retrieved_at']) <= t]
    return dict(market._read(ms[-1]['path']), _meta=ms[-1]) if ms else None

def game_key(g: dict, schedule: list[dict]) -> str | None:
    h, a = (TEAMS.get(g.get('home_team')), TEAMS.get(g.get('away_team')))
    k = datetime.fromisoformat(g['commence_time'].replace('Z', '+00:00'))
    for s in schedule:
        if s['home'] == h and s['away'] == a and (abs(datetime.fromisoformat(s['kickoff']) - k) < timedelta(hours=6)):
            return s['game_id']
    return None
QUOTE_STALE_AFTER_S = 24 * 60 * 60

def _time(value):
    try:
        t = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return t if t.tzinfo is not None else None
    except (AttributeError, ValueError, TypeError):
        return None

def _quote_inputs(game: dict, as_of: datetime | None) -> dict:
    out = {k: [] for k in ('h2h', 'spreads', 'totals')}
    number = lambda x: isinstance(x, (int, float)) and (not isinstance(x, bool)) and math.isfinite(x)
    for book in game.get('bookmakers', []):
        for market in book.get('markets', []):
            kind = market.get('key')
            if kind not in out:
                continue
            names = ['Over', 'Under'] if kind == 'totals' else [game['home_team'], game['away_team']]
            sides = ['over', 'under'] if kind == 'totals' else ['home', 'away']
            raw = market.get('outcomes', [])
            quotes = [next((x for x in raw if x.get('name') == n), None) for n in names]
            prices = [q.get('price') if q else None for q in quotes]
            points = [q.get('point') if q else None for q in quotes]
            valid = len(raw) == 2 and all((q is not None for q in quotes)) and all((number(v) and v != 0 for v in prices))
            if kind != 'h2h':
                valid = valid and all((number(v) for v in points)) and (points[0] == points[1] if kind == 'totals' else points[0] == -points[1])
            update = market.get('last_update') or book.get('last_update')
            t = _time(update)
            age = (as_of - t).total_seconds() if as_of and t else None
            freshness = 'missing or invalid update/as_of time' if age is None else 'inconsistent: source update after as_of' if age < 0 else 'stale under archival age policy' if age > QUOTE_STALE_AFTER_S else 'within archival age policy'
            out[kind].append({'book_key': book.get('key'), 'book_title': book.get('title'), 'market': kind, 'sides': sides, 'outcome_names': names, 'prices': prices, 'points': points, 'threshold': points[0] if kind != 'h2h' else None, 'book_last_update': book.get('last_update'), 'market_last_update': market.get('last_update'), 'update_time_source': 'market_last_update' if market.get('last_update') else 'book_last_update_fallback' if book.get('last_update') else 'missing', 'quote_age_s': age, 'freshness_status': freshness, 'pair_status': 'valid two-side proposition' if valid else 'invalid or missing two-side proposition', 'no_vig_probs': no_vig_probs(prices) if valid else None, 'raw_outcomes': copy.deepcopy(raw)})
    return out

def parse(snap: dict, schedule: list[dict], as_of: datetime | None=None) -> dict[str, dict]:
    out = {}
    meta = snap.get('_meta', {})
    as_of = as_of or _time(meta.get('retrieved_at'))
    for g in snap.get('games', []):
        gid = game_key(g, schedule)
        if not gid:
            continue
        home = g['home_team']
        books = {b['key']: {m['key']: m['outcomes'] for m in b['markets']} for b in g.get('bookmakers', [])}
        if not books:
            continue
        inputs = _quote_inputs(g, as_of)
        admitted = {k: [p for p in v if p['pair_status'] == 'valid two-side proposition'] for k, v in inputs.items()}
        ph = [p['no_vig_probs'][0] for p in admitted['h2h']]
        sp = [p['threshold'] for p in admitted['spreads']]
        ps = [p['no_vig_probs'][0] for p in admitted['spreads']]
        tot = [p['threshold'] for p in admitted['totals']]
        po = [p['no_vig_probs'][0] for p in admitted['totals']]
        ref_key = next((b for b in REF_BOOKS if b in books), sorted(books)[0])
        ref = books[ref_key]

        def side(market, name):
            return next(({'price': x['price'], 'point': x.get('point')} for x in ref.get(market, []) if x['name'] == name), None)
        away = g['away_team']
        sched = next((s for s in schedule if s['game_id'] == gid))
        med = lambda xs: round(statistics.median(xs), 4) if xs else None
        out[gid] = {'game_id': gid, 'kickoff': sched['kickoff'], 'home': sched['home'], 'away': sched['away'], 'ref_book': ref_key, 'ref': {'h2h': {'home': side('h2h', home), 'away': side('h2h', away)}, 'spreads': {'home': side('spreads', home), 'away': side('spreads', away)}, 'totals': {'over': side('totals', 'Over'), 'under': side('totals', 'Under')}}, 'consensus': {'p_home': med(ph), 'spread_home': med(sp), 'p_cover_home': med(ps), 'total': med(tot), 'p_over': med(po)}, 'n_books': len(books), 'dispersion_p_home': round(statistics.pstdev(ph), 4) if len(ph) > 1 else None, 'consensus_threshold_uniform': {'spreads': bool(sp) and len(set(sp)) == 1, 'totals': bool(tot) and len(set(tot)) == 1}, 'snapshot': meta.get('retrieved_at'), 'market_provenance': {'schema': 'observed-market/1', 'odds_event_id': g.get('id'), 'provider_commence_time': g.get('commence_time'), 'as_of': as_of.isoformat() if as_of else None, 'snapshot_manifest': {k: meta.get(k) for k in ('source', 'retrieved_at', 'fetch_started_at', 'retrieval_time_semantics', 'sha256', 'path')}, 'raw_bookmakers': copy.deepcopy(g.get('bookmakers', [])), 'reference_book_key': ref_key, 'reference_selection': 'first available draftkings/fanduel/betmgm; otherwise lexical book key', 'consensus_inputs': inputs, 'consensus_contributing_books': {k: [p['book_key'] for p in admitted[k]] for k in inputs}, 'reference_market_status': {k: 'present' if ref.get(k) else 'missing' for k in inputs}, 'retrieval_provenance_status': 'completion timestamp retained' if meta.get('retrieval_time_semantics') else 'legacy: fetch completion time unverified', 'snapshot_age_s': (as_of - _time(meta.get('retrieved_at'))).total_seconds() if as_of and _time(meta.get('retrieved_at')) else None, 'consensus_method': 'median of per-book proportional no-vig two-side probabilities', 'no_vig_method': 'normalize inverse decimal odds within each two-side book quote; not a calibrated truth probability', 'consensus_proposition_status': {k: 'missing valid two-side quote' if not admitted[k] else 'mixed thresholds; diagnostic aggregation only' if k != 'h2h' and len({p['threshold'] for p in admitted[k]}) > 1 else 'same two-side proposition; excludes push/tie mass' for k in inputs}, 'freshness_policy': {'stale_after_s': QUOTE_STALE_AFTER_S, 'purpose': 'archival age diagnostic; not executable-price admission'}, 'execution_status': 'observed quotes only; availability, limits and liquidity unverified'}}
    return out

def at(t: datetime, schedule: list[dict]) -> dict[str, dict]:
    s = snapshot_at(t)
    return parse(s, schedule, as_of=t) if s else {}

def close(game_id: str, schedule: list[dict], source: str='odds') -> dict | None:
    sched = next((s for s in schedule if s['game_id'] == game_id))
    k = datetime.fromisoformat(sched['kickoff'])
    for m in reversed([m for m in _snapshots(source) if datetime.fromisoformat(m['retrieved_at']) <= k]):
        from bm.stats import market
        g = parse(dict(market._read(m['path']), _meta=m), schedule, as_of=k).get(game_id)
        if g:
            g['market_provenance']['observation_role'] = 'last observed pre-kickoff; actual close unverified'
            return g
    return None

def comparator_at_reference(game: dict, kind: str, side: str, *, source_bytes: bytes | None=None) -> dict:
    import hashlib
    import json
    allowed = {'h2h': ('home', 'away'), 'spreads': ('home', 'away'), 'totals': ('over', 'under')}
    if kind not in allowed or side not in allowed[kind]:
        raise ValueError('unknown comparator market/side')
    provenance = game.get('market_provenance') or {}
    manifest = provenance.get('snapshot_manifest') or {}
    raw_quotes = provenance.get('consensus_inputs', {}).get(kind, [])
    ref = game.get('ref', {}).get(kind, {}).get(side)
    threshold = None if kind == 'h2h' else (ref or {}).get('point')
    if kind == 'spreads' and side == 'away' and (threshold is not None):
        threshold = -threshold
    asof, kickoff = (_time(provenance.get('as_of')), _time(game.get('kickoff')))
    observed = _time(manifest.get('retrieved_at'))
    excluded = {k: 0 for k in ('invalid_pair', 'different_threshold', 'unknown_update', 'future_update', 'at_or_after_kickoff', 'duplicate_book')}
    diagnostic, timed, seen = ([], [], set())
    number = lambda v: type(v) in (int, float) and math.isfinite(v)
    ref_valid = isinstance(ref, dict) and number(ref.get('price')) and (ref['price'] != 0) and (kind == 'h2h' or number(threshold))
    for quote in raw_quotes:
        if quote.get('pair_status') != 'valid two-side proposition':
            excluded['invalid_pair'] += 1
            continue
        if not ref_valid or (kind != 'h2h' and quote.get('threshold') != threshold):
            excluded['different_threshold'] += 1
            continue
        key = quote.get('book_key')
        if not key or key in seen:
            excluded['duplicate_book'] += 1
            continue
        seen.add(key)
        probability = quote['no_vig_probs'][allowed[kind].index(side)]
        diagnostic.append(probability)
        updated = _time(quote.get('market_last_update') or quote.get('book_last_update'))
        if not updated or not asof:
            excluded['unknown_update'] += 1
        elif updated > asof:
            excluded['future_update'] += 1
        elif not kickoff or updated >= kickoff:
            excluded['at_or_after_kickoff'] += 1
        else:
            timed.append(probability)
    bytes_verified = False
    if source_bytes is not None and hashlib.sha256(source_bytes).hexdigest() == manifest.get('sha256'):
        try:
            matches = [g for g in json.loads(source_bytes).get('games', []) if g.get('id') == provenance.get('odds_event_id')]
            bytes_verified = len(matches) == 1 and matches[0].get('bookmakers') == provenance.get('raw_bookmakers') and (TEAMS.get(matches[0].get('home_team')) == game.get('home')) and (TEAMS.get(matches[0].get('away_team')) == game.get('away')) and (matches[0].get('commence_time') == provenance.get('provider_commence_time')) and (_quote_inputs(matches[0], asof).get(kind) == raw_quotes)
            reference_quotes = [q for q in raw_quotes if q.get('book_key') == game.get('ref_book')]
            index = allowed[kind].index(side)
            bytes_verified = bytes_verified and len(reference_quotes) == 1 and ref_valid
            if bytes_verified:
                q = reference_quotes[0]
                bytes_verified = q.get('pair_status') == 'valid two-side proposition' and q['prices'][index] == ref['price'] and (kind == 'h2h' or q['points'][index] == ref.get('point'))
        except (ValueError, TypeError, AttributeError):
            pass
    completed = manifest.get('retrieval_time_semantics') == 'fetch completed; local clock'
    time_valid = bool(asof and observed and kickoff and (observed <= asof <= kickoff) and (observed < kickoff))
    qualified = statistics.median(timed) if timed and bytes_verified and completed and time_valid else None
    return {'schema': 'reference-quote-comparator/1', 'game_id': game.get('game_id'), 'market': kind, 'side': side, 'reference_book': game.get('ref_book'), 'reference_threshold_home_or_total': threshold, 'diagnostic_no_vig_probability': statistics.median(diagnostic) if diagnostic else None, 'matched_quote_count': len(diagnostic), 'time_eligible_quote_count': len(timed), 'excluded_quote_counts': excluded, 'time_source_qualified_quote_probability': qualified, 'snapshot_bytes_and_event_verified': bytes_verified, 'retrieval_completion_verified': completed, 'snapshot_time_eligible': time_valid, 'status': 'time/source-qualified observed quote diagnostic' if qualified is not None else 'unqualified observed quote diagnostic', 'target_conditioning': 'same reference proposition, conditional on no push/tie; not unconditional model forecast probability', 'method': 'median of distinct-book proportional two-side inverse-decimal probabilities at the exact reference threshold', 'inference_status': 'no model pairing/score/rank/calibration; bookmaker quotes are not independent outcome samples', 'execution_status': 'no verified execution, limits, commission, liquidity or exchange attribution'}
