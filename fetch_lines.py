"""Kalshi Line Shop: pull this week's NFL moneylines from Kalshi and FanDuel/DraftKings,
do all the math, write lines.json for index.html.

Sources
  Kalshi     public markets API, no key (KXNFLGAME: one market per team;
             KXNFLTOTAL: a ladder of "Over X.5" markets per game, Under = buy No)
  FD + DK    SharpAPI free tier, key in env SHARPAPI_KEY; one call each for moneylines and main totals
  Schedule   nflverse games.csv (week, kickoff, codes); also the consensus-line fallback

Stdlib only, so the Actions job needs no pip install.
Run:  SHARPAPI_KEY=sk_live_... python3 fetch_lines.py [out.json]
"""
import csv, io, json, math, os, sys, time, urllib.request
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2/markets?status=open&limit=1000&series_ticker="
SHARP = "https://api.sharpapi.io/api/v1/odds?league=nfl&market=moneyline&limit=200"
# market=total alone also returns team totals, halves and every alternate line (600+ rows, past the
# 500 offset cap). total_points + is_main_line is one page with each book's single main total.
SHARP_TOTAL = "https://api.sharpapi.io/api/v1/odds?league=nfl&market=total_points&is_main_line=true&limit=200"
NFLVERSE = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"

# ---------------------------------------------------------------- odds math
# These must match the brief exactly; test_math.py pins them.

def am_to_prob(a):
    return (-a) / (-a + 100) if a < 0 else 100 / (a + 100)

def _round_half_up(x):
    return int(math.floor(x + 0.5))

def prob_to_am(p):
    if p >= 0.5:
        return -_round_half_up(100 * p / (1 - p))
    return _round_half_up(100 * (1 - p) / p)

def taker_fee(p):
    return 0.07 * p * (1 - p)

def tap_cost(ask):
    return ask + taker_fee(ask)

def profit_book(a):
    """Profit on a $100 stake at American odds a."""
    return a if a > 0 else 10000 / (-a)

def profit_price(c):
    """Profit on $100 at an all-in Kalshi cost c per $1 contract."""
    return (1 - c) / c * 100

def never_pay_cents(a):
    """Highest whole-cent Kalshi price that still beats the book.
    Rounded to 9 places first so 0.55 * 100 = 55.000000000000007 doesn't ceil to 56."""
    return math.ceil(round(am_to_prob(a) * 100, 9)) - 1

def fair_probs(a_away, a_home):
    """Multiplicative devig of a two-way line -> (away, home)."""
    pa, ph = am_to_prob(a_away), am_to_prob(a_home)
    return pa / (pa + ph), ph / (pa + ph)

def vs_book(bid, best_book):
    return profit_price(bid) - profit_book(best_book)

# ---------------------------------------------------------------- team codes
# Canonical = nflverse codes. Everything else is mapped onto them.
ALIASES = {"JAC": "JAX", "LAR": "LA", "LVR": "LV", "WSH": "WAS", "LA": "LA"}
canon = lambda c: ALIASES.get(c.upper(), c.upper())

NAMES = {
    "ARI": "Cardinals", "ATL": "Falcons", "BAL": "Ravens", "BUF": "Bills", "CAR": "Panthers",
    "CHI": "Bears", "CIN": "Bengals", "CLE": "Browns", "DAL": "Cowboys", "DEN": "Broncos",
    "DET": "Lions", "GB": "Packers", "HOU": "Texans", "IND": "Colts", "JAX": "Jaguars",
    "KC": "Chiefs", "LA": "Rams", "LAC": "Chargers", "LV": "Raiders", "MIA": "Dolphins",
    "MIN": "Vikings", "NE": "Patriots", "NO": "Saints", "NYG": "Giants", "NYJ": "Jets",
    "PHI": "Eagles", "PIT": "Steelers", "SEA": "Seahawks", "SF": "49ers", "TB": "Buccaneers",
    "TEN": "Titans", "WAS": "Commanders",
}

# ---------------------------------------------------------------- fetching

def get(url, headers=None, tries=3):
    h = {"User-Agent": "kalshi-line-shop/1.0", **(headers or {})}
    last = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=30) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001 - report and retry
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"{url.split('?')[0]}: {last}")

