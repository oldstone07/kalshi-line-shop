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

# ---------------------------------------------------------------- gate features
# Shared by the live board and fill_study.py, so a gate means exactly the same thing in both.
# candles: [(end_time, bid, ask, volume)] hourly, oldest first.

def drift_12h(candles, t):
    """Bid change in cents over the 12 h before t (None if there's no candle ~12 h back)."""
    now = [c for c in candles if c[0] <= t]
    back = [c for c in candles if c[0] <= t - timedelta(hours=12)]
    if not now or not back or (t - timedelta(hours=12) - back[-1][0]) > timedelta(hours=2):
        return None
    if not (0 < now[-1][1] < 1 and 0 < back[-1][1] < 1):
        return None
    return round((now[-1][1] - back[-1][1]) * 100)

def vol_24h(candles, t):
    """Contracts traded in the 24 h before t."""
    return sum(c[3] for c in candles if t - timedelta(hours=24) < c[0] <= t)

def gate_buckets(drift, hours, vol24, vol_cuts):
    """Which bucket of each gate a moment falls in. vol_cuts = the study's activity tertiles."""
    return {
        "direction": None if drift is None else "fell" if drift < 0 else "flat" if drift == 0 else "rose",
        "timing": "far" if hours > 48 else "mid" if hours > 24 else "near",
        "activity": None if (vol24 is None or not vol_cuts) else
                    "quiet" if vol24 < vol_cuts[0] else "mid" if vol24 < vol_cuts[1] else "busy",
    }

GATE_LABELS = {
    "direction": {"fell": "Price falling", "flat": "Price steady", "rose": "Price rising"},
    "timing": {"far": "2+ days to kickoff", "mid": "1-2 days to kickoff", "near": "Under 24 h to kickoff"},
    "activity": {"quiet": "Quiet market", "mid": "Normal activity", "busy": "Busy market"},
}

def gate_verdict(bid, ask, best, hours, drift, vol24, grades):
    """The row's gates, realistic worth and verdict. grades = fills.json "gates" (weekly study).
    Worth is per $100 if a limit at the bid fills, judged by the price at kickoff:
    the study's average, plus each gate's effect where that effect held in both halves of the season."""
    if not grades or not (0 < bid < 1) or not (0 < ask < 1):
        return {}
    mid = (bid + ask) / 2
    book_now = round((mid * (100 + profit_book(best)) / 100 - 1) * 100, 2) if best is not None else None
    buckets = gate_buckets(drift, hours, vol24, grades.get("vol_cuts"))
    gates, worth = [], grades["base"]
    for name in ("direction", "timing", "activity"):
        b = buckets[name]
        g = grades.get(name, {})
        cell = (g.get("buckets") or {}).get(b) if b else None
        eff = cell["effect"] if cell else 0.0
        worth += eff
        detail = {"direction": None if drift is None else f"{drift:+d}¢ in 12 h",
                  "timing": f"{hours:.0f} h out", "activity": None if vol24 is None else f"{vol24/1000:.0f}k traded in 24 h"}[name]
        gates.append({"name": name, "bucket": b, "label": GATE_LABELS[name].get(b, "Unknown"), "detail": detail,
                      "effect": round(eff, 2), "holding": bool(cell and cell.get("effect")),
                      "status": g.get("status", "untested")})
    worth = round(worth, 2)
    # Best realistic limit = posting now, or waiting until under 24 h (the timing gate's best bucket).
    tb = (grades.get("timing") or {}).get("buckets") or {}
    t_now = next((g["effect"] for g in gates if g["name"] == "timing"), 0.0)
    worth_later = round(worth - t_now + (tb.get("near") or {}).get("effect", 0.0), 2) if hours > 24 else worth
    soft = book_now is not None and book_now > 0
    book_better = book_now is not None and book_now > max(worth, worth_later)
    gates.append({"name": "soft", "bucket": "soft" if soft else "no", "status": "unproven",
                  "label": "Book pays more than Kalshi's fair price" if soft else "Book isn't soft",
                  "detail": None if book_now is None else f"book worth {book_now:+.2f} per $100 at Kalshi's mid",
                  "effect": None, "holding": soft})
    if hours < CANCEL_H:
        v, why = "late", "Too late for a limit: cancel unfilled orders before inactives."
    elif book_better:
        v = "book"
        why = ("The book is paying more than Kalshi's fair price right now." if soft else
               "A realistic limit fill is worth less than simply taking the book's price.") + \
              " Rests on the unproven book gate."
    else:
        waits = [g for g in gates[:2] if g["effect"] < 0]
        if waits:
            v = "wait"
            why = {"direction": "Price is falling: fills now tend to come just before it drops further.",
                   "timing": "Far from kickoff: orders posted this early fill at worse moments."}[waits[0]["name"]]
        else:
            v, why = "post", "Gates are clear: post the limit at the bid."
    tcell = ((grades.get("timing") or {}).get("buckets") or {}).get(buckets["timing"]) or {}
    return {"gates": gates, "worth": worth, "worth_later": worth_later, "book_now": book_now, "verdict": v, "why": why,
            "fill_chance": tcell.get("fill_rate")}

CANCEL_H = 1.5

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
                               "bid_size": _f(m, "yes_bid_size_fp"), "ask_size": _f(m, "yes_ask_size_fp"),
                               "volume": _f(m, "volume_fp"), "ticker": m["ticker"]}
        out[(date, canon(away), canon(home))] = teams
        bodies[(date, canon(away), canon(home))] = body
    return out, bodies

