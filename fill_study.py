"""Fill check: does "Set limit at" really earn what the board says?

The board assumes a limit order at the bid fills and that the fill is a good price. Resting orders
get filled by whoever chooses to sell to you, which is often someone who knows more (adverse
selection). This replays the board's advice against Kalshi's real trade history.

For every settled regular-season game:
  * Simulated order: buy Yes at the current bid, placed every 3 h from 96 h before kickoff,
    cancelled 90 min before kickoff (the board's own advice).
  * Fill, as a range, because the queue ahead of you at the bid isn't in the history:
      conservative = a seller traded BELOW your price after you posted (you were certainly filled)
      optimistic   = a seller traded AT or below your price (you'd be filled if first in line)
  * Judge: Kalshi's mid at kickoff = the market's last word on the true probability.
    CLV per $100 = (p_close / price paid - 1) * 100. Book = nflverse consensus moneyline.

Live board snapshots (snapshots/*.jsonl, written by fetch_lines.py) add the real queue size and the
real FD/DK price at the moment you'd have posted; they're reported separately as they accumulate.

Settled markets never change, so each market's replay is cached in study_cache/.
Run: python3 fill_study.py   -> fills.json
"""
import csv, glob, io, json, os, random, time, urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from fetch_lines import get, canon, profit_book, profit_price, tap_cost, NFLVERSE

ET = ZoneInfo("America/New_York")
B = "https://api.elections.kalshi.com/trade-api/v2"
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "study_cache")
LOOKBACK_H, STEP_H, CANCEL_MIN = 96, 3, 90
CACHE_VERSION = 2
# Queue ahead of a new order at the best bid. Historical queue sizes aren't published, so until the
# board's own snapshots have enough of them, use the median best-bid size on the Week 6 board
# (all 28 sides, 0-4 days before kickoff, measured 15 Oct 2026: p25 46k, median 69k, p75 312k).
QUEUE_FALLBACK = 69_000

ts = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))
def jget(path):
    """Kalshi GET with patient backoff: the public tier answers bursts with 429, and networks blip."""
    for i in range(8):
        try:
            return json.loads(get(B + path, tries=1))
        except RuntimeError:
            if i == 7:
                raise
            time.sleep(3 * (i + 1) + random.random())

# ---------------------------------------------------------------- pure math (tested)

def clv(p_close, cost):
    """Expected profit per $100 at an all-in cost, if p_close is the true probability."""
    return (p_close / cost - 1) * 100

def book_clv(p_close, american):
    return (p_close * (100 + profit_book(american)) / 100 - 1) * 100

def fills(bid, trades, t0, deadline):
    """First fill times (conservative, optimistic) for a resting Yes buy at `bid` posted at t0.
    trades: [(time, yes_price, taker_side)]. A taker_side "no" trade is someone selling Yes into bids."""
    cons = opt = None
    for t, yp, side in trades:
        if t <= t0 or t > deadline or side != "no":
            continue
        if opt is None and yp <= bid + 1e-9:
            opt = t
        if yp < bid - 1e-9:
            cons = t
            break
    return cons, opt

def vol_at_price(bid, trades, t0, deadline):
    """Contracts sold into bids at exactly our price after t0, up to the first trade-through
    (after which we're filled regardless of queue). With a queue of Q ahead of us, the order
    fills iff it was traded through, or this exceeds Q."""
    done = 0.0
    for t, yp, side, n in trades:
        if t <= t0 or t > deadline or side != "no":
            continue
        if yp < bid - 1e-9:
            break
        if abs(yp - bid) < 1e-9:
            done += n
    return done

def queue_fill(bid, ahead, trades, t0, deadline):
    """Fill time with a queue: filled once more than `ahead` contracts have traded at our price,
    or anything trades through it."""
    done = 0.0
    for t, yp, side, n in trades:
        if t <= t0 or t > deadline or side != "no":
            continue
        if yp < bid - 1e-9:
            return t
        if abs(yp - bid) < 1e-9:
            done += n
            if done > ahead:
                return t
    return None

# ---------------------------------------------------------------- data

