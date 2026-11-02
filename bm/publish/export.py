from __future__ import annotations
import json
import shutil
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
import yaml
from bm.agent.notes import note_text
from bm.engine.scoring import standings
from bm.league.season import LEAGUES, League, iso, now_utc
from bm.log import ChainLog
ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'export' / 'data'
EMBARGO_H = 0

def _w(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1, default=str))
    tmp.replace(path)

def _notes(text, reasoning=None, est=None) -> list:
    out = [{'source': 'scratchpad', 'text': text or '', 'publishable': True, **({'est_value': est} if isinstance(est, dict) else {})}]
    for r in reasoning or []:
        out.append({'source': r.get('source', 'reasoning'), 'text': r.get('text', '') if r.get('publishable') else '', 'publishable': bool(r.get('publishable'))})
    return out
_VIEWS: dict = {}
_LEAGUES: dict[str, League] = {}
_EVENTS: dict[str, list[dict]] = {}
_STATES: dict[str, dict] = {}

def _clear_snapshots() -> None:
    _LEAGUES.clear()
    _EVENTS.clear()
    _STATES.clear()

def _league(lid: str) -> League:
    if lid not in _LEAGUES:
        _LEAGUES[lid] = League(lid)
    return _LEAGUES[lid]

def _events(lg: League) -> list[dict]:
    lid = getattr(lg, 'id', None)
    if lid is None:
        return lg.events()
    if lid not in _EVENTS:
        _EVENTS[lid] = lg.events()
    return _EVENTS[lid]

def _state(lg: League) -> dict:
    lid = getattr(lg, 'id', None)
    if lid is None:
        return lg.state()
    if lid not in _STATES:
        _STATES[lid] = lg.fold_events(_events(lg))
    return _STATES[lid]

def _agent_week(cfg: dict, lines: list[str]) -> int:
    week = int(cfg.get('start_week') or 0)
    for ln in lines:
        if '"week_advance"' in ln:
            ev = json.loads(ln)
            if ev.get('type') == 'week_advance':
                week = max(week, int((ev.get('payload') or {}).get('week') or 0))
    return week

def _view(lg, week, now):
    rh = getattr(getattr(lg, 'rules', None), 'rules_hash', None)
    if rh is None:
        return lg.view(week, now)
    key = (rh, int(week), now)
    if key not in _VIEWS:
        _VIEWS[key] = lg.view(week, now)
    return _VIEWS[key]

def export() -> Path:
    _VIEWS.clear()
    _clear_snapshots()
    try:
        return _export()
    finally:
        _VIEWS.clear()
        _clear_snapshots()

