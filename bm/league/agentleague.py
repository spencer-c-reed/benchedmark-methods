from __future__ import annotations
import hashlib
import json
import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from bm.engine.defaults import best_lineup
from bm.engine.h2h import h2h_standings, round_robin
from bm.engine.scoring import score_player_line, standings as points_standings
from bm.engine.state import Violation, lineup_violations, offer_violations, owner_of
from bm.engine.waivers import resolve_waivers
from bm.log import canonical
from .season import LEAGUES, ROOT, League, iso, now_utc
ET = ZoneInfo('America/New_York')
STEP = timedelta(minutes=5)
FAAB_RUN = (2, 3)

def next_step(t: datetime) -> datetime:
    s = t.astimezone(ET).replace(second=0, microsecond=0)
    m = (s.minute // 5 + (1 if s.minute % 5 or t.second or t.microsecond else 0)) * 5
    return (s.replace(minute=0) + timedelta(minutes=m)).astimezone(ET)

def day_key(t: datetime) -> str:
    return 'd' + t.astimezone(ET).strftime('%Y-%m-%d')

def faab_run_at(week: int) -> datetime:
    from .season import week_anchor
    return (week_anchor(week) - timedelta(days=1)).replace(hour=3)

class AgentLeague(League):
    _views: dict = {}

    def _cache_lock(self):
        import threading
        return self.__dict__.setdefault('_clock', threading.RLock())

    def events(self) -> list[dict]:
        with self._cache_lock():
            return list(self._events_locked())

    def _events_locked(self) -> list[dict]:
        c = self.__dict__.setdefault('_evc', {'off': 0, 'evs': []})
        p = self.log.path
        if not p.exists():
            return []
        size = p.stat().st_size
        if size < c['off']:
            c.update(off=0, evs=[])
        if size > c['off']:
            with open(p, 'rb') as f:
                f.seek(c['off'])
                chunk = f.read()
            end = chunk.rfind(b'\n') + 1
            for line in chunk[:end].splitlines():
                if line.strip():
                    r = json.loads(line)
                    c['evs'].append(dict(r['payload'], seq=r['seq']))
            c['off'] += end
        return c['evs']

    def state(self, world=None) -> dict:
        with self._cache_lock():
            return self._state_locked()

    def _state_locked(self) -> dict:
        from bm.engine.state import apply_event, genesis_state
        from .context import engine_world
        evs = self._events_locked()
        c = self.__dict__.setdefault('_stc', {'n': 0, 'st': None})
        if c['st'] is None or c['n'] > len(evs):
            c.update(n=0, st=None)
        st = c['st']
        w = engine_world()
        for e in evs[c['n']:]:
            if e['type'] == 'genesis':
                st = genesis_state(self.rules, e['league_id'], e['team_ids'], e['draft_order'], e['week'])
            else:
                st = apply_event(self.rules, w, st, e)
        c.update(n=len(evs), st=st)
        return st

    def view(self, week: int, as_of: datetime):
        b = as_of.replace(minute=as_of.minute - as_of.minute % 15, second=0, microsecond=0)
        k = (self.rules.rules_hash, week, b.isoformat())
        if k not in AgentLeague._views:
            if len(AgentLeague._views) > 48:
                AgentLeague._views.clear()
            AgentLeague._views[k] = super().view(week, b)
        return AgentLeague._views[k]

    @property
    def agent(self) -> bool:
        return self.cfg.get('mode') == 'agent'

    @property
    def finals(self) -> bool:
        return self.cfg.get('format') == 'finals'

    def schedule(self) -> dict[int, list[tuple[str, str]]]:
        if self.finals:
            return {}
        return {int(w): [tuple(p) for p in ps] for w, ps in self.cfg['h2h_schedule'].items()}

    def standings(self, state: dict | None=None) -> list[dict]:
        st = state or self.state()
        if self.finals:
            scores = {w: rows for w, rows in st['scores'].items() if int(self.cfg['start_week']) <= int(w) <= int(self.cfg['end_week'])}
            return points_standings(list(st['teams']), scores, self.cfg['seed'])
        return h2h_standings(list(st['teams']), self.schedule(), st['scores'], self.cfg['seed'])

    def opponent(self, tid: str, week: int) -> str | None:
        for a, b in self.schedule().get(week, []):
            if tid in (a, b):
                return b if a == tid else a
        return None

    def finals_trade_window(self, week: int, at: datetime, view) -> dict | None:
        from .season import week_steps
        if not self.finals or not int(self.cfg['start_week']) <= week <= int(self.cfg['end_week']):
            return None
        if week > self.rules.trades['deadline_week']:
            return None
        for key, scheduled in week_steps(week):
            if not key.startswith('trade:'):
                continue
            n = int(key.rsplit(':', 1)[1])
            opens = scheduled + timedelta(minutes=int(self.cfg.get('step_offset_min') or 0))
            cutoff = self._trade_cutoff(week, n, opens, view)
            if cutoff is not None and opens <= at < cutoff:
                return {'target_week': week, 'original_window': f'w{week}-t{n}', 'original_cutoff': cutoff.isoformat(), 'delivery_protocol': 'agent-finals-trade/1'}
        return None

    def seat_cfg(self, tid: str) -> dict:
        return self.seats[tid]

    def ctx(self, tid: str, now: datetime, wake: dict | None=None) -> 'SeatContext':
        return SeatContext(self, tid, now, wake)

    def book_filed(self, tid: str, week: int, events: list[dict] | None=None) -> bool:
        return any((e['type'] == 'forecast_filed' and e['team'] == tid and (e['week'] == week) for e in events or self.events()))

    def has_own_lineup(self, tid: str, events: list[dict] | None=None) -> bool:
        return any((e['type'] == 'lineup_set' and e['team'] == tid and (e.get('source') == 'model') for e in events or self.events()))

    def submit(self, tid: str, wake: dict, action: dict, now: datetime) -> dict:
        action = dict(action)
        original_run = action.pop('_original_run', None)
        original_trade = action.pop('_original_trade', None)
        aid = hashlib.sha256(f'{wake['wake_id']}:{action['kind']}:{canonical(action).decode()}'.encode()).hexdigest()[:16]
        ev = {'type': 'action_submitted', 'ts': iso(now), 'team': tid, 'wake_id': wake['wake_id'], 'wake_started_at': wake['started_at'], 'action_id': aid, 'resolves_at': next_step(now).isoformat(), 'action': action}
        if action['kind'] == 'waiver_bid':
            if original_run is not None:
                ev.update({k: original_run.get(k) if isinstance(original_run, dict) else None for k in ('target_week', 'original_cutoff', 'delivery_protocol')})
            else:
                week = self.state()['week']
                if week >= 5 and getattr(self.view(week, now), 'mode', 'observed') != 'reconstructed':
                    ev.update(target_week=week, original_cutoff=faab_run_at(week).isoformat(), delivery_protocol='waiver-original-run/1')
        if self.finals and action['kind'] in ('propose_trade', 'respond_trade'):
            week = self.state()['week']
            original = original_trade or self.finals_trade_window(week, now, self.view(week, now))
            if not original:
                raise Violation('TRADE_WINDOW_CLOSED', 'no current finals trade window')
            ev.update({k: original.get(k) for k in ('target_week', 'original_window', 'original_cutoff', 'delivery_protocol')})
        self.append(ev)
        return ev

    def resolve_due(self, now: datetime) -> list[dict]:
        evs = self.events()
        done = {e['action_id'] for e in evs if e['type'] == 'action_resolved'}
        pend = [e for e in evs if e['type'] == 'action_submitted' and e['action_id'] not in done and (datetime.fromisoformat(e['resolves_at']) <= now)]
        out = []
        for step in sorted({e['resolves_at'] for e in pend}):
            batch = sorted([e for e in pend if e['resolves_at'] == step], key=lambda e: (e['wake_started_at'], hashlib.sha256(f'{self.cfg['seed']}:{step}:{e['wake_id']}'.encode()).hexdigest(), e['seq']))
            for e in batch:
                at = now if self.finals and e['action']['kind'] in ('propose_trade', 'respond_trade') else datetime.fromisoformat(step)
                out.append(self._resolve(e, at))
        return out

    def _resolve(self, sub: dict, at: datetime) -> dict:
        a, tid = (sub['action'], sub['team'])
        world = self.view(self.state()['week'], at).world
        st = self.state(world)
        res = {'type': 'action_resolved', 'ts': iso(at), 'team': tid, 'action_id': sub['action_id'], 'kind': a['kind'], 'ok': True, 'detail': ''}
        try:
            for ev in self._events_for(a, tid, sub, st, world, at):
                st = self.state(world)
                self.append(ev, world, st)
        except Violation as v:
            res.update(ok=False, detail=f'{v.code}: {v.detail}')
        self.append(res)
        return res

    def _events_for(self, a: dict, tid: str, sub: dict, st: dict, world, at: datetime) -> list[dict]:
        ts, k = (iso(at), a['kind'])
        base = {'ts': ts, 'decision_id': sub['action_id'], 'wake_id': sub['wake_id'], 'submitted_at': sub['ts']}
        if k == 'set_lineup':
            lu = {s: p for s, p in a['slots'].items()}
            return [{'type': 'lineup_set', 'team': tid, 'lineup': lu, 'source': 'model', 'note': a.get('note', ''), **base}]
        if k == 'waiver_bid':
            prospective = sub.get('delivery_protocol') == 'waiver-original-run/1' or (st['week'] >= 5 and getattr(self.view(st['week'], at), 'mode', 'observed') != 'reconstructed')
            if prospective:
                week = sub.get('target_week')
                try:
                    cutoff = datetime.fromisoformat(sub['original_cutoff'])
                    if sub.get('delivery_protocol') != 'waiver-original-run/1' or type(week) is not int or cutoff.tzinfo is None:
                        raise ValueError('original run unavailable')
                except (KeyError, ValueError, TypeError):
                    raise Violation('WAIVER_CONTEXT', 'original target run is missing/invalid; no inferred retarget')
                if week != st['week']:
                    raise Violation('WAIVER_STALE', 'original target week changed; no retarget to another run')
                if at > cutoff or self.done(f'waiver_run:{week}') or any((e['type'] == 'waiver_run' and e.get('week') == week for e in self.events())):
                    raise Violation('WAIVER_CLOSED', 'original sealed run already closed; bid not applied')
            return [{'type': 'faab_bid', 'team': tid, 'week': st['week'], 'bids': a['bids'], 'note': a.get('note', ''), **base}]
        if k == 'add_drop':
            if self.finals and (not (self.rules.raw.get('waivers') or {}).get('fcfs_after_run')):
                raise Violation('FCFS_DISABLED', 'this finals ruleset uses sealed FAAB only')
            if at < faab_run_at(st['week']) or not any((e['type'] == 'waiver_run' and e['week'] == st['week'] for e in self.events())):
                raise Violation('BEFORE_RUN', "first come, first served opens after this week's FAAB run")
            return [{'type': 'waiver_award', 'team': tid, 'add': a['add'], 'drop': a.get('drop'), 'amount': 0, 'source': 'fcfs', 'note': a.get('note', ''), **base}]
        if k in ('propose_trade', 'respond_trade'):
            if self.finals:
                base.update({k: sub.get(k) for k in ('target_week', 'original_window', 'original_cutoff', 'delivery_protocol')})
            return self._trade_events(a, tid, st, at, base)
        if k == 'post_chat':
            return [{'type': 'chatter', 'team': tid, 'window': day_key(at), 'text': a['text'][:self.rules.max_msg_chars], **base}]
        if k == 'report':
            return [{'type': 'report', 'team': tid, 'category': a['category'], 'text': a['text'], **base}]
        if k == 'resource_request':
            return [{'type': 'resource_request', 'team': tid, 'what': a['what'], 'why': a['why'], 'granted': False, **base}]
        raise Violation('BAD_ACTION', k)

    def _trade_events(self, a: dict, tid: str, st: dict, at: datetime, base: dict) -> list[dict]:
        out = []
        window, expires = (day_key(at), at + timedelta(hours=self._ttl()))
        if self.finals:
            current = self.finals_trade_window(st['week'], at, self.view(st['week'], at))
            try:
                cutoff = datetime.fromisoformat(base['original_cutoff'])
                submitted = datetime.fromisoformat(base['submitted_at'])
                valid = base['delivery_protocol'] == 'agent-finals-trade/1' and current is not None and (base['target_week'] == st['week']) and (base['original_window'] == current['original_window']) and (submitted <= at < cutoff) and (cutoff.tzinfo is not None)
            except (ValueError, KeyError, TypeError):
                valid = False
            if not valid:
                raise Violation('TRADE_WINDOW_CLOSED', 'original finals trade window is closed or unmeasured; no retarget')
            window = current['original_window']
            expires = min(cutoff, datetime.fromisoformat(current['original_cutoff']))
        if a['kind'] == 'propose_trade':
            to = a['to_team']
            if to in self.seats and self.entrant(to) == self.entrant(tid):
                raise Violation('INELIGIBLE_PARTNER', f'{to} is not eligible to trade with {tid}')
            n = sum((1 for e in self.events() if e['type'] == 'trade_msg' and e.get('from_team') == tid and (e.get('action') == 'PROPOSE')))
            th = f'{self.id}-{tid[-3:]}-{n + 1}'
            out.append({'type': 'trade_msg', 'thread_id': th, 'window': window, 'from_team': tid, 'to_team': to, 'action': 'PROPOSE', 'offer': _offer(a), 'text': a['message'][:self.rules.max_msg_chars], 'private_notes': a.get('note', ''), 'est_value': a.get('est_value'), 'p_accept': a.get('p_accept'), 'expires_at': expires.isoformat(), **base})
        else:
            th = a['thread_id']
            t = st['threads'].get(th)
            if not t:
                raise Violation('NO_THREAD', th)
            if self.finals and t['window'] != window:
                raise Violation('TRADE_WINDOW_CLOSED', 'thread belongs to a different finals window')
            other = t['a'] if t['b'] == tid else t['b']
            exp = next((e.get('expires_at') for e in reversed(self.events()) if e['type'] == 'trade_msg' and e['thread_id'] == th and (e.get('action') in ('PROPOSE', 'COUNTER'))), None)
            if exp and a['response'] == 'ACCEPT' and (datetime.fromisoformat(base['submitted_at']) >= datetime.fromisoformat(exp)):
                raise Violation('OFFER_EXPIRED', f'{th}: the offer expired at {exp}')
            ev = {'type': 'trade_msg', 'thread_id': th, 'window': t['window'], 'from_team': tid, 'to_team': other, 'action': a['response'], 'text': a.get('message', '')[:self.rules.max_msg_chars], 'private_notes': a.get('note', ''), 'est_value': a.get('est_value'), **base}
            if a['response'] == 'COUNTER':
                ev['offer'] = _offer(a)
                ev['p_accept'] = a.get('p_accept')
                ev['expires_at'] = expires.isoformat()
            out.append(ev)
        for i, p in enumerate(a.get('iou') or []):
            if isinstance(p, dict) and str(p.get('promise', '')).strip():
                out.append({'type': 'iou', 'team': tid, 'thread_id': th, 'to_team': out[0]['to_team'], 'n': i + 1, 'promise': str(p['promise'])[:500], 'due_week': p.get('due_week'), **base})
        return out

    def _ttl(self) -> int:
        return int(self.rules.trades.get('offer_ttl_hours', 24))

    def expire_offers(self, now: datetime) -> list[str]:
        st = self.state()
        latest = {}
        for e in self.events():
            if e['type'] == 'trade_msg' and e.get('expires_at'):
                latest[e['thread_id']] = e['expires_at']
        gone = []
        for th, t in sorted(st['threads'].items()):
            if t['status'] == 'open' and th in latest and (datetime.fromisoformat(latest[th]) <= now):
                self.append({'type': 'offer_expired', 'ts': iso(now), 'thread_id': th}, self.view(st['week'], now).world, self.state())
                gone.append(th)
        return gone

    def faab_run(self, week: int, now: datetime) -> dict | None:
        key = f'waiver_run:{week}'
        if self.done(key) or now < faab_run_at(week):
            return None
        at = faab_run_at(week)
        view = self.view(week, at)
        state = self.state(view.world)
        latest: dict[str, list] = {}
        for e in self.events():
            if e['type'] == 'faab_bid' and e['week'] == week and (datetime.fromisoformat(e['ts']) <= at):
                latest[e['team']] = e['bids']
        bids = []
        for tid, bl in sorted(latest.items()):
            for i, b in enumerate(bl):
                if isinstance(b, dict) and b.get('add'):
                    try:
                        bids.append({'team': tid, 'add': b['add'], 'drop': b.get('drop') or None, 'amount': int(b.get('amount', 0)), 'priority': i + 1})
                    except (TypeError, ValueError):
                        continue
        worst_first = [r['team_id'] for r in reversed(self.standings(state))]
        eff = max(at, now)
        awards, results = resolve_waivers(self.rules, view.world, state, bids, worst_first, f'{self.id}:w{week}', iso(eff))
        for ev in awards:
            if eff > at:
                ev['scheduled_ts'] = iso(at)
            self.append(ev, view.world, self.state(view.world))
        self.append({'type': 'waiver_run', 'ts': iso(at), 'week': week, 'results': results, 'seed': f'{self.id}:w{week}'})
        self.append({'type': 'step_done', 'ts': iso(at), 'step': key})
        return {'awards': len(awards), 'bids': len(bids)}

def _offer(a: dict) -> dict:
    o = {'give': list(a.get('give') or []), 'get': list(a.get('get') or [])}
    for k in ('faab_give', 'faab_get'):
        if a.get(k):
            o[k] = int(a[k])
    return o

@dataclass
class SeatContext:
    lg: AgentLeague
    team: str
    now: datetime
    wake: dict | None = None
    agent_seat: bool = True
    disabled_tools: tuple = ()
    _cache: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.lg.finals and (not (self.rules.raw.get('waivers') or {}).get('fcfs_after_run')):
            self.disabled_tools = tuple(self.disabled_tools) + ('add_drop',)
        if not self.lg.cfg.get('market_tools', True):
            self.disabled_tools = tuple(self.disabled_tools) + ('market_consensus', 'market_trending', 'our_projection', 'lines')

    @property
    def events(self) -> list[dict]:
        if 'ev' not in self._cache:
            self._cache['ev'] = self.lg.events()
        return self._cache['ev']

    @property
    def state(self) -> dict:
        if 'st' not in self._cache:
            self._cache['st'] = self.lg.state()
        return self._cache['st']

    @property
    def week(self) -> int:
        return self.state['week']

    @property
    def view(self):
        if 'view' not in self._cache:
            self._cache['view'] = self.lg.view(self.week, self.now)
        return self._cache['view']

    @property
    def rules(self):
        return self.lg.rules

    def book_filed(self) -> bool:
        return self.lg.book_filed(self.team, self.week, self.events)

    @property
    def embargo(self):
        from bm.league.private import embargo
        return embargo(self.lg)

    def hidden(self, pid: str | None, first_seen_at: str | None) -> bool:
        if not self.embargo or not pid or (not first_seen_at):
            return False
        o = owner_of(self.state, pid)
        return o is not None and o != self.team and (datetime.fromisoformat(first_seen_at) > self.now - self.embargo)

    def status_view(self, pid: str):
        if self.embargo and owner_of(self.state, pid) not in (None, self.team):
            k = 'pview'
            if k not in self._cache:
                self._cache[k] = self.lg.view(self.week, self.now - self.embargo)
            return self._cache[k]
        return self.view

    def next_step(self) -> datetime:
        return next_step(self.now)

    def notebook(self) -> dict:
        from bm.agent import notebook as nbk
        return nbk.state(self.events, self.team)

    def _pts(self, pid: str, w: int) -> float | None:
        ln = self.view.lines.get(w, {}).get(pid)
        return None if ln is None else score_player_line(self.rules, self.view.world.players[pid]['pos'], ln)

    def player_row(self, pid: str) -> dict:
        p = self.view.world.players.get(pid, {'name': pid, 'pos': '?', 'team': '?'})
        logs = [(w, self._pts(pid, w)) for w in range(1, self.week)]
        played = [x for _, x in logs if x is not None]
        row = {'id': pid, 'name': p['name'], 'pos': p['pos'], 'team': p['team'], 'status': self.status_view(pid).proj.get(pid, {}).get('status') or None, 'ytd': round(sum(played), 1), 'games': len(played), 'last3': [None if x is None else round(x, 1) for _, x in logs[-3:]]}
        opp = self.view.opp.get(p['team'])
        if opp:
            k = datetime.fromisoformat(opp[1])
            row.update(opponent=opp[0], kickoff=k.astimezone(ET).strftime(ET_FMT), locked=self.now >= k)
        else:
            row['bye'] = True
        if self.book_filed():
            pr = self.status_view(pid).proj.get(pid, {})
            row.update(our_projection_week=round(pr.get('week_proj', 0.0), 1))
        return row

    def resolve(self, ref: str) -> str:
        if ref in self.view.world.players:
            return ref
        hits = [p for p, x in self.view.world.players.items() if x['name'].lower() == str(ref).strip().lower()]
        return hits[0] if len(hits) == 1 else ref

    def player_report(self, pid: str) -> dict:
        from bm.stats import nflverse as nv
        from bm.league.context import SOURCE_PROVENANCE_FROM
        source_at = self.view.as_of if self.view.as_of >= SOURCE_PROVENANCE_FROM else None
        pid = self.resolve(pid)
        if pid not in self.view.world.players:
            return {'id': pid, 'error': 'unknown player'}
        row = self.player_row(pid)
        usage = {}
        for w in range(max(1, self.week - 3), self.week):
            ln = self.view.lines.get(w, {}).get(pid)
            if ln:
                usage[f'w{w}'] = {k: ln.get(k) for k in ('targets', 'receptions', 'carries', 'attempts', 'receiving_yards', 'rushing_yards') if ln.get(k) not in (None, '', '0')}
        prior = (nv.prior_lines(2026, source_at) if source_at else nv.prior_lines(2026)).get(pid, [])
        pos = self.view.world.players[pid]['pos']
        ppg = round(sum((score_player_line(self.rules, pos, r) for r in prior)) / len(prior), 1) if prior else None
        inj = [{k: r.get(k) for k in ('week', 'report_status', 'report_primary_injury', 'practice_status', 'first_seen_at')} for r in self.view.injuries if r.get('gsis_id') == pid and (not self.hidden(pid, r.get('first_seen_at')))]
        nxt = [f'w{g['week']} ' + ('@' + g['home'] if g['away'] == row['team'] else g['away']) for g in sorted(nv.schedule(2026, source_at) if source_at else nv.schedule(2026), key=lambda g: g['kickoff']) if g['week'] >= self.week and row['team'] in (g['home'], g['away'])][:3]
        owner = owner_of(self.state, pid) or 'free agent'
        return {**row, 'weekly_points': {f'w{w}': None if x is None else round(x, 1) for w in range(1, self.week) for x in [self._pts(pid, w)]}, 'ppg_2025': ppg, 'usage': usage, 'injury_lines': inj, 'next': nxt, 'owner': owner}

    def stats_rows(self, player_ids, team, pos, weeks, metric) -> list[dict]:
        weeks = [int(w) for w in weeks or range(1, self.week)]
        out = []
        for w in weeks:
            for pid, ln in sorted(self.view.lines.get(w, {}).items()):
                p = self.view.world.players.get(pid)
                if not p or (player_ids and pid not in player_ids) or (team and p['team'] != team) or (pos and p['pos'] != pos.upper()):
                    continue
                val = score_player_line(self.rules, p['pos'], ln) if metric == 'fantasy_points' else ln.get(metric)
                out.append({'week': w, 'id': pid, 'name': p['name'], 'pos': p['pos'], 'team': p['team'], metric: val})
        if metric == 'fantasy_points':
            out.sort(key=lambda r: (-(r[metric] or 0), r['id']))
        return out

    def free_agents(self, pos: str, n: int, sort: str) -> list[dict]:
        owned = {p for t in self.state['teams'].values() for p in t['roster']}
        fa = [p for p, x in self.view.world.players.items() if x['pos'] == pos and p not in owned]
        rows = [self.player_row(p) for p in fa]
        if sort == 'recent':
            key = lambda r: -sum((x or 0 for x in r['last3']))
        elif sort == 'projection':
            key = lambda r: -self.view.proj.get(r['id'], {}).get('week_proj', 0.0)
        elif sort == 'usage':
            key = lambda r: -sum((float(self.view.lines.get(w, {}).get(r['id'], {}).get(k) or 0) for w in range(max(1, self.week - 3), self.week) for k in ('targets', 'carries')))
        else:
            key = lambda r: -r['ytd']
        return sorted(rows, key=lambda r: (key(r), r['id']))[:n]

    def team_roster(self, tid: str) -> dict:
        t = self.state['teams'].get(tid)
        if not t:
            return {'error': f'unknown team {tid}'}
        rec = next((r for r in self.lg.standings(self.state) if r['team_id'] == tid), {})
        inv = {p: s for s, p in t['lineup'].items() if p}
        return {'team_id': tid, 'name': self.lg.names().get(tid), 'faab': t['faab'], **({'points': rec.get('points', 0), 'all_play_w': rec.get('all_play_w', 0), 'all_play_l': rec.get('all_play_l', 0)} if self.lg.finals else {'record': f'{rec.get('w', 0)}-{rec.get('l', 0)}-{rec.get('t', 0)}'}), 'roster': [dict(self.player_row(p), slot=inv.get(p, 'BN')) for p in t['roster']], 'recent_moves': [r for r in self.transactions() if r.get('team') == tid][-5:]}

    def transactions(self) -> list[dict]:
        nm = lambda pid: self.view.world.players.get(pid, {}).get('name', pid) if pid else None
        rows = []
        for e in self.events:
            if e['type'] == 'waiver_award':
                rows.append({'ts': e['ts'], 'team': e['team'], 'kind': 'fcfs' if e.get('source') == 'fcfs' else 'faab', 'add': nm(e['add']), 'drop': nm(e.get('drop')), 'amount': e['amount']})
            elif e['type'] == 'trade_msg' and e['action'] == 'ACCEPT':
                rows.append({'ts': e['ts'], 'team': e['from_team'], 'kind': 'trade', 'thread': e['thread_id'], 'with': e['to_team']})
            elif e['type'] == 'chatter':
                rows.append({'ts': e['ts'], 'team': e['team'], 'kind': 'chat', 'text': e['text'][:300]})
        return rows

    def my_matchups(self, weeks: list[int]) -> dict:
        return {w: self.lg.opponent(self.team, w) for w in weeks}

    def state_snapshot(self) -> dict:
        from bm.agent import budget
        t = self.state['teams'][self.team]
        inv = {p: s for s, p in t['lineup'].items() if p}
        st = self.lg.standings(self.state)
        rec = {} if self.lg.finals else {r['team_id']: f'{r['w']}-{r['l']}-{r['t']}' for r in st}
        opp = self.lg.opponent(self.team, self.week)
        bal = budget.balance(budget.policy_for(self.lg.cfg, self.lg.seat_cfg(self.team).get('tier', 1.0)), self.events, self.team, self.week)
        return {'now': self.now.astimezone(ET).isoformat(), 'week': self.week, 'team': self.team, 'faab': t['faab'], 'lineup': {s: p and self.player_row(p) for s, p in t['lineup'].items()}, 'bench': [self.player_row(p) for p in t['roster'] if p not in inv], 'standings': [{'rank': r['rank'], 'team': r['team_id'], 'points': r['points'], 'all_play_w': r['all_play_w'], 'all_play_l': r['all_play_l']} if self.lg.finals else {'rank': r['rank'], 'team': r['team_id'], 'record': rec[r['team_id']], 'pf': r['pf']} for r in st], **({'format': 'finals', 'start_week': self.lg.cfg['start_week'], 'end_week': self.lg.cfg['end_week']} if self.lg.finals else {}), 'matchup': {'opponent': opp, 'opponent_record': rec.get(opp)} if opp else None, 'due': self.due(), 'budget': bal, 'inbox': self.inbox()}

    def first_lock(self) -> datetime:
        return min((datetime.fromisoformat(k) for k in self.view.world.kickoffs[self.lg.cfg['start_week']].values()))

    def prophecy_sealed(self) -> bool:
        return any((e['type'] == 'prophecy_sealed' and e['team'] == self.team for e in self.events))

    def prophecy_errors(self, p: dict) -> list[str]:
        from bm.agent import book
        if self.prophecy_sealed():
            return ['your prophecy is already sealed']
        if self.now >= self.first_lock():
            return ["the prophecy closed at the league's first lock"]
        return book.prophecy_errors(p, list(self.state['teams']), self.view.world.players)

    def due(self) -> list[str]:
        out = []
        if not self.prophecy_sealed() and self.now < self.first_lock():
            out.append('preseason prophecy (file_prophecy; sealed until the season ends; closes at the first lock)')
        if not self.book_filed():
            out.append(f'forecast book for week {self.week} (market tools locked until filed)')
        run = faab_run_at(self.week)
        if self.now < run:
            out.append(f'FAAB bids close {run.astimezone(ET).strftime(ET_FMT)} ET')
        for th, t in sorted(self.state['threads'].items()):
            if t['status'] == 'open' and self.team in (t['a'], t['b']) and (t['last_from'] != self.team):
                out.append(f'offer awaiting your answer: {th}')
        return out

    def inbox(self) -> list[dict]:
        last = max((e.get('seq', 0) for e in self.events if e['type'] == 'wake_close' and e['team'] == self.team), default=0)
        out = []
        for e in self.events:
            if e.get('seq', 0) <= last:
                continue
            if e['type'] == 'trade_msg' and e['to_team'] == self.team:
                out.append({'kind': 'trade', 'thread': e['thread_id'], 'from': e['from_team'], 'action': e['action'], 'offer': e.get('offer'), 'message': e.get('text', ''), 'expires_at': e.get('expires_at')})
            elif e['type'] == 'action_resolved' and e['team'] == self.team and (not e['ok']):
                out.append({'kind': 'action_failed', 'action': e['kind'], 'detail': e['detail']})
            elif e['type'] == 'waiver_run':
                mine = [r for r in e.get('results', []) if isinstance(r, dict) and r.get('team') == self.team]
                if mine:
                    out.append({'kind': 'waiver_results', 'results': mine})
            elif e['type'] == 'private_item' and e['team'] == self.team:
                out.append({'kind': 'owner_first_report', 'item': e['item'], 'public_at': e['public_at']})
            elif e['type'] == 'chatter' and e['team'] != self.team:
                out.append({'kind': 'chat', 'team': e['team'], 'text': e['text'][:400]})
        return out[-30:]

    def schedule_list(self) -> dict:
        from bm.agent import runtime
        return runtime.schedule_of(self.events, self.team)

    def waiver_delivery_context(self):
        if self.week < 5 or getattr(self.view, 'mode', 'observed') == 'reconstructed':
            return None
        if 'waiver_run_context' not in self._cache:
            self._cache['waiver_run_context'] = {'target_week': self.week, 'original_cutoff': faab_run_at(self.week).isoformat(), 'delivery_protocol': 'waiver-original-run/1'}
        return dict(self._cache['waiver_run_context'])

    def validate_action(self, kind: str, a: dict) -> list[str]:
        st, w = (self.state, self.view.world)
        if self.lg.finals and kind in ('propose_trade', 'respond_trade'):
            window = self.lg.finals_trade_window(self.week, self.now, self.view)
            delivery = self.lg.finals_trade_window(self.week, self.next_step(), self.view)
            if not window or not delivery or window['original_window'] != delivery['original_window']:
                return ['TRADE_WINDOW_CLOSED: finals trades must resolve within their original Tue/Fri window']
            if kind == 'respond_trade':
                thread = st['threads'].get(a.get('thread_id'))
                if thread and thread['window'] != window['original_window']:
                    return ['TRADE_WINDOW_CLOSED: thread belongs to a different finals window']
        if kind == 'file_forecast':
            from bm.agent import book
            return book.validate(a['book'], self.lg, self.team, self.week, self.view)
        if kind == 'set_lineup':
            if not isinstance(a['slots'], dict):
                return ['slots must be an object of slot -> player id']
            return [f'{v.code}: {v.detail}' for v in lineup_violations(self.rules, w, st, self.team, a['slots'], self.now)]
        if kind == 'waiver_bid':
            if self.week >= 5 and getattr(self.view, 'mode', 'observed') != 'reconstructed':
                try:
                    cutoff = datetime.fromisoformat(self.waiver_delivery_context()['original_cutoff'])
                except (ValueError, KeyError, TypeError):
                    return ['WAIVER_CONTEXT: original sealed run cutoff unmeasured']
                if self.next_step() > cutoff or self.lg.done(f'waiver_run:{self.week}') or any((e['type'] == 'waiver_run' and e.get('week') == self.week for e in self.lg.events())):
                    return ['WAIVER_CLOSED: original sealed run closed before this action can resolve']
            errs = []
            for b in a['bids']:
                if not isinstance(b, dict) or b.get('add') not in w.players or owner_of(st, b.get('add')):
                    errs.append(f"bid {b}: add must be a free agent's id")
                elif not isinstance(b.get('amount'), int) or not 0 <= b['amount'] <= st['teams'][self.team]['faab']:
                    errs.append(f'bid {b.get('add')}: amount must be whole dollars 0..{st['teams'][self.team]['faab']}')
            return errs
        if kind == 'add_drop':
            if self.lg.finals and (not (self.rules.raw.get('waivers') or {}).get('fcfs_after_run')):
                return ['FCFS_DISABLED: this finals ruleset uses sealed FAAB only']
            if a['add'] not in w.players or owner_of(st, a['add']):
                return [f'{a['add']} is not a free agent']
            if w.locked(self.week, a['add'], self.now):
                return [f'{a['add']} has kicked off']
            return []
        if kind == 'propose_trade':
            if a['to_team'] not in st['teams'] or a['to_team'] == self.team:
                return ['bad to_team']
            if st['week'] > self.rules.trades['deadline_week']:
                return ['past the trade deadline']
            errs = [f'{v.code}: {v.detail}' for v in offer_violations(self.rules, w, st, self.team, a['to_team'], a['give'], a['get'], self.now, a['faab_give'], a['faab_get'])]
            return errs + (['message too long'] if len(a['message']) > self.rules.max_msg_chars else [])
        if kind == 'respond_trade':
            t = st['threads'].get(a['thread_id'])
            if not t or self.team not in (t['a'], t['b']):
                return ['no such thread of yours']
            if t['status'] != 'open':
                return [f'thread is {t['status']}']
            if t['last_from'] == self.team:
                return ['it is not your turn in this thread']
            if a['response'] not in ('ACCEPT', 'REJECT', 'COUNTER', 'MESSAGE'):
                return ['response must be ACCEPT, REJECT, COUNTER or MESSAGE']
            if a['response'] == 'COUNTER':
                other = t['a'] if t['b'] == self.team else t['b']
                return [f'{v.code}: {v.detail}' for v in offer_violations(self.rules, w, st, self.team, other, a['give'], a['get'], self.now, a['faab_give'], a['faab_get'])]
            return []
        if kind == 'post_chat':
            n = sum((1 for e in self.events if e['type'] == 'chatter' and e['team'] == self.team and (e.get('window') == day_key(self.now))))
            if n >= self.rules.chatter_posts_per_window:
                return ['you have already posted today']
            return ['text too long'] if len(a['text']) > self.rules.max_msg_chars else []
        if kind == 'report':
            return [] if a['category'] in ('bug', 'collusion', 'rules_question', 'other') else ['category must be bug, collusion, rules_question or other']
        return []

    def rules_answer(self, text: str) -> str:
        from bm.agent.prompts_v2 import rulebook
        words = {w for w in text.lower().split() if len(w) > 3}
        hits = [l for l in rulebook(self.rules, self.lg.cfg if self.lg.finals else None).splitlines() if words & set(l.lower().replace(',', ' ').replace('.', ' ').replace(':', ' ').split())]
        return '\n'.join(hits[:6]) or 'The rulebook has nothing on that. (The rules channel answers only from the rulebook.)'
ET_FMT = '%a %b %d %H:%M'

def runtime_requirements(cfg: dict) -> list[str]:
    from bm.agent.runtime import HARNESS_VERSION
    missing = []
    if cfg.get('format', 'h2h') not in ('h2h', 'finals'):
        missing.append(f'agent format {cfg['format']!r} is unsupported')
    harnesses = sorted({s['harness'] for s in cfg.get('seats', []) if isinstance(s, dict) and 'harness' in s and (s['harness'] != HARNESS_VERSION)})
    if harnesses:
        missing.append('agent harness dispatch is missing for: ' + ', '.join(harnesses))
    return missing

def create(cfg: dict, now: datetime | None=None) -> AgentLeague:
    from bm.engine.rules import load_rules
    from bm.stats import nflverse as nv
    missing = runtime_requirements(cfg)
    if missing:
        raise ValueError(f'{cfg['id']}: runtime prerequisites unmet: ' + '; '.join(missing))
    rules = load_rules(cfg['rules_file'])
    d = LEAGUES / cfg['id']
    if (d / 'league.json').exists():
        return AgentLeague(cfg['id'])
    lc = prepare_config(cfg, rules, now)
    d.mkdir(parents=True, exist_ok=True)
    seats, order = (lc['seats'], lc['draft_order'])
    (d / 'league.json').write_text(json.dumps(lc, indent=1))
    lg = AgentLeague(cfg['id'])
    lg.log.append({'type': 'genesis', 'ts': cfg['draft_as_of'], 'league_id': cfg['id'], 'team_ids': [s['team_id'] for s in seats], 'draft_order': order, 'week': int(cfg['start_week']), 'rules_hash': rules.rules_hash, 'rules_version': rules.version, 'seats': seats, 'data': nv.manifest()[-6:], 'logged_at': now_utc().isoformat()})
    return lg

def prepare_config(cfg: dict, rules, now: datetime | None=None) -> dict:
    if cfg.get('format', 'h2h') not in ('h2h', 'finals'):
        raise ValueError(f'unsupported agent format {cfg['format']!r}')
    seed = hashlib.sha256(cfg['id'].encode()).hexdigest()[:12]
    seats = []
    for i, s in enumerate(cfg['seats']):
        s = s if isinstance(s, dict) else {'entrant': s}
        seats.append({'team_id': f'{cfg['id']}-t{i + 1:02d}', 'entrant_id': s['entrant'], 'variant': s.get('variant', 'v2.ref'), 'operator': 'house' if s.get('house') else 'entrant', 'tier': float(s.get('tier', 1.0)), 'disclosure': s.get('disclosure', 'silent'), **({'house': True} if s.get('house') else {}), **({'harness': s['harness']} if 'harness' in s else {})})
    from bm.league.finals import draft_order
    if 'draft_order' in cfg and cfg['draft_order'] is None:
        raise ValueError('draft_order must list each league team ID exactly once')
    args = ([s['team_id'] for s in seats], seed)
    order = draft_order(*args, cfg['draft_order']) if 'draft_order' in cfg else draft_order(*args)
    sched = {} if cfg.get('format') == 'finals' else round_robin([s['team_id'] for s in seats], list(range(int(cfg['start_week']), 15)), seed)
    lc = {'id': cfg['id'], 'label': cfg.get('label', cfg['id']), 'sport': 'nfl', 'mode': 'agent', 'division': cfg.get('division', 'main'), 'rules_file': cfg['rules_file'], 'rules_hash': rules.rules_hash, 'season': 2026, 'start_week': int(cfg['start_week']), 'end_week': int(cfg.get('end_week', 17)), 'format': cfg.get('format', 'h2h'), 'draft_as_of': cfg['draft_as_of'], 'seed': seed, 'seats': seats, 'draft_order': order, 'h2h_schedule': {str(w): [list(p) for p in ps] for w, ps in sched.items()}, 'info': cfg.get('info', {}), 'budget': cfg.get('budget', {}), 'market_tools': cfg.get('market_tools', True), **{key: cfg[key] for key in ('treatment_cohort', 'block', 'disclosure') if key in cfg}, **({'step_offset_min': cfg['step_offset_min']} if 'step_offset_min' in cfg else {}), 'created_at': (now or now_utc()).isoformat(), 'status': cfg.get('status', 'full-game')}
    return lc
