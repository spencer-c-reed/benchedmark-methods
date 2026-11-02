from __future__ import annotations
import hashlib
import json
import re
import sqlite3
from pathlib import Path
CODEX_SSE_PROVIDER = 'model_providers.bm_openai_sse={name="OpenAI",base_url="https://chatgpt.com/backend-api/codex",wire_api="responses",requires_openai_auth=true,supports_websockets=false}'
CODEX_RUST_LOG = 'error,codex_api::sse::responses=trace'
_SSE = 'SSE event: '
_CODEX_ANSWER_ITEMS = {'agent_message', 'reasoning', 'error', 'todo_list'}
_RESPONSE_ANSWER_ITEMS = {'message', 'reasoning'}
_DROP = ('tools', 'instructions', 'safety_identifier', 'prompt_cache_key', 'user')
EFFORT_SUFFIX = re.compile('-(minimal|low|medium|high|xhigh|max)$')
SERVED_ALIASES = {'gemini-3.8-flash-n': 'gemini-3.8-flash'}

def same_model(requested: str | None, served: str | None) -> bool:
    a = str(requested or '').lower().split('/')[-1].replace(':free', '')
    b = str(served or '').lower().split('/')[-1].replace(':free', '')
    if not a or not b:
        return False
    if a == b or b.startswith(a + '-') or a.startswith(b + '-'):
        return True
    return EFFORT_SUFFIX.sub('', a) == SERVED_ALIASES.get(b, b)

def codex_events(out: str, err: str) -> dict:
    created = completed = None
    response_tools = 0
    for line in err.splitlines():
        i = line.find(_SSE)
        if i < 0:
            continue
        try:
            ev = json.loads(line[i + len(_SSE):])
        except ValueError:
            continue
        t = ev.get('type')
        if t == 'response.created' and isinstance(ev.get('response'), dict):
            created = ev['response']
        elif t == 'response.completed' and isinstance(ev.get('response'), dict):
            completed = ev['response']
        elif t == 'response.output_item.done' and (ev.get('item') or {}).get('type') not in _RESPONSE_ANSWER_ITEMS:
            response_tools += 1
    json_tools = 0
    for line in out.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get('type') == 'item.completed' and (ev.get('item') or {}).get('type') not in _CODEX_ANSWER_ITEMS:
            json_tools += 1
    resp = completed or created
    model = resp.get('model') if resp and isinstance(resp.get('model'), str) and resp['model'].strip() else None
    evidence = None
    if resp:
        evidence = {k: v for k, v in resp.items() if k not in _DROP and k != 'output'}
        evidence['event'] = 'response.completed' if completed else 'response.created'
        evidence['output'] = [{'type': o.get('type'), 'id': o.get('id')} for o in resp.get('output') or [] if isinstance(o, dict)]
    return {'model': model, 'evidence': evidence, 'tool_proof': {'tool_calls': max(json_tools, response_tools), 'json_tool_items': json_tools, 'response_tool_items': response_tools, 'evidence': 'codex exec --json items + Responses API output items'}}

def non_sse_stderr(err: str, limit: int=4000) -> str:
    return '\n'.join((l for l in err.splitlines() if _SSE not in l and 'unhandled responses event' not in l))[-limit:]

def _fields(b: bytes):
    i, n = (0, len(b))

    def varint():
        nonlocal i
        r = s = 0
        while True:
            x = b[i]
            i += 1
            r |= (x & 127) << s
            s += 7
            if not x & 128:
                return r
    while i < n:
        key = varint()
        f, w = (key >> 3, key & 7)
        if w == 0:
            yield (f, varint())
        elif w == 2:
            ln = varint()
            yield (f, b[i:i + ln])
            i += ln
        elif w == 1:
            i += 8
        elif w == 5:
            i += 4
        else:
            return
AGY_SERVED_PATH = (1, 19)
_MODEL_ID = re.compile(b'[a-z][a-z0-9.\\-]{2,80}')

def _walk(b: bytes, path: tuple=AGY_SERVED_PATH):
    try:
        for f, v in _fields(b):
            if f != path[0] or not isinstance(v, bytes):
                continue
            if len(path) > 1:
                yield from _walk(v, path[1:])
            elif _MODEL_ID.fullmatch(v):
                yield v.decode()
    except (IndexError, ValueError):
        return

def agy_served(db: Path) -> list[str]:
    if not db.exists():
        return []
    try:
        con = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=5)
        try:
            rows = con.execute('select data from gen_metadata').fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []
    seen: list[str] = []
    for data, in rows:
        if isinstance(data, bytes):
            for m in _walk(data):
                if m not in seen:
                    seen.append(m)
    return seen

def file_sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None

def claude_tool_proof(d: dict) -> dict:
    turns = d.get('num_turns')
    server = (d.get('usage') or {}).get('server_tool_use') or {}
    denials = d.get('permission_denials') or []
    srv = sum((v for v in server.values() if isinstance(v, (int, float)) and (not isinstance(v, bool))))
    calls = None if not isinstance(turns, int) else max(turns - 1, 0) + len(denials) + int(srv)
    return {'tool_calls': calls, 'num_turns': turns, 'permission_denials': len(denials), 'server_tool_requests': int(srv), 'stop_reason': d.get('stop_reason'), 'evidence': 'claude -p result: num_turns, permission_denials, server_tool_use'}
