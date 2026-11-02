"""Offline checks on released methods, using only invented input values."""
import hashlib
import importlib
import json
from pathlib import Path

root = Path(__file__).resolve().parent
manifest = json.loads((root / 'SOURCE-MANIFEST.json').read_text())
for row in manifest['files']:
    assert hashlib.sha256((root / row['path']).read_bytes()).hexdigest() == row['released_sha256'], row['path']
for path in sorted((root / 'bm').rglob('*.py')):
    name = '.'.join(path.relative_to(root).with_suffix('').parts)
    importlib.import_module(name.removesuffix('.__init__'))
from bm.engine.rules import load_rules
from bm.engine.scoring import score_player_line
for path in (root / 'rules').glob('*.yaml'):
    rules = load_rules(path)
    assert score_player_line(rules, 'QB', {'passing_yards': 250, 'passing_tds': 2}) == 18.0
    assert score_player_line(rules, 'RB', {'rushing_yards': 100, 'rushing_tds': 1}) == 16.0
from bm.agent import prompts, prompts_restart
expected = json.loads((root / 'PROMPT-IDENTITIES.json').read_text())
assert prompts.template_hash() == expected['nfl-proto-4']
assert prompts_restart.template_hash() == expected['nfl-restart-1']
from bm.score.validity import decision_validity
assert not decision_validity({}, lock=None, template_hash=None, calls=[])['valid']
from bm.league.private import house_probes
try:
    house_probes(None, None, None)
except NotImplementedError:
    pass
else:
    raise AssertionError('Private scenario generator must remain excluded')
print('All source hashes, imports, rules, prompt identities, unknown-evidence rejection and scenario redaction checks passed.')
