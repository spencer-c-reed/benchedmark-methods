"""Picks/betting scoring: odds conversion, CLV, settlement, Kelly, bankroll, D-PICKS-2 measures."""
from __future__ import annotations

import math
import random
from statistics import mean, stdev


# === Odds helpers ===

def american_to_decimal(american: float) -> float:
    """Convert American odds to decimal (European) odds.

    Negative odds (favorites): decimal = 1 + 100 / |american|
    Positive odds (underdogs): decimal = 1 + american / 100

    Args:
        american: American-format odds (e.g., -110, +150)

    Returns:
        Decimal odds
    """
    if american < 0:
        return 1.0 + 100.0 / (-american)
    else:
        return 1.0 + american / 100.0


def implied_prob(american: float) -> float:
    """Implied probability from American odds (pre-vig).

    Args:
        american: American odds

    Returns:
        Implied probability [0, 1]
    """
    decimal = american_to_decimal(american)
    return 1.0 / decimal if decimal > 0 else 0.0


def no_vig_probs(american_prices: list[float]) -> list[float]:
    """Remove vig from American odds via multiplicative normalization.

    Implied probs sum to > 1 due to vig; normalize proportionally.

    Args:
        american_prices: list of American odds for all outcomes in a market

    Returns:
        List of no-vig probabilities (sum to 1.0)
    """
    if not american_prices:
        return []

    implied = [implied_prob(p) for p in american_prices]
    total = sum(implied)

    if total <= 0:
        return [1.0 / len(implied)] * len(implied)

    return [p / total for p in implied]


def break_even(american: float) -> float:
    """Break-even probability at given American odds.

    Probability at which expected value is zero: p = 1 / decimal.

    Args:
        american: American odds

    Returns:
        Break-even probability
    """
    return implied_prob(american)


# === CLV and Scoring ===

def clv_prob(p_close_novig: float, price_taken_american: float) -> float:
    """Closing Line Value in probability.

    No-vig closing probability of the side taken minus the break-even probability.
    Positive = beat the close, expected to be profitable.

    Args:
        p_close_novig: no-vig probability at close (for the side taken)
        price_taken_american: American odds when bet was placed

    Returns:
        CLV in probability (expected value)
    """
    return p_close_novig - break_even(price_taken_american)


def clv_points(line_taken: float, line_close: float, side: str) -> float:
    """Closing-line value in points; positive = beat the close.

    Spreads use each side's own handicap (the number added to that side's score, as in `settle`): home -3 taken,
    home closes -4.5 -> +1.5; away +3 taken, away closes +4.5 -> -1.5. So for either side CLV = taken - close.
    Totals: over 44 taken, closes 46 -> +2 (close - taken); under is the reverse (taken - close)."""
    if side in ("home", "away", "under"):
        return line_taken - line_close
    if side == "over":
        return line_close - line_taken
    return 0.0


def brier_vs_close(p_model: float, p_close_novig: float, outcome: bool) -> dict:
    """Brier score against closing line benchmark.

    Args:
        p_model: model's predicted probability
        p_close_novig: closing-line no-vig probability
        outcome: actual outcome (True/False)

    Returns:
        Dict with 'model_brier', 'close_brier', 'skill'
    """
    from .forecast import brier

    model_b = brier(p_model, outcome)
    close_b = brier(p_close_novig, outcome)
    skill_score = 1.0 - (model_b / close_b) if close_b > 0 else 0.0

    return {
        'model_brier': model_b,
        'close_brier': close_b,
        'skill': skill_score
    }


