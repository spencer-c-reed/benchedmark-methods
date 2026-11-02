"""Descriptive research use and token/list-price efficiency for live league decisions.

Usage: python3 -m bm.score.research_use --week 5 --out analysis/out/research-w5.json
Default cohort is nfl-l1..l6; an explicit cohort reads only those league directories.
Picks are excluded. No collection, price lookup or score regeneration is performed.
Defaults never enter the research or efficiency denominators. Retained session turns
are enriched from the selected root's gateway chain, without counting copied rows
twice. Other runtime/checkouts are never consulted. Unknown usage/prices remain null; medians state their measured denominator.
"""
from __future__ import annotations

import argparse
import json
import hashlib
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from bm.agent.tools import CATALOGUE
from bm.paths import runtime_root

ROOT = Path(__file__).resolve().parents[2]
LIVE_LEAGUES = tuple(f"nfl-l{i}" for i in range(1, 7))
RESEARCH_TOOLS = {t.name for t in CATALOGUE if t.kind in {"read", "market"}}


def _rows(path: Path, sources: list):
    """Read and hash the same retained bytes; missing sources never create files."""
    ref = {"path": str(path), "rows": 0, "head": None, "sha256": None, "bytes": 0}
    sources.append(ref)
    if not path.exists():
        return
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for line in stream:
            digest.update(line)
            ref["bytes"] += len(line)
            if line.strip():
                row = json.loads(line)
                ref["rows"] += 1
                ref["head"] = row["hash"]
                yield row
    ref["sha256"] = digest.hexdigest()


def _cohort(week, league_ids):
    if not 1 <= week <= 18:
        raise ValueError("week must be between 1 and 18")
    ids = list(LIVE_LEAGUES if league_ids is None else league_ids)
    if any(not isinstance(lid, str) or not lid or Path(lid).name != lid or lid in (".", "..") for lid in ids):
        raise ValueError("league IDs must be single directory names")
    return list(dict.fromkeys(ids))


def collect(week: int, root: Path | None = None, league_ids=None) -> tuple[list[dict], dict]:
    """Join logged decisions, retained sessions and request/route gateway receipts."""
    root = Path(root).resolve() if root is not None else runtime_root(ROOT)
    ids = _cohort(week, league_ids)
    from bm.ops.served_model import ANNOTATION
    decisions, sessions, sources, gone = {}, {}, [], set()
    for lid in ids:
        directory = root / "leagues" / lid
        for name, target in (("log.jsonl", decisions), ("sessions.jsonl", sessions)):
            path = directory / name
            for row in _rows(path, sources):
                p = row["payload"]
                if name == "log.jsonl" and p.get("type") == "decision_annotation" and p.get("annotation") == ANNOTATION:
                    gone.add((lid, p["decision_id"]))
                if (p.get("type") == "decision" if name == "log.jsonl" else p.get("kind") == "decision"):
                    if p.get("week") == week:
                        key = (lid, p["decision_id"])
                        if key in target:
                            raise ValueError(f"duplicate {name} decision: {key}")
                        target[key] = p
    for key in gone:                          # served by another model (bm.ops.served_model): no model's research use
        decisions.pop(key, None)
        sessions.pop(key, None)
    wanted = {rid for d in decisions.values() for rid in d.get("model_call_ids", [])}
    gateway, seen = defaultdict(list), set()
    # gateway_logs() follows BM_RUNTIME_ROOT/worktree links. An explicit retained
    # root must never discover or enrich from another runtime.
    for path in (root / "logs/gateway.jsonl",):
        for row in _rows(path, sources):
            p = row["payload"]
            if p.get("type") == "model_call" and p.get("request_id") in wanted and row["hash"] not in seen:
                seen.add(row["hash"])
                gateway[p["request_id"]].append(dict(p, gateway_log=str(path), gateway_seq=row["seq"]))
    out = []
    for key, d in sorted(decisions.items()):
        rec = sessions.get(key)
        if rec and rec.get("decision") != {k: v for k, v in d.items() if k != "seq"}:
            raise ValueError(f"retained session/event mismatch: {key}")
        retained = defaultdict(list)
        for turn in (rec or {}).get("turns", []):
            retained[turn["request_id"]].append(turn)
        calls, missing = [], []
        for rid in dict.fromkeys(d.get("model_call_ids", [])):
            evidence = gateway.get(rid) or retained.get(rid)
            if evidence:
                calls.extend(evidence)
            else:
                missing.append(rid)
        out.append({"decision": d, "record": rec, "calls": calls, "missing_request_ids": missing})
    return out, {"runtime_root": str(root), "sources": sources,
                 "missing_session_ids": [d["decision_id"] for k, d in decisions.items() if k not in sessions]}


