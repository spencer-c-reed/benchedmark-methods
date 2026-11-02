"""Independent re-scorer (plan-v3 build 12, gate G2): recompute every scored week from the rules file and the raw
nflverse CSVs, without the engine's scoring code, and compare with the league logs.

Independence: this module parses the league's rules YAML itself (scoring, kicking, defense, points_allowed) and reads
the gzipped nflverse snapshots itself (data/raw/nfl/manifest.jsonl -> snapshots/...). It never imports
bm.engine.scoring, bm.engine.rules or bm.stats.nflverse. Rule keys map to stat columns through its own table (COLUMNS);
an unknown rule key is reported, never skipped silently.

For every week_scored event (the latest revision of each week) in each league (read-only):
  * pinned:  the stat snapshots the event names in data_refs (matched by sha256) plus the games table as of the event
             (opponent final score -> points allowed). Each starter's points and each team total are recomputed and
             compared with week_scored.detail[team].starters[].points and .points. Any difference > TOL is a mismatch,
             reported with player, slot, position, the per-rule breakdown (stat, value, rule weight) and both totals.
  * latest:  the same starters against the newest snapshots on disk: differences are stat corrections not yet
             re-scored (informational, not mismatches).
Irregular games: teams in the event's incomplete_games / no_games score no stats (the engine's rule).
Missing data: a week with no week_scored event is reported "not_scored"; a data_ref whose snapshot is not on disk is
reported "snapshot_missing" (the week is not counted as matched).

CLI:
  python3 -m bm.score.rescore_check [--leagues nfl-l1 ...] [--weeks 5 6] [--json PATH]
  python3 -m bm.score.rescore_check --daily        # writes records/rescore-check/<UTC date>.json in the data root
Exit status 0 when every checked week matches, 1 on any mismatch or unreadable pinned snapshot.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import sys
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
LEAGUES = ("nfl-l1", "nfl-l2", "nfl-l3", "nfl-l4", "nfl-l5", "nfl-l6", "nfl-f1", "nfl-f2")
TOL = 0.005
SEASON = 2026

# rule key -> nflverse column(s) summed. Keys absent here map to the column of the same name.
COLUMNS = {
    "fumbles_lost": ("rushing_fumbles_lost", "receiving_fumbles_lost", "sack_fumbles_lost"),
    "fg_made_0_39": ("fg_made_0_19", "fg_made_20_29", "fg_made_30_39"),
    "fg_made_50_plus": ("fg_made_50_59", "fg_made_60_"),
    "blocked_kicks": ("def_punt_blocks", "def_fg_blocks", "def_pat_blocks"),
}


def data_root() -> Path:
    """The checkout whose data/ and leagues/ are read: this one if it has a manifest, else the serving checkout."""
    if (ROOT / "data" / "raw" / "nfl" / "manifest.jsonl").exists():
        return ROOT
    from bm.paths import runtime_root
    return runtime_root(ROOT)


# ------------------------------------------------------------------ rules (parsed here, not by bm.engine.rules)

def load_rules(path: Path) -> dict:
    r = yaml.safe_load(Path(path).read_text())
    out = {"offense": dict(r.get("scoring") or {}), "kicking": dict(r.get("kicking") or {}), "defense": {},
           "points_allowed": []}
    d = dict(r.get("defense") or {})
    out["points_allowed"] = [(float(mx), float(p)) for mx, p in d.pop("points_allowed", [])]
    out["defense"] = d
    return out


def _num(v) -> float:
    if v in (None, "", "NA"):
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _components(table: dict, line: dict, header: set) -> tuple[list[dict], list[str]]:
    comps, unknown = [], []
    for key, w in table.items():
        cols = COLUMNS.get(key, (key,))
        if not any(c in header for c in cols):
            unknown.append(key)
        val = sum(_num(line.get(c)) for c in cols)
        if val:
            comps.append({"rule": key, "stat": "+".join(cols), "value": val, "weight": float(w), "points": round(val * float(w), 4)})
    return comps, unknown


def pa_points(rules: dict, allowed: float) -> float:
    for mx, p in rules["points_allowed"]:
        if allowed <= mx:
            return p
    return rules["points_allowed"][-1][1]


def player_points(rules: dict, pos: str, line: dict | None, header: set) -> tuple[float, list[dict], list[str]]:
    """Points for one stat line under the rules, with the per-rule breakdown and any rule keys with no column."""
    if not line:
        return 0.0, [], []
    if pos == "DEF":
        comps, unk = _components(rules["defense"], line, header)
        pa = _num(line.get("points_allowed"))
        comps.append({"rule": "points_allowed", "stat": "opponent_final_score", "value": pa, "weight": None,
                      "points": pa_points(rules, pa)})
    else:
        comps, unk = _components(rules["offense"], line, header)
        if pos == "K":
            c2, u2 = _components(rules["kicking"], line, header)
            comps += c2
            unk += u2
    return round(sum(c["points"] for c in comps), 2), comps, unk


# ------------------------------------------------------------------ raw data (read here, not by bm.stats.nflverse)

def manifest(root: Path) -> list[dict]:
    p = root / "data" / "raw" / "nfl" / "manifest.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


@lru_cache(maxsize=16)
def _read(path: str, verify_sha: str | None = None) -> tuple[tuple[dict, ...], tuple[str, ...]]:
    raw = gzip.open(path, "rb").read()
    if verify_sha and hashlib.sha256(raw).hexdigest() != verify_sha:
        raise ValueError(f"{path}: sha256 does not match the manifest")
    rd = csv.DictReader(io.StringIO(raw.decode("utf-8")))
    rows = tuple(rd)
    return rows, tuple(rd.fieldnames or ())


def snapshot(root: Path, man: list[dict], asset: str, sha: str | None = None, as_of: datetime | None = None) -> dict | None:
    """A manifest row: by sha256 (pinned), else the newest retrieved at or before as_of (None = newest)."""
    best = None
    for m in man:
        if m["asset"] != asset:
            continue
        if sha is not None:
            if m["sha256"] == sha:
                return m
            continue
        if as_of is not None and datetime.fromisoformat(m["retrieved_at"]) > as_of:
            continue
        best = m
    return best


def load_rows(root: Path, m: dict | None) -> tuple[tuple[dict, ...], set] | None:
    if not m:
        return None
    p = root / m["path"]
    if not p.exists():
        return None
    rows, header = _read(str(p), m["sha256"])
    return rows, set(header)


def week_lines(player_rows, team_rows, games_rows, week: int, season: int = SEASON) -> dict[str, dict]:
    """pid -> stat line for REG `week`; DEF-<team> lines from team stats with the opponent's final score."""
    out = {}
    for r in player_rows:
        if r.get("season_type") == "REG" and r.get("week") == str(week):
            out[r["player_id"]] = r
    games = {g["game_id"]: g for g in games_rows if g.get("season") == str(season) and g.get("game_type") == "REG"}
    for r in team_rows:
        if r.get("season_type") != "REG" or r.get("week") != str(week):
            continue
        g = games.get(r["game_id"])
        if not g:
            continue
        opp = g["home_score"] if r["team"] == g["away_team"] else g["away_score"]
        if opp in ("", "NA", None):
            continue
        out[f"DEF-{r['team']}"] = dict(r, points_allowed=opp)
    return out


