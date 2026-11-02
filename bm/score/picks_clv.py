"""Picks CLV toward the model's decision-time disagreement: the PRIMARY Picks endpoint. Outcome Brier is secondary.

For each model x instance x game x market (h2h, spread, total), at the same proposition (the reference book's posted
spread/total at the slate's as_of; the home side for h2h and spread, the over for total):
  deviation = model probability - decision-time no-vig market probability
  move      = close no-vig probability - decision-time no-vig market probability
Per model, the slope of move on deviation (OLS with intercept, standard errors clustered by game) asks: does the
closing line move toward where the model departed from the market? Slope 0 = no information beyond the market; 1 = the
market moved all the way to the model. Pooled over models too. Reported per slate and per season.

Market probabilities are the median of per-book proportional no-vig two-side probabilities over books quoting exactly
that threshold (bm.picks.lines consensus inputs), else the reference book's own no-vig price. When no close book quotes
the decision-time threshold, the close is translated from the close consensus line with a normal margin/total model
(SIGMA points) and marked `translated`; exact-only results are reported alongside.

Model probability by step: `line` (primary) is the model's explicit P(home wins), P(home covers the posted spread),
P(over the posted total) asked at the decision-time line; `blind` uses its P(home wins) and, for spread and total, the
normal implied by its own p10/p50/p90 at the posted threshold (it never saw the line).

Robustness close (h2h only): Kalshi KXNFLGAME and Polymarket game moneyline mids from data/markets (the T1 archive),
newest row at or before kickoff. Never reads data/markets/_settlement.

Secondary: outcome Brier difference (model minus decision-time market; pushes and ties excluded), game-clustered.
Only own answers with a verified prospective receipt count (bm.picks.run.receipt_status). Missing-answer rule: every
seat of the slate's frozen cohort is expected on every game x market of both steps; a missed or unverified answer is
scored at the decision-time market price (zero deviation, zero Brier difference), so not answering never improves a
score; answer rates and answered-only slopes are reported beside the headline.

  python3 -m bm.score.picks_clv [--slate 2026-w6-early ...] [--z 1.96]
"""
from __future__ import annotations

import gzip
import json
import math
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MARKETS = ("h2h", "spreads", "totals")
SIGMA = {"spreads": 13.5, "totals": 13.5}     # NFL margin / total SD (points) for threshold translation
KALSHI_CODE = {"JAX": "JAC", "LA": "LAR"}       # nflverse -> Kalshi team codes where they differ
MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


