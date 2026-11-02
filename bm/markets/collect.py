from __future__ import annotations
import argparse
import fcntl
import http.client
import gzip
import hashlib
import json
import logging
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
ROOT = Path(__file__).resolve().parents[2]
ET = ZoneInfo('America/New_York')
UA = {'User-Agent': 'Mozilla/5.0 (BenchedMark-research/0.1; +benchedmark.com)', 'Accept': 'application/json'}
log = logging.getLogger('bm.markets')
KALSHI = 'https://api.elections.kalshi.com/trade-api/v2'
GAMMA = 'https://gamma-api.polymarket.com'
CLOB = 'https://clob.polymarket.com'
ODDS = 'https://api.the-odds-api.com/v4'
PRE_WINDOW = timedelta(hours=3)
POST_WINDOW = timedelta(hours=4)
HOURLY_EVERY = timedelta(minutes=55)
SETTLE_EVERY = timedelta(hours=20)
GAME_LENGTH_KALSHI = timedelta(hours=3)

def markets_dir() -> Path:
    return Path(os.environ.get('BM_MARKETS_DIR') or ROOT / 'data' / 'markets')

def live_odds_state_path() -> Path:
    return Path(os.environ.get('BM_LIVE_ODDS_STATE') or markets_dir().parent / 'raw' / 'market' / 'odds-keys.json')
_last_call: dict[str, float] = {}
PACE = {'api.elections.kalshi.com': 0.25, 'gamma-api.polymarket.com': 0.3, 'clob.polymarket.com': 0.25, 'api.the-odds-api.com': 1.0}

def _http(url: str, *, data: bytes | None=None, tries: int=5, want_headers: bool=False):
    host = url.split('/')[2]
    err: Exception | None = None
    for i in range(tries):
        wait = PACE.get(host, 0.5) - (time.monotonic() - _last_call.get(host, 0.0))
        if wait > 0:
            time.sleep(wait)
        _last_call[host] = time.monotonic()
        try:
            headers = dict(UA, **{'Content-Type': 'application/json'} if data is not None else {})
            with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=60) as r:
                body = r.read()
                return (body, r.headers) if want_headers else body
        except urllib.error.HTTPError as e:
            err = e
            if e.code == 429 or e.code >= 500:
                ra = e.headers.get('Retry-After') if e.headers else None
                delay = float(ra) if ra and ra.replace('.', '', 1).isdigit() else 2.0 * 2 ** i
                log.warning('%s HTTP %s; backing off %.0fs', host, e.code, delay)
                time.sleep(min(delay, 120))
                continue
            raise
        except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
            err = e
            time.sleep(2.0 * 2 ** i)
    raise RuntimeError(f'{host}: gave up after {tries} tries ({type(err).__name__})')

def _json(url: str, data=None):
    return json.loads(_http(url, data=None if data is None else json.dumps(data).encode()))

def _append(rel: str, rows: list[dict]) -> int:
    if not rows:
        return 0
    p = markets_dir() / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    before = p.stat().st_size if p.exists() else 0
    with gzip.open(p, 'at', compresslevel=6, encoding='utf-8') as f:
        for r in rows:
            f.write(json.dumps(r, separators=(',', ':'), sort_keys=True) + '\n')
    return p.stat().st_size - before

def _day(ts: datetime) -> str:
    return ts.strftime('%Y-%m-%d')

def _month(ts: datetime) -> str:
    return ts.strftime('%Y-%m')

def load_state() -> dict:
    try:
        return json.loads((markets_dir() / '_state' / 'state.json').read_text())
    except (OSError, ValueError):
        return {}

def save_state(state: dict) -> None:
    p = markets_dir() / '_state' / 'state.json'
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps(state, separators=(',', ':'), sort_keys=True))
    tmp.replace(p)

def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).replace(microsecond=0).isoformat()

def _parse(s) -> datetime | None:
    if not s:
        return None
    try:
        t = datetime.fromisoformat(str(s).replace('Z', '+00:00').replace(' ', 'T', 1))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t

