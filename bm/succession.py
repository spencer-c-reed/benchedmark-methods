from __future__ import annotations
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
import yaml
ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / 'config' / 'successions.yaml'

@lru_cache(maxsize=4)
def _load(path: str, mtime: float) -> dict:
    d = yaml.safe_load(Path(path).read_text()) or {}
    out = []
    for s in d.get('successions') or []:
        s = dict(s)
        s['kind'] = str(s.get('kind') or 'model')
        if s['kind'] not in ('model', 'operator'):
            raise ValueError(f'successions.yaml: unknown kind {s['kind']!r}')
        if s['kind'] == 'operator':
            s.setdefault('retired', s.get('entrant'))
            s.setdefault('successor', s.get('operator'))
            if s.get('picks'):
                raise ValueError(f'successions.yaml: an operator entry ({s['retired']}) is league seats only, never Picks')
        s['from_at'] = datetime.fromisoformat(str(s['from_at']))
        s['leagues'] = list(s.get('leagues') or [])
        out.append(s)
    for a in out:
        for b in out:
            if b['kind'] == 'model' and a['successor'] == b['retired'] and (set(a['leagues']) & set(b['leagues']) or (a.get('picks') and b.get('picks'))):
                raise ValueError(f'successions.yaml: chained succession {a['retired']} -> {b['retired']} -> {b['successor']} is not allowed')
    return {'successions': out, 'watch': list(d.get('watch') or [])}

def load(path: Path | None=None) -> dict:
    p = Path(path or CONFIG)
    if not p.exists():
        return {'successions': [], 'watch': []}
    return _load(str(p), p.stat().st_mtime)

def _now() -> datetime:
    return datetime.now(timezone.utc)

def _active(s: dict, week: int | None, at: datetime | None) -> bool:
    if week is not None:
        return week >= int(s['from_week'])
    at = at or _now()
    if at.tzinfo is None:
        from zoneinfo import ZoneInfo
        at = at.replace(tzinfo=ZoneInfo('America/New_York'))
    return at >= s['from_at']

def _match(entrant: str, league: str | None, week: int | None, at: datetime | None, picks: bool, path) -> dict | None:
    for s in load(path)['successions']:
        if s['retired'] != entrant or (picks and (not s.get('picks'))) or (not picks and league not in s['leagues']):
            continue
        if _active(s, week, at):
            return s
    return None

def controller(entrant: str, *, league: str | None=None, week: int | None=None, at: datetime | None=None, picks: bool=False, path: Path | None=None) -> str:
    s = _match(entrant, league, week, at, picks, path)
    return s['successor'] if s else entrant

def inherited(entrant: str, *, league: str, week: int, path: Path | None=None) -> bool:
    return _match(entrant, league, week, None, False, path) is not None

def operated(entrant: str, *, league: str | None, week: int | None=None, at: datetime | None=None, path: Path | None=None) -> dict | None:
    s = _match(entrant, league, week, at, False, path)
    return s if s and s['kind'] == 'operator' else None

def operator_label(s: dict) -> str:
    return f'operator: {s['successor']} from Week {s['from_week']} ({s.get('reason') or 'operator'})'

def picks_models(models: list[str], at: datetime | None=None, path: Path | None=None) -> list[str]:
    out = []
    for m in models:
        c = controller(m, at=at, picks=True, path=path)
        if c not in out:
            out.append(c)
    return out

def changes(league: str, path: Path | None=None) -> list[dict]:
    return [{k: v.isoformat() if isinstance(v, datetime) else v for k, v in s.items()} for s in load(path)['successions'] if league in s['leagues']]
