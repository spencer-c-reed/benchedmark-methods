"""Expert pickers: a descriptive human baseline for Picks (straight-up and against-the-spread hit rates).

Named pundits' published game picks (Muse captures, data/expert-picks/) are graded on the same retained results the
Picks readout uses (bm.score.picks_readout.retained_inputs: retained schedule snapshot plus read-only settlement
scores). Beside them, each model's side (probability > 0.5 for home win / home cover) and the decision-time market
favourite are graded the same way.

- Descriptive only: hit rates with n, no ranking, no significance or capability claims. Rows are alphabetical.
- Pundits are a baseline, never entrants: nothing here feeds leaderboards, ratings or the export.
- Only settled games are graded. Unsettled games are pending, never misses; ties are SU pushes; ATS pushes are pushes.
- Pundit ATS picks are graded at the pundit's own quoted line. Model ATS sides are graded at the line the model was
  scored on (its record's posted home spread). Majority ATS is graded at that same model/slate line. Each game's
  pundit quoted lines are listed beside the model line with a mismatch flag; lines are never silently mixed.

Ingest (re-runnable; merges any number of original captures, deterministic):
  python3 -m bm.score.picks_experts ingest data/expert-picks/sources/w7-muse.csv [more.csv ...]
Readout (read-only against a runtime root):
  python3 -m bm.score.picks_experts readout --week 7 [--root DIR] [--out FILE.json]
  python3 -m bm.score.picks_experts readout --weeks 5-7 [--root DIR] [--out FILE.json]   (pundits vs models aggregate)
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data" / "expert-picks" / "picks.csv"
SCHEMA = "expert-picks-readout/1"
RULE = "expert-picks-v1"
SOURCE_FIELDS = ("week game_id kickoff_et away home source picker pick_type pick line_quoted predicted_score "
                 "confidence_quoted published_at_quoted published_at_basis url read_date flag").split()
# Backfill captures (Weeks 5-6) add one column after url; both schemas are accepted.
BACKFILL_SOURCE_FIELDS = SOURCE_FIELDS[:15] + ["snapshot_url"] + SOURCE_FIELDS[15:]
FIELDS = ("season week game_id kickoff_et away home source picker pick_type pick pick_side line_quoted line_value "
          "home_line scoring_status predicted_score confidence_quoted published_at_quoted published_at_basis url "
          "snapshot_url read_date flag raw_week raw_game_id input_sha256 input_row").split()
# Capture team codes -> nflverse/BenchedMark game_id codes.
TEAM_ALIASES = {"LAR": "LA", "JAC": "JAX", "WSH": "WAS", "LVR": "LV"}
# Nickname-prefixed line quotes ("Ravens +3.5") in archive captures.
NICKNAMES = {"Cardinals": "ARI", "Falcons": "ATL", "Ravens": "BAL", "Bills": "BUF", "Panthers": "CAR",
             "Bears": "CHI", "Bengals": "CIN", "Browns": "CLE", "Cowboys": "DAL", "Broncos": "DEN", "Lions": "DET",
             "Packers": "GB", "Texans": "HOU", "Colts": "IND", "Jaguars": "JAX", "Chiefs": "KC", "Raiders": "LV",
             "Chargers": "LAC", "Rams": "LA", "Dolphins": "MIA", "Vikings": "MIN", "Patriots": "NE", "Saints": "NO",
             "Giants": "NYG", "Jets": "NYJ", "Eagles": "PHI", "Steelers": "PIT", "49ers": "SF", "Seahawks": "SEA",
             "Buccaneers": "TB", "Titans": "TEN", "Commanders": "WAS"}
PICK_TYPES = ("straight_up", "ats")
# Accepted in captures; totals are kept as rows but not graded.
CAPTURE_PICK_TYPES = PICK_TYPES + ("total",)
# Muse's own capture flags that exclude a row from scoring (row kept, status records why).
FLAG_EXCLUSIONS = {"post_kickoff_update": "excluded: post-kickoff",
                   "no_pre_kickoff_proof": "excluded: pre-kickoff not proven",
                   "uncertain_pick": "excluded: uncertain pick"}
# Backfill pick_type spelling; game_id may carry an unpadded week ("2026_W5_BAL_ATL").
PICK_TYPE_ALIASES = {"su": "straight_up"}
# Same people listed under two outlet names: collapse to one source name.
SOURCE_ALIASES = {"SI Betting": "Sports Illustrated", "Sports Illustrated (MMQB)": "Sports Illustrated",
                  "VSiN": "VSIN", "SI Road to 272": "Sports Illustrated"}
WAYBACK = re.compile(r"/web/(\d{14})(?:[a-z_]{2,3})?/")
LINE = re.compile(r"^[+-]?\d+(?:\.\d+)?$")
STATUS = ("Descriptive human baseline beside the models; unranked exhibition. Hit rates with n only: no ranking, "
          "significance or capability claim. Pundits are not entrants and never appear in leaderboards or ratings.")


# ------------------------------------------------------------------ ingest


def _team(code: str) -> str:
    code = code.strip().upper()
    return TEAM_ALIASES.get(code, code)


def _line(text: str, pick: str = "", away: str = "", home: str = "") -> tuple[float | None, str]:
    """Line from the picked team's side. A team-prefixed quote ("CHI -2.5") is turned to the picked side."""
    t = text.strip()
    if not t:
        return None, "excluded: no line quoted"
    m = re.match(r"^([A-Za-z]{2,3}|[A-Z][a-z]+|49ers)\s+(\S+)$", t)
    team = m and (NICKNAMES.get(m[1]) or _team(m[1]))
    if m and team in (away, home) and pick in (away, home):
        value, status = _line(m[2])
        if value is None:
            return value, status
        return (value if team == pick else -value) + 0.0, status
    if t.upper() in ("PK", "PICK", "PICK'EM", "EVEN"):
        return 0.0, "scorable"
    if LINE.match(t):
        return float(t), "scorable"
    if "teaser" in t.lower():
        return None, "excluded: teaser line"
    return None, "excluded: unparsed line"


