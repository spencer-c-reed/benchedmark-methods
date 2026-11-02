from __future__ import annotations
import glob
import json
import math
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from bm.log import put_blob
from . import identity
from .core import ROOT, GatewayError, ModelRequest, ModelResponse
EMPTY = Path.home() / '.cache' / 'benchedmark' / 'empty-cwd'
KEY_FILE = Path.home() / '.config' / 'secrets' / 'benchedmark.env'
THOUGHT_RE = re.compile('^\\s*<thought>(.*?)</thought>\\s*', re.S)
CAP_RE = re.compile('(usage limit|weekly limit|rate.?limit|hit your .*limit|quota|429|too many requests|FreeUsageLimit)', re.I)

def _now_ms() -> int:
    return int(time.time() * 1000)

def _clean_env(drop_secrets: bool=True) -> dict:
    env = dict(os.environ)
    for k in list(env):
        u = k.upper()
        if u.startswith('CLAUDE_CODE_') or u in ('CLAUDECODE',):
            env.pop(k, None)
        if drop_secrets and any((x in u for x in ('TOKEN', 'KEY', 'SECRET', 'PASSWORD', 'COOKIE', 'CREDENTIAL'))):
            env.pop(k, None)
    env.setdefault('PATH', '/usr/bin:/bin')
    nvm = str(Path.home() / '.nvm/versions/node/v22.22.0/bin')
    if nvm not in env['PATH']:
        env['PATH'] = nvm + ':' + str(Path.home() / '.local/bin') + ':' + env['PATH']
    return env

def _run(cmd: list[str], stdin: str, env: dict, timeout: int, cwd: Path=EMPTY) -> tuple[int, str, str, int]:
    cwd.mkdir(parents=True, exist_ok=True)
    t0 = _now_ms()
    try:
        p = subprocess.run(cmd, input=stdin, capture_output=True, text=True, env=env, timeout=timeout, cwd=str(cwd))
        return (p.returncode, p.stdout, p.stderr, _now_ms() - t0)
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode() if isinstance(e.stdout, bytes) else e.stdout or ''
        return (124, out, 'timeout', _now_ms() - t0)
OC_RATE_RE = re.compile('stream error.*(Rate limit|FreeUsageLimit|\\b429\\b|too many requests)', re.I)
OC_UPSTREAM_RE = re.compile('stream error.*(\\[50[234]\\]|overloaded|Service Unavailable|Bad Gateway)', re.I)
OC_GONE_RE = re.compile('stream error.*(?:\\[410\\]|\\[404\\].*Endpoint is unavailable)', re.I)

def _endpoint_gone(status, detail: str) -> bool:
    return str(status) == '410' or (str(status) == '404' and bool(re.search('Endpoint is unavailable|Specified function.*\\bnot found', detail, re.I)))

def oc_abort_code(line: str) -> str | None:
    if 'stream error' not in line or 'agent=title' in line:
        return None
    if OC_RATE_RE.search(line):
        return 'RATE_LIMITED'
    if OC_GONE_RE.search(line):
        return 'PROVIDER_GONE'
    if OC_UPSTREAM_RE.search(line):
        return 'TRANSIENT'
    return None

def _run_watch(cmd: list[str], stdin: str, env: dict, timeout: int, classify, cwd: Path=EMPTY) -> tuple[int, str, str, int, str | None]:
    import threading
    cwd.mkdir(parents=True, exist_ok=True)
    t0 = _now_ms()
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, cwd=str(cwd))
    out, err, hit = ([], [], [])

    def feed():
        try:
            p.stdin.write(stdin)
            p.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    def rd_out():
        for line in p.stdout:
            out.append(line)

    def rd_err():
        for line in p.stderr:
            err.append(line)
            code = classify(line)
            if code and (not hit):
                hit.append(code)
                try:
                    p.kill()
                except OSError:
                    pass
    ths = [threading.Thread(target=f, daemon=True) for f in (feed, rd_out, rd_err)]
    [t.start() for t in ths]
    try:
        rc = p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()
        rc = 124
    for t in ths:
        t.join(timeout=5)
    if hit:
        rc = 125
    return (rc, ''.join(out), ''.join(err), _now_ms() - t0, hit[0] if hit else None)

def _preflight_timeout(lane: dict, maximum: float) -> float:
    deadline = lane.get('_preflight_deadline')
    if deadline is None:
        return maximum
    remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
    if remaining <= 0:
        raise GatewayError('TIMEOUT', 'decision deadline passed during preflight')
    return min(maximum, remaining)

def _request_timeout(req: ModelRequest, maximum: float) -> float:
    return _preflight_timeout({'_preflight_deadline': req.deadline}, maximum)

class Adapter:

    def preflight(self, lane: dict, model: str) -> None:
        return None

