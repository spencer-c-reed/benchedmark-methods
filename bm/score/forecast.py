"""Forecast scoring: CRPS, Brier, log score, skill, calibration, encompassing regression, bootstrap."""
from __future__ import annotations

import math
import random
from statistics import mean, stdev
from typing import Callable


def crps_quantiles(quantiles: dict[float, float], y: float) -> float:
    """CRPS approximated from quantile forecasts via pinball (quantile) loss.

    CRPS ≈ 2 * mean over levels τ of pinball_τ(q_τ, y), where
    pinball_τ(q, y) = τ * max(y - q, 0) + (1 - τ) * max(q - y, 0).

    Approximation: integrating the CDF built from quantiles gives the exact CRPS
    when the quantiles span sufficient coverage; here we sum across quantile levels.

    Args:
        quantiles: mapping of level (0.1, 0.25, 0.5, 0.75, 0.9) -> forecast value
        y: observed value

    Returns:
        CRPS score (lower is better)
    """
    if not quantiles:
        return float('inf')

    total = 0.0
    for level, q in quantiles.items():
        # Pinball loss at level τ
        residual = y - q
        if residual >= 0:
            pinball = level * residual
        else:
            pinball = (level - 1) * residual
        total += pinball

    return 2.0 * (total / len(quantiles))


def crps_normal(mu: float, sigma: float, y: float) -> float:
    """CRPS for a normal distribution in closed form.

    CRPS = σ * (z * (2 * Φ(z) - 1) + 2 * φ(z) - 1/√π),
    where z = (y - μ) / σ, Φ is CDF, φ is PDF of standard normal.

    Args:
        mu: mean
        sigma: standard deviation (must be > 0)
        y: observed value

    Returns:
        CRPS score (lower is better)
    """
    if sigma <= 0:
        return float('inf')

    z = (y - mu) / sigma

    # Standard normal CDF and PDF
    # Using math.erf: Φ(z) = 0.5 * (1 + erf(z / √2))
    # φ(z) = exp(-z²/2) / √(2π)

    cdf_z = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
    pdf_z = math.exp(-0.5 * z * z) / math.sqrt(2.0 * math.pi)

    term1 = z * (2.0 * cdf_z - 1.0)
    term2 = 2.0 * pdf_z
    term3 = 1.0 / math.sqrt(math.pi)

    return sigma * (term1 + term2 - term3)


def brier(p: float, outcome: bool) -> float:
    """Brier score: mean squared error of forecast probability.

    Brier = (p - outcome)², where outcome is 0 (False) or 1 (True).
    Lower is better. Range: [0, 1].

    Args:
        p: predicted probability [0, 1]
        outcome: actual outcome (True/False or 1/0)

    Returns:
        Brier score
    """
    y = 1.0 if outcome else 0.0
    return (p - y) ** 2


def log_score(p: float, outcome: bool, eps: float = 1e-6) -> float:
    """Negative log likelihood (log score): -log(p) if outcome, -log(1-p) if not.

    Lower is better. Heavily penalizes confident wrong predictions.

    Args:
        p: predicted probability [0, 1]
        outcome: actual outcome (True/False or 1/0)
        eps: small value to clamp p away from 0 or 1

    Returns:
        Log score (negative log likelihood)
    """
    p = max(eps, min(1.0 - eps, p))
    if outcome:
        return -math.log(p)
    else:
        return -math.log(1.0 - p)


def skill(model_scores: list[float], market_scores: list[float]) -> float | None:
    """Skill score: 1 - mean(model) / mean(market).

    Positive = better than market. Assumes lower score is better (like Brier, log score).

    Args:
        model_scores: model scores on same items
        market_scores: market/baseline scores on same items

    Returns:
        Skill score
    """
    if not model_scores or len(model_scores) != len(market_scores):
        return None

    market_mean = mean(market_scores)
    if market_mean == 0:
        return None

    model_mean = mean(model_scores)
    return 1.0 - (model_mean / market_mean)


def market_quantiles(point: float, sd: float, levels: tuple = (0.1, 0.25, 0.5, 0.75, 0.9)) -> dict[float, float]:
    """Build market baseline forecast as quantiles from a normal distribution.

    Args:
        point: market point estimate (median, typically)
        sd: historical error standard deviation
        levels: quantile levels to return

    Returns:
        Dict mapping level -> quantile value
    """
    from statistics import NormalDist

    if not math.isfinite(point) or not math.isfinite(sd) or sd <= 0:
        raise ValueError("market mean and positive sd must be finite")
    if any(not 0 < level < 1 for level in levels):
        raise ValueError("quantile levels must be strictly between 0 and 1")
    normal = NormalDist(mu=point, sigma=sd)
    return {level: normal.inv_cdf(level) for level in levels}