def schedule(now):
    """This week's not-yet-started games from nflverse, keyed (gameday, away, home)."""
    rows = list(csv.DictReader(io.StringIO(get(NFLVERSE).decode())))
    season = max(int(r["season"]) for r in rows)
    games = []
    for r in rows:
        if int(r["season"]) != season or not r["gametime"]:
            continue
        kick = datetime.strptime(f'{r["gameday"]} {r["gametime"]}', "%Y-%m-%d %H:%M").replace(tzinfo=ET)
        if kick <= now:
            continue
        ml = lambda v: int(float(v)) if v not in ("", "NA") else None
        tl = float(r["total_line"]) if r.get("total_line") not in (None, "", "NA") else None
        games.append({
            "week": int(r["week"]), "game_type": r["game_type"], "gameday": r["gameday"],
            "kickoff": kick, "away": canon(r["away_team"]), "home": canon(r["home_team"]),
            "cons": {"away": ml(r["away_moneyline"]), "home": ml(r["home_moneyline"])},
            "cons_total": {"line": tl, "over": ml(r.get("over_odds", "")), "under": ml(r.get("under_odds", ""))},
        })
    if not games:
        return season, None, []
    week = min(g["week"] for g in games)
    return season, week, sorted((g for g in games if g["week"] == week), key=lambda g: (g["kickoff"], g["away"]))

def kalshi_markets(series):
    markets, cursor = [], None
    for _ in range(10):
        d = json.loads(get(KALSHI_BASE + series + (f"&cursor={cursor}" if cursor else "")))
        markets += d.get("markets", [])
        cursor = d.get("cursor")
        if not cursor:
            break
    return markets

_f = lambda m, k: float(m[k]) if m.get(k) not in (None, "") else 0.0

def kalshi():
    """({(gameday, away, home): {team: {bid, ask, volume, ticker}}}, {(gameday, away, home): event body})
    The body ("26OCT18HOUJAC") is shared by every Kalshi series for that game, so totals key off it."""
    markets = kalshi_markets("KXNFLGAME")
    by_event, bodies = {}, {}
    for m in markets:
        by_event.setdefault(m["event_ticker"], []).append(m)
    out = {}
    for ev, ms in by_event.items():
        # KXNFLGAME-26OCT18HOUJAC: yymmmdd (ET) then away+home codes glued together
        body = ev.split("-", 1)[1]
        date = datetime.strptime(body[:7].title(), "%y%b%d").date().isoformat()
        pair = body[7:]
        codes = [m["ticker"].rsplit("-", 1)[1] for m in ms]
        away = next((c for c in codes if pair.startswith(c) and pair[len(c):] in codes), None)
        if away is None or len(codes) != 2:
            continue
        home = pair[len(away):]
        teams = {}
        for m in ms:
            c = m["ticker"].rsplit("-", 1)[1]
            teams[canon(c)] = {"bid": _f(m, "yes_bid_dollars"), "ask": _f(m, "yes_ask_dollars"),
                               "volume": _f(m, "volume_fp"), "ticker": m["ticker"]}
        out[(date, canon(away), canon(home))] = teams
        bodies[(date, canon(away), canon(home))] = body
    return out, bodies

def kalshi_totals():
    """{event body: {strike: {"over": {bid, ask, ...}, "under": {...}}}}. Over = Yes, Under = No."""
    out = {}
    for m in kalshi_markets("KXNFLTOTAL"):
        if m.get("strike_type") != "greater" or m.get("floor_strike") is None:
            continue
        body = m["event_ticker"].split("-", 1)[1]
        vol = _f(m, "volume_fp")
        out.setdefault(body, {})[float(m["floor_strike"])] = {
            "over": {"bid": _f(m, "yes_bid_dollars"), "ask": _f(m, "yes_ask_dollars"), "volume": vol, "ticker": m["ticker"]},
            "under": {"bid": _f(m, "no_bid_dollars"), "ask": _f(m, "no_ask_dollars"), "volume": vol, "ticker": m["ticker"]},
        }
    return out

