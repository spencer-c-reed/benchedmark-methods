"""Restart Picks prompt set (a NEW prompt version; the original prompts in bm.picks.run stay byte-identical).

Differences from the original slate prompts:
  line step  each game's posted spread and posted total are stated as the propositions asked about: P(home covers
             the posted spread of X) and P(over the posted total of Y), at the reference book's decision-time line,
             plus P(home wins). These are the model's decision-time probabilities the Picks CLV endpoint
             (bm.score.picks_clv) compares with the decision-time and closing no-vig market.
  blind step unchanged ask (total and home-margin quantiles, P(home wins)); no betting lines. Its briefing is
             bettor-parity (bm.picks.briefing): full injury report with designations, player news, rest, travel,
             venue/roof/surface, all as-of filtered, and every rendered blind prompt passes briefing.leak_scan.

PROMPT_VERSION names the set; template_hash() is the sha256 of both steps rendered on a fixed fixture slate, so any
change to the wording changes the hash (pinned in tests/test_picks_prompts_restart.py). A change is a new version.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
PROMPT_VERSION = "picks-restart-1"
MAX_STAKE_FRAC = 0.05


def build_sheet(week: int, at: datetime, games: list[dict]) -> dict:
    from bm.picks import briefing
    return briefing.build(week, at, games)


def leak_scan(prompt: str, at: datetime) -> list[str]:
    from bm.picks import briefing
    return briefing.leak_scan(prompt, at)


def blind_prompt(games: list[dict], sheet: dict) -> str:
    from bm.picks import briefing
    return ("STEP 1 (blind). Using only this briefing (records and scoring, the full injury report, player news, rest, travel "
            "and venue; no betting lines), for EACH game give: the median and 80% range (p10, p90) of total points; the median "
            "and 80% range of home margin (home minus away); P(home wins).\n"
            + "\n".join(briefing.render(games, sheet)) +
            '\nReply: {"games": {"<game_id>": {"total": {"p10": x, "p50": x, "p90": x}, "margin": {"p10": x, "p50": x, "p90": x}, "p_home": p}, ...}}')


def line_prompt(games: list[dict], bankroll: float) -> str:
    rows = []
    for g in games:
        r = g["ref"]
        sp, tt, ml = r["spreads"], r["totals"], r["h2h"]
        h, a = g["home"], g["away"]
        rows.append(f"- {g['game_id']}: {a} at {h}: posted spread {h} {sp['home']['point']:+g} ({sp['home']['price']:+d}) / "
                    f"{a} {sp['away']['point']:+g} ({sp['away']['price']:+d}); posted total {tt['over']['point']:g} "
                    f"(over {tt['over']['price']:+d} / under {tt['under']['price']:+d}); moneyline {h} {ml['home']['price']:+d} / "
                    f"{a} {ml['away']['price']:+d}\n"
                    f"    p_cover_home = P({h} covers {sp['home']['point']:+g}, i.e. {h} score + ({sp['home']['point']:+g}) > {a} score); "
                    f"p_over = P(total points > {tt['over']['point']:g}); p_home = P({h} wins)")
    cap = round(bankroll * MAX_STAKE_FRAC, 2)
    return (f"STEP 2 (against the line). Lines at {games[0]['ref_book']}, snapshot {games[0]['snapshot']}:\n" + "\n".join(rows) +
            f"\nBankroll: ${bankroll:.2f} of a $1,000 starting bankroll (simulated books; no real money is wagered). For EACH game give "
            "p_cover_home (the home side covers the posted spread), p_over (the total goes over the posted total) and p_home (the home "
            f"side wins), each your own probability at these posted lines, and optionally stakes in dollars at these prices: a stake of $0 "
            f"is a pass; at most ${cap:.2f} per pick.\n"
            'Reply: {"games": {"<game_id>": {"p_cover_home": p, "p_over": p, "p_home": p, "stakes": [{"market": "totals|spreads|h2h", "side": "over|under|home|away", "usd": x}]}, ...}}')


# A fixed slate for the template hash (never real data).
_FIX_GAME = {"game_id": "2026_07_AAA_BBB", "home": "BBB", "away": "AAA", "kickoff": "2026-10-25T13:00:00-04:00",
             "ref_book": "draftkings", "snapshot": "2026-10-25T11:10:00+00:00",
             "ref": {"h2h": {"home": {"price": -150}, "away": {"price": 130}},
                     "spreads": {"home": {"point": -3.5, "price": -110}, "away": {"point": 3.5, "price": -110}},
                     "totals": {"over": {"point": 44.5, "price": -110}, "under": {"point": 44.5, "price": -110}}}}
_FIX_TEAM = {"w": 3, "l": 2, "t": 0, "pf": 110, "pa": 100, "g": 5,
             "injuries": [{"name": "X Y", "pos": "QB", "status": "Questionable", "injury": "Knee", "practice": "Limited",
                           "seen": "2026-10-23T20:00Z"}],
             "news": [{"name": "X Y", "pos": "QB", "text": "injury Questionable (Knee)", "updated": "2026-10-23T18:00Z"}]}
_FIX_SHEET = {"teams": {"AAA": _FIX_TEAM, "BBB": _FIX_TEAM},
              "games": {"2026_07_AAA_BBB": {"stadium": "S", "neutral": False, "roof": "outdoors", "surface": "grass",
                                            "weather": "no forecast source on disk", "travel": {"away": {"miles": 900, "tz_hours": -1.0}},
                                            "rest_days": {"away": 7, "home": 6}}}}


def template_hash() -> str:
    sheet = _FIX_SHEET
    text = blind_prompt([_FIX_GAME], sheet) + "\n\x00\n" + line_prompt([_FIX_GAME], 1000.0)
    return hashlib.sha256((PROMPT_VERSION + "\n" + text).encode()).hexdigest()