def settle(pick: dict, result: dict) -> dict:
    """Settle a single pick and compute profit.

    Args:
        pick: dict with keys market, side, line (None for h2h), price (american), stake
        result: dict with home_score, away_score

    Returns:
        Dict with outcome ("win", "loss", "push"), profit (in dollars)
    """
    home_score = result.get('home_score', 0)
    away_score = result.get('away_score', 0)

    market = pick.get('market', 'h2h')
    side = pick.get('side', 'home')
    line = pick.get('line')
    price = pick.get('price', 0)
    stake = pick.get('stake', 0)

    decimal = american_to_decimal(price)

    if market == 'h2h':
        # Moneyline: side is "home" or "away"
        if home_score == away_score:
            return {'outcome': 'push', 'profit': 0.0}
        if side == 'home':
            win = home_score > away_score
        else:
            win = away_score > home_score

        if win:
            profit = stake * (decimal - 1.0)
            return {'outcome': 'win', 'profit': profit}
        else:
            return {'outcome': 'loss', 'profit': -stake}

    elif market == 'spreads':
        # Spread: line is the points spread
        if line is None:
            return {'outcome': 'push', 'profit': 0.0}

        if side == 'home':
            # Home +line or -line
            adjusted_home = home_score + line
            if adjusted_home > away_score:
                profit = stake * (decimal - 1.0)
                return {'outcome': 'win', 'profit': profit}
            elif adjusted_home < away_score:
                return {'outcome': 'loss', 'profit': -stake}
            else:
                return {'outcome': 'push', 'profit': 0.0}
        else:
            # Away +line or -line
            adjusted_away = away_score + line
            if adjusted_away > home_score:
                profit = stake * (decimal - 1.0)
                return {'outcome': 'win', 'profit': profit}
            elif adjusted_away < home_score:
                return {'outcome': 'loss', 'profit': -stake}
            else:
                return {'outcome': 'push', 'profit': 0.0}

    elif market == 'totals':
        # Total: line is the total points
        total = home_score + away_score

        if side == 'over':
            if total > line:
                profit = stake * (decimal - 1.0)
                return {'outcome': 'win', 'profit': profit}
            elif total < line:
                return {'outcome': 'loss', 'profit': -stake}
            else:
                return {'outcome': 'push', 'profit': 0.0}
        else:  # 'under'
            if total < line:
                profit = stake * (decimal - 1.0)
                return {'outcome': 'win', 'profit': profit}
            elif total > line:
                return {'outcome': 'loss', 'profit': -stake}
            else:
                return {'outcome': 'push', 'profit': 0.0}

    return {'outcome': 'push', 'profit': 0.0}


def kelly_fraction(p: float, price_american: float) -> float:
    """Kelly fraction for optimal bet sizing.

    Kelly = (b*p - (1-p)) / b, where b = decimal - 1 (profit per unit staked)
    Floored at 0 (no negative Kelly bets).

    Args:
        p: win probability
        price_american: American odds

    Returns:
        Kelly fraction [0, 1], floored at 0
    """
    decimal = american_to_decimal(price_american)
    b = decimal - 1.0

    if b <= 0:
        return 0.0

    kelly = (b * p - (1.0 - p)) / b
    return max(0.0, kelly)


# === Bankroll tracking ===

def bankroll_path(weekly_profits: list[float], start: float = 1000.0) -> list[float]:
    """Cumulative bankroll path from weekly profits.

    Args:
        weekly_profits: list of weekly profit/loss (dollars)
        start: starting bankroll

    Returns:
        List of bankroll values (including starting value)
    """
    path = [start]
    for profit in weekly_profits:
        path.append(path[-1] + profit)
    return path


def log_growth(start: float, end: float) -> float:
    """Log growth (Kelly criterium benchmark).

    log(end / start); can be negative if bankroll shrinks.

    Args:
        start: starting value
        end: ending value

    Returns:
        Log growth
    """
    if start <= 0 or end <= 0:
        return 0.0
    return math.log(end / start)


def roi(start: float, end: float) -> float:
    """Return on investment: (end - start) / start.

    Args:
        start: starting bankroll
        end: ending bankroll

    Returns:
        ROI (e.g., 0.1 = 10%)
    """
    if start <= 0:
        return 0.0
    return (end - start) / start