def sharp_rows(url, key):
    d = json.loads(get(url, {"X-API-Key": key}))
    rows = d.get("data", [])
    if (d.get("pagination") or {}).get("has_more"):
        d2 = json.loads(get(url + "&offset=200", {"X-API-Key": key}))
        rows += d2.get("data", [])
    seen = set()
    for r in rows:
        if r.get("id") in seen or r.get("is_live") or r.get("is_active") is False \
                or r.get("is_main_line") is False or r.get("is_stale_pregame_price"):
            continue
        seen.add(r.get("id"))
        book = {"fanduel": "FD", "draftkings": "DK"}.get(r.get("sportsbook"))
        if not book:
            continue
        # FanDuel stamps kickoff a minute after DraftKings, so match on the ET date, not the time.
        start = datetime.strptime(r["event_start_time"].replace("Z", "+00:00")[:16] + "+00:00",
                                  "%Y-%m-%dT%H:%M%z").astimezone(ET).date().isoformat()
        k = (start, canon(r["away"]["abbreviation"]), canon(r["home"]["abbreviation"]))
        yield k, book, r
    return d.get("updated_at")

def sharp(key):
    """{(gameday, away, home): {"FD": {"away": a, "home": a}, "DK": {...}}}"""
    out, gen = {}, sharp_rows(SHARP, key)
    try:
        while True:
            k, book, r = next(gen)
            side = r.get("team_side") or r.get("selection_type")
            if side in ("away", "home"):
                out.setdefault(k, {}).setdefault(book, {})[side] = int(r["odds_american"])
    except StopIteration as stop:
        return out, stop.value

def sharp_totals(key):
    """{(gameday, away, home): {"FD": {line: {"over": a, "under": a}}, "DK": {...}}}"""
    out = {}
    for k, book, r in sharp_rows(SHARP_TOTAL, key):
        side = r.get("selection_type")
        if r.get("market_type") != "total_points" or side not in ("over", "under") or r.get("line") is None:
            continue
        out.setdefault(k, {}).setdefault(book, {}).setdefault(float(r["line"]), {})[side] = int(r["odds_american"])
    return out

# ---------------------------------------------------------------- assembly

def team_block(code, side, k, books, pair=("away", "home")):
    """One row of the board. books = {"FD": {side_a: odds, side_b: odds}, ...}; pair names the two sides."""
    t = {"code": code, "name": NAMES.get(code, code)}
    for b in ("FD", "DK", "Consensus"):
        if b in books:
            t[b] = books[b].get(side)
    prices = {b: books[b].get(side) for b in books if books[b].get(side) is not None}
    best_book = max(prices, key=lambda b: profit_book(prices[b])) if prices else None
    best = prices.get(best_book)
    t["best_book"], t["best"] = best_book, best
    # Fair odds: average of each book's devigged probability where that book has both sides.
    a, h = pair
    fairs = [fair_probs(v[a], v[h])[0 if side == a else 1]
             for v in books.values() if v.get(a) is not None and v.get(h) is not None]
    if fairs:
        fp = sum(fairs) / len(fairs)
        t["fair_pct"], t["fair_am"] = round(fp * 100, 1), prob_to_am(fp)
    if k:
        bid, ask = k["bid"], k["ask"]
        t.update(bid_c=round(bid * 100), ask_c=round(ask * 100), volume=round(k["volume"]), ticker=k["ticker"])
        if 0 < bid < 1:
            t["limit_am"] = prob_to_am(bid)
        if 0 < ask < 1:
            t["tap_am"] = prob_to_am(tap_cost(ask))
            if best is not None:
                t["tap_vs_book"] = round(profit_price(tap_cost(ask)) - profit_book(best), 2)
        if 0 < bid < 1 and best is not None:
            t["vs_book"] = round(vs_book(bid, best), 2)
            t["beats"] = t["vs_book"] > 0.005
    if best is not None:
        t["never_pay"] = never_pay_cents(best)
    return t

def pick_total_line(book_lines, ladder):
    """book_lines = {"FD": {line: {...}}, ...}. Prefer a line Kalshi has a strike at; among those, the one
    the most books post, then the most-traded Kalshi strike. Returns (line, matched)."""
    count = {}
    for lines in book_lines.values():
        for L in lines:
            count[L] = count.get(L, 0) + 1
    if not count:
        return None, False
    on_k = [L for L in count if L in ladder]
    if on_k:
        return max(on_k, key=lambda L: (count[L], ladder[L]["over"]["volume"])), True
    return max(count, key=lambda L: (count[L], -L)), False

