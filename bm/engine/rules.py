from __future__ import annotations
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import yaml
ROOT = Path(__file__).resolve().parents[2]
STARTER_ORDER = ['QB', 'RB', 'WR', 'TE', 'FLEX', 'K', 'DEF']

@dataclass(frozen=True)
class Rules:
    version: str
    sport: str
    teams_per_league: int
    roster_slots: dict[str, int]
    eligibility: dict[str, list[str]]
    max_per_position: dict[str, int]
    scoring: dict[str, float]
    kicking: dict[str, float]
    defense: dict[str, Any]
    faab_budget: int
    faab_min_bid: int
    trades: dict[str, int]
    max_msg_chars: int
    chatter_posts_per_window: int
    first_week: int
    last_week: int
    rules_hash: str
    raw: dict = field(repr=False, default_factory=dict)

    @property
    def roster_size(self) -> int:
        return sum(self.roster_slots.values())

    @property
    def starter_slots(self) -> list[str]:
        out = []
        for pos in STARTER_ORDER:
            for i in range(self.roster_slots.get(pos, 0)):
                out.append(f'{pos}{i + 1}')
        return out

    def slot_type(self, slot_key: str) -> str:
        return slot_key.rstrip('0123456789')

    def eligible(self, slot_key: str, position: str) -> bool:
        return position in self.eligibility.get(self.slot_type(slot_key), [])

def load_rules(path: str | Path=ROOT / 'rules' / 'nfl-s1.yaml') -> Rules:
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    b = p.read_bytes()
    d = yaml.safe_load(b)
    return Rules(version=d['rules_version'], sport=d['sport'], teams_per_league=d['teams_per_league'], roster_slots=dict(d['roster_slots']), eligibility={k: list(v) for k, v in d['eligibility'].items()}, max_per_position=dict(d.get('max_per_position', {})), scoring=dict(d['scoring']), kicking=dict(d['kicking']), defense=dict(d['defense']), faab_budget=int(d['faab_budget']), faab_min_bid=int(d.get('faab_min_bid', 0)), trades=dict(d['trades']), max_msg_chars=int(d['messages']['max_chars']), chatter_posts_per_window=int(d['chatter']['posts_per_window']), first_week=int(d['season']['first_week']), last_week=int(d['season']['last_week']), rules_hash=hashlib.sha256(b).hexdigest(), raw=d)
