from __future__ import annotations
import hashlib
import json
import random
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from bm import priority, records
from bm.agent import book as bookmod
from bm.agent import budget as bud
from bm.agent import notebook as nbk
from bm.agent import prompts_v2
from bm.agent.harness import extract_json, json_problem
from bm.agent.tools import ToolServer, catalogue_text
from bm.gateway.core import ModelRequest
ET = ZoneInfo('America/New_York')
WAKES_PER_DAY = 8
CALLS_PER_WAKE = 25
CALLS_PER_TURN = 6
MAX_TURNS = 12
RUNAWAY = timedelta(minutes=60)
COALESCE = timedelta(minutes=10)
HOLD_MAX = timedelta(hours=6)
BACKOFF0, BACKOFF_CAP = (30.0, 1800.0)
ALERT_AFTER = timedelta(hours=1)
HARNESS_VERSION = 'agent-1'
INFRA = set(records.INFRA)
PAUSE_POLL_S = 60.0
ETA_RECENT = timedelta(hours=3)
ETA_ALERT_EVERY_S = 2 * 3600

def _now() -> datetime:
    from bm.league.season import now_utc
    return now_utc()

def seat_lock(lg, tid: str):
    import fcntl
    f = open(lg.dir / f'seat-{tid}.lock', 'w')
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except BlockingIOError:
        f.close()
        return None

def seat_busy(lg, tid: str) -> bool:
    f = seat_lock(lg, tid)
    if f is None:
        return True
    f.close()
    return False

def schedule_of(events: list[dict], team: str) -> dict:
    wakes, subs, used = ({}, {}, set())
    for e in events:
        if e.get('team') != team:
            continue
        if e['type'] == 'wake_schedule':
            if e['op'] == 'schedule':
                wakes[e['id']] = {'id': e['id'], 'at': e['at'], 'reason': e['reason'], 'retry': bool(e.get('retry'))}
            elif e['op'] == 'cancel':
                wakes.pop(e['id'], None)
            elif e['op'] == 'subscribe':
                subs[e['filter']] = e['on']
        elif e['type'] == 'wake_open' and e.get('schedule_id'):
            used.add(e['schedule_id'])
    return {'wakes': sorted((w for k, w in wakes.items() if k not in used), key=lambda w: w['at']), 'subscriptions': sorted((f for f, on in subs.items() if on))}

def _my_players(state: dict, team: str) -> set[str]:
    return set(state['teams'][team]['roster'])

def _matches(e: dict, f: str, team: str, mine: set[str]) -> bool:
    if f == 'offers':
        return e['type'] == 'trade_msg' and e.get('to_team') == team
    if f == 'waiver_results':
        return e['type'] == 'waiver_run'
    if f == 'chat':
        return e['type'] == 'chatter' and e.get('team') != team
    if f == 'my_players_status':
        return e['type'] == 'private_item' and e.get('team') == team or (e['type'] == 'status_change' and e.get('player') in mine)
    if f == 'my_players_news':
        return e['type'] == 'news_seen' and e.get('player') in mine
    return False

def due_wakes(lg, now: datetime, check_busy: bool=False) -> list[dict]:
    evs = lg.events()
    st = lg.state()
    out = []
    today = now.astimezone(ET).date().isoformat()
    for tid, seat in sorted(lg.seats.items()):
        if seat.get('house') or lg.is_bot(tid, at=now) or (check_busy and seat_busy(lg, tid)):
            continue
        sch = schedule_of(evs, tid)
        n_today = sum((1 for e in evs if e['type'] == 'wake_open' and e['team'] == tid and e.get('counted', True) and (datetime.fromisoformat(e['started_at']).astimezone(ET).date().isoformat() == today)))
        last_close = max((e['seq'] for e in evs if e['type'] == 'wake_close' and e['team'] == tid), default=0)
        due = [w for w in sch['wakes'] if datetime.fromisoformat(w['at']) <= now]
        pick = next((w for w in due if w.get('retry')), None) or (due[0] if due and n_today < WAKES_PER_DAY else None)
        if pick:
            kind = 'retry' if pick.get('retry') else 'scheduled'
            out.append({'team': tid, 'kind': kind, 'schedule_id': pick['id'], 'text': f'{kind}: "{pick['reason']}"'})
            continue
        mine = _my_players(st, tid)
        hits = [e for e in evs if e.get('seq', 0) > last_close and any((_matches(e, f, tid, mine) for f in sch['subscriptions']))]
        if hits and n_today < WAKES_PER_DAY and (now - datetime.fromisoformat(hits[0]['ts']) >= COALESCE):
            kinds = sorted({e['type'] for e in hits})
            out.append({'team': tid, 'kind': 'event', 'text': f'event: {len(hits)} new ({', '.join(kinds)})', 'events': [e['seq'] for e in hits][:50]})
            continue
        if not lg.has_own_lineup(tid, evs):
            lock = next_lock(lg, tid, now, st)
            if lock and lock - now <= timedelta(minutes=90) and (not any((e['type'] == 'wake_open' and e['team'] == tid and (e.get('kind') == 'engine') and (e.get('lock') == lock.isoformat()) for e in evs))):
                out.append({'team': tid, 'kind': 'engine', 'lock': lock.isoformat(), 'text': 'engine attempt: you have no lineup of your own on record and a lock is near (counted; lineup only)'})
    return out

