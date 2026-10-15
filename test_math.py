"""Acceptance tests from the build brief. Run: python3 test_math.py  (or pytest)"""
import unittest
from fetch_lines import (prob_to_am, tap_cost, vs_book, never_pay_cents, fair_probs,
                         am_to_prob, profit_book, profit_price, team_block, canon, total_block)


class BriefTable(unittest.TestCase):
    def test_seattle_limit(self):
        self.assertEqual(prob_to_am(0.56), -127)
        self.assertEqual(round(vs_book(0.56, -142), 2), 8.15)
        self.assertEqual(never_pay_cents(-142), 58)

    def test_seattle_tap(self):
        self.assertEqual(prob_to_am(tap_cost(0.57)), -142)

    def test_denver_limit(self):
        self.assertEqual(prob_to_am(0.43), 133)
        self.assertEqual(round(vs_book(0.43, 120), 2), 12.56)
        self.assertEqual(never_pay_cents(120), 45)

    def test_denver_tap(self):
        self.assertEqual(prob_to_am(tap_cost(0.44)), 119)

    def test_fair(self):
        sea, den = fair_probs(-142, 120)
        self.assertEqual(round(sea * 100, 1), 56.3)
        self.assertEqual(prob_to_am(sea), -129)
        self.assertAlmostEqual(sea + den, 1.0)


class Edges(unittest.TestCase):
    def test_formulas(self):
        self.assertAlmostEqual(am_to_prob(-150), 0.6)
        self.assertAlmostEqual(am_to_prob(150), 0.4)
        self.assertEqual(profit_book(150), 150)
        self.assertAlmostEqual(profit_book(-200), 50)
        self.assertAlmostEqual(profit_price(0.5), 100)
        self.assertEqual(prob_to_am(0.5), -100)

    def test_never_pay_float_trap(self):
        # -122.2... style lines whose implied prob lands exactly on a whole cent
        self.assertEqual(never_pay_cents(-100), 49)   # 50.0% -> 49c, not 50c
        self.assertEqual(never_pay_cents(-150), 59)   # 60.0%
        self.assertEqual(never_pay_cents(150), 39)    # 40.0%

    def test_codes(self):
        self.assertEqual(canon("JAC"), "JAX")
        self.assertEqual(canon("LAR"), "LA")
        self.assertEqual(canon("LVR"), "LV")

    def test_best_book_and_block(self):
        k = {"bid": 0.56, "ask": 0.57, "volume": 10, "ticker": "X"}
        t = team_block("SEA", "away", k, {"FD": {"away": -145, "home": 122}, "DK": {"away": -142, "home": 120}})
        self.assertEqual(t["best_book"], "DK")      # -142 pays more than -145
        self.assertEqual(t["limit_am"], -127)
        self.assertEqual(t["tap_am"], -142)
        self.assertEqual(t["vs_book"], 8.15)
        self.assertEqual(t["never_pay"], 58)
        self.assertTrue(t["beats"])

    def test_one_book_missing(self):
        t = team_block("TEN", "away", None, {"FD": {"away": 270, "home": -335}})
        self.assertEqual(t["best_book"], "FD")
        self.assertNotIn("DK", t)
        self.assertNotIn("vs_book", t)


def ladder(*strikes):
    """Kalshi totals ladder: Over = Yes at (bid, ask); Under = No, priced off the other side of the book."""
    out = {}
    for L, yb, ya in strikes:
        out[L] = {"over": {"bid": yb, "ask": ya, "volume": 100, "ticker": f"T-{L}"},
                  "under": {"bid": round(1 - ya, 2), "ask": round(1 - yb, 2), "volume": 100, "ticker": f"T-{L}"}}
    return out


