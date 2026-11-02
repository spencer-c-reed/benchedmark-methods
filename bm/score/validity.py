"""Per-decision validity (build 6): may this logged decision count as the requested model's own, timely, fresh answer?

A decision is valid only if all of these hold (each failure is a named reason):
  after_lock / lock_unknown      it was logged (logged_at, else ts) no later than its lock, the `hold_until` of its
                                 ledger row (decision_expected, else its first decision_pending); no ledger row = unknown
  forced_default                 it was not a forced default
  no_rendered_prompt_hash        its rendered prompt hash is present
  template_mismatch              its prompt_template_hash is the league's template (league.json prompt_template)
  no_answering_call              a gateway row of its model_call_ids answered it (error null)
  served_unobserved              that row's identity_source is observed (bm.ops.gateway_audit.OBSERVED_SOURCES), with
                                 a reported model id
  served_other_model             the observed served model is the requested one (bm.gateway.identity.same_model)
  misserved                      no `decision_annotation` (annotation "misserved", bm.ops.served_model) says another
                                 model served it: such a decision is invalid for the requested model

Additive and read-only: logged rows are never rewritten; the export carries the result as a `validity` field.

  python3 -m bm.score.validity [--leagues DIR] [--out FILE] [league ...]   rates per league and model
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from bm.gateway.identity import same_model
from bm.ops.gateway_audit import OBSERVED_SOURCES

SCHEMA = "decision-validity/1"
REASONS = ("lock_unknown", "after_lock", "forced_default", "no_rendered_prompt_hash", "template_mismatch",
           "no_answering_call", "served_unobserved", "served_other_model", "misserved")
_RID = re.compile(rb'"request_id":\s*"([^"]+)"')


def _t(s):
    try:
        return datetime.fromisoformat(s) if s else None
    except (TypeError, ValueError):
        return None


def gateway_calls(request_ids: set[str], paths: list[Path] | None = None) -> dict[str, list[dict]]:
    """request_id -> model_call rows, streaming the gateway logs and parsing only the wanted rows."""
    if paths is None:
        from bm.paths import gateway_logs
        paths = gateway_logs(Path(__file__).resolve().parents[2])
    out: dict[str, list[dict]] = defaultdict(list)
    seen = set()
    if not request_ids:
        return out
    for path in paths:
        if not Path(path).exists():
            continue
        with open(path, "rb") as f:
            for line in f:
                m = _RID.search(line)
                if not m or m.group(1).decode() not in request_ids:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("hash") in seen:
                    continue
                seen.add(row.get("hash"))
                p = row.get("payload") or {}
                if p.get("type") == "model_call":
                    out[p["request_id"]].append(p)
    return out


def locks(events: list[dict]) -> dict[str, str]:
    from bm.ops.invariants import locks as ledger_locks
    return ledger_locks(events)


def misserved_ids(events: list[dict]) -> set[str]:
    from bm.ops.served_model import misserved_ids as annotated
    return annotated(events)


def decision_validity(dec: dict, *, lock: str | None, template_hash: str | None, calls: list[dict],
                      misserved: bool = False) -> dict:
    """{valid, reasons, served_model, identity_source} for one decision event."""
    reasons = []
    at, lk = _t(dec.get("logged_at") or dec.get("ts")), _t(lock)
    if lk is None:
        reasons.append("lock_unknown")
    elif at is None or at > lk:
        reasons.append("after_lock")
    if dec.get("forced_default"):
        reasons.append("forced_default")
    if not dec.get("rendered_prompt_hash"):
        reasons.append("no_rendered_prompt_hash")
    if not template_hash or dec.get("prompt_template_hash") != template_hash:
        reasons.append("template_mismatch")
    answer = next((c for c in reversed(calls) if not c.get("error")), None)
    served = src = None
    if answer is None:
        reasons.append("no_answering_call")
    else:
        served, src = answer.get("model_reported"), answer.get("identity_source")
        if src not in OBSERVED_SOURCES or not served:
            reasons.append("served_unobserved")
        elif not same_model(answer.get("model_requested"), served):
            reasons.append("served_other_model")
    if misserved:
        reasons.append("misserved")
    return {"valid": not reasons, "reasons": reasons, "served_model": served, "identity_source": src}


def league_template_hash(cfg: dict) -> str | None:
    from bm.league.season import template_module
    try:
        return template_module(cfg.get("prompt_template")).template_hash()
    except ValueError:
        return None


def for_events(events: list[dict], cfg: dict, calls: dict[str, list[dict]]) -> dict[str, dict]:
    """decision_id -> validity for every decision event of one league log."""
    lk, gone, th = locks(events), misserved_ids(events), league_template_hash(cfg)
    out = {}
    for e in events:
        if e.get("type") != "decision":
            continue
        rows = [r for rid in e.get("model_call_ids") or [] for r in calls.get(rid, [])]
        out[e["decision_id"]] = decision_validity(e, lock=lk.get(e["decision_id"]), template_hash=th, calls=rows,
                                                  misserved=e["decision_id"] in gone)
    return out


def rates(leagues: Path, league_ids: list[str], gateway_paths: list[Path] | None = None) -> dict:
    from bm.ops.invariants import read_events
    logs = {lid: read_events(leagues / lid / "log.jsonl") for lid in league_ids}
    ids = {rid for evs in logs.values() for e in evs if e.get("type") == "decision" for rid in e.get("model_call_ids") or []}
    calls = gateway_calls(ids, gateway_paths)
    out = {"schema": SCHEMA, "leagues": {}}
    for lid, evs in logs.items():
        cfg = json.loads((leagues / lid / "league.json").read_text())
        val = for_events(evs, cfg, calls)
        by = defaultdict(lambda: {"n": 0, "valid": 0, "reasons": Counter()})
        for e in evs:
            if e.get("type") != "decision":
                continue
            for key in (e.get("entrant") or "?", "_all"):
                r = by[key]
                v = val[e["decision_id"]]
                r["n"] += 1
                r["valid"] += v["valid"]
                r["reasons"].update(v["reasons"])
        out["leagues"][lid] = {k: {"n": r["n"], "valid": r["valid"], "rate": round(r["valid"] / r["n"], 4) if r["n"] else None,
                                   "reasons": dict(r["reasons"])} for k, r in sorted(by.items())}
    return out


def main(argv: list[str] | None = None) -> int:
    from bm.league.season import LEAGUES
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("league_ids", nargs="*")
    ap.add_argument("--leagues", type=Path, default=LEAGUES)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(argv if argv is not None else sys.argv[1:])
    root = a.leagues.resolve()
    ids = a.league_ids or sorted(p.name for p in root.iterdir() if (p / "log.jsonl").exists() and (p / "league.json").exists())
    res = rates(root, ids)
    text = json.dumps(res, indent=1)
    if a.out:
        if a.out.resolve().is_relative_to(root):
            ap.error("the report must be written outside the leagues directory")
        a.out.write_text(text + "\n")
    print(text if not a.out else json.dumps({l: v.get("_all") for l, v in res["leagues"].items()}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