def _call_metrics(call: dict) -> dict:
    totals = call.get("attempt_totals") or {}
    complete = bool(totals) or (call.get("attempts") or 1) <= 1
    usage = totals.get("usage", {}) if totals else call.get("usage") or {}
    usage_complete = complete and totals.get("usage_complete", True)
    cost = call.get("cost") or {}
    price = totals.get("usd_eq") if totals else cost.get("usd_eq", call.get("list_usd"))
    # Only retained estimates qualify. Current process-global prices are neither
    # this snapshot's evidence nor an invoice; absent prices remain unknown.
    terminal = call.get("usage") or {}
    return {"input_tokens": usage.get("input") if usage_complete else None,
            "output_tokens": usage.get("output") if usage_complete else None,
            "usd_eq": price if complete else None,
            "terminal_input_tokens": terminal.get("input"),
            "terminal_output_tokens": terminal.get("output")}


def _sum_known(values: list):
    return sum(values) if values and all(v is not None for v in values) else None


def _decision_metrics(item: dict) -> dict:
    calls = [_call_metrics(c) for c in item["calls"]]
    metrics = {key: _sum_known([c[key] for c in calls]) if not item.get("missing_request_ids") else None
               for key in ("input_tokens", "output_tokens", "usd_eq", "terminal_input_tokens", "terminal_output_tokens")}
    metrics["total_tokens"] = _sum_known([metrics["input_tokens"], metrics["output_tokens"]])
    return metrics


def _distribution(ds: list[dict], key: str) -> dict:
    values = [d[key] for d in ds if d[key] is not None]
    return {"n": len(values), "median": statistics.median(values) if values else None,
            "total": round(sum(values), 6) if len(values) == len(ds) and ds else None,
            "observed_total": round(sum(values), 6) if values else None,
            "missing_decision_ids": [d["decision_id"] for d in ds if d[key] is None]}


def _summary(ds: list[dict], excluded: list[str]) -> dict:
    n = len(ds)
    research = sum(d["has_research"] is True for d in ds)
    known = sum(d["has_research"] is not None for d in ds)
    tools = Counter(t for d in ds for t in d["tool_names"])
    return {"non_default_decisions": n, "default_decisions_excluded": len(excluded),
            "default_decision_ids": excluded, "research_decisions": research,
            "research_rate": research / known if known else None,
            "research_known_decisions": known,
            "research_unknown_decision_ids": [d["decision_id"] for d in ds if d["has_research"] is None],
            "research_tool_calls": sum(tools.values()), "tool_calls_by_name": dict(sorted(tools.items())),
            "model_calls": sum(d["model_calls"] for d in ds),
            "input_tokens": _distribution(ds, "input_tokens"), "output_tokens": _distribution(ds, "output_tokens"),
            "total_tokens": _distribution(ds, "total_tokens"),
            "usd_eq": _distribution(ds, "usd_eq"),
            "terminal_input_tokens": _distribution(ds, "terminal_input_tokens"),
            "terminal_output_tokens": _distribution(ds, "terminal_output_tokens"),
            "decision_ids": [d["decision_id"] for d in ds],
            "research_decision_ids": [d["decision_id"] for d in ds if d["has_research"]]}