def calibration_bins(probs: list[float], outcomes: list[bool], n_bins: int = 10) -> list[dict]:
    """Calibration: group forecasts by probability bin and compute observed frequency.

    Returns:
        List of dicts {bin_lo, bin_hi, n, mean_p, freq}: bin bounds, count, mean forecast prob, observed frequency
    """
    if not probs or len(probs) != len(outcomes):
        return []

    bins = []
    for i in range(n_bins):
        lo = i / n_bins
        hi = (i + 1) / n_bins

        in_bin = [(p, o) for p, o in zip(probs, outcomes) if lo <= p < hi or (i == n_bins - 1 and p == hi)]

        if in_bin:
            mean_p = mean(p for p, _ in in_bin)
            freq = mean(1.0 if o else 0.0 for _, o in in_bin)
            bins.append({
                'bin_lo': lo,
                'bin_hi': hi,
                'n': len(in_bin),
                'mean_p': mean_p,
                'freq': freq
            })

    return bins


def coverage(quantile_forecasts: list[dict[float, float]], ys: list[float], lo: float = 0.1, hi: float = 0.9) -> float | None:
    """Coverage: share of outcomes falling between lo and hi quantile forecasts.

    Should be approximately (hi - lo) [e.g., 0.8 for lo=0.1, hi=0.9].

    Args:
        quantile_forecasts: list of quantile dicts {level -> value}
        ys: observed values
        lo, hi: quantile levels for interval

    Returns:
        Share of outcomes in [q_lo, q_hi]
    """
    if not quantile_forecasts or len(quantile_forecasts) != len(ys):
        return None
    if any(q.get(lo) is None or q.get(hi) is None for q in quantile_forecasts):
        return None

    covered = 0
    for qf, y in zip(quantile_forecasts, ys):
        q_lo = qf.get(lo)
        q_hi = qf.get(hi)
        if q_lo is not None and q_hi is not None and q_lo <= y <= q_hi:
            covered += 1

    return covered / len(ys) if ys else 0.0


def pit_values(cdf_forecasts: list[Callable[[float], float]], ys: list[float]) -> list[float]:
    """Probability integral transform: CDF value at observed outcome.

    For a well-calibrated forecast, PIT should be uniform on [0, 1].

    Args:
        cdf_forecasts: list of CDF functions
        ys: observed values

    Returns:
        List of PIT values
    """
    return [cdf(y) for cdf, y in zip(cdf_forecasts, ys)]


