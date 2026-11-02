from __future__ import annotations
from dataclasses import dataclass
FEE_MARKET_USD = 0.01

@dataclass(frozen=True)
class Policy:
    season_usd: float
    weekly_floor_usd: float
    first_week: int
    last_week: int
    tier: float = 1.0
    token_usd_per_faab: float | None = None

    @property
    def pot(self) -> float:
        return round(self.season_usd * self.tier, 6)

    @property
    def floor(self) -> float:
        return round(self.weekly_floor_usd * self.tier, 6)
REFERENCE = dict(season_usd=300.0, weekly_floor_usd=3.0)

def policy_for(league_cfg: dict, tier: float=1.0) -> Policy:
    b = {**REFERENCE, **(league_cfg.get('budget') or {})}
    first = int(league_cfg.get('start_week'))
    last = int(league_cfg.get('end_week') or 17)
    return Policy(season_usd=float(b['season_usd']), weekly_floor_usd=float(b['weekly_floor_usd']), first_week=first, last_week=last, tier=float(tier), token_usd_per_faab=b.get('token_usd_per_faab'))

def balance(p: Policy, events: list[dict], team: str, week: int) -> dict:
    spent = spent_week = 0.0
    converted = 0.0
    for e in events:
        if e.get('team') != team:
            continue
        if e['type'] == 'budget_debit':
            spent += e['usd']
            if e.get('week') == week:
                spent_week += e['usd']
        elif e['type'] == 'wallet_convert' and p.token_usd_per_faab:
            converted += -int(e['faab_delta']) * p.token_usd_per_faab
    pot_left = p.pot + converted - spent
    later = max(0, p.last_week - week)
    reserved = p.floor * later
    spendable = pot_left - reserved
    return {'pot': p.pot, 'converted_usd': round(converted, 6), 'spent_usd': round(spent, 6), 'spent_this_week_usd': round(spent_week, 6), 'pot_left_usd': round(pot_left, 6), 'reserved_future_floors_usd': round(reserved, 6), 'spendable_usd': round(spendable, 6), 'exhausted': spendable <= 0, 'tier': p.tier}

def debit(team: str, week: int, usd: float, why: str, record_id: str | None=None, ts: str | None=None) -> dict:
    return {'type': 'budget_debit', 'ts': ts, 'team': team, 'week': week, 'usd': round(float(usd), 6), 'why': why, 'record_id': record_id}

def header(b: dict) -> str:
    return f"Budget: ${b['spent_this_week_usd']:.2f} spent this week; ${max(b['spendable_usd'], 0):.2f} spendable now of ${b['pot_left_usd']:.2f} left this season (${b['reserved_future_floors_usd']:.2f} held for later weeks' floors)"