def total_block(book_lines, ladder, cons=None):
    """Over/Under rows for one game. Only books posting the chosen line are compared; a book on a
    different line is shown with its own line and left out of "best". No Kalshi strike at the line
    means no Kalshi math at all - a 39.5 over is a different bet from a 38.5 over."""
    source = "sharpapi"
    if not book_lines and cons and cons.get("line") is not None and cons.get("over") is not None:
        book_lines, source = {"Consensus": {cons["line"]: {"over": cons["over"], "under": cons["under"]}}}, "consensus"
    line, matched = pick_total_line(book_lines, ladder)
    if line is None:
        return None
    at_line = {b: lines[line] for b, lines in book_lines.items() if line in lines}
    off_line = {b: next(iter(lines.items())) for b, lines in book_lines.items() if line not in lines}
    kl = ladder.get(line) if matched else None
    rows = {}
    for side in ("over", "under"):
        t = team_block(side.title(), side, kl[side] if kl else None, at_line, ("over", "under"))
        for b, (L, odds) in off_line.items():
            t[b], t[b + "_line"] = odds.get(side), L
        if not matched:
            t.pop("never_pay", None)  # a ceiling for a Kalshi market that doesn't exist
        rows[side] = t
    out = {"line": line, "matched": matched, "source": source, "over": rows["over"], "under": rows["under"]}
    if not matched and ladder:
        below = [s for s in ladder if s < line]
        above = [s for s in ladder if s > line]
        out["nearest"] = [x for x in (max(below) if below else None, min(above) if above else None) if x is not None]
    return out

def build(now=None):
    now = now or datetime.now(timezone.utc)
    notes = []
    season, week, games = schedule(now.astimezone(ET))
    try:
        kmap, kbodies = kalshi()
    except Exception as e:  # noqa: BLE001
        kmap, kbodies = {}, {}
        notes.append(f"Kalshi unreachable: {e}")
    try:
        ktot = kalshi_totals()
    except Exception as e:  # noqa: BLE001
        ktot = {}
        notes.append(f"Kalshi totals unreachable: {e}")
    key = os.environ.get("SHARPAPI_KEY", "").strip()
    smap, sharp_at, source = {}, None, "consensus"
    if key:
        try:
            smap, sharp_at = sharp(key)
            source = "sharpapi" if smap else "consensus"
            if not smap:
                notes.append("SharpAPI returned no NFL moneylines; using the nflverse consensus line.")
        except Exception as e:  # noqa: BLE001
            notes.append(f"SharpAPI unreachable ({e}); using the nflverse consensus line.")
    else:
        notes.append("No SHARPAPI_KEY set; using the nflverse consensus line.")
    stot, tot_source = {}, "consensus"
    if key:
        try:
            stot = sharp_totals(key)
            tot_source = "sharpapi" if stot else "consensus"
        except Exception as e:  # noqa: BLE001
            notes.append(f"SharpAPI totals unreachable ({e}); totals use the nflverse consensus line.")

    out_games = []
    for g in games:
        key3 = (g["gameday"], g["away"], g["home"])
        books = smap.get(key3, {})
        game_source = "sharpapi"
        if not books:  # this game isn't on SharpAPI: label it, don't hide it
            game_source = "consensus"
            if g["cons"]["away"] is not None:
                books = {"Consensus": g["cons"]}
        k = kmap.get(key3, {})
        out_games.append({
            "id": f'{g["away"]}-{g["home"]}', "kickoff": g["kickoff"].astimezone(timezone.utc).isoformat(),
            "when": g["kickoff"].strftime("%a %-I:%M %p"), "source": game_source,
            "kalshi": bool(k),
            "away": team_block(g["away"], "away", k.get(g["away"]), books),
            "home": team_block(g["home"], "home", k.get(g["home"]), books),
            "total": total_block(stot.get(key3, {}), ktot.get(kbodies.get(key3), {}), g["cons_total"]),
        })
    missing_k = [g["id"] for g in out_games if not g["kalshi"]]
    if kmap and missing_k:
        notes.append("No open Kalshi market for: " + ", ".join(missing_k))
    return {
        "updated": now.isoformat(timespec="seconds"), "season": season, "week": week,
        "books_source": source, "totals_source": tot_source, "sharp_updated": sharp_at, "notes": notes, "games": out_games,
    }

if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "lines.json")
    data = build()
    with open(out, "w") as f:
        json.dump(data, f, indent=1)
    print(f"week {data['week']}: {len(data['games'])} games, books={data['books_source']}, "
          f"kalshi={sum(g['kalshi'] for g in data['games'])}; notes: {data['notes'] or 'none'}")