def positions(roster_rows, week: int) -> dict[str, str]:
    """gsis_id -> position from the weekly roster (latest week <= `week` a player appears in)."""
    best: dict[str, tuple[int, str]] = {}
    for r in roster_rows:
        try:
            w = int(r.get("week") or 0)
        except ValueError:
            continue
        g = r.get("gsis_id")
        if g and w <= week and (g not in best or w >= best[g][0]):
            best[g] = (w, r.get("position") or "")
    return {g: p for g, (_, p) in best.items()}


# ------------------------------------------------------------------ leagues

def league_events(root: Path, league_id: str) -> tuple[dict, list[dict]]:
    d = root / "leagues" / league_id
    meta = json.loads((d / "league.json").read_text())
    evs = []
    for l in (d / "log.jsonl").read_text().splitlines():
        if l.strip():
            e = json.loads(l)
            evs.append(e.get("payload", e))
    return meta, evs


def latest_scored(evs: list[dict]) -> dict[int, dict]:
    out = {}
    for e in evs:
        if e.get("type") == "week_scored":
            out[int(e["week"])] = e                 # the last revision wins
    return out


def _excluded_teams(ev: dict, games_rows) -> set[str]:
    ids = {g["game_id"] for g in (ev.get("incomplete_games") or []) + (ev.get("no_games") or [])}
    return {t for g in games_rows if g.get("game_id") in ids for t in (g.get("home_team"), g.get("away_team"))}