class ClaudeCLI(Adapter):
    shim = ROOT / 'bm' / 'gateway' / 'bin' / 'claude-main.sh'

    def preflight(self, lane: dict, model: str) -> None:
        y = lane.get('yield_at_weekly_pct')
        if str(lane.get('_decision_id') or '').startswith(OFFLINE_DECISION_PREFIXES + ('canary:',)):
            y = lane.get('offline_yield_at_weekly_pct', OFFLINE_YIELD_AT_WEEKLY_PCT) if y else y
        f = Path.home() / '.cache' / 'status' / 'claude-weekly.json'
        if y and f.exists():
            try:
                u = float(json.loads(f.read_text()).get('utilization', 0)) * 100
            except Exception:
                u = 0.0
            if u >= float(y):
                raise GatewayError('QUOTA_YIELD', f'Claude main weekly utilization {u:.0f}% >= yield {y}%')

    def call(self, lane: dict, model: str, effort: str | None, req: ModelRequest) -> ModelResponse:
        with tempfile.NamedTemporaryFile('w', suffix='.txt', delete=False, dir=str(EMPTY.parent)) as f:
            f.write(req.system)
            sysf = f.name
        cmd = [str(self.shim), '--model', model, '--tools', '', '--strict-mcp-config', '--setting-sources', '', '--no-session-persistence', '--system-prompt-file', sysf, '--output-format', 'json']
        if effort:
            cmd += ['--effort', effort]
        try:
            rc, out, err, ms = _run(cmd, req.prompt, _clean_env(drop_secrets=False), _request_timeout(req, int(lane.get('timeout_s', 420))))
        finally:
            os.unlink(sysf)
        raw = put_blob(json.dumps({'rc': rc, 'stdout': out, 'stderr': err[-4000:]}))
        base = dict(request_id=req.request_id, entrant_id=req.entrant_id, lane=lane['lane'], surface='claude-cli', model_requested=model, identity_source='cli_usage_report', latency_ms=ms, raw_blob=raw, reasoning={'kind': 'none', 'publishable': False}, tool_proof={'tool_calls': None, 'evidence': 'no result object'})
        if rc == 75:
            return ModelResponse(**base, model_reported=None, text='', usage={}, cost={'billed_usd': 0.0}, error='QUOTA_YIELD', error_detail='claude main account capped (selector)')
        if rc == 124:
            return ModelResponse(**base, model_reported=None, text='', usage={}, cost={'billed_usd': 0.0}, error='TIMEOUT')
        try:
            d = json.loads(out)
        except Exception:
            d = None
            for line in reversed(out.strip().splitlines()):
                if line.lstrip().startswith('{'):
                    try:
                        d = json.loads(line)
                        break
                    except Exception:
                        continue
        if not isinstance(d, dict):
            code = 'RATE_LIMITED' if CAP_RE.search(out + err) else 'TRANSIENT'
            return ModelResponse(**base, model_reported=None, text='', usage={}, cost={'billed_usd': 0.0}, error=code, error_detail=(err or out)[-400:])
        base['tool_proof'] = identity.claude_tool_proof(d)
        u = d.get('usage') or {}
        usage = {'input': (u.get('input_tokens') or 0) + (u.get('cache_read_input_tokens') or 0) + (u.get('cache_creation_input_tokens') or 0), 'cached_input': u.get('cache_read_input_tokens') or 0, 'output': u.get('output_tokens') or 0, 'reasoning': (u.get('output_tokens_details') or {}).get('thinking_tokens')}
        models = list((d.get('modelUsage') or {}).keys())
        cost = {'billed_usd': 0.0, 'usd_eq': d.get('total_cost_usd'), 'usd_eq_source': 'claude-cli total_cost_usd (list-price estimate)'}
        text = d.get('result') or ''
        if d.get('is_error'):
            if 'safeguards flagged' in text or 'Usage Policy' in text:
                code = 'REFUSED'
            else:
                code = 'QUOTA_YIELD' if CAP_RE.search(text) else 'AUTH' if 'auth' in text.lower() else 'TRANSIENT'
            return ModelResponse(**base, model_reported=models[0] if models else None, text='', usage=usage, cost=cost, error=code, error_detail=text[:400])
        return ModelResponse(**base, model_reported=','.join(models) if models else None, text=text, usage=usage, cost=cost)
CREDITS_HELPER = ROOT / 'bm' / 'gateway' / 'bin' / 'codex-credits.py'

