from __future__ import annotations
import contextlib
import fcntl
import hashlib
import json
import os
import subprocess
import threading
import time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
GATE = Path.home() / 'scripts' / 'heartbeat' / 'notify-gate.sh'
STATE = ROOT / 'state' / 'alerts.json'
SUPPRESS_S = 3600
PENDING_S = 120

@contextlib.contextmanager
def _locked(path: Path | None=None):
    path = path or STATE
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + '.lock'), 'a') as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)

def _load(path: Path | None=None) -> dict:
    try:
        seen = json.loads((path or STATE).read_text())
        return seen if isinstance(seen, dict) else {}
    except (OSError, ValueError):
        return {}

def _save(seen: dict, path: Path | None=None) -> None:
    path = path or STATE
    tmp = path.with_name(f'{path.name}.{os.getpid()}.{threading.get_ident()}.tmp')
    tmp.write_text(json.dumps(seen))
    os.replace(tmp, path)

def stamp(keys, path: Path | None=None) -> None:
    with _locked(path):
        seen = _load(path)
        now = time.time()
        for k in keys:
            seen[k] = now
        _save(seen, path)

def _claim(key: str, suppress_s: float | None=None) -> bool:
    with _locked():
        seen = _load()
        now = time.time()
        if now - float(seen.get(key, 0)) < (SUPPRESS_S if suppress_s is None else suppress_s) or now - float(seen.get('pending|' + key, 0)) < PENDING_S:
            return False
        seen = {k: v for k, v in seen.items() if now - float(v) < 86400}
        seen['pending|' + key] = now
        _save(seen)
        return True

def _settle(key: str, sent: bool) -> None:
    with _locked():
        seen = _load()
        seen.pop('pending|' + key, None)
        if sent:
            seen[key] = time.time()
        _save(seen)

def alert(kind: str, message: str, key: str | None=None, every_s: float | None=None) -> bool:
    try:
        key = hashlib.sha256((f'keyed|{kind}|{key}' if key else f'{kind}|{message[:200]}').encode()).hexdigest()[:16]
        if not _claim(key, every_s):
            return False
    except Exception as e:
        print('alert failed:', e, flush=True)
        return False
    sent = False
    try:
        p = subprocess.run([str(GATE), '--source', f'benchedmark-{kind}', message[:1500]], capture_output=True, text=True, timeout=60)
        if p.returncode != 0:
            print('alert gate rc', p.returncode, (p.stderr or '')[-200:], flush=True)
        sent = p.returncode == 0
    except Exception as e:
        print('alert failed:', e, flush=True)
    try:
        _settle(key, sent)
    except Exception as e:
        print('alert state failed:', e, flush=True)
    return sent
WAKE = Path.home() / 'scripts' / 'wakeup-inject.sh'
OWNER_CHAT = '-4996918674'
OWNER_SESSION = f'chat:{OWNER_CHAT}'

def wake(key: str, message: str) -> bool:
    try:
        k = 'wake|' + hashlib.sha256(key.encode()).hexdigest()[:16]
        if not _claim(k):
            return False
    except Exception as e:
        print('wake failed:', e, flush=True)
        return alert('stall', message)
    woke = False
    try:
        p = subprocess.run([str(WAKE), OWNER_CHAT, OWNER_SESSION, '-'], input=message[:3000], capture_output=True, text=True, timeout=30)
        woke = p.returncode == 0
        if woke:
            print(f'[wake] owning chat woken: {message[:160]}', flush=True)
        else:
            print('wake inject rc', p.returncode, (p.stderr or '')[-200:], flush=True)
    except Exception as e:
        print('wake failed:', e, flush=True)
    try:
        _settle(k, woke)
    except Exception as e:
        print('wake state failed:', e, flush=True)
    return True if woke else alert('stall', message)

def covered_key(unit: str) -> str:
    return f'unit-covered|{unit}'

def covered(unit: str) -> bool:
    try:
        with _locked():
            return time.time() - float(_load().get(covered_key(unit), 0)) < SUPPRESS_S
    except Exception:
        return False

def on_unit_failure(unit: str) -> bool:
    if covered(unit):
        print(f'{unit} failed; its cause was already alerted by the unit itself (deduped)')
        return False
    tail = subprocess.run(['journalctl', '--user', '-u', unit, '-n', '8', '--no-pager', '-o', 'cat'], capture_output=True, text=True).stdout[-700:]
    return alert('unit-failed', f'BenchedMark: {unit} FAILED. Last log lines:\n{tail}')
if __name__ == '__main__':
    import sys
    on_unit_failure(sys.argv[1] if len(sys.argv) > 1 else 'unknown unit')
