"""Counterfactual alternatives (Q3) and lineup TMV.

Every lineup decision is paired with the lineups four reference policies would have set on the same state at the
same moment, each scored with the week's real stat lines:
  projection  the Projection Bot's rule (`bots.policy_lineup`: our projection as of the decision, bye/Out benched);
  consensus   the market's view: the consensus ranking visible at the decision (`baserates.consensus_as_of`; the
              captured consensus is a rest-of-season rank, so this is "start the best-ranked eligible players";
              unavailable before the first consensus snapshot, and then reported as such, never filled in);
  autopilot   each consumer autopilot (`bots.AUTOPILOTS`; their boards as of the decision, with the documented
              fallback to our projection when no board was visible: `bots.provenance` is recorded);
  hindsight   the best lineup the roster allowed given the locks at decision time (players already locked stay).
Copying the projection scores exactly 0 against it, so "points over the projection alternative" is market-relative.

Lineup TMV for a team-week (§6.2 component 4) is the lineup that actually scored minus the Projection Bot's lineup on
the same roster at the team's last lineup decision of the week (locks as of that moment), both scored with real lines.
Other teams' actions are held fixed.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from bm.agent import bots
from bm.engine.defaults import best_lineup
from bm.engine.rules import load_rules
from bm.engine.scoring import score_player_line
from bm.engine.state import apply_event, genesis_state
from bm.league.ceiling import optimal_lineup
from bm.league.context import build_view, engine_world
from bm.log import ChainLog
from bm.stats import nflverse as nv

ROOT = Path(__file__).resolve().parents[2]


def week_points(rules, world, week: int, now: datetime | None = None) -> dict[str, float]:
    """Real fantasy points per player for `week` (newest stat lines; corrections included)."""
    lines, _ = nv.weekly_lines(2026, now, through_week=week)
    out = {}
    for pid, ln in (lines.get(week) or {}).items():
        p = world.players.get(pid)
        if p:
            out[pid] = score_player_line(rules, p["pos"], ln)
    return out


def lineup_points(rules, lineup: dict, pts: dict[str, float]) -> float:
    return round(sum(pts.get(lineup.get(s) or "", 0.0) for s in rules.starter_slots), 2)


def _consensus_lineup(rules, view, state, tid):
    from bm.stats import baserates
    cons = baserates.consensus_as_of(view.as_of)
    if not cons:
        return None
    # rest-of-season rank -> a start score: better rank first; no game this week (bye, Out) -> never started
    score = {p: (1000.0 - (cons[p]["ecr"] or 999.0)) if p in cons and view.proj.get(p, {}).get("week_proj", 0) > 0 else -1.0
             for p in state["teams"][tid]["roster"]}
    return best_lineup(rules, view.world, state, tid, score, view.as_of)


def projection_bot_lineup(rules, view, state, tid: str) -> dict:
    """The Projection Bot reference: the bot's own lineup policy, never raw week_proj."""
    return bots.policy_lineup(rules, view, state, tid)


def alternatives(rules, view, state, tid: str, pts: dict[str, float]) -> dict:
    """The reference lineups for one team at one state and moment, each with its real points."""
    w = state["week"]
    out = {"projection": projection_bot_lineup(rules, view, state, tid)}
    c = _consensus_lineup(rules, view, state, tid)
    out["consensus"] = c
    for kind in sorted(bots.AUTOPILOTS):
        out[f"autopilot:{kind}"] = bots.autopilot_lineup(kind, rules, view, state, tid, "cf")
    # hindsight with the locks of this moment: locked starters stay, the rest is the best the roster allowed
    team = state["teams"][tid]
    locked = {s: p for s, p in team["lineup"].items() if p and view.world.locked(w, p, view.as_of)}
    free = [p for p in team["roster"] if p not in locked.values() and not view.world.locked(w, p, view.as_of)]
    best = optimal_lineup(rules, view.world, free, w, pts)
    hs = dict(locked)
    pool = [p for s in rules.starter_slots for p in [best.get(s)] if p]
    for s in rules.starter_slots:
        if s in hs:
            continue
        cand = next((p for p in sorted(pool, key=lambda p: -pts.get(p, 0.0)) if p not in hs.values() and rules.eligible(s, view.world.pos(p))), None)
        if cand is None:
            cand = next((p for p in sorted(free, key=lambda p: -pts.get(p, 0.0)) if p not in hs.values() and rules.eligible(s, view.world.pos(p))), None)
        hs[s] = cand
    out["hindsight"] = {s: hs.get(s) for s in rules.starter_slots}
    return {k: (None if v is None else {"lineup": v, "points": lineup_points(rules, v, pts)}) for k, v in out.items()}


