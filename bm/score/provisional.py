"""Read-only partial fantasy scores. Never settle, advance or append league events.

python3 -m bm.score.provisional --root RETAINED_ROOT --week 5
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path

from bm.engine.scoring import score_player_line


def complete_line(rules, pos: str, line: dict) -> bool:
    """The settled scorer's missing-field zero convention is unsafe for partial feeds."""
    if pos == "DEF":
        fields = {k for k, value in rules.defense.items() if value and k not in ("points_allowed", "blocked_kicks")}
        fields.add("points_allowed")
        if rules.defense.get("blocked_kicks"):
            fields.update(("def_punt_blocks", "def_fg_blocks", "def_pat_blocks"))
    else:
        fields = {k for k, value in rules.scoring.items() if value and k != "fumbles_lost"}
        if rules.scoring.get("fumbles_lost"):
            fields.update(("rushing_fumbles_lost", "receiving_fumbles_lost", "sack_fumbles_lost"))
        if pos == "K":
            fields.update(("fg_made_0_19", "fg_made_20_29", "fg_made_30_39", "fg_made_40_49",
                           "fg_made_50_59", "fg_made_60_", "fg_missed", "pat_made", "pat_missed"))
    try:
        return all(math.isfinite(float(line[k])) for k in fields)
    except (KeyError, ValueError, TypeError):
        return False


def partial_week(rules, world, lineups: dict, events: list[dict], games: list[dict], lines: dict,
                 week: int, asof: datetime, refs: list[dict]) -> dict:
    """Only retained kickoff-locked starters score; missing evidence is never zero."""
    if not games:
        raise ValueError("week schedule unavailable; cannot infer bye/no-game zeros")
    by_team = {t: g for g in games for t in (g["home"], g["away"])}
    locked, lock_refs = {}, []
    for e in events:
        if e.get("type") != "lineup_lock" or e.get("week") != week or datetime.fromisoformat(e["ts"]) > asof:
            continue
        lock_refs.append({k: e.get(k) for k in ("seq", "ts", "kickoff", "state_hash", "logged_at")})
        for tid, slots in e["locked"].items():
            for slot, pid in slots.items():
                previous = locked.setdefault(tid, {}).get(slot)
                if previous is not None and previous != pid:
                    raise ValueError(f"conflicting retained lineup locks: {tid}/{slot}")
                locked[tid][slot] = pid
    result = {}
    for tid, lineup in lineups.items():
        starters, known = [], []
        for slot in rules.starter_slots:
            frozen = slot in locked.get(tid, {})
            pid = locked.get(tid, {}).get(slot, lineup.get(slot))
            player = world.players.get(pid, {})
            nfl_team = ((world.team_by_week or {}).get(week) or {}).get(pid, player.get("team"))
            game = by_team.get(nfl_team)
            kickoff = datetime.fromisoformat(game["kickoff"]) if game else None
            if game is None:
                game_status = "no_scheduled_game" if player else "unknown"
            elif kickoff > asof:
                game_status = "not_started"
            elif game["home_score"] is not None and game["away_score"] is not None and (asof-kickoff).total_seconds() >= 4*3600:
                game_status = "finished"
            else:
                game_status = "in_progress_or_result_pending"
            points = None
            line = lines.get(pid)
            if not pid:
                status = "empty_slot"
                points = 0.0  # confirmed absence of a starter, not absent stats
            elif not player:
                status = "missing_player_identity"
            elif kickoff is None:
                status = "no_scheduled_game"
                points = 0.0  # known bye/no game in the retained schedule
            elif kickoff > asof:
                status = "not_started"
            elif not frozen:
                status = "missing_lineup_lock"
            elif line is not None and line.get("game_id") == game["game_id"] and complete_line(rules, player["pos"], line):
                points = score_player_line(rules, player["pos"], line)
                status = "stats_available"
            elif game_status == "finished":
                status = "missing_stats"
            else:
                status = "in_progress_or_result_pending"
            if points is not None:
                known.append(points)
            starters.append({"slot": slot, "player_id": pid, "name": player.get("name"), "points": points,
                             "status": status, "game_status": game_status, "game_id": game.get("game_id") if game else None,
                             "lineup_source": "retained_kickoff_lock" if frozen else "current_unlocked_lineup"})
        counts = Counter(r["status"] for r in starters)
        result[tid] = {"known_points": round(sum(known), 2) if any(r["player_id"] and r["points"] is not None for r in starters) else None, "total_points": None,
                       "covered_starters": counts["stats_available"],
                       "pending_starters": counts["not_started"] + counts["in_progress_or_result_pending"],
                       "missing_starters": counts["missing_stats"] + counts["missing_lineup_lock"] + counts["missing_player_identity"],
                       "no_game_or_empty_slots": counts["no_scheduled_game"] + counts["empty_slot"],
                       "complete": all(r["points"] is not None for r in starters), "starters": starters}
    return {"schema": "provisional-fantasy/1", "week": week, "asof": asof.isoformat(),
            "status": "partial_unsettled", "interpretation": "Known fantasy points only; missing/pending stats are not zero. Not final standings or model ability ranks.",
            "game_status_basis": "Retained schedule; finished requires reported scores plus elapsed four hours (inferred, not live final-status feed).",
            "data_refs": [{k: r.get(k) for k in ("asset", "retrieved_at", "source_updated_at", "sha256")} for r in refs],
            "lineup_lock_refs": lock_refs, "teams": result}