def nfl_schedule():
    rows = list(csv.DictReader(io.StringIO(get(NFLVERSE).decode())))
    season = max(int(r["season"]) for r in rows)
    out = {}
    for r in rows:
        if int(r["season"]) != season or not r["gametime"]:
            continue
        kick = datetime.strptime(f'{r["gameday"]} {r["gametime"]}', "%Y-%m-%d %H:%M").replace(tzinfo=ET)
        ml = lambda v: int(float(v)) if v not in ("", "NA") else None
        out[(r["gameday"], canon(r["away_team"]), canon(r["home_team"]))] = {
            "week": int(r["week"]), "kickoff": kick.astimezone(timezone.utc),
            "ml": {canon(r["away_team"]): ml(r["away_moneyline"]), canon(r["home_team"]): ml(r["home_moneyline"])},
        }
    return out

def settled_markets():
    ms, cursor = [], None
    for _ in range(20):
        d = jget(f"/markets?series_ticker=KXNFLGAME&status=settled&limit=1000" + (f"&cursor={cursor}" if cursor else ""))
        ms += d.get("markets", [])
        cursor = d.get("cursor")
        if not cursor:
            break
    by_event = {}
    for m in ms:
        by_event.setdefault(m["event_ticker"], []).append(m)
    return by_event

def event_key(ev, codes):
    body = ev.split("-", 1)[1]
    date = datetime.strptime(body[:7].title(), "%y%b%d").date().isoformat()
    pair = body[7:]
    away = next((c for c in codes if pair.startswith(c) and pair[len(c):] in codes), None)
    return (date, canon(away), canon(pair[len(away):])) if away else None

def trades_between(ticker, t_from, t_to):
    out, cursor = [], None
    for _ in range(200):
        q = {"ticker": ticker, "limit": 1000, "min_ts": int(t_from.timestamp()), "max_ts": int(t_to.timestamp())}
        if cursor:
            q["cursor"] = cursor
        d = jget("/markets/trades?" + urllib.parse.urlencode(q))
        for t in d.get("trades", []):
            out.append((ts(t["created_time"]), float(t["yes_price_dollars"]), t["taker_side"], float(t.get("count_fp") or 0)))
        cursor = d.get("cursor")
        if not cursor:
            break
    out.sort()
    return out

def candles(ticker, t_from, t_to):
    d = jget(f"/series/KXNFLGAME/markets/{ticker}/candlesticks?start_ts={int(t_from.timestamp())}"
             f"&end_ts={int(t_to.timestamp())}&period_interval=60")
    out = []
    for c in d.get("candlesticks", []):
        b = c.get("yes_bid", {}).get("close_dollars")
        a = c.get("yes_ask", {}).get("close_dollars")
        if b is None or a is None:
            continue
        out.append((datetime.fromtimestamp(c["end_period_ts"], timezone.utc), float(b), float(a)))
    return out

# ---------------------------------------------------------------- replay one market

