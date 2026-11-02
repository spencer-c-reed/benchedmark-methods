from __future__ import annotations
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from bm.gateway.core import Gateway, ModelRequest
from . import prompts
MAX_TOOL_CALLS = 4
HARNESS_VERSION = 'windowed-1'

def extract_json(text: str) -> dict | None:
    if not text:
        return None
    t = text.strip()
    t = re.sub('^```(?:json)?\\s*', '', t)
    t = re.sub('\\s*```$', '', t)

    def unique_keys(pairs):
        d = {}
        for key, value in pairs:
            if key in d:
                raise ValueError('duplicate JSON key')
            d[key] = value
        return d

    def invalid_constant(value):
        raise ValueError('non-JSON numeric constant')
    decoder = json.JSONDecoder(object_pairs_hook=unique_keys, parse_constant=invalid_constant)
    try:
        d = decoder.decode(t)
        return d if isinstance(d, dict) else None
    except (ValueError, RecursionError):
        pass
    start = t.find('{')
    if start == -1 or any((c in t[:start] for c in '[]}')):
        return None
    try:
        d, end = decoder.raw_decode(t, start)
    except (ValueError, RecursionError):
        return None
    suffix = t[end:].strip()
    if suffix and all((c.isspace() or c in '}]' for c in suffix)):
        return d
    if suffix.startswith(',') and isinstance(d.get('action'), dict):
        try:
            notes = decoder.decode('{' + suffix[1:])
        except (ValueError, RecursionError):
            return None
        if len(notes) == 1 and set(notes) <= {'private_notes', 'memo'} and isinstance(next(iter(notes.values())), str):
            return d
        return None
    if any((c in suffix for c in '{}[]')):
        return None
    return d if isinstance(d, dict) else None

def _open_brackets(t: str) -> str:
    stack, in_str, esc = ([], False, False)
    for c in t:
        if in_str:
            if esc:
                esc = False
            elif c == '\\':
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c in '{[':
            stack.append(c)
        elif c in '}]' and stack:
            stack.pop()
    return ''.join(stack)

def json_problem(text: str) -> str:
    if not text or not text.strip():
        return 'your reply was empty'
    t = text.strip()
    t = re.sub('^```(?:json)?\\s*', '', t)
    t = re.sub('\\s*```$', '', t)
    start = t.find('{')
    if start == -1:
        return "your reply contained no JSON object (no '{' found)"
    if any((c in t[:start] for c in '[]}')):
        return f'your reply had other JSON-like text before the object (at char {start}); send the object alone'

    def dup(pairs):
        seen = set()
        for k, _ in pairs:
            if k in seen:
                raise ValueError(f'duplicate JSON key {k!r}')
            seen.add(k)
        return dict(pairs)

    def near(pos: int) -> str:
        return json.dumps(t[max(0, pos - 60):pos + 40])
    try:
        d, end = json.JSONDecoder(object_pairs_hook=dup).raw_decode(t, start)
    except json.JSONDecodeError as e:
        msg = f'JSON parse error: {e.msg} at line {e.lineno} column {e.colno} (char {e.pos} of {len(t)})'
        if e.pos >= len(t):
            still = _open_brackets(t[start:])
            closers = ''.join(('}' if c == '{' else ']' for c in reversed(still)))
            msg += f'; the reply ended before the object was complete (still open at the end: {len(still)} bracket(s), would need {closers!r} in that order)' if still else '; the reply ended before the object was complete'
        return msg + f'; the text there: {near(e.pos)}'
    except (ValueError, RecursionError) as e:
        return f'JSON error: {e}'
    if not isinstance(d, dict):
        return "the reply's top-level JSON value was not an object"
    suffix = t[end:].strip()
    if suffix:
        return f'the JSON object closed at char {end} but more text followed it ({json.dumps(suffix[:80])}); send ONE object and nothing after it (check for an extra or misplaced closing brace)'
    return 'the reply was not a single JSON object'

