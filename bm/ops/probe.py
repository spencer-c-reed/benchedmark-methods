from __future__ import annotations
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

def model_seats() -> list[str]:
    import yaml
    from bm.cli import ROOT, league_cfgs, seat_entrants
    ents = {e['id']: e for e in yaml.safe_load((ROOT / 'config' / 'entrants.yaml').read_text())}
    from bm import succession
    ctl = {succession.controller(s, league=l.get('id')) for l in league_cfgs() for s in seat_entrants(l) if ents.get(s, {}).get('kind') != 'baseline'}
    return sorted((c for c in ctl if ents.get(c, {}).get('kind') != 'baseline'))

def probe(entrants: list[str], label: str) -> dict:
    from bm.gateway.core import Gateway, ModelRequest
    from bm.publish import lanehealth
    gw = Gateway()
    t0 = time.time()
    since = datetime.now(timezone.utc).isoformat()

    def one(e):
        r = gw.call(ModelRequest(entrant_id=e, decision_id=f'probe:{label}:{e}', system='Reply with JSON only.', prompt='Return {"ok":1}', max_output_tokens=400, deadline=datetime.now(timezone.utc) + timedelta(seconds=200)))
        return (e, r.lane, r.error or 'OK', r.attempts, round(time.time() - t0), (r.error_detail or '')[:100])
    with ThreadPoolExecutor(4) as ex:
        res = list(ex.map(one, entrants))
    for x in res:
        print(*x, flush=True)
    m = lanehealth.measure(since)
    lanehealth.record(m, label)
    return {e: err for e, _, err, *_ in res}
if __name__ == '__main__':
    args = sys.argv[1:]
    label = 'probe'
    if args[:1] == ['--label']:
        label, args = (args[1], args[2:])
    out = probe(args or model_seats(), label)
    sys.exit(0 if all((v == 'OK' for v in out.values())) else 3)
