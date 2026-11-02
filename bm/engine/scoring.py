"""Scoring from box-score lines (nflverse weekly player and team stats). Pure."""
from __future__ import annotations

from typing import Mapping

from .rules import Rules


def _f(line: Mapping, k: str) -> float:
    v = line.get(k)
    if v in (None, "", "NA"):
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def score_offense(rules: Rules, line: Mapping) -> float:
    s = rules.scoring
    pts = 0.0
    for k in ("passing_yards", "passing_tds", "passing_interceptions", "passing_2pt_conversions",
              "rushing_yards", "rushing_tds", "rushing_2pt_conversions", "receptions", "receiving_yards",
              "receiving_tds", "receiving_2pt_conversions", "special_teams_tds", "fumble_recovery_tds"):
        pts += s.get(k, 0) * _f(line, k)
    fumbles_lost = _f(line, "rushing_fumbles_lost") + _f(line, "receiving_fumbles_lost") + _f(line, "sack_fumbles_lost")
    pts += s.get("fumbles_lost", 0) * fumbles_lost
    return pts


def score_kicker(rules: Rules, line: Mapping) -> float:
    k = rules.kicking
    short = _f(line, "fg_made_0_19") + _f(line, "fg_made_20_29") + _f(line, "fg_made_30_39")
    mid = _f(line, "fg_made_40_49")
    long_ = _f(line, "fg_made_50_59") + _f(line, "fg_made_60_")
    return (k["fg_made_0_39"] * short + k["fg_made_40_49"] * mid + k["fg_made_50_plus"] * long_
            + k["fg_missed"] * _f(line, "fg_missed") + k["pat_made"] * _f(line, "pat_made")
            + k["pat_missed"] * _f(line, "pat_missed"))


def points_allowed_score(rules: Rules, allowed: float) -> float:
    for mx, pts in rules.defense["points_allowed"]:
        if allowed <= mx:
            return float(pts)
    return float(rules.defense["points_allowed"][-1][1])


def score_defense(rules: Rules, team_line: Mapping, points_allowed: float) -> float:
    d = rules.defense
    blocks = _f(team_line, "def_punt_blocks") + _f(team_line, "def_fg_blocks") + _f(team_line, "def_pat_blocks")
    return (d["def_sacks"] * _f(team_line, "def_sacks") + d["def_interceptions"] * _f(team_line, "def_interceptions")
            + d["fumble_recovery_opp"] * _f(team_line, "fumble_recovery_opp") + d["def_tds"] * _f(team_line, "def_tds")
            + d["special_teams_tds"] * _f(team_line, "special_teams_tds") + d["def_safeties"] * _f(team_line, "def_safeties")
            + d["blocked_kicks"] * blocks + points_allowed_score(rules, points_allowed))


def score_player_line(rules: Rules, pos: str, line: Mapping) -> float:
    """`line` is an nflverse weekly row; for DEF it must carry `points_allowed`."""
    if pos == "DEF":
        return round(score_defense(rules, line, _f(line, "points_allowed")), 2)
    pts = score_offense(rules, line)
    if pos == "K":
        pts += score_kicker(rules, line)
    return round(pts, 2)


def score_week(rules: Rules, players: Mapping[str, Mapping], lineups: Mapping[str, Mapping[str, str | None]],
               lines: Mapping[str, Mapping]) -> dict:
    """lineups: team -> {slot: pid}, the lineup in force when each player locked.
    lines: pid -> weekly stat line (missing = did not play = 0).
    Returns team -> {"points": total, "starters": [{slot, player, points}]}"""
    out = {}
    for tid in sorted(lineups):
        rows = []
        total = 0.0
        for slot, pid in lineups[tid].items():
            pts = 0.0
            if pid and pid in lines:
                pts = score_player_line(rules, players[pid]["pos"], lines[pid])
            rows.append({"slot": slot, "player": pid, "points": pts})
            total += pts
        out[tid] = {"points": round(total, 2), "starters": rows}
    return out


def standings(team_ids: list[str], scores: Mapping[str, Mapping[str, float]], seed: str | None = None) -> list[dict]:
    """Total points (primary), all-play W-L (secondary), then a seeded draw (the league seed) when `seed` is
    given; without a seed the old team-id order applies (kept for weeks before 5, so replays are unchanged).
    scores: week -> team -> points."""
    import hashlib
    draw = (lambda t: hashlib.sha256(f"{seed}:{t}".encode()).hexdigest()) if seed is not None else (lambda t: t)
    tot = {t: 0.0 for t in team_ids}
    apw = {t: 0 for t in team_ids}
    apl = {t: 0 for t in team_ids}
    hist: dict[str, list[int]] = {t: [] for t in team_ids}
    for wk in sorted(scores, key=int):
        sc = scores[wk]
        for t in team_ids:
            tot[t] += sc.get(t, 0.0)
            for o in team_ids:
                if o == t:
                    continue
                if sc.get(t, 0) > sc.get(o, 0):
                    apw[t] += 1
                elif sc.get(t, 0) < sc.get(o, 0):
                    apl[t] += 1
        ranked = sorted(team_ids, key=lambda x: (-tot[x], -apw[x], draw(x)))
        for i, t in enumerate(ranked):
            hist[t].append(i + 1)
    ranked = sorted(team_ids, key=lambda x: (-tot[x], -apw[x], draw(x)))
    return [{"team_id": t, "rank": i + 1, "points": round(tot[t], 2), "all_play_w": apw[t], "all_play_l": apl[t],
             "rank_history": hist[t]} for i, t in enumerate(ranked)]