def _pos_of(pid: str, pos: dict, lines: dict) -> str:
    if pid.startswith("DEF-"):
        return "DEF"
    return pos.get(pid) or (lines.get(pid) or {}).get("position") or ""


def check_week(root: Path, man: list[dict], rules: dict, ev: dict, week: int) -> dict:
    refs = {r["asset"]: r for r in ev.get("data_refs") or []}
    when = datetime.fromisoformat(ev.get("logged_at") or ev["ts"])
    out = {"week": week, "status": ev.get("status"), "revision": ev.get("revision", 0), "scored_at": ev["ts"],
           "data_refs": list(refs.values()), "teams": 0, "starters": 0, "matched_starters": 0, "mismatches": [],
           "team_mismatches": [], "unknown_rule_keys": [], "stat_corrections_since": [], "problems": [], "match": False}
    pinned = {}
    for asset in (f"stats_player_week_{SEASON}", f"stats_team_week_{SEASON}"):
        r = refs.get(asset)
        m = snapshot(root, man, asset, sha=r["sha256"]) if r else snapshot(root, man, asset, as_of=when)
        rows = load_rows(root, m)
        if rows is None:
            out["problems"].append({"kind": "snapshot_missing", "asset": asset, "ref": r})
            return out
        pinned[asset] = rows
    games = load_rows(root, snapshot(root, man, "games", as_of=when))
    roster = load_rows(root, snapshot(root, man, f"roster_weekly_{SEASON}", as_of=when)) or ((), set())
    if games is None:
        out["problems"].append({"kind": "snapshot_missing", "asset": "games"})
        return out
    ps, ph = pinned[f"stats_player_week_{SEASON}"]
    ts, th = pinned[f"stats_team_week_{SEASON}"]
    lines = week_lines(ps, ts, games[0], week)
    pos = positions(roster[0], week)
    gone = _excluded_teams(ev, games[0])
    latest = None
    lp = load_rows(root, snapshot(root, man, f"stats_player_week_{SEASON}"))
    lt = load_rows(root, snapshot(root, man, f"stats_team_week_{SEASON}"))
    lg = load_rows(root, snapshot(root, man, "games"))
    if lp and lt and lg:
        latest = week_lines(lp[0], lt[0], lg[0], week)
    unknown = set()
    for team, d in sorted((ev.get("detail") or {}).items()):
        out["teams"] += 1
        total = 0.0
        for s in d.get("starters") or []:
            pid = s.get("player")
            if not pid:
                continue
            out["starters"] += 1
            p = _pos_of(pid, pos, lines)
            ln = lines.get(pid)
            if ln is not None and (pid[4:] if pid.startswith("DEF-") else ln.get("team")) in gone:
                ln = None
            hdr = th if p == "DEF" else ph
            mine, comps, unk = player_points(rules, p, ln, hdr)
            unknown |= set(unk)
            total += mine
            logged = float(s.get("points") or 0.0)
            if abs(mine - logged) > TOL:
                out["mismatches"].append({"team": team, "slot": s.get("slot"), "player": pid, "position": p,
                                          "logged": logged, "rescored": mine, "diff": round(mine - logged, 2),
                                          "breakdown": comps, "has_line": ln is not None})
            else:
                out["matched_starters"] += 1
            if latest is not None:
                ln2 = latest.get(pid)
                if ln2 is not None and (pid[4:] if pid.startswith("DEF-") else ln2.get("team")) in gone:
                    ln2 = None
                now_pts, comps2, _ = player_points(rules, p, ln2, th if p == "DEF" else ph)
                if abs(now_pts - mine) > TOL:
                    b0 = {c["rule"]: c["value"] for c in comps}
                    b1 = {c["rule"]: c["value"] for c in comps2}
                    out["stat_corrections_since"].append({
                        "team": team, "player": pid, "position": p, "scored": mine, "latest": now_pts,
                        "diff": round(now_pts - mine, 2),
                        "changed": {k: [b0.get(k, 0.0), b1.get(k, 0.0)] for k in sorted(set(b0) | set(b1)) if b0.get(k) != b1.get(k)}})
        total = round(total, 2)
        logged_total = float(d.get("points") or 0.0)
        if abs(total - logged_total) > TOL:
            out["team_mismatches"].append({"team": team, "logged": logged_total, "rescored": total, "diff": round(total - logged_total, 2)})
    out["unknown_rule_keys"] = sorted(unknown)
    out["match"] = not out["mismatches"] and not out["team_mismatches"] and not out["problems"]
    return out


