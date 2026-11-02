"""Forecast books (Q4, §4.2.5, D-INFO-2) and the preseason prophecy book (approved Oct 7 ~23:58).

Weekly book: due before a seat's market tools unlock; revisable; every version kept and scored. It has the seat's own
roster, a league slate that is the same for every manager in every league that week, engine-written calls, up to 5 bold
calls, this week's matchup and the season outlook, and a self-forecast.

  {"roster": {pid: ITEM}, "slate": {pid: ITEM}, "calls": {call_id: p}, "bold": [{player, measure, threshold, horizon, p}],
   "week": {"p_win": p, "my_points": x, "opp_points": x}, "season": {"p_playoffs": p, "p_title": p},
   "self": {"expected_points": x, "expected_finish": n}}
  ITEM = {"p_plays": p, "q": {"10": x, "25": x, "50": x, "75": x, "90": x}, "ros_ppg": {"10": x, "50": x, "90": x}}

Missing items are allowed and counted (completeness is published); malformed values are refused at filing.
In control leagues the book is an observation-only call: nothing in it is ever shown back to the seat.

Prophecy book: filed once, right after the draft, and sealed (its hash is logged; nothing reads it back until the
season ends): champion, 3 breakouts, 3 busts, own final rank, P(title for itself).
"""
from __future__ import annotations

import hashlib
import math
from datetime import datetime, timedelta
from functools import lru_cache

from bm.log import canonical

LEVELS = ("10", "25", "50", "75", "90")
SLATE_SIZE = {"QB": 4, "RB": 8, "WR": 8, "TE": 4}


def slate_time(week: int) -> datetime:
    from bm.league.season import week_anchor
    return week_anchor(week) - timedelta(days=2) + timedelta(hours=12)      # Tue 12:00 ET of the NFL week


@lru_cache(maxsize=8)
def _slate(week: int) -> tuple:
    return tuple(_slate_build(week))


def slate(week: int) -> list[str]:
    return list(_slate(week))


def _slate_build(week: int) -> list[str]:
    """The league slate: the same players for every manager in every league. Chosen from data visible at the week's
    Tuesday noon: the top season-to-date scorers per game at each position among players whose team plays that week
    (so it never depends on any league's rosters or on our projection's ranking)."""
    from bm.engine.rules import load_rules
    from bm.league.context import build_view
    v = build_view(load_rules(), week, slate_time(week))
    out = []
    for pos, n in SLATE_SIZE.items():
        c = [p for p, x in v.world.players.items() if x["pos"] == pos and x["team"] in v.opp and v.proj.get(p, {}).get("games", 0) >= 2]
        c.sort(key=lambda p: (-v.proj[p]["ytd"] / max(v.proj[p]["games"], 1), p))
        out += c[:n]
    return out


@lru_cache(maxsize=8)
def _calls(week: int) -> tuple:
    return tuple(_calls_build(week))


def calls(week: int) -> list[dict]:
    return [dict(c) for c in _calls(week)]


def _calls_build(week: int) -> list[dict]:
    """Engine-written propositions, deterministic per week: one 'scores at least T' call for every slate player
    (T = his season points per game rounded to the nearest whole point) and one 'is active' call for every slate
    player listed Questionable at slate time."""
    from bm.engine.rules import load_rules
    from bm.league.context import build_view
    v = build_view(load_rules(), week, slate_time(week))
    out = []
    for pid in slate(week):
        pr = v.proj.get(pid, {})
        t = round(pr.get("ytd", 0) / max(pr.get("games", 1), 1))
        out.append({"id": f"w{week}:{pid}:ge{t}", "player": pid, "text": f"{v.world.players[pid]['name']} scores at least {t} points in week {week}",
                    "kind": "points_ge", "threshold": t})
        if pr.get("status") == "Questionable":
            out.append({"id": f"w{week}:{pid}:active", "player": pid, "text": f"{v.world.players[pid]['name']} is active in week {week}", "kind": "active"})
    return out


def _prob(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and 0.0 <= x <= 1.0      # NaN fails both bounds


def _item_errors(pid: str, it) -> list[str]:
    if not isinstance(it, dict):
        return [f"{pid}: item must be an object"]
    errs = []
    if "p_plays" in it and not _prob(it["p_plays"]):
        errs.append(f"{pid}: p_plays must be a probability")
    q = it.get("q")
    if q is not None:
        try:
            vals = [q[l] for l in LEVELS]
            if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in vals):
                raise ValueError
            if any(b < a for a, b in zip(vals, vals[1:])):
                errs.append(f"{pid}: quantiles must not decrease from 10 to 90")
        except (KeyError, TypeError, ValueError):
            errs.append(f"{pid}: q needs numbers at 10, 25, 50, 75, 90")
    return errs