class CodexCLI(Adapter):

    def preflight(self, lane: dict, model: str) -> None:
        guard = os.path.expanduser(lane.get('credit_guard', '~/scripts/codex-profile.py'))
        prof = lane.get('codex_profile', 'main')
        if prof != 'main':
            raise GatewayError('PAID_REFUSED', "only the operator main Codex profile is eligible")
        p = subprocess.run(['python3', guard, 'guard', '--machine', 'vps', '--profile', 'main', '--model', model, '--fresh', '--require-quota'], capture_output=True, text=True, env=_clean_env(drop_secrets=False), timeout=_preflight_timeout(lane, 120))
        if p.returncode == 75 and lane.get('credits_daily_cap'):
            c = subprocess.run(['python3', str(CREDITS_HELPER), 'cap', str(float(lane['credits_daily_cap']))], capture_output=True, text=True, env=_clean_env(drop_secrets=False), timeout=_preflight_timeout(lane, 120))
            if c.returncode != 0:
                raise GatewayError('QUOTA_YIELD', f'Codex credit daily cap check refused: {c.stdout[-200:]}')
            return
        if p.returncode == 75:
            try:
                msg = json.loads(p.stdout).get('message', '')
            except Exception:
                msg = p.stdout[-300:]
            raise GatewayError('QUOTA_YIELD', msg[:300])
        if p.returncode != 0:
            raise GatewayError('QUOTA_YIELD', f'credit guard rc={p.returncode}: fail closed')

    @staticmethod
    def _balance(timeout=60) -> float | None:
        try:
            c = subprocess.run(['python3', str(CREDITS_HELPER), 'balance'], capture_output=True, text=True, env=_clean_env(drop_secrets=False), timeout=timeout)
            return json.loads(c.stdout).get('balance')
        except Exception:
            return None

    def _skills_off(self) -> str:
        home = Path(os.environ.get('CODEX_HOME', Path.home() / '.codex'))
        ps = sorted(set(glob.glob(str(home / 'skills' / '*' / 'SKILL.md')) + glob.glob(str(home / 'skills' / '.system' / '*' / 'SKILL.md'))))
        return 'skills.config=[' + ','.join(('{path="%s",enabled=false}' % p for p in ps)) + ']'

    def call(self, lane: dict, model: str, effort: str | None, req: ModelRequest) -> ModelResponse:
        with tempfile.NamedTemporaryFile('w', suffix='.md', delete=False, dir=str(EMPTY.parent)) as f:
            f.write(req.system)
            sysf = f.name
        cmd = ['codex', 'exec', '--json', '-m', model, '--ephemeral', '--skip-git-repo-check', '--ignore-user-config', '--dangerously-bypass-approvals-and-sandbox', '--disable', 'shell_tool', '--disable', 'memories', '--disable', 'plugins', '--disable', 'apps', '--disable', 'multi_agent', '--disable', 'browser_use', '--disable', 'computer_use', '--disable', 'goals', '-c', 'web_search=disabled', '-c', self._skills_off(), '-c', f'model_instructions_file={sysf}', '-c', identity.CODEX_SSE_PROVIDER, '-c', 'model_provider=bm_openai_sse']
        if effort:
            cmd += ['-c', f'model_reasoning_effort={effort}']
        cmd += ['-']
        env = _clean_env(drop_secrets=False)
        env.pop('CODEX_HOME', None)
        env['RUST_LOG'] = identity.CODEX_RUST_LOG
        bal0 = self._balance(timeout=_request_timeout(req, 60)) if lane.get('credits_daily_cap') else None
        try:
            rc, out, err, ms = _run(cmd, req.prompt, env, _request_timeout(req, int(lane.get('timeout_s', 420))))
        finally:
            os.unlink(sysf)
        credits = None
        if bal0 is not None:
            bal1 = self._balance()
            credits = round(bal0 - bal1, 3) if bal1 is not None else None
        seen = identity.codex_events(out, err)
        raw = put_blob(json.dumps({'rc': rc, 'stdout': out, 'stderr': identity.non_sse_stderr(err), 'served': seen['evidence']}))
        texts, reasoning, usage, errs = ([], [], {}, [])
        for line in out.splitlines():
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if ev.get('type') == 'item.completed':
                it = ev.get('item', {})
                if it.get('type') == 'agent_message':
                    texts.append(it.get('text', ''))
                elif it.get('type') == 'reasoning':
                    reasoning.append(it.get('text', '') or it.get('summary', '') or '')
                elif it.get('type') == 'error' and 'Under-development' not in it.get('message', '') and ('Code Mode' not in it.get('message', '')):
                    errs.append(it.get('message', ''))
            elif ev.get('type') == 'turn.completed':
                u = ev.get('usage', {})
                usage = {'input': u.get('input_tokens', 0), 'cached_input': u.get('cached_input_tokens', 0), 'output': u.get('output_tokens', 0), 'reasoning': u.get('reasoning_output_tokens')}
            elif ev.get('type') in ('error', 'turn.failed'):
                errs.append(json.dumps(ev)[:400])
        text = texts[-1] if texts else ''
        r = {'kind': 'summary', 'text': '\n'.join((x for x in reasoning if x)), 'publishable': True} if any(reasoning) else {'kind': 'none', 'publishable': False}
        base = dict(request_id=req.request_id, entrant_id=req.entrant_id, lane=lane['lane'], surface='codex-cli', model_requested=model, model_reported=seen['model'], identity_source='cli_response_stream' if seen['model'] else 'requested_cli_configuration', latency_ms=ms, raw_blob=raw, reasoning=r, usage=usage, tool_proof=seen['tool_proof'], cost={'billed_usd': 0.0, 'usd_eq': None, **({'codex_credits': credits} if bal0 is not None else {})})
        if rc == 124:
            return ModelResponse(**base, text='', error='TIMEOUT')
        if seen['model'] and (not identity.same_model(model, seen['model'])):
            return ModelResponse(**base, text='', error='TRANSIENT', error_detail=f'codex served {seen['model']}, requested {model}')
        if rc != 0 or errs or (not text):
            blob = ' '.join(errs) + err[-500:]
            code = 'QUOTA_YIELD' if CAP_RE.search(blob) else 'TRANSIENT'
            return ModelResponse(**base, text=text, error=code, error_detail=blob[:400] or f'CLI exited {rc}')
        return ModelResponse(**base, text=text)
OC_PERMS = {'permission': {k: 'ask' for k in ('task', 'read', 'edit', 'bash', 'webfetch', 'glob', 'grep', 'list', 'skill', 'external_directory')}}