def parse_retry_message(text: str) -> str:
    return '\n\nYour previous reply could not be used: ' + json_problem(text) + '.\nFix that and reply again with your complete answer as ONE valid JSON object only.'

@dataclass
class DecisionResult:
    parsed: dict | None
    action: dict | None
    forced_default: str | None
    calls: list = field(default_factory=list)
    research: list = field(default_factory=list)
    violations: list = field(default_factory=list)
    retries: int = 0
    reasoning: list = field(default_factory=list)
    journaled: int = 0

class AnswerJournal:

    def __init__(self, path):
        from pathlib import Path
        self.path = Path(path)

    @staticmethod
    def key(decision_id: str, system: str, prompt: str) -> str:
        return hashlib.sha256(f'{decision_id}\x00{system}\x00{prompt}'.encode()).hexdigest()

    def get(self, key: str) -> dict | None:
        if not self.path.exists():
            return None
        from bm.log import ChainLog
        return next((r for r in reversed(ChainLog(self.path).payloads()) if r.get('key') == key), None)

    def put(self, key: str, decision_id: str, r) -> None:
        from bm.log import ChainLog
        ChainLog(self.path).append({'type': 'model_answer', 'ts': datetime.now().astimezone().isoformat(), 'key': key, 'decision_id': decision_id, 'request_id': r.request_id, 'text': r.text, 'reasoning': r.reasoning or {}, 'route_history': r.route_history or [], **{k: getattr(r, k, None) for k in ('usage', 'cost', 'latency_ms', 'model_reported', 'lane', 'model_requested', 'attempts', 'attempt_history', 'attempt_totals', 'identity_source', 'stop_reason', 'gateway_seq', 'prompt_blob', 'system_blob', 'text_blob')}})

    @staticmethod
    def response(stored: dict):
        from types import SimpleNamespace
        return SimpleNamespace(**{k: v for k, v in stored.items() if k not in ('type', 'ts', 'key', 'decision_id', 'seq')}, error=None, error_detail=None, from_journal=True)