def check(leagues=LEAGUES, weeks: list[int] | None = None, root: Path | None = None) -> dict:
    root = root or data_root()
    man = manifest(root)
    report = {"generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(), "root": str(root),
              "leagues": {}, "summary": {}}
    by_week: dict[int, dict] = {}
    for lid in leagues:
        if not (root / "leagues" / lid / "log.jsonl").exists():
            report["leagues"][lid] = {"problem": "no league log"}
            continue
        meta, evs = league_events(root, lid)
        rules = load_rules(root / meta["rules_file"])
        scored = latest_scored(evs)
        want = sorted(set(weeks) if weeks else set(scored))
        res = {"rules_file": meta["rules_file"], "weeks": {}}
        for w in want:
            if w not in scored:
                res["weeks"][w] = {"week": w, "not_scored": True}
                continue
            r = check_week(root, man, rules, scored[w], w)
            res["weeks"][w] = r
            s = by_week.setdefault(w, {"leagues": 0, "teams": 0, "starters": 0, "matched_starters": 0, "mismatches": 0,
                                       "team_mismatches": 0, "problems": 0, "stat_corrections_since": 0, "match": True})
            s["leagues"] += 1
            for k in ("teams", "starters", "matched_starters"):
                s[k] += r[k]
            for k in ("mismatches", "team_mismatches", "problems", "stat_corrections_since"):
                s[k] += len(r[k])
            s["match"] = s["match"] and r["match"]
        report["leagues"][lid] = res
    for w in sorted(set(weeks or []) - set(by_week)):
        by_week[w] = {"not_scored_anywhere": True, "match": None}
    report["summary"] = {str(w): by_week[w] for w in sorted(by_week)}
    report["ok"] = all(v.get("match") is not False for v in by_week.values())
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Independent re-scorer: recompute scored weeks from the rules file and raw nflverse CSVs.")
    ap.add_argument("--leagues", nargs="*", default=list(LEAGUES))
    ap.add_argument("--weeks", nargs="*", type=int, default=None)
    ap.add_argument("--json", type=Path, default=None, help="write the full report here")
    ap.add_argument("--daily", action="store_true", help="write records/rescore-check/<UTC date>.json in the data root")
    ap.add_argument("--root", type=Path, default=None, help="checkout to read (default: this one if it has data, else the serving one)")
    a = ap.parse_args(argv)
    rep = check(a.leagues, a.weeks, a.root)
    out = a.json
    if a.daily and out is None:
        out = Path(rep["root"]) / "records" / "rescore-check" / f"{rep['generated_at'][:10]}.json"
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rep, indent=1, default=str))
    for w, s in rep["summary"].items():
        if s.get("not_scored_anywhere"):
            print(f"week {w}: not scored in any league yet")
            continue
        print(f"week {w}: {s['leagues']} leagues, {s['teams']} teams, starters {s['matched_starters']}/{s['starters']} match, "
              f"player mismatches {s['mismatches']}, team-total mismatches {s['team_mismatches']}, problems {s['problems']}, "
              f"stat corrections since scoring {s['stat_corrections_since']}: {'MATCH' if s['match'] else 'MISMATCH'}")
    for lid, r in rep["leagues"].items():
        for w, x in (r.get("weeks") or {}).items():
            for m in x.get("mismatches") or []:
                print(f"  {lid} w{w} {m['team']} {m['slot']} {m['player']} ({m['position']}): logged {m['logged']} rescored "
                      f"{m['rescored']} [{'; '.join(f'{c['rule']}={c['value']}x{c['weight']}' for c in m['breakdown'])}]")
            for p in x.get("problems") or []:
                print(f"  {lid} w{w} PROBLEM {p}")
    if out:
        print(f"wrote {out}")
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
