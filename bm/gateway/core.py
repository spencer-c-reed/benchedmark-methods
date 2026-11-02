from __future__ import annotations
import json
import os
import random
import subprocess
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import timedelta
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import yaml
from bm.log import ChainLog, put_blob
from bm.paths import runtime_root
ROOT = runtime_root(Path(__file__).resolve().parents[2])
ET = ZoneInfo('America/New_York')
STATE = ROOT / 'state'
BREAKERS = STATE / 'breakers'

@dataclass
class ModelRequest:
    entrant_id: str
    decision_id: str
    system: str
    prompt: str
    max_output_tokens: int = 4000
    effort: str | None = None
    deadline: datetime | None = None
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    priority: str = 'bench'
    allowed_pricing: tuple[str, ...] | None = None
    decision_deadline: datetime | None = None
    no_fallback: bool = False
    track: str | None = None
    manifest: dict | None = None
    pin_lane: str | None = None

@dataclass
class ModelResponse:
    request_id: str
    entrant_id: str
    lane: str
    surface: str
    model_requested: str
    model_reported: str | None
    text: str
    reasoning: dict
    usage: dict
    cost: dict
    latency_ms: int
    raw_blob: str | None
    error: str | None = None
    error_detail: str | None = None
    attempts: int = 1
    retry_after_s: float | None = None
    attempt_history: list[dict] = field(default_factory=list)
    attempt_totals: dict = field(default_factory=dict)
    identity_source: str = 'unknown'
    stop_reason: str | None = None
    refusal_category: str | None = None
    gateway_seq: int | None = None
    prompt_blob: str | None = None
    system_blob: str | None = None
    text_blob: str | None = None
    route_history: list[dict] = field(default_factory=list)
    upstream_provider: str | None = None
    generation_id: str | None = None
    provider_order: list | None = None
    tool_proof: dict | None = None
CLI_SURFACES = frozenset({'claude-cli', 'codex-cli', 'agy-cli', 'opencode-zen'})
REFUSED_MAX_ATTEMPTS = 1

class GatewayError(Exception):

    def __init__(self, code: str, detail: str=''):
        super().__init__(f'{code}: {detail}')
        self.code = code
        self.detail = detail

def load_yaml(name: str):
    return yaml.safe_load((ROOT / 'config' / name).read_text())

def model_daily_cap(lane: dict, model: str, day: str | None=None):
    day = day or datetime.now(ET).date().isoformat()
    for k, caps in (lane.get('model_daily_usd_by_day') or {}).items():
        if str(k) == day and isinstance(caps, dict) and (model in caps):
            return caps[model]
    caps = lane.get('model_daily_usd') or {}
    return caps.get(model, caps.get('default'))

def model_timeout_s(lane: dict, model: str, default: float=300) -> float:
    per = lane.get('model_timeout_s') or {}
    return float(per[model]) if isinstance(per, dict) and per.get(model) is not None else float(lane.get('timeout_s', default))
_PRICES = None

def usd_eq(model: str, usage: dict) -> float | None:
    global _PRICES
    if _PRICES is None:
        _PRICES = load_yaml('prices.yaml') or {}
    p = _PRICES.get(model)
    if not p:
        return None
    cached = usage.get('cached_input') or 0
    unc = max((usage.get('input') or 0) - cached, 0)
    writes = min(max(usage.get('cache_creation_input') or 0, 0), unc)
    writes_1h = min(max(usage.get('cache_creation_1h_input') or 0, 0), writes)
    out = usage.get('output') or 0
    return round(((unc - writes) * p['input'] + (writes - writes_1h) * p.get('cache_write', 1.25 * p['input']) + writes_1h * p.get('cache_write_1h', 2 * p['input']) + cached * p.get('cache_read', p['input']) + out * p['output']) / 1000000.0, 6)