def _export() -> Path:
    from bm.cli import league_ids
    ents = {e['id']: e for e in yaml.safe_load((ROOT / 'config' / 'entrants.yaml').read_text())}
    now = now_utc()
    leagues, details, trades, decisions, feed, log_heads = ([], [], [], [], [], [])
    players_used: dict[str, dict] = {}
    entrant_ids = set()
    msg_count = 0
    provisional_scores = []
    validity_inputs: dict[str, tuple] = {}
    ledger: list[dict] = []
    from bm.publish.own_ledger import own_ledger
    kinds = {k: 'baseline' if v.get('kind') == 'baseline' else 'llm' for k, v in ents.items()}
    stage = OUT.with_name('data.next')
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    for lid in league_ids():
        if not (LEAGUES / lid / 'league.json').exists():
            continue
        _cfg = json.loads((LEAGUES / lid / 'league.json').read_text())
        if _cfg.get('mode') == 'agent':
            _log = LEAGUES / lid / 'log.jsonl'
            _lines = _log.read_text().splitlines() if _log.exists() else []
            drafted = any((json.loads(ln).get('type') == 'draft_complete' for ln in _lines if '"draft_complete"' in ln))
            seats = _cfg.get('seats') or []
            for sd in seats:
                entrant_ids.add(sd['entrant_id'])
            a_teams = [{'id': sd['team_id'], 'entrant_id': sd['entrant_id'], 'name': sd['team_id']} for sd in seats]
            a_week = _agent_week(_cfg, _lines)
            leagues.append({'id': lid, 'sport': 'nfl', 'mode': 'agent', 'division': _cfg.get('division', 'main'), 'label': _cfg.get('label', lid), 'week': a_week, 'state_week': a_week, 'status': 'ended' if _cfg.get('status') == 'ended' else 'active', 'draft_complete': drafted, 'detail': True})
            details.append({'id': lid, 'sport': 'nfl', 'mode': 'agent', 'label': _cfg.get('label', lid), 'week': a_week, 'teams': a_teams})
            continue
        lg = _league(lid)
        evs = _events(lg)
        st = _state(lg)
        names = lg.names()
        ok, n, head = lg.log.verify()
        log_heads.append({'league_id': lid, 'sha256': head, 'records': n, 'at': iso(now)})
        last_week = int(lg.cfg.get('end_week') or lg.rules.last_week)
        display_week = min(st['week'], last_week)
        league_status = 'archived' if lg.cfg.get('status') == 'archived' else 'ended' if lg.cfg.get('status') == 'ended' or st['week'] > last_week else 'active'
        view = _view(lg, display_week, now)
        for t in st['teams'].values():
            for p in t['roster']:
                players_used[p] = view
        teams = []
        for tid, s in lg.seats.items():
            ctl = lg.entrant(tid, st['week'])
            op = _operator(lg, tid, st['week'])
            entrant_ids.update({s['entrant_id'], ctl})
            tm = st['teams'][tid]
            inv = {p: sl for sl, p in tm['lineup'].items() if p}
            teams.append({'id': tid, 'entrant_id': ctl, 'name': names[tid], **({'clone_of': s['clone_of']} if s.get('clone_of') else {}), **({'seat_entrant_id': s['entrant_id'], 'controllers': _controllers(lg, tid)} if ctl != s['entrant_id'] else {}), **({'seat_operator': op} if op else {}), 'variant': s.get('variant', 'standard'), 'operator': s.get('operator', 'entrant'), 'faab_remaining': tm['faab'], 'roster': [{'slot': inv.get(p, 'BN'), 'player_id': p, 'status': 'active' if p in inv else 'bench'} for p in tm['roster']]})
        finals = lg.swiss_view(st, now)
        if finals is None:
            stand = standings(list(st['teams']), st['scores'], lg._tie_seed(display_week))
            scores = st['scores']
        else:
            stand = [{**r, 'rank_history': []} for r in finals['standings']]
            scores = finals['weekly_scores']
        weeks = sorted(scores, key=int)
        partial = None
        if lg.cfg.get('status') == 'private-live-season' and str(display_week) not in scores:
            from bm.score.provisional import league_partial
            partial = league_partial(lg, display_week, now)
            provisional_scores.append(partial)
        txns = []
        decs = {e['decision_id']: e for e in evs if e['type'] == 'decision'}

        def _pick_src(e):
            if e.get('source') == 'queue' and (decs.get(e.get('decision_id')) or {}).get('forced_default'):
                return 'default'
            return e.get('source')
        for e in evs:
            if e['type'] == 'draft_pick':
                txns.append({'id': f'{lid}-d{e['pick_no']}', 'at': e['ts'], 'week': lg.cfg['start_week'], 'kind': 'draft', 'team_ids': [e['team']], 'summary': f'Pick {e['pick_no']}: {view.world.players.get(e['player'], {}).get('name', e['player'])} ({_pick_src(e)})', **({'decision_id': e['decision_id']} if e.get('decision_id') else {})})
            elif e['type'] == 'waiver_award':
                txns.append({'id': f'{lid}-wa{e['seq']}', 'at': e['ts'], 'week': e.get('week', st['week']), 'kind': 'waiver', 'team_ids': [e['team']], 'summary': f'Claimed {view.world.players.get(e['add'], {}).get('name', e['add'])} for ${e['amount']}' + (f', dropped {view.world.players.get(e['drop'], {}).get('name', e['drop'])}' if e.get('drop') else '')})
            elif e['type'] == 'trade_msg' and e['action'] == 'ACCEPT':
                txns.append({'id': f'{lid}-tr{e['seq']}', 'at': e['ts'], 'week': int(e['window'].split('-')[0][1:]), 'kind': 'trade', 'team_ids': [e['from_team'], e['to_team']], 'summary': 'Trade accepted', 'trade_id': e['thread_id']})
        details.append({'id': lid, 'sport': 'nfl', 'division': lg.cfg['division'], 'disclosure': lg.cfg['disclosure'], 'label': lg.cfg['label'], 'block': lg.cfg['block'], 'week': display_week, 'state_week': st['week'], 'status': league_status, 'teams': teams, 'standings': stand, 'weekly_points': [{'team_id': t, 'points': [scores[w].get(t, 0) for w in weeks]} for t in st['teams']], **({'provisional_scoreboard': partial} if partial is not None else {}), **({'finals': {'standings_method': 'swiss', **{k: finals[k] for k in ('rounds', 'scored_rounds', 'complete', 'final', 'champion', 'score_statuses', 'tie_policy')}}} if finals is not None else {}), 'transactions': txns[-300:]})
        threads: dict[str, list] = {}
        for e in evs:
            if e['type'] == 'trade_msg':
                threads.setdefault(e['thread_id'], []).append(e)
        final = st['threads']
        for th_id, ms in threads.items():
            th = final.get(th_id, {})
            msgs = []
            for i, m in enumerate(ms):
                for side in ('give', 'get'):
                    for player_id in (m.get('offer') or {}).get(side, []):
                        players_used.setdefault(player_id, view)
                msgs.append({'seq': i + 1, 'at': m['ts'], 'from_team': m['from_team'], 'to_team': m['to_team'], 'action': m['action'], **({'offer': m['offer']} if m.get('offer') else {}), 'text': m.get('text', ''), 'private': _notes(note_text(m), None, m.get('est_value'))})
                msg_count += 1
            status = {'accepted': 'accepted', 'rejected': 'rejected'}.get(th.get('status'), 'expired')
            wk = int(ms[0]['window'].split('-')[0][1:])
            closed = ms[-1]['ts']
            lo = th.get('latest_offer') or {}
            tr = {'id': th_id, 'league_id': lid, 'sport': 'nfl', 'week': wk, 'window': int(ms[0]['window'].split('-t')[1]), 'opened_at': ms[0]['ts'], 'closed_at': closed, 'embargo_until': iso(datetime.fromisoformat(closed) + timedelta(hours=EMBARGO_H)), 'parties': [th.get('a', ms[0]['from_team']), th.get('b', ms[0]['to_team'])], 'status': status, 'verdict': 'pending' if status == 'accepted' else 'no-deal', 'title': f'{names[ms[0]['from_team']]} → {names[ms[0]['to_team']]}', 'dek': (ms[0].get('text') or '')[:160], 'messages': msgs, **({'final_offer': {**{k: lo[k] for k in ('give', 'get')}, 'from_team': lo['from']}} if lo else {}), 'tells': [], 'highlight_tags': []}
            trades.append(tr)
            for m, mm in zip(ms, msgs):
                kind = {'PROPOSE': 'offer', 'COUNTER': 'counter', 'ACCEPT': 'accepted', 'REJECT': 'rejected'}.get(m['action'], 'offer')
                feed.append({'id': f'{th_id}#{mm['seq']}', 'at': m['ts'], 'league_id': lid, 'kind': kind, 'channel': 'private', 'team_id': m['from_team'], 'entrant_id': lg.entrant(m['from_team'], _wk(m.get('window'))), 'text': m.get('text', '')[:500], 'thought': note_text(m)[:500], 'ref': {'trade_id': th_id, 'message_seq': mm['seq']}})
        from bm.ops.served_model import misserved_ids
        misserved = misserved_ids(evs)
        validity_inputs[lid] = (evs, lg.cfg, len(decisions))
        ledger.extend(own_ledger(lg, evs, kinds))
        for e in evs:
            if e['type'] == 'decision':
                decisions.append({'id': e['decision_id'], 'league_id': lid, 'team_id': e['team'], 'entrant_id': e['entrant'], 'week': e['week'], **({'unattributed': 'served by another model'} if e['decision_id'] in misserved else {}), 'at': e['ts'], 'kind': e['kind'], 'question': e['question'], 'unit': e['unit'] if e['unit'] in ('ROS pts', 'week pts', 'FAAB $') else 'ROS pts', 'options': [{'id': str(o.get('id', i)), 'label': str(o.get('label', ''))[:200], 'est_value': _num(o.get('est_value')), **({'note': str(o['note'])[:300]} if o.get('note') else {})} for i, o in enumerate(e.get('options') or []) if isinstance(o, dict)], 'chosen': str(e.get('chosen') or ''), 'rationale_private': _notes(note_text(e), e.get('provider_reasoning'), e.get('est_value')), **({'statement_public': e['statement_public']} if e.get('statement_public') else {}), **({'forecast': e['forecast']} if e.get('forecast') else {}), **({'forced_default': e['forced_default']} if e.get('forced_default') else {}), 'research': [{'tool': r['tool'] if r['tool'] in ('stats_query', 'news_search', 'fetch', 'injury_report') else 'stats_query', 'query': f'{r['tool']} {r['query']}', 'result_summary': r['result_summary'], 'tokens': r['tokens']} for r in e.get('research') or []], 'budget': {'calls_used': (e.get('usage') or {}).get('calls', 0), 'calls_cap': 4, 'tokens_used': (e.get('usage') or {}).get('input', 0) + (e.get('usage') or {}).get('output', 0), 'tokens_cap': 60000}, 'highlight_tags': []})
                feed.append({'id': f'fd-{e['decision_id']}', 'at': e['ts'], 'league_id': lid, 'kind': 'lineup' if e['kind'] == 'start_sit' else 'decision', 'channel': 'engine', 'team_id': e['team'], 'entrant_id': e['entrant'], 'text': f'{names[e['team']]}: {e['question']}' + (' (forced default)' if e.get('forced_default') else ''), 'thought': note_text(e)[:400], 'ref': {'decision_id': e['decision_id']}})
            elif e['type'] == 'chatter':
                feed.append({'id': f'ch-{lid}-{e['seq']}', 'at': e['ts'], 'league_id': lid, 'kind': 'chatter', 'channel': 'public', 'team_id': e['team'], 'entrant_id': lg.entrant(e['team'], _wk(e.get('window'))), 'text': e['text'], 'thought': note_text(e)[:500]})
            elif e['type'] == 'waiver_award':
                feed.append({'id': f'wv-{lid}-{e['seq']}', 'at': e['ts'], 'league_id': lid, 'kind': 'waiver', 'channel': 'engine', 'team_id': e['team'], 'entrant_id': lg.entrant(e['team'], e.get('week')), 'text': f'{names[e['team']]} claimed {view.world.players.get(e['add'], {}).get('name', e['add'])} for ${e['amount']}'})
        leader = stand[0]['team_id'] if stand else teams[0]['id']
        leagues.append({'id': lid, 'sport': 'nfl', 'division': lg.cfg['division'], 'disclosure': lg.cfg['disclosure'], 'variants': ['standard'], 'label': lg.cfg['label'], 'block': lg.cfg['block'], 'week': display_week, 'state_week': st['week'], 'status': league_status, 'leader_team': leader, 'leader_entrant': lg.entrant(leader), 'draft_complete': any((e['type'] == 'draft_complete' for e in evs)), 'trades': sum((1 for t in trades if t['league_id'] == lid and t['status'] == 'accepted')), 'detail': True})
        if partial is not None:
            teams_so_far = partial['teams']
            order = partial['display_order']
            known_leader = next((tid for tid in order if teams_so_far[tid]['known_points'] is not None and teams_so_far[tid]['covered_starters'] > 0), None)
            leagues[-1]['provisional_scoreboard'] = {'week': display_week, 'asof': partial['asof'], 'status': partial['status'], 'known_points_leader_team': known_leader, 'known_points_leader_entrant': lg.entrant(known_leader) if known_leader else None, 'known_points': teams_so_far[known_leader]['known_points'] if known_leader else None, 'covered_starters': teams_so_far[known_leader]['covered_starters'] if known_leader else None, 'pending_starters': teams_so_far[known_leader]['pending_starters'] if known_leader else None, 'missing_starters': teams_so_far[known_leader]['missing_starters'] if known_leader else None}
    _attach_validity(decisions, validity_inputs)
    players = []
    for pid, v in players_used.items():
        p = v.world.players.get(pid, {})
        pr = v.proj.get(pid, {})
        st_ = pr.get('status') or ''
        players.append({'id': pid, 'name': p.get('name', pid), 'sport': 'nfl', 'team': p.get('team', ''), 'pos': [p.get('pos', '')], 'fpg': round(pr['ytd'] / pr['games'], 2) if pr.get('games') else 0.0, 'status': 'out' if st_ in ('Out', 'Doubtful') else 'ir' if st_ == 'IR' else 'day-to-day' if st_ == 'Questionable' else 'healthy'})
    entrants = []
    from bm.agent.onboard import load as load_identity
    for eid in sorted(entrant_ids):
        e = ents[eid]
        ident = load_identity(eid) or {}
        logo = None
        if ident.get('status') == 'ok' and ident.get('logo'):
            for k in ('svg', 'png_64', 'png_256'):
                src = ROOT / ident['logo'][k]
                dst = stage / 'logos' / f'{eid}{(src.suffix if k == 'svg' else '-' + k.split('_')[1] + '.png')}'
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
            logo = {'svg': f'logos/{eid}.svg', 'png_64': f'logos/{eid}-64.png', 'png_256': f'logos/{eid}-256.png', 'sha256': ident['logo']['sha256'], 'author': 'model'}
        entrants.append({'id': eid, 'name': e['name'], 'short': e['short'], 'kind': 'baseline' if e.get('kind') == 'baseline' else 'llm', 'maker': e['maker'], 'surface': e['surface'], 'model_id': e.get('model', 'rule-based'), 'color_slot': e.get('color_slot'), 'team_name': ident.get('team_name'), 'tagline': ident.get('tagline'), 'logo': logo, 'blurb': '', 'style': '', 'tendencies': [], 'signature_quotes': []})
    lb, history = _leaderboard(ents, now)
    rivalries = _rivalries(now)
    season_stats = _season_stats(ents)
    feed.sort(key=lambda f: f['at'], reverse=True)
    trades_idx = [{k: v for k, v in t.items() if k != 'messages'} for t in trades]
    meta = {'schema_version': '1.1', 'generated_at': iso(now), 'preview': True, 'preview_note': 'Private NFL Exhibition prototype: an engine shakedown, not a ranking. Not public.', 'season': {'id': 'nfl-exhibition-2026-proto', 'label': 'NFL Exhibition (private prototype)', 'status': 'pilot', 'week': max((l['week'] for l in leagues), default=0), 'weeks_total': 14, 'draft_day': 'Mid-season draft as of Tue Sep 29, 2026'}, 'counts': {'leagues': len(leagues), 'entrants': len(entrants), 'trades_negotiated': len(trades), 'trades_accepted': sum((1 for t in trades if t['status'] == 'accepted')), 'messages': msg_count, 'decisions_logged': len(decisions), 'tells_flagged': 0, 'wakeups': 0, 'news_items': 0, 'tips': 0, 'probes': 0}, 'log_heads': log_heads, 'embargo': {'policy': 'Private prototype: no publication. Public seasons publish private notes after each trade window.', 'lag_hours': EMBARGO_H}, 'links': {'x': {'handle': 'benchedmarkai', 'live': False}, 'github': None, 'dataset': None}}
    _w(stage / 'meta.json', meta)
    _w(stage / 'entrants.json', entrants)
    _w(stage / 'leaderboard.json', lb)
    for wk, snap in history.items():
        _w(stage / 'leaderboard_history' / 'nfl-exhibition-2026' / f'{wk}.json', snap)
    for eid, ss in season_stats.items():
        _w(stage / 'season_stats' / f'{eid}.json', ss)
    _w(stage / 'players.json', players)
    _w(stage / 'leagues.json', leagues)
    _w(stage / 'provisional_scores.json', {'generated_at': iso(now), 'status': 'partial_unsettled', 'leagues': provisional_scores})
    for d in details:
        _w(stage / 'leagues' / f'{d['id']}.json', d)
    _w(stage / 'trades.json', trades_idx)
    for t in trades:
        _w(stage / 'trades' / f'{t['id']}.json', t)
    _w(stage / 'decisions.json', decisions)
    _w(stage / 'own_ledger.json', ledger)
    _w(stage / 'feed.json', feed[:2000])
    _w(stage / 'rivalries.json', rivalries)
    for name in ('highlights', 'recaps', 'agents', 'news', 'tips'):
        _w(stage / f'{name}.json', [])
    _w(stage / 'methodology.json', {'power_sim': {'source': 'n/a (NFL exhibition prototype)', 'curves': []}, 'budgets': []})
    boards = OUT.parent / 'boards.json'
    if boards.exists():
        shutil.copy2(boards, stage / 'boards.json')
    shutil.rmtree(OUT, ignore_errors=True)
    stage.rename(OUT)
    return OUT

