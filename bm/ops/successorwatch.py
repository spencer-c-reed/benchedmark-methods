from __future__ import annotations
import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / 'state' / 'successor-watch.json'
ALERTS = ROOT / 'state' / 'alerts.json'
AGY = Path(os.environ.get('AGY_BIN', Path.home() / '.local' / 'bin' / 'agy'))
OPENROUTER_MODELS = 'https://openrouter.ai/api/v1/models'
ET = ZoneInfo('America/New_York')

def agy_models() -> list[str]:
    p = subprocess.run([str(AGY), 'models'], capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        raise RuntimeError(f'agy models rc={p.returncode}: {(p.stderr or p.stdout)[-200:]}')
    return [ln.split()[0] for ln in p.stdout.splitlines() if ln.strip() and (not ln.startswith('Fetching')) and ('\t' in ln)]

def openrouter_models() -> list[str]:
    req = urllib.request.Request(OPENROUTER_MODELS, headers={'User-Agent': 'benchedmark-successor-watch'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return [m['id'] for m in json.load(r).get('data', []) if isinstance(m, dict) and m.get('id')]
SOURCES = {'agy': agy_models, 'openrouter': openrouter_models}

def _read(p: Path) -> dict:
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}

def check(now: datetime | None=None, *, force: bool=False, sources: dict | None=None, notify: bool=True) -> dict | None:
    from bm import succession
    now = now or datetime.now(timezone.utc)
    st = _read(STATE)
    today = now.astimezone(ET).date().isoformat()
    if not force and st.get('last_day') == today:
        return None
    watch = succession.load()['watch']
    if not watch:
        return None
    srcs = sources or SOURCES
    need = sorted({s for w in watch for s in w.get('sources') or srcs})
    lists, errors = ({}, {})
    for name in need:
        try:
            lists[name] = srcs[name]()
        except Exception as e:
            errors[name] = repr(e)[:200]
    seen = st.get('seen') or {}
    new = []
    for w in watch:
        pat = str(w['pattern']).lower()
        for name in w.get('sources') or need:
            for slug in lists.get(name, []):
                if pat in slug.lower():
                    k = f'{w['name']}|{name}:{slug}'
                    if k not in seen:
                        seen[k] = now.isoformat()
                        new.append({'watch': w['name'], 'source': name, 'slug': slug})
    rep = {'last_day': today, 'checked_at': now.isoformat(), 'counts': {k: len(v) for k, v in lists.items()}, 'errors': errors, 'seen': seen, 'new': new}
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(rep, indent=1))
    if new and notify:
        msg = 'BenchedMark successor watch: ' + '; '.join((f'{n['watch']} slug {n['slug']} now listed on {n['source']}' for n in new)) + '. Action: add it as a NEW ENTRANT via late-entry leagues + Picks (entrants.yaml row, probe first; a paid route needs a spend authorization). Never a seat swap of an existing or inherited team.'
        from bm.ops import alert as _alert
        _alert.stamp([f'successor-watch|{n['watch']}|{n['source']}:{n['slug']}' for n in new], ALERTS)
        _alert.alert('successor-watch', msg)
    return rep

def main(argv: list[str] | None=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    rep = check(force='--force' in argv, notify='--no-alert' not in argv)
    print(json.dumps(rep if rep is not None else {'skipped': 'already checked today'}, indent=1))
    return 0
if __name__ == '__main__':
    raise SystemExit(main())