class Ledger:
    STRIKE_DECAY_S = 600.0

    def __init__(self, path: Path=STATE / 'gateway.sqlite'):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self._db() as db:
            db.execute('create table if not exists calls (day text, lane text, n integer, primary key(day, lane))')
            db.execute('create table if not exists cooldown (key text primary key, until real, strikes integer)')
            db.execute('create table if not exists pace (key text, ts real, tokens integer)')
            db.execute('create table if not exists spend (day text, key text, usd real, primary key(day, key))')
            db.execute('create table if not exists cap_override (day text, lane text, cap integer, why text, at text, primary key(day, lane))')
            db.execute('create table if not exists route_health (key text primary key, streak integer, last_try real)')

    def _db(self):
        db = sqlite3.connect(self.path, timeout=60, isolation_level=None)
        db.execute('pragma journal_mode=wal')
        return db

    def reserve(self, lane: str, cap: int) -> int:
        day = datetime.now(ET).date().isoformat()
        db = self._db()
        try:
            db.execute('begin immediate')
            row = db.execute('select n from calls where day=? and lane=?', (day, lane)).fetchone()
            n = row[0] if row else 0
            ov = db.execute('select cap from cap_override where day=? and lane=?', (day, lane)).fetchone()
            eff = max(int(cap), int(ov[0])) if ov else int(cap)
            if n >= eff:
                db.execute('rollback')
                raise GatewayError('SELF_CAP', f'{lane} reached OUR daily_call_cap {eff} (config/lanes.yaml' + (f', raised today from {cap}' if eff != int(cap) else '') + f'; {n} calls today): not a provider limit')
            db.execute('insert into calls(day,lane,n) values(?,?,1) on conflict(day,lane) do update set n=n+1', (day, lane))
            db.execute('commit')
            return n + 1
        finally:
            db.close()

    def cooldown_left(self, key: str) -> float:
        db = self._db()
        try:
            row = db.execute('select until from cooldown where key=?', (key,)).fetchone()
        finally:
            db.close()
        return max(0.0, row[0] - time.time()) if row else 0.0

    def strike(self, key: str, base_s: float, cap_s: float, hint_s: float | None=None) -> float:
        db = self._db()
        try:
            db.execute('begin immediate')
            row = db.execute('select strikes, until from cooldown where key=?', (key,)).fetchone()
            now = time.time()
            n = (row[0] if row and now - row[1] < self.STRIKE_DECAY_S else 0) + 1
            if hint_s is not None and hint_s > 0:
                wait = min(float(cap_s), float(hint_s)) + random.uniform(0.5, 2.0)
            else:
                wait = min(float(cap_s), float(base_s) * 2 ** (n - 1)) * random.uniform(0.85, 1.15)
            db.execute('insert into cooldown(key,until,strikes) values(?,?,?) on conflict(key) do update set until=excluded.until, strikes=excluded.strikes', (key, now + wait, n))
            db.execute('commit')
            return wait
        finally:
            db.close()

    def hold(self, key: str, seconds: float) -> None:
        db = self._db()
        try:
            db.execute('insert into cooldown(key,until,strikes) values(?,?,0) on conflict(key) do update set until=max(until, excluded.until)', (key, time.time() + float(seconds)))
        finally:
            db.close()

    def clear(self, key: str) -> None:
        db = self._db()
        try:
            db.execute('delete from cooldown where key=?', (key,))
        finally:
            db.close()

    def note_route(self, key: str, error: str | None) -> None:
        db = self._db()
        try:
            if error is None:
                db.execute('insert into route_health(key,streak,last_try) values(?,0,?) on conflict(key) do update set streak=0, last_try=excluded.last_try', (key, time.time()))
            elif error == 'TRANSIENT':
                db.execute('insert into route_health(key,streak,last_try) values(?,1,?) on conflict(key) do update set streak=streak+1, last_try=excluded.last_try', (key, time.time()))
            else:
                db.execute('insert into route_health(key,streak,last_try) values(?,0,?) on conflict(key) do update set last_try=excluded.last_try', (key, time.time()))
        finally:
            db.close()

    def dead_route(self, key: str, streak_min: int, probe_every_s: float) -> dict | None:
        db = self._db()
        try:
            row = db.execute('select streak, last_try from route_health where key=?', (key,)).fetchone()
        finally:
            db.close()
        if not row or row[0] < streak_min:
            return None
        since = time.time() - row[1]
        return {'streak': row[0], 'probe_in_s': round(probe_every_s - since, 1)} if since < probe_every_s else None

    def pace(self, key: str, rpm: int | None, tpm: int | None, tokens: int, max_wait: float) -> float:
        waited = 0.0
        while True:
            db = self._db()
            try:
                db.execute('begin immediate')
                now = time.time()
                db.execute('delete from pace where ts < ?', (now - 60,))
                rows = db.execute('select ts, tokens from pace where key=? order by ts', (key,)).fetchall()

                def fits(rs):
                    return (not rpm or len(rs) < rpm) and (not tpm or not rs or sum((t for _, t in rs)) + tokens <= tpm)
                if fits(rows):
                    db.execute('insert into pace(key, ts, tokens) values(?,?,?)', (key, now, int(tokens)))
                    db.execute('commit')
                    return waited
                db.execute('rollback')
                k = next((i for i in range(1, len(rows) + 1) if fits(rows[i:])))
                wake = rows[k - 1][0] + 60.0 - now + random.uniform(0.5, 1.5)
            finally:
                db.close()
            if waited + wake > max_wait:
                raise GatewayError('RATE_LIMITED', f'{key} pace: next free-tier slot in {wake:.0f}s, outside the decision window; no request sent')
            time.sleep(wake)
            waited += wake
    CLAIM_S = 15.0

    def spread_pick(self, routes: list[tuple[str, dict]]) -> list[str]:
        db = self._db()
        try:
            db.execute('create table if not exists spread_claim (key text, ts real)')
            db.execute('begin immediate')
            now = time.time()
            db.execute('delete from spread_claim where ts < ?', (now - self.CLAIM_S,))
            scored = []
            for key, info in routes:
                n, tok = db.execute('select count(*), coalesce(sum(tokens), 0) from pace where key=? and ts >= ?', (key, now - 60)).fetchone()
                claims = db.execute('select count(*) from spread_claim where key=?', (key,)).fetchone()[0]
                rpm, tpm = (info.get('rpm'), info.get('tpm'))
                frac = max(n / float(rpm) if rpm else 0.0, tok / float(tpm) if tpm else 0.0)
                scored.append(((1 if info.get('down') else 0, 1 if info.get('cooling') else 0, int(info.get('busy', 0)) + int(claims), round(frac, 2), random.random()), key))
            scored.sort()
            order = [k for _, k in scored]
            if order and (not scored[0][0][0]):
                db.execute('insert into spread_claim(key, ts) values(?, ?)', (order[0], now))
            db.execute('commit')
            return order
        finally:
            db.close()

    def spent(self, key: str, day: str | None=None) -> float:
        day = day or datetime.now(ET).date().isoformat()
        db = self._db()
        try:
            row = db.execute('select usd from spend where day=? and key=?', (day, key)).fetchone()
            return float(row[0]) if row else 0.0
        finally:
            db.close()

    def add_spend(self, key: str, usd: float) -> None:
        day = datetime.now(ET).date().isoformat()
        db = self._db()
        try:
            db.execute('begin immediate')
            for d in (day, 'ALL'):
                db.execute('insert into spend(day,key,usd) values(?,?,?) on conflict(day,key) do update set usd=usd+?', (d, key, usd, usd))
            db.execute('commit')
        finally:
            db.close()

    def set_cap_override(self, lane: str, cap: int, why: str) -> None:
        day = datetime.now(ET).date().isoformat()
        db = self._db()
        try:
            db.execute('insert into cap_override(day,lane,cap,why,at) values(?,?,?,?,?) on conflict(day,lane) do update set cap=max(cap, excluded.cap), why=excluded.why, at=excluded.at', (day, lane, int(cap), why[:300], datetime.now(timezone.utc).isoformat()))
        finally:
            db.close()

    def cap_overrides(self) -> dict[str, int]:
        day = datetime.now(ET).date().isoformat()
        db = self._db()
        try:
            return {l: int(c) for l, c in db.execute('select lane, cap from cap_override where day=?', (day,))}
        finally:
            db.close()

    def today(self) -> dict[str, int]:
        day = datetime.now(ET).date().isoformat()
        db = self._db()
        try:
            return {l: n for l, n in db.execute('select lane, n from calls where day=?', (day,))}
        finally:
            db.close()