def league(league_id: str, weeks: list[int] | None = None) -> dict:
    """Alternatives for every lineup decision in a league's scored weeks, plus lineup TMV per team-week."""
    lg = json.loads((ROOT / "leagues" / league_id / "league.json").read_text())
    rules = load_rules(lg["rules_file"])
    world = engine_world()
    evs = ChainLog(ROOT / "leagues" / league_id / "log.jsonl").payloads()
    agent = lg.get("mode") == "agent"
    from bm import succession
    seat_of = {s["team_id"]: s["entrant_id"] for s in lg["seats"]}
    scored = {}
    for e in evs:
        if e["type"] == "week_scored":
            scored[e["week"]] = e                               # last revision wins
    weeks = [w for w in (weeks or sorted(scored)) if w in scored]
    pts = {w: week_points(rules, world, w) for w in weeks}
    views: dict = {}

    def view_at(w, ts):
        k = (w, ts)
        if k not in views:
            views[k] = build_view(rules, w, datetime.fromisoformat(ts))
        return views[k]

    from bm.ops.served_model import misserved_ids, misserved_team_weeks
    nobody, nobody_weeks = misserved_ids(evs), misserved_team_weeks(evs)   # served by another model: no one's
    decisions, last_state = [], {}
    st = None
    for e in evs:
        if e["type"] == "genesis":
            st = genesis_state(rules, e["league_id"], e["team_ids"], e["draft_order"], e["week"])
            continue
        if agent and e["type"] == "lineup_set" and e.get("source") == "model" and st["week"] in weeks:
            # agent leagues: a lineup decision is the seat's own lineup_set at the step it resolved; its own lineup is the
            # state after applying it, and the alternatives are computed on the state just before, at that moment
            after = apply_event(rules, world, st, e)
            v = view_at(st["week"], e["ts"])
            alts = alternatives(rules, v, st, e["team"], pts[st["week"]])
            own_pts = lineup_points(rules, after["teams"][e["team"]]["lineup"], pts[st["week"]])
            decisions.append({"decision_id": e.get("decision_id"), "team": e["team"], "entrant": succession.controller(seat_of[e["team"]], league=league_id, week=st["week"]) if e["team"] in seat_of else None, "week": st["week"],
                              "own_points": own_pts, "forced_default": False,
                              "alternatives": {k: (None if a is None else a["points"]) for k, a in alts.items()},
                              "diff": {k: (None if a is None else round(own_pts - a["points"], 2)) for k, a in alts.items()},
                              "consensus_available": alts["consensus"] is not None})
            last_state[(e["team"], st["week"])] = (st, e["ts"])
        if e["type"] == "decision" and e.get("kind") == "start_sit" and e.get("week") in weeks:
            v = view_at(e["week"], e["ts"])
            alts = alternatives(rules, v, st, e["team"], pts[e["week"]])
            own = {s: (e.get("action") or {}).get("lineup", {}).get(s) for s in rules.starter_slots}
            own_pts = lineup_points(rules, own, pts[e["week"]])
            decisions.append({"decision_id": e["decision_id"], "team": e["team"], "entrant": e["entrant"], "week": e["week"],
                              "own_points": own_pts, "forced_default": bool(e.get("forced_default")),
                              "unattributed": e["decision_id"] in nobody,
                              "alternatives": {k: (None if a is None else a["points"]) for k, a in alts.items()},
                              "diff": {k: (None if a is None else round(own_pts - a["points"], 2)) for k, a in alts.items()},
                              "consensus_available": alts["consensus"] is not None})
            last_state[(e["team"], e["week"])] = (st, e["ts"])      # apply_event never mutates: st is a snapshot
        if e["type"] in ("week_scored",) and e["week"] in weeks:
            # teams with no lineup decision this week: the Projection Bot reference is taken at the week's lock state
            for t in st["teams"]:
                last_state.setdefault((t, e["week"]), (st, None))
        st = apply_event(rules, world, st, e)

    from bm import succession
    tmv = []
    seats = {s["team_id"]: s["entrant_id"] for s in lg["seats"]}
    for w in weeks:
        det = scored[w]["detail"]
        for t, d in sorted(det.items()):
            actual = round(sum(r["points"] for r in d["starters"]), 2)
            s, ts = last_state.get((t, w), (None, None))
            if s is None:
                continue
            ts = ts or _week_default_ts(w)
            v = view_at(w, ts)
            pb = projection_bot_lineup(rules, v, s, t)
            pb_pts = lineup_points(rules, pb, pts[w])
            tmv.append({"team": t, "entrant": succession.controller(seats.get(t), league=league_id, week=w) if seats.get(t) else None, "week": w, "actual": actual, "projection_bot": pb_pts,
                        "lineup_tmv": round(actual - pb_pts, 2), "reference_at": ts,
                        # a bot operator's team-week is no entrant's (rules/policies.md seat_operators)
                        "unattributed": (t, w) in nobody_weeks or bool(seats.get(t) and succession.operated(seats[t], league=league_id, week=w))})
    return {"league": league_id, "weeks": weeks, "decisions": decisions, "lineup_tmv": tmv}


def _week_default_ts(week: int) -> str:
    from bm.league.season import week_steps
    return next(d for k, d in week_steps(week) if k == f"lineup:{week}:2").isoformat()


def main(argv: list[str] | None = None) -> int:
    import sys
    argv = argv if argv is not None else sys.argv[1:]
    ids = argv or sorted(p.name for p in (ROOT / "leagues").iterdir() if (p / "log.jsonl").exists())
    allrows = []
    for lid in ids:
        r = league(lid)
        allrows.append(r)
        n = len(r["decisions"])
        full = sum(all(r_["alternatives"][k] is not None for k in r_["alternatives"] if k != "consensus") for r_ in r["decisions"])
        own = [d for d in r["decisions"] if not d["forced_default"] and not d.get("unattributed")]
        avg = lambda xs: round(sum(xs) / len(xs), 2) if xs else None
        print(f"{lid}: weeks {r['weeks']} lineup decisions {n} (own {len(own)}), with all non-consensus alternatives {full}, "
              f"consensus visible {sum(d['consensus_available'] for d in r['decisions'])}")
        if own:
            for k in own[0]["diff"]:
                xs = [d["diff"][k] for d in own if d["diff"][k] is not None]
                print(f"   own decision minus {k:28s} mean {avg(xs)}  (n {len(xs)})")
        for t in sorted(r["lineup_tmv"], key=lambda x: -x["lineup_tmv"]):
            print(f"   w{t['week']} {t['team']} {t['entrant']:22s} actual {t['actual']:6.1f}  projection-bot {t['projection_bot']:6.1f}  lineup TMV {t['lineup_tmv']:+6.1f}")
    out = ROOT / "export" / "counterfactuals.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(allrows, indent=1))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
