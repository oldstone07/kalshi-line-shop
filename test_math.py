"""Acceptance tests from the build brief. Run: python3 test_math.py  (or pytest)"""
import unittest
from fetch_lines import (prob_to_am, tap_cost, vs_book, never_pay_cents, fair_probs,
                         am_to_prob, profit_book, profit_price, team_block, canon)


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
        t = team_block("SEA", "away", k, {"FD": {"away": -145, "home": 122}, "DK": {"away": -142, "home": 120}}, None)
        self.assertEqual(t["best_book"], "DK")      # -142 pays more than -145
        self.assertEqual(t["limit_am"], -127)
        self.assertEqual(t["tap_am"], -142)
        self.assertEqual(t["vs_book"], 8.15)
        self.assertEqual(t["never_pay"], 58)
        self.assertTrue(t["beats"])

    def test_one_book_missing(self):
        t = team_block("TEN", "away", None, {"FD": {"away": 270, "home": -335}}, None)
        self.assertEqual(t["best_book"], "FD")
        self.assertNotIn("DK", t)
        self.assertNotIn("vs_book", t)


if __name__ == "__main__":
    unittest.main(verbosity=2)
