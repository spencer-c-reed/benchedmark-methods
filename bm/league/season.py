from __future__ import annotations
import time
import copy
import fcntl
import hashlib
import os
import json
import random
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import yaml
from bm import priority
from bm.agent import bots, prompts
from bm.agent.notes import carry_note, note_text
from bm.agent.harness import Harness, ToolBox, sha
from bm.engine.rules import Rules, load_rules
from bm.engine.scoring import score_week, standings
from bm.engine.state import STATEFUL, TIME_RULE, Violation, World, apply_event, effective_time, fold, lineup_violations, offer_violations, state_hash, current_drafter, owner_of
from bm.engine.waivers import resolve_waivers
from bm.engine.defaults import best_lineup, default_draft_pick, legal_draft_choices
from bm.gateway.core import Gateway
from bm.log import ChainLog
from bm.stats import nflverse as nv
from .context import SEASON, build_view
from .swiss import finals_view
IRREGULAR_GRACE = timedelta(hours=48)
SUSPENDED_COUNT = timedelta(days=7)
LATE_WINDOWS_FROM_WEEK = 5
LATE_LEAD = timedelta(minutes=80)
SLOT_GAP = timedelta(minutes=35)
CORRECTION_EVERY = timedelta(hours=20)
CORRECTIONS_FROM = datetime.fromisoformat('2026-10-08T00:00:00-04:00')
SEEDED_TIES_FROM_WEEK = 5
SAME_MODEL_BLOCK_FROM_WEEK = 5
RETRY_FROM_WEEK = 5
RETRY_BASE_S, RETRY_CAP_S = (30.0, 1800.0)
HELD_ALERT = timedelta(hours=1)
STEP_KINDS = ('trade', 'waiver_bids', 'waiver_run', 'lineup', 'score')
CALL_WINDOW_S = 2700
REFUSED_TERMINAL_AFTER = 2
LOCK_WAKE = timedelta(hours=3)
STALL_WAKE = timedelta(minutes=5)
ROOT = Path(__file__).resolve().parents[2]
LEAGUES = ROOT / 'leagues'
ET = ZoneInfo('America/New_York')
BOTS = bots.ALL

def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)

def iso(d: datetime) -> str:
    return d.astimezone(ET).isoformat()

def week_anchor(week: int) -> datetime:
    games = [g for g in nv.schedule(SEASON) if g['week'] == week]
    suns = [datetime.fromisoformat(g['kickoff']) for g in games if datetime.fromisoformat(g['kickoff']).weekday() == 6]
    sun = min(suns) if suns else min((datetime.fromisoformat(g['kickoff']) for g in games))
    d = sun.astimezone(ET).date() - timedelta(days=3)
    return datetime(d.year, d.month, d.day, tzinfo=ET)

def week_steps(week: int) -> list[tuple[str, datetime]]:
    t = week_anchor(week)
    sched = [g for g in nv.schedule(SEASON) if g['week'] == week]
    last_end = max((nv.game_end(g['kickoff']) for g in sched))
    steps = [(f'trade:{week}:1', t - timedelta(days=2) + timedelta(hours=14)), (f'waiver_bids:{week}', t - timedelta(days=2) + timedelta(hours=18)), (f'waiver_run:{week}', t - timedelta(days=1) + timedelta(hours=3)), (f'lineup:{week}:1', t + timedelta(hours=12)), (f'trade:{week}:2', t + timedelta(days=1, hours=12)), (f'lineup:{week}:2', t + timedelta(days=3, hours=8, minutes=30))]
    if week >= LATE_WINDOWS_FROM_WEEK:
        fixed = [d for k, d in steps if k.startswith('lineup:')]
        for i, d in enumerate(late_windows(sched, fixed)):
            steps.append((f'lineup:{week}:{i + 3}', d))
        steps.sort(key=lambda s: s[1])
    steps.append((f'score:{week}', (last_end + timedelta(hours=12)).astimezone(ET).replace(minute=0, second=0)))
    return steps

def late_windows(games: list[dict], fixed: list[datetime]) -> list[datetime]:
    kicks = sorted({datetime.fromisoformat(g['kickoff']) for g in games})
    slots: list[list[datetime]] = []
    for k in kicks:
        if slots and k - slots[-1][-1] <= SLOT_GAP:
            slots[-1].append(k)
        else:
            slots.append([k])
    out = []
    for sl in slots:
        at = sl[-1] - LATE_LEAD
        if any((abs(at - f) < timedelta(minutes=45) for f in fixed)) or at <= min(fixed):
            continue
        out.append(at)
    return out
LOCK_TIMED = ('lineup_set', 'waiver_award', 'drop', 'trade_msg')
STANDING_NOTES = {'start_sit': "the lock passed before the seat's lineup call ran; its own last lineup stands (D-REL-1a)", 'waiver_bid': "the waiver run passed before the seat's bid call was answered; the seat has no bids this week (D-REL-1a)", 'trade_offer': "the trade window closed before the seat's offer call was answered; the seat makes no offers (D-REL-1a)", 'trade_response': "the trade window closed before the seat's reply was answered; the engine sends no reply for it (D-REL-1a)"}
PROMPT_TEMPLATES = {'nfl-proto-4': 'bm.agent.prompts', 'nfl-restart-1': 'bm.agent.prompts_restart'}

def template_module(name: str | None):
    if not name:
        return prompts
    if name not in PROMPT_TEMPLATES:
        raise ValueError(f'unknown prompt_template {name!r}; known: {sorted(PROMPT_TEMPLATES)}')
    import importlib
    mod = importlib.import_module(PROMPT_TEMPLATES[name])
    if mod.TEMPLATE_VERSION != name:
        raise ValueError(f'prompt_template {name!r} resolves to {mod.TEMPLATE_VERSION!r}')
    return mod