SHAPE = ('{"roster": {"<player id>": {"p_plays": 0.9, "q": {"10": 4, "25": 7, "50": 10, "75": 14, "90": 19}, '
         '"ros_ppg": {"10": 6, "50": 9, "90": 13}}, ...}, "slate": {...same per slate player...}, "calls": {"<call id>": 0.6}, '
         '"bold": [], "week": {"p_win": 0.5, "my_points": 110, "opp_points": 105}, "season": {"p_playoffs": 0.5, "p_title": 0.1}, '
         '"self": {"expected_points": 110, "expected_finish": 5}}')


def validate(book, lg, team: str, week: int, view) -> list[str]:
    """A book counts as filed (and unlocks the market tools) only if it is a real book: points quantiles for at least
    half of the seat's own roster and half of the league slate. Missing items beyond that are allowed and counted."""
    if not isinstance(book, dict):
        return ["the book must be an object shaped like: " + SHAPE]
    errs = []
    roster = (lg.state()["teams"][team]["roster"] if lg is not None and hasattr(lg, "state") else [])
    has_q = lambda part, ids: sum(1 for p in ids if isinstance((book.get(part) or {}).get(p), dict) and (book[part][p].get("q")))
    sl = slate(week) if lg is not None else []
    if roster and has_q("roster", roster) * 2 < len(roster):
        errs.append(f"incomplete: points quantiles for {has_q('roster', roster)} of your {len(roster)} roster players (need at least half). "
                    f"Shape: " + SHAPE)
    if sl and has_q("slate", sl) * 2 < len(sl):
        errs.append(f"incomplete: points quantiles for {has_q('slate', sl)} of the {len(sl)} slate players (need at least half)")
    for part in ("roster", "slate"):
        if not isinstance(book.get(part) or {}, dict):
            errs.append(f"{part} must be an object keyed by player id")
            continue
        for pid, it in (book.get(part) or {}).items():
            if pid not in view.world.players:
                errs.append(f"{part}: unknown player {pid}")
            else:
                errs += _item_errors(pid, it)
    for cid, p in ((book.get("calls") or {}) if isinstance(book.get("calls") or {}, dict) else {}).items():
        if not _prob(p):
            errs.append(f"call {cid}: probability must be 0..1")
    for b in (book.get("bold") or [])[:5]:
        if not isinstance(b, dict) or not _prob(b.get("p")):
            errs.append("bold calls need {player, measure, threshold, horizon, p}")
    for k in ("week", "season"):
        if not isinstance(book.get(k) or {}, dict):
            errs.append(f"{k} must be an object, e.g. " + ('{"p_win": 0.5, "my_points": 110, "opp_points": 105}' if k == "week" else '{"p_playoffs": 0.5, "p_title": 0.1}'))
            continue
        for f, x in (book.get(k) or {}).items():
            if f.startswith("p_") and not _prob(x):
                errs.append(f"{k}.{f} must be a probability")
    if len(book.get("bold") or []) > 5:
        errs.append("at most 5 bold calls")
    return errs[:12]


def completeness(book: dict, roster: list[str], slate_ids: list[str], call_ids: list[str]) -> dict:
    has = lambda part, ids: sum(1 for p in ids if isinstance((book.get(part) or {}).get(p), dict) and (book[part][p].get("q") or "p_plays" in book[part][p]))
    return {"roster": f"{has('roster', roster)}/{len(roster)}", "slate": f"{has('slate', slate_ids)}/{len(slate_ids)}",
            "calls": f"{sum(1 for c in call_ids if c in (book.get('calls') or {}))}/{len(call_ids)}"}


def filed_event(team: str, week: int, book: dict, version: int, ts: str, roster: list[str], wake_id: str | None, league: str) -> dict:
    sl = slate(week)
    cs = [c["id"] for c in calls(week)]
    return {"type": "forecast_filed", "ts": ts, "team": team, "week": week, "version": version, "book": book,
            "sha256": hashlib.sha256(canonical(book)).hexdigest(), "completeness": completeness(book, roster, sl, cs),
            "wake_id": wake_id, "league": league}