def _phi(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _phi_inv(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    lo, hi = -10.0, 10.0
    for _ in range(80):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if _phi(mid) < p else (lo, mid)
    return (lo + hi) / 2


# ------------------------------------------------------------------ market probabilities


def _ref_threshold(game: dict, kind: str) -> float | None:
    side = "home" if kind == "spreads" else "over"
    return ((game.get("ref") or {}).get(kind, {}).get(side) or {}).get("point")


def market_prob(game: dict, kind: str, threshold: float | None) -> tuple[float | None, str]:
    """No-vig P(home wins | home covers threshold | over threshold) at exactly `threshold`: median over books quoting it,
    else the reference book's own price pair (h2h: the consensus)."""
    if kind == "h2h":
        p = (game.get("consensus") or {}).get("p_home")
        return (p, "consensus") if p is not None else (None, "missing")
    inputs = ((game.get("market_provenance") or {}).get("consensus_inputs") or {}).get(kind) or []
    same = [q["no_vig_probs"][0] for q in inputs if q.get("pair_status") == "valid two-side proposition"
            and q.get("no_vig_probs") and q.get("threshold") == threshold]
    if same:
        return statistics.median(same), "books at threshold"
    ref = (game.get("ref") or {}).get(kind) or {}
    a, b = (ref.get("home"), ref.get("away")) if kind == "spreads" else (ref.get("over"), ref.get("under"))
    if a and b and a.get("point") == threshold and a.get("price") and b.get("price"):
        from bm.score.picks import no_vig_probs
        return no_vig_probs([a["price"], b["price"]])[0], "reference book"
    return None, "missing"


def close_prob(close: dict | None, kind: str, threshold: float | None) -> tuple[float | None, str]:
    """Close no-vig probability of the same proposition: `exact` when close quotes the threshold, else `translated`
    from the close consensus line and its probability (normal model, SIGMA points)."""
    if not close:
        return None, "missing"
    p, how = market_prob(close, kind, threshold)
    if p is not None:
        return p, "exact"
    c = close.get("consensus") or {}
    line, pc = (c.get("spread_home"), c.get("p_cover_home")) if kind == "spreads" else (c.get("total"), c.get("p_over"))
    if kind == "h2h" or line is None or pc is None or threshold is None:
        return None, "missing"
    shift = (threshold - line) if kind == "spreads" else (line - threshold)   # more points to the home side / lower total
    return _phi(_phi_inv(pc) + shift / SIGMA[kind]), "translated"


# ------------------------------------------------------------------ model probabilities


def model_prob(answer: dict, step: str, kind: str, threshold: float | None) -> float | None:
    if step == "line":
        return answer.get({"h2h": "p_home", "spreads": "p_cover_home", "totals": "p_over"}[kind])
    if kind == "h2h":
        return answer.get("p_home")
    q = answer.get("margin" if kind == "spreads" else "total") or {}
    try:
        p10, p50, p90 = float(q["p10"]), float(q["p50"]), float(q["p90"])
    except (KeyError, TypeError, ValueError):
        return None
    if threshold is None:
        return None
    sd = max((p90 - p10) / 2.5631, 1.0)
    cut = -threshold if kind == "spreads" else threshold      # home covers h <=> margin > -h
    return 1 - _phi((cut - p50) / sd)


def outcome(g: dict, kind: str, threshold: float | None) -> float | None:
    hs, as_ = g.get("home_score"), g.get("away_score")
    if hs is None or as_ is None:
        return None
    v = {"h2h": hs - as_, "spreads": hs - as_ + (threshold or 0), "totals": hs + as_ - (threshold or 0)}[kind]
    if kind != "h2h" and threshold is None:
        return None
    return None if v == 0 else float(v > 0)


# ------------------------------------------------------------------ prediction-market robustness close


def _markets_dir() -> Path:
    from bm.markets.collect import markets_dir
    return markets_dir()


def _rows(path: Path) -> list[dict]:
    if not path.exists() or "_settlement" in path.parts:
        return []
    with gzip.open(path, "rt") as f:
        return [json.loads(l) for l in f if l.strip()]


def _mid(bid, ask, last=None) -> float | None:
    try:
        b, a = float(bid), float(ask)
        if 0 < b <= a < 1:
            return (a + b) / 2
    except (TypeError, ValueError):
        pass
    try:
        return float(last) if last is not None and 0 < float(last) < 1 else None
    except (TypeError, ValueError):
        return None


def predmarket_close(game: dict, root: Path | None = None) -> dict[str, float]:
    """{'kalshi': p_home, 'polymarket': p_home} from the newest archived row at or before kickoff (absent = no data)."""
    from bm.picks.lines import TEAMS
    root = root or _markets_dir()
    from datetime import timezone
    from zoneinfo import ZoneInfo
    k = datetime.fromisoformat(game["kickoff"])
    days = sorted({(k - timedelta(days=d)).astimezone(timezone.utc).strftime("%Y-%m-%d") for d in (0, 1)})  # files: UTC day
    out = {}
    ke = k.astimezone(ZoneInfo("America/New_York"))
    code = lambda t: KALSHI_CODE.get(t, t)
    ticker = f"KXNFLGAME-{ke:%y}{MONTHS[ke.month - 1]}{ke:%d}{code(game['away'])}{code(game['home'])}-{code(game['home'])}"
    best = None
    for d in days:
        for r in _rows(root / "kalshi" / "markets" / f"{d}.jsonl.gz"):
            if r.get("ticker") == ticker and datetime.fromisoformat(r["ts"]) <= k and (best is None or r["ts"] > best["ts"]):
                best = r
    if best and (p := _mid(best.get("yes_bid_dollars"), best.get("yes_ask_dollars"), best.get("last_price_dollars"))) is not None:
        out["kalshi"] = p
    nick = {abbr: full.split()[-1] for full, abbr in TEAMS.items()}
    meta = {}
    for f in sorted((root / "polymarket" / "meta").glob("*.jsonl.gz")) if (root / "polymarket" / "meta").exists() else []:
        for m in _rows(f):
            if m.get("kind") != "game" or m.get("sportsMarketType") != "moneyline":
                continue
            try:
                start = datetime.fromisoformat(m["gameStartTime"].replace(" ", "T").replace("+00", "+00:00"))
                names = json.loads(m.get("outcomes") or "[]")
            except (KeyError, ValueError, TypeError):
                continue
            if abs(start - k) < timedelta(hours=6) and set(names) == {nick.get(game["home"]), nick.get(game["away"])}:
                meta[m["id"]] = names[0] == nick.get(game["home"])
    best = None
    for d in days:
        for r in _rows(root / "polymarket" / "markets" / f"{d}.jsonl.gz"):
            if r.get("id") in meta and datetime.fromisoformat(r["ts"]) <= k and (best is None or r["ts"] > best["ts"]):
                best = r
    if best and (p := _mid(best.get("bestBid"), best.get("bestAsk"), best.get("lastTradePrice"))) is not None:
        out["polymarket"] = p if meta[best["id"]] else 1 - p
    return out


# ------------------------------------------------------------------ items


QLEVELS = {"p10": 0.1, "p50": 0.5, "p90": 0.9}
Z90 = 1.2815516


def quantile_items(r: dict, answer: dict, g: dict, sched: dict, seat: str, seen: set) -> list[dict]:
    """Blind-step margin/total quantiles scored on every game: quantile (pinball-CRPS) score of the model's p10/p50/p90
    and of the decision-time market's normal (median = -posted spread / posted total, SD SIGMA) at the same levels."""
    from bm.score.forecast import crps_quantiles
    hs, as_ = sched.get("home_score"), sched.get("away_score")
    out = []
    c = g.get("consensus") or {}
    for name, kind, y in (("margin", "spreads", None if hs is None else hs - as_), ("total", "totals", None if hs is None else hs + as_)):
        key = (seat, g.get("game_id"), name)
        q = answer.get(name) or {}
        line = c.get("spread_home" if kind == "spreads" else "total")
        line = line if line is not None else _ref_threshold(g, kind)
        if key in seen or y is None or line is None or not all(isinstance(q.get(k), (int, float)) for k in QLEVELS):
            continue
        seen.add(key)
        mu = -line if kind == "spreads" else line
        mq = {0.1: mu - Z90 * SIGMA[kind], 0.5: mu, 0.9: mu + Z90 * SIGMA[kind]}
        out.append({"slate": r.get("slate"), "entrant": r["entrant"], "instance": r.get("instance"), "step": "blind",
                    "game_id": g.get("game_id"), "market": name, "type": "quantile", "y": y,
                    "q_model": crps_quantiles({QLEVELS[k]: float(q[k]) for k in QLEVELS}, y), "q_market": crps_quantiles(mq, y)})
    return out


def sharp_prob(sharp: dict | None, close: dict | None, kind: str, threshold: float | None) -> tuple[float | None, str | None]:
    """The sharp close for one proposition: Pinnacle (odds_sharp pull) when it quotes it, else the low-margin us-region
    books (bm.stats.market.US_SHARP_FALLBACK) in the main close snapshot. Returns (probability, book)."""
    from bm.stats.market import SHARP_BOOKS, US_SHARP_FALLBACK
    for game, books in ((sharp, SHARP_BOOKS), (close, US_SHARP_FALLBACK)):
        inputs = ((game or {}).get("market_provenance") or {}).get("consensus_inputs") or {}
        for book in books:
            for q in inputs.get(kind) or []:
                if (q.get("book_key") == book and q.get("pair_status") == "valid two-side proposition"
                        and (kind == "h2h" or q.get("threshold") == threshold)):
                    return q["no_vig_probs"][0], book
    return None, None


def slate_items(rows: list[dict], schedule: list[dict], close_fn, alt_fn=None, verified_only: bool = True,
                now: datetime | None = None, sharp_fn=None) -> tuple[list[dict], dict]:
    """Items for one slate log (payload rows). close_fn(game_id) -> parsed close game (bm.picks.lines.close shape).
    A game counts only once it has kicked off by `now` (default: the clock), so its close is final."""
    from datetime import timezone
    now = now or datetime.now(timezone.utc)
    from bm.picks.receipts import receipts_by_record
    from bm.picks.run import receipt_status
    by_id = {g["game_id"]: g for g in schedule}
    ctx = next((r for r in rows if r.get("kind") == "slate_context"), None)
    dec = {g["game_id"]: g for g in (ctx or {}).get("games", [])}
    if not dec:     # legacy slate without a frozen context: every seat's line step carries the same snapshot's lines
        for r in rows:
            if r.get("kind") == "pick" and r.get("step") == "line":
                for gid, ln in (r.get("lines") or {}).items():
                    dec.setdefault(gid, dict(ln, game_id=gid))
    receipts = receipts_by_record(rows)
    counts = defaultdict(int)
    items, seen, closes, alts, sharps = [], set(), {}, {}, {}
    slate_name = (ctx or {}).get("key") or next((r.get("slate") for r in rows if r.get("slate")), None)

    def market_inputs(gid, sched):
        if gid not in closes:
            closes[gid] = close_fn(gid)
            alts[gid] = alt_fn(sched) if alt_fn else {}
            sharps[gid] = sharp_fn(gid) if sharp_fn else None

    def add(entrant, inst, step, gid, kind, g, sched, a):
        """One item; a=None is a missed answer, scored at the decision-time market (zero edge, imputed)."""
        key = (entrant, inst, step, gid, kind)
        if key in seen:
            return
        t = None if kind == "h2h" else _ref_threshold(g, kind)
        pd, how_d = market_prob(g, kind, t)
        pm = pd if a is None else model_prob(a, step, kind, t)
        if pd is None or pm is None:
            counts[f"{kind}: missing model or decision-time market"] += 1
            return
        seen.add(key)
        pc, how_c = close_prob(closes[gid], kind, t)
        item = {"slate": slate_name, "entrant": entrant, "instance": inst, "step": step,
                "game_id": gid, "market": kind, "threshold": t, "p_model": pm, "p_decision": pd,
                "decision_method": how_d, "p_close": pc, "close_method": how_c, "imputed": a is None,
                "deviation": pm - pd, "move": None if pc is None else pc - pd,
                "outcome": outcome(sched, kind, t)}
        ps, book = sharp_prob(sharps[gid], closes[gid], kind, t)
        item.update(sharp_book=book, move_sharp=None if ps is None else ps - pd)
        if kind == "h2h":
            for src, p in (alts[gid] or {}).items():
                item[f"move_{src}"] = p - pd
        items.append(item)
        counts["imputed items" if a is None else "items"] += 1

    def ready(gid):
        g, sched = dec.get(gid), by_id.get(gid)
        if not g or not sched:
            counts["no decision-time market or schedule"] += 1
            return None
        if datetime.fromisoformat(sched["kickoff"]) > now:
            counts["not kicked off yet (close not final)"] += 1
            return None
        market_inputs(gid, sched)
        return sched

    for r in rows:
        if r.get("kind") != "pick" or r.get("outcome") != "own" or not isinstance(r.get("answer"), dict):
            continue
        step = r.get("step")
        for gid, a in r["answer"].items():
            g = dict(dec.get(gid) or {}, **((r.get("lines") or {}).get(gid) or {}))
            sched = ready(gid) if dec.get(gid) else None
            if sched is None:
                if not dec.get(gid):
                    counts["no decision-time market or schedule"] += 1
                continue
            status = receipt_status(r, sched["kickoff"], receipts.get(r.get("record_id")))
            if verified_only and status != "prospective receipt verified":
                counts[status] += 1
                continue
            if step == "blind":
                items += quantile_items(dict(r, slate=slate_name), a, g, sched, (r["entrant"], r.get("instance")), seen)
            for kind in MARKETS:
                add(r["entrant"], r.get("instance"), step, gid, kind, g, sched, a)
    # Missing-answer rule: every seat of the slate's cohort is expected on every game and market of both steps; an
    # item without a verified own answer is scored at the decision-time market price, so not answering never helps.
    cohort = (ctx or {}).get("models") or sorted({r["entrant"] for r in rows if r.get("kind") == "pick"})
    instances = (ctx or {}).get("instances") or 3
    for gid in sorted(dec):
        sched = by_id.get(gid)
        if not sched or datetime.fromisoformat(sched["kickoff"]) > now:
            continue
        market_inputs(gid, sched)
        for m in cohort:
            for inst in range(1, instances + 1):
                for step in ("blind", "line"):
                    for kind in MARKETS:
                        add(m, inst, step, gid, kind, dec[gid], sched, None)
    return items, dict(counts)


# ------------------------------------------------------------------ baseline entrants

ELO_K = 20.0             # rating change per game (538 NFL Elo)
ELO_HFA = 48.0           # home-field advantage in Elo points (none at neutral sites)
ELO_REVERT = 1 / 3       # share each rating reverts toward ELO_MEAN between seasons
ELO_MEAN = 1505.0
ELO_PER_POINT = 25.0     # Elo points per point of spread


def elo_ratings(games: list[dict], before: datetime) -> dict[str, float]:
    """538-style NFL Elo over every regular- and post-season game final before `before` (kickoff + 4 h): K=20, home edge
    48, margin-of-victory multiplier ln(|margin|+1) * 2.2 / (0.001 * winner Elo edge + 2.2), 1/3 reversion to 1505
    each new season. games: nflverse games rows (season, gameday, gametime, home/away team and score, location)."""
    from bm.stats.nflverse import kickoff_iso
    r: dict[str, float] = defaultdict(lambda: ELO_MEAN)
    season = None
    done = []
    for g in games:
        try:
            k = datetime.fromisoformat(kickoff_iso(g["gameday"], g.get("gametime") or "13:00"))
            hs, as_ = int(g["home_score"]), int(g["away_score"])
        except (KeyError, TypeError, ValueError):
            continue
        if k + timedelta(hours=4) <= before:
            done.append((k, g, hs, as_))
    for k, g, hs, as_ in sorted(done, key=lambda x: x[0]):
        if g.get("season") != season:
            season = g.get("season")
            for t in list(r):
                r[t] = r[t] + (ELO_MEAN - r[t]) * ELO_REVERT
        h, a = g["home_team"], g["away_team"]
        diff = r[h] - r[a] + (0 if g.get("location") == "Neutral" else ELO_HFA)
        p = 1 / (1 + 10 ** (-diff / 400))
        y = 1.0 if hs > as_ else 0.0 if hs < as_ else 0.5
        win_edge = diff if hs > as_ else -diff
        mult = math.log(abs(hs - as_) + 1) * 2.2 / (0.001 * win_edge + 2.2) if hs != as_ else 1.0
        r[h] += ELO_K * mult * (y - p)
        r[a] -= ELO_K * mult * (y - p)
    return dict(r)


def elo_game(ratings: dict[str, float], home: str, away: str, neutral: bool = False) -> tuple[float, float]:
    """(P(home wins), home margin in points) from Elo ratings."""
    diff = ratings.get(home, ELO_MEAN) - ratings.get(away, ELO_MEAN) + (0 if neutral else ELO_HFA)
    return 1 / (1 + 10 ** (-diff / 400)), diff / ELO_PER_POINT


def baseline_items(items: list[dict], ratings: dict[str, float] | None = None, games: dict[str, dict] | None = None) -> list[dict]:
    """Baseline entrants on the same game x market x step propositions: `baseline:market` (the decision-time market,
    zero edge by definition), `baseline:ensemble` (equal-weight mean of the models' answered probabilities),
    `baseline:ensemble+market` (their average) and `baseline:elo` (h2h and spread only; no Elo total model)."""
    groups = defaultdict(list)
    for i in items:
        if i.get("type") != "quantile" and not i.get("imputed") and not i["entrant"].startswith("baseline:"):
            groups[(i["step"], i["game_id"], i["market"])].append(i)
    out = []
    for (step, gid, kind), its in sorted(groups.items()):
        base = {k: its[0][k] for k in ("slate", "game_id", "market", "threshold", "p_decision", "decision_method", "p_close",
                                       "close_method", "move", "outcome", "sharp_book", "move_sharp")}
        base.update({k: v for k, v in its[0].items() if k.startswith("move_")}, step=step, instance=0, imputed=False)
        ens = sum(i["p_model"] for i in its) / len(its)
        probs = {"baseline:market": base["p_decision"], "baseline:ensemble": ens,
                 "baseline:ensemble+market": (ens + base["p_decision"]) / 2}
        g = (games or {}).get(gid)
        if ratings is not None and g and kind != "totals":
            ph, margin = elo_game(ratings, g["home"], g["away"], g.get("neutral", False))
            probs["baseline:elo"] = ph if kind == "h2h" else (
                None if base["threshold"] is None else 1 - _phi((-base["threshold"] - margin) / SIGMA["spreads"]))
        for name, p in probs.items():
            if p is not None:
                out.append(dict(base, entrant=name, p_model=p, deviation=p - base["p_decision"]))
    return out


# ------------------------------------------------------------------ statistics


def slope(items: list[dict], move: str = "move", z: float = 1.96) -> dict:
    """OLS move = a + b * deviation, CR1 standard errors clustered by game."""
    xs = [(i["deviation"], i[move], i["game_id"]) for i in items if i.get(move) is not None]
    n, games = len(xs), len({g for _, _, g in xs})
    if n < 3 or games < 2:
        return {"n": n, "games": games, "beta": None}
    mx = sum(x for x, _, _ in xs) / n
    my = sum(y for _, y, _ in xs) / n
    sxx = sum((x - mx) ** 2 for x, _, _ in xs)
    if sxx <= 0:
        return {"n": n, "games": games, "beta": None}
    b = sum((x - mx) * (y - my) for x, y, _ in xs) / sxx
    a = my - b * mx
    by = defaultdict(float)
    for x, y, g in xs:
        by[g] += (x - mx) * (y - a - b * x)
    var = sum(v * v for v in by.values()) / sxx ** 2 * games / (games - 1) * (n - 1) / (n - 2)
    se = math.sqrt(var)
    return {"n": n, "games": games, "beta": round(b, 4), "se": round(se, 4), "ci": [round(b - z * se, 4), round(b + z * se, 4)],
            "intercept": round(a, 5)}


def brier_diff(items: list[dict], z: float = 1.96) -> dict:
    """Mean over items of (p_model - y)^2 - (p_decision - y)^2 (positive = worse than the market), game-clustered."""
    xs = [((i["p_model"] - i["outcome"]) ** 2 - (i["p_decision"] - i["outcome"]) ** 2, i["game_id"]) for i in items
          if i.get("outcome") is not None]
    n = len(xs)
    if not n:
        return {"n": 0, "mean": None}
    m = sum(d for d, _ in xs) / n
    by = defaultdict(float)
    for d, g in xs:
        by[g] += d - m
    G = len(by)
    se = math.sqrt(sum(v * v for v in by.values()) / n ** 2 * G / max(G - 1, 1)) if G > 1 else None
    return {"n": n, "games": G, "mean": round(m, 4), "se": None if se is None else round(se, 4),
            "ci": None if se is None else [round(m - z * se, 4), round(m + z * se, 4)]}


def mean_diff(pairs: list[tuple[float, str]], z: float = 1.96) -> dict:
    """Mean of per-item differences with a game-clustered SE (pairs: (difference, game_id))."""
    n = len(pairs)
    if not n:
        return {"n": 0, "mean": None}
    m = sum(d for d, _ in pairs) / n
    by = defaultdict(float)
    for d, g in pairs:
        by[g] += d - m
    G = len(by)
    se = math.sqrt(sum(v * v for v in by.values()) / n ** 2 * G / (G - 1)) if G > 1 else None
    return {"n": n, "games": G, "mean": round(m, 4), "se": None if se is None else round(se, 4),
            "ci": None if se is None else [round(m - z * se, 4), round(m + z * se, 4)]}


def _ll(p: float, y: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return -math.log(p if y else 1 - p)


def summarize(items: list[dict], z: float = 1.96) -> dict:
    """Per step: per model and pooled, all markets and each market: CLV slope (primary), exact-close-only slope,
    prediction-market robustness slopes (h2h), outcome Brier difference (secondary)."""
    out = {}
    quants = [i for i in items if i.get("type") == "quantile"]
    items = [i for i in items if i.get("type") != "quantile"]
    for step in sorted({i["step"] for i in items}):
        st = [i for i in items if i["step"] == step]
        models = [i for i in st if not i["entrant"].startswith("baseline:")]
        groups = {"pooled": models, **{m: [i for i in st if i["entrant"] == m] for m in sorted({i["entrant"] for i in st})}}
        res = {}
        for who, its in groups.items():
            exact = [i for i in its if i["close_method"] == "exact"]
            h2h = [i for i in its if i["market"] == "h2h"]
            res[who] = {"clv_slope": slope(its, z=z), "clv_slope_exact_close": slope(exact, z=z),
                        "clv_slope_answered_only": slope([i for i in its if not i.get("imputed")], z=z),
                        "answer_rate": round(sum(not i.get("imputed") for i in its) / len(its), 4) if its else None,
                        "by_market": {m: slope([i for i in its if i["market"] == m], z=z) for m in MARKETS},
                        "robustness_close": {**{src: slope(h2h, f"move_{src}", z) for src in ("kalshi", "polymarket")
                                                if any(i.get(f"move_{src}") is not None for i in h2h)},
                                             "sharp": dict(slope(its, "move_sharp", z), books=sorted(
                                                 {i["sharp_book"] for i in its if i.get("sharp_book")}))},
                        "brier_vs_market": brier_diff(its, z),
                        "brier_vs_close": mean_diff([((i["p_model"] - i["outcome"]) ** 2 - (i["p_close"] - i["outcome"]) ** 2,
                                                      i["game_id"]) for i in its if i.get("outcome") is not None
                                                     and i.get("p_close") is not None], z),
                        "log_loss_vs_market": mean_diff([(_ll(i["p_model"], i["outcome"]) - _ll(i["p_decision"], i["outcome"]),
                                                          i["game_id"]) for i in its if i.get("outcome") is not None], z),
                        "items": len(its), "games": len({i["game_id"] for i in its})}
            if step == "blind":
                if who.startswith("baseline:"):
                    continue
                qs = quants if who == "pooled" else [q for q in quants if q["entrant"] == who]
                res[who]["quantile_score_vs_market"] = {m: mean_diff([(q["q_model"] - q["q_market"], q["game_id"])
                                                                      for q in qs if q["market"] == m], z)
                                                        for m in ("margin", "total")}
        out[step] = res
    return out


def _week(slate: str | None) -> int | None:
    import re
    m = re.search(r"-w(\d+)-", slate or "")
    return int(m.group(1)) if m else None


def encompassing_report(items: list[dict], B: int = 500, seed: int = 0) -> dict:
    """Forecast encompassing (bm.score.forecast.encompassing: outcome on [1, market, model]) per step and entrant over
    answered items with an outcome; the model coefficient's 95% band is a bootstrap over weeks (clusters resampled
    with replacement, B draws, fixed seed). One week gives no week-clustered band."""
    import random
    from bm.score.forecast import encompassing
    out = {}
    probs = [i for i in items if i.get("type") != "quantile" and not i.get("imputed") and i.get("outcome") is not None]
    for step in sorted({i["step"] for i in probs}):
        st = [i for i in probs if i["step"] == step]
        groups = {"pooled": [i for i in st if not i["entrant"].startswith("baseline:")],
                  **{m: [i for i in st if i["entrant"] == m] for m in sorted({i["entrant"] for i in st})}}
        res = {}
        for who, its in groups.items():
            e = encompassing([i["outcome"] for i in its], [i["p_decision"] for i in its], [i["p_model"] for i in its])
            weeks = defaultdict(list)
            for i in its:
                weeks[_week(i.get("slate"))].append(i)
            band = None
            if len(weeks) >= 2 and e.get("coef", [None])[2] is not None:
                rng, keys, draws = random.Random(seed), sorted(weeks, key=str), []
                for _ in range(B):
                    pick = [i for k in (rng.choice(keys) for _ in keys) for i in weeks[k]]
                    c = encompassing([i["outcome"] for i in pick], [i["p_decision"] for i in pick], [i["p_model"] for i in pick])
                    if c.get("coef", [None])[2] is not None:
                        draws.append(c["coef"][2])
                draws.sort()
                if len(draws) >= 20:
                    band = [round(draws[int(0.025 * len(draws))], 4), round(draws[int(0.975 * len(draws)) - 1], 4)]
            res[who] = {"n": e.get("n"), "weeks": len(weeks),
                        "coef": [None if c is None else round(c, 4) for c in e.get("coef", [None] * 3)],
                        "se": [None if c is None else round(c, 4) for c in e.get("se", [None] * 3)],
                        "model_coef_week_band": band,
                        "note": None if band else "fewer than 2 weeks (or a singular fit): no week-clustered band"}
        out[step] = res
    return out


def _elo_inputs(rows: list[dict], schedule: list[dict], elo_games: list[dict] | None):
    ctx = next((r for r in rows if r.get("kind") == "slate_context"), None)
    # legacy slates (no frozen context, no as_of on records): the earliest pick record's time bounds the decision time
    at = (ctx or {}).get("as_of") or min((r.get("as_of") or r.get("ts") for r in rows
                                          if r.get("kind") == "pick" and (r.get("as_of") or r.get("ts"))), default=None)
    if not elo_games or not at:
        return None, None
    neutral = {g["game_id"]: g.get("location") == "Neutral" for g in elo_games}
    games = {g["game_id"]: {"home": g["home"], "away": g["away"], "neutral": neutral.get(g["game_id"], False)} for g in schedule}
    return elo_ratings(elo_games, datetime.fromisoformat(at)), games


def score(slates: list[str] | None = None, *, root: Path | None = None, schedule: list[dict] | None = None,
          close_fn=None, alt_fn=None, z: float = 1.96, now: datetime | None = None, sharp_fn=None,
          elo_games: list[dict] | None = None) -> dict:
    """Per-slate and season report from records/picks/<slate>.jsonl (scripted slates excluded)."""
    from bm import records
    root = root or ROOT
    if schedule is None:
        from bm.stats import nflverse as nv
        schedule = nv.schedule(2026)
    if close_fn is None:
        from bm.picks import lines as L
        close_fn = lambda gid: L.close(gid, schedule)
        sharp_fn = lambda gid: L.close(gid, schedule, source="odds_sharp")
    if alt_fn is None:
        alt_fn = predmarket_close
    if elo_games is None:
        try:
            from bm.stats import nflverse as nv
            elo_games = nv.rows("games")[0]          # history only: elo_ratings uses games final before each as_of
        except Exception:
            elo_games = []
    d = root / "records" / "picks"
    files = sorted(d.glob("*.jsonl")) if d.exists() else []
    files = [f for f in files if not f.stem.endswith("-scripted") and (slates is None or f.stem in slates)]
    per_slate, season, counts = {}, [], {}
    for f in files:
        rows = [r["payload"] for r in records.ChainLog(f).rows()]
        its, c = slate_items(rows, schedule, close_fn, alt_fn, now=now, sharp_fn=sharp_fn)
        its += baseline_items(its, *_elo_inputs(rows, schedule, elo_games))
        per_slate[f.stem] = summarize(its, z)
        counts[f.stem] = c
        season += its
    return {"endpoint": "CLV slope: close no-vig move on model's decision-time deviation (primary); outcome Brier vs "
                        "decision-time market (secondary)", "z": z, "slates": per_slate, "season": summarize(season, z),
            "season_encompassing": encompassing_report(season),
            "counts": counts, "items": len(season)}


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    slates = [argv[i + 1] for i, a in enumerate(argv) if a == "--slate"] or None
    z = float(argv[argv.index("--z") + 1]) if "--z" in argv else 1.96
    print(json.dumps(score(slates, z=z), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
