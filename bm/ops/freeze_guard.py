from __future__ import annotations
import hashlib
import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
import yaml
RECHECK_S = 5.0

class ManifestRefused(Exception):
    pass

def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')

def body_hash(manifest: dict) -> str:
    return hashlib.sha256(canonical({k: v for k, v in manifest.items() if k != 'manifest_sha256'})).hexdigest()

@dataclass
class Scope:
    track: str
    routes: list
    effort: str | None
    manifest_sha256: str
    manifest: str

    def tag(self, route: tuple) -> dict:
        return {'sha256': self.manifest_sha256, 'path': self.manifest, 'track': self.track, 'alternate': tuple(route) != tuple(self.routes[0])}

class RouteGuard:

    def __init__(self, root: Path, code_root: Path):
        self.root, self.code_root = (Path(root), Path(code_root))
        self._lock = threading.Lock()
        self._required: dict[str, str] = {}
        self._req_key = None
        self._checked = 0.0
        self._manifests: dict[str, tuple] = {}

    def required(self) -> dict[str, str]:
        now = time.monotonic()
        with self._lock:
            if now - self._checked < RECHECK_S and self._req_key is not None:
                return self._required
            self._checked = now
            files = sorted((self.root / 'config').glob('leagues*.yaml'))
            key = tuple(((str(f), f.stat().st_mtime_ns) for f in files if f.exists()))
            if key == self._req_key:
                return self._required
            out: dict[str, str] = {}
            for f in files:
                try:
                    c = yaml.safe_load(f.read_text()) or {}
                except Exception:
                    text = f.read_text(errors='replace') if f.exists() else ''
                    if 'freeze_manifest' in text:
                        for line in text.splitlines():
                            s = line.strip()
                            if s.startswith('- id:'):
                                out[s.split(':', 1)[1].strip().strip('"\'')] = '<unreadable config ' + f.name + '>'
                    continue
                path = c.get('freeze_manifest') if isinstance(c, dict) else None
                if not path:
                    continue
                for l in c.get('leagues') or []:
                    if isinstance(l, dict) and l.get('id'):
                        out[str(l['id'])] = str(path)
            self._required, self._req_key = (out, key)
            return out

    def _load(self, path: str) -> dict:
        if path.startswith('<unreadable'):
            raise ManifestRefused(f'league config is unreadable {path[1:-1]}; frozen leagues refuse until it parses')
        p = Path(path) if Path(path).is_absolute() else self.root / path
        try:
            st = p.stat()
        except OSError:
            raise ManifestRefused(f'no frozen manifest at {path}: record it with python3 -m bm.ops.freeze record')
        cached = self._manifests.get(str(p))
        if cached and cached[0] == st.st_mtime_ns:
            return cached[1]
        try:
            m = json.loads(p.read_text())
        except (OSError, ValueError) as e:
            raise ManifestRefused(f'manifest {path} unreadable: {type(e).__name__}')
        if not isinstance(m, dict) or m.get('manifest_sha256') != body_hash(m):
            raise ManifestRefused(f'manifest {path} self-hash invalid')
        if not isinstance(m.get('routes'), dict):
            raise ManifestRefused(f'manifest {path} has no route table (record it from a config with seat_routes)')
        self._manifests[str(p)] = (st.st_mtime_ns, m)
        return m

    def _check_code(self, m: dict, path: str) -> None:
        for rel in m.get('runtime_check') or []:
            want = ((m.get('files') or {}).get(rel) or {}).get('sha256')
            try:
                have = hashlib.sha256((self.code_root / rel).read_bytes()).hexdigest()
            except OSError:
                have = None
            if want is None or have != want:
                raise ManifestRefused(f'{rel} changed since the freeze ({path}); record a successor manifest after review')

    def scope(self, decision_id: str, entrant: str, track: str | None, effort: str | None) -> Scope | None:
        req = self.required()
        league = (decision_id or '').split(':', 1)[0]
        if track == 'panel':
            paths = sorted(set(req.values()))
            if not paths:
                raise ManifestRefused('panel track requested but no league config names a freeze_manifest')
            hits = []
            for path in paths:
                m = self._load(path)
                if (m['routes'].get(entrant) or {}).get('panel'):
                    hits.append((path, m))
            if not hits:
                raise ManifestRefused(f'no frozen panel route for {entrant}')
            path, m = hits[-1]
        elif league in req:
            track, path = ('league', req[league])
            m = self._load(path)
            if league not in (m.get('route_leagues') or []):
                raise ManifestRefused(f'manifest {path} does not cover league {league}')
        elif track not in (None, 'league'):
            raise ManifestRefused(f'unknown track {track!r}')
        else:
            return None
        r = m['routes'].get(entrant)
        if not r or not r.get(track):
            raise ManifestRefused(f'manifest {path} has no {track} route for {entrant}')
        frozen = None if r.get('effort') in (None, 'vendor-default') else r['effort']
        if effort is not None and (None if effort == 'vendor-default' else effort) != frozen:
            raise ManifestRefused(f'effort {effort} differs from frozen {frozen or 'vendor-default'} for {entrant}')
        self._check_code(m, path)
        return Scope(track, [tuple(x) for x in r[track]], frozen, m['manifest_sha256'], path)
