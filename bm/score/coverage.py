"""Read-only own-decision coverage beside provisional scores.

One obligation, not one attempt, is the denominator. Like scorecard.own_shares,
league decisions, wake sessions and unanswered infrastructure receipts count;
Picks adds one obligation per slate/entrant/instance/step. Classification is
exclusively nodecisions.failure_kind, including the shared effective output caps.
This is operational coverage, not proof of prospective score eligibility.

python3 -m bm.score.coverage --week 5 --out analysis/out/coverage-w5.json
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

from bm import records
from bm.ops import nodecisions
from bm.paths import runtime_root
from bm.score.scorecard import _week_of

CODE_ROOT = Path(__file__).resolve().parents[2]
OURS = ("capacity", "gateway", "our_parse")
KINDS = ("draft_pick", "start_sit", "waiver_bid", "trade_offer", "trade_response",
         "forecast_book", "picks_blind", "picks_line", "wake")


def _read(path: Path, sources: list) -> list[dict]:
    # ChainLog creates its parent even for reads; don't instantiate absent logs.
    if not path.exists():
        return []
    data = path.read_bytes()
    rows = [json.loads(line) for line in data.splitlines() if line.strip()]
    sources.append({"path": str(path), "sha256": hashlib.sha256(data).hexdigest(),
                    "rows": len(rows), "head_hash": rows[-1].get("hash") if rows else None})
    return [dict(r["payload"], seq=r["seq"]) for r in rows]


def _outputs(rec: dict, kind: str, floors: dict) -> list[tuple]:
    cap = 16000 if kind.startswith("picks_") or kind == "wake" else nodecisions.HARNESS_OUTPUT_CAP.get(kind, 3000)
    turns = rec.get("turns") or []
    if turns:
        return [((t.get("usage") or {}).get("output", 0), max(t.get("max_output_tokens") or cap, floors.get(t.get("lane"), 0)))
                for t in turns]
    return [((rec.get("usage") or {}).get("output", 0), cap)]


def _attribution(rec: dict, kind: str, floors: dict) -> tuple[str, str | None]:
    fail = rec.get("failure") or {}
    if rec.get("outcome") == "own" and not fail:
        return "own", None
    code = fail.get("code")
    # runtime's legacy records.failure('unparseable') normalized this to TRANSIENT.
    # Preserve the explicit terminal runtime cause before applying the shared rule.
    if rec.get("outcome") == "model_failed" and rec.get("ended_by") == "unparseable":
        code = "PARSE"
    if not code:
        return "unknown", None
    return nodecisions.failure_kind(code, fail.get("detail", ""), _outputs(rec, kind, floors)), code


def _summary(items: list[dict]) -> dict:
    counts = Counter(i["attribution"] for i in items)
    statuses = Counter(i["status"] for i in items)
    n = len(items)
    return {"n": n, "own": counts["own"], "own_share": round(counts["own"] / n, 6) if n else None,
            "ours": sum(counts[k] for k in OURS), "capacity": counts["capacity"],
            "gateway": counts["gateway"], "our_parse": counts["our_parse"],
            "provider_refusal": counts["refusal"], "model_failure": counts["model"],
            "unknown": counts["unknown"], "pending": counts["pending"],
            "decisions": statuses["decision"] + statuses["forced_default"],
            "forced_defaults": statuses["forced_default"], "missed": statuses["missed"],
            "attempt_records": sum(i.get("attempt_records", 0) for i in items)}


def _cohort(lid: str, cfg: dict) -> str:
    if lid.startswith("_void-"):
        return "void"
    if lid.startswith("nfl-x") or "ended" in (cfg.get("status") or ""):
        return "ended_development"
    if cfg.get("status") == "private-live-season" and cfg.get("format") == "season":
        return "live_season"
    return "separate_cohort"


def build(week: int, root: Path | None = None, *, now: datetime | None = None) -> dict:
    """Read retained data only. No backfill writes, probes, settlement or scheduler calls."""
    root = Path(root) if root is not None else runtime_root(CODE_ROOT)
    now = now or datetime.now(timezone.utc)
    sources, obligations, leagues, warnings = [], [], {}, []
    # Shared nodecisions floors and records adapters come from the code checkout.
    floors = nodecisions._lane_floors()
    import yaml
    adapters = {l["lane"]: l.get("adapter") for l in yaml.safe_load((CODE_ROOT / "config/lanes.yaml").read_text())}
    snapshots = {}
    requests = set()
    for path in sorted((root / "leagues").glob("*/league.json")):
        lid = path.parent.name
        metadata = path.read_bytes()
        cfg = json.loads(metadata)
        sources.append({"path": str(path), "sha256": hashlib.sha256(metadata).hexdigest(), "kind": "league_metadata"})
        events = _read(path.parent / "log.jsonl", sources)
        sessions = _read(path.parent / "sessions.jsonl", sources)
        leagues[lid] = {"cohort": _cohort(lid, cfg), "status": cfg.get("status"), "format": cfg.get("format")}
        snapshots[lid] = (cfg, events, sessions)
        for e in events:
            if e.get("type") == "decision" and e.get("week") == week:
                requests.update(e.get("model_call_ids") or [])
    # Reuse the records index interface, retaining only calls needed for this week.
    # Reading every historical replay into GatewayIndex would waste considerable RAM.
    calls = defaultdict(list)
    gateway_path = root / "logs/gateway.jsonl"
    if gateway_path.exists():
        last = None
        count = 0
        digest = hashlib.sha256()
        with gateway_path.open("rb") as stream:
            for line in stream:
                digest.update(line)
                if not line.strip():
                    continue
                row = json.loads(line)
                count += 1
                last = row
                p = row["payload"]
                if p.get("type") == "model_call" and p.get("request_id") in requests:
                    calls[p["request_id"]].append(dict(p, gateway_seq=row["seq"], gateway_log=str(gateway_path)))
        sources.append({"path": str(gateway_path), "sha256": digest.hexdigest(), "rows": count,
                        "head_hash": last.get("hash") if last else None})
    gw = records.CallsIndex(calls)
    for lid, (cfg, events, sessions) in snapshots.items():
        seats = {s["team_id"]: s["entrant_id"] for s in cfg.get("seats", [])}
        cohort = leagues[lid]["cohort"]
        answers, gaps = {}, defaultdict(list)
        for e in events:
            did = e.get("decision_id")
            if not did:
                continue
            if e["type"] == "decision":
                answers[did] = e
            elif e["type"] in ("decision_expected", "decision_pending", "decision_missed", "decision_skipped_infra"):
                gaps[did].append(e)
        stored = {r["decision_id"]: r for r in sessions if r.get("kind") == "decision" and r.get("decision_id")}
        from bm.ops.served_model import misserved_ids
        nobody = misserved_ids(events)          # served by another model (bm.ops.served_model): no model's obligation
        for did, dec in answers.items():
            if dec.get("week") != week or did in nobody:
                continue
            rec = records.from_decision(dec, cfg, gw, adapters)
            # Persisted turn evidence is available even if a gateway chain is absent.
            old = stored.get(did)
            if not rec["turns"] and old and old.get("decision") == {k: v for k, v in dec.items() if k != "seq"}:
                rec = old
            kind = dec.get("kind") or "unknown"
            attribution, cause = _attribution(rec, kind, floors)
            obligations.append({"decision_id": did, "league": lid, "domain": "league", "cohort": cohort,
                                "entrant_id": dec.get("entrant") or seats.get(dec.get("team")), "team": dec.get("team"),
                                "week": week, "kind": kind, "timing": None, "attribution": attribution,
                                "cause": cause, "raw_cause": (dec.get("forced_default") or {}).get("cause"),
                                "status": "forced_default" if dec.get("forced_default") else "decision",
                                "source_seq": dec["seq"], "attempt_records": 1,
                                "prior_gap_receipts": len(gaps.get(did, []))})
        for did, receipts in gaps.items():
            if did in answers:  # a recovered draft pause is not a second obligation
                continue
            e = receipts[-1]
            if e.get("week") != week:
                continue
            # pending_due closes failed lanes with a generic lane_failed marker.
            # It proves the miss, but must not erase the retained failure evidence.
            fail = next((r for r in reversed(receipts)
                         if r.get("cause") and r["cause"].upper() != "LANE_FAILED"),
                        next((r for r in reversed(receipts) if r.get("cause")), {}))
            code = fail.get("cause")
            attribution = nodecisions.failure_kind(code, fail.get("detail", "")) or "unknown"
            deadline = next((r["hold_until"] for r in reversed(receipts) if r.get("hold_until")), None)
            terminal = next((r for r in reversed(receipts) if r["type"] == "decision_missed"), None)
            open_ = not terminal and deadline and now < datetime.fromisoformat(deadline)
            if open_:
                attribution = "pending"
            obligations.append({"decision_id": did, "league": lid, "domain": "league", "cohort": cohort,
                                "entrant_id": e.get("entrant") or seats.get(e.get("team")), "team": e.get("team"),
                                "week": week, "kind": e.get("kind") or "unknown", "timing": None,
                                "attribution": attribution, "cause": code, "raw_cause": e.get("cause"),
                                "status": "pending" if open_ else "missed", "source_seq": e["seq"],
                                "terminal_source_seq": terminal["seq"] if terminal else None,
                                "attribution_source_seq": fail.get("seq"), "attribution_detail": fail.get("detail", ""),
                                "attempt_records": 0, "gap_receipts": len(receipts), "deadline": deadline,
                                "deadline_status": "retained" if deadline else "legacy cutoff unverified"})
        # Same wake-session units as scorecard.own_shares, plus receipt-backed lost workers.
        wakes = {r.get("key") or r.get("wake_id") or r["record_id"]: r
                 for r in sessions if r.get("kind") == "wake"}
        opens = {e["wake_id"]: e for e in events if e["type"] == "wake_open"}
        closes = {e["wake_id"]: e for e in events if e["type"] in ("wake_close", "wake_failed")}
        for wid in sorted(set(wakes) | set(opens)):
            rec = wakes.get(wid)
            e = opens.get(wid) or closes.get(wid) or rec
            w = rec.get("week") if rec else e.get("week") or _week_of(e, events)
            if w != week:
                continue
            if rec:
                attribution, cause = _attribution(rec, "wake", floors)
                status = "wake"
            elif wid in closes:
                end = closes[wid]
                cause = end.get("cause") or (end.get("ended_by") if end.get("outcome") != "own" else None)
                attribution = nodecisions.failure_kind(cause) or "unknown"
                status = "missed"
            else:
                cause = None
                attribution, status = "pending", "pending"
            obligations.append({"decision_id": f"{lid}:wake:{wid}", "league": lid, "domain": "league", "cohort": cohort,
                                "entrant_id": (rec or e).get("entrant") or seats.get(e.get("team")), "team": e.get("team"),
                                "week": week, "kind": "wake", "timing": None, "attribution": attribution,
                                "cause": cause, "status": status, "source_seq": (rec or e)["seq"],
                                "attempt_records": int(rec is not None)})
        # Agent drafts have pick receipts instead of windowed decision events.
        # Queues/bot picks execute standing policies and aren't fresh obligations.
        if cfg.get("mode") == "agent":
            for e in events:
                if e["type"] != "draft_pick" or e.get("source") in ("queue", "bot") or e.get("decision_id") in answers:
                    continue
                if (e.get("week") or _week_of(e, events)) != week:
                    continue
                src = e.get("source")
                attribution = "own" if src == "model" else "unknown"
                cause = None
                if src in ("infra_default", "model_default"):
                    prior = [r for r in wakes.values() if r.get("team") == e.get("team") and r.get("ts", "") <= e["ts"]]
                    if prior:
                        attribution, cause = _attribution(prior[-1], "wake", floors)
                        if attribution == "own":
                            attribution = "unknown"
                obligations.append({"decision_id": e.get("decision_id") or f"{lid}:draft:{e['team']}:p{e['pick_no']}",
                                    "league": lid, "domain": "league", "cohort": cohort,
                                    "entrant_id": seats.get(e["team"]), "team": e["team"], "week": week,
                                    "kind": "draft_pick", "timing": None, "attribution": attribution,
                                    "cause": cause, "source": src, "source_seq": e["seq"],
                                    "status": "forced_default" if src in ("infra_default", "model_default") else "decision",
                                    "attempt_records": 0})
    for path in sorted((root / "records/picks").glob(f"2026-w{week}-*.jsonl")):
        rows = _read(path, sources)
        match = re.fullmatch(r"2026-w\d+-(early|prelock|london)", path.stem)
        cohort = "live_picks" if match else "separate_picks"
        ctx = next((r for r in rows if r.get("kind") == "slate_context"), None)
        from bm.ops.served_model import annotated_records
        nobody = annotated_records(rows)        # answers another model served: no model's obligation
        by_seat, gone_seats = defaultdict(list), set()
        for r in rows:
            if r.get("kind") == "pick" and r.get("week") == week:
                key = r.get("seat_key") or re.sub(r":attempt\d+$", "", r["key"])
                if r.get("record_id") in nobody:
                    gone_seats.add(key)
                    continue
                by_seat[key].append(r)
        if ctx:
            for model in ctx["models"]:
                for instance in range(1, ctx["instances"] + 1):
                    for step in ("blind", "line"):
                        by_seat.setdefault(f"{path.stem}:{model}:i{instance}:{step}", [])
        else:
            warnings.append(f"{path.stem}: legacy observed seat-step denominator; frozen cohort absent, unattempted seats unknown")
        for key, attempts in sorted(by_seat.items()):
            if not attempts and key in gone_seats:
                continue
            # A later failed attempt must never erase a retained own answer.
            rec = next((r for r in attempts if r.get("outcome") == "own" and not r.get("failure")),
                       attempts[-1] if attempts else None)
            _, model, inst, step = key.rsplit(":", 3)
            kind = f"picks_{step}"
            attribution, cause = _attribution(rec, kind, floors) if rec else ("unknown", None)
            if ctx and attribution == "own":
                from bm.picks.run import completed_attempt, START_BANKROLL
                seat = ctx.get("seat_prompts", {}).get(f"{model}:{inst}", {})
                valid = next((r for r in attempts if completed_attempt(r, ctx["games"], step, seat.get("bankroll", START_BANKROLL))), None)
                if valid:
                    rec = valid
                else:
                    attribution = "unknown"
            obligations.append({"decision_id": f"picks:{key}", "league": None, "slate": path.stem,
                                "domain": "picks", "cohort": cohort, "entrant_id": model,
                                "instance": int(inst[1:]), "team": None, "week": week, "kind": kind,
                                "timing": match[1] if match else (rec or {}).get("timing"), "attribution": attribution,
                                "cause": cause, "status": "pick" if rec else "missed",
                                "source_seq": rec["seq"] if rec else ctx["seq"], "attempt_records": len(attempts),
                                "record_ids": [r.get("record_id") for r in attempts],
                                "receipt_status": "operational answer; prospective score eligibility evaluated separately"})
    obligations.sort(key=lambda i: (i["cohort"], i["decision_id"]))
    grouped = defaultdict(list)
    for item in obligations:
        grouped[(item["entrant_id"], item["week"], item["cohort"], item["domain"], item["kind"], item["timing"])].append(item)
    rows = [dict(entrant_id=e, week=w, cohort=c, domain=d, kind=k, timing=t, **_summary(items))
            for (e, w, c, d, k, t), items in sorted(grouped.items(), key=lambda x: str(x[0]))]
    headline = [i for i in obligations if i["cohort"] in ("live_season", "live_picks")]
    by_entrant = defaultdict(list)
    for item in headline:
        by_entrant[item["entrant_id"]].append(item)
    return {"schema": "own-decision-coverage/1", "week": week, "generated_at": now.isoformat(),
            "runtime_root": str(root), "decision_kinds": list(KINDS),
            "denominator": "unique receipt-backed obligations; retries collapsed; no invented unscheduled decisions",
            "attribution_rule": "bm.ops.nodecisions.failure_kind; capacity/gateway/our_parse are ours; refusal separate",
            "measurement_status": "descriptive operational coverage; small samples; no capability ranking",
            "cohorts": leagues, "totals": _summary(headline),
            "league_totals": _summary([i for i in headline if i["domain"] == "league"]),
            "picks_totals": {t: _summary([i for i in headline if i["domain"] == "picks" and i["timing"] == t])
                             for t in ("early", "prelock", "london")},
            "separate_totals": {c: _summary([i for i in obligations if i["cohort"] == c])
                                for c in sorted({i["cohort"] for i in obligations} - {"live_season", "live_picks"})},
            "entrants": [dict(entrant_id=e, week=week, **_summary(items)) for e, items in sorted(by_entrant.items(), key=lambda x: str(x[0]))],
            "rows": rows, "obligations": obligations, "sources": sources, "warnings": warnings}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--week", type=int, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.week <= 18:
        parser.error("week must be between 1 and 18")
    out = args.out.resolve()
    root = runtime_root(CODE_ROOT)
    if any(out.is_relative_to(root / d) for d in ("leagues", "records", "logs", "state", "export")):
        parser.error("output must be outside live data directories")
    result = build(args.week, root)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"out": str(out), "league_totals": result["league_totals"], "picks_totals": result["picks_totals"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