class OpenCode(Adapter):

    def preflight(self, lane: dict, model: str) -> None:
        if lane.get('price_guard'):
            g = os.path.expanduser(lane['price_guard'])
            p = subprocess.run([g, model.removeprefix('openrouter/')], capture_output=True, text=True, timeout=_preflight_timeout(lane, 60))
            if p.returncode != 0:
                raise GatewayError('PAID_REFUSED', f'live price check did not prove {model} is free (fail closed)')
        if lane.get('key_env') and (not _key(lane['key_env'])):
            raise GatewayError('AUTH', f'{lane['key_env']} not set')

    def call(self, lane: dict, model: str, effort: str | None, req: ModelRequest) -> ModelResponse:
        home = Path.home() / '.cache' / 'benchedmark' / 'opencode'
        for d in ('config', 'data', 'state'):
            (home / d).mkdir(parents=True, exist_ok=True)
        env = _clean_env(drop_secrets=True)
        env.update({'XDG_CONFIG_HOME': str(home / 'config'), 'XDG_DATA_HOME': str(home / 'data'), 'XDG_STATE_HOME': str(home / 'state'), 'OPENCODE_DISABLE_AUTOUPDATE': '1', 'OPENCODE_DISABLE_CLAUDE_CODE': '1', 'OPENCODE_DISABLE_CLAUDE_CODE_PROMPT': '1', 'OPENCODE_DISABLE_CLAUDE_CODE_SKILLS': '1', 'OPENCODE_CONFIG_CONTENT': json.dumps(OC_PERMS)})
        env.pop('OPENCODE_CONFIG', None)
        if lane.get('key_env'):
            k = _key(lane['key_env'])
            if k:
                env[lane['key_env']] = k
            del k
        prompt = 'SYSTEM INSTRUCTIONS (follow exactly; do not use any tools):\n' + req.system + '\n\n---\n\n' + req.prompt
        title = re.sub('[^A-Za-z0-9:_.-]', '_', req.decision_id)[:80] or 'benchedmark'
        cmd = ['opencode', 'run', '--pure', '--format', 'json', '--title', title, '--print-logs', '--log-level', 'ERROR', '--model', model, '--dir', str(EMPTY)]
        rc, out, err, ms, abort = _run_watch(cmd, prompt, env, _request_timeout(req, int(lane.get('timeout_s', 300))), oc_abort_code)
        raw = put_blob(json.dumps({'rc': rc, 'stdout': out, 'stderr': err[-4000:]}))
        texts, reasoning, errs = ([], [], [])
        endpoint_gone = False
        usage = {'input': 0, 'cached_input': 0, 'output': 0, 'reasoning': 0}
        billed = 0.0
        stop_reason = None
        for line in out.splitlines():
            try:
                ev = json.loads(line)
            except Exception:
                continue
            t = ev.get('type')
            part = ev.get('part') or {}
            if t == 'text':
                texts.append(part.get('text', ''))
            elif t == 'reasoning':
                reasoning.append(part.get('text', ''))
            elif t == 'step_finish':
                stop_reason = part.get('reason')
                tk = part.get('tokens') or {}
                usage['input'] += (tk.get('input') or 0) + ((tk.get('cache') or {}).get('read') or 0)
                usage['cached_input'] += (tk.get('cache') or {}).get('read') or 0
                usage['output'] += tk.get('output') or 0
                usage['reasoning'] += tk.get('reasoning') or 0
                try:
                    billed += float(part.get('cost') or 0)
                except (TypeError, ValueError):
                    pass
            elif t == 'error':
                error = ev.get('error', ev)
                detail = json.dumps(error)
                data = error.get('data', error) if isinstance(error, dict) else {}
                if isinstance(data, dict):
                    endpoint_gone |= _endpoint_gone(data.get('statusCode', data.get('code')), detail)
                errs.append(detail[:600])
        r = {'kind': 'raw', 'text': '\n'.join(reasoning), 'publishable': False} if any(reasoning) else {'kind': 'none', 'publishable': False}
        base = dict(request_id=req.request_id, entrant_id=req.entrant_id, lane=lane['lane'], surface='opencode-zen', model_requested=model, model_reported=None, identity_source='requested_cli_configuration', latency_ms=ms, raw_blob=raw, reasoning=r, usage=usage, cost={'billed_usd': billed, 'usd_eq': None}, stop_reason=stop_reason)
        if abort:
            line = next((l for l in reversed(err.splitlines()) if oc_abort_code(l)), '')
            m = re.search('error\\.(?:error(?:\\.message)?)="?([^"]*)', line)
            return ModelResponse(**base, text='', error=abort, error_detail=('provider: ' + (m.group(1) if m else line))[:400])
        if endpoint_gone:
            return ModelResponse(**base, text='', error='PROVIDER_GONE', error_detail=' '.join(errs)[:400])
        if rc == 124:
            return ModelResponse(**base, text=''.join(texts), error='TIMEOUT')
        text = ''.join(texts)
        if rc != 0 or errs or stop_reason in ('length', 'max_tokens'):
            blob = ' '.join(errs) or f'CLI exited {rc}; stop_reason {stop_reason}'
            code = 'RATE_LIMITED' if CAP_RE.search(blob) else 'PAID_REFUSED' if 'FreeTierError' in blob else 'TRANSIENT'
            return ModelResponse(**base, text=text, error=code, error_detail=blob[:400])
        if not text:
            return ModelResponse(**base, text='', error='TRANSIENT', error_detail=(err or 'empty')[-300:])
        return ModelResponse(**base, text=text)

