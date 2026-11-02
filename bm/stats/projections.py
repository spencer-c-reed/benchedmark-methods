from __future__ import annotations
from typing import Mapping
from bm.engine.rules import Rules
from bm.engine.scoring import score_player_line
K_PRIOR = 3.0
REPLACEMENT = {'QB': 12.0, 'RB': 6.0, 'WR': 6.0, 'TE': 4.5, 'K': 7.0, 'DEF': 6.0}
STATUS_FACTOR = {'Out': 0.0, 'Doubtful': 0.1, 'Questionable': 0.8}
DEPARTED_STATUS = {'CUT': 'FA', 'DEV': 'PS', 'RET': 'RET'}

def build(rules: Rules, players: Mapping[str, Mapping], lines: Mapping[int, Mapping[str, Mapping]], prior: Mapping[str, list], injuries: list[dict], week: int, teams_playing: set[str], extra_status: Mapping[str, str] | None=None, fresh: bool=False) -> dict[str, dict]:
    status = {}
    for r in injuries:
        if r.get('report_status'):
            status[r['gsis_id']] = r['report_status']
    status.update(extra_status or {})
    out = {}
    for pid, p in players.items():
        pos = p['pos']
        pts = [score_player_line(rules, pos, wk[pid]) for w, wk in sorted(lines.items()) if w < week and pid in wk]
        g = len(pts)
        ytd = round(sum(pts), 2)
        ppg = ytd / g if g else 0.0
        pl = prior.get(pid, []) if pos != 'DEF' else []
        if pl:
            pp = [score_player_line(rules, pos, r) for r in pl]
            prior_ppg = sum(pp) / len(pp)
        else:
            prior_ppg = REPLACEMENT.get(pos, 5.0) * (0.6 if pos not in ('K', 'DEF') else 1.0)
        ros = (g * ppg + K_PRIOR * prior_ppg) / (g + K_PRIOR)
        st = status.get(pid)
        if p.get('roster_status') == 'RES':
            st = st or 'IR'
        gone = fresh and p.get('roster_status') in DEPARTED_STATUS
        if gone:
            st = DEPARTED_STATUS[p['roster_status']]
        factor = 0.0 if st in ('IR',) or gone else STATUS_FACTOR.get(st, 1.0)
        wk = ros * factor if p['team'] in teams_playing else 0.0
        avail = 0.0 if gone else 0.3 if st == 'IR' else 0.75 if st == 'Out' else 1.0
        out[pid] = {'value': round(ros * avail, 2), 'ros_ppg': round(ros, 2), 'week_proj': round(wk, 2), 'ytd': ytd, 'games': g, 'last': round(pts[-1], 2) if pts else None, 'status': st or '', 'ros_ppg_prior': round(prior_ppg, 2)}
    add_vor(out, players)
    return out
REPLACEMENT_RANK = {'QB': 12, 'RB': 30, 'WR': 32, 'TE': 12, 'K': 10, 'DEF': 10}

def add_vor(proj: dict, players: Mapping[str, Mapping]) -> None:
    for key, out_key in (('value', 'vor'), ('ytd', 'ytd_vor')):
        by_pos: dict[str, list[float]] = {}
        for pid, p in players.items():
            by_pos.setdefault(p['pos'], []).append(proj[pid][key])
        repl = {}
        for pos, vals in by_pos.items():
            vals.sort(reverse=True)
            n = REPLACEMENT_RANK.get(pos, 12)
            repl[pos] = vals[min(n, len(vals) - 1)] if vals else 0.0
        for pid, p in players.items():
            proj[pid][out_key] = round(proj[pid][key] - repl[p['pos']], 2)