def next_lock(lg, tid: str, now: datetime, state: dict | None=None) -> datetime | None:
    st = state or lg.state()
    w = st['week']
    world = lg.view(w, now).world
    ks = [world.kickoff(w, p) for p in st['teams'][tid]['lineup'].values() if p]
    ks = [k for k in ks if k and k > now]
    return min(ks) if ks else None

def call_with_hold(gw, req: ModelRequest, hold_until: datetime, clock=_now, sleep=time.sleep, on_alert=None, on_activity=None) -> tuple:
    attempts, backoff, t0 = ([], BACKOFF0, clock())
    alerted = False
    r = None
    while True:
        now = clock()
        if now >= hold_until:
            if attempts:
                return (r, attempts)
            from bm.gateway.core import ModelResponse
            return (ModelResponse(req.request_id, req.entrant_id, '', '', '', None, '', {'kind': 'none'}, {}, {'billed_usd': 0.0}, 0, None, error='TIMEOUT', error_detail='hold deadline passed before the call'), attempts)
        window = min(hold_until - now, timedelta(minutes=30))
        from datetime import timezone as _tz
        r = gw.call(ModelRequest(entrant_id=req.entrant_id, decision_id=req.decision_id, system=req.system, prompt=req.prompt, max_output_tokens=req.max_output_tokens, effort=req.effort, deadline=datetime.now(_tz.utc) + window))
        if clock() >= hold_until:
            r.error = 'TIMEOUT'
            r.error_detail = 'response received after the hold deadline; standing state retained'
        attempts.append({'at': now.isoformat(), 'request_id': r.request_id, 'error': r.error, 'detail': (r.error_detail or '')[:200], 'route_history': getattr(r, 'route_history', []), 'attempt_totals': getattr(r, 'attempt_totals', {})})
        if r.error is None or r.error not in INFRA:
            return (r, attempts)
        now = clock()
        if now >= hold_until:
            return (r, attempts)
        if not alerted and now - t0 >= ALERT_AFTER and on_alert:
            on_alert(f'seat {req.entrant_id} failing for {(now - t0).total_seconds() / 60:.0f} min: {r.error}')
            alerted = True
        wait = min(backoff * random.uniform(0.85, 1.15), (hold_until - now).total_seconds())
        if on_activity:
            on_activity('paused', cause=r.error, lane=r.lane, retry_at=(now + timedelta(seconds=max(wait, 0))).isoformat())
        sleep(max(wait, 0))
        if on_activity and clock() < hold_until:
            on_activity('on_clock')
        backoff = min(backoff * 2, BACKOFF_CAP)

def _params(lg, tid: str, rules) -> dict:
    p = bud.policy_for(lg.cfg, lg.seat_cfg(tid).get('tier', 1.0))
    return {'team_name': lg.names().get(tid, tid), 'team_id': tid, 'league_name': lg.cfg.get('label', lg.id), 'n_teams': len(lg.seats), 'asymmetry_clause': '', 'embargo': f'{(lg.cfg.get('info') or {}).get('embargo_h', 6)} hours', 'wakes_per_day': WAKES_PER_DAY, 'calls_per_wake': CALLS_PER_WAKE, 'own_latency': 'unknown yet', 'fee': f'${bud.FEE_MARKET_USD:.2f}', 'floor': f'${p.floor:.2f}', 'calls_per_turn': CALLS_PER_TURN, **({'league_format': 'finals', 'start_week': lg.cfg['start_week'], 'end_week': lg.cfg['end_week']} if lg.cfg.get('format') == 'finals' else {})}

