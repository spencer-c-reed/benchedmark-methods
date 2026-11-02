"""The Agentic composite (D-PF-1), from full-game league logs and their wake session records.

Components (each per seat, higher is better):
  1 event_response   D-SNK-9 traces. Event = bad news about one of the seat's starters (an owner-first item, or a status of
                     Out/Doubtful/IR seen before his kickoff). Trace = noticed (a wake opened after the event, before the
                     lock), verified (that wake looked the player up: player_report/injury_report/news on him), decided (a
                     lineup action resolved before the lock). Score = mean of the three over events.
  2 self_initiated   share of the seat's own lineup actions made in wakes it scheduled or subscribed to (not the engine's
                     one counted attempt).
  3 deadline         1 - share of locks where a starter it could see was Out/IR/on bye stayed in the lineup while it had
                     budget to wake.
  4 commitments      notebook commitments acted on or revised: needs the judges; "insufficient" until graded.
  5 procedure        1 - (refused actions + failed tool calls) / (actions + tool calls).
  6 self_prediction  minus the mean absolute error of its book's self.expected_points against the points it scored.
Combining: z-scores across the seat pool, each shrunk by n/(n+5), equal weights over the components a seat has at its
minimum n; fewer than 4 components at minimum n is "insufficient" (not ranked). Each component is shown beside the headline.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from bm.log import ChainLog

ROOT = Path(__file__).resolve().parents[2]
MIN_N = {"event_response": 3, "self_initiated": 3, "deadline": 3, "procedure": 10, "self_prediction": 2}
BAD = ("Out", "Doubtful", "IR")
LOOKUPS = ("player_report", "injury_report", "news_search", "news_item")


def _instant(value: str) -> datetime:
    """Parse retained timestamps as instants, preserving offset semantics."""
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def _failure_evidence(rec: dict | None, fallback: str) -> tuple[datetime, datetime]:
    """First retained failure observation and the wake's retained cutoff.

    A failed wake may have completed model turns and tools before its lane dies.
    Starting attribution at wake_open would erase that real work, so use the
    terminal failed turn's retained failure receipts.  Old records without that
    evidence fall back to the wake_failed timestamp, never to wake_open.  The
    cutoff falls back to that same terminal timestamp rather than inventing a
    later opportunity.
    """
    if rec:
        for turn in reversed(rec.get("turns") or []):
            failure = turn.get("failure") or {}
            if failure.get("layer") != "infra":
                continue
            seen = []
            deadlines = []
            for hold in turn.get("hold_attempts") or []:
                if not hold.get("error"):
                    continue
                route_seen = []
                for route in hold.get("route_history") or []:
                    if route.get("decision_deadline"):
                        deadlines.append(_instant(route["decision_deadline"]))
                    for attempt in route.get("attempt_history") or []:
                        if attempt.get("error") and attempt.get("ended_at"):
                            route_seen.append(_instant(attempt["ended_at"]))
                    if route.get("error") and route.get("completed_at"):
                        route_seen.append(_instant(route["completed_at"]))
                seen.extend(route_seen)
                if not route_seen and hold.get("at"):
                    # Immediate failures such as a tripped breaker can have no
                    # provider attempt; the hold receipt is then the evidence.
                    seen.append(_instant(hold["at"]))
            if seen:
                start = min(seen)
                cutoff = max((d for d in deadlines if d >= start), default=_instant(fallback))
                return start, max(start, cutoff)
    terminal = _instant(fallback)
    return terminal, terminal


def infrastructure_windows(evs: list[dict], recs: list[dict]) -> tuple[set[str], dict[str, list[dict]]]:
    """Return failed wake ids and bounded infrastructure-attribution windows.

    Recovery is established only by an own-outcome wake reached through the
    retained retry schedule chain.  A missing recovery stays unknown.  Every
    window is bounded by the failed turn's retained decision deadline, so an
    outage cannot erase later, unrelated opportunities.
    """
    opens = {e["wake_id"]: e for e in evs if e["type"] == "wake_open"}
    failures = {e["wake_id"]: e for e in evs if e["type"] == "wake_failed"}
    closes = {e["wake_id"]: e for e in evs if e["type"] == "wake_close"}
    records = {r["key"]: r for r in recs if r.get("kind") == "wake"}
    retry_schedule = {e["wake_id"]: e["id"] for e in evs
                      if e["type"] == "wake_schedule" and e.get("retry") and e.get("op") == "schedule"}
    by_schedule = {e.get("schedule_id"): e for e in opens.values() if e.get("schedule_id")}
    windows = defaultdict(list)
    for wid, failed in failures.items():
        opened = opens.get(wid)
        if not opened:
            continue
        start, cutoff = _failure_evidence(records.get(wid), failed["ts"])
        recovery = None
        current, seen = wid, {wid}
        while current in retry_schedule:
            nxt = by_schedule.get(retry_schedule[current])
            if not nxt or nxt["wake_id"] in seen:
                break
            current = nxt["wake_id"]
            seen.add(current)
            close = closes.get(current)
            if current not in failures and close and close.get("outcome") == "own":
                recovery = _instant(close["ts"])
                break
        windows[opened["team"]].append({"start": start, "recovery": recovery, "cutoff": cutoff, "wake_id": wid})
    return set(failures), windows


def infrastructure_at(windows: dict[str, list[dict]], tid: str, instant: datetime) -> bool:
    instant = instant.astimezone(timezone.utc)
    return any(w["start"] <= instant
               and instant <= w["cutoff"]
               and (w["recovery"] is None or instant < w["recovery"])
               for w in windows.get(tid, []))


def _scorable_wake(wid: str, failed: set[str], tools_by_wake: dict, actions_by_wake: dict) -> bool:
    """A lane failure erases only a wake with no retained agent behavior."""
    return wid not in failed or bool(tools_by_wake.get(wid) or actions_by_wake.get(wid))


def components(league_id: str) -> dict[str, dict]:
    from bm.league.agentleague import AgentLeague
    lg = AgentLeague(league_id)
    evs = lg.events()
    recs = [r["payload"] for r in ChainLog(ROOT / "leagues" / league_id / "sessions.jsonl").rows()] if (ROOT / "leagues" / league_id / "sessions.jsonl").exists() else []
    failed_wakes, outages = infrastructure_windows(evs, recs)
    actions_by_wake = defaultdict(list)
    for e in evs:
        if e["type"] in ("action_submitted", "action_resolved") and e.get("wake_id"):
            actions_by_wake[e["wake_id"]].append(e)
    tools_by_wake = {r["key"]: r.get("tools") or [] for r in recs if r.get("kind") == "wake"}
    wakes = defaultdict(list)
    for e in evs:
        if e["type"] == "wake_open" and _scorable_wake(e["wake_id"], failed_wakes, tools_by_wake, actions_by_wake):
            wakes[e["team"]].append(e)
    from bm import wire
    wire_bad = [wire.news_item(i["id"], datetime.now().astimezone()) for i in wire.news_search("", datetime.now().astimezone(), limit=100000)
                if any(b in i["headline"] for b in ("Out", "Doubtful", "IR", "Did Not Participate"))]
    out = {}
    scored = {}
    for e in evs:
        if e["type"] == "week_scored":
            scored[e["week"]] = e
    from bm import succession
    for tid, seat in lg.seats.items():
        if seat.get("house") or lg.is_bot(tid, lg.cfg["start_week"]):
            continue
        # a seat a bot operator runs from a week (config/successions.yaml kind: operator): only the model's own weeks
        op = next((s for s in succession.load()["successions"] if s["kind"] == "operator" and s["retired"] == seat["entrant_id"]
                   and lg.id in s["leagues"]), None)
        until_week = int(op["from_week"]) if op else None
        until_at = op["from_at"] if op else None

        def own_week(w, until_week=until_week):
            return until_week is None or (w is not None and int(w) < until_week)

        def own_time(ts, until_at=until_at):
            return until_at is None or _instant(ts) < _instant(until_at)
        c = {}
        # 1 event response: bad news about one of its starters before his kickoff: owner-first items (G3/G4) and wire
        # items (every league) about a player who was in its lineup when his game locked
        locked = {}
        for lk in (e for e in evs if e["type"] == "lineup_lock" and own_week(e.get("week"))):
            for pid in (lk.get("locked") or {}).get(tid, {}).values():
                locked[pid] = lk["kickoff"]
        events = [(e["player"], e["ts"]) for e in evs if e["type"] == "private_item" and e["team"] == tid and e.get("bad_news")]
        events += [(i["gsis_id"], i["first_seen_at"]) for i in wire_bad if i.get("gsis_id") in locked
                   and _instant(i["first_seen_at"]) < _instant(locked[i["gsis_id"]])
                   and _instant(i["first_seen_at"]) >= _instant(lg.cfg["draft_as_of"])]
        traces = []
        for pid, ts in sorted({x for x in events if own_time(x[1])}, key=lambda x: x[1]):
            t0, lock = _instant(ts), _instant(locked.get(pid, "2100-01-01T00:00:00+00:00"))
            ws = [w for w in wakes[tid] if t0 <= _instant(w["started_at"]) < lock]
            verified = any(any(pid in json.dumps(x.get("args")) and x["tool"] in LOOKUPS for x in tools_by_wake.get(w["wake_id"], [])) for w in ws)
            decided = any(a["type"] == "action_resolved" and a["team"] == tid and a["kind"] == "set_lineup" and a["ok"]
                          and t0 <= _instant(a["ts"]) < lock for a in evs)
            if ws or verified or decided or not infrastructure_at(outages, tid, lock):
                traces.append((bool(ws) + verified + decided) / 3)
        if traces:
            c["event_response"] = (sum(traces) / len(traces), len(traces))
        # 2 self-initiated share
        mine = [w for w in wakes[tid] if w.get("kind") in ("scheduled", "event", "engine")]
        if mine:
            c["self_initiated"] = (sum(1 for w in mine if w["kind"] in ("scheduled", "event")) / len(mine), len(mine))
        # 3 deadline discipline: of the starters it let lock while it had budget to wake, the share it could see were
        # Out/Doubtful/IR or on bye is the miss rate
        from bm.agent import budget as bud
        pol = bud.policy_for(lg.cfg, seat.get("tier", 1.0))
        opp = miss = 0
        for lk in (e for e in evs if e["type"] == "lineup_lock" and own_week(e.get("week"))):
            if bud.balance(pol, [x for x in evs if _instant(x["ts"]) <= _instant(lk["ts"])], tid, lk["week"])["exhausted"]:
                continue
            v = lg.view(lk["week"], datetime.fromisoformat(lk["ts"]))
            for pid in (lk.get("locked") or {}).get(tid, {}).values():
                bad = (v.proj.get(pid, {}).get("status") in BAD) or v.world.players.get(pid, {}).get("team") not in v.opp
                if bad and infrastructure_at(outages, tid, _instant(lk["ts"])):
                    # Attribute only the miss the outage could have caused;
                    # healthy starters at the same lock remain real opportunities.
                    continue
                opp += 1
                miss += bad
        if opp:
            c["deadline"] = (1 - miss / opp, opp)
        # 5 procedure
        acts = [e for e in evs if e["type"] == "action_resolved" and e["team"] == tid]
        tcalls = [x for w in wakes[tid] for x in tools_by_wake.get(w["wake_id"], [])]
        n = len(acts) + len(tcalls)
        if n:
            bad = sum(1 for a in acts if not a["ok"]) + sum(1 for x in tcalls if not x.get("ok"))
            c["procedure"] = (1 - bad / n, n)
        # 6 self-prediction
        errs = []
        for b in (e for e in evs if e["type"] == "forecast_filed" and e["team"] == tid and own_week(e.get("week"))):
            exp = ((b["book"].get("self") or {}).get("expected_points"))
            s = scored.get(b["week"])
            if isinstance(exp, (int, float)) and s and tid in s["points"]:
                errs.append(abs(exp - s["points"][tid]))
        if errs:
            c["self_prediction"] = (-sum(errs) / len(errs), len(errs))
        out[tid] = {"entrant": seat["entrant_id"], "components": c}
    return out


def composite(rows: list[dict]) -> list[dict]:
    """rows: [{entrant, components: {name: (value, n)}}] across the pool -> headline per row (or insufficient)."""
    names = sorted({k for r in rows for k in r["components"]})
    stats = {}
    for k in names:
        xs = [r["components"][k][0] for r in rows if k in r["components"] and r["components"][k][1] >= MIN_N.get(k, 1)]
        if len(xs) >= 2:
            mu = sum(xs) / len(xs)
            sd = math.sqrt(sum((x - mu) ** 2 for x in xs) / (len(xs) - 1)) or 1.0
            stats[k] = (mu, sd)
    out = []
    for r in rows:
        zs = {}
        for k, (v, n) in r["components"].items():
            if k in stats and n >= MIN_N.get(k, 1):
                mu, sd = stats[k]
                zs[k] = round((v - mu) / sd * n / (n + 5), 3)
        ok = len(zs) >= 4
        out.append({**r, "z": zs, "headline": round(sum(zs.values()) / len(zs), 3) if ok else None, "ranked": ok,
                    "unranked_reason": None if ok else f"insufficient: {len(zs)} of 4 components at minimum n (commitments await the judges)"})
    return out


if __name__ == "__main__":
    import sys
    rows = [dict(v, team=t) for lid in sys.argv[1:] for t, v in components(lid).items()]
    for r in composite(rows):
        print(r["team"], r["entrant"], r["headline"], {k: (round(v[0], 3), v[1]) for k, v in r["components"].items()}, r.get("unranked_reason") or "")
