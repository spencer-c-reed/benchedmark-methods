"""Read-only provider refusal ledger, separate from model skill and defaults.

python3 -m bm.score.refusals --since 2026-10-07 --out analysis/out/refusals.json

Dates are UTC by default; --until is exclusive. Counts distinguish affected
requests, gateway route rows, and observed refused attempts. Old route rows lack
attempt histories: their refusal is a lower bound, never `attempts` refusals.
Outcomes use all retained decision receipts, including retries after --until;
successful transport alone never proves an own decision. No blobs are opened.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date, datetime, time, timezone
import hashlib
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from bm.log import canonical, GENESIS_PREV
from bm.paths import gateway_logs, runtime_root

ROOT = Path(__file__).resolve().parents[2]


def _at(value: str) -> datetime:
    at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if at.tzinfo is None:
        raise ValueError(f"timestamp requires timezone: {value}")
    return at


def _rows(path: Path, sources: list[dict]):
    """Verify each chain independently, up to its initial byte boundary."""
    if not path.exists():
        sources.append({"path": str(path), "exists": False})
        return
    prev, n = GENESIS_PREV, 0
    with path.open("rb") as stream:
        size = path.stat().st_size
        while stream.tell() < size:
            line = stream.readline(size - stream.tell())
            row = json.loads(line)
            body = canonical(row["payload"])
            digest = hashlib.sha256(prev.encode() + body).hexdigest()
            if (row["seq"] != n + 1 or row["prev_hash"] != prev or
                    row["payload_hash"] != hashlib.sha256(body).hexdigest() or row["hash"] != digest):
                raise ValueError(f"invalid hash chain: {path} row {n + 1}")
            n, prev = n + 1, digest
            yield row
    sources.append({"path": str(path), "exists": True, "rows": n,
                    "snapshot_bytes": size, "head_hash": prev, "verified": True})


def _ref(path: Path, row: dict) -> dict:
    return {"path": str(path), "seq": row["seq"], "hash": row["hash"]}


def _scope(league: str, config: dict) -> str:
    status = config.get("status", "")
    if league.startswith("_void") or league.startswith("nfl-x") or "prototype" in status:
        return "development"
    if "rehearsal" in status:
        return "rehearsal"
    if status == "private-live-season":
        return "live"
    return "unclassified"


def _pick_scope(p: dict) -> str:
    slate = p.get("slate") or (p.get("seat_key") or p.get("key") or "").split(":")[0]
    match = re.fullmatch(r"\d{4}-w(\d+)-(early|prelock|london)", slate)
    if match:
        return "live" if int(match[1]) >= 5 else "development"
    return "scripted" if slate.endswith("-scripted") else "unclassified"


def _contexts(root: Path, sources: list[dict]):
    by_decision, by_request, configs = defaultdict(list), defaultdict(list), {}

    def add(p, path, row, scope, decision_id, kind, outcome, received):
        requests = list(p.get("model_call_ids") or [])
        if p.get("request_id"):
            requests.append(p["request_id"])
        turns = p.get("turns") or []
        if isinstance(turns, list):
            requests.extend(t["request_id"] for t in turns if t.get("request_id"))
        c = {"decision_id": decision_id, "decision_kind": kind, "traffic": scope,
             "entrant": p.get("entrant"), "outcome": outcome, "received_at": received,
             "request_ids": sorted(set(requests)),
             "record_id": p.get("record_id"), "source": _ref(path, row),
             "prompt_hash": p.get("prompt_sha256") or p.get("rendered_prompt_hash")}
        by_decision[decision_id].append(c)
        for request in set(requests):
            by_request[request].append(c)

    for directory in sorted((root / "leagues").glob("*")):
        config_path = directory / "league.json"
        config = json.loads(config_path.read_text()) if config_path.exists() else {}
        configs[directory.name] = config
        scope = _scope(directory.name, config)
        for path in (directory / "log.jsonl", directory / "sessions.jsonl"):
            for row in _rows(path, sources):
                p = row["payload"]
                if p.get("type") == "decision":
                    outcome = "default" if p.get("forced_default") else "own"
                    add(p, path, row, scope, p["decision_id"], p.get("kind"), outcome,
                        p.get("logged_at") or p["ts"])
                elif p.get("type") in ("decision_pending", "decision_skipped_infra", "decision_missed"):
                    add(p, path, row, scope, p["decision_id"], p.get("kind"), "no_decision",
                        p.get("logged_at") or p["ts"])
                elif p.get("kind") == "decision" and p.get("type") == "session":
                    dec = p.get("decision") or {}
                    outcome = "own" if p.get("outcome") == "own" and not p.get("failure") else "default"
                    add(p, path, row, scope, p["decision_id"], p.get("decision_kind"), outcome,
                        dec.get("logged_at") or p["ts"])
    for path in sorted((root / "records" / "picks").glob("*.jsonl")):
        for row in _rows(path, sources):
            p = row["payload"]
            if p.get("kind") != "pick":
                continue
            seat = p.get("seat_key") or re.sub(r":attempt\d+$", "", p["key"])
            outcome = "own" if p.get("outcome") == "own" and not p.get("failure") else "default"
            add(p, path, row, _pick_scope(p), "picks:" + seat, "pick_" + p["step"], outcome,
                p.get("received_at") or p["ts"])
    # Panel and canary calls share the replay: gateway prefix. Their exact
    # request receipts, never a reused replay key, distinguish these lanes.
    for directory in ("replay", "canary", "panel", "panel_call", "judge"):
        for path in sorted((root / "records" / directory).glob("*.jsonl")):
            for row in _rows(path, sources):
                p = row["payload"]
                if not p.get("request_id"):
                    continue
                scope = "canary" if p.get("kind") == "canary" else (
                    "panel" if p.get("kind") in ("panel_call", "judge") else "replay")
                add(p, path, row, scope, p.get("decision_id") or "replay:" + p["key"],
                    p.get("decision_kind"), "offline", p["ts"])
    return by_decision, by_request, configs


def _evidence(p: dict) -> str | None:
    if p.get("error") == "REFUSED":
        return "REFUSED"
    if p.get("stop_reason") == "refusal":
        return "stop_reason"
    # Older Anthropic adapter emitted this precise structured stop reason as TRANSIENT.
    if p.get("error") == "TRANSIENT" and "stop_reason refusal" in (p.get("error_detail") or ""):
        return "legacy_stop_reason"
    return None


def _refused_attempts(p: dict) -> list[dict]:
    history = p.get("attempt_history") or []
    refusals = [{"attempt": h.get("attempt"), "started_at": h.get("started_at"),
                 "ended_at": h.get("ended_at") or p["ts"], "evidence": _evidence(h),
                 "excerpt": (h.get("error_detail") or "")[:120]}
                for h in history if _evidence(h)]
    if _evidence(p) and (not history or not _evidence(history[-1])):
        refusals.append({"attempt": None, "started_at": None, "ended_at": p["ts"],
                         "evidence": _evidence(p), "excerpt": (p.get("error_detail") or "")[:120]})
    return refusals


def _traffic(p: dict, linked: list[dict], configs: dict) -> str:
    did = p.get("decision_id") or ""
    offline = {c["traffic"] for c in linked if c["traffic"] in ("panel", "canary", "replay", "recovery")}
    if len(offline) == 1:
        return next(iter(offline))
    for prefix, scope in (("replay:", "replay"), ("panel", "panel"), ("judge:", "panel"),
                          ("canary", "canary"), ("probe:", "canary"), ("recovery:", "recovery")):
        if did.startswith(prefix):
            return scope
    if linked:
        scopes = {c["traffic"] for c in linked}
        return next(iter(scopes)) if len(scopes) == 1 else "unclassified"
    league = did.split(":")[0]
    if league in configs:
        scope = _scope(league, configs[league])
        # A live-looking request without a decision receipt cannot certify a live decision.
        return "unlinked" if scope == "live" else scope
    return "unclassified"


def _inferred_kind(did: str) -> str:
    if ":draft:" in did:
        return "draft_pick"
    if ":lineup" in did:
        return "start_sit"
    if ":waivers:" in did:
        return "waiver_bid"
    if ":offer:" in did:
        return "trade_offer"
    if re.search(r":m\d+:", did):
        return "trade_response"
    return "unknown"


def _summary(entries: list[dict]) -> dict:
    return {"requests": len({e["request_id"] for e in entries}),
            "gateway_rows": len({e["source"]["hash"] for e in entries}),
            "observed_refusal_attempts": sum(len(e["refusals"]) for e in entries),
            "retried_to_own_requests": len({e["request_id"] for e in entries if e["retried_to_own"]}),
            "default_requests": len({e["request_id"] for e in entries if e["outcome"] == "default"}),
            "no_decision_requests": len({e["request_id"] for e in entries if e["outcome"] == "no_decision"}),
            "unresolved_requests": len({e["request_id"] for e in entries if e["outcome"] == "unresolved"}),
            "classified_refused_requests": len({e["request_id"] for e in entries
                                                if any(f["evidence"] == "REFUSED" for f in e["refusals"])}),
            "legacy_refusal_requests": len({e["request_id"] for e in entries
                                            if any(f["evidence"] == "legacy_stop_reason" for f in e["refusals"])}),
            "incomplete_attempt_history_rows": len({e["source"]["hash"] for e in entries if not e["attempt_history_complete"]})}


def ledger(since: str, until: str | None = None, *, code_root: Path = ROOT,
           timezone_name: str = "UTC") -> dict:
    tz = ZoneInfo(timezone_name)
    start = datetime.combine(date.fromisoformat(since), time.min, tzinfo=tz)
    end = datetime.combine(date.fromisoformat(until), time.min, tzinfo=tz) if until else None
    if end and end <= start:
        raise ValueError("--until must be after --since")
    generated_at = datetime.now(timezone.utc).isoformat()
    sources, entries, seen = [], [], set()
    contexts, requests, configs = _contexts(runtime_root(code_root), sources)
    for path in gateway_logs(code_root):
        for row in _rows(path, sources):
            p = row["payload"]
            if row["hash"] in seen or p.get("type") != "model_call":
                continue
            seen.add(row["hash"])
            refusals = [f for f in _refused_attempts(p)
                        if start <= _at(f["ended_at"]) and (end is None or _at(f["ended_at"]) < end)]
            if not refusals:
                continue
            linked = [c for c in requests.get(p["request_id"], []) if c["entrant"] == p.get("entrant")]
            did = p.get("decision_id") or ""
            base_did = re.sub(r":attempt\d+$", "", did) if did.startswith("picks:") else did
            candidates = contexts.get(base_did, [])
            # Request linkage is authoritative for Picks and older rows without decision_id.
            if linked:
                candidates = contexts.get(linked[0]["decision_id"], [])
            candidates = [c for c in candidates if c["entrant"] == p.get("entrant")]
            traffic = _traffic(p, linked or candidates, configs)
            if traffic in ("replay", "panel", "canary", "recovery"):
                candidates = []
            last_refusal = max(_at(f["ended_at"]) for f in refusals)
            # Historical league logged_at has second precision, gateway ts has
            # microseconds. An exact request reference proves causal receipt even
            # when that rounded timestamp looks a fraction of a second earlier.
            later = [c for c in candidates if p["request_id"] in c["request_ids"] or
                     _at(c["received_at"]) >= last_refusal]
            own = [c for c in later if c["outcome"] == "own"]
            resolution = min(own, key=lambda c: _at(c["received_at"])) if own else (
                max(later, key=lambda c: _at(c["received_at"])) if later else None)
            kinds = {c["decision_kind"] for c in linked or candidates if c["decision_kind"]}
            kind = next(iter(kinds)) if len(kinds) == 1 else _inferred_kind(did)
            by_date = defaultdict(list)
            for f in refusals:
                by_date[_at(f["ended_at"]).astimezone(tz).date().isoformat()].append(f)
            history = p.get("attempt_history") or []
            history_complete = (bool(history) and len(history) == p.get("attempts") and
                                [h.get("attempt") for h in history] == list(range(1, len(history) + 1)))
            for day, fs in sorted(by_date.items()):
                entries.append({"date": day, "model": p.get("entrant"), "model_requested": p.get("model_requested"),
                                "lane": p.get("lane"), "decision_kind": kind,
                                "kind_source": "record" if len(kinds) == 1 else "decision_id_inference",
                                "traffic": traffic, "request_id": p["request_id"], "decision_id": did,
                                "failure_kind": "refusal",
                                "prompt_hash": p.get("prompt_blob") or (linked[0]["prompt_hash"] if linked else None),
                                "logged_at": p["ts"], "source": _ref(path, row), "refusals": fs,
                                "attempt_history_complete": history_complete,
                                "reported_attempts": p.get("attempts"), "terminal_error": p.get("error"),
                                "outcome": resolution["outcome"] if resolution else "unresolved",
                                "retried_to_own": bool(own), "resolution": resolution})
    entries.sort(key=lambda e: (e["date"], e["logged_at"], e["request_id"], e["source"]["hash"]))
    groups = defaultdict(list)
    for e in entries:
        groups[(e["date"], e["traffic"], e["model"], e["lane"], e["decision_kind"])].append(e)
    return {"schema": "refusal-ledger/1", "generated_at": generated_at,
            "range": {"since": since, "until_exclusive": until, "timezone": timezone_name},
            "notes": ["Provider refusals are separate from model behavior; no prompt/reply texts are copied and no blobs are opened.",
                      "Panel and canary receipts distinguish calls sharing the replay: gateway prefix; unlinked replay rows retain the replay label.",
                      "classified_refused_requests uses REFUSED; legacy_refusal_requests preserves explicit TRANSIENT stop_reason refusal evidence.",
                      "Legacy rows without attempt history prove at least one refusal, not reported_attempts refusals.",
                      "Outcomes reflect all retained receipts, including retries after the reporting range.",
                      "Only linked live-season decisions and untagged Picks from week 5 enter live counts."],
            "live": _summary([e for e in entries if e["traffic"] == "live"]),
            "by_traffic": {scope: _summary([e for e in entries if e["traffic"] == scope])
                           for scope in sorted({e["traffic"] for e in entries})},
            "daily": [{"date": k[0], "traffic": k[1], "model": k[2], "lane": k[3],
                       "decision_kind": k[4], **_summary(es)} for k, es in sorted(groups.items())],
            "entries": entries, "sources": sources}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", required=True, help="inclusive date (YYYY-MM-DD)")
    parser.add_argument("--until", help="exclusive date (YYYY-MM-DD)")
    parser.add_argument("--timezone", default="UTC")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    # A worktree report must never be written into the serving data directories.
    output = args.out.resolve()
    serving = runtime_root(ROOT)
    for name in ("leagues", "records", "logs", "state", "export", "blobs"):
        if output.is_relative_to(serving / name):
            parser.error("--out must be outside live data directories")
    report = ledger(args.since, args.until, timezone_name=args.timezone)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"out": str(output), "live": report["live"], "by_traffic": report["by_traffic"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