class League:
    clock = None

    def __init__(self, league_id: str, gateway: Gateway | None=None):
        self.dir = LEAGUES / league_id
        self.cfg = json.loads((self.dir / 'league.json').read_text())
        self.rules = load_rules(self.cfg['rules_file'])
        self.log = ChainLog(self.dir / 'log.jsonl')
        self.gw = gateway
        self.entrants = {e['id']: e for e in yaml.safe_load((ROOT / 'config' / 'entrants.yaml').read_text())}
        self.seats = {s['team_id']: s for s in self.cfg['seats']}
        self._lock = threading.Lock()

    def exclusive(self):
        import contextlib, fcntl

        @contextlib.contextmanager
        def cm():
            f = open(self.dir / 'run.lock', 'w')
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                f.close()
                raise RuntimeError(f'{self.id} is being run by another process')
            try:
                self._exclusive_held = True
                self.recover_batch()
                yield
            finally:
                self._exclusive_held = False
                hung = getattr(self, '_hung', None)
                if hung is not None and hung.is_alive():
                    self._hung_lock = f
                else:
                    fcntl.flock(f, fcntl.LOCK_UN)
                    f.close()
        return cm()

    @property
    def call_window_s(self) -> int:
        return int(self.cfg.get('call_window_s') or CALL_WINDOW_S)

    @property
    def prompts(self):
        return template_module(self.cfg.get('prompt_template'))

    @property
    def id(self) -> str:
        return self.cfg['id']

    def events(self) -> list[dict]:
        return self.log.payloads()

    def entrant(self, tid: str, week: int | None=None, at: datetime | None=None) -> str:
        from bm import succession
        return succession.controller(self.seats[tid]['entrant_id'], league=self.id, week=week, at=at)

    def seat_entrant(self, tid: str) -> str:
        return self.seats[tid]['entrant_id']

    def is_bot(self, tid: str, week: int | None=None, at: datetime | None=None) -> bool:
        return (self.entrant(tid, week, at) if at is not None else self.entrant(tid, week)) in BOTS

    def standing_lineup(self) -> bool:
        return bool(self.cfg.get('standing_lineup'))

    def names(self) -> dict:
        from bm.agent.onboard import load as load_identity
        n = {t: s.get('name') or f'{self.entrants[s['entrant_id']]['short']} {t[-2:]}' for t, s in self.seats.items()}
        for e in self.events():
            if e['type'] == 'team_named':
                n[e['team']] = e['name']
        seen: dict[str, int] = {}
        for t in sorted(self.seats):
            ent = self.seats[t]['entrant_id']
            ident = load_identity(ent, self.seats[t].get('variant', 'standard'))
            if ident and ident.get('status') == 'ok' and ident.get('team_name'):
                k = seen.get(ent, 0)
                seen[ent] = k + 1
                n[t] = ident['team_name'] + (' II' if k == 1 else f' {k + 1}' if k > 1 else '')
        return n

    def view(self, week: int, as_of: datetime):
        return build_view(self.rules, week, as_of)

    def state(self, world: World | None=None) -> dict:
        return self.fold_events(self.events())

    def done(self, key: str) -> bool:
        if 'events' in vars(self):
            return any((e['type'] == 'step_done' and e['step'] == key for e in self.events()))
        return key in self._done_steps()

    def _done_steps(self) -> frozenset:
        try:
            st = self.log.path.stat()
            sig = (st.st_ino, st.st_size, st.st_mtime_ns)
        except OSError:
            sig = None
        cached = getattr(self, '_done_cache', None)
        if sig is not None and cached is not None and (cached[0] == sig):
            return cached[1]
        steps = frozenset((e['step'] for e in self.events() if e['type'] == 'step_done'))
        if sig is not None:
            self._done_cache = (sig, steps)
        return steps

    def decision(self, decision_id: str) -> dict | None:
        for e in self.events():
            if e['type'] == 'decision' and e['decision_id'] == decision_id:
                return e
        return None

    def now(self, as_of: datetime) -> datetime:
        clock = getattr(self, 'clock', None)
        return max(as_of, clock().replace(microsecond=0)) if clock else as_of

    def live_view(self, view):
        t = self.now(view.as_of)
        if t == view.as_of:
            return view
        v = copy.copy(view)
        v.as_of = t
        return v

    def _scheduled_at(self, step: str) -> str | None:
        parts = step.split(':')
        if len(parts) < 2 or not parts[1].isdigit():
            return None
        off = timedelta(minutes=int(self.cfg.get('step_offset_min') or 0))
        due = next((d for k, d in week_steps(int(parts[1])) if k == step), None) if parts[0] in STEP_KINDS else None
        return iso(due + off) if due else None

    def append(self, ev: dict, world: World | None=None, state: dict | None=None) -> dict:
        ev = self._stamp(ev)
        with self._lock:
            if world is not None and state is not None:
                from .context import engine_world
                apply_event(self.rules, engine_world(), state, ev)
            self.log.append(ev)
        return ev

    def _stamp(self, ev: dict) -> dict:
        ev = dict(ev)
        clock = getattr(self, 'clock', None)
        ev.setdefault('logged_at', clock().isoformat() if clock else now_utc().isoformat())
        if clock and ev.get('type') in STATEFUL:
            ev['time_rule'] = TIME_RULE
        if clock and ev.get('ts'):
            if ev.get('type') in STATEFUL:
                stamped = datetime.fromisoformat(ev['ts'])
                at = self.now(stamped)
                if at > stamped:
                    ev['scheduled_ts'], ev['ts'] = (ev['ts'], iso(at))
            elif ev.get('type') == 'step_done' and 'scheduled_at' not in ev:
                sched = self._scheduled_at(str(ev.get('step') or ''))
                if sched:
                    ev['scheduled_at'] = sched
        return ev

    def _journal_path(self) -> Path:
        return self.dir / 'step-journal.json'

    def append_batch(self, evs: list[dict]) -> list[dict]:
        self.recover_batch()
        from .context import engine_world
        stamped = [self._stamp(e) for e in evs]
        with self._lock:
            st = self.state()
            for e in stamped:
                if e.get('type') in STATEFUL:
                    st = apply_event(self.rules, engine_world(), st, e)
            _, head = self.log.head()
            self._write_journal({'head': head, 'events': stamped})
            self.log.append_many(stamped)
            self._journal_path().unlink(missing_ok=True)
        return stamped

    def _write_journal(self, body: dict) -> None:
        tmp = self._journal_path().with_suffix(f'.{os.getpid()}.tmp')
        with open(tmp, 'w') as f:
            f.write(json.dumps(body, default=str))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._journal_path())
        fd = os.open(self.dir, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def recover_batch(self) -> int:
        path = self._journal_path()
        if not path.exists():
            return 0
        from bm.log import canonical
        body = json.loads(path.read_text())
        rows = list(self.log.rows())
        idx = next((i for i, r in enumerate(rows) if r['hash'] == body['head']), None)
        if idx is None and body['head'] != '0' * 64:
            raise RuntimeError(f'{self.id}: step journal head not in the log; refusing to recover')
        after = [r['payload'] for r in rows[idx + 1 if idx is not None else 0:]]
        want = body['events']
        if len(after) > len(want) or any((canonical(a) != canonical(b) for a, b in zip(after, want))):
            raise RuntimeError(f"{self.id}: rows after the step journal's head are not a prefix of its batch; refusing")
        rest = want[len(after):]
        self.log.append_many(rest)
        path.unlink()
        return len(rest)

    def memory(self, tid: str, entrant: str | None=None) -> str:
        m = ''
        evs = self.events()
        by = {e['decision_id']: e.get('entrant') for e in evs if e['type'] == 'decision'} if entrant else {}
        for e in evs:
            if e['type'] == 'memory' and e['team'] == tid and (not entrant or by.get(e.get('decision_id')) == entrant):
                m = e['text']
        return m

    def roster_block(self, view, state, tid: str, with_lock: bool=False) -> str:
        t = state['teams'][tid]
        inv = {p: s for s, p in t['lineup'].items() if p}
        rows = []
        for pid in sorted(t['roster'], key=lambda p: (['QB', 'RB', 'WR', 'TE', 'K', 'DEF'].index(view.world.pos(p)), -view.proj.get(p, {}).get('value', 0))):
            rows.append(f'  {inv.get(pid, 'BN'):5} {view.ptoken(pid, with_lock)}')
        empty = [s for s, p in t['lineup'].items() if p is None]
        return '\n'.join(rows) + (f'\n  EMPTY starting slots: {', '.join(empty)}' if empty and state['phase'] == 'SEASON' else '')

    def swiss_view(self, state=None, as_of: datetime | None=None) -> dict | None:
        if self.rules.raw.get('season', {}).get('standings') != 'swiss':
            return None
        from bm.engine.state import STATEFUL
        events, stateful = ([], 0)
        for event in self.events():
            if event['type'] in STATEFUL:
                stateful += 1
                if state is not None and stateful > state['seq']:
                    break
            if as_of is not None and event['type'] in ('week_scored', 'swiss_round_scheduled'):
                times = [event.get(k) for k in ('ts', 'logged_at')]
                if any((not t for t in times)):
                    continue
                try:
                    if any((datetime.fromisoformat(t) > as_of for t in times)):
                        continue
                except (ValueError, TypeError):
                    continue
            events.append(event)
        return finals_view(self.cfg, self.rules, events)

    def prepare_swiss(self, now: datetime, week: int | None=None) -> None:
        view = self.swiss_view()
        if view is None:
            return
        if not getattr(self, '_exclusive_held', False):
            raise RuntimeError('Swiss scheduling requires the league exclusive lock')
        while view['proposed_round'] is not None:
            self.append({**view['proposed_round'], 'ts': iso(now)})
            view = self.swiss_view()
        if week is not None and (not any((r['week'] == week for r in view['rounds']))):
            raise RuntimeError(f'Swiss week {week} is not ready: preceding games unresolved')

    def competition_standings(self, state, week: int | None=None, as_of: datetime | None=None) -> list[dict]:
        swiss = self.swiss_view(state, as_of)
        if swiss is not None:
            return swiss['standings']
        return standings(list(state['teams']), state['scores'], self._tie_seed(state['week'] if week is None else week))

    def swiss_opponent(self, tid: str, week: int, state=None, as_of: datetime | None=None) -> str | None:
        swiss = self.swiss_view(state, as_of)
        if swiss is not None:
            for round_ in swiss['rounds']:
                if round_['week'] == week:
                    for a, b in round_['pairings']:
                        if tid in (a, b):
                            return b if tid == a else a
        return None

    def standings_block(self, state, as_of: datetime | None=None) -> str:
        st = self.competition_standings(state, as_of=as_of)
        names = self.names()
        if self.swiss_view(state, as_of) is not None:
            return '\n'.join((f'  {r['rank']:>2}. {r['team_id']} {names[r['team_id']]}: {r['w']}-{r['l']}-{r['t']} W-L-T, {r['points']:.1f} pts, all-play {r['all_play_w']}-{r['all_play_l']}' for r in st))
        if not state['scores']:
            return '  (no weeks scored yet)'
        last = max(state['scores'], key=int)
        return '\n'.join((f'  {r['rank']:>2}. {r['team_id']} {names[r['team_id']]}: {r['points']:.1f} pts (week {last}: {state['scores'][last].get(r['team_id'], 0):.1f}), all-play {r['all_play_w']}-{r['all_play_l']}' for r in st))

    def common_brief(self, view, state, tid: str) -> str:
        names = self.names()
        teams = '  ' + '; '.join((f'{t} {names[t]}' for t in sorted(state['teams'])))
        ctl = self.entrant(tid, view.week)
        succeeded = ctl != self.seat_entrant(tid)
        mem = self.memory(tid, ctl) if succeeded else self.memory(tid)
        tpl = self.prompts
        notes = [e for e in self.events() if e['type'] == 'decision' and e['team'] == tid and note_text(e) and (e.get('kind') != 'forecast_book') and (not succeeded or e.get('entrant') == ctl)][-3:]
        chat = [e for e in self.events() if e['type'] == 'chatter'][-6:]
        parts = [f'NOW: {view.as_of.astimezone(ET).strftime('%a %b %d %Y %H:%M')} ET, NFL week {view.week}.' + (' (Data visibility for this past-dated decision is reconstructed from game times and report times.)' if view.mode == 'reconstructed' else ''), f'TEAMS:\n{teams}', f'STANDINGS:\n{self.standings_block(state, view.as_of)}', f'YOUR ROSTER ({tid}, FAAB ${state['teams'][tid]['faab']}):\n{self.roster_block(view, state, tid, with_lock=True)}']
        if self.swiss_view(state, view.as_of) is not None:
            other = self.swiss_opponent(tid, view.week, state, view.as_of)
            parts.append(f'SWISS MATCHUP (NFL week {view.week}): ' + (f'{other} {names[other]}' if other else 'not scheduled; preceding games unresolved'))
        if mem:
            parts.append(f'{getattr(tpl, 'CARRY_LABEL', 'YOUR MEMORY FILE')}:\n{mem[:3000]}')
        if notes:
            parts.append(f'{getattr(tpl, 'MEMO_LABEL', 'YOUR RECENT PRIVATE NOTES')}:\n' + '\n'.join((f'  [{n['kind']} w{n['week']}] {note_text(n)[:400]}' for n in notes)))
        if chat:
            rows = [f'  {c['team']} {names.get(c['team'], '')}: {c['text'][:300]}' for c in chat]
            fence = getattr(tpl, 'fence', None)
            parts.append('LEAGUE CHAT (latest):\n' + (fence('LEAGUE CHAT', rows) if fence else '\n'.join(rows)))
        return '\n\n'.join(parts)

    def run_decision(self, *, view, state, tid: str, kind: str, decision_id: str, briefing: str, task: str, validate, default_action: dict, window: str, unit: str, question: str, window_s: int | None=None, hold_until: datetime | None=None, retry: dict | None=None, pause_on_infra: bool=False, request_deadline: datetime | None=None) -> dict | None:
        prior = self.decision(decision_id)
        if prior:
            return prior
        if self.is_bot(tid, view.week):
            return None
        if self._refused_terminal(decision_id):
            return None
        ent = self.entrant(tid, view.week)
        tpl = self.prompts
        system = tpl.system_prompt(self.rules, self.cfg, {'team_id': tid, 'name': self.names()[tid]})
        tool_names, tool_events = (self.names(), self.events())
        if kind in ('trade_offer', 'trade_response') and view.mode != 'reconstructed' and (view.week >= RETRY_FROM_WEEK):
            from . import trade_context as tc
            packet = tc.read(self, 'trade_decision_context', decision_id)
            if packet is None:
                packet = tc.write(self, 'trade_decision_context', decision_id, dict(system=system, briefing=briefing, task=task, state=state, names=tool_names, events=tool_events, entrant=ent, template_hash=tpl.template_hash(), request_deadline=request_deadline.isoformat() if request_deadline else None))
            if packet['entrant'] != ent or packet['template_hash'] != tpl.template_hash():
                raise ValueError('trade original identity/prompt implementation changed')
            system, briefing, task, state = (packet['system'], packet['briefing'], packet['task'], packet['state'])
            tool_names, tool_events = (packet['names'], packet['events'])
            if packet.get('request_deadline'):
                request_deadline = datetime.fromisoformat(packet['request_deadline'])
        tools = ToolBox(view, state, tool_names, tool_events)
        res = self._harness().decide(entrant_id=ent, decision_id=decision_id, system=system, briefing=briefing, task=task, tools=tools, validate=validate, window_s=window_s or self.call_window_s, hard_deadline=request_deadline or hold_until)
        if (pause_on_infra or view.week >= RETRY_FROM_WEEK) and res.forced_default and res.calls and res.calls[-1].get('error'):
            from bm import records
            if res.calls[-1]['error'] in records.INFRA:
                refused = res.calls[-1]['error'] == 'REFUSED' and self._refused_asks(decision_id) >= REFUSED_TERMINAL_AFTER
                if not refused:
                    self._held(decision_id, tid, kind, view, hold_until, retry, res)
                    return None
                if kind != 'draft_pick':
                    self._refuse(decision_id, tid, kind, view, res)
                    return None
        p = res.parsed or {}
        note_field, carry_field = (getattr(tpl, 'NOTE_FIELD', 'private_notes'), getattr(tpl, 'CARRY_FIELD', 'memory'))
        action = res.action if res.action is not None else default_action
        usage = {'calls': len(res.calls), 'input': sum(((c['usage'] or {}).get('input', 0) or 0 for c in res.calls)), 'cached_input': sum(((c['usage'] or {}).get('cached_input', 0) or 0 for c in res.calls)), 'output': sum(((c['usage'] or {}).get('output', 0) or 0 for c in res.calls)), 'reasoning': sum(((c['usage'] or {}).get('reasoning', 0) or 0 for c in res.calls)), 'usd_eq': round(sum(((c['cost'] or {}).get('usd_eq') or 0 for c in res.calls)), 5)}
        rec = {'type': 'decision', 'ts': iso(view.as_of), 'decision_id': decision_id, 'league': self.id, 'team': tid, 'entrant': ent, 'kind': kind, 'week': view.week, 'window': window, 'state_hash': state_hash(state), 'briefing_hash': sha(briefing), 'prompt_template_hash': tpl.template_hash(), 'rendered_prompt_hash': sha(system + briefing + task), 'data_refs': view.data_refs, 'visibility': view.mode, 'question': str(p.get('question') or question)[:300], 'options': p.get('options') if isinstance(p.get('options'), list) else [], 'chosen': p.get('chosen'), 'unit': p.get('unit') or unit, 'statement_public': p.get('statement_public'), note_field: str(p.get(note_field) or '')[:6000], 'est_value': p.get('est_value'), 'forecast': p.get('forecast') if isinstance(p.get('forecast'), dict) else None, 'provider_reasoning': [{k: v for k, v in r.items()} for r in res.reasoning], 'research': res.research, 'action': action, 'chatter': p.get('chatter'), 'team_name': p.get('team_name'), 'validation': {'violations': res.violations[:10], 'retries': res.retries}, 'forced_default': {'cause': res.forced_default} if res.forced_default else None, 'model_call_ids': [c['request_id'] for c in res.calls], 'usage': usage}
        rec = self.append(rec)
        self._session_record(rec, res.calls)
        if isinstance(p.get(carry_field), str) and p[carry_field].strip():
            self.append({'type': 'memory', 'ts': iso(view.as_of), 'team': tid, 'text': p[carry_field][:3000], 'sha256': sha(p[carry_field][:3000]), 'decision_id': decision_id})
        return rec

    def _refusals(self, decision_id: str) -> int:
        return sum((1 for e in self.events() if e['type'] in ('decision_pending', 'decision_skipped_infra') and e.get('decision_id') == decision_id and (e.get('cause') == 'REFUSED')))

    def _refused_asks(self, decision_id: str) -> int:
        return max(self._refusals(decision_id) + 1, _gateway_refusals(decision_id))

    def _refused_terminal(self, decision_id: str) -> bool:
        return any((e['type'] == 'decision_missed' and e.get('decision_id') == decision_id and (e.get('outcome') == 'refused') for e in self.events()))

    def _refuse(self, decision_id: str, tid: str, kind: str, view, res) -> None:
        err = res.calls[-1]
        self.append({'type': 'decision_missed', 'ts': iso(self.now(view.as_of)), 'logged_at': now_utc().isoformat(), 'decision_id': decision_id, 'team': tid, 'entrant': self.entrant(tid, view.week), 'kind': kind, 'week': view.week, 'cause': 'REFUSED', 'outcome': 'refused', 'refusals': self._refused_asks(decision_id), 'lane': err.get('lane'), 'detail': (err.get('error_detail') or '')[:200], 'model_call_ids': [c['request_id'] for c in res.calls], 'note': f"the provider refused this decision {REFUSED_TERMINAL_AFTER} times; terminal: the seat's own last decision stands; excluded from capability scores, counted as a refusal"})

    def _harness(self):
        h = Harness(self.gw)
        h.journal = self.answers()
        return h

    def answers(self):
        from bm.agent.harness import AnswerJournal
        return AnswerJournal(self.dir / 'answers.jsonl')

    def _held(self, decision_id: str, tid: str, kind: str, view, hold_until, retry, res) -> None:
        now = now_utc()
        n = sum((1 for e in self.events() if e['type'] == 'decision_pending' and e['decision_id'] == decision_id))
        err = res.calls[-1]
        ev = {'type': 'decision_pending' if hold_until else 'decision_skipped_infra', 'ts': iso(view.as_of), 'logged_at': now.isoformat(), 'decision_id': decision_id, 'team': tid, 'entrant': self.entrant(tid, view.week), 'kind': kind, 'week': view.week, 'cause': err.get('error'), 'lane': err.get('lane'), 'detail': (err.get('error_detail') or '')[:200], 'attempt': n + 1, 'model_call_ids': [c['request_id'] for c in res.calls], 'note': "infrastructure failure: the seat's standing decisions stand (D-REL-1a)"}
        if hold_until:
            ev.update(hold_until=hold_until.isoformat(), retry=retry, next_try_at=(now + timedelta(seconds=min(RETRY_BASE_S * 2 ** n, RETRY_CAP_S))).isoformat())
        self.append(ev)
        self._last_held = ev

    def retry_pending(self, now: datetime) -> list[str]:
        ran, due = self.pending_due(now)
        for p in due:
            self.retry_one(p, now)
            ran.append(f'retry:{p['decision_id']}')
        return ran

    def retry_one(self, p: dict, now: datetime) -> None:
        if now >= datetime.fromisoformat(p['hold_until']):
            return
        r = p.get('retry') or {}
        if r.get('kind') == 'lineup':
            self._lineup_decision(r['week'], r['n'], p['team'], now, hard_deadline=datetime.fromisoformat(p['hold_until']), did=r.get('decision_id'))
        elif r.get('kind') == 'waiver_bid':
            self._waiver_decision(r['week'], p['team'], now, hard_deadline=datetime.fromisoformat(p['hold_until']))

    def reopen_infra_lineups(self, now: datetime, min_left_s: int=600) -> list[str]:
        from bm import records
        evs = self.events()
        voided = {e['decision_id'] for e in evs if e['type'] == 'decision_voided'}
        expected = {e['decision_id']: e for e in evs if e['type'] == 'decision_expected'}
        out = []
        for e in evs:
            if e['type'] != 'decision' or e.get('kind') != 'start_sit' or e['decision_id'] in voided:
                continue
            cause = (e.get('forced_default') or {}).get('cause')
            gateway = any((str(v).startswith('gateway:') for v in (e.get('validation') or {}).get('violations') or []))
            x = expected.get(e['decision_id']) or {}
            r = x.get('retry') or {}
            if cause not in records.INFRA or not gateway or r.get('kind') != 'lineup' or (not x.get('hold_until')):
                continue
            hold = datetime.fromisoformat(x['hold_until'])
            if (hold - now).total_seconds() < min_left_s:
                continue
            stem = f'{self.id}:w{r['week']}:lineup{r['n']}'
            k = 1 + sum((1 for d in voided if d.startswith(stem) and d.endswith(f':{e['team']}')))
            new = f'{self.id}:w{r['week']}:lineup{r['n']}r{k}:{e['team']}'
            self.append({'type': 'decision_voided', 'ts': iso(now), 'logged_at': now_utc().isoformat(), 'decision_id': e['decision_id'], 'voided_seq': e.get('seq'), 'team': e['team'], 'cause': cause, 'replacement': new, 'note': 'gateway-caused forced default before its lock; re-asked under the replacement id, original retained'})
            self.append({'type': 'decision_expected', 'ts': iso(now), 'logged_at': now_utc().isoformat(), 'decision_id': new, 'team': e['team'], 'entrant': self.entrant(e['team'], r['week']), 'kind': 'start_sit', 'week': r['week'], 'hold_until': x['hold_until'], 'retry': dict(r, decision_id=new), 'reopens': e['decision_id'], 'eligibility': 'reopened: gateway-caused forced default before its lock'})
            voided.add(e['decision_id'])
            out.append(new)
        return out

    def expected_missing(self, now: datetime) -> list[dict]:
        evs = self.events()
        handled = {e.get('decision_id') for e in evs if e['type'] in ('decision', 'decision_pending', 'decision_missed')}
        return [e for e in evs if e['type'] == 'decision_expected' and (e.get('retry') or {}).get('kind') in ('lineup', 'waiver_bid') and (e['decision_id'] not in handled) and ((datetime.fromisoformat(e['hold_until']) - now).total_seconds() >= (60 if e['kind'] == 'start_sit' else 0)) and (datetime.fromisoformat(e['hold_until']) > now)]

    def _expect(self, did: str, tid: str, kind: str, view, deadline, retry: dict) -> None:
        if not deadline or view.mode == 'reconstructed' or view.week < RETRY_FROM_WEEK:
            return
        if any((e.get('decision_id') == did and e['type'] in ('decision_expected', 'decision') for e in self.events())):
            return
        self.append({'type': 'decision_expected', 'ts': iso(view.as_of), 'logged_at': now_utc().isoformat(), 'decision_id': did, 'team': tid, 'entrant': self.entrant(tid, view.week), 'kind': kind, 'week': view.week, 'hold_until': deadline.isoformat(), 'retry': retry, 'eligibility': 'ordinary window selected this seat before request dispatch'})

    def pending_due(self, now: datetime) -> tuple[list[str], list[dict]]:
        evs = self.events()
        decided = {e['decision_id'] for e in evs if e['type'] in ('decision', 'decision_missed')}
        last: dict[str, dict] = {}
        first: dict[str, str] = {}
        for e in evs:
            if e['type'] == 'decision_pending':
                last[e['decision_id']] = e
                first.setdefault(e['decision_id'], e['logged_at'])
        ran, due = ([], [])
        if self.standing_lineup():
            handled = decided | set(last)
            skipped: dict[str, int] = {}
            for e in evs:
                if e['type'] == 'decision_skipped_infra':
                    skipped[e['decision_id']] = skipped.get(e['decision_id'], 0) + 1
            for e in evs:
                if e['type'] == 'decision_expected' and e.get('kind') in STANDING_NOTES and (e['decision_id'] not in handled) and (now >= datetime.fromisoformat(e['hold_until'])):
                    handled.add(e['decision_id'])
                    self.append({'type': 'decision_missed', 'ts': iso(now), 'decision_id': e['decision_id'], 'team': e['team'], 'kind': e['kind'], 'week': e['week'], 'cause': 'lane_failed', 'attempts': skipped.get(e['decision_id'], 0), 'note': STANDING_NOTES[e['kind']]})
                    ran.append(f'missed:{e['decision_id']}')
        for did, p in sorted(last.items()):
            if did in decided:
                continue
            if now >= datetime.fromisoformat(p['hold_until']):
                self.append({'type': 'decision_missed', 'ts': iso(now), 'decision_id': did, 'team': p['team'], 'kind': p['kind'], 'week': p['week'], 'cause': 'lane_failed', 'attempts': p['attempt'], 'first_failed_at': first[did], 'note': "the lock passed while our routes failed; the seat's own last decision stands (D-REL-1a rule 4)"})
                ran.append(f'missed:{did}')
                continue
            if now - datetime.fromisoformat(first[did]) >= HELD_ALERT and (not any((e['type'] == 'held_alert' and e['decision_id'] == did for e in evs))):
                from bm.ops.alert import alert
                alert('seat-held', f'BenchedMark: {p['entrant']} in {self.id} has failed on our routes for over an hour ({p['cause']}); retrying until {p['hold_until']}.')
                self.append({'type': 'held_alert', 'ts': iso(now), 'decision_id': did})
            run = [e for e in evs if e['type'] == 'decision_pending' and e['decision_id'] == did]
            sig = (p.get('lane'), p.get('cause'))
            start = p['logged_at']
            for e in reversed(run):
                if (e.get('lane'), e.get('cause')) != sig:
                    break
                start = e['logged_at']
            if now - datetime.fromisoformat(start) >= STALL_WAKE and datetime.fromisoformat(p['hold_until']) - now <= LOCK_WAKE and (not any((e['type'] == 'stall_wake' and e['decision_id'] == did and (e.get('sig') == list(sig)) for e in evs))):
                self._stall_wake(did, p['team'], p, (now - datetime.fromisoformat(start)).total_seconds(), f'held before its lock ({p['hold_until']})')
                self.append({'type': 'stall_wake', 'ts': iso(now), 'decision_id': did, 'sig': list(sig)})
            if now < datetime.fromisoformat(p['next_try_at']):
                continue
            due.append(p)
        return (ran, due)

    def _draft_paused(self, dec_id: str, tid: str, pick: int, tries: int, stall: tuple | None) -> tuple:
        held = getattr(self, '_last_held', None) or {}
        sig = (held.get('lane'), held.get('cause'))
        if stall is None or tuple(stall[:2]) != sig:
            stall = (*sig, time.time())
        print(f'{self.id} pick {pick + 1} {tid} ({self.entrant(tid)}): lane failure #{tries} ({sig[0]}: {sig[1]}), draft paused, retrying', flush=True)
        if sig[1] == 'SELF_CAP':
            self._raise_pacing_caps()
        if time.time() - stall[2] >= STALL_WAKE.total_seconds():
            self._stall_wake(dec_id, tid, held, time.time() - stall[2], 'draft paused')
        return stall

    def _draft_default_page(self, dec_id: str, tid: str, pick: int, d: dict) -> None:
        from bm import records
        cause = (d.get('forced_default') or {}).get('cause')
        gateway = any((str(v).startswith('gateway:') for v in (d.get('validation') or {}).get('violations') or []))
        if cause not in records.INFRA and (not gateway):
            return
        msg = f'BenchedMark DRAFT INFRA DEFAULT: {self.id} pick {pick + 1} {tid} ({self.entrant(tid)}) was forced to a default by our infrastructure ({cause}{(', gateway' if gateway else '')}), not by the model. Decision {dec_id}. The pick is final; diagnose the lane now before more picks hit it.'
        try:
            from bm.ops.alert import alert, wake
            alert('draft-infra-default', msg)
            wake(f'draft-infra-default|{dec_id}', msg)
        except Exception as e:
            print(f'{self.id} draft default page failed: {e}', flush=True)

    def _raise_pacing_caps(self) -> None:
        try:
            from bm.ops import capacity
            rep = capacity.preflight(capacity.draft_work([self.id]), f'{self.id} draft (SELF_CAP mid-draft)', gw=self.gw)
            if rep.raised:
                print(rep.summary(), flush=True)
        except Exception as e:
            print(f'{self.id} capacity re-check failed: {e}', flush=True)

    def _stall_wake(self, decision_id: str, tid: str, held: dict, secs: float, what: str) -> None:
        from bm.ops.alert import wake
        msg = f'BenchedMark STALL: {self.id} {decision_id} ({self.entrant(tid)}) {what} {secs / 60:.0f} min on the same lane error. Lane {held.get('lane')}: {held.get('cause')} {held.get('detail') or ''}'.strip() + ('. This is OUR cap in config/lanes.yaml (SELF_CAP): pacing caps are raised by the capacity preflight, money caps need the operator; edits to lanes.yaml apply without a restart.' if held.get('cause') == 'SELF_CAP' else '') + ' Fix the lane; the decision retries on its own.'
        wake(f'{decision_id}|{held.get('lane')}|{held.get('cause')}', msg)

    def _hold_lineup(self, view, state, tid: str):
        now = self.now(view.as_of)
        ks = [view.world.kickoff(view.week, p) for p in state['teams'][tid]['roster']]
        ks = [k for k in ks if k and k > now]
        return min(ks) if ks else None

    def _session_record(self, dec: dict, calls: list[dict]) -> None:
        try:
            from bm import records
            from bm.agent.harness import HARNESS_VERSION
            idx = {c['request_id']: [dict(c, model_reported=c.get('model_reported'))] for c in calls}
            hv = 'windowed-2' if (dec.get('week') or 0) >= RETRY_FROM_WEEK else HARNESS_VERSION
            rec = records.from_decision(dec, self.cfg, records.CallsIndex(idx), harness_version=hv)
            records.write(rec, records.store(self.id))
        except Exception as e:
            print(f'[session record] {dec.get('decision_id')}: {e}', flush=True)

    def _book_retry_at(self, week: int, tid: str, evs: list[dict]) -> datetime | None:
        did = f'{self.id}:w{week}:book:{tid}'
        skips = [e for e in evs if e['type'] == 'decision_skipped_infra' and e.get('decision_id') == did]
        if not skips:
            return None
        return datetime.fromisoformat(skips[-1]['logged_at']) + timedelta(seconds=min(RETRY_BASE_S * 2 ** (len(skips) - 1), RETRY_CAP_S))

    def book_backoff_until(self, week: int, now: datetime) -> datetime | None:
        first_kick = min((datetime.fromisoformat(g['kickoff']) for g in nv.schedule(SEASON) if g['week'] == week))
        if (first_kick - now).total_seconds() < 120:
            return None
        evs, waits = (self.events(), [])
        for tid in [t for t in sorted(self.seats) if not self.is_bot(t, week)]:
            did = f'{self.id}:w{week}:book:{tid}'
            if self.decision(did) or self._refused_terminal(did):
                continue
            at = self._book_retry_at(week, tid, evs)
            if at is None or now >= at:
                return None
            waits.append(at)
        return min(waits) if waits else None

    def observation_book(self, week: int, as_of: datetime) -> None:
        from bm.agent import book as bk
        key = f'book:{week}'
        if self.done(key):
            return
        view = self.view(week, as_of)
        state = self.state(view.world)
        first_kick = min((datetime.fromisoformat(g['kickoff']) for g in nv.schedule(SEASON) if g['week'] == week))
        tpl = self.prompts
        note_field = getattr(tpl, 'NOTE_FIELD', 'private_notes')

        def one(tid):
            did = f'{self.id}:w{week}:book:{tid}'
            if self.decision(did) or self._refused_terminal(did):
                return
            retry_at = self._book_retry_at(week, tid, self.events())
            if retry_at and now_utc() < retry_at and ((first_kick - now_utc()).total_seconds() >= 120):
                return
            roster = state['teams'][tid]['roster']
            briefing = self.common_brief(view, state, tid)
            task = tpl.book_task(week, roster, view) if hasattr(tpl, 'book_task') else bk.task_text(week, roster, view) + '\nThis book is for the research record only: it does not change your team, and it is never shown back to you or anyone in the league.\nReply with ONE JSON object: {"action": {"kind": "forecast_book", "book": {...}}, "private_notes": "..."}'

            def validate(a):
                if a.get('kind') != 'forecast_book' or not isinstance(a.get('book'), dict):
                    return ['action must be {"kind":"forecast_book","book":{...}}']
                return bk.validate(a['book'], self, tid, week, view)
            ent = self.entrant(tid, week)
            system = tpl.system_prompt(self.rules, self.cfg, {'team_id': tid, 'name': self.names()[tid]})
            left = int((first_kick - now_utc()).total_seconds())
            if left < 120:
                if any((e['type'] == 'decision_expected' and e.get('decision_id') == did for e in self.events())) and (not any((e['type'] == 'decision_missed' and e.get('decision_id') == did for e in self.events()))):
                    self.append({'type': 'decision_missed', 'ts': iso(now_utc()), 'decision_id': did, 'team': tid, 'kind': 'forecast_book', 'week': week, 'hold_until': first_kick.isoformat(), 'cause': 'lane_failed', 'note': "the week's first kickoff was under two minutes away before the book call ran; no book"})
                return
            res = self._harness().decide(entrant_id=ent, decision_id=did, system=system, briefing=briefing, task=task, tools=ToolBox(view, state, self.names(), self.events()), validate=validate, max_output_tokens=16000, allow_research=False, window_s=min(self.call_window_s, left - 60))
            from bm import records
            if week >= RETRY_FROM_WEEK and res.forced_default and res.calls and (res.calls[-1].get('error') in records.INFRA):
                if res.calls[-1]['error'] == 'REFUSED' and self._refused_asks(did) >= REFUSED_TERMINAL_AFTER:
                    self._refuse(did, tid, 'forecast_book', view, res)
                    return
                self._held(did, tid, 'forecast_book', view, None, None, res)
                return
            if now_utc() >= first_kick:
                self.append({'type': 'book_late', 'ts': iso(now_utc()), 'team': tid, 'decision_id': did, 'week': week})
                return
            p = res.parsed or {}
            rec = {'type': 'decision', 'ts': iso(view.as_of), 'decision_id': did, 'league': self.id, 'team': tid, 'entrant': ent, 'kind': 'forecast_book', 'week': week, 'window': f'w{week}-book', 'state_hash': state_hash(state), 'briefing_hash': sha(briefing), 'prompt_template_hash': tpl.template_hash(), 'rendered_prompt_hash': sha(system + briefing + task), 'data_refs': view.data_refs, 'visibility': view.mode, 'question': f'Week {week} forecast book', 'options': [], 'chosen': None, 'unit': 'book', 'statement_public': None, note_field: str(p.get(note_field) or '')[:2000], 'est_value': None, 'forecast': None, 'provider_reasoning': res.reasoning, 'research': [], 'action': res.action, 'chatter': None, 'team_name': None, 'validation': {'violations': res.violations[:10], 'retries': res.retries}, 'forced_default': {'cause': res.forced_default} if res.forced_default else None, 'model_call_ids': [c['request_id'] for c in res.calls], 'usage': {'calls': len(res.calls), **{k: sum(((c['usage'] or {}).get(k, 0) or 0 for c in res.calls)) for k in ('input', 'cached_input', 'output', 'reasoning')}}}
            rec = self.append(rec)
            self._session_record(rec, res.calls)
            if res.action:
                self.append(bk.filed_event(tid, week, res.action['book'], 1, iso(now_utc()), roster, None, self.id) | {'observation_only': True})
        seats = [t for t in sorted(self.seats) if not self.is_bot(t, week)]
        for tid in seats:
            self._expect(f'{self.id}:w{week}:book:{tid}', tid, 'forecast_book', view, first_kick, {'kind': 'book', 'week': week})
        _parallel(one, seats)
        if all((self.decision(f'{self.id}:w{week}:book:{t}') or self._refused_terminal(f'{self.id}:w{week}:book:{t}') for t in seats)):
            self.append({'type': 'step_done', 'ts': iso(as_of), 'step': key})

    def _publish_draft_feed(self) -> None:
        try:
            from bm.publish.draft import publish
            publish(self.dir)
        except Exception as e:
            try:
                from bm.ops.alert import alert
                alert('draft-feed', f'BenchedMark {self.id} draft feed export failed: {type(e).__name__}')
            except Exception:
                pass
            print(f'{self.id} draft feed export failed: {type(e).__name__}', flush=True)

    def _draft_activity(self, status: str, tid: str, pick: int, **fields) -> None:
        self.append({'type': 'draft_activity', 'ts': iso(now_utc()), 'status': status, 'team': tid, 'pick_no': pick + 1, **fields})
        self._publish_draft_feed()

    def _draft_cutoff(self, view):
        if view.week < RETRY_FROM_WEEK or view.mode == 'reconstructed':
            return None
        kicks = [datetime.fromisoformat(k) for k in view.world.kickoffs.get(view.week, {}).values() if datetime.fromisoformat(k) >= view.as_of]
        if not kicks:
            raise ValueError('prospective draft first lock unmeasured; no request authorized')
        return min(kicks)

    def _draft_closed(self, deadline, decision_id=None):
        if deadline is None or now_utc() < deadline:
            return False
        if not any((e['type'] == 'draft_deadline_gap' and e.get('decision_id') == decision_id for e in self.events())):
            self.append(dict(type='draft_deadline_gap', ts=iso(now_utc()), deadline=deadline.isoformat(), decision_id=decision_id, cause='original first lineup lock passed; no late draft pick'))
        return True

    def run_draft(self, as_of: datetime) -> None:
        if self.done('draft'):
            return
        week = self.cfg['start_week']
        self.prepare_swiss(as_of, week)
        view = self.view(week, as_of)
        deadline = self._draft_cutoff(view)
        if self._draft_closed(deadline):
            return
        queues: dict[str, list[str]] = {}
        for e in self.events():
            if e['type'] == 'decision' and e['kind'] == 'draft_pick' and isinstance(e.get('action'), dict):
                queues[e['team']] = list(e['action'].get('queue') or [])
        total = self.rules.roster_size * len(self.seats)
        while True:
            state = self.state(view.world)
            tid = current_drafter(state)
            if tid is None:
                break
            pick = state['draft']['pick']
            if self._draft_closed(deadline):
                return
            self._draft_activity('on_clock', tid, pick)
            ent = self.entrant(tid)
            src, dec_id = ('bot', None)
            if self.is_bot(tid):
                pid = bots.draft(ent, self.rules, view, state, tid, self.cfg['seed'])
            else:
                legal_q = legal_draft_choices(self.rules, view.world, state, tid, queues.get(tid, []))
                if legal_q:
                    pid, src = (legal_q[0], 'queue')
                    dec_id = next((e['decision_id'] for e in reversed(self.events()) if e['type'] == 'decision' and e['team'] == tid and (e['kind'] == 'draft_pick')), None)
                    if dec_id and (self.decision(dec_id) or {}).get('forced_default'):
                        src = 'default'
                else:
                    picked = self._llm_draft_pick(view, state, tid, pick, total)
                    if picked is None:
                        return
                    pid, dec_id = picked
                    src = 'model'
                    queues[tid] = list((self.decision(dec_id)['action'] or {}).get('queue') or [])
                    d = self.decision(dec_id)
                    if d.get('forced_default'):
                        src = 'default'
                        self._draft_default_page(dec_id, tid, pick, d)
            if self._draft_closed(deadline, dec_id):
                return
            ev = {'type': 'draft_pick', 'ts': iso(now_utc()), 'data_as_of': iso(as_of), 'team': tid, 'player': pid, 'pick_no': pick + 1, 'round': pick // len(self.seats) + 1, 'source': src, 'decision_id': dec_id}
            try:
                self.append(ev, view.world, state)
            except Violation:
                pid = default_draft_pick(self.rules, view.world, state, tid, {p: v['vor'] for p, v in view.proj.items()})
                ev.update(player=pid, source='default')
                self.append(ev, view.world, state)
            self._publish_draft_feed()
        state = self.state(view.world)
        self.append({'type': 'draft_complete', 'ts': iso(now_utc()), 'data_as_of': iso(as_of)}, view.world, state)
        self._publish_draft_feed()
        state = self.state(view.world)
        for tid in sorted(self.seats):
            if self.standing_lineup() and (not self.is_bot(tid)):
                continue
            at = self.now(as_of + timedelta(seconds=total + 2))
            lu = best_lineup(self.rules, view.world, state, tid, {p: v['week_proj'] for p, v in view.proj.items()}, at if getattr(self, 'clock', None) else as_of)
            self.append({'type': 'lineup_set', 'ts': iso(as_of + timedelta(seconds=total + 2)), 'team': tid, 'lineup': lu, 'source': 'post_draft_default'}, view.world, state)
            state = self.state(view.world)
        self.append({'type': 'step_done', 'ts': iso(as_of), 'step': 'draft'})

    def _llm_draft_pick(self, view, state, tid, pick, total) -> tuple[str, str]:
        n = len(self.seats)
        rnd = pick // n + 1
        dec_id = f'{self.id}:draft:{tid}:p{pick + 1}'
        owned = {p for t in state['teams'].values() for p in t['roster']}
        avail = [p for p in view.world.players if p not in owned]
        by_val = sorted(avail, key=lambda p: -view.proj[p]['vor'])[:45]
        by_ytd = [p for p in sorted(avail, key=lambda p: -view.proj[p]['ytd'])[:25] if p not in by_val]
        picks = [e for e in self.events() if e['type'] == 'draft_pick'][-12:]
        names = self.names()
        recent = '\n'.join((f'  #{e['pick_no']} {e['team']}: {view.ptoken(e['player'])}' for e in picks)) or '  (none)'
        briefing = '\n\n'.join([self.common_brief(view, state, tid).replace(f'YOUR ROSTER ({tid}', f'YOUR ROSTER SO FAR ({tid}'), f'DRAFT ORDER (snake): {', '.join(state['draft']['order'])}', f'RECENT PICKS:\n{recent}', 'BEST AVAILABLE (ordered by projected points/game above replacement level at the position):\n' + '\n'.join(('  ' + view.ptoken(p) for p in by_val)), 'ALSO AVAILABLE, TOP SEASON-TO-DATE SCORERS:\n' + ('\n'.join(('  ' + view.ptoken(p) for p in by_ytd)) or '  (none)')])
        picks_left = self.rules.roster_size - len(state['teams'][tid]['roster']) - 1
        task = self.prompts.TASKS['draft_pick'].format(round=rnd, pick=pick + 1, total=total, picks_left=picks_left)
        if not any((e['type'] == 'team_named' and e['team'] == tid for e in self.events())):
            task += '\nThis is your first pick: also add a top-level "team_name" (max 30 chars) for your team.'

        def validate(a):
            pid = a.get('player_id')
            if a.get('kind') != 'draft_pick' or not pid:
                return ['action must be {"kind":"draft_pick","player_id":...}']
            if pid not in view.world.players:
                return [f'unknown player id {pid}']
            if not legal_draft_choices(self.rules, view.world, state, tid, [pid]):
                return [f'{pid} is taken or would make your roster unable to fill its starting slots / exceed a position cap']
            return []
        dflt = default_draft_pick(self.rules, view.world, state, tid, {p: v['vor'] for p, v in view.proj.items()})
        deadline = self._draft_cutoff(view)
        tries = 0
        stall = None
        while True:
            if self._draft_closed(deadline, dec_id):
                return None
            self._expect(dec_id, tid, 'draft_pick', view, deadline, {'kind': 'draft', 'week': view.week, 'pick': pick + 1})
            if tries:
                self._draft_activity('on_clock', tid, pick)
            rec = self.run_decision(view=view, state=state, tid=tid, kind='draft_pick', decision_id=dec_id, briefing=briefing, task=task, validate=validate, default_action={'kind': 'draft_pick', 'player_id': dflt, 'queue': []}, window='draft', unit='ROS pts', question=f'Round {rnd} pick {pick + 1}', window_s=int(self.cfg.get('draft_pick_window_s', 300)), pause_on_infra=True, request_deadline=deadline)
            if rec is not None:
                if tries:
                    priority.draft_paused(self.id, None)
                break
            tries += 1
            stall = self._draft_paused(dec_id, tid, pick, tries, stall)
            delay = min(30.0 * 2 ** (tries - 1), 300.0)
            held = getattr(self, '_last_held', {})
            priority.draft_paused(self.id, held.get('lane'))
            self._draft_activity('paused', tid, pick, cause=held.get('cause'), lane=held.get('lane'), retry_at=iso(now_utc() + timedelta(seconds=delay)))
            if deadline:
                delay = min(delay, max(0, (deadline - now_utc()).total_seconds()))
            time.sleep(delay)
        nm = rec.get('team_name')
        if isinstance(nm, str) and nm.strip() and (not any((e['type'] == 'team_named' and e['team'] == tid for e in self.events()))):
            self.append({'type': 'team_named', 'ts': rec['ts'], 'team': tid, 'name': nm.strip()[:30]})
        return (rec['action']['player_id'], dec_id)

    def waiver_bids(self, week: int, as_of: datetime) -> None:
        key = f'waiver_bids:{week}'
        if self.done(key):
            return
        self.prepare_swiss(as_of, week)
        view = self.view(week, as_of)
        state = self.state(view.world)
        owned = {p for t in state['teams'].values() for p in t['roster']}
        fa_block = []
        for pos, n in (('QB', 6), ('RB', 10), ('WR', 10), ('TE', 6), ('K', 4), ('DEF', 5)):
            fa = sorted([p for p, x in view.world.players.items() if x['pos'] == pos and p not in owned], key=lambda p: -view.proj[p]['value'])[:n]
            fa_block.append(f' {pos}:\n' + '\n'.join(('  ' + view.ptoken(p) for p in fa)))
        jobs = []
        for tid in sorted(self.seats):
            if self.is_bot(tid, week):
                continue
            jobs.append(tid)
        run_at = next((d for k, d in week_steps(week) if k == f'waiver_run:{week}'))
        for tid in jobs:
            self._expect(f'{self.id}:w{week}:waivers:{tid}', tid, 'waiver_bid', view, run_at, {'kind': 'waiver_bid', 'week': week})
        _parallel(lambda tid: self._waiver_decision(week, tid, as_of, view, fa_block), jobs)
        st = self.state(view.world)
        bview = self.live_view(view)
        bot_bids = {tid: bots.waiver_bids(self.entrant(tid, week), self.rules, bview, st, tid) for tid in self.seats if self.is_bot(tid, week)}
        self.append({'type': 'waiver_bot_bids', 'ts': iso(as_of), 'week': week, 'bids': bot_bids})
        self.append({'type': 'step_done', 'ts': iso(as_of), 'step': key})

    def _waiver_decision(self, week: int, tid: str, as_of: datetime, view=None, fa_block=None, hard_deadline=None):
        view = view or self.view(week, as_of)
        st = self.state(view.world)
        owned = {p for t in st['teams'].values() for p in t['roster']}
        if fa_block is None:
            fa_block = []
            for pos, n in (('QB', 6), ('RB', 10), ('WR', 10), ('TE', 6), ('K', 4), ('DEF', 5)):
                fa = sorted([p for p, x in view.world.players.items() if x['pos'] == pos and p not in owned], key=lambda p: -view.proj[p]['value'])[:n]
                fa_block.append(f' {pos}:\n' + '\n'.join(('  ' + view.ptoken(p) for p in fa)))
        briefing = self.common_brief(view, st, tid) + '\n\nBEST FREE AGENTS (more via the free_agents tool):\n' + '\n'.join(fa_block)
        task = self.prompts.TASKS['waiver_bid'].format(week=week, faab=st['teams'][tid]['faab'])

        def validate(a):
            if a.get('kind') != 'waiver_bid' or not isinstance(a.get('bids'), list):
                return ['action must be {"kind":"waiver_bid","bids":[...]}']
            errs = []
            for b in a['bids'][:5]:
                if not isinstance(b, dict):
                    errs.append('each bid must be an object')
                    continue
                if b.get('add') not in view.world.players:
                    errs.append(f'unknown player {b.get('add')}')
                elif b.get('add') in owned:
                    errs.append(f'{b.get('add')} is not a free agent')
                d = b.get('drop')
                if d and d not in st['teams'][tid]['roster']:
                    errs.append(f'drop {d} is not on your roster')
                if not d and len(st['teams'][tid]['roster']) >= self.rules.roster_size:
                    errs.append(f'roster is full: bid on {b.get('add')} needs a drop')
                try:
                    amt = int(b.get('amount', 0))
                    if amt < 0 or amt > st['teams'][tid]['faab']:
                        errs.append(f'amount {amt} outside 0..{st['teams'][tid]['faab']}')
                except (TypeError, ValueError):
                    errs.append('amount must be an integer')
            return errs
        run_at = next((d for k, d in week_steps(week) if k == f'waiver_run:{week}'))
        run_at = min(run_at, hard_deadline) if hard_deadline else run_at
        rec = self.run_decision(view=view, state=st, tid=tid, kind='waiver_bid', decision_id=f'{self.id}:w{week}:waivers:{tid}', briefing=briefing, task=task, validate=validate, default_action={'kind': 'waiver_bid', 'bids': []}, window=f'w{week}-waivers', unit='FAAB $', question=f'Week {week} FAAB claims', hold_until=run_at, retry={'kind': 'waiver_bid', 'week': week})
        if rec is not None and rec.get('forced_default') and self.standing_lineup():
            self._not_applied('bids_not_applied', tid, rec, view, 'no usable answer; the seat places no bids this week (D-REL-1a)')
        return rec

    def _not_applied(self, typ: str, tid: str, rec: dict, view, note: str, cause: str | None=None, **extra) -> None:
        if any((e['type'] == typ and e.get('decision_id') == rec['decision_id'] for e in self.events())):
            return
        from bm import records
        cause = cause or (rec.get('forced_default') or {}).get('cause')
        self.append({'type': typ, 'ts': iso(self.now(view.as_of)), 'team': tid, 'decision_id': rec['decision_id'], 'cause': cause, 'attribution': 'model' if cause in records.MODEL else 'infra', **extra, 'note': note})

    def waiver_run(self, week: int, as_of: datetime) -> None:
        key = f'waiver_run:{week}'
        self.recover_batch()
        if self.done(key):
            return
        self.prepare_swiss(as_of, week)
        view = self.view(week, as_of)
        state = self.state(view.world)
        standing = self.standing_lineup()
        if standing:
            self.pending_due(self.now(as_of))
        bids = []
        for e in self.events():
            if e['type'] == 'decision' and e['kind'] == 'waiver_bid' and (e['week'] == week):
                if standing and e.get('forced_default'):
                    continue
                for i, b in enumerate((e.get('action') or {}).get('bids') or []):
                    if isinstance(b, dict) and b.get('add'):
                        try:
                            amt = int(b.get('amount', 0))
                        except (TypeError, ValueError):
                            continue
                        bids.append({'team': e['team'], 'add': b['add'], 'drop': b.get('drop') or None, 'amount': amt, 'priority': i + 1})
            if e['type'] == 'waiver_bot_bids' and e['week'] == week:
                for tid, bl in e['bids'].items():
                    for i, b in enumerate(bl):
                        bids.append({'team': tid, 'add': b['add'], 'drop': b.get('drop'), 'amount': int(b['amount']), 'priority': i + 1})
        evs = self.events()
        logged_run = next((e for e in evs if e['type'] == 'waiver_run' and e.get('week') == week), None)
        if logged_run is not None:
            self.append({'type': 'step_done', 'ts': logged_run['ts'], 'step': key})
            return
        written = self._run_awards(evs)
        if written:
            state = self.fold_events([e for i, e in enumerate(evs) if i not in written])
        at = evs[written[0]].get('scheduled_ts') or evs[written[0]]['ts'] if written else iso(self.now(as_of))
        st = self.competition_standings(state, week, as_of)
        worst_first = [r['team_id'] for r in reversed(st)]
        awards, results = resolve_waivers(self.rules, view.world, state, bids, worst_first, f'{self.id}:w{week}', at)
        done = [evs[i] for i in written]
        sig = lambda e: (e['team'], e['add'], e.get('drop'), int(e['amount']))
        if [sig(e) for e in done] != [sig(e) for e in awards[:len(done)]]:
            raise RuntimeError(f'{self.id} waiver_run:{week}: logged awards are not a prefix of the resolution; not resuming')
        ts = at if written else iso(as_of)
        self.append_batch(list(awards[len(done):]) + [{'type': 'waiver_run', 'ts': ts, 'week': week, 'results': results, 'seed': f'{self.id}:w{week}'}, {'type': 'step_done', 'ts': ts, 'step': key}])

    @staticmethod
    def _run_awards(evs: list[dict]) -> list[int]:
        start = 0
        for i, e in enumerate(evs):
            if e['type'] == 'waiver_run' or (e['type'] == 'step_done' and str(e.get('step', '')).startswith('waiver_run:')):
                start = i + 1
        return [i for i in range(start, len(evs)) if evs[i]['type'] == 'waiver_award' and 'source' not in evs[i]]

    def fold_events(self, evs: list[dict]) -> dict:
        from .context import engine_world
        return fold(self.rules, engine_world(), evs)

    def lineup_window(self, week: int, n: int, as_of: datetime) -> None:
        key = f'lineup:{week}:{n}'
        if self.done(key):
            return
        self.prepare_swiss(as_of, week)
        view = self.view(week, as_of)
        state = self.state(view.world)
        jobs = []
        for tid in sorted(self.seats):
            if self.is_bot(tid, week):
                bview = self.live_view(view)
                lu = bots.lineup(self.entrant(tid, week), self.rules, bview, state, tid, self.cfg['seed'])
                try:
                    self.append({'type': 'lineup_set', 'ts': iso(as_of), 'team': tid, 'lineup': lu, 'source': 'bot'}, view.world, state)
                except Violation:
                    pass
                state = self.state(view.world)
                continue
            late = week >= LATE_WINDOWS_FROM_WEEK
            if n >= 2 and (not self._needs_swap(self.live_view(view), state, tid, questionable=late)):
                continue
            jobs.append(tid)
        for tid in jobs:
            self._expect(f'{self.id}:w{week}:lineup{n}:{tid}', tid, 'start_sit', view, self._hold_lineup(view, state, tid), {'kind': 'lineup', 'week': week, 'n': n})
        _parallel(lambda tid: self._lineup_decision(week, n, tid, as_of, view), jobs)
        self.append({'type': 'step_done', 'ts': iso(as_of), 'step': key})

    def _lineup_decision(self, week: int, n: int, tid: str, as_of: datetime, view=None, hard_deadline=None, did: str | None=None) -> None:
        retry = {'kind': 'lineup', 'week': week, 'n': n, **({'decision_id': did} if did else {})}
        did = did or f'{self.id}:w{week}:lineup{n}:{tid}'
        view = view or self.view(week, as_of)
        st = self.state(view.world)
        slots = ', '.join((f'"{s}":"<id>"' for s in self.rules.starter_slots))
        briefing = self.common_brief(view, st, tid)
        if n == 2 and week < LATE_WINDOWS_FROM_WEEK:
            briefing += '\n\nSUNDAY SWAP: at least one of your starters is Out, Doubtful, on bye, or a slot is empty.'
        elif n >= 2:
            briefing += '\n\nLATE SWAP: inactive lists for the next games are out (about 90 minutes before kickoff). At least one of your unlocked starters is Out, Doubtful, Questionable, on IR, not on an active roster, on bye, or a slot is empty. Players whose games have started are locked.'
        task = prompts.TASKS['start_sit'].format(week=week, slots=slots)

        def validate(a):
            if a.get('kind') != 'start_sit' or not isinstance(a.get('lineup'), dict):
                return ['action must be {"kind":"start_sit","lineup":{...}}']
            lu = {s: a['lineup'].get(s) or None for s in self.rules.starter_slots}
            return [f'{v.code}: {v.detail}' for v in lineup_violations(self.rules, view.world, st, tid, lu, self.now(view.as_of))]
        live2 = week >= RETRY_FROM_WEEK and view.mode != 'reconstructed'
        standing = self.standing_lineup()
        if standing or (live2 and n >= 2):
            dflt = {s: st['teams'][tid]['lineup'].get(s) for s in self.rules.starter_slots}
        else:
            dflt = best_lineup(self.rules, view.world, st, tid, {p: v['week_proj'] for p, v in view.proj.items()}, self.now(view.as_of))
        hold = self._hold_lineup(view, st, tid)
        hold = min(hold, hard_deadline) if hold and hard_deadline else hard_deadline or hold
        window_s = self.call_window_s
        if live2 and hold:
            window_s = int((hold - now_utc()).total_seconds())
            if window_s < 60:
                if not any((e['type'] == 'decision_missed' and e.get('decision_id') == did for e in self.events())):
                    self.append({'type': 'decision_missed', 'ts': iso(self.now(view.as_of)), 'decision_id': did, 'team': tid, 'kind': 'start_sit', 'week': week, 'hold_until': hold.isoformat(), **({'cause': 'lane_failed'} if standing else {}), 'note': "the seat's next lock was under a minute away when its lineup window ran; its own lineup stands"})
                return
            window_s = min(self.call_window_s, window_s)
        rec = self.run_decision(view=view, state=st, tid=tid, kind='start_sit', decision_id=did, briefing=briefing, task=task, validate=validate, default_action={'kind': 'start_sit', 'lineup': dflt}, window=f'w{week}-lineup{n}', unit='week pts', question=f'Week {week} lineup', window_s=window_s, hold_until=hold, retry=retry)
        if rec is None:
            return
        if standing and rec.get('forced_default'):
            from bm import records
            cause = (rec['forced_default'] or {}).get('cause')
            if cause == 'PARSE' and (rec.get('validation') or {}).get('violations'):
                cause = records.decision_failure(rec, [])['code']
            self.append({'type': 'lineup_not_applied', 'ts': iso(self.now(view.as_of)), 'team': tid, 'decision_id': rec['decision_id'], 'cause': cause, 'attribution': 'model' if cause in records.MODEL else 'infra', 'note': "no usable answer; the seat's own last lineup stands (D-REL-1a)"})
            return
        lu = {s: rec['action']['lineup'].get(s) or None for s in self.rules.starter_slots}
        with self._lock:
            st2 = self.state(view.world)
        if live2:
            at = now_utc()
            cur = st2['teams'][tid]['lineup']
            lu = {s: p if not (view.world.locked(week, p, at) if p else False) and (not (view.world.locked(week, cur.get(s), at) if cur.get(s) else False)) else cur.get(s) for s, p in lu.items()}
            try:
                self.append({'type': 'lineup_set', 'ts': iso(at), 'team': tid, 'lineup': lu, 'decision_id': rec['decision_id'], 'source': 'default' if rec.get('forced_default') else 'model'}, view.world, st2)
            except Violation as v:
                self.append({'type': 'lineup_not_applied', 'ts': iso(at), 'team': tid, 'decision_id': rec['decision_id'], 'violation': v.as_dict()})
            return
        try:
            self.append({'type': 'lineup_set', 'ts': iso(view.as_of), 'team': tid, 'lineup': lu, 'decision_id': rec['decision_id'], 'source': 'default' if rec.get('forced_default') else 'model'}, view.world, st2)
        except Violation as v:
            if standing:
                self.append({'type': 'lineup_not_applied', 'ts': iso(view.as_of), 'team': tid, 'decision_id': rec['decision_id'], 'violation': v.as_dict()})
                return
            st2 = self.state(view.world)
            lu = best_lineup(self.rules, view.world, st2, tid, {p: v['week_proj'] for p, v in view.proj.items()}, view.as_of)
            self.append({'type': 'lineup_set', 'ts': iso(view.as_of), 'team': tid, 'lineup': lu, 'decision_id': rec['decision_id'], 'source': 'default'}, view.world, st2)

    def _tie_seed(self, week: int) -> str | None:
        return self.cfg['seed'] if week >= SEEDED_TIES_FROM_WEEK else None

    def _needs_swap(self, view, state, tid, questionable: bool=False) -> bool:
        risky = ('Out', 'Doubtful', 'IR', 'FA', 'PS', 'RET') + (('Questionable',) if questionable else ())
        for s, pid in state['teams'][tid]['lineup'].items():
            if pid is None:
                return True
            if view.world.locked(view.week, pid, view.as_of):
                continue
            pr = view.proj.get(pid, {})
            if pr.get('week_proj', 0) <= 0.0 or pr.get('status') in risky:
                return True
        return False

    def _trade_original_state(self, did, current):
        from . import trade_context as tc
        packet = tc.read(self, 'trade_decision_context', did)
        return packet['state'] if packet else current

    def _trade_pause(self, window, deadline, phase):
        n = sum((e['type'] == 'trade_batch_paused' and e.get('window') == window for e in self.events()))
        delay = min(RETRY_BASE_S * 2 ** min(n, 20), RETRY_CAP_S)
        self.append(dict(type='trade_batch_paused', ts=iso(now_utc()), window=window, phase=phase, deadline=iso(deadline), next_try_at=iso(min(deadline, now_utc() + timedelta(seconds=delay))), note='selected original opportunities incomplete; no expiry or completion claim'))

    def _trade_cutoff(self, week, n, as_of, view):
        if week < RETRY_FROM_WEEK or view.mode == 'reconstructed':
            return None
        if hasattr(self, 'dir'):
            from . import trade_context as tc
            original = tc.read(self, 'trade_original_context', f'w{week}-t{n}')
            if original:
                return datetime.fromisoformat(original['deadline'])
        key = f'trade:{week}:{n}'
        scheduled = next((due for step, due in week_steps(week) if step == key), None)
        if scheduled is None:
            raise ValueError('original trade schedule unmeasured; no request authorized')
        original = as_of + timedelta(hours=6)
        cap = scheduled + timedelta(minutes=int(self.cfg.get('step_offset_min') or 0), hours=6)
        return min(original, cap)

    def _close_unanswered(self, marker: str, note: str) -> list[str]:
        evs = self.events()
        decided = {e['decision_id'] for e in evs if e['type'] == 'decision'}
        missed = {e.get('decision_id') for e in evs if e['type'] == 'decision_missed'}
        expected = {e['decision_id']: e for e in evs if e['type'] == 'decision_expected' and marker in e.get('decision_id', '')}
        fails: dict[str, list] = {}
        for e in evs:
            if e['type'] in ('decision_pending', 'decision_skipped_infra') and e.get('decision_id') in expected:
                fails.setdefault(e['decision_id'], []).append(e)
        unanswered = sorted(set(expected) - decided)
        for did in unanswered:
            if did in missed:
                continue
            x, last = (expected[did], (fails.get(did) or [{}])[-1])
            refused = last.get('cause') == 'REFUSED'
            self.append({'type': 'decision_missed', 'ts': iso(now_utc()), 'decision_id': did, 'team': x.get('team'), 'entrant': x.get('entrant'), 'kind': x.get('kind'), 'week': x.get('week'), 'hold_until': x.get('hold_until'), 'cause': 'REFUSED' if refused else 'lane_failed', **({'outcome': 'refused'} if refused else {}), 'attempts': len(fails.get(did) or []), 'note': note})
        return unanswered

    def _close_trade_window(self, key: str, window: str, note: str, cause: str | None=None) -> None:
        unanswered = self._close_unanswered(f':{window}:', "the trade window closed before the seat's call was answered; no offer or reply for it (D-REL-1a)")
        self.append(dict(type='step_done', ts=iso(now_utc()), step=key, note=note, completion_status='closed_incomplete', unanswered_decision_ids=unanswered, **{'cause': cause} if cause else {}))

    def _trade_blocked(self, week, n, deadline):
        if deadline is None:
            return False
        marker = f':w{week}-t{n}:'
        partial = any((e.get('window') == marker.strip(':') or marker in (e.get('decision_id') or '') for e in self.events()))
        if now_utc() < deadline and (not partial):
            return False
        key = f'trade:{week}:{n}'
        if not any((e['type'] == 'trade_window_gap' and e.get('step') == key for e in self.events())):
            self.append(dict(type='trade_window_gap', ts=iso(now_utc()), step=key, deadline=iso(deadline), cause='partial batch has no resumable context' if partial else 'original scheduled cutoff passed'))
        return True

    def trade_window(self, week: int, n: int, as_of: datetime) -> None:
        key = f'trade:{week}:{n}'
        if self.done(key):
            return
        if week > self.rules.trades['deadline_week']:
            self.append({'type': 'step_done', 'ts': iso(as_of), 'step': key, 'note': 'past trade deadline'})
            return
        window = f'w{week}-t{n}'
        from . import trade_context as tc
        try:
            original = tc.read(self, 'trade_original_context', window)
            view = tc.restore_view(self, original) if original else None
        except (OSError, ValueError, KeyError, TypeError) as exc:
            if not any((e['type'] == 'trade_context_gap' and e.get('window') == window for e in self.events())):
                self.append(dict(type='trade_context_gap', ts=iso(now_utc()), window=window, cause='original retained context unavailable/invalid', error=type(exc).__name__))
            due = next((d for k, d in week_steps(week) if k == key), None)
            cutoff = due + timedelta(minutes=int(self.cfg.get('step_offset_min') or 0), hours=6) if due else None
            if cutoff is None or now_utc() >= cutoff:
                self._close_trade_window(key, window, 'closed: retained trade context unreadable at the original cutoff', cause='context unreadable')
            return
        if original:
            as_of = view.as_of
            deadline = datetime.fromisoformat(original['deadline'])
            if now_utc() >= deadline:
                if not any((e['type'] == 'trade_window_gap' and e.get('step') == key for e in self.events())):
                    self.append(dict(type='trade_window_gap', ts=iso(now_utc()), step=key, deadline=iso(deadline), cause='original retained cutoff passed'))
                self._close_trade_window(key, window, 'closed with retained unresolved trade gap')
                return
            pause = next((e for e in reversed(self.events()) if e['type'] == 'trade_batch_paused' and e.get('window') == window), None)
            if pause and now_utc() < datetime.fromisoformat(pause['next_try_at']):
                return
        else:
            self.prepare_swiss(as_of, week)
            view = self.view(week, as_of)
            deadline = self._trade_cutoff(week, n, as_of, view)
            if self._trade_blocked(week, n, deadline):
                self._close_trade_window(key, window, 'closed: no retained trade context to resume (trade_window_gap)')
                return
            if deadline:
                tc.freeze_view(self, view, deadline, window)
        prospective = deadline is not None
        names = self.names()
        state = self.state(view.world)
        jobs = [t for t in sorted(self.seats) if not self.is_bot(t, view.week)]

        def offer(tid):
            st = self.state(view.world)
            if prospective:
                st = self._trade_original_state(f'{self.id}:{window}:offer:{tid}', st)
            others = '\n'.join((f'{o} {names[o]}:\n{self.roster_block(view, st, o)}' for o in sorted(st['teams']) if o != tid))
            briefing = self.common_brief(view, st, tid) + '\n\nOTHER ROSTERS:\n' + others
            task = self.prompts.TASKS['trade_offer'].format(window=n, week=week, max_threads=self.rules.trades['max_threads_per_team_per_window'])

            def validate(a):
                if a.get('kind') != 'trade_offer' or not isinstance(a.get('offers', []), list):
                    return ['action must be {"kind":"trade_offer","offers":[...]}']
                errs = []
                for o in a.get('offers', [])[:self.rules.trades['max_threads_per_team_per_window']]:
                    if not isinstance(o, dict) or o.get('to_team') not in st['teams'] or o.get('to_team') == tid:
                        errs.append(f'bad to_team in {o}')
                        continue
                    errs += [f'{v.code}: {v.detail}' for v in offer_violations(self.rules, view.world, st, tid, o['to_team'], list(o.get('give') or []), list(o.get('get') or []), self.now(view.as_of))]
                    if len(str(o.get('message') or '')) > self.rules.max_msg_chars:
                        errs.append('message too long')
                return errs
            return self.run_decision(view=view, state=st, tid=tid, kind='trade_offer', decision_id=f'{self.id}:{window}:offer:{tid}', briefing=briefing, task=task, validate=validate, default_action={'kind': 'trade_offer', 'offers': []}, window=window, unit='ROS pts', question=f'Trade window {window}: offers', request_deadline=deadline)
        for tid in jobs:
            self._expect(f'{self.id}:{window}:offer:{tid}', tid, 'trade_offer', view, deadline or as_of + timedelta(hours=6), {'kind': 'trade', 'week': week, 'n': n})
        recs = _parallel(offer, jobs)
        if prospective and (now_utc() >= deadline or len(recs) != len(jobs) or any((r is None for r in recs))):
            self._trade_pause(window, deadline, 'offers')
            return
        for rec in sorted([r for r in recs if r], key=lambda r: r['team']):
            if prospective and now_utc() >= deadline:
                self._trade_pause(window, deadline, 'offer_application')
                return
            tid = rec['team']
            if rec.get('forced_default') and self.standing_lineup():
                self._not_applied('trade_not_applied', tid, rec, view, 'no usable answer; the seat makes no offers (D-REL-1a)')
                continue
            ch = rec.get('chatter')
            if isinstance(ch, str) and ch.strip() and (not any((e['type'] == 'chatter' and e['team'] == tid and (e['window'] == window) for e in self.events()))):
                try:
                    self.append({'type': 'chatter', 'ts': rec['ts'], 'team': tid, 'window': window, 'text': ch.strip()[:self.rules.max_msg_chars], 'decision_id': rec['decision_id'], **carry_note(rec, 2000)}, view.world, self.state(view.world))
                except Violation:
                    pass
            for i, o in enumerate(((rec.get('action') or {}).get('offers') or [])[:self.rules.trades['max_threads_per_team_per_window']]):
                if prospective and now_utc() >= deadline:
                    self._trade_pause(window, deadline, 'offer_application')
                    return
                th_id = f'{self.id}-{window}-{tid[-3:]}-{i + 1}'
                if any((e['type'] in ('trade_msg', 'trade_msg_rejected') and e.get('thread_id') == th_id for e in self.events())):
                    continue
                if week >= SAME_MODEL_BLOCK_FROM_WEEK and o.get('to_team') in self.seats and (self.entrant(o['to_team'], week) == self.entrant(tid, week)):
                    self.append({'type': 'trade_msg_rejected', 'ts': rec['ts'], 'thread_id': th_id, 'violation': {'code': 'INELIGIBLE_PARTNER', 'detail': f'{o['to_team']} is not eligible to trade with {tid}'}})
                    continue
                ev = {'type': 'trade_msg', 'ts': rec['ts'], 'thread_id': th_id, 'window': window, 'from_team': tid, 'to_team': o['to_team'], 'action': 'PROPOSE', 'offer': {'give': list(o.get('give') or []), 'get': list(o.get('get') or [])}, 'text': str(o.get('message') or '')[:self.rules.max_msg_chars], 'decision_id': rec['decision_id'], **carry_note(rec, 4000), 'est_value': rec.get('est_value'), 'seq_in_thread': 1}
                try:
                    self.append(ev, view.world, self.state(view.world))
                except Violation as v:
                    self.append({'type': 'trade_msg_rejected', 'ts': rec['ts'], 'thread_id': th_id, 'violation': v.as_dict()})
        for rnd in range(1, self.rules.trades['max_msgs_per_thread']):
            state = self.state(view.world)
            turns = []
            for th_id, th in sorted(state['threads'].items()):
                if th['window'] != window or th['status'] != 'open':
                    continue
                nxt = th['b'] if th['last_from'] == th['a'] else th['a']
                turns.append((th_id, nxt))
            if not turns and (not prospective):
                break
            if prospective:
                phase_key = f'{window}:round{rnd}'
                if any((e['type'] == 'trade_phase_complete' and e.get('key') == phase_key for e in self.events())):
                    continue
                phase = tc.read(self, 'trade_phase_selected', phase_key)
                if phase is None:
                    if not turns:
                        break
                    gapped = {e.get('decision_id') for e in self.events() if e['type'] == 'trade_application_gap'}
                    turns = [(th, t) for th, t in turns if f'{self.id}:{window}:{th}:m{state['threads'][th]['msgs'] + 1}:{t}' not in gapped]
                    if not turns:
                        break
                    phase = tc.write(self, 'trade_phase_selected', phase_key, dict(turns=[dict(thread=th, team=t, decision_id=f'{self.id}:{window}:{th}:m{state['threads'][th]['msgs'] + 1}:{t}') for th, t in turns]))
                slots_by_pair = {(x['thread'], x['team']): x['decision_id'] for x in phase['turns']}
                turns = [(x['thread'], x['team']) for x in phase['turns'] if not any((e['type'] == 'trade_msg' and e.get('decision_id') == x['decision_id'] for e in self.events()))]
            by_team: dict[str, list] = {}
            for th_id, t in turns:
                by_team.setdefault(t, []).append(th_id)

            def respond_all(tid):
                for th_id in by_team[tid]:
                    result = self._respond(view, window, th_id, tid, rnd, expected_decision_id=slots_by_pair[th_id, tid]) if prospective else self._respond(view, window, th_id, tid, rnd)
                    if result is False and prospective:
                        return False
                return True
            outcomes = _parallel(respond_all, sorted(by_team))
            if prospective:
                if len(outcomes) != len(by_team) or not all(outcomes):
                    self._trade_pause(window, deadline, phase_key)
                    return
                self.append(dict(type='trade_phase_complete', key=phase_key, ts=iso(now_utc())))
        if prospective and now_utc() >= deadline:
            self._trade_pause(window, deadline, 'final_application')
            return
        state = self.state(view.world)
        for th_id, th in sorted(state['threads'].items()):
            if th['window'] == window and th['status'] == 'open':
                if prospective and now_utc() >= deadline:
                    self._trade_pause(window, deadline, 'final_application')
                    return
                self._expire_thread(th_id, as_of)
        if prospective and now_utc() >= deadline:
            self._trade_pause(window, deadline, 'final_application')
            return
        self.append({'type': 'step_done', 'ts': iso(as_of), 'step': key})

    def _expire_thread(self, th_id: str, as_of: datetime) -> None:
        close = as_of + timedelta(hours=6)
        if getattr(self, 'clock', None):
            self.append({'type': 'trade_expired', 'ts': iso(self.now(as_of)), 'scheduled_ts': iso(close), 'thread_id': th_id})
        else:
            self.append({'type': 'trade_expired', 'ts': iso(close), 'thread_id': th_id})

    def _respond(self, view, window: str, th_id: str, tid: str, rnd: int, expected_decision_id=None):
        deadline = self._trade_cutoff(view.week, int(window.split('-t')[1]), view.as_of, view)
        if deadline and now_utc() >= deadline:
            return False
        with self._lock:
            st = self.state(view.world)
        th = st['threads'].get(th_id)
        if expected_decision_id and (not th or th['status'] != 'open' or th['last_from'] == tid or (expected_decision_id != f'{self.id}:{window}:{th_id}:m{th['msgs'] + 1}:{tid}')):
            if not any((e['type'] == 'trade_application_gap' and e.get('decision_id') == expected_decision_id for e in self.events())):
                self.append(dict(type='trade_application_gap', ts=iso(now_utc()), window=window, decision_id=expected_decision_id, cause='original selected thread state changed before dispatch'))
            return None
        if not th or th['status'] != 'open' or th['last_from'] == tid:
            return
        if not self.is_bot(tid, view.week) and view.mode != 'reconstructed' and (view.week >= RETRY_FROM_WEEK):
            st = self._trade_original_state(f'{self.id}:{window}:{th_id}:m{th['msgs'] + 1}:{tid}', st)
            th = st['threads'][th_id]
        other = th['a'] if th['b'] == tid else th['b']
        lo = th['latest_offer']
        give_me = lo['give'] if lo['from'] == other else lo['get']
        take_me = lo['get'] if lo['from'] == other else lo['give']
        msgs = [e for e in self.events() if e['type'] == 'trade_msg' and e['thread_id'] == th_id]
        ent = self.entrant(tid, view.week)
        ts = iso(view.as_of + timedelta(minutes=10 * rnd))
        if self.is_bot(tid, view.week):
            resp = bots.trade_response(ent, view, give_me, take_me, rules=self.rules, roster=st['teams'][tid]['roster'])
            ev = {'type': 'trade_msg', 'ts': ts, 'thread_id': th_id, 'window': window, 'from_team': tid, 'to_team': other, 'action': resp, 'text': 'Accepted.' if resp == 'ACCEPT' else 'No thanks.', 'source': 'bot', 'seq_in_thread': len(msgs) + 1}
            if expected_decision_id:
                ev['decision_id'] = expected_decision_id
            if deadline and now_utc() >= deadline:
                return False
            try:
                self.append(ev, view.world, st)
            except Violation:
                ev.update(action='REJECT', text='No thanks.')
                self.append(ev, view.world, self.state(view.world))
            return
        names = self.names()
        rows = [f'  #{i + 1} {m['from_team']} {m['action']}' + (f" offer give={m['offer']['give']} get={m['offer']['get']} (from {m['from_team']}'s side)" if m.get('offer') else '') + f': "{m.get('text', '')[:600]}"' for i, m in enumerate(msgs)]
        fence = getattr(self.prompts, 'fence', None)
        hist = fence(f'TRADE THREAD {th_id} MESSAGES', rows) if fence else '\n'.join(rows)
        involved = sorted(set(lo['give']) | set(lo['get']))
        briefing = '\n\n'.join([self.common_brief(view, st, tid), f'THREAD {th_id} between you ({tid}) and {other} {names[other]}:\n{hist}', 'LATEST OFFER ON THE TABLE:\n  you would RECEIVE: ' + ('; '.join((view.ptoken(p) for p in give_me)) or 'nothing') + '\n  you would SEND: ' + ('; '.join((view.ptoken(p) for p in take_me)) or 'nothing'), f'{other} ROSTER:\n{self.roster_block(view, st, other)}'])
        task = self.prompts.TASKS['trade_response'].format(thread_id=th_id, other=f'{other} {names[other]}', n=th['msgs'] + 1, max_msgs=self.rules.trades['max_msgs_per_thread'])
        if th['msgs'] + 1 >= self.rules.trades['max_msgs_per_thread']:
            task += '\nThis is the LAST message allowed in this thread: only ACCEPT or REJECT make sense.'

        def validate(a):
            r = a.get('response')
            if a.get('kind') != 'trade_response' or r not in ('ACCEPT', 'REJECT', 'COUNTER', 'MESSAGE'):
                return ['action must be {"kind":"trade_response","response":"ACCEPT|REJECT|COUNTER|MESSAGE",...}']
            ev = self._resp_event(a, th_id, window, tid, other, iso(self.now(datetime.fromisoformat(ts))), '', None)
            try:
                apply_event(self.rules, view.world, st, ev)
            except Violation as v:
                return [f'{v.code}: {v.detail}']
            return []
        self._expect(f'{self.id}:{window}:{th_id}:m{th['msgs'] + 1}:{tid}', tid, 'trade_response', view, self._trade_cutoff(view.week, int(window.split('-t')[1]), view.as_of, view) or view.as_of + timedelta(hours=6), {'kind': 'trade', 'week': view.week, 'n': int(window.split('-t')[1])})
        rec = self.run_decision(view=view, state=st, tid=tid, kind='trade_response', decision_id=f'{self.id}:{window}:{th_id}:m{th['msgs'] + 1}:{tid}', briefing=briefing, task=task, validate=validate, default_action={'kind': 'trade_response', 'response': 'REJECT', 'message': ''}, window=window, unit='ROS pts', question=f'Respond in {th_id}', request_deadline=self._trade_cutoff(view.week, int(window.split('-t')[1]), view.as_of, view))
        if rec is None or (deadline and now_utc() >= deadline):
            return False
        if rec.get('forced_default') and self.standing_lineup():
            self._not_applied('trade_not_applied', tid, rec, view, 'no usable answer; the engine sends no reply for the seat (D-REL-1a)', thread_id=th_id)
            return
        ev = self._resp_event(rec['action'], th_id, window, tid, other, ts, rec['decision_id'], rec)
        ev['seq_in_thread'] = len(msgs) + 1
        with self._lock:
            st2 = self.state(view.world)
        try:
            self.append(ev, view.world, st2)
        except Violation as violation:
            if view.mode != 'reconstructed' and view.week >= RETRY_FROM_WEEK and (state_hash(st) != state_hash(st2)):
                if not any((e['type'] == 'trade_application_gap' and e.get('decision_id') == rec['decision_id'] for e in self.events())):
                    self.append(dict(type='trade_application_gap', ts=iso(now_utc()), window=window, decision_id=rec['decision_id'], cause='original action no longer applies to changed state', violation=violation.as_dict()))
                self._not_applied('trade_not_applied', tid, rec, view, "the seat's reply no longer applies to the changed rosters; no engine reply for this thread (D-REL-1a)", cause='STATE_CHANGED', thread_id=th_id, violation=violation.as_dict())
                return
            if self.standing_lineup():
                self._not_applied('trade_not_applied', tid, rec, view, "the seat's reply did not apply; no engine reply (D-REL-1a)", cause='ILLEGAL', thread_id=th_id, violation=violation.as_dict())
                return
            ev = self._resp_event({'response': 'REJECT', 'message': '(forced default: reject)'}, th_id, window, tid, other, ts, rec['decision_id'], rec)
            ev['source'] = 'default'
            self.append(ev, view.world, self.state(view.world))

    def _resp_event(self, a, th_id, window, tid, other, ts, dec_id, rec) -> dict:
        ev = {'type': 'trade_msg', 'ts': ts, 'thread_id': th_id, 'window': window, 'from_team': tid, 'to_team': other, 'action': a.get('response', 'REJECT'), 'text': str(a.get('message') or '')[:self.rules.max_msg_chars], 'decision_id': dec_id}
        if ev['action'] == 'COUNTER':
            o = a.get('offer') or {}
            ev['offer'] = {'give': list(o.get('give') or []), 'get': list(o.get('get') or [])}
        if rec:
            ev.update(carry_note(rec, 4000))
            ev['est_value'] = rec.get('est_value')
            if rec.get('forced_default'):
                ev['source'] = 'default'
        return ev

    def record_locks(self, week: int, now: datetime) -> None:
        pending = []
        for g in sorted({g['kickoff'] for g in nv.schedule(SEASON) if g['week'] == week}):
            k = datetime.fromisoformat(g)
            key = f'lock:{week}:{g}'
            if k <= now and (not self.done(key)):
                pending.append((g, k, key))
        if not pending:
            return
        view_world = self.view(week, now).world
        for g, k, key in pending:
            state = fold(self.rules, view_world, [e for e in self.events() if e['type'] != 'lineup_set' or effective_time(e) < k])
            lus = {t: {s: p for s, p in tm['lineup'].items() if p and view_world.kickoff(week, p) == k} for t, tm in state['teams'].items()}
            self.append({'type': 'lineup_lock', 'ts': iso(k), 'week': week, 'kickoff': g, 'locked': lus, 'state_hash': state_hash(state)})
            self.append({'type': 'step_done', 'ts': iso(k), 'step': key})

    def score(self, week: int, now: datetime) -> bool:
        key = f'score:{week}'
        self.recover_batch()
        if self.done(key):
            return True
        scored = self.latest_scored(week)
        if scored is not None:
            ts = scored['ts']
            if not any((e['type'] == 'week_advance' and e.get('week') == week + 1 for e in self.events())):
                self.prepare_swiss(datetime.fromisoformat(ts))
                view = self.view(week, now)
                self.append({'type': 'week_advance', 'ts': ts, 'week': week + 1}, view.world, self.state(view.world))
            self.append({'type': 'step_done', 'ts': ts, 'step': key})
            return True
        self.prepare_swiss(now, week)
        games = [g for g in nv.schedule(SEASON) if g['week'] == week]
        if any((nv.game_end(g['kickoff']) > now for g in games)):
            return False
        view = self.view(week, now)
        lines, refs = nv.weekly_lines(SEASON, now, through_week=week)
        wk = lines.get(week, {})
        teams_played = {t for g in games for t in (g['home'], g['away'])}
        have = {pid[4:] for pid in wk if pid.startswith('DEF-')}
        missing = sorted(teams_played - have)
        if missing:
            last_end = max((nv.game_end(g['kickoff']) for g in games))
            if now < last_end + IRREGULAR_GRACE:
                return False
            gone = {t for g in games if g['home'] in missing or g['away'] in missing for t in (g['home'], g['away'])}
            wk = {pid: ln for pid, ln in wk.items() if (pid[4:] if pid.startswith('DEF-') else ln.get('team')) not in gone}
        state = self.state(view.world)
        lus = {t: dict(tm['lineup']) for t, tm in state['teams'].items()}
        res = score_week(self.rules, view.world.players, lus, wk)
        ev = {'type': 'week_scored', 'ts': iso(now), 'week': week, 'status': 'provisional', 'points': {t: r['points'] for t, r in res.items()}, 'detail': res, 'data_refs': [{k: r.get(k) for k in ('asset', 'retrieved_at', 'sha256')} for r in refs]}
        if missing:
            ev['incomplete_games'] = [{'game_id': g['game_id'], 'kickoff': g['kickoff']} for g in games if g['home'] in missing or g['away'] in missing]
        batch = [ev]
        batch += self._swiss_proposals(self.events() + batch, iso(now))
        self.append_batch(batch + [{'type': 'week_advance', 'ts': iso(now), 'week': week + 1}, {'type': 'step_done', 'ts': iso(now), 'step': key}])
        return True

    def _swiss_proposals(self, events: list[dict], ts: str) -> list[dict]:
        if self.rules.raw.get('season', {}).get('standings') != 'swiss':
            return []
        evs, out = (list(events), [])
        view = finals_view(self.cfg, self.rules, evs)
        while view['proposed_round'] is not None:
            ev = {**view['proposed_round'], 'ts': ts}
            out.append(ev)
            evs.append(ev)
            view = finals_view(self.cfg, self.rules, evs)
        return out

    def latest_scored(self, week: int) -> dict | None:
        return next((e for e in reversed(self.events()) if e['type'] == 'week_scored' and e['week'] == week), None)

    def rescore(self, week: int, now: datetime, reason: str, status: str='provisional', resolved: list | None=None) -> dict | None:
        prev = self.latest_scored(week)
        if prev is None:
            return None
        if self.rules.raw.get('season', {}).get('standings') == 'swiss' and (not getattr(self, '_exclusive_held', False)):
            raise RuntimeError('Swiss re-scoring requires the league exclusive lock')
        view = self.view(week, now)
        lines, refs = nv.weekly_lines(SEASON, now, through_week=week)
        wk = lines.get(week, {})
        before = prev.get('incomplete_games') or []
        still = [g for g in before if g['game_id'] not in (resolved or [])]
        out_ids = {g['game_id'] for g in still} | {g['game_id'] for g in prev.get('no_games') or []}
        if status == 'final' and resolved:
            out_ids |= {g['game_id'] for g in before if g['game_id'] in resolved and now > datetime.fromisoformat(g['kickoff']) + SUSPENDED_COUNT}
        if out_ids:
            gone = {t for g in nv.schedule(SEASON) if g['game_id'] in out_ids for t in (g['home'], g['away'])}
            wk = {pid: ln for pid, ln in wk.items() if (pid[4:] if pid.startswith('DEF-') else ln.get('team')) not in gone}
        lus = {t: {r['slot']: r['player'] for r in d['starters']} for t, d in prev['detail'].items()}
        res = score_week(self.rules, view.world.players, lus, wk)
        pts = {t: r['points'] for t, r in res.items()}
        if pts == prev['points'] and status == prev.get('status') and (len(still) == len(before)):
            return None
        state = self.state(view.world)
        ev = {'type': 'week_scored', 'ts': iso(now), 'week': week, 'status': status, 'revision': int(prev.get('revision', 0)) + 1, 'reason': reason, 'points': pts, 'detail': res, 'correction': {t: round(pts[t] - prev['points'].get(t, 0.0), 2) for t in pts if pts[t] != prev['points'].get(t)}, 'data_refs': [{k: r.get(k) for k in ('asset', 'retrieved_at', 'sha256')} for r in refs]}
        if still:
            ev['incomplete_games'] = still
        nog = (prev.get('no_games') or []) + [g for g in before if g not in still and g['game_id'] in out_ids]
        if nog:
            ev['no_games'] = nog
        self.append(ev, view.world, state)
        self.prepare_swiss(now)
        return ev

    def rescore_due(self, now: datetime) -> list[int]:
        done = []
        weeks = sorted({e['week'] for e in self.events() if e['type'] == 'week_scored'})
        sched = None
        checks = _load_checks()
        check_updates = {}
        for week in weeks:
            prev = self.latest_scored(week)
            if prev.get('status') == 'final':
                continue
            open_ = [g for g in prev.get('incomplete_games') or [] if now <= datetime.fromisoformat(g['kickoff']) + SUSPENDED_COUNT]
            if open_:
                sched = sched or {g['game_id']: g for g in nv.schedule(SEASON)}
                lines, _ = nv.weekly_lines(SEASON, now, through_week=week)
                have = {pid[4:] for pid in lines.get(week, {}) if pid.startswith('DEF-')}
                finished = [g['game_id'] for g in open_ if g['game_id'] in sched and {sched[g['game_id']]['home'], sched[g['game_id']]['away']} <= have]
                if finished and self.rescore(week, now, reason=f'suspended game(s) completed within {SUSPENDED_COUNT.days} days: {', '.join(finished)}', resolved=finished):
                    done.append(week)
                    continue
            if datetime.fromisoformat(prev['ts']) < CORRECTIONS_FROM:
                continue
            deadline = correction_deadline(week)
            key = f'{self.id}:{week}'
            if now > deadline and (not open_):
                if self.rescore(week, now, reason='stat-correction window closed (Saturday after the week)', status='final', resolved=[g['game_id'] for g in prev.get('incomplete_games') or []]):
                    done.append(week)
            elif now - datetime.fromisoformat(checks.get(key, '2000-01-01T00:00:00+00:00')) >= CORRECTION_EVERY:
                checks[key] = now.isoformat()
                check_updates[key] = checks[key]
                if self.rescore(week, now, reason='stat correction'):
                    done.append(week)
        _save_checks(check_updates)
        return done

def _gateway_refusals(decision_id: str) -> int:
    from bm.paths import gateway_logs
    key = ('"decision_id":' + json.dumps(decision_id)).encode()
    n, seen = (0, set())
    for path in gateway_logs(ROOT):
        if not path.exists():
            continue
        with open(path, 'rb') as f:
            for line in f:
                if key not in line or b'"REFUSED"' not in line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                p = row.get('payload') or {}
                if p.get('decision_id') == decision_id and p.get('error') == 'REFUSED' and (row.get('hash') not in seen):
                    seen.add(row.get('hash'))
                    n += 1
    return n

def correction_deadline(week: int) -> datetime:
    return (week_anchor(week) + timedelta(days=9, hours=23, minutes=59)).astimezone(ET)
_CHECKS = ROOT / 'state' / 'rescore-checks.json'

def _load_checks() -> dict:
    try:
        return json.loads(_CHECKS.read_text())
    except (OSError, ValueError):
        return {}

def _save_checks(c: dict) -> None:
    if not c:
        return
    try:
        _CHECKS.parent.mkdir(parents=True, exist_ok=True)
        lock_path = _CHECKS.with_suffix('.lock')
        with lock_path.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            current = _load_checks()
            for key, value in c.items():
                old = current.get(key)
                if old is not None:
                    try:
                        if datetime.fromisoformat(old) >= datetime.fromisoformat(value):
                            continue
                    except (TypeError, ValueError):
                        pass
                current[key] = value
            tmp = _CHECKS.with_suffix(f'.{os.getpid()}.{threading.get_ident()}.tmp')
            tmp.write_text(json.dumps(current))
            os.replace(tmp, _CHECKS)
    except OSError:
        pass

def _parallel(fn, items, workers: int=8):
    out = []
    errs = []
    lock = threading.Lock()

    def wrap(x):
        try:
            r = fn(x)
            with lock:
                out.append(r)
        except Exception as e:
            import traceback
            with lock:
                errs.append((x, traceback.format_exc()))
    ths = []
    for x in items:
        t = threading.Thread(target=wrap, args=(x,))
        t.start()
        ths.append(t)
        if len([t for t in ths if t.is_alive()]) >= workers:
            for t in ths:
                t.join()
    for t in ths:
        t.join()
    if errs:
        for x, tb in errs:
            print(f'[parallel error] {x}: {tb}', flush=True)
        from bm.ops.alert import alert
        alert('decision-crash', f'BenchedMark: {len(errs)} team decision(s) crashed: ' + '; '.join((f'{x}: {tb.strip().splitlines()[-1][:160]}' for x, tb in errs[:3])))
    return out