def run_wake(lg, gw, tid: str, reason: dict, clock=_now, sleep=time.sleep, alert=None, draft: dict | None=None) -> dict:
    from bm.league.season import iso
    requested = lg.seat_cfg(tid).get('harness', HARNESS_VERSION)
    if requested != HARNESS_VERSION:
        raise ValueError(f'{lg.id}/{tid}: agent harness {requested!r} has no runtime dispatcher')
    start = clock()
    if not draft and lg.is_bot(tid, at=start):
        return {'team': tid, 'skipped': 'bot'}
    data_as_of = datetime.fromisoformat(draft['data_as_of']) if draft and draft.get('data_as_of') else start
    st = lg.state()
    week = st['week']
    seat = lg.seat_cfg(tid)
    ent = lg.entrant(tid, at=start)
    pol = bud.policy_for(lg.cfg, seat.get('tier', 1.0))
    bal = bud.balance(pol, lg.events(), tid, week)
    counted = reason['kind'] in ('scheduled', 'event', 'retry')
    if bal['exhausted'] and counted:
        lg.append({'type': 'wake_skipped', 'ts': iso(start), 'team': tid, 'why': 'budget', 'budget': bal})
        return {'team': tid, 'skipped': 'budget'}
    wake_id = hashlib.sha256(f'{lg.id}:{tid}:{start.isoformat()}:{reason['kind']}'.encode()).hexdigest()[:16]
    variant = seat.get('variant', 'v2.ref')
    if seat.get('disclosure') == 'disclosed' and '+disclosed' not in variant:
        variant += '+disclosed'
    system, hashes = prompts_v2.render_system(variant, lg.rules, dict(_params(lg, tid, lg.rules), tool_catalogue=catalogue_text(ToolServer(lg.ctx(tid, data_as_of)).available()) + ('\n- draft_pick(player_id: string): make your pick (draft only)\n- draft_queue(player_ids: array): your next picks in order; used automatically when you are on the clock (draft only)' if draft else '')))
    wake = {'wake_id': wake_id, 'started_at': start.isoformat()}
    lg.append({'type': 'wake_open', 'ts': iso(start), 'team': tid, 'wake_id': wake_id, 'started_at': start.isoformat(), 'kind': reason['kind'], 'reason': reason['text'], 'schedule_id': reason.get('schedule_id'), 'lock': reason.get('lock'), 'counted': counted, 'variant': variant, 'prompt_hashes': hashes, 'harness_version': HARNESS_VERSION, 'data_as_of': data_as_of.isoformat(), 'context_clock_source': 'configured_draft_as_of' if draft and draft.get('data_as_of') else 'wake_start_header_tool_call_clock'})
    hold_until = min(start + HOLD_MAX, next_lock(lg, tid, start, st) or start + HOLD_MAX)
    if draft:
        hold_until = datetime.fromisoformat(draft['hold_until'])
    ctx = lg.ctx(tid, data_as_of, wake)
    header = _header(lg, ctx, reason, bal, draft)
    header_view = getattr(ctx, '_cache', {}).get('view')
    header_view_as_of = getattr(header_view, 'as_of', None)
    convo, turns, tools_log, calls_used, fees = ([], [], [], 0, 0.0)
    ended_by, outcome, repaired = ('turns', 'own', False)
    for k in range(MAX_TURNS):
        if clock() - start > RUNAWAY:
            ended_by = 'runaway_guard'
            break
        prompt = header + ('\n\n' + '\n\n'.join(convo) if convo else '') + '\n\nYour turn: reply with ONE JSON object.'
        max_out = 16000
        if counted:
            spend = bud.balance(pol, lg.events(), tid, week)['spendable_usd']
            est_in = (len(system) + len(prompt)) / 2.5 * records.INWORLD_IN_PER_M / 1000000.0
            if est_in + 16000 * records.INWORLD_OUT_PER_M / 1000000.0 > spend:
                ended_by = 'budget'
                break
        effort = seat.get('effort', 'vendor-default')
        req = ModelRequest(entrant_id=ent, decision_id=f'{lg.id}:wake:{wake_id}:t{k + 1}', system=system, prompt=prompt, max_output_tokens=max_out, effort=effort)
        r, attempts = call_with_hold(gw, req, hold_until, clock=clock, sleep=sleep, on_alert=alert, on_activity=draft.get('on_activity') if draft else None)
        turns.append(dict(_turn_row(r, attempts, gw), effort=effort, max_output_tokens=max_out))
        cost = records.inworld_usd(r.usage or {}, _adapter(gw, r.lane))
        if cost and counted and (not (r.error and r.error in INFRA)):
            lg.append(bud.debit(tid, week, cost, 'turn', f'{wake_id}:t{k + 1}', iso(clock())))
        if r.error:
            outcome = 'lane_failed' if r.error in INFRA else 'model_failed'
            ended_by = r.error
            break
        d = extract_json(r.text)
        if d is None or not isinstance(d.get('tool_calls'), list):
            if repaired:
                outcome, ended_by = ('model_failed', 'unparseable')
                break
            repaired = True
            why = json_problem(r.text) if d is None else f'the object had no "tool_calls" list (its keys: {', '.join(sorted(map(str, d))[:12]) or 'none'})' if 'tool_calls' not in d else f'"tool_calls" was a {type(d['tool_calls']).__name__}, not a list'
            convo.append(f'YOUR REPLY WAS NOT A SINGLE JSON OBJECT WITH tool_calls: {why}. Fix that and reply again with ONE JSON object only.')
            continue
        results, stop = ([], False)
        for c in d['tool_calls'][:CALLS_PER_TURN]:
            if not isinstance(c, dict) or not isinstance(c.get('args', {}), dict) or (not isinstance(c.get('tool', ''), str)):
                results.append({'tool': '-', 'error': 'each tool call must be {"tool": "<name>", "args": {...}}'})
                continue
            if calls_used >= CALLS_PER_WAKE:
                results.append({'tool': '-', 'error': f'tool-call cap of {CALLS_PER_WAKE} for this wake reached'})
                stop = True
                break
            calls_used += 1
            name, args = (str((c or {}).get('tool', '')), (c or {}).get('args') or {})
            t0 = time.time()
            if draft and name in ('draft_pick', 'draft_queue'):
                errs = draft['on_pick' if name == 'draft_pick' else 'on_queue'](args.get('player_id') if name == 'draft_pick' else args.get('player_ids'))
                res_data, ok = ({'error': '; '.join(errs)} if errs else {'ok': True}, not errs)
                tools_log.append({'tool': name, 'args': args, 'ok': ok, 'result_sha256': hashlib.sha256(json.dumps(res_data, sort_keys=True).encode()).hexdigest(), 'fee_usd': 0})
                results.append({'tool': name, 'result': res_data})
                if name == 'draft_pick' and ok:
                    stop = True
                continue
            ctx = lg.ctx(tid, data_as_of if draft else clock(), wake)
            res = ToolServer(ctx).call(name, args)
            _side_effects(lg, tid, wake, res, clock(), week)
            if res.fee_usd:
                fees += res.fee_usd
                lg.append(bud.debit(tid, week, res.fee_usd, f'fee:{name}', wake_id, iso(clock())))
            tool_view_as_of = getattr(getattr(ctx, '_cache', {}).get('view'), 'as_of', None)
            tools_log.append({'tool': name, 'args': args, 'ok': res.ok, 'result_sha256': res.hash, 'fee_usd': res.fee_usd, 'context_as_of': ctx.now.isoformat(), 'view_as_of': tool_view_as_of.isoformat() if tool_view_as_of else None, 'ms': int((time.time() - t0) * 1000)})
            results.append({'tool': name, 'result': res.data})
            if res.ends_wake:
                stop = True
        convo.append('YOU: ' + json.dumps(d)[:4000] + '\nRESULTS: ' + json.dumps(results, default=str)[:24000])
        if stop:
            ended_by = 'sleep' if any((t['tool'] == 'sleep' for t in tools_log[-CALLS_PER_TURN:])) else 'draft_pick' if draft else 'cap'
            break
        if bud.balance(pol, lg.events(), tid, week)['exhausted'] and counted:
            ended_by = 'budget'
            break
    end = clock()
    inworld = round(sum((t['inworld_usd'] for t in turns)), 6)
    lg.append({'type': 'wake_close', 'ts': iso(end), 'team': tid, 'wake_id': wake_id, 'turns': len(turns), 'tool_calls': calls_used, 'ended_by': ended_by, 'outcome': outcome, 'inworld_usd': inworld, 'fees_usd': round(fees, 4)})
    if outcome == 'lane_failed':
        lg.append({'type': 'wake_failed', 'ts': iso(end), 'team': tid, 'wake_id': wake_id, 'cause': ended_by, 'note': "infrastructure: the seat's standing decisions stand; excluded from timing and agentic scores (D-REL-1a)"})
        if not draft:
            lock = next_lock(lg, tid, end) or end + timedelta(days=7)
            at = end + timedelta(minutes=30)
            if at < lock:
                sid = hashlib.sha256(f'retry:{wake_id}'.encode()).hexdigest()[:10]
                lg.append({'type': 'wake_schedule', 'ts': iso(end), 'team': tid, 'wake_id': wake_id, 'op': 'schedule', 'id': sid, 'at': at.isoformat(), 'reason': f'resume after an infrastructure failure ({ended_by}): {reason['text']}'[:300], 'retry': True})
    rec = records.make('wake', wake_id, iso(start), league=lg.id, week=week, team=tid, entrant=ent, variant_id=variant, prompt_hashes=hashes, harness='agent', harness_version=HARNESS_VERSION, reason=reason, outcome=outcome, ended_by=ended_by, data_as_of=data_as_of.isoformat(), header_view_as_of=header_view_as_of.isoformat() if header_view_as_of else None, context_clock_source='configured_draft_as_of' if draft and draft.get('data_as_of') else 'wake_start_header_tool_call_clock', failure=records.failure(ended_by if outcome != 'own' else None), turns=turns, tools=tools_log, served_models=sorted({t['served_model'] for t in turns if t.get('served_model')}), cost={'inworld_usd': inworld, 'fees_usd': round(fees, 4), **records.cost_summary(turns)})
    records.write(rec, records.store(lg.id))
    return {'team': tid, 'wake_id': wake_id, 'turns': len(turns), 'tool_calls': calls_used, 'outcome': outcome, 'ended_by': ended_by, 'inworld_usd': inworld}