def _f(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None

def _changed(cache: dict, key: str, row: dict, full: bool) -> bool:
    h = hashlib.blake2b(json.dumps({k: v for k, v in row.items() if k not in ('ts', 'mode', 'book_ts')}, sort_keys=True).encode(), digest_size=6).hexdigest()
    if not full and cache.get(key) == h:
        return False
    cache[key] = h
    return True

def _dedupe(rows: list[dict], cache: dict, keyf, full: bool, prune: bool=False) -> list[dict]:
    out = [r for r in rows if _changed(cache, keyf(r), r, full)]
    if prune:
        live = {keyf(r) for r in rows}
        for k in [k for k in cache if k not in live]:
            del cache[k]
    return out

def _full_refresh(state: dict, now: datetime, mode: str) -> bool:
    return mode == 'hourly' and state.get('keyframe_day') != _day(now)

def in_window(kickoff: datetime | None, now: datetime) -> bool:
    return kickoff is not None and kickoff - PRE_WINDOW <= now <= kickoff + POST_WINDOW
KALSHI_GAME = ['KXNFLGAME', 'KXNFLSPREAD', 'KXNFLTOTAL', 'KXNFLTEAMTOTAL']
KALSHI_PLAYER = ['KXNFLPASSYDS', 'KXNFLRSHYDS', 'KXNFLRECYDS', 'KXNFLREC', 'KXNFLANYTD', 'KXNFLTD', 'KXNFLFIRSTTD', 'KXNFLPASSTDS', 'KXNFLRRYDS']
KALSHI_FUTURES = ['KXSB', 'KXNFLMVP', 'KXNFLOPOTY', 'KXNFLOPOY', 'KXNFLDPOTY', 'KXNFLDPOY', 'KXNFLOROTY', 'KXNFLOROY', 'KXNFLDROTY', 'KXNFLDROY', 'KXNFLCPOTY', 'KXNFLCOTY', 'KXNFLSBMVP', 'KXNFLAFCCHAMP', 'KXNFLNFCCHAMP', 'KXNFLPLAYOFF', 'KXNFLSEED', 'KXNFLWINS', 'KXNFLAFCEAST', 'KXNFLAFCNORTH', 'KXNFLAFCSOUTH', 'KXNFLAFCWEST', 'KXNFLNFCEAST', 'KXNFLNFCNORTH', 'KXNFLNFCSOUTH', 'KXNFLNFCWEST', 'KXLEADERNFLPYDS', 'KXLEADERNFLRUSHYDS', 'KXLEADERNFLRYDS', 'KXLEADERNFLPTDS', 'KXLEADERNFLRTDS', 'KXLEADERNFLRUSHTDS', 'KXLEADERNFLSACKS', 'KXLEADERNFLINT', 'KXNFLSEASONPASSYDS', 'KXNFLSEASONRSHYDS', 'KXNFLSEASONRECYDS', 'KXNFLSEASONREC']
KALSHI_PER_GAME = KALSHI_GAME + KALSHI_PLAYER
KALSHI_KEEP = ('ticker', 'status', 'close_time', 'expected_expiration_time', 'yes_bid_dollars', 'yes_ask_dollars', 'yes_bid_size_fp', 'yes_ask_size_fp', 'no_bid_dollars', 'no_ask_dollars', 'last_price_dollars', 'volume_fp', 'volume_24h_fp', 'open_interest_fp', 'liquidity_dollars')
KALSHI_META = ('ticker', 'event_ticker', 'title', 'subtitle', 'yes_sub_title', 'no_sub_title', 'market_type', 'strike_type', 'floor_strike', 'cap_strike', 'custom_strike', 'open_time', 'close_time', 'expected_expiration_time', 'occurrence_datetime', 'can_close_early', 'notional_value_dollars', 'rules_primary', 'rules_secondary', 'early_close_condition', 'price_level_structure', 'price_ranges', 'settlement_bounds_type')
KALSHI_SETTLEMENT = ('result', 'expiration_value', 'settlement_value_dollars', 'settlement_value', 'settlement_ts')
KALSHI_DONE = {'determined', 'finalized', 'settled', 'amended'}

def kalshi_row(m: dict, ts: str, mode: str) -> dict:
    row = {k: m[k] for k in KALSHI_KEEP if k in m and m[k] not in (None, '')}
    row.update(ts=ts, mode=mode)
    return row

def kalshi_settlement(m: dict, ts: str) -> dict | None:
    got = {k: m[k] for k in KALSHI_SETTLEMENT if m.get(k) not in (None, '')}
    if not got:
        return None
    return dict(got, ticker=m.get('ticker'), event_ticker=m.get('event_ticker'), status=m.get('status'), close_time=m.get('close_time'), recorded_at=ts)

def kalshi_two_sided(m: dict) -> bool:
    bid, ask = (_f(m.get('yes_bid_dollars')), _f(m.get('yes_ask_dollars')))
    return bid is not None and ask is not None and (bid >= 0.01) and (0 < ask <= 0.99)

def kalshi_list(series: str, status: str='open', **params) -> list[dict]:
    out, cursor = ([], None)
    q = '&'.join((f'{k}={v}' for k, v in params.items()))
    while True:
        url = f'{KALSHI}/markets?series_ticker={series}&limit=1000' + (f'&status={status}' if status else '')
        url += (f'&{q}' if q else '') + (f'&cursor={cursor}' if cursor else '')
        d = _json(url)
        out += d.get('markets') or []
        cursor = d.get('cursor')
        if not cursor:
            return out

def kalshi_by_tickers(tickers: list[str]) -> list[dict]:
    out = []
    for i in range(0, len(tickers), 100):
        out += _json(f'{KALSHI}/markets?limit=1000&tickers={','.join(tickers[i:i + 100])}').get('markets') or []
    return out

def kalshi_books(tickers: list[str], ts: str, mode: str) -> list[dict]:
    rows = []
    for i in range(0, len(tickers), 100):
        q = '&'.join(('tickers=' + t for t in tickers[i:i + 100]))
        for ob in _json(f'{KALSHI}/markets/orderbooks?{q}').get('orderbooks') or []:
            book = ob.get('orderbook_fp') or ob.get('orderbook') or {}
            rows.append({'ts': ts, 'mode': mode, 'ticker': ob.get('ticker'), 'yes': book.get('yes_dollars') or book.get('yes') or [], 'no': book.get('no_dollars') or book.get('no') or []})
    return rows

def _suffix(event_ticker: str) -> str:
    return event_ticker.split('-', 1)[1] if '-' in event_ticker else event_ticker

def kalshi_kickoffs(game_markets: list[dict]) -> dict[str, str]:
    out = {}
    for m in game_markets:
        t = _parse(m.get('expected_expiration_time'))
        if t and m.get('event_ticker'):
            out[_suffix(m['event_ticker'])] = _iso(t - GAME_LENGTH_KALSHI)
    return out

def collect_kalshi(now: datetime, mode: str, state: dict) -> dict:
    ts, rep = (_iso(now), {'mode': mode, 'markets': 0, 'books': 0, 'bytes': 0, 'errors': []})
    ks = state.setdefault('kalshi', {})
    seen_meta = set(ks.get('meta_seen') or [])
    rows, meta, settle, book_tickers = ([], [], [], [])
    if mode == 'hourly':
        by_series: dict[str, list[dict]] = {}
        for s in KALSHI_PER_GAME + KALSHI_FUTURES:
            try:
                by_series[s] = kalshi_list(s)
            except Exception as e:
                rep['errors'].append(f'{s}: {type(e).__name__}')
        kick = kalshi_kickoffs(by_series.get('KXNFLGAME', [])) or ks.get('kickoffs', {})
        events: dict[str, list[str]] = {}
        for s, ms in by_series.items():
            for m in ms:
                if m.get('status') in KALSHI_DONE:
                    continue
                rows.append(kalshi_row(m, ts, mode))
                if (st := kalshi_settlement(m, ts)):
                    settle.append(st)
                if m['ticker'] not in seen_meta:
                    meta.append(dict({k: m[k] for k in KALSHI_META if k in m}, series=s, first_seen=ts))
                    seen_meta.add(m['ticker'])
                if kalshi_two_sided(m):
                    book_tickers.append(m['ticker'])
                if s in KALSHI_PER_GAME:
                    events.setdefault(_suffix(m.get('event_ticker', '')), []).append(m['ticker'])
        ks['kickoffs'] = {k: v for k, v in kick.items() if _parse(v) + POST_WINDOW >= now}
        if rep['errors']:
            events = {**(ks.get('events') or {}), **events}
        ks['events'] = {k: v for k, v in events.items() if k in ks['kickoffs']}
    else:
        tickers = sorted({t for suf, kt in (ks.get('kickoffs') or {}).items() if in_window(_parse(kt), now) for t in (ks.get('events') or {}).get(suf, [])})
        if not tickers:
            rep['skipped'] = 'no game in window'
            return rep
        try:
            for m in kalshi_by_tickers(tickers):
                if m.get('status') in KALSHI_DONE:
                    if (st := kalshi_settlement(m, ts)):
                        settle.append(st)
                    continue
                rows.append(kalshi_row(m, ts, mode))
                if kalshi_two_sided(m):
                    book_tickers.append(m['ticker'])
        except Exception as e:
            rep['errors'].append(f'tickers: {type(e).__name__}')
    books = []
    try:
        books = kalshi_books(book_tickers, ts, mode)
    except Exception as e:
        rep['errors'].append(f'books: {type(e).__name__}')
    d = _day(now)
    full = _full_refresh(state, now, mode)
    prune = mode == 'hourly' and (not rep['errors'])
    seen = (len(rows), len(books))
    rows = _dedupe(rows, ks.setdefault('row_hash', {}), lambda r: r['ticker'], full, prune)
    books = _dedupe(books, ks.setdefault('book_hash', {}), lambda r: r['ticker'], full, prune)
    rep.update(seen_markets=seen[0], seen_books=seen[1])
    rep['bytes'] += _append(f'kalshi/markets/{d}.jsonl.gz', rows)
    rep['bytes'] += _append(f'kalshi/books/{d}.jsonl.gz', books)
    rep['bytes'] += _append(f'kalshi/meta/{_month(now)}.jsonl.gz', meta)
    rep['bytes'] += _append(f'_settlement/kalshi/{_month(now)}.jsonl.gz', settle)
    ks['meta_seen'] = sorted(seen_meta)[-60000:]
    rep.update(markets=len(rows), books=len(books), settlements=len(settle))
    return rep

def settle_kalshi(now: datetime, state: dict) -> dict:
    ks = state.setdefault('kalshi', {})
    since = _parse(ks.get('settled_through')) or now - timedelta(days=3)
    ts, rows, errors = (_iso(now), [], [])
    for s in KALSHI_PER_GAME + KALSHI_FUTURES:
        try:
            for m in kalshi_list(s, status='settled', min_settled_ts=int(since.timestamp())):
                if (st := kalshi_settlement(m, ts)):
                    rows.append(st)
        except Exception as e:
            errors.append(f'{s}: {type(e).__name__}')
    b = _append(f'_settlement/kalshi/{_month(now)}.jsonl.gz', rows)
    if not errors:
        ks['settled_through'] = _iso(now - timedelta(minutes=5))
    return {'settled': len(rows), 'bytes': b, 'errors': errors}
PM_GAME_TYPES = {'moneyline', 'spreads', 'totals', 'team_totals', 'first_half_moneyline', 'first_half_spreads', 'first_half_totals'}
PM_FUTURES = re.compile('champion|mvp|player of the year|rookie of the year|coach of the year|comeback|postseason|playoff|division|conference|super bowl|big game winner|leader|win total|wins|seed|undefeated|season series|1st overall pick', re.I)
PM_KEEP = ('id', 'bestBid', 'bestAsk', 'lastTradePrice', 'spread', 'volumeNum', 'volume24hr', 'liquidityNum', 'active', 'acceptingOrders')
PM_META = ('id', 'conditionId', 'slug', 'question', 'groupItemTitle', 'sportsMarketType', 'line', 'outcomes', 'clobTokenIds', 'gameStartTime', 'startDate', 'endDate', 'negRisk', 'orderPriceMinTickSize', 'description')
PM_SETTLEMENT = ('outcomePrices', 'umaResolutionStatus', 'umaResolutionStatuses', 'closedTime', 'resolvedBy')

def pm_event_kind(e: dict) -> str | None:
    ms = e.get('markets') or []
    if ms and ms[0].get('gameStartTime') and str(e.get('slug', '')).startswith('nfl-'):
        rest = re.sub('^nfl-[a-z0-9]+-[a-z0-9]+-\\d{4}-\\d\\d-\\d\\d-?', '', e['slug'])
        return 'game' if rest == '' else 'player' if rest in ('player-props', 'first-td-scorer') else None
    return 'futures' if PM_FUTURES.search(e.get('title') or '') else None

def pm_row(m: dict, e: dict, kind: str, ts: str, mode: str) -> dict:
    row = {k: m[k] for k in PM_KEEP if k in m and m[k] not in (None, '')}
    row.update(ts=ts, mode=mode)
    return row

def pm_meta(m: dict, e: dict, kind: str, ts: str) -> dict:
    return dict({k: m[k] for k in PM_META if k in m and m[k] not in (None, '')}, kind=kind, event_slug=e.get('slug'), event_title=e.get('title'), first_seen=ts)

def pm_yes_token(m: dict) -> str | None:
    try:
        toks = json.loads(m.get('clobTokenIds') or '[]')
    except ValueError:
        return None
    return toks[0] if toks else None

def pm_two_sided(m: dict) -> bool:
    bid, ask = (_f(m.get('bestBid')), _f(m.get('bestAsk')))
    return bid is not None and ask is not None and (bid >= 0.01) and (0 < ask <= 0.99)

def pm_books(tokens: list[str], ts: str, mode: str) -> list[dict]:
    rows = []
    for i in range(0, len(tokens), 100):
        for b in _json(f'{CLOB}/books', [{'token_id': t} for t in tokens[i:i + 100]]) or []:
            rows.append({'ts': ts, 'mode': mode, 'token': b.get('asset_id'), 'market': b.get('market'), 'book_ts': b.get('timestamp'), 'last': b.get('last_trade_price'), 'bids': [[x.get('price'), x.get('size')] for x in b.get('bids') or []], 'asks': [[x.get('price'), x.get('size')] for x in b.get('asks') or []]})
    return rows

def pm_events(closed: bool=False, **params) -> 'list[dict]':
    off = 0
    q = ''.join((f'&{k}={v}' for k, v in params.items()))
    while True:
        page = _json(f'{GAMMA}/events?tag_slug=nfl&closed={('true' if closed else 'false')}&limit=100&offset={off}{q}')
        if not page:
            return
        yield page
        if len(page) < 100:
            return
        off += 100

def collect_polymarket(now: datetime, mode: str, state: dict) -> dict:
    ts, rep = (_iso(now), {'mode': mode, 'markets': 0, 'books': 0, 'bytes': 0, 'errors': []})
    ps = state.setdefault('polymarket', {})
    seen_meta = set(ps.get('meta_seen') or [])
    rows, tokens, meta = ([], [], [])
    if mode == 'hourly':
        games: dict[str, dict] = {}
        try:
            for page in pm_events():
                for e in page:
                    kind = pm_event_kind(e)
                    if kind is None:
                        continue
                    for m in e.get('markets') or []:
                        if m.get('closed') or m.get('umaResolutionStatus') == 'resolved':
                            continue
                        if kind == 'game' and m.get('sportsMarketType') not in PM_GAME_TYPES:
                            continue
                        rows.append(pm_row(m, e, kind, ts, mode))
                        if m.get('id') not in seen_meta:
                            meta.append(pm_meta(m, e, kind, ts))
                            seen_meta.add(m.get('id'))
                        tok = pm_yes_token(m) if pm_two_sided(m) else None
                        if tok:
                            tokens.append(tok)
                        if kind in ('game', 'player') and (ko := _parse(m.get('gameStartTime'))):
                            g = games.setdefault(re.sub('-(player-props|first-td-scorer)$', '', e['slug']), {'kickoff': _iso(ko), 'tokens': []})
                            if tok:
                                g['tokens'].append(tok)
                del page
        except Exception as e:
            rep['errors'].append(f'gamma: {type(e).__name__}')
        if games and (not rep['errors']):
            ps['games'] = {k: v for k, v in games.items() if _parse(v['kickoff']) + POST_WINDOW >= now}
    else:
        tokens = sorted({t for g in (ps.get('games') or {}).values() if in_window(_parse(g['kickoff']), now) for t in g['tokens']})
        if not tokens:
            rep['skipped'] = 'no game in window'
            return rep
    books = []
    try:
        books = pm_books(tokens, ts, mode)
    except Exception as e:
        rep['errors'].append(f'books: {type(e).__name__}')
    d = _day(now)
    full = _full_refresh(state, now, mode)
    prune = mode == 'hourly' and (not rep['errors'])
    seen = (len(rows), len(books))
    rows = _dedupe(rows, ps.setdefault('row_hash', {}), lambda r: r['id'], full, prune)
    books = _dedupe(books, ps.setdefault('book_hash', {}), lambda r: str(r['token'])[-16:], full, prune)
    rep.update(seen_markets=seen[0], seen_books=seen[1])
    rep['bytes'] += _append(f'polymarket/markets/{d}.jsonl.gz', rows)
    rep['bytes'] += _append(f'polymarket/meta/{_month(now)}.jsonl.gz', meta)
    rep['bytes'] += _append(f'polymarket/books/{d}.jsonl.gz', books)
    ps['meta_seen'] = sorted(seen_meta)[-60000:]
    rep.update(markets=len(rows), books=len(books))
    return rep

def settle_polymarket(now: datetime, state: dict) -> dict:
    ps = state.setdefault('polymarket', {})
    done = set(ps.get('settled_ids') or [])
    ts, rows, errors = (_iso(now), [], [])
    try:
        for page in pm_events(closed=True, end_date_min=(now - timedelta(days=4)).strftime('%Y-%m-%dT%H:%M:%SZ')):
            for e in page:
                for m in e.get('markets') or []:
                    if m.get('id') in done or not m.get('closed'):
                        continue
                    got = {k: m[k] for k in PM_SETTLEMENT if m.get(k) not in (None, '')}
                    if got:
                        rows.append(dict(got, id=m.get('id'), conditionId=m.get('conditionId'), slug=m.get('slug'), question=m.get('question'), outcomes=m.get('outcomes'), event_slug=e.get('slug'), recorded_at=ts))
                        done.add(m.get('id'))
    except Exception as e:
        errors.append(f'gamma closed: {type(e).__name__}')
    ps['settled_ids'] = sorted(done)[-50000:]
    return {'settled': len(rows), 'bytes': _append(f'_settlement/polymarket/{_month(now)}.jsonl.gz', rows), 'errors': errors}
PROPS_MARKETS = ('player_pass_yds', 'player_rush_yds', 'player_reception_yds', 'player_receptions', 'player_anytime_td')
PROPS_COST = len(PROPS_MARKETS)
LIVE_CALL_COST = 3
RESET_BUFFER = timedelta(days=2)
ADHOC_MARGIN = 30
GAMES_PER_WEEK = 16
T2H = timedelta(hours=2, minutes=5)
SLOTS = {2: 'wed', 4: 'fri'}

def _month_end(now: datetime) -> datetime:
    n = now.astimezone(timezone.utc)
    first_next = (n.replace(day=28) + timedelta(days=4)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return first_next + RESET_BUFFER

def live_need_credits(now: datetime) -> int:
    from bm.stats import market
    return market.planned_odds_credits(market.planned_odds_pulls(now, _month_end(now))) + ADHOC_MARGIN

def pool_gate(remaining: dict[str, float | None], label: str, live_need: int, cost: int, extra_reserve: int=0) -> tuple[bool, str]:
    rem = remaining.get(label)
    if rem is None:
        return (False, f'{label} remaining unknown')
    share = math.ceil(live_need / max(1, len(remaining)))
    if rem - cost < share:
        return (False, f'{label} would drop to {rem - cost:.0f} < its live share {share}')
    total = sum((v for v in remaining.values() if v is not None))
    if total - cost < live_need + extra_reserve:
        return (False, f'pool would drop to {total - cost:.0f} < reserve {live_need + extra_reserve}')
    return (True, 'ok')

def _pick_key(remaining: dict[str, float | None], live_need: int, cost: int, extra_reserve: int, exclude: set[str]) -> tuple[str | None, str]:
    why = None
    for label in sorted((l for l in remaining if l not in exclude and remaining[l] is not None), key=lambda l: -remaining[l]):
        ok, why_l = pool_gate(remaining, label, live_need, cost, extra_reserve)
        if ok:
            return (label, 'ok')
        why = why or why_l
    return (None, why or 'no usable key (remaining unknown or refused)')

def _known_remaining(labels: list[str], own: dict) -> dict[str, float | None]:
    try:
        live = json.loads(live_odds_state_path().read_text())
    except (OSError, ValueError):
        live = {}
    from bm.stats.market import odds_quota_label
    out = {}
    for label in labels:
        group = odds_quota_label(label)
        recs = [r for source in (live, own) for name, r in source.items() if odds_quota_label(name) == group and isinstance(r, dict) and (_f(r.get('remaining')) is not None)]
        best = max(recs, key=lambda r: r.get('seen_at') or '', default=None)
        out[label] = _f(best.get('remaining')) if best else None
    return out

def _props_keys() -> list[tuple[str, str]]:
    from bm.stats.market import _odds_api_keys
    only = os.environ.get('BM_PROPS_ODDS_KEY_LABEL')
    from bm.stats.market import odds_quota_label
    result = []
    seen = set()
    for label, key in _odds_api_keys():
        if only and label != only:
            continue
        group = odds_quota_label(label)
        if group not in seen:
            result.append((label, key))
            seen.add(group)
    return result

def _odds(path: str, key: str) -> tuple[object, dict]:
    sep = '&' if '?' in path else '?'
    body, h = _http(f'{ODDS}/{path}{sep}apiKey={key}', want_headers=True, tries=2)
    quota = {k: h.get(f'x-requests-{k}') for k in ('remaining', 'used', 'last') if h.get(f'x-requests-{k}') is not None}
    return (json.loads(body), quota)

def collect_props(now: datetime, state: dict) -> dict:
    pr = state.setdefault('props', {})
    rep: dict = {'pulled': 0, 'skipped': [], 'credits': 0, 'bytes': 0, 'errors': []}
    et = now.astimezone(ET)
    slot = SLOTS.get(et.weekday()) if et.hour >= 12 else None
    slot_key = f'{slot}:{et.date()}' if slot else None
    slot_due = bool(slot_key) and slot_key not in (pr.get('slots_done') or [])
    events = pr.get('events') or []
    refresh = _parse(pr.get('events_at')) is None or now - _parse(pr['events_at']) >= timedelta(hours=6)
    done_t2h = set(pr.get('t2h_done') or [])

    def t2h_due(evs):
        return [e for e in evs if e['id'] not in done_t2h and timedelta(0) < (_parse(e['commence_time']) or now) - now <= T2H]
    if not (slot_due or refresh or t2h_due(events)):
        rep['skipped'].append('nothing due')
        return rep
    pool = dict(_props_keys())
    if not pool:
        rep['errors'].append('no odds api key in the pool')
        return rep
    own = pr.setdefault('keys', {})
    remaining = _known_remaining(list(pool), own)
    refused: set[str] = set()
    evs = None
    for first in sorted(pool, key=lambda l: -(remaining.get(l) if remaining.get(l) is not None else -1)):
        try:
            evs, quota = _odds('sports/americanfootball_nfl/events', pool[first])
            break
        except Exception as e:
            rep['errors'].append(f'events {first}: {type(e).__name__}')
            if isinstance(e, urllib.error.HTTPError) and e.code in (401, 403, 429):
                refused.add(first)
                continue
            return rep
    if evs is None:
        return rep
    if _f(quota.get('remaining')) is not None:
        remaining[first] = _f(quota['remaining'])
        own[first] = {'remaining': remaining[first], 'seen_at': _iso(now)}
        from bm.stats.market import odds_record_quota
        odds_record_quota(first, quota, datetime.now(timezone.utc), live_odds_state_path())
    events = [{'id': e['id'], 'commence_time': e['commence_time'], 'home_team': e.get('home_team'), 'away_team': e.get('away_team')} for e in evs if isinstance(e, dict) and e.get('id')]
    pr.update(events=events, events_at=_iso(now))
    live_need = live_need_credits(now)
    end = _month_end(now)
    days_left = max(0.0, (end - now).total_seconds() / 86400)
    known_ahead = sum((1 for e in events if now < (_parse(e['commence_time']) or now) <= end and e['id'] not in done_t2h))
    t2h_reserve = PROPS_COST * max(known_ahead, math.ceil(GAMES_PER_WEEK * days_left / 7))
    rep.update(remaining_before=dict(remaining), live_need=live_need, t2h_reserve=t2h_reserve, keys_used={})
    plan = [('t2h', e, 0) for e in t2h_due(events)]
    if slot_due:
        soon = {e['id'] for e in t2h_due(events)}
        plan += [(slot, e, t2h_reserve) for e in events if e['id'] not in soon and timedelta(0) < (_parse(e['commence_time']) or now) - now <= timedelta(days=6)]
    rows = []
    for tag, e, extra in plan:
        while True:
            label, why = _pick_key(remaining, live_need, PROPS_COST, extra, refused)
            if label is None:
                rep['skipped'].append(f'{tag} {e['id'][:8]}: {why}')
                if tag == 't2h':
                    done_t2h.add(e['id'])
                break
            try:
                data, q = _odds(f'sports/americanfootball_nfl/events/{e['id']}/odds?regions=us&oddsFormat=american&markets={','.join(PROPS_MARKETS)}', pool[label])
            except Exception as ex:
                rep['errors'].append(f'{tag} {e['id'][:8]} {label}: {type(ex).__name__}')
                if isinstance(ex, urllib.error.HTTPError) and ex.code in (401, 403, 429):
                    refused.add(label)
                    continue
                break
            if _f(q.get('remaining')) is not None:
                remaining[label] = _f(q['remaining'])
                observed_at = datetime.now(timezone.utc)
                own[label] = {'remaining': remaining[label], 'seen_at': _iso(observed_at)}
                from bm.stats.market import odds_record_quota
                odds_record_quota(label, q, observed_at, live_odds_state_path())
            cost = int(_f(q.get('last')) or 0)
            rep['credits'] += cost
            rep['keys_used'][label] = rep['keys_used'].get(label, 0) + cost
            rows.append({'ts': _iso(datetime.now(timezone.utc)), 'tag': tag, 'event_id': e['id'], 'commence_time': e['commence_time'], 'home_team': e.get('home_team'), 'away_team': e.get('away_team'), 'markets': list(PROPS_MARKETS), 'regions': 'us', 'cost': cost, 'key': label, 'bookmakers': (data or {}).get('bookmakers') or []})
            rep['pulled'] += 1
            if tag == 't2h':
                done_t2h.add(e['id'])
            break
    if slot_due:
        pr['slots_done'] = ((pr.get('slots_done') or []) + [slot_key])[-20:]
    keep = {e['id'] for e in events}
    pr['t2h_done'] = sorted((i for i in done_t2h if i in keep))
    pr.pop('key_remaining', None)
    rep['remaining_after'] = dict(remaining)
    rep['bytes'] = _append(f'props/{_day(now)}.jsonl.gz', rows)
    return rep

def run(mode: str='auto', only: set[str] | None=None, settle: bool | None=None, now: datetime | None=None) -> dict:
    now = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    only = only or {'kalshi', 'polymarket', 'props'}
    lock_path = markets_dir() / '_state' / 'run.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'ts': _iso(now), 'skipped': 'previous run still going'}
        state = load_state()
        last_h = _parse(state.get('last_hourly'))
        if mode == 'auto':
            mode = 'hourly' if last_h is None or now - last_h >= HOURLY_EVERY else 'fast'
        last_s = _parse(state.get('last_settle'))
        settle = settle if settle is not None else last_s is None or now - last_s >= SETTLE_EVERY
        t0 = time.monotonic()
        rep: dict = {'ts': _iso(now), 'mode': mode}
        for name, fn in (('kalshi', collect_kalshi), ('polymarket', collect_polymarket)):
            if name in only:
                try:
                    rep[name] = fn(now, mode, state)
                except Exception as e:
                    rep[name] = {'errors': [f'{type(e).__name__}']}
                    log.exception('%s failed', name)
        if 'props' in only:
            try:
                rep['props'] = collect_props(now, state)
            except Exception as e:
                rep['props'] = {'errors': [f'{type(e).__name__}']}
                log.exception('props failed')
        if settle:
            rep['settle'] = {}
            if 'kalshi' in only:
                rep['settle']['kalshi'] = settle_kalshi(now, state)
            if 'polymarket' in only:
                rep['settle']['polymarket'] = settle_polymarket(now, state)
            state['last_settle'] = _iso(now)
        if mode == 'hourly':
            state['last_hourly'] = _iso(now)
            if not any(((rep.get(k) or {}).get('errors') for k in ('kalshi', 'polymarket'))):
                state['keyframe_day'] = _day(now)
        rep['secs'] = round(time.monotonic() - t0, 1)
        save_state(state)
        with open(markets_dir() / 'runs.jsonl', 'a') as f:
            f.write(json.dumps(rep, separators=(',', ':'), sort_keys=True) + '\n')
        return rep

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description='BenchedMark T1 market price archive')
    p.add_argument('--mode', choices=('auto', 'hourly', 'fast'), default='auto')
    p.add_argument('--only', default='kalshi,polymarket,props')
    p.add_argument('--settle', action='store_true', help='run the settlement pass now')
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    rep = run(a.mode, set(a.only.split(',')), True if a.settle else None)
    print(json.dumps(rep, separators=(',', ':'), sort_keys=True))
    errs = [x for k in ('kalshi', 'polymarket', 'props') for x in (rep.get(k) or {}).get('errors', [])]
    dead = [k for k in ('kalshi', 'polymarket') if k in rep and rep[k].get('errors') and (not rep[k].get('markets')) and (not rep[k].get('books')) and (not rep[k].get('skipped'))]
    if errs:
        log.warning('errors: %s', errs)
    return 1 if dead else 0
if __name__ == '__main__':
    sys.exit(main())