def max_drawdown(path: list[float]) -> dict:
    """Maximum drawdown from peak, in dollars and percent.

    Args:
        path: list of bankroll values

    Returns:
        Dict with 'dollars' and 'pct'
    """
    if not path:
        return {'dollars': 0.0, 'pct': 0.0}

    max_so_far = path[0]
    max_dd_dollars = 0.0
    max_dd_pct = 0.0

    for val in path[1:]:
        dd = max_so_far - val
        if dd > max_dd_dollars:
            max_dd_dollars = dd
            max_dd_pct = dd / max_so_far if max_so_far > 0 else 0.0

        if val > max_so_far:
            max_so_far = val

    return {'dollars': max_dd_dollars, 'pct': max_dd_pct}


def busted(path: list[float], floor: float = 0.0) -> bool:
    """Whether bankroll ever fell below floor.

    Args:
        path: bankroll path
        floor: bankruptcy threshold (default 0)

    Returns:
        True if busted
    """
    return any(val <= floor for val in path) if path else False


def risk_of_ruin(stake_fractions: list[float], win_probs: list[float], prices: list[float],
                 n_sims: int = 2000, weeks: int = 17, seed: int = 0) -> float:
    """Simple bootstrap risk of ruin: simulate picking these bets weekly.

    Args:
        stake_fractions: bet sizing as fraction of bankroll
        win_probs: win probability for each bet
        prices: American odds for each bet
        n_sims: number of simulations
        weeks: number of weeks to simulate
        seed: random seed

    Returns:
        Fraction of simulations that busted
    """
    if not stake_fractions or len(stake_fractions) != len(win_probs) or len(win_probs) != len(prices):
        return 0.0

    rng = random.Random(seed)
    busted_count = 0

    for _ in range(n_sims):
        bankroll = 1000.0

        for week in range(weeks):
            for frac, p_win, price in zip(stake_fractions, win_probs, prices):
                stake = bankroll * frac
                decimal = american_to_decimal(price)

                if rng.random() < p_win:
                    bankroll += stake * (decimal - 1.0)
                else:
                    bankroll -= stake

            if bankroll <= 0:
                busted_count += 1
                break

    return busted_count / n_sims


# === D-PICKS-2 measures ===