class FileSlots:

    def __init__(self, key: str, n: int, root: Path | None=None):
        self.root = root or STATE / 'slots'
        self.root.mkdir(parents=True, exist_ok=True)
        safe = ''.join((c if c.isalnum() or c in '-_.' else '_' for c in key))
        self.paths = [self.root / f'{safe}.{i}.lock' for i in range(max(1, int(n)))]
        self._held = threading.local()

    def acquire(self, timeout: float | None=None, priority: float | None=None) -> bool:
        import fcntl
        end = None if timeout is None else time.time() + max(0.0, timeout)
        me = self._enqueue(priority, end) if os.environ.get('BM_LANE_PRIORITY', '1') != '0' else None
        try:
            while True:
                if me is not None:
                    try:
                        os.utime(me)
                    except FileNotFoundError:
                        me.touch()
                if me is None or self._my_turn(me):
                    for p in random.sample(self.paths, len(self.paths)):
                        f = open(p, 'a')
                        try:
                            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            f.close()
                            continue
                        stack = getattr(self._held, 'stack', None)
                        if stack is None:
                            stack = self._held.stack = []
                        stack.append(f)
                        return True
                if end is not None and time.time() >= end:
                    return False
                time.sleep(random.uniform(0.2, 0.6))
        finally:
            if me is not None:
                try:
                    me.unlink()
                except OSError:
                    pass

    def _queue_dir(self) -> Path:
        d = self.root / (self.paths[0].name.rsplit('.', 2)[0] + '.waiters')
        d.mkdir(parents=True, exist_ok=True)
        return d
    LEASE_S = 30.0

    def _enqueue(self, priority: float | None, end: float | None) -> Path:
        now = time.time()
        pr = priority if priority is not None else now + 1000000000.0
        name = f'{pr:017.3f}-{now:017.6f}-{os.getpid()}-{threading.get_ident()}'
        p = self._queue_dir() / name
        p.touch()
        return p

    def _my_turn(self, me: Path) -> bool:
        now = time.time()
        live = []
        for q in sorted(self._queue_dir().iterdir()):
            try:
                stale = q.stat().st_mtime < now - self.LEASE_S
            except FileNotFoundError:
                continue
            if stale:
                try:
                    q.unlink()
                except OSError:
                    pass
                continue
            live.append(q.name)
        return me.name in live[:len(self.paths)]

    def busy(self) -> int:
        import fcntl
        held = 0
        for p in self.paths:
            try:
                f = open(p, 'a')
            except OSError:
                continue
            try:
                fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
                fcntl.flock(f, fcntl.LOCK_UN)
            except BlockingIOError:
                held += 1
            finally:
                f.close()
        now = time.time()
        waiting = 0
        try:
            for q in self._queue_dir().iterdir():
                try:
                    if q.stat().st_mtime >= now - self.LEASE_S:
                        waiting += 1
                except FileNotFoundError:
                    pass
        except OSError:
            pass
        return held + waiting

    def release(self) -> None:
        import fcntl
        f = self._held.stack.pop()
        try:
            fcntl.flock(f, fcntl.LOCK_UN)
        finally:
            f.close()

def validate_config(lanes: object, entrants: object, adapter_names) -> list[str]:
    errs = []
    if not isinstance(lanes, list) or not lanes:
        return ['lanes.yaml is not a non-empty list']
    names = set()
    for l in lanes:
        if not isinstance(l, dict) or not isinstance(l.get('lane'), str):
            errs.append(f'lane entry without a name: {str(l)[:80]}')
            continue
        n = l['lane']
        names.add(n)
        if l.get('adapter') not in adapter_names and l.get('enabled') is not False:
            errs.append(f'{n}: unknown adapter {l.get('adapter')!r}')
        if not isinstance(l.get('models'), list) or not l['models']:
            errs.append(f'{n}: models must be a non-empty list')
        try:
            if int(l.get('daily_call_cap')) < 0:
                errs.append(f'{n}: negative daily_call_cap')
        except (TypeError, ValueError):
            errs.append(f'{n}: daily_call_cap must be an integer')
        by_day = l.get('model_daily_usd_by_day')
        if by_day is not None and (not (isinstance(by_day, dict) and all((isinstance(c, dict) and all((isinstance(v, (int, float)) and (not isinstance(v, bool)) and (v >= 0) for v in c.values())) for c in by_day.values())))):
            errs.append(f'{n}: model_daily_usd_by_day must be {{day: {{model: usd >= 0}}}}')
        mt = l.get('model_timeout_s')
        if mt is not None and (not (isinstance(mt, dict) and all((isinstance(v, (int, float)) and (not isinstance(v, bool)) and (v > 0) for v in mt.values())))):
            errs.append(f'{n}: model_timeout_s must be {{model: seconds > 0}}')
    if not isinstance(entrants, list):
        return errs + ['entrants.yaml is not a list']
    for e in entrants:
        if not isinstance(e, dict) or 'id' not in e:
            errs.append(f'entrant without an id: {str(e)[:80]}')
            continue
        if e.get('kind') == 'baseline':
            continue
        for ln in [e.get('lane')] + [f.get('lane') for k in ('fallback', 'spread') for f in e.get(k) or [] if isinstance(f, dict)]:
            if ln not in names:
                errs.append(f'entrant {e['id']}: unknown lane {ln!r}')
    return errs