def _adapter(gw, lane: str | None) -> str | None:
    try:
        return gw.lanes[lane]['adapter']
    except Exception:
        return None

def _turn_row(r, attempts: list, gw) -> dict:
    row = records.turn({'request_id': r.request_id, 'lane': r.lane, 'model_reported': r.model_reported, 'model_requested': r.model_requested, 'usage': r.usage, 'cost': r.cost, 'latency_ms': r.latency_ms, 'attempts': r.attempts, 'error': r.error, 'error_detail': r.error_detail, **{k: getattr(r, k, None) for k in ('attempt_history', 'attempt_totals', 'identity_source', 'stop_reason', 'gateway_seq', 'prompt_blob', 'system_blob', 'text_blob')}}, {r.lane: _adapter(gw, r.lane)})
    row['hold_attempts'] = attempts
    totals = [a['attempt_totals'] for a in attempts if a.get('attempt_totals')]
    if totals:
        complete = len(totals) == len(attempts)
        keys = {k for t in totals for k in t.get('usage') or {}}
        row['attempt_totals'] = {'scope': 'hold_all_gateway_calls', 'usd_eq_observed_lower_bound': sum((t.get('usd_eq_observed_lower_bound', t.get('usd_eq') or 0) for t in totals)), 'usage': {k: sum(((t.get('usage') or {}).get(k) or 0 for t in totals)) for k in keys}, 'usage_complete': complete and all((t.get('usage_complete', True) for t in totals)), 'billed_cost_complete': complete and all((t.get('billed_cost_complete', True) for t in totals)), 'billed_usd': sum((t.get('billed_usd') or 0 for t in totals)), 'usd_eq': sum((t.get('usd_eq') or 0 for t in totals)) if complete and all((t.get('usd_eq') is not None for t in totals)) else None}
    row.update(records.transport_cost_fields(row))
    row['terminal_uniform_cost_usd'] = row['inworld_usd']
    if r.error in INFRA:
        row['inworld_usd'] = 0.0
    return row

