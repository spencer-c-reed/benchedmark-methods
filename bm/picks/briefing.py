"""Bettor-parity briefing for the restart Picks blind step (prompt version picks-restart-1).

What a bettor has at decision time, all as-of filtered and with no outcomes and no betting lines:
  - each team's record and points for/against per game (games final by as_of only);
  - the full injury report for the week: every designation (Out/Doubtful/Questionable/...) at every position and
    every non-full practice participant, each with the time we first saw it (nflverse injuries, first_seen_at <= as_of);
  - recent player news (Sleeper player map snapshot retrieved <= as_of; news_updated within NEWS_DAYS and <= as_of);
  - rest days since each team's previous game; the away side's travel (great-circle miles, time zones);
  - venue, roof and surface. No weather forecast source exists on disk, so the briefing says so instead of guessing;
    nflverse temp/wind are filled in after games and are never read.
leak_scan(prompt, as_of) fails a rendered prompt carrying a timestamp after as_of, a final score of a game not final at
as_of, or a betting-line term in its data rows (the P2 leak_scan pattern, bm/panel_p2.py).
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
NEWS_DAYS = 7
NEWS_PER_TEAM = 8
SEASON = 2026

# Home stadium (lat, lon, IANA zone); neutral venues by nflverse stadium name.
HOME = {"ARI": (33.528, -112.263, "America/Phoenix"), "ATL": (33.755, -84.401, "America/New_York"),
        "BAL": (39.278, -76.623, "America/New_York"), "BUF": (42.774, -78.787, "America/New_York"),
        "CAR": (35.226, -80.853, "America/New_York"), "CHI": (41.862, -87.617, "America/Chicago"),
        "CIN": (39.095, -84.516, "America/New_York"), "CLE": (41.506, -81.700, "America/New_York"),
        "DAL": (32.748, -97.093, "America/Chicago"), "DEN": (39.744, -105.020, "America/Denver"),
        "DET": (42.340, -83.046, "America/Detroit"), "GB": (44.501, -88.062, "America/Chicago"),
        "HOU": (29.685, -95.411, "America/Chicago"), "IND": (39.760, -86.164, "America/Indiana/Indianapolis"),
        "JAX": (30.324, -81.637, "America/New_York"), "KC": (39.049, -94.484, "America/Chicago"),
        "LV": (36.091, -115.184, "America/Los_Angeles"), "LAC": (33.953, -118.339, "America/Los_Angeles"),
        "LA": (33.953, -118.339, "America/Los_Angeles"), "MIA": (25.958, -80.239, "America/New_York"),
        "MIN": (44.974, -93.258, "America/Chicago"), "NE": (42.091, -71.264, "America/New_York"),
        "NO": (29.951, -90.081, "America/Chicago"), "NYG": (40.813, -74.074, "America/New_York"),
        "NYJ": (40.813, -74.074, "America/New_York"), "PHI": (39.901, -75.168, "America/New_York"),
        "PIT": (40.447, -80.016, "America/New_York"), "SF": (37.403, -121.970, "America/Los_Angeles"),
        "SEA": (47.595, -122.332, "America/Los_Angeles"), "TB": (27.976, -82.503, "America/New_York"),
        "TEN": (36.166, -86.771, "America/Chicago"), "WAS": (38.908, -76.864, "America/New_York")}
NEUTRAL = {"Wembley Stadium": (51.556, -0.280, "Europe/London"),
           "Tottenham Hotspur Stadium": (51.604, -0.066, "Europe/London"),
           "Bernabeu": (40.453, -3.688, "Europe/Madrid"), "Melbourne Cricket Ground": (-37.820, 144.983, "Australia/Melbourne"),
           "Estadio Banorte": (19.303, -99.150, "America/Mexico_City"),
           "FC Bayern Munich Stadium": (48.219, 11.625, "Europe/Berlin"), "Stade de France": (48.924, 2.360, "Europe/Paris"),
           "Maracana Stadium": (-22.912, -43.230, "America/Sao_Paulo")}
VENUE_FIELDS = ("game_id", "location", "roof", "surface", "stadium", "stadium_id")   # never scores, lines, temp or wind


def _miles(a: tuple, b: tuple) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


def _tz_hours(z1: str, z2: str, at: datetime) -> float:
    return (at.astimezone(ZoneInfo(z2)).utcoffset() - at.astimezone(ZoneInfo(z1)).utcoffset()).total_seconds() / 3600


def _stamp(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def venue_rows(at: datetime) -> dict[str, dict]:
    """Static venue fields per game from the nflverse games snapshot retrieved <= at (whitelisted fields only)."""
    from bm.stats import nflverse as nv
    try:
        rows, _ = nv.rows("games", at)
    except Exception:
        return {}
    return {r["game_id"]: {k: r.get(k, "") for k in VENUE_FIELDS} for r in rows if r.get("game_id")}


def build(week: int, at: datetime, games: list[dict], *, schedule: list[dict] | None = None,
          injuries: list[dict] | None = None, news: dict | None = None, venues: dict | None = None) -> dict:
    """The briefing for one slate: {"teams": {team: {...}}, "games": {game_id: {...}}}. Sources default to the
    on-disk nflverse schedule/injuries/games and the Sleeper news snapshot, each as of `at`."""
    from bm.stats import market, nflverse as nv
    if schedule is None:
        from bm.picks.run import _schedule_at
        schedule = _schedule_at(nv, at)
    injuries = nv.injuries(SEASON, at, week) if injuries is None else injuries
    news = market.news_as_of(at) if news is None else news
    venues = venue_rows(at) if venues is None else venues
    playing = {t for g in games for t in (g["home"], g["away"])}
    teams: dict = defaultdict(lambda: {"w": 0, "l": 0, "t": 0, "pf": 0, "pa": 0, "g": 0, "injuries": [], "news": [],
                                       "last_game": None})
    for g in schedule:
        k = datetime.fromisoformat(g["kickoff"])
        if g["week"] < week and g.get("home_score") is not None and k + timedelta(hours=4) <= at:
            for t, f, a in ((g["home"], g["home_score"], g["away_score"]), (g["away"], g["away_score"], g["home_score"])):
                o = teams[t]
                o["g"] += 1
                o["pf"] += f
                o["pa"] += a
                o["w" if f > a else "l" if f < a else "t"] += 1
        if k < at:
            for t in (g["home"], g["away"]):
                if teams[t]["last_game"] is None or k.isoformat() > teams[t]["last_game"]:
                    teams[t]["last_game"] = k.isoformat()
    for r in injuries:
        t = r.get("team")
        seen = r.get("first_seen_at")
        if t not in playing or not seen or datetime.fromisoformat(seen) > at:
            continue
        status, practice = (r.get("report_status") or "").strip(), (r.get("practice_status") or "").strip()
        if not status and (not practice or practice.startswith("Full")):
            continue
        injury = r.get("report_primary_injury") or r.get("practice_primary_injury") or ""
        teams[t]["injuries"].append({"name": r.get("full_name"), "pos": r.get("position"), "status": status or "no game status",
                                     "injury": injury, "practice": practice, "seen": _stamp(datetime.fromisoformat(seen))})
    lo = at - timedelta(days=NEWS_DAYS)
    for pid, p in (news or {}).items():
        t = p.get("team")
        try:
            upd = datetime.fromtimestamp(float(p.get("news_updated")) / 1000, timezone.utc)
        except (TypeError, ValueError):
            continue
        if t not in playing or not lo <= upd <= at:
            continue
        bits = [f"status {p['status']}" if p.get("status") and p.get("status") != "Active" else "",
                f"injury {p['injury_status']}" + (f" ({p['injury_body_part']})" if p.get("injury_body_part") else "") if p.get("injury_status") else "",
                p.get("injury_notes") or "",
                f"practice {p['practice_participation']}" if p.get("practice_participation") else "",
                f"depth {p['depth_chart_position']}{p.get('depth_chart_order') or ''}" if p.get("depth_chart_position") else ""]
        text = "; ".join(b for b in bits if b) or "news update"
        if _MARKET.search(text):
            continue                           # a note quoting a line or odds is market data, not a bettor-side fact
        teams[t]["news"].append({"name": p.get("full_name") or pid, "pos": p.get("position"), "text": text,
                                 "updated": _stamp(upd)})
    for t in playing:
        teams[t]["news"] = sorted(teams[t]["news"], key=lambda x: x["updated"], reverse=True)[:NEWS_PER_TEAM]
        teams[t]["injuries"].sort(key=lambda x: (x["status"] == "no game status", x["name"] or ""))
    out_games = {}
    for g in games:
        k = datetime.fromisoformat(g["kickoff"])
        v = venues.get(g["game_id"], {})
        neutral = v.get("location") == "Neutral"
        site = NEUTRAL.get(v.get("stadium")) if neutral else HOME.get(g["home"])
        travel = {}
        for side in ("away", "home"):
            t = g[side]
            if t in HOME and site and (neutral or side == "away"):
                travel[side] = {"miles": round(_miles(HOME[t], site)), "tz_hours": _tz_hours(HOME[t][2], site[2], k)}
        rest = {}
        for side in ("away", "home"):
            lg = teams[g[side]]["last_game"]
            if lg:
                rest[side] = (k.astimezone(ET).date() - datetime.fromisoformat(lg).astimezone(ET).date()).days
        out_games[g["game_id"]] = {"stadium": v.get("stadium") or None, "neutral": neutral,
                                   "roof": v.get("roof") or "unknown", "surface": v.get("surface") or "unknown",
                                   "weather": "no forecast source on disk", "travel": travel, "rest_days": rest}
    return {"teams": {t: {k: v for k, v in teams[t].items() if k != "last_game"} for t in sorted(playing)},
            "games": out_games}


def _team_rows(t: str, s: dict, rest: int | None, travel: dict | None) -> list[str]:
    g = max(s["g"], 1)
    head = (f"{t} {s['w']}-{s['l']}" + (f"-{s['t']}" if s["t"] else "")
            + f", {s['pf'] / g:.1f} pts for / {s['pa'] / g:.1f} against per game"
            + (f"; rest {rest} days" if rest is not None else "")
            + (f"; travel {travel['miles']:,} mi, {travel['tz_hours']:+g} h clock change" if travel else ""))
    rows = [f"    {head}"]
    inj = "; ".join(f"{x['name']} {x['pos']}: {x['status']}" + (f" ({x['injury']})" if x["injury"] else "")
                    + (f", practice {x['practice']}" if x["practice"] else "") + f" [seen {x['seen']}]" for x in s["injuries"])
    rows.append(f"      injury report: {inj or 'none listed'}")
    if s["news"]:
        rows.append("      news: " + "; ".join(f"{x['name']} {x['pos']}: {x['text']} [updated {x['updated']}]" for x in s["news"]))
    return rows


def render(games: list[dict], sheet: dict) -> list[str]:
    rows = []
    for g in games:
        info = sheet["games"][g["game_id"]]
        h, a = g["home"], g["away"]
        venue = (f"venue {info['stadium'] or 'unknown'}" + (" (neutral site)" if info["neutral"] else "")
                 + f", roof {info['roof']}, surface {info['surface']}; weather: {info['weather']}")
        rows.append(f"- {g['game_id']}: {a} at {h}, kickoff {datetime.fromisoformat(g['kickoff']).astimezone(ET):%a %b %d %H:%M} ET; {venue}")
        rows += _team_rows(a, sheet["teams"][a], info["rest_days"].get("away"), info["travel"].get("away"))
        rows += _team_rows(h, sheet["teams"][h], info["rest_days"].get("home"), info["travel"].get("home"))
    return rows


_TS = re.compile(r"\[(?:seen|updated) (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})?)\]")
_MARKET = re.compile(r"\b(spread|moneyline|money line|over/under|o/u|odds|vig|point total|total line|favou?red by|"
                     r"underdog|implied probability|sportsbook|kalshi|polymarket)\b", re.I)


def leak_scan(prompt: str, at: datetime, schedule: list[dict] | None = None) -> list[str]:
    """Findings (empty = clean) for a rendered blind prompt: any [seen|updated] time after `at`, a final score of a game
    not final at `at` (truth = the newest schedule on disk), or a betting-line term in a data row (rows starting
    with '- ' or indented)."""
    if schedule is None:
        from bm.stats import nflverse as nv
        schedule = nv.schedule(SEASON)
    found = []
    for ts in _TS.findall(prompt):
        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        t = t if t.tzinfo else t.replace(tzinfo=timezone.utc)
        if t > at:
            found.append(f"item time after as_of: {ts}")
    late = [g for g in schedule if g.get("home_score") is not None and g.get("away_score") is not None
            and datetime.fromisoformat(g["kickoff"]) + timedelta(hours=4) > at]
    for ln in prompt.splitlines():
        if not (ln.startswith("- ") or ln.startswith("    ")):
            continue
        m = _MARKET.search(ln)
        if m:
            found.append(f"betting-line term in a data row: {m.group(0)!r}")
        for g in late:
            hs, as_ = g["home_score"], g["away_score"]
            if (re.search(rf"\b{g['home']}\b", ln) and re.search(rf"\b{g['away']}\b", ln)
                    and re.search(rf"(?<![\d.-]){hs}\s*[-–]\s*{as_}(?![\d.])|(?<![\d.-]){as_}\s*[-–]\s*{hs}(?![\d.])", ln)):
                found.append(f"final score of a game not final at as_of: {g['away']}@{g['home']}")
    return found