def stake_edge_discipline(picks: list[dict]) -> dict:
    """Median stake as fraction of Kelly, and correlation of stake with edge.

    Edge = p_model - break_even probability.
    Uses Spearman rank correlation (implemented from scratch).

    Each pick dict must have: stake, p_model, price

    Returns:
        Dict with 'median_kelly_ratio', 'spearman_corr'
    """
    if not picks:
        return {'median_kelly_ratio': 0.0, 'spearman_corr': 0.0}

    stakes = []
    kelly_fractions = []
    edges = []

    for p in picks:
        stake = p.get('stake', 0)
        p_model = p.get('p_model', 0.5)
        price = p.get('price', 0)

        kelly = kelly_fraction(p_model, price)
        edge = p_model - break_even(price)

        if kelly > 0:
            stakes.append(stake)
            kelly_fractions.append(kelly)
            edges.append(edge)

    if not stakes or not kelly_fractions:
        return {'median_kelly_ratio': 0.0, 'spearman_corr': 0.0}

    kelly_ratios = [s / k if k > 0 else 0 for s, k in zip(stakes, kelly_fractions)]
    kelly_ratios.sort()
    median_ratio = kelly_ratios[len(kelly_ratios) // 2]

    # Spearman: rank correlation of stakes and edges
    def spearman_rank(x, y):
        if len(x) != len(y) or len(x) < 2:
            return 0.0

        # Rank both lists
        ranked_x = [sorted(x).index(val) + 1 for val in x]
        ranked_y = [sorted(y).index(val) + 1 for val in y]

        # Pearson correlation of ranks
        mean_rx = mean(ranked_x)
        mean_ry = mean(ranked_y)

        cov = sum((rx - mean_rx) * (ry - mean_ry) for rx, ry in zip(ranked_x, ranked_y)) / len(x)
        sd_x = (sum((rx - mean_rx) ** 2 for rx in ranked_x) / len(x)) ** 0.5
        sd_y = (sum((ry - mean_ry) ** 2 for ry in ranked_y) / len(y)) ** 0.5

        if sd_x == 0 or sd_y == 0:
            return 0.0

        return cov / (sd_x * sd_y)

    spear = spearman_rank(stakes, edges)

    return {'median_kelly_ratio': median_ratio, 'spearman_corr': spear}


def ev_at_close(picks: list[dict]) -> float:
    """Sum of stake * (p_close_novig - break_even).

    Expected value if closing line is the true probability.

    Each pick dict must have: stake, p_close_novig, price

    Returns:
        Total EV in dollars
    """
    total_ev = 0.0

    for p in picks:
        stake = p.get('stake', 0)
        p_close = p.get('p_close_novig', 0.5)
        price = p.get('price', 0)

        be = break_even(price)
        clv = p_close - be
        total_ev += stake * clv

    return total_ev


def risk_adjusted_return(picks: list[dict], weekly_profits: dict[int, float]) -> dict:
    """Mean weekly return / SD, plus log growth.

    Args:
        picks: list of pick dicts (for reference)
        weekly_profits: dict mapping week -> profit

    Returns:
        Dict with 'sharpe', 'log_growth'
    """
    if not weekly_profits:
        return {'sharpe': 0.0, 'log_growth': 0.0}

    profits = list(weekly_profits.values())

    if not profits or len(profits) < 2:
        return {'sharpe': 0.0, 'log_growth': 0.0}

    avg = mean(profits)
    sd = stdev(profits) if len(profits) > 1 else 0.0

    sharpe = avg / sd if sd > 0 else 0.0

    # Log growth: start=1000, end=1000+sum(profits)
    total_profit = sum(profits)
    end = 1000.0 + total_profit
    lg = log_growth(1000.0, end) if end > 0 else 0.0

    return {'sharpe': sharpe, 'log_growth': lg}


def concentration(picks: list[dict]) -> dict:
    """Stake concentration: Herfindahl index, largest stake share, pick count, pass rate.

    Each pick dict must have: week, stake, outcome

    Returns:
        Dict with 'herfindahl_mean', 'max_stake_pct', 'n_picks', 'pass_rate'
    """
    if not picks:
        return {'herfindahl_mean': 0.0, 'max_stake_pct': 0.0, 'n_picks': 0, 'pass_rate': 0.0}

    # Group by week
    by_week = {}
    for p in picks:
        w = p.get('week')
        if w is not None:
            if w not in by_week:
                by_week[w] = []
            by_week[w].append(p.get('stake', 0))

    herfindahls = []
    for week, stakes in by_week.items():
        total = sum(stakes)
        if total > 0:
            h = sum((s / total) ** 2 for s in stakes)
            herfindahls.append(h)
            max_pct = max(stakes) / total if stakes else 0.0

    herfindahl_mean = mean(herfindahls) if herfindahls else 0.0
    max_stake_pct = max([max(stakes) / sum(stakes) if sum(stakes) > 0 else 0.0 for stakes in by_week.values()]) if by_week else 0.0
    n_picks = len(picks)

    # Pass rate: share of picks with positive outcome
    wins = sum(1 for p in picks if p.get('outcome') == 'win')
    pass_rate = wins / n_picks if n_picks > 0 else 0.0

    return {
        'herfindahl_mean': herfindahl_mean,
        'max_stake_pct': max_stake_pct,
        'n_picks': n_picks,
        'pass_rate': pass_rate
    }


def hedging(picks: list[dict]) -> dict:
    """Per-game hedging: share of gross exposure hedged, and same-game stacking.

    Net exposure: home/over = +, away/under = -.
    Hedging = 1 - |net| / gross per game.

    Each pick dict must have: game_id, market, side, stake, outcome

    Returns:
        Dict with 'mean_hedging_ratio', 'stacked_games'
    """
    if not picks:
        return {'mean_hedging_ratio': 0.0, 'stacked_games': 0}

    # Group by game
    by_game = {}
    for p in picks:
        g = p.get('game_id')
        if g is not None:
            if g not in by_game:
                by_game[g] = {'h2h': [], 'spreads': [], 'totals': []}

            market = p.get('market', 'h2h')
            side = p.get('side', 'home')
            stake = p.get('stake', 0)

            # Sign: home/over = +1, away/under = -1
            sign = 1.0 if side in ('home', 'over') else -1.0
            by_game[g][market].append((sign, stake))

    hedging_ratios = []
    stacked = 0

    for game, markets in by_game.items():
        # For each market, compute net exposure
        net_h2h = sum(s * st for s, st in markets['h2h'])
        net_spreads = sum(s * st for s, st in markets['spreads'])
        net_totals = sum(s * st for s, st in markets['totals'])

        # Gross exposure per market
        gross_h2h = sum(st for _, st in markets['h2h'])
        gross_spreads = sum(st for _, st in markets['spreads'])
        gross_totals = sum(st for _, st in markets['totals'])

        # Total gross and net per game
        total_gross = gross_h2h + gross_spreads + gross_totals
        total_net = abs(net_h2h) + abs(net_spreads) + abs(net_totals)

        if total_gross > 0:
            hedging = 1.0 - (total_net / total_gross)
            hedging_ratios.append(max(0.0, hedging))

        # Same-game stacking: correlated sides in the same game
        # E.g., home spread and home h2h; over and over spread
        if (net_h2h > 0 and net_spreads > 0) or (net_h2h < 0 and net_spreads < 0):
            stacked += 1

    mean_hedge = mean(hedging_ratios) if hedging_ratios else 0.0

    return {'mean_hedging_ratio': mean_hedge, 'stacked_games': stacked}


def herding_across_models(picks: list[dict]) -> dict:
    """Pairwise Pearson correlation of signed stakes across models on same (game, market).

    Effective number of independent models.
    If no numpy, use n_eff = n / (1 + (n-1) * mean_pairwise_corr).

    Each pick dict must have: model, game_id, market, side, stake

    Returns:
        Dict with 'mean_corr', 'n_eff', 'consensus_pct'
    """
    if not picks:
        return {'mean_corr': 0.0, 'n_eff': 0.0, 'consensus_pct': 0.0}

    # Group by (game, market)
    by_market = {}
    models_seen = set()

    for p in picks:
        model = p.get('model')
        game = p.get('game_id')
        market = p.get('market', 'h2h')
        side = p.get('side', 'home')
        stake = p.get('stake', 0)

        if model and game:
            models_seen.add(model)
            key = (game, market)
            if key not in by_market:
                by_market[key] = {}

            sign = 1.0 if side in ('home', 'over') else -1.0
            by_market[key][model] = sign * stake

    n_models = len(models_seen)
    if n_models < 2:
        return {'mean_corr': 0.0, 'n_eff': 1.0, 'consensus_pct': 0.0}

    # Pairwise correlations
    models = list(models_seen)
    correlations = []

    for i, m1 in enumerate(models):
        for m2 in models[i+1:]:
            stakes_m1 = []
            stakes_m2 = []

            for key, model_stakes in by_market.items():
                if m1 in model_stakes and m2 in model_stakes:
                    stakes_m1.append(model_stakes[m1])
                    stakes_m2.append(model_stakes[m2])

            if len(stakes_m1) > 1:
                # Pearson correlation
                mean_m1 = mean(stakes_m1)
                mean_m2 = mean(stakes_m2)

                cov = sum((s1 - mean_m1) * (s2 - mean_m2) for s1, s2 in zip(stakes_m1, stakes_m2)) / len(stakes_m1)
                sd_m1 = (sum((s - mean_m1) ** 2 for s in stakes_m1) / len(stakes_m1)) ** 0.5
                sd_m2 = (sum((s - mean_m2) ** 2 for s in stakes_m2) / len(stakes_m2)) ** 0.5

                if sd_m1 > 0 and sd_m2 > 0:
                    corr = cov / (sd_m1 * sd_m2)
                    correlations.append(corr)

    mean_corr = mean(correlations) if correlations else 0.0

    # Effective number of independent models
    if n_models == 1:
        n_eff = 1.0
    else:
        n_eff = n_models / (1.0 + (n_models - 1) * mean_corr) if mean_corr < 1 else 1.0

    # Consensus: share on majority side (by median model stake)
    consensus_votes = {}
    for key, model_stakes in by_market.items():
        if len(model_stakes) >= n_models // 2:  # At least half the models have a pick
            majority_sign = 1.0 if sum(model_stakes.values()) >= 0 else -1.0
            consensus_votes[key] = majority_sign

    total_picks = len([p for p in picks if p.get('model')])
    consensus_picks = sum(1 for p in picks if p.get('model') and (p.get('game_id'), p.get('market')) in consensus_votes
                         and (1.0 if p.get('side') in ('home', 'over') else -1.0) == consensus_votes.get((p.get('game_id'), p.get('market'))))

    consensus_pct = consensus_picks / total_picks if total_picks > 0 else 0.0

    return {'mean_corr': mean_corr, 'n_eff': n_eff, 'consensus_pct': consensus_pct}


def herding_with_market(picks: list[dict]) -> dict:
    """Correlation of signed stakes with line movement and market lean.

    Favourite lean: share of picks on favourite (negative spread, lower h2h odds).
    Over lean: share on over.

    Each pick dict must have: game_id, market, side, stake, line_open, line_at_pick, line_close, line

    Returns:
        Dict with 'corr_open_pick', 'corr_pick_close', 'fav_lean_pct', 'over_lean_pct'
    """
    if not picks:
        return {'corr_open_pick': 0.0, 'corr_pick_close': 0.0, 'fav_lean_pct': 0.0, 'over_lean_pct': 0.0}

    movements_open_pick = []
    movements_pick_close = []
    signed_stakes = []

    fav_count = 0
    over_count = 0

    for p in picks:
        line_open = p.get('line_open')
        line_at_pick = p.get('line_at_pick')
        line_close = p.get('line_close')
        market = p.get('market')
        side = p.get('side')
        stake = p.get('stake', 0)

        sign = 1.0 if side in ('home', 'over') else -1.0
        signed_stakes.append(sign * stake)

        if line_open is not None and line_at_pick is not None:
            move_op = line_at_pick - line_open
            movements_open_pick.append(move_op)

        if line_at_pick is not None and line_close is not None:
            move_pc = line_close - line_at_pick
            movements_pick_close.append(move_pc)

        # Favourite lean: negative line for spreads means favourite
        line = p.get('line')
        if market == 'spreads' and line is not None:
            if (line < 0 and side == 'home') or (line > 0 and side == 'away'):
                fav_count += 1

        # Over lean
        if market == 'totals' and side == 'over':
            over_count += 1

    n_picks = len(picks)
    fav_lean_pct = fav_count / n_picks if n_picks > 0 else 0.0
    over_lean_pct = over_count / n_picks if n_picks > 0 else 0.0

    # Correlations
    def pearson(x, y):
        if len(x) != len(y) or len(x) < 2:
            return 0.0

        mean_x = mean(x)
        mean_y = mean(y)

        cov = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y)) / len(x)
        sd_x = (sum((xi - mean_x) ** 2 for xi in x) / len(x)) ** 0.5
        sd_y = (sum((yi - mean_y) ** 2 for yi in y) / len(y)) ** 0.5

        if sd_x == 0 or sd_y == 0:
            return 0.0

        return cov / (sd_x * sd_y)

    corr_op = pearson(signed_stakes[:len(movements_open_pick)], movements_open_pick) if movements_open_pick else 0.0
    corr_pc = pearson(signed_stakes[:len(movements_pick_close)], movements_pick_close) if movements_pick_close else 0.0

    return {
        'corr_open_pick': corr_op,
        'corr_pick_close': corr_pc,
        'fav_lean_pct': fav_lean_pct,
        'over_lean_pct': over_lean_pct
    }


