from __future__ import annotations
import argparse
import json
from pathlib import Path
import yaml
ROOT = Path(__file__).resolve().parents[2]
SCORECARD = ROOT / 'export' / 'scorecard.json'
ENTRANTS_YAML = ROOT / 'config' / 'entrants.yaml'
f1 = lambda x: '—' if x is None else f'{x:+.1f}'
pct = lambda x: '—' if x is None else f'{x:.0%}'
BOARDS = {'league': {'headline_label': 'Points vs Projection Bot (descriptive diagnostic, not a headline; certification off)', 'unit': 'points a week above the Projection Bot (bot weaker than intended in Weeks 5-6)', 'fmt': f1, 'cells': [('Own decisions', lambda r: pct(r.get('own_share')) if r.get('own_share') is not None else 'bot'), ('Draft', lambda r: f1(r['components']['draft'])), ('Waivers', lambda r: f1(r['components']['waivers'])), ('Trades', lambda r: f1(r['components']['trades'])), ('Lineups', lambda r: f1(r['components']['lineups']))]}, 'picks': {'headline_label': 'Closing-line value', 'unit': 'points a pick, pooled across 3 instances', 'fmt': lambda x: f'{x:+.2f}', 'cells': []}, 'agentic': {'headline_label': 'Agentic composite', 'unit': 'standardized, 1x budget tier', 'fmt': lambda x: f'{x:+.2f}', 'cells': []}, 'alignment': {'headline_label': 'Clean-conduct score', 'unit': '1 minus verified misconduct per opportunity', 'fmt': lambda x: f'{x:.0%}', 'cells': [('Dishonesty', lambda r: _kn(r, 'dishonesty')), ('Omission', lambda r: _kn(r, 'omission')), ('Collusion', lambda r: _kn(r, 'collusion')), ('Broken promises', lambda r: _kn(r, 'broken_promises'))]}, 'efficiency': {'headline_label': 'Cost frontier', 'unit': 'Pareto tier on TMV and list-price dollars a seat-week', 'fmt': lambda x: f'tier {int(x)}', 'cells': [('TMV', lambda r: f1(r.get('tmv'))), ('Dollars a seat-week', lambda r: '—' if r.get('list_usd_per_seat_week') is None else f'${r['list_usd_per_seat_week']:.2f}'), ('Points per dollar', lambda r: '—' if r.get('points_per_dollar') is None else f'{r['points_per_dollar']:.1f}')]}, 'sim': {'headline_label': 'Sim TMV', 'unit': 'points a week', 'fmt': f1, 'cells': []}}

def _kn(r: dict, comp: str) -> str:
    c = (r.get('components') or {}).get(comp)
    return '—' if not c else f'{c['k']} of {c['n']}'

def build_boards(scorecard: dict, entrants: dict) -> dict:
    out = {}
    for bid, cfg in BOARDS.items():
        rows = scorecard.get('boards', {}).get(bid) or []
        if bid == 'efficiency':
            rows = [dict(r, headline=r['pareto_tier'], ranked=r.get('ranked', False), ci=[None, None], tier=r['pareto_tier']) for r in rows]
        name = lambda e: (entrants.get(e) or {}).get('name', e)
        ranked = [r for r in rows if r.get('ranked') and r.get('headline') is not None]
        unranked = [{'entrant_id': r['entrant_id'], 'name': name(r['entrant_id']), 'descriptive_value': r.get('tmv') if bid == 'efficiency' else r.get('headline'), 'ci': r.get('ci'), 'cells': [fn(r) for _, fn in cfg['cells']], 'components': r.get('components'), 'reason': r.get('unranked_reason') or 'not enough data yet'} for r in rows if r.get('entrant_id') and (not (r.get('ranked') and r.get('headline') is not None))]
        b = {'headline_label': cfg['headline_label'], 'unit': cfg['unit'], 'better': 'higher', 'cells_labels': [c[0] for c in cfg['cells']], 'rows': [{'entrant_id': r['entrant_id'], 'name': name(r['entrant_id']), 'headline': cfg['fmt'](r['headline']), 'value': r['headline'], 'lo': (r.get('ci') or [None, None])[0], 'hi': (r.get('ci') or [None, None])[1], 'tier': r.get('tier'), 'color': f'var(--c-{(entrants.get(r['entrant_id']) or {}).get('color_slot') or 0})', 'delta': 0, 'cells': [fn(r) for _, fn in cfg['cells']], 'style': r.get('style', [])} for r in ranked], 'unranked_rows': unranked}
        if not ranked:
            b['empty_state'] = 'Not enough data yet: ' + (unranked[0]['reason'] if unranked else next((r.get('unranked_reason') for r in rows if r.get('unranked_reason')), 'this board starts when its first week is scored'))
        out[bid] = b
    out['notes'] = scorecard.get('notes', [])
    out['generated_at'] = scorecard.get('generated_at')
    return out

def main():
    ap = argparse.ArgumentParser(description='export/scorecard.json -> boards.json for the site')
    ap.add_argument('--out', type=Path, default=ROOT / 'export')
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    sc = json.loads(SCORECARD.read_text())
    ents = {e['id']: e for e in yaml.safe_load(ENTRANTS_YAML.read_text())}
    (a.out / 'boards.json').write_text(json.dumps(build_boards(sc, ents), indent=1))
    print(f'Wrote {a.out / 'boards.json'}')
if __name__ == '__main__':
    main()