def _key(name: str) -> str | None:
    v = os.environ.get(name, '').strip()
    if v:
        return v
    try:
        p = subprocess.run(['systemctl', '--user', 'show-environment'], capture_output=True, text=True, timeout=10, env={**os.environ, 'XDG_RUNTIME_DIR': os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')})
        for line in p.stdout.splitlines():
            k, _, val = line.partition('=')
            if k == name and val:
                return val
    except Exception:
        pass
    try:
        if KEY_FILE.exists() and KEY_FILE.stat().st_mode & 63 == 0:
            for line in KEY_FILE.read_text().splitlines():
                k, _, val = line.partition('=')
                if k.strip() == name and val.strip():
                    return val.strip()
    except Exception:
        return None
    return None
DAILY_RE = re.compile('per.?day|PerDay|daily', re.I)
_RESET_MS_RE = re.compile('"X-RateLimit-Reset"\\s*:\\s*"(\\d{13})"')
_RETRY_DELAY_RE = re.compile('"retryDelay"\\s*:\\s*"(\\d+(?:\\.\\d+)?)s"')

def retry_hint_s(header: str | None, body: str) -> float | None:
    if header:
        try:
            return max(0.0, float(header.strip()))
        except ValueError:
            pass
    m = _RETRY_DELAY_RE.search(body or '')
    if m:
        return float(m.group(1))
    m = _RESET_MS_RE.search(body or '')
    if m:
        import time as _t
        return max(0.0, int(m.group(1)) / 1000.0 - _t.time())
    return None

def _err_provider(text: str) -> str | None:
    m = re.search('"provider_name"\\s*:\\s*"([^"]+)"', text or '')
    return m.group(1) if m else None
QUOTA_TEXT_RE = re.compile('quota|usage limit|weekly|membership|subscription|plan limit', re.I)
_PLAN_USAGE: dict[str, tuple[float, list]] = {}

def plan_windows(d: dict) -> list[tuple[float, float | None]]:
    out = []
    for w in [d.get('usage')] + [(x or {}).get('detail') for x in d.get('limits') or [] if isinstance(x, dict)]:
        if not isinstance(w, dict) or not w.get('limit'):
            continue
        try:
            lim = float(w['limit'])
            used = float(w['used']) if w.get('used') is not None else lim - float(w.get('remaining', lim))
            reset = w.get('resetTime')
            left = (datetime.fromisoformat(reset.replace('Z', '+00:00')) - datetime.now(timezone.utc)).total_seconds() if reset else None
            out.append((used / lim, max(0.0, left) if left is not None else None))
        except (TypeError, ValueError):
            continue
    return out

def plan_usage(lane: dict, fresh: bool=False) -> list | None:
    url = lane.get('usage_url')
    if not url:
        return None
    hit = _PLAN_USAGE.get(lane['lane'])
    if hit and (not fresh) and (time.time() - hit[0] < 60):
        return hit[1]
    key = _key(lane['key_env'])
    if not key:
        return None
    try:
        r = urllib.request.Request(url, headers={'Authorization': f'Bearer {key}'})
        del key
        with urllib.request.urlopen(r, timeout=20) as resp:
            wins = plan_windows(json.loads(resp.read().decode()))
    except Exception:
        return None
    _PLAN_USAGE[lane['lane']] = (time.time(), wins)
    return wins

def plan_spent(lane: dict, wins: list | None) -> float | None:
    if not wins:
        return None
    y = float(lane.get('usage_yield_pct', 100)) / 100.0
    spent = [left for frac, left in wins if frac >= y]
    if not spent:
        return None
    return max((x for x in spent if x is not None), default=0.0)

class OpenAICompat(Adapter):

    def preflight(self, lane: dict, model: str) -> None:
        if lane.get('price_guard'):
            g = os.path.expanduser(lane['price_guard'])
            p = subprocess.run([g, model], capture_output=True, text=True, timeout=_preflight_timeout(lane, 60))
            if p.returncode != 0:
                raise GatewayError('PAID_REFUSED', f'live price check did not prove {model} is free (fail closed)')
        if not _key(lane['key_env']):
            raise GatewayError('AUTH', f'{lane['key_env']} not set')
        if lane.get('usage_url'):
            wait = plan_spent(lane, plan_usage(lane))
            if wait is not None:
                raise GatewayError('QUOTA_YIELD', f'{lane['lane']} plan window at its yield point; resets in {wait:.0f}s')

    def call(self, lane: dict, model: str, effort: str | None, req: ModelRequest) -> ModelResponse:
        key = _key(lane['key_env'])
        body = {'model': model, 'messages': [{'role': 'system', 'content': req.system}, {'role': 'user', 'content': req.prompt}], 'max_tokens': max(int(req.max_output_tokens), int(lane.get('min_output_tokens') or 0), int((lane.get('model_min_output_tokens') or {}).get(model) or 0))}
        per = lane.get('model_temperature') or {}
        temp = per[model] if model in per else lane.get('temperature', 0.7)
        if temp is not None:
            body['temperature'] = temp
        pin = None
        if 'openrouter.ai' in lane['base_url']:
            body['usage'] = {'include': True}
            if lane.get('pricing') != 'paid':
                body['provider'] = {'max_price': {'prompt': 0, 'completion': 0}}
            pin = (lane.get('provider_pin') or {}).get(model)
            if pin:
                body.setdefault('provider', {}).update({'order': list(pin), 'allow_fallbacks': False})
        url = lane['base_url'].rstrip('/') + '/chat/completions'
        r = urllib.request.Request(url, data=json.dumps(body).encode(), method='POST', headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json', 'HTTP-Referer': 'https://benchedmark.com', 'X-Title': 'BenchedMark'})
        del key
        t0 = _now_ms()
        base = dict(request_id=req.request_id, entrant_id=req.entrant_id, lane=lane['lane'], surface='openai-compat', model_requested=model, reasoning={'kind': 'none', 'publishable': False}, provider_order=list(pin) if pin else None)
        try:
            with urllib.request.urlopen(r, timeout=_request_timeout(req, int(lane.get('timeout_s', 300)))) as resp:
                raw_s = resp.read().decode()
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors='replace')[:2000]
            code = 'RATE_LIMITED' if e.code == 429 else 'AUTH' if e.code in (401, 403) else 'TRANSIENT'
            if _endpoint_gone(e.code, msg):
                code = 'PROVIDER_GONE'
            if e.code == 402:
                code = 'QUOTA_YIELD' if lane.get('pricing') == 'paid' else 'PAID_REFUSED'
            hint = None
            if e.code == 429:
                if DAILY_RE.search(msg):
                    code = 'QUOTA_YIELD'
                hint = retry_hint_s(e.headers.get('Retry-After') if e.headers else None, msg)
            if lane.get('usage_url') and e.code in (402, 403, 429):
                wins = plan_usage(lane, fresh=True)
                wait = plan_spent(lane, wins)
                if wait is not None:
                    code, hint = ('QUOTA_YIELD', wait)
                elif not wins and QUOTA_TEXT_RE.search(msg) and ('auth' not in msg.lower()):
                    code = 'QUOTA_YIELD'
            return ModelResponse(**base, model_reported=None, text='', usage={}, cost={'billed_usd': 0.0}, latency_ms=_now_ms() - t0, raw_blob=put_blob(msg), error=code, error_detail=f'HTTP {e.code}: {msg[:300]}', retry_after_s=hint, upstream_provider=_err_provider(msg))
        except Exception as e:
            return ModelResponse(**base, model_reported=None, text='', usage={}, cost={'billed_usd': None, 'cost_unknown': True}, latency_ms=_now_ms() - t0, raw_blob=None, error='TIMEOUT' if 'timed out' in str(e) else 'TRANSIENT', error_detail=str(e)[:300])
        ms = _now_ms() - t0
        raw = put_blob(raw_s)
        try:
            d = json.loads(raw_s)
        except Exception:
            return ModelResponse(**base, model_reported=None, text='', usage={}, cost={'billed_usd': None, 'cost_unknown': True}, latency_ms=ms, raw_blob=raw, error='TRANSIENT', error_detail=raw_s[:300])
        if not isinstance(d, dict) or (d.get('choices') is not None and (not isinstance(d['choices'], list) or any((not isinstance(c, dict) for c in d['choices'])))) or (isinstance(d, dict) and any((c.get('message') is not None and (not isinstance(c['message'], dict)) for c in d.get('choices') or []))) or (isinstance(d, dict) and d.get('usage') is not None and (not isinstance(d['usage'], dict))):
            return ModelResponse(**base, model_reported=None, text='', usage={}, cost={'billed_usd': None, 'cost_unknown': True}, latency_ms=ms, raw_blob=raw, error='TRANSIENT', error_detail='invalid provider envelope schema; cost unknown')
        if d.get('error'):
            detail = json.dumps(d['error'])
            em = detail[:400]
            status = d['error'].get('code') if isinstance(d['error'], dict) else None
            return ModelResponse(**base, model_reported=None, text='', usage={}, cost={'billed_usd': 0.0}, latency_ms=ms, raw_blob=raw, error='PROVIDER_GONE' if _endpoint_gone(status, detail) else 'RATE_LIMITED' if CAP_RE.search(em) else 'TRANSIENT', error_detail=em, upstream_provider=d.get('provider') if isinstance(d.get('provider'), str) else _err_provider(json.dumps(d['error'])))
        ch = (d.get('choices') or [{}])[0]
        msg = ch.get('message') or {}
        u = d.get('usage') or {}
        if d.get('model') is not None and (not isinstance(d['model'], str)) or any((msg.get(k) is not None and (not isinstance(msg[k], str)) for k in ('reasoning', 'reasoning_content'))) or (msg.get('content') is not None and (not isinstance(msg['content'], str))) or any((u.get(k) is not None and (not isinstance(u[k], (int, float)) or not math.isfinite(u[k]) or u[k] < 0) for k in ('prompt_tokens', 'completion_tokens', 'cost'))) or any((u.get(k) is not None and (not isinstance(u[k], dict)) for k in ('prompt_tokens_details', 'completion_tokens_details'))):
            return ModelResponse(**base, model_reported=None, text='', usage={}, cost={'billed_usd': None, 'cost_unknown': True}, latency_ms=ms, raw_blob=raw, error='TRANSIENT', error_detail='invalid provider content/usage schema; cost unknown')
        content = msg.get('content') or ''
        reasoning_text = msg.get('reasoning') or msg.get('reasoning_content')
        if content.lstrip().startswith('<thought>') and '</thought>' not in content:
            reasoning_text = (reasoning_text + '\n' if reasoning_text else '') + content.lstrip()[len('<thought>'):].strip()
            content = ''
            msg = dict(msg, content='')
        m = THOUGHT_RE.match(content)
        if m:
            reasoning_text = (reasoning_text + '\n' if reasoning_text else '') + m.group(1).strip()
            content = content[m.end():]
            msg = dict(msg, content=content)
        rr = {'kind': 'raw', 'text': reasoning_text, 'publishable': False} if reasoning_text else {'kind': 'none', 'publishable': False}
        base['reasoning'] = rr
        cost = float(u.get('cost') or 0.0)
        stop = ch.get('finish_reason')
        refused = stop == 'content_filter' or bool(msg.get('refusal'))
        truncated = stop == 'length'
        return ModelResponse(**base, model_reported=d.get('model'), identity_source='provider_response' if d.get('model') else 'unknown', stop_reason=stop, text=msg.get('content') or '', upstream_provider=d.get('provider') if isinstance(d.get('provider'), str) else None, generation_id=d.get('id') if isinstance(d.get('id'), str) else None, usage={'input': u.get('prompt_tokens', 0), 'cached_input': (u.get('prompt_tokens_details') or {}).get('cached_tokens', 0), 'output': u.get('completion_tokens', 0), 'reasoning': (u.get('completion_tokens_details') or {}).get('reasoning_tokens')}, cost={'billed_usd': cost, 'usd_eq': None}, latency_ms=ms, raw_blob=raw, error='REFUSED' if refused else 'TRANSIENT' if truncated or not (msg.get('content') or '').strip() else None, error_detail='provider content refusal' if refused else 'truncated: output budget exhausted (finish_reason length)' if truncated else 'empty content' if not (msg.get('content') or '').strip() else None)
AGY_LANE = Path.home() / 'scripts' / 'agy-lane.sh'
AGY_SEAT = Path.home() / '.cache' / 'benchedmark' / 'agy-seat'
AGY_HOOK = ROOT / 'config' / 'agy-seat' / 'hooks.json'
AGY_RECEIPT_RE = re.compile('agy-lane: receipt (\\S+)')
AGY_WRAPPER_TIMEOUT_RE = re.compile('agy-lane: FAILED status=\\S* rc=124\\b')
AGY_CONVERSATIONS = Path.home() / '.gemini' / 'antigravity-cli' / 'conversations'

def agy_seat_ready() -> Path:
    want = AGY_HOOK.read_text()
    (AGY_SEAT / '.agents').mkdir(parents=True, exist_ok=True)
    if not (AGY_SEAT / '.git').exists():
        subprocess.run(['git', 'init', '-q', str(AGY_SEAT)], check=True, timeout=30)
    h = AGY_SEAT / '.agents' / 'hooks.json'
    if not h.exists() or h.read_text() != want:
        h.write_text(want)
    extra = [p.name for p in AGY_SEAT.iterdir() if p.name not in ('.git', '.agents')]
    if extra or h.read_text() != want:
        raise GatewayError('TRANSIENT', f'agy seat workspace not clean: {extra}')
    return AGY_SEAT

def _session_pids(sid: int) -> list[int]:
    pids = []
    for d in Path('/proc').iterdir():
        if not d.name.isdigit():
            continue
        try:
            fields = (d / 'stat').read_text().rsplit(')', 1)[1].split()
        except (OSError, IndexError):
            continue
        if len(fields) > 3 and fields[3] == str(sid):
            pids.append(int(d.name))
    return pids

def _kill_session(sid: int) -> None:
    import signal
    for _ in range(5):
        pids = _session_pids(sid)
        if not pids:
            return
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        time.sleep(0.2)

def _run_group(cmd: list[str], stdin: str, env: dict, timeout: int, cwd: Path=EMPTY) -> tuple[int, str, str, int]:
    cwd.mkdir(parents=True, exist_ok=True)
    t0 = _now_ms()
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, cwd=str(cwd), start_new_session=True)
    try:
        out, err = p.communicate(stdin, timeout=timeout)
        return (p.returncode, out, err, _now_ms() - t0)
    except subprocess.TimeoutExpired:
        _kill_session(p.pid)
        out, err = p.communicate()
        return (124, out or '', (err or '') + '\ntimeout', _now_ms() - t0)

class AgyCLI(Adapter):

    def preflight(self, lane: dict, model: str) -> None:
        if not os.access(AGY_LANE, os.X_OK):
            raise GatewayError('AUTH', f'{AGY_LANE} missing')
        agy_seat_ready()

    def call(self, lane: dict, model: str, effort: str | None, req: ModelRequest) -> ModelResponse:
        seat = agy_seat_ready()
        prompt = 'SYSTEM INSTRUCTIONS (follow exactly; do not use any tools):\n' + req.system + '\n\n---\n\n' + req.prompt
        with tempfile.NamedTemporaryFile('w', suffix='.md', delete=False, dir=str(EMPTY.parent)) as f:
            f.write(prompt)
            pf = f.name
        t = int(lane.get('timeout_s', 300))
        outer = _request_timeout(req, t)
        name = 'bm-' + (re.sub('[^A-Za-z0-9_.-]', '_', req.decision_id)[:60] or 'seat')
        cmd = [str(AGY_LANE), 'run', '--cwd', str(seat), '--model', model, '--timeout', f'{max(30, int(outer) - 10)}s', '--name', name, pf]
        try:
            rc, out, err, ms = _run_group(cmd, '', _clean_env(drop_secrets=True), outer)
        finally:
            os.unlink(pf)
        m = AGY_RECEIPT_RE.search(err)
        rdir = Path(m.group(1)) if m else None
        init, result, tools, tool_steps = ({}, {}, 0, set())
        if rdir and (rdir / 'events.jsonl').exists():
            for line in (rdir / 'events.jsonl').read_text().splitlines():
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                if ev.get('event') == 'init':
                    init = ev.get('init') or {}
                    init['conversation_id'] = ev.get('conversation_id')
                elif ev.get('event') == 'result':
                    result = ev.get('result') or {}
                elif ev.get('event') == 'step_update' and (ev.get('step_update') or {}).get('step_type') == 'tool':
                    tool_steps.add((ev.get('step_update') or {}).get('step_index'))
                    if (ev.get('step_update') or {}).get('state') == 'ERROR':
                        tools += 1
        conv = init.get('conversation_id') or result.get('conversation_id')
        served_ids = identity.agy_served(AGY_CONVERSATIONS / f'{conv}.db') if conv and re.fullmatch('[0-9a-f-]{36}', str(conv)) else []
        hook_sha = identity.file_sha256(seat / '.agents' / 'hooks.json')
        tool_proof = {'tool_calls': len(tool_steps), 'tool_calls_denied': tools, 'hook_sha256': hook_sha, 'evidence': 'agy events.jsonl tool step_updates; PreToolUse deny-all hook in the seat workspace'}
        raw = put_blob(json.dumps({'rc': rc, 'receipt': str(rdir) if rdir else None, 'init_model': init.get('model'), 'served_models': served_ids, 'served_evidence': 'gen_metadata field 19', 'conversation_id': init.get('conversation_id'), 'tool_calls_denied': tools, 'result': {k: v for k, v in result.items() if k != 'json_schema'}, 'stderr': err[-3000:]}))
        u = result.get('usage') or {}
        usage = {'input': u.get('input_tokens') or 0, 'cached_input': u.get('cache_read_tokens') or 0, 'output': u.get('output_tokens') or 0, 'reasoning': u.get('thinking_tokens'), 'tool_calls_denied': tools}
        selected = init.get('model')
        served = ','.join(served_ids) or selected
        base = dict(request_id=req.request_id, entrant_id=req.entrant_id, lane=lane['lane'], surface='agy-cli', model_requested=model, model_reported=served, identity_source='cli_generation_metadata' if served_ids else 'cli_init_event' if selected else 'unknown', latency_ms=ms, raw_blob=raw, usage=usage, tool_proof=tool_proof, reasoning={'kind': 'none', 'publishable': False}, cost={'billed_usd': 0.0, 'usd_eq': None, 'usd_eq_source': 'Google AI Pro subscription quota; credit overage forced off'})
        tail = (err or out)[-400:]
        if rc == 124 and result.get('status') == 'SUCCESS':
            rc = 0
        if rc != 0 and result.get('status') != 'SUCCESS' and AGY_WRAPPER_TIMEOUT_RE.search(err):
            rc = 124
        if rc == 124:
            return ModelResponse(**base, text='', error='TIMEOUT')
        if rc == 75:
            return ModelResponse(**base, text='', error='QUOTA_YIELD', error_detail=tail)
        if rc == 77:
            return ModelResponse(**base, text='', error='AUTH', error_detail=tail)
        if selected and selected != model:
            return ModelResponse(**base, text='', error='TRANSIENT', error_detail=f'agy selected {selected}, requested {model}')
        if served_ids and (not all((identity.same_model(model, x) for x in served_ids))):
            return ModelResponse(**base, text='', error='TRANSIENT', error_detail=f'agy served {served}, requested {model}')
        if rc != 0 or result.get('status') != 'SUCCESS':
            code = 'RATE_LIMITED' if CAP_RE.search(tail) else 'TRANSIENT'
            return ModelResponse(**base, text='', error=code, error_detail=f'rc={rc} status={result.get('status')}: {tail}')
        text = result.get('response') or out
        if not text.strip():
            return ModelResponse(**base, text='', error='TRANSIENT', error_detail='empty response')
        return ModelResponse(**base, text=text)
CREDITS_LEDGER = Path.home() / 'scripts' / 'claude-api-credits.py'
OFFLINE_DECISION_PREFIXES = ('replay:', 'recovery:', 'panel-rewrite:')
OFFLINE_YIELD_AT_WEEKLY_PCT = 85

class AnthropicAPI(Adapter):

    def _est(self, model: str, req: ModelRequest) -> float:
        from .core import usd_eq
        v = usd_eq(model, {'input': (len(req.system) + len(req.prompt)) // 3, 'cached_input': 0, 'cache_creation_input': len(req.system) // 3, 'output': int(req.max_output_tokens)})
        return float(v or 0.25)

    def preflight(self, lane: dict, model: str) -> None:
        if not _key(lane['key_env']):
            raise GatewayError('AUTH', f'{lane['key_env']} not set')

    def call(self, lane: dict, model: str, effort: str | None, req: ModelRequest) -> ModelResponse:
        from .core import usd_eq
        who = lane.get('credits_consumer', 'benchedmark')
        base = dict(request_id=req.request_id, entrant_id=req.entrant_id, lane=lane['lane'], surface='anthropic-api', model_requested=model, reasoning={'kind': 'none', 'publishable': False})
        if str(req.decision_id).startswith(OFFLINE_DECISION_PREFIXES):
            return ModelResponse(**base, model_reported=None, text='', usage={}, cost={'billed_usd': 0.0}, latency_ms=0, raw_blob=None, error='LANE_DISABLED', error_detail='offline work never uses the Claude API credits lane')
        need = self._est(model, req)
        g = subprocess.run([str(CREDITS_LEDGER), 'can-spend', who, f'{need:.4f}'], capture_output=True, text=True, timeout=_request_timeout(req, 30))
        if g.returncode != 0:
            return ModelResponse(**base, model_reported=None, text='', usage={}, cost={'billed_usd': 0.0}, latency_ms=0, raw_blob=None, error='QUOTA_YIELD', error_detail=f'Claude API credit ledger: {g.stdout.strip()[:200]}')
        key = _key(lane['key_env'])
        body = {'model': model, 'system': req.system, 'messages': [{'role': 'user', 'content': req.prompt}], 'max_tokens': max(int(req.max_output_tokens), int(lane.get('min_output_tokens') or 0))}
        if req.system:
            body['system'] = [{'type': 'text', 'text': req.system, 'cache_control': {'type': 'ephemeral'}}]
        if effort:
            body['output_config'] = {'effort': effort}
        r = urllib.request.Request('https://api.anthropic.com/v1/messages', data=json.dumps(body).encode(), method='POST', headers={'x-api-key': key, 'anthropic-version': '2023-06-01', 'content-type': 'application/json'})
        del key
        t0 = _now_ms()
        try:
            with urllib.request.urlopen(r, timeout=_request_timeout(req, int(lane.get('timeout_s', 300)))) as resp:
                raw_s = resp.read().decode()
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors='replace')[:2000]
            code = {429: 'RATE_LIMITED', 529: 'TRANSIENT', 401: 'AUTH', 403: 'AUTH'}.get(e.code, 'TRANSIENT')
            if 'credit balance is too low' in msg:
                code = 'QUOTA_YIELD'
            return ModelResponse(**base, model_reported=None, text='', usage={}, cost={'billed_usd': 0.0}, latency_ms=_now_ms() - t0, raw_blob=put_blob(msg), error=code, error_detail=f'HTTP {e.code}: {msg[:300]}', retry_after_s=retry_hint_s(e.headers.get('retry-after') if e.headers else None, msg))
        except Exception as e:
            return ModelResponse(**base, model_reported=None, text='', usage={}, cost={'billed_usd': 0.0, 'cost_unknown': True}, latency_ms=_now_ms() - t0, raw_blob=None, error='TIMEOUT' if 'timed out' in str(e) else 'TRANSIENT', error_detail=str(e)[:300])
        ms = _now_ms() - t0
        raw = put_blob(raw_s)
        try:
            d = json.loads(raw_s)
            if not isinstance(d, dict) or (d.get('content') is not None and (not isinstance(d['content'], list) or any((not isinstance(b, dict) or (b.get('text') is not None and (not isinstance(b['text'], str))) for b in d['content'])))) or (d.get('usage') is not None and (not isinstance(d['usage'], dict))) or (d.get('model') is not None and (not isinstance(d['model'], str))):
                raise ValueError('invalid provider envelope')
            u = d.get('usage') or {}
            if any((u.get(k) is not None and (not isinstance(u[k], (int, float)) or not math.isfinite(u[k]) or u[k] < 0) for k in ('input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens'))) or any((u.get(k) is not None and (not isinstance(u[k], dict)) for k in ('output_tokens_details', 'cache_creation'))):
                raise ValueError('invalid provider usage')
        except (ValueError, TypeError):
            return ModelResponse(**base, model_reported=None, text='', usage={}, cost={'billed_usd': None, 'cost_unknown': True}, latency_ms=ms, raw_blob=raw, error='TRANSIENT', error_detail='invalid provider JSON envelope; cost unknown')
        text = ''.join((b.get('text', '') for b in d.get('content') or [] if b.get('type') == 'text'))
        u = d.get('usage') or {}
        usage = {'input': (u.get('input_tokens') or 0) + (u.get('cache_read_input_tokens') or 0) + (u.get('cache_creation_input_tokens') or 0), 'cached_input': u.get('cache_read_input_tokens') or 0, 'output': u.get('output_tokens') or 0, 'reasoning': (u.get('output_tokens_details') or {}).get('thinking_tokens')}
        usage['cache_creation_input'] = u.get('cache_creation_input_tokens') or 0
        usage['cache_creation_1h_input'] = (u.get('cache_creation') or {}).get('ephemeral_1h_input_tokens') or 0
        spent = usd_eq(model, usage) or 0.0
        accounting_error = None
        try:
            debit = subprocess.run([str(CREDITS_LEDGER), 'record', who, f'{spent:.6f}', req.decision_id[:80]], capture_output=True, timeout=180)
            if debit.returncode:
                accounting_error = 'credit ledger record failed'
        except Exception as exc:
            accounting_error = type(exc).__name__
        refused = d.get('stop_reason') == 'refusal'
        sd = d.get('stop_details') if refused else None
        category = sd.get('category') if isinstance(sd, dict) and isinstance(sd.get('category'), str) else None
        truncated = d.get('stop_reason') == 'max_tokens'
        return ModelResponse(**base, model_reported=d.get('model'), identity_source='provider_response' if d.get('model') else 'unknown', stop_reason=d.get('stop_reason'), refusal_category=category, text=text, usage=usage, cost={'billed_usd': 0.0, 'usd_eq': spent, 'credits_usd': spent, 'usd_eq_source': 'config/prices.yaml (Max 20x API credits)', **({'accounting_error': accounting_error} if accounting_error else {})}, latency_ms=ms, raw_blob=raw, error='PAID_REFUSED' if accounting_error else 'REFUSED' if refused else 'TRANSIENT' if truncated or not text.strip() else None, error_detail='credit ledger record failed; actual cost retained' if accounting_error else (f'provider stop_reason refusal ({category})' if category else 'provider stop_reason refusal') if refused else 'truncated: stop_reason max_tokens' if truncated else None if text.strip() else f'empty content (stop_reason {d.get('stop_reason')})')