def style_descriptors(picks: list[dict]) -> dict:
    """Betting style: favourite/underdog lean, over/under lean, dispersion, contrarian index, revision rate.

    Each pick dict must have: market, side, line, stake, timing ("early"/"prelock")

    Returns:
        Dict with style metrics
    """
    if not picks:
        return {
            'fav_lean': 0.0,
            'over_lean': 0.0,
            'stake_dispersion': 0.0,
            'contrarian_index': 0.0,
            'revision_rate': 0.0
        }

    fav_count = 0
    over_count = 0
    stakes = []
    contrarian_count = 0

    early_picks = {}
    prelock_picks = {}

    for p in picks:
        stakes.append(p.get('stake', 0))
        market = p.get('market')
        side = p.get('side')
        line = p.get('line')
        timing = p.get('timing', 'early')

        # Favourite lean (negative line = favourite)
        if market == 'spreads' and line is not None:
            if (line < 0 and side == 'home') or (line > 0 and side == 'away'):
                fav_count += 1

        # Over lean
        if market == 'totals' and side == 'over':
            over_count += 1

        # Contrarian: picking underdog or under
        if market == 'spreads' and line is not None:
            if (line > 0 and side == 'home') or (line < 0 and side == 'away'):
                contrarian_count += 1
        if market == 'totals' and side == 'under':
            contrarian_count += 1

        # Revision tracking
        key = (p.get('game_id'), market, side)
        if timing == 'early':
            early_picks[key] = p
        elif timing == 'prelock':
            prelock_picks[key] = p

    n = len(picks)
    fav_lean = fav_count / n if n > 0 else 0.0
    over_lean = over_count / n if n > 0 else 0.0

    # Stake dispersion: SD / mean
    stake_dispersion = 0.0
    if stakes and len(stakes) > 1:
        mean_stake = mean(stakes)
        if mean_stake > 0:
            sd_stake = stdev(stakes) if len(stakes) > 1 else 0.0
            stake_dispersion = sd_stake / mean_stake

    # Contrarian index: share that contradict the market
    contrarian_index = contrarian_count / n if n > 0 else 0.0

    # Revision rate: share of picks changed from early to prelock
    revised = sum(1 for key in early_picks if key in prelock_picks and early_picks[key].get('stake') != prelock_picks[key].get('stake'))
    revision_rate = revised / len(early_picks) if early_picks else 0.0

    return {
        'fav_lean': fav_lean,
        'over_lean': over_lean,
        'stake_dispersion': stake_dispersion,
        'contrarian_index': contrarian_index,
        'revision_rate': revision_rate
    }


def pooled_ensemble(probs_by_instance: list[list[float]]) -> float:
    """Mean probability across instances.

    For 3 instances, average their forecast probabilities.

    Args:
        probs_by_instance: list of probability lists (one per instance)

    Returns:
        Pooled (mean) probability
    """
    if not probs_by_instance:
        return 0.5

    all_probs = []
    for probs in probs_by_instance:
        if probs:
            all_probs.extend(probs)

    return mean(all_probs) if all_probs else 0.5


def ensemble_clv(picks_by_instance: list[list[dict]]) -> dict:
    """Ensemble CLV: ranked headline CLV for pooled ensemble.

    Args:
        picks_by_instance: list of pick lists (one per instance)

    Returns:
        Dict with ensemble metrics
    """
    if not picks_by_instance or not picks_by_instance[0]:
        return {'ensemble_clv': 0.0}

    # Pool CLVs
    all_clvs = []
    for picks in picks_by_instance:
        for p in picks:
            p_close = p.get('p_close_novig', 0.5)
            price = p.get('price', 0)
            be = break_even(price)
            clv = p_close - be
            all_clvs.append(clv)

    ensemble_clv = mean(all_clvs) if all_clvs else 0.0

    return {'ensemble_clv': ensemble_clv}