class Gateway:
    RELOAD_CHECK_S = 5.0

    def __init__(self, log_path: Path=ROOT / 'logs' / 'gateway.jsonl'):
        from . import adapters
        self.adapters = {'claude_cli': adapters.ClaudeCLI(), 'codex_cli': adapters.CodexCLI(), 'opencode': adapters.OpenCode(), 'openai_compat': adapters.OpenAICompat(), 'agy_cli': adapters.AgyCLI(), 'anthropic_api': adapters.AnthropicAPI()}
        self.log = ChainLog(log_path)
        self.ledger = Ledger()
        self._reload_lock = threading.Lock()
        self._cfg_mtimes = self._config_mtimes()
        self._apply_config(load_yaml('lanes.yaml'), load_yaml('entrants.yaml'))
    CONFIG_FILES = ('lanes.yaml', 'entrants.yaml')

    def _config_mtimes(self) -> tuple:
        out = []
        for f in self.CONFIG_FILES:
            try:
                out.append((ROOT / 'config' / f).stat().st_mtime_ns)
            except OSError:
                out.append(None)
        return tuple(out)

    def _apply_config(self, lanes: list, entrants: list) -> None:

        def _mc(l, m):
            mc = l.get('model_concurrency')
            return int(mc.get(m, mc.get('default', l.get('concurrency', 1)))) if isinstance(mc, dict) else int(mc)
        new_lanes = {l['lane']: l for l in lanes}
        sems = {name: FileSlots(name, int(l.get('concurrency', 1))) for name, l in new_lanes.items()}
        msems = {(name, m): FileSlots(f'{name}:{m}', _mc(l, m)) for name, l in new_lanes.items() if l.get('model_concurrency') for m in l.get('models', [])}
        self._sems, self._msems = (sems, msems)
        self.lanes = new_lanes
        self.entrants = {e['id']: e for e in entrants}

    def maybe_reload(self, force: bool=False) -> bool:
        now = time.time()
        if not force and now - getattr(self, '_reload_checked', 0.0) < self.RELOAD_CHECK_S:
            return False
        lock = getattr(self, '_reload_lock', None)
        if lock is None or not lock.acquire(blocking=False):
            return False
        try:
            self._reload_checked = now
            m = self._config_mtimes()
            if m == getattr(self, '_cfg_mtimes', None):
                return False
            self._cfg_mtimes = m
            try:
                lanes, ents = (load_yaml('lanes.yaml'), load_yaml('entrants.yaml'))
                errs = validate_config(lanes, ents, self.adapters)
            except Exception as e:
                errs = [f'{type(e).__name__}: {str(e)[:200]}']
            if errs:
                msg = 'BenchedMark gateway: refused an edit of config/lanes.yaml or entrants.yaml; the running config stays. ' + '; '.join(errs)[:600]
                print(msg, flush=True)
                try:
                    from bm.ops.alert import alert
                    alert('config', msg)
                except Exception:
                    pass
                return False
            self._apply_config(lanes, ents)
            print(f'[gateway] reloaded config/lanes.yaml + entrants.yaml at {datetime.now(ET):%H:%M:%S} ET', flush=True)
            return True
        finally:
            lock.release()

    def spend_guard(self, lane: dict, model: str) -> None:
        name = lane['lane']
        if lane.get('enabled') is False:
            raise GatewayError('LANE_DISABLED', name)
        if (BREAKERS / f'{name}.tripped').exists():
            raise GatewayError('LANE_BREAKER', f'{name} breaker tripped; see {BREAKERS}/{name}.tripped')
        pricing = lane.get('pricing')
        if pricing == 'paid' and (not (ROOT / 'config' / 'spend-authorizations' / f'{name}.yaml').exists()):
            raise GatewayError('PAID_REFUSED', f'{name} is a paid lane with no spend authorization')
        if pricing not in ('free', 'subscription', 'sponsor', 'paid'):
            raise GatewayError('PAID_REFUSED', f'{name} has unknown pricing {pricing!r}')
        if model not in lane.get('models', []):
            raise GatewayError('PAID_REFUSED', f'model {model} is not on the {name} allowlist')

    def money_guard(self, lane: dict, model: str) -> None:
        cap = model_daily_cap(lane, model)
        if cap is not None and self.ledger.spent(f'{lane['lane']}:{model}') >= float(cap):
            raise GatewayError('SELF_CAP', f'{model} reached OUR money cap model_daily_usd ${cap}/day on {lane['lane']} (config/lanes.yaml): not a provider limit; money caps are never raised automatically')
        if lane.get('budget_usd') is not None and self.ledger.spent(lane['lane'], 'ALL') >= float(lane['budget_usd']):
            raise GatewayError('SELF_CAP', f'{lane['lane']} used OUR lifetime money cap budget_usd ${lane['budget_usd']} (config/lanes.yaml)')

    def record_money(self, lane: dict, model: str, usd: float) -> None:
        name = lane['lane']
        before = self.ledger.spent(name, 'ALL')
        self.ledger.add_spend(f'{name}:{model}', usd)
        self.ledger.add_spend(name, usd)
        b = lane.get('budget_usd')
        if b:
            frac = float(lane.get('alert_at_fraction', 0.5))
            after = before + usd
            if before < frac * float(b) <= after:
                from bm.ops.alert import alert
                alert('spend', f'BenchedMark: {name} has used ${after:.2f} of its ${float(b):.2f} budget ({int(frac * 100)}% alert).')

    def trip(self, lane: str, why: str) -> None:
        BREAKERS.mkdir(parents=True, exist_ok=True)
        (BREAKERS / f'{lane}.tripped').write_text(json.dumps({'at': datetime.now(timezone.utc).isoformat(), 'why': why}))
    FALLBACK_ON = ('RATE_LIMITED', 'QUOTA_YIELD', 'SELF_CAP', 'LANE_DISABLED', 'LANE_BREAKER', 'AUTH', 'TIMEOUT', 'TRANSIENT', 'PROVIDER_GONE')

    def route_guard(self):
        g = getattr(self, '_route_guard', None)
        if g is None:
            from bm.ops.freeze_guard import RouteGuard
            g = self._route_guard = RouteGuard(ROOT, Path(__file__).resolve().parents[2])
        return g

    def _manifest_refused(self, req: ModelRequest, ent: dict, why: str) -> ModelResponse:
        resp = ModelResponse(req.request_id, req.entrant_id, ent.get('lane') or '-', 'manifest', ent.get('model') or '-', None, '', {'kind': 'none'}, {}, {'billed_usd': 0.0}, 0, None, 'MANIFEST_REFUSED', why[:400], 0)
        self._log(req, ent, resp, 0)
        return resp

    def call(self, req: ModelRequest) -> ModelResponse:
        self.maybe_reload()
        ent = self.entrants.get(req.entrant_id)
        if not ent or ent.get('kind') == 'baseline':
            raise GatewayError('NO_ENTRANT', req.entrant_id)
        routes = self._spread_order(ent) + ([] if req.no_fallback else [(f['lane'], f['model']) for f in ent.get('fallback') or []])
        if req.pin_lane:
            routes = [r for r in routes if r[0] == req.pin_lane]
            if not routes:
                raise GatewayError('NO_ROUTE', f'{req.entrant_id} declares no route on lane {req.pin_lane}')
        from bm.ops.freeze_guard import ManifestRefused
        try:
            scope = self.route_guard().scope(req.decision_id, req.entrant_id, req.track, req.effort)
        except ManifestRefused as e:
            return self._manifest_refused(req, ent, str(e))
        if scope is not None:
            configured = set(routes)
            if scope.routes[0] not in configured:
                return self._manifest_refused(req, ent, f'frozen surface {scope.routes[0]} is no longer a configured route of {req.entrant_id}')
            routes = [r for r in scope.routes if r in configured]
            req = replace(req, effort=scope.effort or 'vendor-default', manifest={'sha256': scope.manifest_sha256, 'path': scope.manifest, 'track': scope.track})
        if req.allowed_pricing is not None:
            routes = [(ln, model) for ln, model in routes if self.lanes.get(ln, {}).get('pricing') in req.allowed_pricing]
            if not routes:
                raise GatewayError('PRICING_NOT_ALLOWED', 'no route satisfies request pricing restriction')
        from .adapters import OFFLINE_DECISION_PREFIXES
        if str(req.decision_id).startswith(OFFLINE_DECISION_PREFIXES):
            barred = [r for r in routes if self.lanes.get(r[0], {}).get('adapter') == 'anthropic_api']
            routes = [r for r in routes if r not in barred]
            if not routes:
                resp = ModelResponse(req.request_id, req.entrant_id, barred[0][0], 'anthropic-api', barred[0][1], None, '', {'kind': 'none'}, {}, {'billed_usd': 0.0}, 0, None, 'LANE_DISABLED', 'offline work (replay/canary/recovery) never uses the Claude API credits lane', 0)
                self._log(req, ent, resp, 0)
                return resp
        if req.deadline and len(routes) > 1:
            win = (req.deadline - datetime.now(timezone.utc)).total_seconds()
            routes = routes[:1] + [r for r in routes[1:] if self.ledger.cooldown_left(f'{r[0]}:{r[1]}') < win]
        resp = None
        route_history = []
        skipped: list[dict] = []

        def finish(response):
            response.route_history = list(route_history)
            if not route_history:
                return response
            totals = [r.get('attempt_totals') or {} for r in route_history if r.get('attempt_history')]
            keys = {k for total in totals for k in total.get('usage') or {}}
            response.attempt_totals = {'usage': {k: sum(((t.get('usage') or {}).get(k) or 0 for t in totals)) for k in keys}, 'billed_usd': sum((t.get('billed_usd') or 0 for t in totals)), 'usd_eq_observed_lower_bound': sum((t.get('usd_eq_observed_lower_bound', t.get('usd_eq')) or 0 for t in totals)), 'usd_eq': sum((t['usd_eq'] for t in totals)) if all((t.get('usd_eq') is not None for t in totals)) else None, 'provider_latency_ms': sum((t.get('provider_latency_ms') or 0 for t in totals)), 'scope': 'gateway_call_all_routes', 'billed_cost_complete': all((t.get('billed_cost_complete', True) for t in totals)), 'usage_complete': all((t.get('usage_complete', True) for t in totals))}
            return response
        for i, (lane_name, model) in enumerate(routes):
            rest = routes[i + 1:]
            if scope is None and rest:
                skip = self._cooling_skip(req, lane_name, model, rest) or self._dead_route_skip(req, lane_name, model, rest)
                if skip:
                    skipped.append(skip)
                    continue
            sub = req
            if rest and req.deadline:
                nxt = self.lanes[rest[0][0]]
                hold = max(float(nxt.get('min_attempt_s', 20)), float(ent.get('fallback_reserve_s', 120)))
                sub = replace(req, deadline=req.deadline - timedelta(seconds=hold), decision_deadline=req.decision_deadline or req.deadline)
                if (sub.deadline - datetime.now(timezone.utc)).total_seconds() < float(self.lanes[lane_name].get('min_attempt_s', 20)):
                    continue
            resp = self._route(sub, ent, self.lanes[lane_name], model, i, skipped=list(skipped))
            route_history.extend(getattr(resp, 'route_history', []) or [])
            if resp.error is None or resp.error not in self.FALLBACK_ON or (not rest):
                return finish(resp)
            if req.deadline and (req.deadline - datetime.now(timezone.utc)).total_seconds() < float(self.lanes[rest[0][0]].get('min_attempt_s', 20)):
                return finish(resp)
        return finish(resp) if resp is not None else resp
    COOLDOWN_SKIP_S = 120.0

    def _cooling_skip(self, req: ModelRequest, lane_name: str, model: str, rest: list) -> dict | None:
        lane = self.lanes.get(lane_name) or {}
        if lane.get('pricing') != 'free':
            return None
        key = f'{lane_name}:{model}'
        cool = self.ledger.cooldown_left(key)
        if cool <= 0:
            return None
        win = (req.deadline - datetime.now(timezone.utc)).total_seconds() if req.deadline else float('inf')
        thr = float(lane.get('cooldown_skip_s', self.COOLDOWN_SKIP_S))
        if cool <= min(thr, win):
            return None
        for ln, m in rest:
            nl = self.lanes.get(ln)
            if nl is None or nl.get('enabled') is False or (BREAKERS / f'{ln}.tripped').exists():
                continue
            if self.ledger.cooldown_left(f'{ln}:{m}') <= min(float(nl.get('cooldown_skip_s', self.COOLDOWN_SKIP_S)), win):
                return {'lane': lane_name, 'model': model, 'cooldown_left_s': round(cool, 1), 'threshold_s': thr, 'window_left_s': None if win == float('inf') else round(win, 1), 'cause': 'shared_429_cooldown'}
        return None
    DEAD_ROUTE_STREAK = 5
    DEAD_ROUTE_PROBE_S = 900.0

    def _dead_route_skip(self, req: ModelRequest, lane_name: str, model: str, rest: list) -> dict | None:
        lane = self.lanes.get(lane_name) or {}
        if lane.get('pricing') != 'free':
            return None
        key = f'{lane_name}:{model}'
        dead = self.ledger.dead_route(key, int(lane.get('dead_route_streak', self.DEAD_ROUTE_STREAK)), float(lane.get('dead_route_probe_s', self.DEAD_ROUTE_PROBE_S)))
        if not dead:
            return None
        win = (req.deadline - datetime.now(timezone.utc)).total_seconds() if req.deadline else float('inf')
        for ln, m in rest:
            nl = self.lanes.get(ln)
            if nl is None or nl.get('enabled') is False or (BREAKERS / f'{ln}.tripped').exists():
                continue
            if self.ledger.cooldown_left(f'{ln}:{m}') <= min(float(nl.get('cooldown_skip_s', self.COOLDOWN_SKIP_S)), win):
                return {'lane': lane_name, 'model': model, 'cause': 'dead_free_route', **dead}
        return None

    def _spread_order(self, ent: dict) -> list[tuple[str, str]]:
        primary = (ent['lane'], ent['model'])
        spread = [(s['lane'], s['model']) for s in ent.get('spread') or []]
        if not spread:
            return [primary]
        cands = list(dict.fromkeys([primary] + spread))
        info = []
        for lane_name, model in cands:
            lane = self.lanes.get(lane_name)
            key = f'{lane_name}:{model}'
            if lane is None or lane.get('enabled') is False or (BREAKERS / f'{lane_name}.tripped').exists():
                info.append((key, {'down': True}))
                continue
            busy = 0
            for sm in (self._sems.get(lane_name), self._msems.get((lane_name, model))):
                if sm is not None and hasattr(sm, 'busy'):
                    busy += sm.busy()
            info.append((key, {'rpm': lane.get('rpm'), 'tpm': lane.get('tpm'), 'busy': busy, 'cooling': self.ledger.cooldown_left(key) > 0}))
        by_key = {f'{l}:{m}': (l, m) for l, m in cands}
        return [by_key[k] for k in self.ledger.spread_pick(info)]

    def _route(self, req: ModelRequest, ent: dict, lane: dict, model: str, route: int, skipped: list | None=None) -> ModelResponse:
        effort = None if req.effort == 'vendor-default' else req.effort or ent.get('effort')
        t0 = time.time()
        resp: ModelResponse | None = None
        attempts = 0
        try:
            if req.allowed_pricing is not None and lane.get('pricing') not in req.allowed_pricing:
                raise GatewayError('PRICING_NOT_ALLOWED', 'route no longer satisfies request pricing restriction')
            self.spend_guard(lane, model)
            self.money_guard(lane, model)
            ad = self.adapters[lane['adapter']]
            ad.preflight(dict(lane, _preflight_deadline=req.deadline, _decision_id=req.decision_id), model)

            def retain(resp):
                if resp.cost.get('usd_eq') is None and resp.usage:
                    resp.cost['usd_eq'] = usd_eq(model, resp.usage)
                    resp.cost['usd_eq_source'] = 'config/prices.yaml' if resp.cost['usd_eq'] is not None else 'no public list price'
                if resp.cost.get('billed_usd', 0) and lane['pricing'] in ('paid', 'sponsor'):
                    try:
                        self.record_money(lane, model, float(resp.cost['billed_usd']))
                    except Exception as exc:
                        resp.cost['accounting_error'] = type(exc).__name__
                if resp.cost.get('accounting_error'):
                    self.trip(lane['lane'], f'accounting write failed on request {req.request_id}')
                    resp.error, resp.error_detail = ('PAID_REFUSED', 'accounting write failed; actual provider cost retained; lane breaker tripped')
                if resp.cost.get('billed_usd', 0) and lane['pricing'] in ('free', 'subscription'):
                    self.trip(lane['lane'], f'billed cost {resp.cost} on request {req.request_id}')
                    resp.error, resp.error_detail = ('PAID_REFUSED', 'provider reported a billed cost; lane breaker tripped')
            resp = self._attempts(req, lane, model, effort, ad, on_attempt=retain)
            attempts = resp.attempts
        except subprocess.TimeoutExpired:
            resp = ModelResponse(req.request_id, req.entrant_id, lane['lane'], lane['adapter'], model, None, '', {'kind': 'none'}, {}, {'billed_usd': 0.0}, int((time.time() - t0) * 1000), None, 'TIMEOUT', 'preflight exceeded decision deadline', 0)
        except GatewayError as e:
            resp = ModelResponse(req.request_id, req.entrant_id, lane['lane'], lane['adapter'], model, None, '', {'kind': 'none'}, {}, {'billed_usd': 0.0}, int((time.time() - t0) * 1000), None, e.code, e.detail, attempts)
        self._log(req, ent, resp, route, skipped=skipped)
        return resp

    def _attempts(self, req: ModelRequest, lane: dict, model: str, effort: str | None, ad, on_attempt=None) -> ModelResponse:
        from .adapters import OFFLINE_DECISION_PREFIXES
        name = lane['lane']
        key = f'{name}:{model}'
        max_att = int(lane.get('max_attempts', 4))
        base = float(lane.get('backoff_s', 30))
        t_base = float(lane.get('transient_backoff_s', base))
        cap = float(lane.get('cooldown_cap_s', 1800))
        min_s = float(lane.get('min_attempt_s', 20))
        lane_timeout = model_timeout_s(lane, model)
        no_deadline_wait = 120.0
        last: ModelResponse | None = None
        attempts = 0
        history = []

        def left() -> float:
            return (req.deadline - datetime.now(timezone.utc)).total_seconds() if req.deadline else float('inf')

        def closed(why: str) -> ModelResponse:
            if last is not None:
                return last
            raise GatewayError('TIMEOUT', why)
        probing = False
        while True:
            if left() < min_s:
                return closed('decision window closed')
            if attempts >= max_att:
                return last
            cool = 0.0 if probing else self.ledger.cooldown_left(key)
            if cool > 0:
                if cool > min(left() - min_s, no_deadline_wait if not req.deadline else float('inf')):
                    if last is None and req.deadline and lane.get('last_chance_probe', True):
                        reserve = max(float(lane.get('last_chance_s', 2 * min_s)), min_s)
                        hold = left() - reserve
                        if hold > 0:
                            time.sleep(hold)
                        probing = True
                        continue
                    if last is not None:
                        return last
                    raise GatewayError('RATE_LIMITED', f'{key} cooling down {cool:.0f}s after provider rate limits; no request sent')
                time.sleep(cool)
                continue
            probing = False
            if lane.get('rpm') or lane.get('tpm'):
                cpt = float(lane.get('tpm_chars_per_token', 2.0))
                est = int((len(req.system) + len(req.prompt)) / cpt) + int(lane.get('tpm_output_allowance', 1000))
                cap_wait = max(0.0, left() - min_s) if req.deadline else no_deadline_wait
                try:
                    self.ledger.pace(key, lane.get('rpm'), lane.get('tpm'), est, cap_wait)
                except GatewayError:
                    if last is not None:
                        return last
                    raise
                if left() < min_s:
                    return closed('decision window closed while pacing')
            attempts += 1
            try:
                self.ledger.reserve(name, int(lane['daily_call_cap']))
            except GatewayError:
                if last is not None:
                    return last
                raise
            sems = [self._sems[name]] + ([self._msems[name, model]] if (name, model) in self._msems else [])
            got = []
            try:
                for sm in sems:
                    wait = max(0.0, left() - min_s) if req.deadline else None
                    pr = req.deadline.timestamp() if req.deadline and (not str(req.decision_id).startswith(OFFLINE_DECISION_PREFIXES)) else None
                    if not (sm.acquire(timeout=wait, priority=pr) if isinstance(sm, FileSlots) else sm.acquire(timeout=wait)):
                        return closed('no free lane slot inside the decision window')
                    got.append(sm)
                rem = left()
                if rem < min_s:
                    return closed('decision window closed while waiting for a lane slot')
                call_lane = dict(lane, timeout_s=int(min(lane_timeout, rem)))
                if attempts > 1 and lane.get('pricing') in ('paid', 'sponsor'):
                    try:
                        self.money_guard(lane, model)
                    except GatewayError:
                        if last is not None:
                            return last
                        raise
                rem = left()
                if rem < min_s or rem <= 0:
                    return closed('decision window closed during money guard')
                call_lane['timeout_s'] = int(min(lane_timeout, rem))
                started_at = datetime.now(timezone.utc).isoformat()
                started_tick = time.monotonic()
                try:
                    from bm import priority
                    with priority.active_call(call_lane, req.decision_id):
                        resp = ad.call(call_lane, model, effort, req)
                except Exception as exc:
                    code = exc.code if isinstance(exc, GatewayError) else 'TIMEOUT' if isinstance(exc, subprocess.TimeoutExpired) else 'TRANSIENT'
                    resp = ModelResponse(req.request_id, req.entrant_id, name, lane.get('adapter', 'unknown'), model, None, '', {'kind': 'none'}, {}, {'billed_usd': None, 'cost_unknown': True}, int((time.monotonic() - started_tick) * 1000), None, code, f'adapter raised {type(exc).__name__}; provider result/cost unknown')
                ended_at = datetime.now(timezone.utc).isoformat()
                if req.deadline and datetime.now(timezone.utc) >= req.deadline and (resp.error is None):
                    resp.error, resp.error_detail = ('TIMEOUT', 'provider answer arrived after the decision deadline; partial text retained')
            finally:
                for sm in reversed(got):
                    sm.release()
            resp.attempts = attempts
            if on_attempt:
                on_attempt(resp)
            history.append({'attempt': attempts, 'started_at': started_at, 'ended_at': ended_at, 'timeout_s': call_lane['timeout_s'], 'raw_blob': resp.raw_blob, 'effective_max_output_tokens': max(req.max_output_tokens, int(lane.get('min_output_tokens') or 0), int((lane.get('model_min_output_tokens') or {}).get(model) or 0)) if lane.get('adapter') in ('openai_compat', 'anthropic_api') else None, 'output_limit_enforcement': 'provider_parameter' if lane.get('adapter') in ('openai_compat', 'anthropic_api') else 'cli_vendor_default', 'model_reported': resp.model_reported, 'identity_source': resp.identity_source, **({'upstream_provider': resp.upstream_provider} if resp.upstream_provider else {}), **({'generation_id': resp.generation_id} if resp.generation_id else {}), **({'provider_order': resp.provider_order} if resp.provider_order else {}), 'stop_reason': resp.stop_reason, 'usage': dict(resp.usage), 'cost': dict(resp.cost), 'latency_ms': resp.latency_ms, 'error': resp.error, 'error_detail': resp.error_detail, **({'refusal_category': resp.refusal_category} if resp.refusal_category else {})})
            resp.attempt_history = list(history)
            numeric_usage = {k for h in history for k, v in h['usage'].items() if isinstance(v, (int, float))}
            resp.attempt_totals = {'usage': {k: sum((h['usage'].get(k) or 0 for h in history)) for k in numeric_usage}, 'billed_usd': sum((h['cost'].get('billed_usd') or 0 for h in history)), 'usd_eq_observed_lower_bound': sum((h['cost'].get('usd_eq') or 0 for h in history)), 'usd_eq': sum((h['cost']['usd_eq'] for h in history)) if all((h['cost'].get('usd_eq') is not None for h in history)) else None, 'provider_latency_ms': sum((h['latency_ms'] for h in history)), 'billed_cost_complete': all((h['cost'].get('billed_usd') is not None for h in history)), 'usage_complete': all((not h['cost'].get('cost_unknown') for h in history))}
            last = resp
            try:
                self.ledger.note_route(key, resp.error)
            except sqlite3.Error:
                pass
            if resp.error == 'RATE_LIMITED':
                self.ledger.strike(key, base, cap, resp.retry_after_s)
                continue
            if resp.error is None:
                self.ledger.clear(key)
                return resp
            if resp.error == 'REFUSED' and attempts < max_att:
                if resp.refusal_category and sum((1 for h in history if h.get('refusal_category'))) > 1:
                    return resp
                if sum((1 for h in history if h.get('error') == 'REFUSED')) >= int(lane.get('refused_max_attempts', REFUSED_MAX_ATTEMPTS)):
                    return resp
                room = left() - min_s
                if room <= 0:
                    return resp
                time.sleep(min(float(lane.get('refused_pause_s', 5.0)), room))
                continue
            if resp.error in ('TIMEOUT', 'TRANSIENT') and attempts < max_att:
                pause = min(t_base * 2 ** (attempts - 1), cap) * random.uniform(0.85, 1.15)
                room = min(left() - min_s, no_deadline_wait if not req.deadline else float('inf'))
                if room <= 0:
                    return resp
                if req.deadline and pause >= room:
                    return resp
                time.sleep(min(pause, room))
                continue
            if resp.error == 'QUOTA_YIELD' and resp.retry_after_s:
                self.ledger.hold(key, resp.retry_after_s)
            return resp

    def _log(self, req: ModelRequest, ent: dict, resp: ModelResponse, route: int=0, skipped: list | None=None) -> None:
        logged_at = datetime.now(timezone.utc)
        completed_at = type(logged_at).fromisoformat(resp.attempt_history[-1]['ended_at']) if resp.attempt_history else logged_at
        decision_deadline = req.decision_deadline or req.deadline
        usd_equivalent = resp.attempt_totals.get('usd_eq') if resp.attempt_history else resp.cost.get('usd_eq')
        sys_h = put_blob(req.system)
        prm_h = put_blob(req.prompt)
        row = {'type': 'model_call', 'ts': logged_at.isoformat(), 'request_id': req.request_id, 'decision_id': req.decision_id, 'entrant': req.entrant_id, 'lane': resp.lane, 'route': route, 'surface': resp.surface, 'model_requested': resp.model_requested, 'model_reported': resp.model_reported, 'upstream_provider': resp.upstream_provider, 'generation_id': resp.generation_id, 'provider_order': resp.provider_order, 'deadline': req.deadline.isoformat() if req.deadline else None, 'decision_deadline': decision_deadline.isoformat() if decision_deadline else None, 'completed_at': completed_at.isoformat(), 'deadline_margin_s': (decision_deadline - completed_at).total_seconds() if decision_deadline else None, 'usd_equivalent': usd_equivalent, 'usd_equivalent_source': 'attempt_totals.usd_eq' if resp.attempt_history else 'cost.usd_eq', 'attempt_history': resp.attempt_history, 'attempt_totals': resp.attempt_totals, 'identity_source': resp.identity_source, 'stop_reason': resp.stop_reason, 'cli_lane': resp.surface in CLI_SURFACES, 'tool_proof': resp.tool_proof, 'effort': None if req.effort == 'vendor-default' else req.effort or ent.get('effort'), 'max_output_tokens': req.max_output_tokens, 'system_blob': sys_h, 'prompt_blob': prm_h, 'raw_blob': resp.raw_blob, 'text_blob': put_blob(resp.text or ''), 'reasoning': {k: v for k, v in resp.reasoning.items() if k != 'text'} | ({'text_blob': put_blob(resp.reasoning['text'])} if resp.reasoning.get('text') else {}), 'usage': resp.usage, 'cost': resp.cost, 'latency_ms': resp.latency_ms, 'attempts': resp.attempts, 'error': resp.error, 'error_detail': (resp.error_detail or '')[:500]}
        if req.manifest:
            row['manifest'] = {**req.manifest, 'alternate': route > 0}
        if skipped:
            dead = any((x.get('cause') == 'dead_free_route' for x in skipped))
            row['alternate_route'] = {'alternate': True, 'skipped': skipped, 'reason': 'earlier route in a shared 429 cooldown' if not dead else 'earlier free route failing every request (dead_free_route)'}
        appended = self.log.append(row)
        resp.gateway_seq = appended.get('seq') if isinstance(appended, dict) else None
        resp.prompt_blob, resp.system_blob, resp.text_blob = (prm_h, sys_h, row['text_blob'])
        resp.route_history = [dict(row, gateway_seq=resp.gateway_seq)]