def summarize(items: list[dict], outcomes: list[dict] = ()) -> dict:
    """Pure aggregation; all efficiencies use non-default decisions only."""
    rows, excluded = [], []
    for item in items:
        d, rec = item["decision"], item.get("record") or {}
        base = {"decision_id": d["decision_id"], "league": d["league"], "team": d["team"],
                "entrant": d["entrant"], "kind": d["kind"], "week": d["week"]}
        if d.get("forced_default") or rec.get("outcome") in {"infra_default", "model_default"} or rec.get("failure"):
            excluded.append(base)
            continue
        tools = rec.get("tools", d.get("research"))
        known_research = isinstance(tools, list)
        names = [t.get("tool", t.get("name")) for t in (tools or [])]
        research = [name for name in names if name in RESEARCH_TOOLS]
        rows.append({**base, "has_research": bool(research) if known_research else None, "tool_names": research,
                     "other_tool_names": [name for name in names if name not in RESEARCH_TOOLS],
                     "model_calls": len(item["calls"]), "missing_request_ids": item.get("missing_request_ids", []),
                     "retained_request_ids": list(dict.fromkeys(c.get("request_id") for c in item["calls"])),
                     "legacy_decision_output_tokens": (d.get("usage") or {}).get("output"),
                     **_decision_metrics(item)})
    entrants = sorted({d["entrant"] for d in rows + excluded})
    by_entrant, by_kind = [], []
    for ent in entrants:
        ds = [d for d in rows if d["entrant"] == ent]
        defaults = [d for d in excluded if d["entrant"] == ent]
        by_entrant.append({"entrant": ent, **_summary(ds, [d["decision_id"] for d in defaults])})
        for kind in sorted({d["kind"] for d in ds + defaults}):
            by_kind.append({"entrant": ent, "kind": kind,
                            **_summary([d for d in ds if d["kind"] == kind],
                                       [d["decision_id"] for d in defaults if d["kind"] == kind])})
    tmv = {(r["league"], r["team"], r["week"]): r["components"]["lineups"] for r in outcomes}
    links = []
    for ent in entrants:
        link = {"entrant": ent, "label": "descriptive small-sample; association, not causal effect",
                "unit": "start_sit decision linked to team-week lineup TMV; repeated team-weeks are not independent"}
        for has_research, name in ((True, "with_research"), (False, "without_research")):
            ds = [d for d in rows if d["entrant"] == ent and d["kind"] == "start_sit"
                  and d["has_research"] == has_research and (d["league"], d["team"], d["week"]) in tmv]
            values = [tmv[(d["league"], d["team"], d["week"])] for d in ds]
            link[name] = {"n": len(ds), "mean_lineup_tmv": statistics.mean(values) if len(values) >= 5 else None,
                          "team_weeks": len({(d["league"], d["team"], d["week"]) for d in ds}),
                          "decision_ids": [d["decision_id"] for d in ds],
                          "status": "descriptive" if len(values) >= 5 else "insufficient scored decisions (need n>=5)"}
        links.append(link)
    return {"non_default_decisions": len(rows), "default_decisions_excluded": len(excluded),
            "by_entrant": by_entrant, "by_kind": by_kind, "outcome_link": links, "decision_evidence": rows,
            "excluded_default_evidence": excluded}


def _outcomes(root: Path, week: int, league_ids=None, sources=None) -> tuple[list[dict], dict]:
    """Select an existing scorecard, never regenerate it using implicit globals."""
    ids = _cohort(week, league_ids)
    path = root / "export/scorecard.json"
    raw = path.read_bytes() if path.exists() else None
    if sources is not None:
        sources.append({"path": str(path), "sha256": hashlib.sha256(raw).hexdigest() if raw is not None else None,
                        "bytes": len(raw) if raw is not None else 0})
    if raw is None:
        return [], {"status": "missing retained scorecard; outcomes unavailable", "generated_at": None}
    data = json.loads(raw)
    if not isinstance(data.get("tmv_rows"), list):
        raise ValueError("retained scorecard lacks tmv_rows")
    rows = [r for r in data["tmv_rows"] if r["league"] in ids and r["week"] == week]
    keys = [(r["league"], r["team"], r["week"]) for r in rows]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate retained outcome team-week")
    return rows, {"status": "retained descriptive outcomes" if rows else "no retained scored team-weeks; pending or export not refreshed",
                  "generated_at": data.get("generated_at")}