class Totals(unittest.TestCase):
    def test_matched_line(self):
        books = {"FD": {40.5: {"over": -115, "under": -105}}, "DK": {40.5: {"over": -110, "under": -110}}}
        t = total_block(books, ladder((39.5, 0.54, 0.55), (40.5, 0.50, 0.51)))
        self.assertEqual(t["line"], 40.5)
        self.assertTrue(t["matched"])
        o, u = t["over"], t["under"]
        self.assertEqual((o["best_book"], o["best"]), ("DK", -110))
        self.assertEqual((u["best_book"], u["best"]), ("FD", -105))
        self.assertEqual(o["limit_am"], -100)          # 50c
        self.assertEqual(u["limit_am"], 104)           # No bid 49c
        self.assertEqual(o["vs_book"], round(100 - 10000 / 110, 2))
        self.assertEqual(u["never_pay"], 51)           # -105 -> 51.2% -> 51c

    def test_no_strike_means_no_math(self):
        books = {"DK": {38.5: {"over": -112, "under": -108}}}
        t = total_block(books, ladder((37.5, 0.55, 0.56), (39.5, 0.47, 0.48)))
        self.assertFalse(t["matched"])
        self.assertEqual(t["nearest"], [37.5, 39.5])
        for side in ("over", "under"):
            self.assertNotIn("vs_book", t[side])
            self.assertNotIn("limit_am", t[side])

    def test_books_on_different_lines(self):
        # Kalshi has 41.5 only; FD's 40.5 is shown but not compared.
        books = {"FD": {40.5: {"over": -105, "under": -115}}, "DK": {41.5: {"over": -110, "under": -110}}}
        t = total_block(books, ladder((41.5, 0.48, 0.49)))
        self.assertEqual(t["line"], 41.5)
        self.assertEqual(t["over"]["best_book"], "DK")
        self.assertEqual((t["over"]["FD"], t["over"]["FD_line"]), (-105, 40.5))

    def test_consensus_fallback(self):
        t = total_block({}, ladder((44.5, 0.50, 0.51)), {"line": 44.5, "over": -110, "under": -110})
        self.assertEqual(t["source"], "consensus")
        self.assertEqual(t["over"]["best_book"], "Consensus")


class FillCheck(unittest.TestCase):
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    T0 = _dt(2026, 10, 11, 12, tzinfo=_tz.utc)

    def at(self, minutes):
        return self.T0 + self._td(minutes=minutes)

    def test_clv(self):
        from fill_study import clv, book_clv
        self.assertAlmostEqual(clv(0.60, 0.56), (0.60 / 0.56 - 1) * 100)
        self.assertAlmostEqual(clv(0.56, 0.56), 0.0)
        # +100 at a true 50% is break-even; -110 at 50% loses the vig
        self.assertAlmostEqual(book_clv(0.5, 100), 0.0)
        self.assertAlmostEqual(book_clv(0.5, -110), (0.5 * (100 + 10000 / 110) / 100 - 1) * 100)

    def test_fill_bounds(self):
        from fill_study import fills
        dl = self.at(600)
        tr = [(self.at(-5), 0.50, "no"),     # before we posted: ignored
              (self.at(10), 0.57, "yes"),    # buyer lifting the ask: never fills a bid
              (self.at(20), 0.56, "no"),     # seller at our price: optimistic fill
              (self.at(30), 0.55, "no")]     # seller below our price: certain fill
        c, o = fills(0.56, tr, self.T0, dl)
        self.assertEqual(o, self.at(20))
        self.assertEqual(c, self.at(30))

    def test_no_fill_after_deadline(self):
        from fill_study import fills
        c, o = fills(0.56, [(self.at(100), 0.50, "no")], self.T0, self.at(90))
        self.assertIsNone(c)
        self.assertIsNone(o)

    def test_queue(self):
        from fill_study import vol_at_price, queue_fill
        dl = self.at(600)
        tr = [(self.at(10), 0.56, "no", 400.0), (self.at(20), 0.56, "no", 700.0),
              (self.at(25), 0.57, "no", 5000.0),   # above our price: sellers hit higher bids first
              (self.at(30), 0.55, "no", 1.0)]
        self.assertEqual(vol_at_price(0.56, tr, self.T0, dl), 1100.0)
        self.assertEqual(queue_fill(0.56, 1000, tr, self.T0, dl), self.at(20))   # 1100 > 1000 ahead
        self.assertEqual(queue_fill(0.56, 5000, tr, self.T0, dl), self.at(30))   # only the trade-through fills
        self.assertIsNone(queue_fill(0.56, 5000, tr[:3], self.T0, dl))