def encompassing(y: list[float], market: list[float], model: list[float]) -> dict:
    """Forecast-encompassing regression: OLS of y on [1, market, model].

    Tests whether the model adds information beyond the market forecast.
    Closed-form 3x3 normal equations (no numpy).

    Returns:
        Dict with 'coef' [intercept, market_coef, model_coef], 'se' [std errors], 'n'
    """
    if len(y) != len(market) or len(y) != len(model):
        return {'coef': [None] * 3, 'se': [None] * 3, 'n': 0, 'status': 'unavailable'}

    n = len(y)
    if n < 3:
        return {'coef': [None] * 3, 'se': [None] * 3, 'n': n, 'status': 'unavailable'}

    # Build design matrix X = [1, market, model] (n x 3)
    # Normal equations: X'X β = X'y

    sum_y = sum(y)
    sum_m = sum(market)
    sum_mo = sum(model)
    sum_y2 = sum(yi * yi for yi in y)
    sum_m2 = sum(mi * mi for mi in market)
    sum_mo2 = sum(mi * mi for mi in model)
    sum_ym = sum(yi * mi for yi, mi in zip(y, market))
    sum_ymo = sum(yi * mi for yi, mi in zip(y, model))
    sum_m_mo = sum(mi * mi2 for mi, mi2 in zip(market, model))

    # X'X is 3x3, X'y is 3x1
    xtx = [
        [n, sum_m, sum_mo],
        [sum_m, sum_m2, sum_m_mo],
        [sum_mo, sum_m_mo, sum_mo2]
    ]
    xty = [sum_y, sum_ym, sum_ymo]
    determinant = (xtx[0][0] * (xtx[1][1] * xtx[2][2] - xtx[1][2] ** 2)
                   - xtx[0][1] * (xtx[0][1] * xtx[2][2] - xtx[1][2] * xtx[0][2])
                   + xtx[0][2] * (xtx[0][1] * xtx[1][2] - xtx[1][1] * xtx[0][2]))
    scale = max(abs(xtx[0][0] * xtx[1][1] * xtx[2][2]), 1.0)
    if n <= 3 or abs(determinant) <= 1e-12 * scale:
        return {'coef': [None] * 3, 'se': [None] * 3, 'n': n,
                'status': 'unavailable', 'reason': 'singular design or no residual degrees of freedom'}

    # Solve 3x3 system by Gaussian elimination
    def solve_3x3(A, b):
        """Solve Ax = b for 3x3 A."""
        # Make a copy
        A = [row[:] for row in A]
        b = b[:]

        # Forward elimination
        for i in range(3):
            # Find pivot
            max_row = i
            for k in range(i + 1, 3):
                if abs(A[k][i]) > abs(A[max_row][i]):
                    max_row = k
            A[i], A[max_row] = A[max_row], A[i]
            b[i], b[max_row] = b[max_row], b[i]

            # Make all rows below this one 0 in current column
            if abs(A[i][i]) < 1e-12:
                continue
            for k in range(i + 1, 3):
                c = A[k][i] / A[i][i]
                for j in range(i, 3):
                    A[k][j] -= c * A[i][j]
                b[k] -= c * b[i]

        # Back substitution
        x = [0, 0, 0]
        for i in range(2, -1, -1):
            x[i] = b[i]
            for j in range(i + 1, 3):
                x[i] -= A[i][j] * x[j]
            if abs(A[i][i]) > 1e-12:
                x[i] /= A[i][i]

        return x

    coef = solve_3x3(xtx, xty)

    # Residuals and error variance
    residuals = [y[i] - (coef[0] + coef[1] * market[i] + coef[2] * model[i]) for i in range(n)]
    sse = sum(r * r for r in residuals)
    mse = sse / (n - 3) if n > 3 else 0.0

    # Standard errors: sqrt(diag(mse * (X'X)^{-1}))
    # Invert X'X using cofactor formula for 3x3
    def invert_3x3(A):
        det = (A[0][0] * (A[1][1] * A[2][2] - A[1][2] * A[2][1]) -
               A[0][1] * (A[1][0] * A[2][2] - A[1][2] * A[2][0]) +
               A[0][2] * (A[1][0] * A[2][1] - A[1][1] * A[2][0]))
        if abs(det) < 1e-12:
            return [[0, 0, 0], [0, 0, 0], [0, 0, 0]]

        inv = [
            [(A[1][1] * A[2][2] - A[1][2] * A[2][1]) / det,
             (A[0][2] * A[2][1] - A[0][1] * A[2][2]) / det,
             (A[0][1] * A[1][2] - A[0][2] * A[1][1]) / det],
            [(A[1][2] * A[2][0] - A[1][0] * A[2][2]) / det,
             (A[0][0] * A[2][2] - A[0][2] * A[2][0]) / det,
             (A[0][2] * A[1][0] - A[0][0] * A[1][2]) / det],
            [(A[1][0] * A[2][1] - A[1][1] * A[2][0]) / det,
             (A[0][1] * A[2][0] - A[0][0] * A[2][1]) / det,
             (A[0][0] * A[1][1] - A[0][1] * A[1][0]) / det]
        ]
        return inv

    xtx_inv = invert_3x3(xtx)
    se = [math.sqrt(max(0, mse * xtx_inv[i][i])) for i in range(3)]

    return {'coef': coef, 'se': se, 'n': n}


def bootstrap_ci(values: list[float], stat: Callable[[list], float] = mean, n: int = 2000,
                 alpha: float = 0.1, seed: int = 0, clusters: list | None = None) -> tuple[float, float]:
    """Percentile bootstrap confidence interval.

    If clusters are given, resample clusters (block bootstrap by week/league).

    Args:
        values: data points
        stat: statistic to compute (default: mean)
        n: number of bootstrap resamples
        alpha: significance level (returns 100*(1-alpha)% CI)
        seed: random seed for reproducibility
        clusters: optional list of cluster assignments (e.g., week IDs)

    Returns:
        (lower, upper) confidence interval bounds
    """
    if n < 1 or not 0 < alpha < 1:
        raise ValueError("positive resamples and 0 < alpha < 1 required")
    if clusters is not None and len(clusters) != len(values):
        raise ValueError("each observation requires a cluster")
    if len(values) < 2 or (clusters is not None and len(set(clusters)) < 2):
        return (None, None)

    rng = random.Random(seed)
    boot_stats = []

    if clusters is not None and len(clusters) == len(values):
        # Cluster bootstrap: resample clusters, then all values in that cluster
        unique_clusters = sorted(set(clusters), key=lambda c: (type(c).__name__, repr(c)))
        for _ in range(n):
            sample_indices = []
            for _ in range(len(unique_clusters)):
                c = rng.choice(unique_clusters)
                sample_indices.extend([i for i, cl in enumerate(clusters) if cl == c])

            sample = [values[i] for i in sample_indices]
            if sample:
                boot_stats.append(stat(sample))
    else:
        # Regular bootstrap: resample values with replacement
        for _ in range(n):
            sample = [rng.choice(values) for _ in range(len(values))]
            boot_stats.append(stat(sample))

    if not boot_stats:
        return (None, None)

    boot_stats.sort()
    idx_lo = int(alpha / 2 * len(boot_stats))
    idx_hi = int((1.0 - alpha / 2) * len(boot_stats))

    return (boot_stats[idx_lo], boot_stats[idx_hi])