class Harness:

    def __init__(self, gateway: Gateway, journal: AnswerJournal | None=None):
        self.gw = gateway
        self.journal = journal

    def decide(self, *, entrant_id: str, decision_id: str, system: str, briefing: str, task: str, tools: 'ToolBox', validate, max_output_tokens: int=3000, allow_research: bool=True, window_s: int=900, hard_deadline: datetime | None=None) -> DecisionResult:
        res = DecisionResult(None, None, None)
        prompt = briefing + '\n\n' + task
        convo = prompt
        research_done = False
        retried = False
        from datetime import timedelta, timezone
        deadline = datetime.now(timezone.utc) + timedelta(seconds=window_s)
        if hard_deadline is not None:
            deadline = min(deadline, hard_deadline)
        for _turn in range(4):
            jkey = self.journal.key(decision_id, system, convo) if self.journal else None
            stored = self.journal.get(jkey) if self.journal else None
            if stored is not None:
                r = AnswerJournal.response(stored)
                res.journaled += 1
            else:
                r = self.gw.call(ModelRequest(entrant_id=entrant_id, decision_id=decision_id, system=system, prompt=convo, max_output_tokens=max_output_tokens, deadline=deadline))
                if datetime.now(timezone.utc) >= deadline:
                    r.error = 'TIMEOUT'
                    r.error_detail = 'response received after the decision deadline; not a model action'
                if self.journal and r.error is None:
                    self.journal.put(jkey, decision_id, r)
            res.calls.extend(r.route_history or [{'request_id': r.request_id, 'error': r.error, 'usage': r.usage, 'cost': r.cost, 'latency_ms': r.latency_ms, 'model_reported': r.model_reported, 'lane': r.lane, 'model_requested': r.model_requested, 'attempts': r.attempts, 'error_detail': r.error_detail, **{k: getattr(r, k, None) for k in ('attempt_history', 'attempt_totals', 'identity_source', 'stop_reason', 'gateway_seq', 'prompt_blob', 'system_blob', 'text_blob')}}])
            if r.error and res.calls and (not res.calls[-1].get('error')):
                res.calls[-1] = dict(res.calls[-1], error=r.error, error_detail=r.error_detail)
            if r.reasoning and r.reasoning.get('kind') not in (None, 'none') and r.reasoning.get('text'):
                res.reasoning.append({'source': 'reasoning_summary' if r.reasoning['kind'] == 'summary' else 'reasoning', 'text': r.reasoning['text'][:6000], 'publishable': bool(r.reasoning.get('publishable'))})
            if r.error:
                res.forced_default = {'QUOTA_YIELD': 'QUOTA_YIELD', 'TIMEOUT': 'TIMEOUT', 'REFUSED': 'REFUSED'}.get(r.error, 'QUOTA_YIELD' if r.error in ('RATE_LIMITED', 'SELF_CAP', 'LANE_DISABLED', 'LANE_BREAKER', 'PAID_REFUSED', 'AUTH') else 'TIMEOUT')
                res.violations.append(f'gateway: {r.error} {r.error_detail or ''}'[:300])
                return res
            d = extract_json(r.text)
            if d is None:
                if not retried:
                    retried = True
                    res.retries += 1
                    convo = prompt + parse_retry_message(r.text)
                    continue
                res.forced_default = 'PARSE'
                res.violations.append('unparseable reply')
                return res
            if 'tool_calls' in d and 'action' not in d:
                if research_done or not allow_research:
                    convo = convo + '\n\nResearch is closed for this decision. Give your final JSON answer now.'
                    research_done = True
                    continue
                research_done = True
                calls = (d.get('tool_calls') or [])[:MAX_TOOL_CALLS]
                out = []
                for c in calls:
                    name, args = (str(c.get('tool', '')), c.get('args') or {})
                    result = tools.run(name, args)
                    res.research.append({'tool': name, 'query': json.dumps(args)[:200], 'result_summary': result[:300], 'tokens': len(result) // 4})
                    out.append(f'## {name} {json.dumps(args)}\n{result}')
                convo = prompt + '\n\nRESEARCH RESULTS (your one research turn)\n' + '\n\n'.join(out) + '\n\nNow give your final JSON answer (no more tool calls).'
                continue
            action = d.get('action')
            vs = validate(action) if isinstance(action, dict) else ['missing or malformed action object']
            if vs:
                res.violations.extend(vs)
                if not retried:
                    retried = True
                    res.retries += 1
                    convo = prompt + '\n\nYour previous answer was illegal:\n- ' + '\n- '.join(vs[:8]) + f'\nPrevious action: {json.dumps(action)[:1500]}\nFix it and reply with ONE JSON object.'
                    continue
                res.parsed = d
                res.forced_default = 'PARSE'
                return res
            res.parsed = d
            res.action = action
            return res
        res.forced_default = res.forced_default or 'PARSE'
        return res

class ToolBox:

    def __init__(self, view, state, names: dict, league_events: list):
        self.v = view
        self.s = state
        self.names = names
        self.events = league_events

    def run(self, name: str, args: dict) -> str:
        try:
            if name == 'player_report':
                return self.player_report([str(x) for x in args.get('ids') or []][:6])
            if name == 'free_agents':
                return self.free_agents(str(args.get('pos', 'RB')).upper(), min(int(args.get('n', 15)), 25))
            if name == 'team_roster':
                return self.team_roster(str(args.get('team_id', '')))
            if name == 'transactions':
                return self.transactions(min(int(args.get('n', 15)), 30))
        except Exception as e:
            return f'error: {e}'
        return f'unknown tool {name!r}'

    def player_report(self, ids: list[str]) -> str:
        from bm.stats import nflverse as nv
        from bm.league.context import SOURCE_PROVENANCE_FROM
        from bm.engine.scoring import score_player_line
        source_at = self.v.as_of if self.v.as_of >= SOURCE_PROVENANCE_FROM else None
        prior = nv.prior_lines(2026, source_at) if source_at else nv.prior_lines(2026)
        rows = []
        for pid in ids:
            p = self.v.world.players.get(pid)
            if not p:
                rows.append(f'{pid}: unknown player')
                continue
            log = []
            for w in sorted(self.v.lines):
                if pid in self.v.lines[w]:
                    log.append(f'w{w}:{score_player_line(self.v.rules, p['pos'], self.v.lines[w][pid]):.1f}')
                else:
                    log.append(f'w{w}:-')
            pl = prior.get(pid, [])
            ppg = sum((score_player_line(self.v.rules, p['pos'], r) for r in pl)) / len(pl) if pl else None
            inj = [f'{r['report_status'] or '-'} ({r['report_primary_injury'] or r['practice_primary_injury'] or ''}; practice: {r['practice_status'] or '-'}; seen {r['first_seen_at'][:16]})' for r in self.v.injuries if r.get('gsis_id') == pid]
            nxt = []
            for g in sorted(nv.schedule(2026, source_at) if source_at else nv.schedule(2026), key=lambda g: g['kickoff']):
                if g['week'] >= self.v.week and p['team'] in (g['home'], g['away']) and (len(nxt) < 3):
                    nxt.append(f'w{g['week']} ' + ('@' + g['home'] if g['away'] == p['team'] else g['away']))
            owner = next((t for t, tm in self.s['teams'].items() if pid in tm['roster']), 'free agent')
            ppg_s = f'{ppg:.1f}' if ppg is not None else 'n/a'
            rows.append(f'{self.v.ptoken(pid)} | owner {owner} | log {' '.join(log)} | 2025 ppg {ppg_s} | injury {'; '.join(inj) or 'none listed'} | next {', '.join(nxt)}')
        return '\n'.join(rows)

    def free_agents(self, pos: str, n: int) -> str:
        owned = {p for t in self.s['teams'].values() for p in t['roster']}
        fa = [pid for pid, p in self.v.world.players.items() if p['pos'] == pos and pid not in owned]
        fa.sort(key=lambda x: -self.v.proj.get(x, {}).get('value', 0))
        return '\n'.join((self.v.ptoken(x) for x in fa[:n])) or 'none'

    def team_roster(self, tid: str) -> str:
        t = self.s['teams'].get(tid)
        if not t:
            return f'unknown team {tid}'
        return f'{tid} {self.names.get(tid, '')} FAAB ${t['faab']}\n' + '\n'.join((self.v.ptoken(p) for p in t['roster']))

    def transactions(self, n: int) -> str:
        rows = []
        for e in self.events:
            if e['type'] in ('waiver_award', 'drop', 'trade_executed_note', 'chatter') or (e['type'] == 'trade_msg' and e.get('action') in ('ACCEPT',)):
                rows.append(_txn_line(e, self.v.world.players))
        return '\n'.join(rows[-n:]) or 'none yet'

def _txn_line(e: dict, players: dict) -> str:
    nm = lambda pid: players.get(pid, {}).get('name', pid) if pid else '-'
    if e['type'] == 'waiver_award':
        return f'{e['ts'][:10]} waiver: {e['team']} added {nm(e['add'])} (${e['amount']}), dropped {nm(e.get('drop'))}'
    if e['type'] == 'drop':
        return f'{e['ts'][:10]} drop: {e['team']} dropped {nm(e['player'])}'
    if e['type'] == 'chatter':
        return f'{e['ts'][:10]} chat {e['team']}: {e['text'][:200]}'
    return f'{e['ts'][:10]} trade accepted in {e.get('thread_id')}'

def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()