class Gates(unittest.TestCase):
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    NOW = _dt(2026, 10, 15, 20, tzinfo=_tz.utc)
    GRADES = {"base": -1.0, "vol_cuts": [20000, 60000],
              "direction": {"status": "holding", "buckets": {"fell": {"effect": -0.2}, "flat": {"effect": 0.0}, "rose": {"effect": 0.3}}},
              "timing": {"status": "holding", "buckets": {"far": {"effect": -0.25, "fill_rate": 46}, "mid": {"effect": 0.25},
                                                            "near": {"effect": 0.5, "fill_rate": 24}}},
              "activity": {"status": "holding", "buckets": {"quiet": {"effect": -0.6}, "mid": {"effect": 0.0}, "busy": {"effect": 0.6}}}}

    def candles(self, bids, vol=1000):
        """Hourly candles ending at NOW, oldest first."""
        n = len(bids)
        return [(self.NOW - self._td(hours=n - 1 - i), b, b + 0.01, vol) for i, b in enumerate(bids)]

    def test_drift_and_volume(self):
        from fetch_lines import drift_12h, vol_24h
        cs = self.candles([0.50] * 13 + [0.52] * 12)          # 25 hourly candles
        self.assertEqual(drift_12h(cs, self.NOW), 2)
        self.assertEqual(vol_24h(cs, self.NOW), 24 * 1000)    # the candle exactly 24 h back is excluded
        self.assertIsNone(drift_12h(self.candles([0.5] * 5), self.NOW))   # no history 12 h back

    def test_buckets(self):
        from fetch_lines import gate_buckets
        b = gate_buckets(-1, 60, 10000, [20000, 60000])
        self.assertEqual(b, {"direction": "fell", "timing": "far", "activity": "quiet"})
        self.assertEqual(gate_buckets(0, 30, 60000, [20000, 60000])["activity"], "busy")
        self.assertEqual(gate_buckets(3, 10, 30000, [20000, 60000])["timing"], "near")

    def test_verdicts(self):
        from fetch_lines import gate_verdict
        # near kickoff, rising, busy: post, worth = base + effects
        v = gate_verdict(0.56, 0.57, -142, 10, 1, 90000, self.GRADES)
        self.assertEqual(v["verdict"], "post")
        self.assertAlmostEqual(v["worth"], -1.0 + 0.3 + 0.5 + 0.6)
        self.assertEqual(v["fill_chance"], 24)
        # falling price: wait, even near kickoff
        self.assertEqual(gate_verdict(0.56, 0.57, -142, 10, -2, 90000, self.GRADES)["verdict"], "wait")
        # far from kickoff: wait
        self.assertEqual(gate_verdict(0.56, 0.57, -142, 60, 0, 90000, self.GRADES)["verdict"], "wait")
        # book pays more than Kalshi's mid: bet the book (+370 at a 21.5c mid is worth +1.05)
        v = gate_verdict(0.21, 0.22, 370, 60, 0, 30000, self.GRADES)
        self.assertEqual(v["verdict"], "book")
        self.assertEqual(v["book_now"], 1.05)
        # book negative, but still better than any realistic limit (quiet market): bet the book
        v = gate_verdict(0.56, 0.57, -130, 10, 0, 5000, self.GRADES)   # worth -1.1, book at mid -0.5
        self.assertEqual(v["verdict"], "book")
        # far out, but waiting for kickoff beats the book: wait, not book
        v = gate_verdict(0.56, 0.57, -142, 60, 0, 90000, self.GRADES)
        self.assertEqual((v["verdict"], v["worth_later"]), ("wait", round(-1.0 + 0.5 + 0.6, 2)))
        # inside the cancel window: too late for a limit
        self.assertEqual(gate_verdict(0.56, 0.57, -142, 1.0, 0, 90000, self.GRADES)["verdict"], "late")
        # no grades yet: no verdict at all, rather than a made-up one
        self.assertEqual(gate_verdict(0.56, 0.57, -142, 10, 0, 90000, None), {})

    def test_grading_rules(self):
        from fill_study import grade_gates
        flat = []
        # direction "rose" helps in both halves (+3 then +1) -> effect = the smaller, +1
        # timing "far" helps early, hurts late -> disagreement -> 0
        for wk, h, clv_rose, clv_far in ((1, 10, 3, 2), (2, 10, 3, 2), (3, 10, 1, -2), (4, 10, 1, -2)):
            for i in range(20):
                flat.append({"game": f"g{wk}-{i}", "week": wk, "h": 10, "drift": 1, "vol24": 1, "fill_q": True, "clv_limit": clv_rose})
                flat.append({"game": f"g{wk}-{i}", "week": wk, "h": 10, "drift": 0, "vol24": 1, "fill_q": True, "clv_limit": 0})
                flat.append({"game": f"g{wk}-{i}", "week": wk, "h": 60, "drift": 0, "vol24": 1, "fill_q": True, "clv_limit": clv_far})
        g = grade_gates(flat)
        self.assertEqual(g["split"], {"early": [1, 2], "late": [3, 4]})
        rose = g["direction"]["buckets"]["rose"]
        self.assertGreater(rose["early"], rose["late"] > 0)
        self.assertEqual(rose["effect"], rose["late"])
        self.assertEqual(g["timing"]["buckets"]["far"]["effect"], 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
