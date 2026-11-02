from dataclasses import asdict
from datetime import datetime
import hashlib, json
from .context import View
from bm.engine.state import World

def write(league, kind, key, payload):
    data = json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
    digest = hashlib.sha256(data).hexdigest()
    directory = league.dir / 'trade-context'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (digest + '.json')
    if not path.exists():
        temp = path.with_suffix('.tmp')
        temp.write_bytes(data)
        temp.replace(path)
    event = dict(type=kind, key=key, path=str(path.relative_to(league.dir)), sha256=digest)
    league.append(event)
    return json.loads(data)

def read(league, kind, key):
    ev = next((e for e in league.events() if e['type'] == kind and e.get('key') == key), None)
    if ev is None:
        return None
    path = league.dir / ev['path']
    if path.is_symlink() or (league.dir / 'trade-context').is_symlink():
        raise ValueError('trade context symlink invalid')
    if path.parent.resolve() != (league.dir / 'trade-context').resolve():
        raise ValueError('trade context path invalid')
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != ev['sha256']:
        raise ValueError('trade context hash mismatch')
    return json.loads(data)

def freeze_view(league, view, deadline, window):
    payload = {k: v for k, v in asdict(view).items() if k not in ('rules', 'as_of')}
    payload.update(as_of=view.as_of.isoformat(), deadline=deadline.isoformat(), rules_hash=league.rules.rules_hash, entrants={t: league.entrant(t, view.week) for t in league.seats})
    return write(league, 'trade_original_context', window, payload)

def restore_view(league, payload):
    if payload['rules_hash'] != league.rules.rules_hash or payload['entrants'] != {t: league.entrant(t, payload.get('week')) for t in league.seats}:
        raise ValueError('trade original rules/seat identity changed')
    d = dict(payload)
    world = d.pop('world')
    d.pop('deadline')
    d.pop('rules_hash')
    d.pop('entrants')
    world['kickoffs'] = {int(k): v for k, v in world['kickoffs'].items()}
    if world.get('team_by_week') is not None:
        world['team_by_week'] = {int(k): v for k, v in world['team_by_week'].items()}
    d['as_of'] = datetime.fromisoformat(d['as_of'])
    d['lines'] = {int(k): v for k, v in d['lines'].items()}
    return View(rules=league.rules, world=World(**world), **d)