def skill_with_ci(model_scores: list[float], market_scores: list[float], clusters: list | None = None,
                  n: int = 2000, alpha: float = 0.1, seed: int = 0) -> dict:
    """Skill score with cluster-bootstrap 90% confidence interval.

    Args:
        model_scores: model scores
        market_scores: market scores
        clusters: optional cluster assignments (e.g., week IDs) for pairing
        n: bootstrap resamples
        alpha: significance level
        seed: random seed

    Returns:
        Dict with 'skill', 'ci_lower', 'ci_upper'
    """
    if n < 1 or not 0 < alpha < 1:
        raise ValueError("positive resamples and 0 < alpha < 1 required")
    if clusters is not None and len(clusters) != len(model_scores):
        raise ValueError("each observation requires a cluster")
    s = skill(model_scores, market_scores)
    if s is None or len(model_scores) < 2 or (clusters is not None and len(set(clusters)) < 2):
        return {'skill': s, 'ci_lower': None, 'ci_upper': None,
                'ci_status': 'unavailable: missing paired data, undefined baseline or fewer than two units'}

    # Bootstrap paired samples
    rng = random.Random(seed)
    boot_skills = []
    indices = list(range(len(model_scores)))

    if clusters is not None and len(clusters) == len(model_scores):
        # Resample clusters
        unique_clusters = sorted(set(clusters), key=lambda c: (type(c).__name__, repr(c)))
        for _ in range(n):
            sample_idx = []
            for _ in range(len(unique_clusters)):
                c = rng.choice(unique_clusters)
                sample_idx.extend([i for i, cl in enumerate(clusters) if cl == c])

            model_sample = [model_scores[i] for i in sample_idx]
            market_sample = [market_scores[i] for i in sample_idx]
            boot_skills.append(skill(model_sample, market_sample))
    else:
        # Regular bootstrap paired
        for _ in range(n):
            sample_idx = [rng.choice(indices) for _ in range(len(model_scores))]
            model_sample = [model_scores[i] for i in sample_idx]
            market_sample = [market_scores[i] for i in sample_idx]
            boot_skills.append(skill(model_sample, market_sample))

    if not boot_skills or any(x is None for x in boot_skills):
        return {'skill': s, 'ci_lower': None, 'ci_upper': None, 'ci_status': 'undefined bootstrap ratio'}

    boot_skills.sort()
    idx_lo = int(alpha / 2 * len(boot_skills))
    idx_hi = int((1.0 - alpha / 2) * len(boot_skills))

    return {
        'skill': s,
        'ci_lower': boot_skills[idx_lo],
        'ci_upper': boot_skills[idx_hi]
    }


def timing(first_version_scores: list[float], last_version_scores: list[float]) -> dict:
    """Accurate-early vs accurate-late summary.

    Compares forecast skill between an early version and a later version.

    Args:
        first_version_scores: scores from early version
        last_version_scores: scores from late version

    Returns:
        Dict with 'early_mean', 'late_mean', 'improvement'
    """
    if not first_version_scores or len(first_version_scores) != len(last_version_scores):
        return {'early_mean': None, 'late_mean': None, 'improvement': None}

    early_mean = mean(first_version_scores)
    late_mean = mean(last_version_scores)

    return {
        'early_mean': early_mean,
        'late_mean': late_mean,
        'improvement': late_mean - early_mean  # negative = improvement (lower score is better)
    }


def copying_index(model_points: list[float], market_points: list[float]) -> dict:
    """Forecast similarity to market: MAD and share within 0.5 pts.

    Args:
        model_points: model point forecasts
        market_points: market/consensus point forecasts

    Returns:
        Dict with 'mad' (mean absolute deviation) and 'pct_close' (% within 0.5 pts)
    """
    if not model_points or len(model_points) != len(market_points):
        return {'mad': None, 'pct_close': None}

    diffs = [abs(m - mk) for m, mk in zip(model_points, market_points)]
    mad = mean(diffs)
    close = sum(1 for d in diffs if d <= 0.5)
    pct_close = close / len(diffs) if diffs else 0.0

    return {'mad': mad, 'pct_close': pct_close}