def _attach_validity(decisions: list[dict], inputs: dict[str, tuple]) -> None:
    from bm.score import validity
    try:
        ids = {rid for evs, _, _ in inputs.values() for e in evs if e['type'] == 'decision' for rid in e.get('model_call_ids') or []}
        calls = validity.gateway_calls(ids)
        by_id = {}
        for lid, (evs, cfg, _) in inputs.items():
            by_id.update({(lid, k): v for k, v in validity.for_events(evs, cfg, calls).items()})
        for d in decisions:
            d['validity'] = by_id.get((d['league_id'], d['id']), {'valid': None, 'reasons': ['unmeasured']})
    except Exception as exc:
        for d in decisions:
            d['validity'] = {'valid': None, 'reasons': [f'unmeasured: {type(exc).__name__}']}

def _num(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0

def _league_objs():
    from bm.cli import league_ids
    for lid in league_ids():
        if (LEAGUES / lid / 'league.json').exists():
            yield _league(lid)

def _superseded(evs) -> set[int]:
    last = {}
    for i, e in enumerate(evs):
        if e['type'] == 'week_scored':
            last[e['week']] = i
    keep = set(last.values())
    return {i for i, e in enumerate(evs) if e['type'] == 'week_scored' and i not in keep}

def _wk(window) -> int | None:
    import re
    m = re.match('w(\\d+)', str(window or ''))
    return int(m.group(1)) if m else None

def _operator(lg, tid: str, week) -> dict | None:
    from bm import succession
    s = succession.operated(lg.seat_entrant(tid), league=lg.id, week=week)
    return {'entrant_id': s['successor'], 'from_week': int(s['from_week']), 'reason': s.get('reason'), 'label': succession.operator_label(s)} if s else None

def _controllers(lg, tid: str) -> list[dict]:
    out = []
    for w in range(lg.cfg['start_week'], lg.cfg.get('end_week', lg.cfg['start_week']) + 1):
        e = lg.entrant(tid, w)
        op = _operator(lg, tid, w)
        if out and out[-1]['entrant_id'] == e:
            out[-1]['to_week'] = w
        else:
            out.append({'entrant_id': e, 'from_week': w, 'to_week': w, **({'operator': op['label']} if op else {})})
    return out

def _weekly_rows():
    import statistics
    from bm.ops.served_model import misserved_team_weeks
    rows = []
    for lg in _league_objs():
        evs = _events(lg)
        old = _superseded(evs)
        nobody = misserved_team_weeks(evs)
        for i, e in enumerate(evs):
            if e['type'] != 'week_scored' or i in old:
                continue
            pts = e['points']
            bots = [v for t, v in pts.items() if lg.seat_entrant(t) == 'projection-bot']
            pb = statistics.mean(bots) if bots else None
            for t, v in pts.items():
                w = sum((1 for o, ov in pts.items() if o != t and v > ov))
                l = sum((1 for o, ov in pts.items() if o != t and v < ov))
                op = _operator(lg, t, e['week'])
                rows.append({'league': lg.id, 'week': e['week'], 'team': t, 'entrant': lg.entrant(t, e['week']), 'inherited': lg.entrant(t, e['week']) != lg.seat_entrant(t), 'unattributed': (t, e['week']) in nobody or bool(op), **({'operator': op['label']} if op else {}), 'points': v, 'proj_bot': pb, 'w': w, 'l': l})
    return rows

def _estimate(xs):
    import statistics
    return {'value': round(statistics.mean(xs), 2) if xs else None, 'ci': [None, None], 'n': len(xs), 'ci_status': 'unavailable: independent support not established'}
RANK_MIN_OWN = 0.98

def default_side(dec: dict) -> str | None:
    from bm import records
    fail = records.decision_failure(dec, [])
    if not fail:
        return None
    return 'refused' if fail['code'] == 'REFUSED' else fail['layer']

def _fd_tally() -> dict:
    from bm.ops.exclusions import excluded_ids
    out = {}
    for lg in _league_objs():
        evs = _events(lg)
        gone = excluded_ids(evs)
        for e in evs:
            if e['type'] == 'decision' and e['decision_id'] not in gone:
                t = out.setdefault((lg.id, e['entrant']), {'decisions': 0, 'forced': 0, 'forced_model': 0, 'forced_infra': 0, 'forced_refused': 0, 'infra_team_weeks': {}, 'infra_draft_picks': 0})
                t['decisions'] += 1
                side = default_side(e) if e.get('forced_default') else None
                if not side:
                    continue
                t['forced'] += 1
                t[{'model': 'forced_model', 'infra': 'forced_infra', 'refused': 'forced_refused'}[side]] += 1
                if side == 'infra':
                    if e.get('kind') == 'draft_pick':
                        t['infra_draft_picks'] += 1
                    else:
                        t['infra_team_weeks'].setdefault((e.get('team'), e.get('week')), []).append(e.get('kind'))
    return out

def _rows_for(sel, ents, fd=None):
    by = {}
    for r in sel:
        by.setdefault(r['entrant'], []).append(r)
    out = []
    for eid, rs in by.items():
        fds = {l: fd[l, eid] for l in {r['league'] for r in rs} if fd and (l, eid) in fd}
        n_dec = sum((t['decisions'] for t in fds.values()))
        n_model = sum((t.get('forced_model', t['forced']) + t.get('forced_refused', 0) for t in fds.values()))
        n_infra = sum((t.get('forced_infra', 0) for t in fds.values()))
        fd_rate = {'forced_default_rate': round(n_model / n_dec, 3), 'model_forced_defaults': n_model, 'refused_defaults': sum((t.get('forced_refused', 0) for t in fds.values())), 'infra_default_rate': round(n_infra / n_dec, 3), 'infra_forced_defaults': n_infra, 'all_default_rate': round((n_model + n_infra) / n_dec, 3), 'decisions': n_dec} if n_dec else {}
        hit = [{'league': r['league'], 'team': r['team'], 'week': r['week'], 'kinds': sorted(set(kinds)), 'points': r['points']} for r in rs if r.get('team') is not None for kinds in [(fds.get(r['league']) or {}).get('infra_team_weeks', {}).get((r['team'], r['week']))] if kinds]
        infra_tw = {'infra_default_team_weeks': len(hit), 'infra_default_lineup_team_weeks': sum(('start_sit' in h['kinds'] for h in hit)), 'infra_default_draft_picks': sum((t.get('infra_draft_picks', 0) for t in fds.values())), 'infra_affected_team_weeks': sorted(hit, key=lambda h: (h['league'], h['week'], h['team']))} if fds else {}
        diffs = [r['points'] - r['proj_bot'] for r in rs if r['proj_bot'] is not None]
        est = _estimate(diffs)
        w = sum((r['w'] for r in rs))
        l = sum((r['l'] for r in rs))
        own = 1 - fd_rate['forced_default_rate'] if n_dec else None
        reliability_eligible = own is not None and own >= RANK_MIN_OWN or eid in {'projection-bot', 'adp-bot', 'random-bot'}
        ranked = False
        out.append({'entrant_id': eid, 'rating': {'value': round(1500 + 10 * est['value'], 1) if est['value'] is not None else None, 'ci': [None, None]}, 'band': None, 'pts_over_projection': est, 'all_play_win_pct': round(w / (w + l), 3) if w + l else None, 'trade_surplus': None, 'tell_rate': None, 'leagues': len({r['league'] for r in rs}), **fd_rate, **infra_tw, 'ranked': ranked, 'reliability_eligible': reliability_eligible, 'measurement_status': 'descriptive deployment outcome; defaults included', 'points_basis': "defaults included: points count every team-week as played, including lineups the engine defaulted because our infrastructure failed (infra_default_team_weeks) or the model's answer was unusable", 'unranked_reason': 'prospective capability inference not validated' if reliability_eligible else f'{1 - own:.1%} of its decisions were forced defaults caused by its own unusable or refused answers (over the {1 - RANK_MIN_OWN:.0%} limit); infrastructure defaults ({fd_rate['infra_default_rate']:.1%}, our failures) are not counted against it' if own is not None else 'no decision provenance'})
    out.sort(key=lambda r: (not r['ranked'], -r['pts_over_projection']['value'] if r['pts_over_projection']['value'] is not None else float('inf')))
    n = 0
    for r in out:
        if r['ranked']:
            n += 1
            r['rank'] = n
    return out

def _leaderboard(ents, now):
    rows = [r for r in _weekly_rows() if not r.get('inherited') and (not r.get('unattributed'))]
    fd = _fd_tally()
    weeks = sorted({r['week'] for r in rows})

    def scopes(sel):
        sc = [{'id': 'sport:nfl', 'label': 'NFL Exhibition (unranked engine shakedown)', 'exhibition': True, 'leagues': len({r['league'] for r in sel}), 'weeks': len({r['week'] for r in sel}), 'rows': _rows_for(sel, ents, fd)}]
        makers = sorted({ents[r['entrant']]['maker'] for r in sel})
        for m in makers:
            ms = [r for r in sel if ents[r['entrant']]['maker'] == m]
            sc.append({'id': f'lab:{m}', 'label': f'{m} (NFL Exhibition)', 'exhibition': True, 'leagues': len({r['league'] for r in ms}), 'weeks': len({r['week'] for r in ms}), 'rows': _rows_for(ms, ents, fd)})
        return sc
    cur = scopes(rows)
    history = {}
    prev = None
    for wk in weeks:
        snap = scopes([r for r in rows if r['week'] <= wk])
        history[wk] = {'as_of_week': wk, 'scopes': snap}
        prev = snap
    if len(weeks) >= 2:
        before = {s['id']: {r['entrant_id']: r['rank'] for r in s['rows'] if r.get('rank')} for s in history[weeks[-2]]['scopes']}
        for s in cur:
            for r in s['rows']:
                if r['entrant_id'] in before.get(s['id'], {}):
                    r['prev_rank'] = before[s['id']][r['entrant_id']]
    return ({'as_of': iso(now), 'scopes': cur}, history)

def _rivalries(now):
    out = {}
    for lg in _league_objs():
        evs = _events(lg)
        view = None
        threads = {}
        for e in evs:
            if e['type'] == 'trade_msg':
                threads.setdefault(e['thread_id'], []).append(e)
        if not threads:
            continue
        st = _state(lg)
        for th_id, ms in threads.items():
            a_t, b_t = (ms[0]['from_team'], ms[0]['to_team'])
            wk0 = _wk(ms[0].get('window'))
            a, b = sorted([lg.entrant(a_t, wk0), lg.entrant(b_t, wk0)])
            if a == b:
                continue
            k = (a, b)
            r = out.setdefault(k, {'a': a, 'b': b, 'trades': 0, 'net_to_a': 0.0, 'tells': 0, 'threads_by_week': {}, 'notes_mentions': 0, 'last_accepted_week': None})
            wk = int(ms[0]['window'].split('-')[0][1:])
            r['threads_by_week'][wk] = r['threads_by_week'].get(wk, 0) + 1
            th = st['threads'].get(th_id, {})
            if th.get('status') == 'accepted':
                r['trades'] += 1
                r['last_accepted_week'] = max(r['last_accepted_week'] or 0, wk)
                lo = th['latest_offer']
                if view is None:
                    view = _view(lg, lg.cfg['start_week'], now)
                val = lambda ps: sum((view.proj.get(p, {}).get('value', 0) for p in ps))
                to_author = val(lo['get']) - val(lo['give'])
                author_ent = lg.entrant(lo['from'], wk)
                r['net_to_a'] += round(to_author if author_ent == a else -to_author, 2)
            for m in ms:
                other = m['to_team']
                if note_text(m) and other in note_text(m):
                    r['notes_mentions'] += 1
    res = []
    for r in out.values():
        r['threads_by_week'] = [{'week': w, 'threads': n} for w, n in sorted(r['threads_by_week'].items())]
        res.append(r)
    return res

def _season_stats(ents):
    stats = {}
    for lg in _league_objs():
        evs = _events(lg)
        decs = {e['decision_id']: e for e in evs if e['type'] == 'decision'}
        old = _superseded(evs)
        from bm.ops.exclusions import excluded_ids
        from bm.ops.served_model import misserved_team_weeks
        gone = excluded_ids(evs)
        nobody = misserved_team_weeks(evs)
        for i, e in enumerate(evs):
            if i in old:
                continue
            if e['type'] == 'decision' and e['decision_id'] not in gone:
                s = stats.setdefault(e['entrant'], _blank(e['entrant']))
                s['decisions'] += 1
                s['forced_defaults'] += 1 if e.get('forced_default') else 0
                if e.get('forced_default'):
                    c = e['forced_default'].get('cause') or 'UNKNOWN'
                    s['forced_default_causes'][c] = s['forced_default_causes'].get(c, 0) + 1
                    side = default_side(e) or 'infra'
                    s['forced_defaults_' + side] += 1
                else:
                    s['model_decisions'] += 1
                s['calls'] += (e.get('usage') or {}).get('calls', 0)
                s['input_tokens'] += (e.get('usage') or {}).get('input', 0)
                s['output_tokens'] += (e.get('usage') or {}).get('output', 0)
                s['usd_eq'] = round(s['usd_eq'] + (e['usage'].get('usd_eq') or 0), 4)
                s['research_calls'] += len(e.get('research') or [])
            elif e['type'] == 'draft_pick' and (not lg.is_bot(e['team'], lg.cfg.get('start_week'))):
                s = stats.setdefault(lg.entrant(e['team'], lg.cfg.get('start_week')), _blank(lg.entrant(e['team'], lg.cfg.get('start_week'))))
                dflt = e.get('source') == 'default' or (e.get('source') == 'queue' and e.get('decision_id') and (decs.get(e['decision_id']) or {}).get('forced_default'))
                k = 'draft_picks_default' if dflt else 'draft_picks_model'
                s[k] += 1
            elif e['type'] == 'week_scored':
                for t, d in e['detail'].items():
                    if (t, e['week']) in nobody or _operator(lg, t, e['week']):
                        continue
                    s = stats.setdefault(lg.entrant(t, e['week']), _blank(lg.entrant(t, e['week'])))
                    s['team_weeks'] += 1
                    s['lineup_errors'] += sum((1 for x in d['starters'] if x['player'] is None or x['points'] == 0.0))
                    p = d['points']
                    if s['best_week'] is None or p > s['best_week']['points']:
                        s['best_week'] = {'league': lg.id, 'team': t, 'week': e['week'], 'points': p}
                    if s['worst_week'] is None or p < s['worst_week']['points']:
                        s['worst_week'] = {'league': lg.id, 'team': t, 'week': e['week'], 'points': p}
    for s in stats.values():
        n = s['decisions']
        s['forced_default_rate'] = round((s['forced_defaults_model'] + s['forced_defaults_refused']) / n, 3) if n else 0.0
        s['infra_default_rate'] = round(s['forced_defaults_infra'] / n, 3) if n else 0.0
    return stats

def _blank(eid):
    return {'entrant_id': eid, 'decisions': 0, 'forced_defaults': 0, 'model_decisions': 0, 'forced_default_causes': {}, 'forced_defaults_model': 0, 'forced_defaults_infra': 0, 'forced_defaults_refused': 0, 'draft_picks_model': 0, 'draft_picks_default': 0, 'calls': 0, 'input_tokens': 0, 'output_tokens': 0, 'usd_eq': 0.0, 'research_calls': 0, 'team_weeks': 0, 'lineup_errors': 0, 'best_week': None, 'worst_week': None, 'note': 'lineup_errors = starting slots that were empty or scored exactly 0 (bye, inactive, or a true zero)'}