def build(week: int, root: Path | None = None, league_ids=None) -> dict:
    root = Path(root).resolve() if root is not None else runtime_root(ROOT)
    ids = _cohort(week, league_ids)
    items, sources = collect(week, root, ids)
    outcomes, outcome_source = _outcomes(root, week, ids, sources["sources"])
    gaps = [f"Missing retained source: {ref['path']}" for ref in sources["sources"] if ref["sha256"] is None]
    if sources["missing_session_ids"]:
        gaps.append("Missing sessions: use only decision-level retained research/turn evidence; no unseen tool use inferred")
    if not outcomes:
        gaps.append(outcome_source["status"])
    for ref in sources["sources"]:
        path = Path(ref["path"])
        if path.exists():
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
        else:
            digest = None
        ref["unchanged_at_finish"] = digest == ref["sha256"]
        if not ref["unchanged_at_finish"]:
            gaps.append(f"Source changed during read; rerun for a consistent cut: {path}")
    return {"schema": "research-use/1", "week": week, "generated_at": datetime.now(timezone.utc).isoformat(),
            "scope": {"leagues": ids,
                      "cohort_basis": "explicit cohort" if league_ids is not None else "six live control leagues; rehearsal/development excluded",
                      "defaults_excluded": True, "picks_excluded": True,
                      "defaults_included": False, "picks_included": False,
                      "rehearsal_and_dev_included": (True if any(lid.startswith(("nfl-f", "nfl-x")) for lid in ids)
                                                     else False if all(lid in LIVE_LEAGUES for lid in ids) else None),
                      "cohort_classification_note": "Known l1..l6 controls and nfl-f/nfl-x rehearsal/development IDs; other explicit IDs unclassified"},
            "method": {"research": "logged read/market tool calls within the decision session before its final answer",
                       "efficiency": "sum retained transport attempts/routes per decision; terminal token sums provided separately",
                       "usd_eq": "retained gateway/session list-price estimate only; unknown stays null; not billed spend",
                       "missing": "unknown totals stay null; medians use known decisions with n and missing IDs; research rates use only decisions with retained tool evidence",
                       "outcome": "retained export/scorecard.json tmv_rows components.lineups; no recomputation; export may lag league scoring"},
            "sources": sources, "outcome_source": outcome_source, "collection_gaps": gaps,
            "scored_team_weeks": len(outcomes), **summarize(items, outcomes)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--week", type=int, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--root", type=Path, help="Explicit retained data root; no live fallback")
    parser.add_argument("--league", action="append", help="Explicit cohort; defaults to nfl-l1..l6")
    args = parser.parse_args(argv)
    if not 1 <= args.week <= 18:
        parser.error("--week must be between 1 and 18")
    destination = args.out.resolve()
    live = runtime_root(ROOT)
    if any(destination.is_relative_to(live / name) for name in ("leagues", "records", "logs", "state", "export")):
        parser.error("--out must be outside live data directories")
    root = args.root.resolve() if args.root else live
    if destination == root or any(destination.is_relative_to(root / name) for name in ("leagues", "records", "logs", "state", "export", "data", "blobs")):
        parser.error("--out must be outside retained data directories")
    result = build(args.week, root=root, league_ids=args.league)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"out": str(destination), "week": args.week,
                      "non_default_decisions": result["non_default_decisions"],
                      "default_decisions_excluded": result["default_decisions_excluded"],
                      "entrants": len(result["by_entrant"]), "scored_team_weeks": result["scored_team_weeks"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
