"""Kalshi Line Shop: pull this week's NFL moneylines from Kalshi and FanDuel/DraftKings,
do all the math, write lines.json for index.html.

Sources
  Kalshi     public markets API, no key (KXNFLGAME series, one market per team)
  FD + DK    SharpAPI free tier, key in env SHARPAPI_KEY; one call for the whole league
  Schedule   nflverse games.csv (week, kickoff, codes); also the consensus-line fallback

Stdlib only, so the Actions job needs no pip install.
Run:  SHARPAPI_KEY=sk_live_... python3 fetch_lines.py [out.json]
"""
import csv, io, json, math, os, sys, time, urllib.request
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
KALSHI = "https://api.elections.kalshi.com/trade-api/v2/markets?series_ticker=KXNFLGAME&status=open&limit=1000"
SHARP = "https://api.sharpapi.io/api/v1/odds?league=nfl&market=moneyline&limit=200"
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
        games.append({
            "week": int(r["week"]), "game_type": r["game_type"], "gameday": r["gameday"],
            "kickoff": kick, "away": canon(r["away_team"]), "home": canon(r["home_team"]),
            "cons": {"away": ml(r["away_moneyline"]), "home": ml(r["home_moneyline"])},
        })
    if not games:
        return season, None, []
    week = min(g["week"] for g in games)
    return season, week, sorted((g for g in games if g["week"] == week), key=lambda g: (g["kickoff"], g["away"]))

def kalshi():
    """{(gameday, away, home): {team: {bid, ask, volume, ticker}}}"""
    markets, cursor = [], None
    for _ in range(10):
        d = json.loads(get(KALSHI + (f"&cursor={cursor}" if cursor else "")))
        markets += d.get("markets", [])
        cursor = d.get("cursor")
        if not cursor:
            break
    by_event = {}
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
            f = lambda k: float(m[k]) if m.get(k) not in (None, "") else 0.0
            teams[canon(c)] = {"bid": f("yes_bid_dollars"), "ask": f("yes_ask_dollars"),
                               "volume": f("volume_fp"), "ticker": m["ticker"]}
        out[(date, canon(away), canon(home))] = teams
    return out

def sharp(key):
    """{(gameday, away, home): {"FD": {"away": a, "home": a}, "DK": {...}}}"""
    d = json.loads(get(SHARP, {"X-API-Key": key}))
    rows = d.get("data", [])
    if (d.get("pagination") or {}).get("has_more"):
        d2 = json.loads(get(SHARP + "&offset=200", {"X-API-Key": key}))
        rows += d2.get("data", [])
    out, seen = {}, set()
    for r in rows:
        if r.get("id") in seen or r.get("is_live") or r.get("is_active") is False \
                or r.get("is_main_line") is False or r.get("is_stale_pregame_price"):
            continue
        seen.add(r.get("id"))
        book = {"fanduel": "FD", "draftkings": "DK"}.get(r.get("sportsbook"))
        side = r.get("team_side") or r.get("selection_type")
        if not book or side not in ("away", "home"):
            continue
        # FanDuel stamps kickoff a minute after DraftKings, so match on the ET date, not the time.
        start = datetime.strptime(r["event_start_time"].replace("Z", "+00:00")[:16] + "+00:00",
                                  "%Y-%m-%dT%H:%M%z").astimezone(ET).date().isoformat()
        k = (start, canon(r["away"]["abbreviation"]), canon(r["home"]["abbreviation"]))
        out.setdefault(k, {}).setdefault(book, {})[side] = int(r["odds_american"])
    return out, d.get("updated_at")

# ---------------------------------------------------------------- assembly

def team_block(code, side, k, books, other_books):
    t = {"code": code, "name": NAMES.get(code, code)}
    for b in ("FD", "DK", "Consensus"):
        if b in books:
            t[b] = books[b].get(side)
    prices = {b: books[b].get(side) for b in books if books[b].get(side) is not None}
    best_book = max(prices, key=lambda b: profit_book(prices[b])) if prices else None
    best = prices.get(best_book)
    t["best_book"], t["best"] = best_book, best
    # Fair odds: average of each book's devigged probability where that book has both sides.
    fairs = [fair_probs(v["away"], v["home"])[0 if side == "away" else 1]
             for v in books.values() if v.get("away") is not None and v.get("home") is not None]
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

def build(now=None):
    now = now or datetime.now(timezone.utc)
    notes = []
    season, week, games = schedule(now.astimezone(ET))
    try:
        kmap = kalshi()
    except Exception as e:  # noqa: BLE001
        kmap = {}
        notes.append(f"Kalshi unreachable: {e}")
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
            "away": team_block(g["away"], "away", k.get(g["away"]), books, None),
            "home": team_block(g["home"], "home", k.get(g["home"]), books, None),
        })
    missing_k = [g["id"] for g in out_games if not g["kalshi"]]
    if kmap and missing_k:
        notes.append("No open Kalshi market for: " + ", ".join(missing_k))
    return {
        "updated": now.isoformat(timespec="seconds"), "season": season, "week": week,
        "books_source": source, "sharp_updated": sharp_at, "notes": notes, "games": out_games,
    }

if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "lines.json")
    data = build()
    with open(out, "w") as f:
        json.dump(data, f, indent=1)
    print(f"week {data['week']}: {len(data['games'])} games, books={data['books_source']}, "
          f"kalshi={sum(g['kalshi'] for g in data['games'])}; notes: {data['notes'] or 'none'}")