def _side_effects(lg, tid: str, wake: dict, res, now: datetime, week: int) -> None:
    from bm.league.season import iso
    if res.notebook_change:
        nb = nbk.state(lg.events(), tid)
        lg.append(nbk.edit_event(tid, nb, res.notebook_change, iso(now), wake['wake_id']))
    if res.wake_change:
        lg.append({'type': 'wake_schedule', 'ts': iso(now), 'team': tid, 'wake_id': wake['wake_id'], **res.wake_change})
    if res.queued_action:
        a = res.queued_action
        if a['kind'] == 'file_prophecy':
            lg.append(bookmod.sealed_event(tid, a['prophecy'], iso(now), lg.id))
        elif a['kind'] == 'file_forecast':
            evs = lg.events()
            v = 1 + sum((1 for e in evs if e['type'] == 'forecast_filed' and e['team'] == tid and (e['week'] == week)))
            roster = lg.state()['teams'][tid]['roster']
            lg.append(bookmod.filed_event(tid, week, a['book'], v, iso(now), roster, wake['wake_id'], lg.id))
        else:
            lg.submit(tid, wake, a, now)

def _header(lg, ctx, reason: dict, bal: dict, draft: dict | None) -> str:
    snap = ctx.state_snapshot()
    nb = nbk.opening(ctx.notebook())
    k = 1 + sum((1 for e in ctx.events if e['type'] == 'wake_open' and e['team'] == ctx.team))
    rec = next((f'Week {snap['week']}: {s['record']}' for s in snap['standings'] if s['team'] == ctx.team and 'record' in s), '')
    if snap.get('format') == 'finals':
        row = next((s for s in snap['standings'] if s['team'] == ctx.team))
        rec = f'Finals weeks {snap['start_week']}-{snap['end_week']}; week {snap['week']}: rank {row['rank']}, {row['points']} total points, all-play {row['all_play_w']}-{row['all_play_l']}; no head-to-head matchup'
    m = snap['matchup']
    if m:
        rec += f', vs {m['opponent']} ({m['opponent_record']})'
    st = ctx.state['teams'][ctx.team]
    locks = []
    for s, p in st['lineup'].items():
        if p:
            kk = ctx.view.world.kickoff(ctx.week, p)
            if kk and kk > ctx.now:
                locks.append((kk, s, p))
    nl = '; '.join((f'{kk.astimezone(ET).strftime('%a %H:%M')} {s} {ctx.view.world.players[p]['name']}' for kk, s, p in sorted(locks)[:6])) or 'none this week'
    h = prompts_v2.wake_header(k, ctx.now.astimezone(ET).strftime('%a %b %d %H:%M'), reason['text'], bud.header(bal), rec, nl, '; '.join(snap['due']) or 'nothing', json.dumps(snap['inbox'], default=str)[:6000] if snap['inbox'] else 'nothing new', nb)
    if draft:
        h += f'\n\nDRAFT: you are on the clock: pick {draft['pick']} of {draft['total']} (snake). Research with the tools, then call draft_pick with one player id. You may also set draft_queue (your next picks in order; used automatically when you are on the clock). The board waits for you; there is no pick clock.\n{draft.get('board', '')}'
    if not ctx.book_filed() and (not draft):
        h += '\n\n' + bookmod.task_text(ctx.week, ctx.state['teams'][ctx.team]['roster'], ctx.view)
    return h