def task_text(week: int, roster: list[str], view) -> str:
    """The FORECAST BOOK instructions shown to a seat (§4.2.5), with this week's slate and calls."""
    sl = slate(week)
    cs = calls(week)
    nm = lambda p: f"{p} {view.world.players[p]['name']} {view.world.players[p]['pos']} {view.world.players[p]['team']}"
    return ("FORECAST BOOK · Week %d · due before market tools unlock (now locked for you)\n"
            "File your own beliefs. You may research first (stats, injury reports, news, base rates). Score: proper scoring rules "
            "against real outcomes; only beliefs that turn out better than the market's earn credit.\n"
            "1. Your roster: for each player, p_plays and this week's points as q {10,25,50,75,90}; ros_ppg {10,50,90}.\n"
            "2. League slate (%d players, the same for every manager in every league): the same fields.\n"
            "3. Calls: a probability each.\n4. Bold calls: up to 5 of your own, as {player, measure, threshold, horizon, p}.\n"
            "5. week {p_win, my_points, opp_points}; season {p_playoffs, p_title}.\n6. self {expected_points, expected_finish}.\n"
            "Shape: " + '{"roster": {id: {"p_plays": p, "q": {"10":x,"25":x,"50":x,"75":x,"90":x}, "ros_ppg": {"10":x,"50":x,"90":x}}}, '
            '"slate": {...same...}, "calls": {call_id: p}, "bold": [...], "week": {...}, "season": {...}, "self": {...}}\n'
            "YOUR ROSTER: %s\nSLATE: %s\nCALLS:\n%s") % (
        week, len(sl), "; ".join(nm(p) for p in roster), "; ".join(nm(p) for p in sl), "\n".join(f"  {c['id']}: {c['text']}" for c in cs))


# ------------------------------------------------------------------ prophecy (preseason, sealed at the draft)

PROPHECY_FIELDS = ("champion", "breakouts", "busts", "own_final_rank", "p_title_self")


def prophecy_errors(p: dict, team_ids: list[str], players) -> list[str]:
    if not isinstance(p, dict):
        return ["the prophecy must be an object"]
    errs = []
    if p.get("champion") not in team_ids:
        errs.append("champion must be a team id in this league")
    for k in ("breakouts", "busts"):
        v = p.get(k)
        if not isinstance(v, list) or not 1 <= len(v) <= 3 or any(x not in players for x in v):
            errs.append(f"{k}: 1-3 player ids")
    if not isinstance(p.get("own_final_rank"), int) or not 1 <= p["own_final_rank"] <= len(team_ids):
        errs.append(f"own_final_rank must be 1..{len(team_ids)}")
    if not _prob(p.get("p_title_self")):
        errs.append("p_title_self must be a probability")
    return errs


def sealed_event(team: str, prophecy: dict, ts: str, league: str) -> dict:
    """The prophecy is logged with its hash and never read back to any seat until the season ends."""
    return {"type": "prophecy_sealed", "ts": ts, "team": team, "league": league, "prophecy": prophecy,
            "sha256": hashlib.sha256(canonical(prophecy)).hexdigest()}


# ------------------------------------------------------------------ scoring a week's books (Q5 scorers)

def score_week(lg_events: list[dict], week: int, pts: dict[str, float], played: set[str], market: dict[str, dict]) -> dict:
    """Score every seat's last book version filed before the week's first kickoff ("accurate late") and its first
    version ("accurate early"): CRPS on the points quantiles and Brier on p_plays, against the market baseline (`market`:
    pid -> quantiles from the market point estimate plus the historical error spread, bm.stats.baserates)."""
    from bm.score import forecast as F
    from bm.stats import nflverse as nv
    first_kick = min(g["kickoff"] for g in nv.schedule(2026) if g["week"] == week)
    books: dict[str, list[dict]] = {}
    for e in lg_events:                               # only versions filed before the week's first kickoff are scored
        if e["type"] == "forecast_filed" and e["week"] == week and datetime.fromisoformat(e["ts"]) < datetime.fromisoformat(first_kick):
            books.setdefault(e["team"], []).append(e)
    out = {}
    for team, vs in books.items():
        res = {}
        for label, ev in (("first", vs[0]), ("last", vs[-1])):
            m_crps, k_crps, br = [], [], []
            for part in ("roster", "slate"):
                for pid, it in (ev["book"].get(part) or {}).items():
                    if pid in pts and isinstance(it, dict) and it.get("q") and pid in market:
                        q = {float(l) / 100: float(it["q"][l]) for l in LEVELS}
                        m_crps.append(F.crps_quantiles(q, pts[pid]))
                        k_crps.append(F.crps_quantiles({float(l) / 100: market[pid][l] for l in LEVELS}, pts[pid]))
                    if isinstance(it, dict) and "p_plays" in it:
                        br.append(F.brier(it["p_plays"], pid in played))
            res[label] = {"n_points": len(m_crps), "crps": round(sum(m_crps) / len(m_crps), 3) if m_crps else None,
                          "market_crps": round(sum(k_crps) / len(k_crps), 3) if k_crps else None,
                          "skill_vs_market": round(F.skill(m_crps, k_crps), 4) if m_crps else None,
                          "n_plays": len(br), "brier_plays": round(sum(br) / len(br), 4) if br else None}
        out[team] = res
    return out
