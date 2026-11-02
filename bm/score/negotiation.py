"""Read-only negotiation measures, grouped by opening week (live weeks >=5).

Rounds are response turns after the opening proposal, not pairs of messages.
MESSAGE is retained separately. Forced defaults are no-replies, never model
rejects. Missing evidence stays unknown; infra and provider refusals are excluded
from the model response-mix denominator. These are descriptive small samples,
not a skill/alignment ranking. A negative raw est_value spread can reflect a
positional tradeoff; the flag does not establish irrationality or deception.

python3 -m bm.score.negotiation --leagues nfl-l1,nfl-l2,nfl-l3,nfl-l4,nfl-l5,nfl-l6 --out analysis/out/negotiation.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from bm.agent import bots
from bm.engine.rules import load_rules
from bm.log import GENESIS_PREV, canonical
from bm.ops.nodecisions import _session_outputs, failure_kind
from bm.paths import runtime_root

ROOT = Path(__file__).resolve().parents[2]
LIVE_LEAGUES = tuple(f"nfl-l{i}" for i in range(1, 7))
MODEL_RESPONSES = ("accept", "counter", "reject", "message", "model_no_reply")


def _missing(event, outputs):
    default = event.get("forced_default") or {}
    cause = default.get("cause") or event.get("cause")
    fk = failure_kind(cause, event.get("detail", ""), outputs.get(event.get("decision_id"), ()))
    fk = fk or "not_dispatched"
    category = "model_no_reply" if fk == "model" else "provider_no_reply" if fk == "refusal" else "infra_no_reply"
    return category, fk


def analyze_league(events: list[dict], cfg: dict, now: datetime, *, outputs=None, max_messages=None) -> list[dict]:
    """Analyze applied messages and decision receipts without rebuilding rosters.

    failure_kind uses the commissioner's effective-cap session evidence, as in
    trade_reconcile. We retain all weeks, rather than that monitor's 24h issues.
    Only accepted trades expire overlapping offers (engine.state semantics).
    """
    outputs = outputs or {}
    seated = {s["team_id"]: s["entrant_id"] for s in cfg["seats"]}
    from bm import succession

    def ent(team, wk):            # the seat's controller that week: a successor, or a bot operator (bm/succession.py)
        return succession.controller(seated[team], league=cfg.get("id"), week=wk)
    decisions, misses, expected = {}, {}, {}
    closed_windows = set()
    for e in events:
        if e.get("kind") == "trade_response":
            target = decisions if e["type"] == "decision" else expected if e["type"] == "decision_expected" else misses
            if e["type"] in ("decision", "decision_expected", "decision_skipped_infra", "decision_pending", "decision_missed"):
                target[e["decision_id"]] = e
        if e["type"] in ("step_done", "trade_window_gap") and e.get("step", "").startswith("trade:"):
            _, w, n = e["step"].split(":")
            closed_windows.add(f"w{w}-t{n}")
        if e["type"] == "trade_window_gap":
            closed_windows.add(e.get("window"))

    threads = {}
    week = cfg.get("start_week", 5)
    for e in events:
        typ = e["type"]
        if typ in ("genesis", "week_advance"):
            week = e.get("week", week)
        if typ in ("trade_expired", "offer_expired") and e["thread_id"] in threads:
            t = threads[e["thread_id"]]
            if t["_closed"] is None:
                t["_closed"] = e
                t["_expiry"] = "window_expired" if typ == "trade_expired" else "offer_expired"
        if typ == "week_advance":
            for t in threads.values():
                if t["_closed"] is None:
                    t["_closed"], t["_expiry"] = e, "week_advanced"
        if typ != "trade_msg":
            continue
        th = e["thread_id"]
        if e["action"] == "PROPOSE":
            if th in threads:
                raise ValueError(f"duplicate thread proposal: {th}")
            threads[th] = dict(league=cfg["id"], thread_id=th, week=e.get("week", week), window=e["window"],
                               parties=[dict(team=t, entrant=ent(t, e.get("week", week)), is_bot=ent(t, e.get("week", week)) in bots.ALL)
                                        for t in (e["from_team"], e["to_team"])],
                               _messages=[], _offer=e.get("offer") or {}, _closed=None, _expiry=None)
        t = threads[th]
        t["_messages"].append(e)
        if e["action"] in ("PROPOSE", "COUNTER"):
            t["_offer"] = e.get("offer") or {}
        if e["action"] in ("ACCEPT", "REJECT"):
            t["_closed"] = e
        if e["action"] == "ACCEPT":
            moved = set(t["_offer"].get("give", [])) | set(t["_offer"].get("get", []))
            for other in threads.values():
                offer = other["_offer"]
                if other is not t and other["_closed"] is None and moved & (set(offer.get("give", [])) | set(offer.get("get", []))):
                    other["_closed"], other["_expiry"] = e, "players_moved"

    out = []
    for t in threads.values():
        messages = t.pop("_messages")
        closed, expiry = t.pop("_closed"), t.pop("_expiry")
        t.pop("_offer")
        responses, contradictions = [], []
        for e in messages[1:]:
            did = e.get("decision_id")
            d = decisions.get(did, {})
            category, fk = e["action"].lower(), None
            if d.get("forced_default"):
                category, fk = _missing(d, outputs)
            elif e.get("source") == "default":
                # The model answered but the engine substituted a reject. Keep
                # its attempted action; old logs do not retain the violation.
                category = ((d.get("action") or {}).get("response") or "unknown_no_reply").lower()
                fk = "application_gap" if d else None
            team = e["from_team"]
            response = dict(team=team, entrant=ent(team, t["week"]), is_bot=ent(team, t["week"]) in bots.ALL,
                            counterparty=ent(e["to_team"], t["week"]), counterparty_is_bot=ent(e["to_team"], t["week"]) in bots.ALL,
                            category=category, failure_kind=fk, decision_id=did,
                            decision_seq=d.get("seq"), message_seq=e.get("seq"),
                            applied_action=e["action"].lower(),
                            was_applied=not d.get("forced_default") and e.get("source") != "default")
            responses.append(response)
        last = messages[-1]
        terminal = last["action"] in ("ACCEPT", "REJECT")
        deadline = last.get("expires_at")
        next_did = f"{cfg['id']}:{t['window']}:{t['thread_id']}:m{len(messages)+1}:{last['to_team']}"
        exp = expected.get(next_did, {})
        deadline = exp.get("hold_until") or deadline
        ended = closed is not None or t["window"] in closed_windows or (deadline and now >= datetime.fromisoformat(deadline))
        outcome = responses[-1]["category"] if terminal else "expired" if expiry == "players_moved" else "open"
        if terminal and responses[-1]["failure_kind"] == "application_gap":
            outcome = "application_gap"
        gap = misses.get(next_did)
        answer = decisions.get(next_did)
        capped = max_messages is not None and len(messages) >= max_messages
        if capped and not terminal:
            outcome, expiry, ended = "expired", "message_limit", True
        if not terminal and not capped and (expiry != "players_moved" or gap or answer or exp):
            category, fk = None, None
            if answer and not answer.get("forced_default"):
                # A successful model decision that was not applied is a harness gap.
                category, fk = "infra_no_reply", "application_gap"
            elif answer or gap:
                category, fk = _missing(answer or gap, outputs)
            elif exp:
                category, fk = "infra_no_reply", "not_dispatched"
            elif ended:
                category = "unknown_no_reply"
            if category:
                if not ended:
                    category = "pending"  # still eligible for ordinary recovery
                team = last["to_team"]
                response_category = category
                if answer and not answer.get("forced_default"):
                    response_category = ((answer.get("action") or {}).get("response") or "unknown_no_reply").lower()
                responses.append(dict(team=team, entrant=ent(team, t["week"]), is_bot=ent(team, t["week"]) in bots.ALL,
                                      counterparty=ent(last["from_team"], t["week"]), counterparty_is_bot=ent(last["from_team"], t["week"]) in bots.ALL,
                                      category=response_category, failure_kind=fk, decision_id=next_did,
                                      decision_seq=(answer or gap or exp).get("seq"), message_seq=None,
                                      applied_action=None, was_applied=False))
                outcome = "expired" if expiry == "players_moved" else category
            elif ended:
                outcome = "expired"
        by_seq = {e.get("seq"): e for e in messages}
        for response in responses:
            d = decisions.get(response["decision_id"], {})
            value = d.get("est_value") if d else by_seq.get(response["message_seq"], {}).get("est_value")
            if response["category"] == "accept" and not response["is_bot"] and isinstance(value, dict):
                give, get = value.get("give"), value.get("get")
                if all(type(x) in (int, float) and math.isfinite(x) for x in (give, get)) and get < give:
                    contradictions.append(dict(league=cfg["id"], thread_id=t["thread_id"], week=t["week"],
                                               **response, est_value=value, spread=round(get - give, 6)))
        t.update(outcome={"accept": "accepted", "reject": "rejected"}.get(outcome, outcome),
                 raw_last_action=last["action"], message_count=len(messages), response_turns=len(messages)-1,
                 rounds_to_close=len(messages)-1 if terminal or ended else None,
                 closure_reason=expiry or ("terminal_message" if terminal else "cutoff" if ended else None),
                 opening_seq=messages[0].get("seq"), closing_seq=closed.get("seq") if closed else None,
                 responses=responses, self_contradicting_accepts=contradictions)
        out.append(t)
    return out


def summarize(threads: list[dict]) -> dict:
    entrants = {}
    for t in threads:
        for party in t["parties"]:
            entrants.setdefault(party["entrant"], dict(entrant=party["entrant"], is_bot=party["is_bot"], counts=Counter(), counterparties={}))
        for r in t["responses"]:
            ent = entrants[r["entrant"]]
            ent["counts"][r["category"]] += 1
            cp = ent["counterparties"].setdefault(r["counterparty"], dict(is_bot=r["counterparty_is_bot"], counts=Counter()))
            cp["counts"][r["category"]] += 1
    for ent in entrants.values():
        counts = ent["counts"]
        n = sum(counts[k] for k in MODEL_RESPONSES)
        ent.update(model_response_n=n, response_mix={k: counts[k]/n if n else None for k in MODEL_RESPONSES},
                   response_opportunities=sum(counts.values()))
    rounds = defaultdict(list)
    for t in threads:
        if t["rounds_to_close"] is not None:
            rounds[t["outcome"]].append(t["rounds_to_close"])
    return dict(thread_count=len(threads), outcomes=dict(Counter(t["outcome"] for t in threads)),
                raw_outcomes=dict(Counter(t["raw_last_action"].lower() for t in threads)),
                raw_single_reject=sum(t["message_count"] == 2 and t["raw_last_action"] == "REJECT" for t in threads),
                raw_no_reply=sum(t["message_count"] == 1 for t in threads),
                rounds_to_close={k: dict(n=len(v), mean=sum(v)/len(v), histogram=dict(Counter(v))) for k, v in rounds.items()},
                entrants=entrants, self_contradicting_accepts=[a for t in threads for a in t["self_contradicting_accepts"]])


def _snapshot(path: Path):
    """Verify the exact bytes scored; no ChainLog constructor or writes to runtime."""
    data = path.read_bytes()
    rows = [json.loads(line) for line in data.splitlines() if line.strip()]
    prev = GENESIS_PREV
    for seq, row in enumerate(rows, 1):
        body = canonical(row["payload"])
        if (row["seq"] != seq or row["prev_hash"] != prev or
                row["payload_hash"] != hashlib.sha256(body).hexdigest() or
                row["hash"] != hashlib.sha256(prev.encode() + body).hexdigest()):
            raise ValueError(f"invalid chain at {path}:{seq}")
        prev = row["hash"]
    return [dict(r["payload"], seq=r["seq"]) for r in rows], dict(path=str(path), sha256=hashlib.sha256(data).hexdigest(), rows=len(rows), head=prev)


def report(leagues=LIVE_LEAGUES, *, now=None, min_week=5, weeks=None, root=None) -> dict:
    now = now or datetime.now(timezone.utc)
    root = Path(root) if root is not None else runtime_root(ROOT)
    threads, sources = [], []
    for lid in leagues:
        if Path(lid).name != lid or lid in (".", ".."):
            raise ValueError(f"invalid league id: {lid}")
        directory = root / "leagues" / lid
        cfg_path = directory / "league.json"
        config_bytes = cfg_path.read_bytes()
        cfg = json.loads(config_bytes)
        if cfg["id"] != lid:
            raise ValueError(f"league id mismatch: {lid}")
        if cfg.get("mode") == "agent":
            raise ValueError("continuous agent trade wake attribution is not supported by this windowed measure")
        events, source = _snapshot(directory / "log.jsonl")
        source.update(league=lid, config_sha256=hashlib.sha256(config_bytes).hexdigest())
        max_messages = None
        if cfg.get("rules_file"):
            rules_path = ROOT / cfg["rules_file"]
            source["rules_sha256"] = hashlib.sha256(rules_path.read_bytes()).hexdigest()
            max_messages = load_rules(rules_path).trades["max_msgs_per_thread"]
        session_path = directory / "sessions.jsonl"
        source["sessions_sha256"] = hashlib.sha256(session_path.read_bytes()).hexdigest() if session_path.exists() else None
        outputs = _session_outputs(SimpleNamespace(dir=directory))
        if session_path.exists() and hashlib.sha256(session_path.read_bytes()).hexdigest() != source["sessions_sha256"]:
            raise ValueError(f"session evidence changed during scoring: {session_path}")
        sources.append(source)
        threads.extend(t for t in analyze_league(events, cfg, now, outputs=outputs, max_messages=max_messages)
                       if t["week"] >= min_week and (weeks is None or t["week"] in weeks))
    summary = summarize(threads)
    reference = dict(thread_count=146, raw_single_reject=75, raw_no_reply=21, accepted=28, self_contradicting_accepts=8)
    observed = dict(thread_count=summary["thread_count"], raw_single_reject=summary["raw_single_reject"],
                    raw_no_reply=summary["raw_no_reply"], accepted=summary["raw_outcomes"].get("accept", 0),
                    self_contradicting_accepts=len(summary["self_contradicting_accepts"]))
    return dict(schema="bm.negotiation.v1", generated_at=now.isoformat(), leagues=list(leagues), min_week=min_week,
                notes=["Descriptive small samples; no ranking. Weeks are assigned at proposal opening.",
                       "Response mix includes MESSAGE; denominator excludes infra, provider, unknown and pending gaps.",
                       "Forced-default REJECT is a no-reply; expired overlapping offers are not model no-replies.",
                       "Raw review counts are message-based; attributed outcomes are reported separately.",
                       "An own decision replaced by a default retains its attempted response; application_gap does not assign fault without a violation receipt.",
                       "Self-contradiction means raw get < give, not net lineup utility or proof of deception."],
                sources=sources, summary=summary,
                weeks={str(w): summarize([t for t in threads if t["week"] == w]) for w in sorted({t["week"] for t in threads})},
                threads=threads, data_review_comparison=dict(source="plan/DATA-REVIEW-2026-10-12.md section 4 item 4",
                reference=reference, observed=observed, delta={k: observed[k]-v for k, v in reference.items()},
                explanation="Snapshot differences are itemized; the review has no source heads, so the cause of disagreement is unverified."))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--leagues", default=",".join(LIVE_LEAGUES))
    parser.add_argument("--weeks", help="comma-separated opening weeks")
    parser.add_argument("--min-week", type=int, default=5)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    # A worktree shares read-only runtime data; exports must not enter live dirs.
    output = args.out.expanduser().resolve()
    runtime = runtime_root(ROOT)
    if any(output.is_relative_to(runtime / name) for name in ("leagues", "records", "logs", "state", "export")):
        parser.error("output must be outside live data directories")
    result = report(tuple(args.leagues.split(",")), weeks={int(w) for w in args.weeks.split(",")} if args.weeks else None, min_week=args.min_week)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n")
    print(json.dumps(dict(thread_count=result["summary"]["thread_count"], outcomes=result["summary"]["outcomes"],
                         data_review_comparison=result["data_review_comparison"],
                         self_contradicting_accepts=result["summary"]["self_contradicting_accepts"]), sort_keys=True))


if __name__ == "__main__":
    main()