def _kickoff(text: str) -> datetime | None:
    """Kickoff as an aware datetime: ISO with offset, or 'YYYY-MM-DD HH:MM[ ET]' (US Eastern: the column is kickoff_et;
    the Week 8 capture omits the suffix). None if unparseable."""
    t = text.strip()
    try:
        if t.endswith(" ET") or re.match(r"^\d{4}-\d\d-\d\d \d\d:\d\d$", t):
            from zoneinfo import ZoneInfo
            return datetime.strptime(t.removesuffix(" ET"), "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo("America/New_York"))
        d = datetime.fromisoformat(t.replace("Z", "+00:00"))
        return d if d.tzinfo else None
    except ValueError:
        return None


def pre_kickoff_proof(r: dict) -> tuple[bool, str, str]:
    """Proof that a row was published before its game's kickoff: (proven, rule, utc-sortable proof time).

    (a) Wayback timestamp in snapshot_url (UTC) earlier than kickoff; (b) published_at_basis 'page stamp' with an ISO
    time in published_at_quoted earlier than kickoff; (c) basis 'article date' with a date strictly before the kickoff
    date (same day is not proven). Nothing else counts."""
    kick = _kickoff(r["kickoff_et"])
    kday = re.match(r"^(\d{4}-\d\d-\d\d)", r["kickoff_et"].strip())
    snap = WAYBACK.search(r.get("snapshot_url", ""))
    if snap and kick:
        ts = datetime.strptime(snap[1], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        if ts < kick:
            return True, "wayback", ts.isoformat()
    basis, quoted = r["published_at_basis"].strip(), r["published_at_quoted"].strip()
    if basis == "page stamp" and kick:
        try:
            ts = datetime.fromisoformat(quoted.replace("Z", "+00:00"))
        except ValueError:
            ts = None
        if ts and ts.tzinfo and ts < kick:
            return True, "page stamp", ts.astimezone(timezone.utc).isoformat()
    if basis == "article date" and kday:
        d = re.match(r"^(\d{4}-\d\d-\d\d)", quoted)
        if d and date.fromisoformat(d[1]) < date.fromisoformat(kday[1]):
            return True, "article date", f"{d[1]}T00:00:00+00:00"
    return False, "", ""


def normalize(r: dict, sha: str, line: int, season: int) -> dict:
    """One capture row -> one normalized row. Raises on rows that cannot be identified (never guesses)."""
    away, home = _team(r["away"]), _team(r["home"])
    r = dict(r, snapshot_url=r.get("snapshot_url", ""),
             source=SOURCE_ALIASES.get(r["source"].strip(), r["source"].strip()),
             pick_type=PICK_TYPE_ALIASES.get(r["pick_type"].strip(), r["pick_type"].strip()))
    m = re.match(r"^(\d{4})_(?:W(\d{1,2})|(\d{2}))_([A-Z]+)_([A-Z]+)$", r["game_id"].strip())
    if not m:
        raise ValueError(f"{line}: bad game_id {r['game_id']!r}")
    if int(m[1]) != season or (_team(m[4]), _team(m[5])) != (away, home):
        raise ValueError(f"{line}: game_id {r['game_id']!r} disagrees with season/away/home")
    week = int(m[2] or m[3])
    raw_week = r["week"].strip()
    # Known capture quirk: the week column carries the season year. Any other disagreement is an error.
    if raw_week not in (str(week), str(season)):
        raise ValueError(f"{line}: week column {raw_week!r} disagrees with game_id week {week}")
    if r["pick_type"] not in CAPTURE_PICK_TYPES:
        raise ValueError(f"{line}: unsupported pick_type {r['pick_type']!r}")
    pick = (r["pick"].strip().lower() if r["pick_type"] == "total" else _team(r["pick"])) if r["pick"].strip() else ""
    if pick and r["pick_type"] != "total" and pick not in (away, home):
        raise ValueError(f"{line}: pick {r['pick']!r} is neither team")
    side = "" if r["pick_type"] == "total" else "home" if pick == home else "away" if pick else ""
    value, status, home_line = None, "scorable", None
    if not pick:
        status = "excluded: empty pick"
    elif r["pick_type"] == "total":
        status = "excluded: totals not graded"
    elif r["pick_type"] == "ats":
        value, status = _line(r["line_quoted"], pick, away, home)
        if value is not None:
            home_line = value if side == "home" else -value
    flag = r.get("flag", "").strip().lower()
    if "POST-KICKOFF" in (flag + r.get("published_at_quoted", "")).upper():
        status = "excluded: post-kickoff"
    elif flag in FLAG_EXCLUSIONS:
        status = FLAG_EXCLUSIONS[flag]
    else:
        kick = re.match(r"^(\d{4}-\d\d-\d\d)", r["kickoff_et"].strip())
        if kick and r["read_date"].strip() and date.fromisoformat(r["read_date"].strip()) > date.fromisoformat(kick[1]):
            # Read after kickoff day: the read date proves nothing, so require proof of publication before kickoff.
            if not pre_kickoff_proof(r)[0]:
                status = "excluded: pre-kickoff not proven"
    out = {k: r[k].strip() for k in BACKFILL_SOURCE_FIELDS}
    out.update(season=str(season), week=str(week), game_id=f"{season}_{week:02d}_{away}_{home}", away=away, home=home,
               pick=pick, pick_side=side, line_value="" if value is None else repr(value),
               home_line="" if home_line is None else repr(home_line), scoring_status=status, raw_week=raw_week,
               raw_game_id=r["game_id"].strip(), input_sha256=sha, input_row=str(line))
    return out


def ingest(paths: list[Path], out: Path = DATA, season: int = 2026) -> dict:
    """Merge original captures (input order does not matter; identical bytes ingested once).

    Identity is (season, week, game, source, picker, pick type); the latest read_date, then input hash/row, wins and
    replaced rows are listed in the manifest."""
    groups, inputs, seen = defaultdict(list), [], set()
    for path in paths:
        raw = Path(path).read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        if sha in seen:
            continue
        seen.add(sha)
        with Path(path).open(newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            if reader.fieldnames not in (SOURCE_FIELDS, BACKFILL_SOURCE_FIELDS):
                raise ValueError(f"{path}: expected the original {len(SOURCE_FIELDS)}- or "
                                 f"{len(BACKFILL_SOURCE_FIELDS)}-column schema")
            count = 0
            for line, original in enumerate(reader, 2):
                if None in original or any(v is None for v in original.values()):
                    raise ValueError(f"{path}:{line}: malformed row")
                r = normalize(original, sha, line, season)
                groups[(r["season"], r["week"], r["game_id"], r["source"], r["picker"], r["pick_type"])].append(r)
                count += 1
        inputs.append({"sha256": sha, "rows": count})
    # Timing-clean rows first, then latest read_date, then latest proven pre-kickoff publication/snapshot time (the
    # picker's final pre-kickoff pick), then input hash/row. Unproven rows have no proof time and sort first within a read date.
    # A row excluded for timing (post-kickoff or unproven) never replaces one that is not, whatever its read date.
    timing = ("excluded: post-kickoff", "excluded: pre-kickoff not proven", "excluded: read after kickoff day")
    pref = lambda r: (r["scoring_status"] not in timing, r["read_date"], pre_kickoff_proof(r)[2], r["input_sha256"],
                      int(r["input_row"]))
    selected = sorted((max(rs, key=pref) for rs in groups.values()),
                      key=lambda r: (int(r["week"]), r["game_id"], r["source"], r["picker"], r["pick_type"]))
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, FIELDS, lineterminator="\n")
        w.writeheader()
        w.writerows({k: r[k] for k in FIELDS} for r in selected)
    manifest = {"rule": RULE, "team_aliases": TEAM_ALIASES, "source_aliases": SOURCE_ALIASES,
                "pick_type_aliases": PICK_TYPE_ALIASES,
                "inputs": sorted({x["sha256"]: x for x in inputs}.values(), key=lambda x: x["sha256"]),
                "rows": len(selected), "sha256": hashlib.sha256(out.read_bytes()).hexdigest(),
                "by_week": {wk: dict(sorted(Counter(f"{r['pick_type']}: {r['scoring_status']}" for r in selected
                                                    if r["week"] == wk).items()))
                            for wk in sorted({r["week"] for r in selected}, key=int)},
                "superseded": [{"key": list(k), "kept": {f: max(rs, key=pref)[f] for f in ("input_sha256", "input_row")},
                                "replaced": [{f: r[f] for f in ("input_sha256", "input_row")}
                                             for r in sorted(rs, key=pref)[:-1]]}
                               for k, rs in sorted(groups.items()) if len(rs) > 1]}
    out.with_suffix(".manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return {k: v for k, v in manifest.items() if k != "superseded"}


# Archive-proof returns (one row per pick found in a pre-kickoff Wayback capture) -> a backfill-schema capture.
# Proof pickers named differently from earlier captures of the same outlet map to the existing name, so a pick is
# never counted twice under two spellings. Covers AI is a model, not a pundit, and is never ingested.
PROOF_FIELDS = ("week url source picker game_id pick_type pick_in_capture line_in_capture snapshot_url snapshot_utc "
                "proof_type matches_live note").split()
PROOF_PICKER_ALIASES = {("Bleacher Report", "Moe Moton"): "Maurice Moton",
                        ("Bleacher Report", "Wes O\u2019Donnell"): "Wes O'Donnell",
                        ("VikingTerritory", "Frey"): "Josh Frey",
                        ("FanSided", "Cody Williams"): "FanSided panel",
                        ("Covers", "Chris Vasile", "7"): "Covers panel"}
# USA Today's Week 8 roundup quotes the Arizona Republic's picks: Jeremy Cluff's own page, already counted.
PROOF_SKIP_PICKERS = {("Covers", "Covers AI"), ("USA Today", "Arizona Republic")}


def from_archive_proof(proof: Path, picks: Path, out: Path, read_date: str) -> dict:
    """Convert an archive-proof CSV into a capture ingest accepts: each filled capture cell becomes a row whose
    pick and line are the archived ones, published_at_basis 'wayback snapshot'. Blank capture cells, no-proof rows
    and skipped pickers are dropped (the earlier capture's row stands). kickoff/away/home come from the existing
    normalized picks; a game not already there is an error. Ingest's timing rule then keeps snapshots that postdate
    a game's own kickoff from replacing anything."""
    existing = rows(picks)
    games = {r["game_id"]: r for r in existing}
    named = {(r["week"], r["source"], r["picker"]) for r in existing}
    counts, written = Counter(), []
    with proof.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != PROOF_FIELDS:
            raise ValueError(f"{proof}: expected the archive-proof schema {PROOF_FIELDS}")
        for line, r in enumerate(reader, 2):
            if not r["picker"].strip():
                counts["no proof (page)"] += 1
                continue
            source, picker = r["source"].strip(), r["picker"].strip()
            if (source, picker) in PROOF_SKIP_PICKERS:
                counts["skipped picker"] += 1
                continue
            if not r["pick_in_capture"].strip() or r["proof_type"].strip() != "wayback":
                counts["blank in capture"] += 1
                continue
            m = re.match(r"^(\d{4})_W?(\d{1,2})_([A-Z]+)_([A-Z]+)$", r["game_id"].strip())
            if not m:
                raise ValueError(f"{proof}:{line}: bad game_id {r['game_id']!r}")
            # An alias applies only when the proof name is not already that week's name (Week 8 lists "Moe Moton").
            if (str(int(m[2])), source, picker) not in named:
                picker = PROOF_PICKER_ALIASES.get((source, picker, r["week"].strip()),
                                                  PROOF_PICKER_ALIASES.get((source, picker), picker))
            gid = f"{m[1]}_{int(m[2]):02d}_{_team(m[3])}_{_team(m[4])}"
            if gid not in games:
                raise ValueError(f"{proof}:{line}: game {gid} not in {picks}")
            g = games[gid]
            written.append({"week": str(int(m[2])), "game_id": gid, "kickoff_et": g["kickoff_et"], "away": g["away"],
                            "home": g["home"], "source": source, "picker": picker, "pick_type": r["pick_type"].strip(),
                            "pick": r["pick_in_capture"].strip(), "line_quoted": r["line_in_capture"].strip(),
                            "predicted_score": "", "confidence_quoted": "",
                            "published_at_quoted": r["snapshot_utc"].strip(), "published_at_basis": "wayback snapshot",
                            "url": r["url"].strip(), "snapshot_url": r["snapshot_url"].strip(),
                            "read_date": read_date, "flag": ""})
            counts["capture rows"] += 1
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, BACKFILL_SOURCE_FIELDS, lineterminator="\n")
        w.writeheader()
        w.writerows(written)
    return dict(counts, sha256=hashlib.sha256(out.read_bytes()).hexdigest())


def proof_tally(picks: list[dict]) -> dict:
    """Scorable straight-up/ATS pundit picks by the rule proving publication before kickoff (pre_kickoff_proof);
    'unproven' rows rest on a read before kickoff day only."""
    c = Counter(pre_kickoff_proof(r)[1] or "unproven" for r in picks
                if r["scoring_status"] == "scorable" and r["pick_type"] in PICK_TYPES)
    return {k: c.get(k, 0) for k in ("wayback", "page stamp", "article date", "unproven")} | {"scorable": sum(c.values())}


def rows(path: Path | None = None, week: int | None = None, season: int = 2026) -> list[dict]:
    path = Path(path or DATA)
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return [r for r in csv.DictReader(f)
                if r["season"] == str(season) and (week is None or int(r["week"]) == week)]


# ------------------------------------------------------------------ grading


def _result(game: dict | None) -> tuple[str, float | None]:
    """('settled', home margin) or ('pending', None)."""
    if not game or game.get("home_score") is None or game.get("away_score") is None:
        return "pending", None
    return "settled", game["home_score"] - game["away_score"]


def _grade_su(side: str, margin: float) -> str:
    if margin == 0:
        return "push"
    return "hit" if (margin > 0) == (side == "home") else "miss"


def _grade_ats(side: str, margin: float, home_line: float) -> str:
    v = margin + home_line          # home covers <=> margin + home handicap > 0
    if v == 0:
        return "push"
    return "hit" if (v > 0) == (side == "home") else "miss"


def _tally(grades: list[tuple[str, str]], **extra) -> dict:
    """grades: (game_id, 'hit'|'miss'|'push'|'pending'). Hit rate over decided (hit+miss) only."""
    c = Counter(g for _, g in grades)
    decided = c["hit"] + c["miss"]
    return dict(n=len(grades), settled_n=decided + c["push"], decided_n=decided, hits=c["hit"], misses=c["miss"],
                pushes=c["push"], pending=c["pending"], hit_rate=round(c["hit"] / decided, 4) if decided else None,
                games_n=len({g for g, _ in grades}), **extra)


def _prob(v) -> bool:
    return type(v) in (int, float) and math.isfinite(v) and 0 <= v <= 1


def _model_line(record: dict, contexts: dict, gid: str) -> float | None:
    """The home spread the model was scored on: its record's shown lines, else its slate's frozen context."""
    ln = ((record.get("lines") or {}).get(gid) or {})
    if not ln:
        ctx = contexts.get(record.get("slate") or record.get("key", "").split(":")[0]) or {}
        ln = next((g for g in ctx.get("games", []) if g.get("game_id") == gid), {})
    point = (((ln.get("ref") or {}).get("spreads") or {}).get("home") or {}).get("point")
    return float(point) if type(point) in (int, float) else None


def _model_rows(attempts: list[dict], games: dict, contexts: dict, misserved: set, week: int):
    """First own answer per slate/model/instance/step (as picks_readout); defaults, misserved, late answers excluded."""
    seen, excluded, out = set(), Counter(), []
    for r in attempts:
        if r.get("kind") != "pick" or r.get("week") != week:
            continue
        if r.get("served_by_other_model") or r.get("record_id") in misserved:
            excluded["served_by_other_model"] += 1
            continue
        if r.get("outcome") != "own" or r.get("failure") or not isinstance(r.get("answer"), dict):
            excluded[f"no own answer ({r.get('outcome', 'unknown')})"] += 1
            continue
        slate = r.get("slate") or r["key"].split(":")[0]
        seat = (slate, r["entrant"], r.get("instance"), r.get("step"))
        if seat in seen:
            excluded["duplicate_own_seat_step"] += 1
            continue
        seen.add(seat)
        for gid, a in sorted(r["answer"].items()):
            g = games.get(gid)
            if g is None:
                excluded["game not in schedule"] += 1
                continue
            if r.get("received_at") and g.get("kickoff"):
                try:
                    cutoff = datetime.fromisoformat(g["kickoff"])
                    if r.get("deadline"):
                        cutoff = min(cutoff, datetime.fromisoformat(r["deadline"]))
                    if datetime.fromisoformat(r["received_at"]) >= cutoff:
                        excluded["late_forecast"] += 1
                        continue
                except (TypeError, ValueError):
                    excluded["invalid_receipt"] += 1
                    continue
            out.append(dict(entrant=r["entrant"], step=r.get("step"), instance=r.get("instance"), slate=slate,
                            game_id=gid, answer=a if isinstance(a, dict) else {},
                            line=_model_line(r, contexts, gid)))
    return out, dict(excluded)


def _market_favourites(attempts: list[dict], games: dict, contexts: dict, week: int) -> tuple[dict, list[str]]:
    """Earliest retained pre-kickoff quote per game; legacy line records only when their slate lacks a context.

    A quote is market evidence independent of whether the model answered. Never rebuild a legacy slate from
    current feeds, use the reply time as the quote time, or resolve conflicting earliest quotes by record order.
    """
    quotes = defaultdict(list)

    def add(gid, line, clock, **provenance):
        if gid not in games:
            return
        p = (line.get("consensus") or {}).get("p_home")
        if not _prob(p):
            return
        try:
            at = datetime.fromisoformat(clock)
            cutoff = datetime.fromisoformat(games[gid]["kickoff"])
            if not at.tzinfo or not cutoff.tzinfo or at >= cutoff:
                return
        except (TypeError, ValueError, KeyError):
            return
        quotes[gid].append(dict(game_id=gid, p_home=p, as_of=at.astimezone(timezone.utc).isoformat(),
                                side=None if p == .5 else "home" if p > .5 else "away", **provenance))

    for slate, ctx in sorted(contexts.items()):
        for g in ctx.get("games", []):
            add(g["game_id"], g, ctx.get("as_of"), source="slate_context", slate=slate,
                record_id=ctx.get("record_id"))
    for r in attempts:
        if r.get("kind") != "pick" or r.get("week") != week or r.get("step") != "line":
            continue
        slate = r.get("slate") or r.get("key", "").split(":")[0]
        if not slate or slate in contexts:
            continue  # a frozen context is authoritative for its slate
        for gid, line in (r.get("lines") or {}).items():
            add(gid, line, r.get("snapshot"), source="legacy_pick_lines", slate=slate,
                record_id=r.get("record_id"))
    favourites, conflicting = {}, []
    for gid, candidates in sorted(quotes.items()):
        first = min(q["as_of"] for q in candidates)
        earliest = [q for q in candidates if q["as_of"] == first]
        if len({q["p_home"] for q in earliest}) != 1:
            conflicting.append(gid)
        elif earliest[0]["side"] is not None:
            favourites[gid] = min(earliest, key=lambda q: (q["source"], q["slate"], q["record_id"] or ""))
    return favourites, conflicting


def summarize(picks: list[dict], games: list[dict], attempts: list[dict], contexts: dict | None = None,
              misserved: set | None = None, *, week: int, season: int = 2026) -> dict:
    """Pure grading seam. `picks` are normalized rows (picks.csv); `games` carry home/away scores or None."""
    contexts, misserved = contexts or {}, misserved or set()
    by_game = {g["game_id"]: g for g in games}
    picks = [p for p in picks if int(p["week"]) == week and p["season"] == str(season)]
    slate_ids = {g.get("game_id") for c in contexts.values() for g in c.get("games", [])}
    frame_ids = sorted(set(by_game) & (slate_ids | {p["game_id"] for p in picks}) or set(by_game))
    status = {gid: _result(by_game.get(gid)) for gid in frame_ids}
    unknown = sorted({p["game_id"] for p in picks} - set(by_game))
    settled = [g for g in frame_ids if status[g][0] == "settled"]

    def grade(gid, side, kind, home_line=None):
        st, margin = status.get(gid, ("pending", None))
        if st == "pending":
            return "pending"
        return _grade_su(side, margin) if kind == "straight_up" else _grade_ats(side, margin, home_line)

    # Pundits: identity (picker, source); alphabetical, never ordered by result.
    exclusions = Counter(f"{p['pick_type']}: {p['scoring_status']}" for p in picks if p["scoring_status"] != "scorable")
    scorable = [p for p in picks if p["scoring_status"] == "scorable" and p["game_id"] in by_game]
    pundits = []
    for (picker, source) in sorted({(p["picker"], p["source"]) for p in picks}):
        mine = [p for p in picks if (p["picker"], p["source"]) == (picker, source)]
        row = dict(picker=picker, source=source)
        for kind in PICK_TYPES:
            ok = [p for p in scorable if p in mine and p["pick_type"] == kind]
            grades = [(p["game_id"], grade(p["game_id"], p["pick_side"], kind,
                                           float(p["home_line"]) if p["home_line"] else None)) for p in ok]
            row[kind] = _tally(grades, excluded=sum(1 for p in mine if p["pick_type"] == kind and p not in ok))
        pundits.append(row)

    # Models: side = probability > 0.5 (0.5 exactly is no side). ATS: explicit p_cover_home at the line step.
    model_rows, model_excluded = _model_rows(attempts, by_game, contexts, misserved, week)
    models = {}
    model_game_grades = defaultdict(lambda: defaultdict(Counter))
    for m in sorted({r["entrant"] for r in model_rows}):
        res = {}
        for step in sorted({r["step"] for r in model_rows if r["entrant"] == m}):
            mine = [r for r in model_rows if r["entrant"] == m and r["step"] == step and r["game_id"] in frame_ids]
            entry = {}
            # Only the line step asks for an explicit home-cover probability at a posted line.
            kinds = (("straight_up", "p_home"), ("ats", "p_cover_home")) if step == "line" else (("straight_up", "p_home"),)
            for kind, key in kinds:
                grades, no_side, missing = [], 0, 0
                for r in mine:
                    p = r["answer"].get(key)
                    if not _prob(p) or (kind == "ats" and r["line"] is None):
                        missing += 1
                        continue
                    if p == 0.5:
                        no_side += 1
                        continue
                    grades.append((r["game_id"], grade(r["game_id"], "home" if p > 0.5 else "away", kind, r["line"])))
                entry[kind] = _tally(grades, no_side_at_half=no_side, missing_probability_or_line=missing,
                                     sample_unit="instance-game")
            entry["slates"] = sorted({r["slate"] for r in mine})
            res[step] = entry
            for kind, key in kinds:       # per-game counts across all models, for the multi-week aggregate
                for r in mine:
                    p = r["answer"].get(key)
                    if _prob(p) and p != 0.5 and not (kind == "ats" and r["line"] is None):
                        g = grade(r["game_id"], "home" if p > 0.5 else "away", kind, r["line"])
                        model_game_grades[f"{step}:{kind}"][r["game_id"]][g] += 1
        models[m] = res

    # Model line per game (the line(s) models were scored on), for majority ATS and the mismatch table.
    model_lines = defaultdict(Counter)
    for r in model_rows:
        if r["step"] == "line" and r["line"] is not None and _prob(r["answer"].get("p_cover_home")):
            model_lines[r["game_id"]][r["line"]] += 1
    ctx_lines = {}
    for key in sorted(contexts, key=lambda k: contexts[k].get("as_of") or ""):
        for g in contexts[key].get("games", []):
            pt = (((g.get("ref") or {}).get("spreads") or {}).get("home") or {}).get("point")
            if type(pt) in (int, float):
                ctx_lines.setdefault(g["game_id"], float(pt))

    def reference_line(gid):
        return model_lines[gid].most_common(1)[0][0] if model_lines.get(gid) else ctx_lines.get(gid)

    majority, line_table = [], []
    maj_grades = {k: [] for k in PICK_TYPES}
    for gid in frame_ids:
        st, margin = status[gid]
        g = by_game[gid]
        entry = dict(game_id=gid, away=g.get("away"), home=g.get("home"), status=st,
                     home_score=g.get("home_score"), away_score=g.get("away_score"))
        for kind in PICK_TYPES:
            votes = Counter(p["pick_side"] for p in scorable if p["game_id"] == gid and p["pick_type"] == kind)
            if not votes:
                entry[kind] = None
                continue
            top = votes.most_common()
            side = None if len(top) > 1 and top[0][1] == top[1][1] else top[0][0]
            line = reference_line(gid) if kind == "ats" else None
            result = None
            if side is None:
                result = "no majority (tie)"
            elif kind == "ats" and line is None:
                result = "no model/slate line"
            else:
                result = grade(gid, side, kind, line)
                maj_grades[kind].append((gid, result))
            entry[kind] = dict(side=side, team=None if side is None else g.get(side), votes=dict(votes),
                               graded_at_home_line=line, result=result)
        majority.append(entry)
        quoted = Counter(float(p["home_line"]) for p in scorable if p["game_id"] == gid and p["pick_type"] == "ats")
        ref = reference_line(gid)
        line_table.append(dict(game_id=gid, model_home_lines={repr(k): v for k, v in sorted(model_lines[gid].items())},
                               slate_home_line=ctx_lines.get(gid), pundit_quoted_home_lines={repr(k): v for k, v in sorted(quoted.items())},
                               pundit_rows_at_other_lines=sum(v for k, v in quoted.items() if ref is None or k != ref),
                               line_mismatch=bool(quoted) and (ref is None or any(k != ref for k in quoted)) or len(model_lines[gid]) > 1))

    # Market quotes predate kickoff; legacy records retain them even without the newer slate_context record.
    fav, conflicts = _market_favourites(attempts, {gid: by_game[gid] for gid in frame_ids}, contexts, week)
    market = _tally([(gid, grade(gid, q["side"], "straight_up")) for gid, q in sorted(fav.items())],
                    basis="decision-time no-vig consensus favourite (earliest retained pre-kickoff quote)",
                    by_game=[dict(q, result=grade(gid, q["side"], "straight_up")) for gid, q in sorted(fav.items())],
                    unavailable_games=sorted(set(frame_ids) - set(fav)), conflicting_game_ids=conflicts)

    return dict(schema=SCHEMA, season=season, week=week, measurement_status=STATUS, rule=RULE,
                methods=dict(
                    straight_up="pick side vs final score; ties are pushes; unsettled games pending, never misses",
                    ats="home covers iff home margin + home handicap > 0; exact landings are pushes",
                    pundit_line="each pundit ATS pick graded at that pundit's own quoted line; rows with no numeric "
                                "line (blank, teaser) are excluded and counted",
                    model_side="probability > 0.5 for home (p_home; ATS: line-step p_cover_home); exactly 0.5 is no side",
                    model_line="line the model was scored on: its record's posted home spread (frozen slate context)",
                    market="earliest pre-kickoff frozen context, or retained line-record snapshot for legacy slates "
                           "without a context; aware quote and kickoff times required; conflicting earliest quotes withheld",
                    majority="most common pundit side per game among scorable rows; equal split is no majority; "
                             "majority ATS graded at the model/slate line (see line_comparison for quoted lines)",
                    selection="first own model answer per slate/model/instance/step; defaults and misserved excluded",
                    hit_rate="hits / (hits + misses); pushes and pending excluded from the rate, counted separately",
                    results="bm.score.picks_readout.retained_inputs (retained schedule + retained settlement scores)"),
                frame=dict(games=len(frame_ids), settled_games=len(settled), settled_game_ids=settled,
                           pending_games=len(frame_ids) - len(settled),
                           pending_game_ids=[g for g in frame_ids if g not in settled],
                           pick_rows=len(picks), scorable_pick_rows=len(scorable),
                           excluded_pick_rows=dict(sorted(exclusions.items())),
                           pick_games_not_in_schedule=unknown, model_excluded=model_excluded),
                human_baseline=dict(label="Expert pickers (human baseline; not entrants)", pundits=pundits,
                                    majority=dict(straight_up=_tally(maj_grades["straight_up"]),
                                                  ats=_tally(maj_grades["ats"]), by_game=majority)),
                market_favourite=market, models=models, line_comparison=line_table,
                model_game_grades={k: {g: dict(c) for g, c in sorted(v.items())}
                                   for k, v in sorted(model_game_grades.items())})


# ------------------------------------------------------------------ readout / CLI


def readout(week: int, root=None, *, season: int = 2026, path: Path | None = None) -> dict:
    """Read-only: retained results and model records from the runtime root, pundit rows from picks.csv."""
    from bm.score.picks_readout import retained_inputs
    path = Path(path or DATA)
    inputs = retained_inputs(week, root, season=season)
    picks = rows(path, week, season)
    result = summarize(picks, inputs["games"], inputs["attempts"], inputs["contexts"],
                       inputs["misserved"], week=week, season=season)
    result.update(pre_kickoff_proof=proof_tally(picks))
    raw = path.read_bytes() if path.exists() else b""
    result.update(generated_at=datetime.now(timezone.utc).isoformat(),
                  sources=dict(runtime_root=str(inputs["root"]), expert_picks=str(path),
                               expert_picks_sha256=hashlib.sha256(raw).hexdigest() if raw else None,
                               schedule_snapshot=inputs["schedule_source"], nfl_manifest_sha256=inputs["nfl_hash"],
                               records=inputs["record_sources"], retained_settlement_scores=len(inputs["settled_scores"])))
    return result


def wilson(hits: int, n: int, z: float = 1.959964) -> tuple[float, float] | None:
    """Wilson score interval for a proportion (descriptive; picks within a game/week are not independent)."""
    if not n:
        return None
    p = hits / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)


def _cell(hits: int, misses: int, pushes: int = 0, pending: int = 0, **extra) -> dict:
    n = hits + misses
    return dict(hits=hits, decided_n=n, pushes=pushes, pending=pending, hit_rate=round(hits / n, 4) if n else None,
                wilson95=wilson(hits, n), **extra)


def aggregate(results: dict[int, dict]) -> dict:
    """Pundits vs models over several weeks, per week and pooled. `results` maps week -> summarize()/readout() output.

    Units: pundit per-pick = pundit-picks (each identity once); pundit majority = games; models = instance-games
    pooled over all models, on the same settled games where pundits have scorable picks of that kind (line-step
    SU and ATS, blind-step SU). Pundit ATS is graded at each pundit's own quoted line, model and majority ATS at
    the model/slate line. Wilson 95% intervals treat units as independent, which they are not (picks within a game
    and models within a slate are correlated); they are descriptive only, never a ranking or a capability claim."""
    rows = {}
    for wk, r in sorted(results.items()):
        settled = set(r["frame"]["settled_game_ids"])
        by = defaultdict(Counter)
        for p in r["human_baseline"]["pundits"]:
            for kind in PICK_TYPES:
                t = p[kind]
                by[f"pundit_per_pick:{kind}"].update(hit=t["hits"], miss=t["misses"], push=t["pushes"], pending=t["pending"])
        for kind in PICK_TYPES:
            t = r["human_baseline"]["majority"][kind]
            by[f"pundit_majority:{kind}"].update(hit=t["hits"], miss=t["misses"], push=t["pushes"], pending=t["pending"])
            games = {g["game_id"] for g in r["human_baseline"]["majority"]["by_game"]
                     if g.get(kind) and g[kind].get("result") in ("hit", "miss", "push")}
            for step_kind, per_game in r["model_game_grades"].items():
                step, mkind = step_kind.split(":")
                if mkind != kind:
                    continue
                # Same games as the pundit pool: settled games on which pundits have a graded majority/scorable pick.
                for gid, c in per_game.items():
                    if gid in settled and gid in _pundit_games(r, kind):
                        by[f"models_{step}:{kind}"].update(hit=c.get("hit", 0), miss=c.get("miss", 0), push=c.get("push", 0))
        by["market_favourite:straight_up"].update(hit=r["market_favourite"]["hits"], miss=r["market_favourite"]["misses"],
                                                  push=r["market_favourite"]["pushes"], pending=r["market_favourite"]["pending"])
        rows[wk] = dict(by)
    keys = sorted({k for c in rows.values() for k in c})
    def cells(selector):
        return {k: _cell(sum(selector[w].get(k, Counter())["hit"] for w in selector),
                         sum(selector[w].get(k, Counter())["miss"] for w in selector),
                         sum(selector[w].get(k, Counter())["push"] for w in selector),
                         sum(selector[w].get(k, Counter())["pending"] for w in selector)) for k in keys}
    return dict(schema="expert-picks-aggregate/1", rule=RULE, measurement_status=STATUS, weeks=sorted(rows),
                per_week={str(w): cells({w: rows[w]}) for w in sorted(rows)}, pooled=cells(rows),
                games_per_week={str(w): dict(games=results[w]["frame"]["games"], settled=results[w]["frame"]["settled_games"])
                                for w in sorted(results)},
                pre_kickoff_proof_per_week={str(w): results[w]["pre_kickoff_proof"] for w in sorted(results)
                                            if "pre_kickoff_proof" in results[w]},
                market_evidence_per_week={str(w): {k: results[w]["market_favourite"].get(k, [])
                                                   for k in ("by_game", "unavailable_games", "conflicting_game_ids")}
                                          for w in sorted(results)},
                methods=dict(
                    units="pundit_per_pick: pundit picks; pundit_majority and market_favourite: games; models_*: "
                          "instance-games pooled over all models on the same settled games",
                    interval="Wilson 95% on hits/(hits+misses); picks/instances are correlated, so intervals are "
                             "descriptive and too narrow for any inference",
                    market="earliest pre-kickoff quote; legacy line-record snapshots used only for slates without "
                           "a frozen context; per-game source and quote time in market_evidence_per_week",
                    lines="pundit ATS at own quoted line; majority and model ATS at the model/slate line"))


def _pundit_games(r: dict, kind: str) -> set:
    """Settled games on which at least one scorable pundit pick of this kind was graded (majority rows carry votes)."""
    return {g["game_id"] for g in r["human_baseline"]["majority"]["by_game"] if g.get(kind)}


def aggregate_markdown(a: dict) -> str:
    def cell(c):
        if not c["decided_n"]:
            return "-"
        lo, hi = c["wilson95"]
        return f"{c['hits']}/{c['decided_n']} = {c['hit_rate']:.3f} [{lo:.3f}, {hi:.3f}]"
    labels = [("pundit_per_pick", "Pundits, per pick"), ("pundit_majority", "Pundits, majority per game"),
              ("models_blind", "Models, blind step"), ("models_line", "Models, line step"),
              ("market_favourite", "Market favourite")]
    out = [f"# Pundits vs models: Weeks {a['weeks'][0]}-{a['weeks'][-1]}", "", a["measurement_status"], "",
           "Cells are hits/decided = rate [Wilson 95%]. " + a["methods"]["interval"] + ". " + a["methods"]["lines"] + ".", ""]
    cols = [(f"Week {w}", a["per_week"][str(w)]) for w in a["weeks"]] + [("Pooled", a["pooled"])]
    for kind, title in (("straight_up", "Straight up"), ("ats", "Against the spread")):
        out += [f"## {title}", "", "| Group | " + " | ".join(c for c, _ in cols) + " |", "|---|" + "---|" * len(cols)]
        for key, label in labels:
            if not any(f"{key}:{kind}" in d and d[f"{key}:{kind}"]["decided_n"] for _, d in cols):
                continue
            out.append(f"| {label} | " + " | ".join(cell(d[f"{key}:{kind}"]) if f"{key}:{kind}" in d else "-" for _, d in cols) + " |")
        out.append("")
    out += ["Games per week: " + ", ".join(f"W{w} {g['settled']}/{g['games']} settled" for w, g in a["games_per_week"].items()), ""]
    if a.get("pre_kickoff_proof_per_week"):
        out += ["Scorable pundit picks by pre-kickoff proof (archive snapshot / page time stamp / earlier article date / "
                "read before kickoff day only): " + "; ".join(
                    f"W{w} {p['wayback']} wayback, {p['page stamp']} page stamp, {p['article date']} article date, "
                    f"{p['unproven']} unproven of {p['scorable']}" for w, p in a["pre_kickoff_proof_per_week"].items()), ""]
    for w, evidence in a.get("market_evidence_per_week", {}).items():
        legacy = sum(g["source"] == "legacy_pick_lines" for g in evidence["by_game"])
        if legacy:
            out += [f"Week {w} market: {legacy} games use retained legacy line-record quotes; no frozen slate context. "
                    "Quote provenance is retained in the JSON; this does not establish prospective model receipts.", ""]
    return "\n".join(out)


def markdown(r: dict) -> str:
    def rate(t):
        if not t["n"]:
            return "-"
        return "pending" if not t["decided_n"] else f"{t['hits']}/{t['decided_n']} ({t['hit_rate']:.3f})"
    f = r["frame"]
    out = [f"# Expert pickers: Week {r['week']} human baseline", "", r["measurement_status"], "",
           f"Games {f['games']}: settled {f['settled_games']}, pending {f['pending_games']} "
           f"(pending: {', '.join(f['pending_game_ids']) or 'none'}). Pick rows {f['pick_rows']}, scorable "
           f"{f['scorable_pick_rows']}; excluded {json.dumps(f['excluded_pick_rows'], sort_keys=True)}.", "",
           "## Pundit majority", ""]
    hb = r["human_baseline"]
    out += [f"- Straight-up: {rate(hb['majority']['straight_up'])}; pending {hb['majority']['straight_up']['pending']}.",
            f"- ATS (graded at the model/slate line): {rate(hb['majority']['ats'])}; pending {hb['majority']['ats']['pending']}.",
            f"- Market favourite (straight-up): {rate(r['market_favourite'])}; pending {r['market_favourite']['pending']}.", ""]
    legacy = sum(g["source"] == "legacy_pick_lines" for g in r["market_favourite"].get("by_game", []))
    if legacy:
        out += [f"Market source: {legacy} games use retained legacy line-record quotes without a frozen slate "
                "context. Per-game quote provenance is in the JSON; prospective model receipts remain unproven.", ""]
    out += ["## Pundits (alphabetical; hits/decided)", "", "| Picker | Source | SU | SU n | SU pending | ATS | ATS n | ATS pending | ATS excluded |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for p in hb["pundits"]:
        su, ats = p["straight_up"], p["ats"]
        out.append(f"| {p['picker']} | {p['source']} | {rate(su)} | {su['n']} | {su['pending']} | "
                   f"{rate(ats)} | {ats['n']} | {ats['pending']} | {ats['excluded']} |")
    out += ["", "## Models (side = probability > 0.5; instance-games)", "", "| Model | Step | SU | SU pending | ATS | ATS pending |",
            "|---|---|---:|---:|---:|---:|"]
    for m, steps in r["models"].items():
        for step, e in steps.items():
            su, ats = e.get("straight_up"), e.get("ats")
            out.append(f"| {m} | {step} | {rate(su) if su else '-'} | {su['pending'] if su else '-'} | "
                       f"{rate(ats) if ats else '-'} | {ats['pending'] if ats else '-'} |")
    out += ["", "## Line comparison (home spread)", "", "| Game | Model line(s) | Pundit quoted lines (rows) | Mismatch |", "|---|---|---|---|"]
    for row in r["line_comparison"]:
        out.append(f"| {row['game_id']} | {', '.join(row['model_home_lines']) or row['slate_home_line'] or '-'} | "
                   f"{', '.join(f'{k} ({v})' for k, v in row['pundit_quoted_home_lines'].items()) or '-'} | "
                   f"{'yes' if row['line_mismatch'] else 'no'} |")
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("ingest")
    i.add_argument("csvs", type=Path, nargs="+")
    i.add_argument("--out", type=Path, default=DATA)
    i.add_argument("--season", type=int, default=2026)
    pr = sub.add_parser("from-proof", help="archive-proof CSV -> backfill-schema capture for ingest")
    pr.add_argument("proof", type=Path)
    pr.add_argument("out", type=Path)
    pr.add_argument("--picks", type=Path, default=DATA)
    pr.add_argument("--read-date", required=True, help="date the archived captures were read (YYYY-MM-DD)")
    r = sub.add_parser("readout")
    g = r.add_mutually_exclusive_group(required=True)
    g.add_argument("--week", type=int)
    g.add_argument("--weeks", help="inclusive range such as 5-7: multi-week pundits-vs-models aggregate")
    r.add_argument("--season", type=int, default=2026)
    r.add_argument("--root", type=Path)
    r.add_argument("--picks", type=Path, default=DATA)
    r.add_argument("--out", type=Path, help="JSON output (a .md beside it); outside the live checkout")
    args = parser.parse_args(argv)
    if args.cmd == "ingest":
        print(json.dumps(ingest(args.csvs, args.out, args.season), indent=2))
        return 0
    if args.cmd == "from-proof":
        print(json.dumps(from_archive_proof(args.proof, args.picks, args.out, args.read_date), indent=2))
        return 0
    if args.out:
        from bm.paths import runtime_root
        live = runtime_root(ROOT).resolve()
        if live != ROOT and args.out.resolve().is_relative_to(live):
            parser.error("--out must be outside the shared live checkout")
    if args.weeks:
        lo, _, hi = args.weeks.partition("-")
        weeks = range(int(lo), int(hi or lo) + 1)
        per = {w: readout(w, args.root, season=args.season, path=args.picks) for w in weeks}
        agg = aggregate(per)
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(agg, indent=2, sort_keys=True, allow_nan=False) + "\n")
            args.out.with_suffix(".md").write_text(aggregate_markdown(agg))
        print(aggregate_markdown(agg))
        return 0
    result = readout(args.week, args.root, season=args.season, path=args.picks)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n")
        args.out.with_suffix(".md").write_text(markdown(result))
    print(markdown(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