def kalshi_candles(ticker, hours=26, series="KXNFLGAME", side="yes"):
    """Hourly (end, bid, ask, volume) for the last `hours`, for the direction and activity gates.
    side "no" (a totals Under) flips to the No book: No bid = 1 - Yes ask."""
    end = int(time.time())
    d = json.loads(get(f"https://api.elections.kalshi.com/trade-api/v2/series/{series}/markets/{ticker}/candlesticks"
                       f"?start_ts={end - hours * 3600}&end_ts={end}&period_interval=60"))
    out = []
    for c in d.get("candlesticks", []):
        b, a = c.get("yes_bid", {}).get("close_dollars"), c.get("yes_ask", {}).get("close_dollars")
        if b is not None and a is not None:
            b, a = (1 - float(a), 1 - float(b)) if side == "no" else (float(b), float(a))
            out.append((datetime.fromtimestamp(c["end_period_ts"], timezone.utc), round(b, 4), round(a, 4),
                        float(c.get("volume_fp") or 0)))
    return out

def load_grades():
    """Gate grades from the weekly study: {"ml": ..., "totals": ...}. Totals are graded on their own."""
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "fills.json")) as f:
            d = json.load(f)
        return {"ml": d.get("gates"), "totals": (d.get("totals") or {}).get("gates")}
    except (OSError, ValueError):
        return {"ml": None, "totals": None}

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
        t.update(bid_c=round(bid * 100), ask_c=round(ask * 100), volume=round(k["volume"]), ticker=k["ticker"],
                 bid_size=round(k.get("bid_size", 0)))
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

def gated(t, k, g, now, grades, series="KXNFLGAME", side="yes"):
    """Attach live gates to a row. A candles failure just leaves those gates unknown."""
    if not k or not grades:
        return t
    hours = (g["kickoff"] - now).total_seconds() / 3600
    drift = vol24 = None
    try:
        cs = kalshi_candles(k["ticker"], series=series, side=side)
        drift, vol24 = drift_12h(cs, now), round(vol_24h(cs, now))
    except Exception:  # noqa: BLE001
        pass
    t.update(gate_verdict(k["bid"], k["ask"], t.get("best"), hours, drift, vol24, grades))
    return t

def gated_total(tot, ladder, g, now, grades):
    """Gates for a totals card, on the strike the board compares at. Over = Yes, Under = No."""
    if not tot or not tot.get("matched"):
        return tot
    kl = ladder.get(tot["line"]) or {}
    for side, yn in (("over", "yes"), ("under", "no")):
        gated(tot[side], kl.get(side), g, now, grades, "KXNFLTOTAL", yn)
    return tot

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

    grades = load_grades()
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
            "away": gated(team_block(g["away"], "away", k.get(g["away"]), books), k.get(g["away"]), g, now, grades["ml"]),
            "home": gated(team_block(g["home"], "home", k.get(g["home"]), books), k.get(g["home"]), g, now, grades["ml"]),
            "total": gated_total(total_block(stot.get(key3, {}), ktot.get(kbodies.get(key3), {}), g["cons_total"]),
                                 ktot.get(kbodies.get(key3), {}), g, now, grades["totals"]),
        })
    missing_k = [g["id"] for g in out_games if not g["kalshi"]]
    if kmap and missing_k:
        notes.append("No open Kalshi market for: " + ", ".join(missing_k))
    return {
        "updated": now.isoformat(timespec="seconds"), "season": season, "week": week,
        "books_source": source, "totals_source": tot_source, "sharp_updated": sharp_at, "notes": notes, "games": out_games,
    }

def snapshot(data, folder):
    """Append this run's moneyline board to snapshots/<season>-w<week>.jsonl, so fill_study.py can later
    replay exactly what the board advised, with the real queue at the bid and the real FD/DK price."""
    if not data.get("week"):
        return
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f'{data["season"]}-w{data["week"]:02d}.jsonl')
    with open(path, "a") as f:
        for g in data["games"]:
            for side in ("away", "home"):
                t = g[side]
                if not t.get("ticker"):
                    continue
                f.write(json.dumps({"t": data["updated"], "ticker": t["ticker"], "kickoff": g["kickoff"],
                                    "bid": t["bid_c"] / 100, "ask": t["ask_c"] / 100, "bid_size": t.get("bid_size"),
                                    "best": t.get("best"), "best_book": t.get("best_book"),
                                    "vs_book": t.get("vs_book"), "verdict": t.get("verdict")}) + "\n")
            tot = g.get("total") or {}
            for side in ("over", "under"):
                t = tot.get(side) or {}
                if not tot.get("matched") or not t.get("ticker"):
                    continue
                f.write(json.dumps({"t": data["updated"], "ticker": t["ticker"], "side": side, "line": tot["line"],
                                    "kickoff": g["kickoff"], "bid": t["bid_c"] / 100, "ask": t["ask_c"] / 100,
                                    "best": t.get("best"), "best_book": t.get("best_book"),
                                    "vs_book": t.get("vs_book"), "verdict": t.get("verdict")}) + "\n")

if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(here, "lines.json")
    data = build()
    with open(out, "w") as f:
        json.dump(data, f, indent=1)
    if len(sys.argv) <= 1:
        snapshot(data, os.path.join(here, "snapshots"))
    print(f"week {data['week']}: {len(data['games'])} games, books={data['books_source']}, "
          f"kalshi={sum(g['kalshi'] for g in data['games'])}; notes: {data['notes'] or 'none'}")