def run_draft(lg, gw, clock=_now, sleep=time.sleep, alert=None) -> dict:
    from bm.agent import bots
    from bm.engine.defaults import best_lineup, default_draft_pick, legal_draft_choices
    from bm.engine.state import current_drafter
    from bm.league.season import iso
    if lg.done('draft'):
        lg._publish_draft_feed()
        return {'done': True}
    as_of = datetime.fromisoformat(lg.cfg['draft_as_of'])
    view = lg.view(lg.cfg['start_week'], as_of)
    prospective = lg.cfg['start_week'] >= 5 and getattr(view, 'mode', 'observed') != 'reconstructed'

    def blocked(cause):
        if not any((e['type'] == 'draft_window_gap' and e.get('cause') == cause for e in lg.events())):
            lg.append({'type': 'draft_window_gap', 'ts': iso(clock()), 'step': 'draft', 'cause': cause, 'data_as_of': as_of.isoformat(), 'deadline': first_lock.isoformat() if first_lock else None})
        lg._publish_draft_feed()
        return {'blocked': cause}
    locks = [datetime.fromisoformat(k) for k in view.world.kickoffs.get(lg.cfg['start_week'], {}).values()]
    first_lock = min(locks) if locks else None
    if prospective and first_lock is None:
        return blocked('original draft cutoff unmeasured')
    if first_lock is None:
        raise ValueError('draft first lock missing')
    total = lg.rules.roster_size * len(lg.seats)
    made = {'model': 0, 'queue': 0, 'bot': 0, 'infra_default': 0, 'model_default': 0}
    paused = None
    while True:
        if prospective and clock() >= first_lock:
            return blocked('original draft cutoff passed')
        st = lg.state()
        tid = current_drafter(st)
        if tid is None:
            break
        pick = st['draft']['pick']
        pz = priority.draft_pause(lg.id, clock(), first_lock)
        if pz and pz['hold']:
            if paused is None:
                at = iso(clock())
                paused = {'at': at, **pz}
                lg.append({'type': 'draft_pause', 'ts': at, 'op': 'pause', 'team': tid, 'pick_no': pick + 1, **{k: v for k, v in pz.items() if k != 'hold'}})
                lg._draft_activity('paused', tid, pick, ts=at, logged_at=at, cause='restart_draft_priority', retry_at=pz.get('resume_by'), data_as_of=lg.cfg['draft_as_of'])
            sleep(PAUSE_POLL_S)
            continue
        if paused is not None:
            lg.append({'type': 'draft_pause', 'ts': iso(clock()), 'op': 'resume', 'team': tid, 'pick_no': pick + 1, 'paused_at': paused['at'], 'reason': (pz or {}).get('reason', 'restart priority window no longer applies')})
            paused = None

        def activity(status, **fields):
            at = iso(clock())
            lg._draft_activity(status, tid, pick, ts=at, logged_at=at, data_as_of=lg.cfg['draft_as_of'], **fields)
        activity('on_clock')
        ent = lg.entrant(tid)
        src = 'bot'
        if lg.is_bot(tid) or lg.seat_cfg(tid).get('house'):
            pid = bots.draft('projection-bot' if lg.seat_cfg(tid).get('house') else ent, lg.rules, view, st, tid, lg.cfg['seed'])
        else:
            q = next((e['player_ids'] for e in reversed(lg.events()) if e['type'] == 'draft_queue' and e['team'] == tid), [])
            legal_q = legal_draft_choices(lg.rules, view.world, st, tid, list(q or []))
            if legal_q:
                pid, src = (legal_q[0], 'queue')
            else:
                chosen: dict = {}

                def on_pick(p, st=st, tid=tid):
                    if prospective and clock() >= first_lock:
                        return ['original draft cutoff passed']
                    if not p or not legal_draft_choices(lg.rules, view.world, st, tid, [str(p)]):
                        return [f'{p} is not a legal pick for you (taken, unknown, or your roster could not fill its slots)']
                    chosen['pid'] = str(p)
                    return []

                def on_queue(ps, tid=tid):
                    if prospective and clock() >= first_lock:
                        return ['original draft cutoff passed']
                    ps = [str(x) for x in ps or []][:15]
                    lg.append({'type': 'draft_queue', 'ts': iso(clock()), 'team': tid, 'player_ids': ps})
                    return []
                board = 'BEST AVAILABLE BY SEASON-TO-DATE POINTS: use free_agents (pos, sort) and player_report; picks so far are in transactions.'
                res = {'outcome': 'own'}
                for attempt in range(2):
                    res = run_wake(lg, gw, tid, {'kind': 'draft', 'text': f'draft pick {pick + 1}'}, clock=clock, sleep=sleep, alert=alert, draft={'pick': pick + 1, 'total': total, 'on_pick': on_pick, 'on_queue': on_queue, 'hold_until': first_lock.isoformat(), **({'data_as_of': as_of.isoformat()} if prospective else {}), 'board': board, 'on_activity': activity})
                    if chosen or res['outcome'] == 'lane_failed':
                        break
                if prospective and clock() >= first_lock:
                    return blocked('original draft cutoff passed')
                if prospective and res['outcome'] == 'lane_failed':
                    activity('paused', cause=res.get('ended_by', 'infrastructure'), hold_until=first_lock.isoformat())
                    return {'paused': 'infrastructure', 'deadline': first_lock.isoformat()}
                if chosen:
                    pid, src = (chosen['pid'], 'model')
                else:
                    q = next((e['player_ids'] for e in reversed(lg.events()) if e['type'] == 'draft_queue' and e['team'] == tid), [])
                    legal_q = legal_draft_choices(lg.rules, view.world, lg.state(), tid, list(q or []))
                    if legal_q:
                        pid, src = (legal_q[0], 'queue')
                    else:
                        pid = default_draft_pick(lg.rules, view.world, lg.state(), tid, {p: v['vor'] for p, v in view.proj.items()})
                        src = 'infra_default' if res['outcome'] == 'lane_failed' else 'model_default'
        if prospective and clock() >= first_lock:
            return blocked('original draft cutoff passed')
        made[src] = made.get(src, 0) + 1
        lg.append({'type': 'draft_pick', 'ts': iso(clock()), 'team': tid, 'player': pid, 'pick_no': pick + 1, 'round': pick // len(lg.seats) + 1, 'source': src, 'data_as_of': lg.cfg['draft_as_of']}, view.world, lg.state())
        lg._publish_draft_feed()
    if prospective and clock() >= first_lock:
        return blocked('original draft cutoff passed')
    if not any((e['type'] == 'draft_complete' for e in lg.events())):
        lg.append({'type': 'draft_complete', 'ts': iso(clock()), 'data_as_of': lg.cfg['draft_as_of']}, view.world, lg.state())
    lg._publish_draft_feed()
    from bm.engine.state import Violation
    now = clock()
    lv = lg.view(lg.cfg['start_week'], now)
    done_lu = {e['team'] for e in lg.events() if e['type'] == 'lineup_set'}
    for t in sorted(set(lg.seats) - done_lu):
        if prospective and clock() >= first_lock:
            return blocked('original draft cutoff passed')
        st = lg.state()
        lu = best_lineup(lg.rules, lv.world, st, t, {p: v['week_proj'] for p, v in lv.proj.items()}, now)
        if prospective and clock() >= first_lock:
            return blocked('original draft cutoff passed')
        try:
            lg.append({'type': 'lineup_set', 'ts': iso(now), 'team': t, 'lineup': lu, 'source': 'post_draft_default'}, lv.world, st)
        except Violation as v:
            lg.append({'type': 'lineup_default_failed', 'ts': iso(now), 'team': t, 'violation': v.as_dict()})
    if prospective and clock() >= first_lock:
        return blocked('original draft cutoff passed')
    lg.append({'type': 'step_done', 'ts': iso(clock()), 'step': 'draft'})
    return made

def draft_eta(lg, now: datetime) -> dict:
    picks = [e for e in lg.events() if e['type'] == 'draft_pick']
    total = lg.rules.roster_size * len(lg.seats)
    start = datetime.fromisoformat(lg.cfg['draft_as_of'])
    elapsed_h = max((now - start).total_seconds() / 3600, 1e-09)
    overall = len(picks) / elapsed_h

    def at(e):
        return datetime.fromisoformat(e.get('logged_at') or e['ts'])
    recent = None
    if now - start >= ETA_RECENT:
        recent = sum((1 for e in picks if at(e) >= now - ETA_RECENT)) / (ETA_RECENT.total_seconds() / 3600)
    rate = recent if recent is not None else overall
    left = total - len(picks)
    finish = now + timedelta(hours=left / rate) if rate > 0 else None
    return {'picks': len(picks), 'total': total, 'left': left, 'picks_per_h': round(rate, 2), 'picks_per_h_overall': round(overall, 2), 'picks_per_h_recent': None if recent is None else round(recent, 2), 'projected_finish': finish.isoformat() if finish else None}

def draft_eta_check(lg, now: datetime, send=None) -> dict:
    eta = draft_eta(lg, now)
    bound = priority.draft_eta_bound(lg.cfg)
    eta['bound'] = bound.isoformat() if bound else None
    if bound is None or eta['left'] <= 0:
        return eta
    pz = priority.draft_pause(lg.id, now)
    if pz and pz['hold']:
        eta['paused'] = True
        return eta
    fin = datetime.fromisoformat(eta['projected_finish']) if eta['projected_finish'] else None
    if fin is not None and fin <= bound:
        return eta
    fmt = lambda d: d.astimezone(ET).strftime('%a %b %d %H:%M ET')
    msg = f'BenchedMark {lg.id} agent draft ETA: {eta['picks']}/{eta['total']} picks, {eta['picks_per_h']:.1f} picks/h' + (f' (last 3 h; {eta['picks_per_h_overall']:.1f}/h overall)' if eta['picks_per_h_recent'] is not None else '') + f', {eta['left']} left; projected finish {(fmt(fin) if fin else 'never (no picks in the rate window)')}, past the bound ' + f'{fmt(bound)} (restart draft priority pauses this board from then).'
    if send is None:
        from bm.ops.alert import alert
        send = lambda m: alert('draft-eta', m, key=f'draft-eta|{lg.id}', every_s=ETA_ALERT_EVERY_S)
    eta['alerted'] = bool(send(msg))
    eta['alert'] = msg
    return eta

def tick(lg, gw, now: datetime | None=None, clock=None, sleep=time.sleep, alert=None, max_parallel: int=4, spawn=None) -> dict:
    import threading
    now = now or _now()
    clock = clock or (lambda: now)
    out = {'at': now.isoformat()}
    if not lg.done('draft'):
        if now >= datetime.fromisoformat(lg.cfg['draft_as_of']):
            try:
                out['draft_eta'] = draft_eta_check(lg, now)
            except Exception as e:
                print(f'{lg.id} draft ETA failed: {type(e).__name__}: {e}', flush=True)
            if spawn:
                out['draft'] = 'spawned' if spawn(lg.id, 'draft', None) else 'busy'
            else:
                out['draft'] = run_draft(lg, gw, clock=clock, sleep=sleep, alert=alert)
        return out
    out['rescored'] = lg.rescore_due(now)
    week = lg.state()['week']
    if week > int(lg.cfg.get('end_week', 17)):
        return dict(out, over=True)
    lg.record_locks(week, now)
    out['resolved'] = len(lg.resolve_due(now))
    r = lg.faab_run(week, now)
    if r:
        out['faab_run'] = r
    out['expired'] = lg.expire_offers(now)
    try:
        from bm.league import private as priv
        out['private'] = priv.tick(lg, now)
    except ImportError:
        pass
    out['bot_lineups'] = bot_lineups(lg, now)
    wakes = due_wakes(lg, now, check_busy=bool(spawn))
    if spawn:
        out['wakes'] = [w['team'] for w in wakes if spawn(lg.id, w['team'], w)]
        if lg.score(week, now):
            out['scored'] = week
        return out
    res, lock = ([], threading.Lock())

    def go(w):
        x = run_wake(lg, gw, w['team'], w, clock=clock, sleep=sleep, alert=alert)
        with lock:
            res.append(x)
    for i in range(0, len(wakes), max_parallel):
        ths = [threading.Thread(target=go, args=(w,)) for w in wakes[i:i + max_parallel]]
        [t.start() for t in ths]
        [t.join() for t in ths]
    out['wakes'] = res
    if lg.score(week, now):
        out['scored'] = week
    return out

def bot_lineups(lg, now: datetime) -> int:
    from bm.agent import bots
    from bm.engine.state import Violation
    from bm.league.season import iso, week_steps
    st = lg.state()
    w = st['week']
    due = [d for k, d in week_steps(w) if k.startswith('lineup:') and d <= now]
    if not due:
        return 0
    last = max(due)
    n = 0
    for tid in sorted(lg.seats):
        if not (lg.is_bot(tid, at=now) or lg.seat_cfg(tid).get('house')):
            continue
        if any((e['type'] == 'lineup_set' and e['team'] == tid and (e.get('source') == 'bot') and (datetime.fromisoformat(e['ts']) >= last) for e in lg.events())):
            continue
        kind = 'projection-bot' if lg.seat_cfg(tid).get('house') else lg.entrant(tid, at=now)
        view = lg.view(w, now)
        st = lg.state()
        lu = bots.lineup(kind, lg.rules, view, st, tid, lg.cfg['seed'])
        try:
            lg.append({'type': 'lineup_set', 'ts': iso(now), 'team': tid, 'lineup': lu, 'source': 'bot'}, view.world, st)
            n += 1
        except Violation:
            pass
    return n

def worker(league_id: str, team: str, reason: dict | None, alert=None) -> dict:
    from bm.gateway.core import Gateway
    from bm.league.agentleague import AgentLeague
    lg = AgentLeague(league_id)
    lock = seat_lock(lg, team)
    if lock is None:
        return {'busy': team}
    try:
        gw = Gateway()
        if team == 'draft':
            return {'draft': run_draft(lg, gw, alert=alert)}
        return run_wake(lg, gw, team, reason, alert=alert)
    finally:
        lock.close()
        if team == 'draft':
            lg._publish_draft_feed()