def league_partial(lg, week: int, asof: datetime) -> dict:
    from bm.stats import nflverse as nv
    from bm.league.context import engine_world
    world = engine_world()
    state = lg.state(world)
    lines, refs = nv.weekly_lines(lg.cfg.get("season", 2026), asof, through_week=week, schedule_as_of=asof)
    games = [g for g in nv.schedule(lg.cfg.get("season", 2026), asof) if g["week"] == week]
    schedule_ref = nv.snapshot_for("games", asof)
    report = partial_week(lg.rules, world, {t: tm["lineup"] for t, tm in state["teams"].items()},
                          lg.events(), games, lines.get(week, {}), week, asof, refs + ([schedule_ref] if schedule_ref else []))
    report["league_id"] = lg.id
    report["rules_hash"] = lg.rules.rules_hash
    report["settled_scores_unchanged"] = state["scores"]
    for tid, row in report["teams"].items():
        row["entrant_id"] = lg.entrant(tid, week)
    report["display_order"] = sorted(report["teams"], key=lambda t: (
        report["teams"][t]["known_points"] is None, -(report["teams"][t]["known_points"] or 0), t))
    report["order_basis"] = "Known points so far, with unequal remaining starters; not final standings or ability ranking."
    return report


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="Existing retained checkout; read-only")
    parser.add_argument("--week", type=int, required=True)
    parser.add_argument("--league", action="append")
    parser.add_argument("--asof", help="ISO8601 aware timestamp; defaults to current UTC")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    from bm.stats import nflverse as nv
    from bm.league import season
    nv.ROOT, nv.RAW = root, root / "data/raw/nfl"
    nv.MANIFEST, nv.SNAP = nv.RAW / "manifest.jsonl", nv.RAW / "snapshots"
    season.LEAGUES = root / "leagues"
    now = datetime.fromisoformat(args.asof) if args.asof else datetime.now(timezone.utc)
    if now.tzinfo is None:
        parser.error("asof must include timezone")
    ids = args.league or [d.name for d in sorted(season.LEAGUES.iterdir())
                         if (d / "league.json").exists() and json.loads((d / "league.json").read_text()).get("status") == "private-live-season"]
    print(json.dumps({"asof": now.isoformat(), "week": args.week,
                      "leagues": [league_partial(season.League(lid), args.week, now) for lid in ids]}, sort_keys=True))


if __name__ == "__main__":
    main()