def replay(ticker, team, game, result):
    """Simulated orders for one team market. Cached forever once the game has settled."""
    path = os.path.join(CACHE, ticker + ".json")
    if os.path.exists(path):
        c = json.load(open(path))
        if c.get("v") == CACHE_VERSION:
            return c
    kick = game["kickoff"]
    deadline = kick - timedelta(minutes=CANCEL_MIN)
    start = kick - timedelta(hours=LOOKBACK_H)
    cs = candles(ticker, start, kick)
    close = next(((b + a) / 2 for t, b, a in reversed(cs) if t <= kick and 0 < b < a < 1), None)
    tr = trades_between(ticker, start, deadline)
    tr3 = [(t, yp, side) for t, yp, side, _ in tr]
    orders = []
    for t, b, a in cs:
        if t > deadline - timedelta(minutes=30) or not (0 < b < 1) or not (0 < a < 1):
            continue
        h = int((kick - t).total_seconds() // 3600)
        if h % STEP_H:   # one order every 3 h (kickoffs at :15 or :25 aren't on the hour)
            continue
        fc, fo = fills(b, tr3, t, deadline)
        orders.append({"t": t.isoformat(), "h": h, "bid": b, "ask": a, "vol_at": round(vol_at_price(b, tr, t, deadline), 2),
                       "fill_c": fc.isoformat() if fc else None, "fill_o": fo.isoformat() if fo else None})
    out = {"v": CACHE_VERSION, "ticker": ticker, "team": team, "week": game["week"], "kickoff": kick.isoformat(),
           "won": result == "yes", "p_close": close, "book": game["ml"].get(team), "orders": orders,
           "n_trades": len(tr)}
    os.makedirs(CACHE, exist_ok=True)
    json.dump(out, open(path, "w"))
    return out

# ---------------------------------------------------------------- summarise

def mean(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else None

def boot_ci(groups, stat, n=1000, seed=7):
    """95% interval by resampling whole games (orders inside a game are not independent)."""
    rnd = random.Random(seed)
    vals = []
    for _ in range(n):
        pick = [g for _ in groups for g in [groups[rnd.randrange(len(groups))]]]
        v = stat([o for g in pick for o in g])
        if v is not None:
            vals.append(v)
    vals.sort()
    if not vals:
        return None
    return [round(vals[int(0.025 * len(vals))], 2), round(vals[int(0.975 * len(vals)) - 1], 2)]

def summarise(orders, groups):
    """orders: flat list with fill_c / fill_q / fill_o flags (conservative / typical queue / optimistic)."""
    return _summarise(orders, groups)

def _summarise(orders, groups):
    """orders: flat list of dicts with clv_limit, clv_tap, clv_book, fill_c, fill_o, won, bid."""
    def block(key):
        filled = [o for o in orders if o[key]]
        st_fill = lambda os_: mean(1.0 if o[key] else 0.0 for o in os_)
        st_ev = lambda os_: mean(o["clv_limit"] for o in os_ if o[key])
        st_att = lambda os_: mean(o["clv_limit"] if o[key] else 0.0 for o in os_)
        return {
            "fill_rate": round(st_fill(orders) * 100, 1) if orders else None,
            "ev_if_filled": round(st_ev(orders), 2) if filled else None,
            "ev_if_filled_ci": boot_ci(groups, st_ev) if filled else None,
            "ev_per_order": round(st_att(orders), 2) if orders else None,
            "actual_per_100": round(mean((profit_price(o["bid"]) if o["won"] else -100) for o in filled), 2) if filled else None,
            "n_filled": len(filled),
        }
    with_book = [o for o in orders if o.get("clv_book") is not None]
    return {
        "n_orders": len(orders), "n_games": len({o["game"] for o in orders}),
        "conservative": block("fill_c"), "queue": block("fill_q"), "optimistic": block("fill_o"),
        "ev_if_all_filled": round(mean(o["clv_limit"] for o in orders), 2) if orders else None,
        "ev_tap": round(mean(o["clv_tap"] for o in orders), 2) if orders else None,
        "ev_book": round(mean(o["clv_book"] for o in with_book), 2) if with_book else None,
    }

def flatten(markets, queue):
    flat = []
    for m in markets:
        if m["p_close"] is None:
            continue
        p = m["p_close"]
        for o in m["orders"]:
            flat.append({
                "game": m["ticker"].rsplit("-", 1)[0], "week": m["week"], "h": o["h"], "bid": o["bid"],
                "fav": o["bid"] >= 0.5, "won": m["won"],
                "fill_c": bool(o["fill_c"]), "fill_o": bool(o["fill_o"]),
                "fill_q": bool(o["fill_c"]) or o.get("vol_at", 0) > queue,
                "clv_limit": clv(p, o["bid"]), "clv_tap": clv(p, tap_cost(o["ask"])),
                "clv_book": book_clv(p, m["book"]) if m["book"] is not None else None,
            })
    return flat

def grouped(flat):
    g = {}
    for o in flat:
        g.setdefault(o["game"], []).append(o)
    return list(g.values())

def board_snapshots(sched, settled_by_ticker):
    """Replays of the live board's own snapshots: real queue size and real FD/DK best price."""
    rows = []
    for f in sorted(glob.glob(os.path.join(HERE, "snapshots", "*.jsonl"))):
        for line in open(f):
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    by_ticker = {}
    for r in rows:
        if r.get("ticker") in settled_by_ticker and r.get("bid", 0) > 0:
            by_ticker.setdefault(r["ticker"], []).append(r)
    out = []
    for tk, snaps in by_ticker.items():
        m = settled_by_ticker[tk]
        if m["p_close"] is None:
            continue
        kick = ts(m["kickoff"]); deadline = kick - timedelta(minutes=CANCEL_MIN)
        tr = trades_between(tk, ts(min(s["t"] for s in snaps)), deadline)
        for s in snaps:
            t0 = ts(s["t"])
            if t0 > deadline - timedelta(minutes=30):
                continue
            f = queue_fill(s["bid"], s.get("bid_size", 0), tr, t0, deadline)
            p = m["p_close"]
            out.append({"game": tk.rsplit("-", 1)[0], "fill": bool(f), "clv_limit": clv(p, s["bid"]),
                        "clv_book": book_clv(p, s["best"]) if s.get("best") is not None else None,
                        "claimed": s.get("vs_book")})
    if not out:
        return None
    filled = [o for o in out if o["fill"]]
    wb = [o for o in out if o["clv_book"] is not None]
    return {"n_orders": len(out), "n_games": len({o["game"] for o in out}),
            "fill_rate": round(100 * len(filled) / len(out), 1),
            "ev_if_filled": round(mean(o["clv_limit"] for o in filled), 2) if filled else None,
            "ev_book": round(mean(o["clv_book"] for o in wb), 2) if wb else None,
            "claimed_vs_book": round(mean(o["claimed"] for o in out if o["claimed"] is not None) or 0, 2)}

def main():
    sched = nfl_schedule()
    events = settled_markets()
    jobs = []
    for ev, ms in sorted(events.items()):
        codes = [m["ticker"].rsplit("-", 1)[1] for m in ms]
        k = event_key(ev, codes)
        game = sched.get(k) if k else None
        if not game or len(ms) != 2:   # preseason isn't in nflverse, so it drops out here
            continue
        jobs += [(m["ticker"], canon(m["ticker"].rsplit("-", 1)[1]), game, m.get("result")) for m in ms]

    def run(job):
        try:
            return replay(*job)
        except Exception as e:  # noqa: BLE001 - one bad market shouldn't sink the study
            print("skip", job[0], e, flush=True)
    # 4 at a time keeps well under Kalshi's public read limit; only uncached markets hit the API.
    with ThreadPoolExecutor(4) as pool:
        markets = [m for m in pool.map(run, jobs) if m]
    snaps_q = []
    for f in glob.glob(os.path.join(HERE, "snapshots", "*.jsonl")):
        for line in open(f):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("bid_size") and (ts(r["kickoff"]) - ts(r["t"])).total_seconds() < LOOKBACK_H * 3600:
                snaps_q.append(r["bid_size"])
    snaps_q.sort()
    queue = snaps_q[len(snaps_q) // 2] if len(snaps_q) >= 200 else QUEUE_FALLBACK
    flat = flatten(markets, queue)
    groups = grouped(flat)
    hours = [("Over 48 h before", lambda o: o["h"] > 48), ("12–48 h before", lambda o: 12 < o["h"] <= 48),
             ("Last 12 h", lambda o: o["h"] <= 12)]
    out = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "method": {"lookback_h": LOOKBACK_H, "step_h": STEP_H, "cancel_min": CANCEL_MIN, "queue": round(queue),
                   "queue_source": "board snapshots" if len(snaps_q) >= 200 else "Week 6 board, 15 Oct",
                   "judge": "Kalshi mid at kickoff", "book": "nflverse consensus moneyline"},
        "weeks": sorted({m["week"] for m in markets}),
        "all": summarise(flat, groups),
        "by_when": [{"label": l, **summarise([o for o in flat if f(o)], grouped([o for o in flat if f(o)]))} for l, f in hours],
        "by_side": [{"label": l, **summarise([o for o in flat if f(o)], grouped([o for o in flat if f(o)]))}
                    for l, f in (("Favourites (50¢+)", lambda o: o["fav"]), ("Underdogs", lambda o: not o["fav"]))],
        "by_week": [{"label": f"Week {w}", **summarise([o for o in flat if o["week"] == w], grouped([o for o in flat if o["week"] == w]))}
                    for w in sorted({o["week"] for o in flat})],
        "board": board_snapshots(sched, {m["ticker"]: m for m in markets}),
    }
    with open(os.path.join(HERE, "fills.json"), "w") as f:
        json.dump(out, f, indent=1)
    a = out["all"]
    print(f"{a['n_games']} games, {a['n_orders']} orders. fill {a['conservative']['fill_rate']} / {a['queue']['fill_rate']} / {a['optimistic']['fill_rate']}%, "
          f"EV if filled {a['conservative']['ev_if_filled']} / {a['queue']['ev_if_filled']} / {a['optimistic']['ev_if_filled']}, all-filled {a['ev_if_all_filled']}, "
          f"tap {a['ev_tap']}, book {a['ev_book']}")

if __name__ == "__main__":
    main()
