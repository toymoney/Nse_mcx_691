import sys, tempfile, unittest, math
from datetime import date, datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mcx_gap_scanner as m

CFG = m.Config()


class Maths(unittest.TestCase):
    def test_parity(self):
        F, K, T, v, r = 7500, 7400, 0.1, .3, .06
        c, p = m.black76(F, K, T, v, "CE", r), m.black76(F, K, T, v, "PE", r)
        self.assertAlmostEqual(c - p, math.exp(-r * T) * (F - K), places=6)

    def test_iv_roundtrip(self):
        for side in ("CE", "PE"):
            px = m.black76(7500, 7600, 0.08, .37, side, .06)
            self.assertAlmostEqual(m.implied_vol(px, 7500, 7600, 0.08, side, .06), .37, places=4)

    def test_iv_rejects_below_intrinsic(self):
        self.assertIsNone(m.implied_vol(50, 7500, 7000, .1, "CE", .06))


class Expiry(unittest.TestCase):
    def test_roll(self):
        t = date(2026, 11, 25)
        ex = [date(2026, 11, 30), date(2026, 12, 18)]
        self.assertEqual(m.pick_expiry(ex, t, 5), date(2026, 12, 18))      # 5 days left -> next
        self.assertEqual(m.pick_expiry([date(2026, 12, 1)] + ex[1:], t, 5), date(2026, 12, 1))  # 6 days -> stay
        self.assertEqual(m.pick_expiry([date(2026, 11, 28)], t, 5), None)  # nothing later


class Pipeline(unittest.TestCase):
    def run_demo(self, **kw):
        feed = m.DemoFeed(**kw)
        d = Path(tempfile.mkdtemp())
        n = m.Notifier(d, quiet=True)
        st = {"open": {}, "last_alert": {}}
        sig, res = m.scan_once(feed, CFG, st, n, m.now_ist())
        return feed, sig, res, st, n

    def test_only_injected_signal(self):
        _, sig, res, st, n = self.run_demo()
        self.assertEqual([(s["symbol"], s["strike"], s["side"]) for s in sig], [("CRUDEOIL", 7500.0, "PE")])
        self.assertGreater(sig[0]["gap_pct"], 10)
        self.assertEqual(len(sig[0]["models"]), 2)

    def test_traps_rejected(self):
        _, sig, res, *_ = self.run_demo()
        by = {(r["symbol"], r["strike"], r["side"]): r for r in res}
        self.assertNotEqual(by[("CRUDEOIL", 7300.0, "CE")]["status"], "BUY")   # stale LTP
        self.assertEqual(by[("CRUDEOIL", 7700.0, "CE")]["status"], "SPREAD_WIDE")

    def test_near_expiry_rolled(self):
        _, sig, res, *_ = self.run_demo()
        gas = {r["expiry"] for r in res if r["symbol"] == "NATURALGAS"}
        self.assertEqual(len(gas), 1)
        self.assertGreater((date.fromisoformat(gas.pop()) - m.now_ist().date()).days, 5)

    def test_clean_chain_no_signal(self):
        _, sig, *_ = self.run_demo(inject=False)
        self.assertEqual(sig, [])

    def test_target_exit_and_no_duplicate(self):
        feed, sig, res, st, n = self.run_demo()
        self.assertEqual(len(st["open"]), 1)
        key = sig[0]["key"]
        sent = len(n.sent)
        m.scan_once(feed, CFG, st, n, m.now_ist())          # same data again -> no duplicate alert
        self.assertEqual(len(n.sent), sent)
        feed.book[key]["depth"]["buy"][0]["price"] = sig[0]["fair"] + 1   # price converges
        m.scan_once(feed, CFG, st, n, m.now_ist())
        self.assertTrue(any("TARGET REACHED" in s for s in n.sent))
        self.assertNotIn(key, st["open"])

    def test_wider_costs_raise_bar(self):
        cfg = m.Config(brokerage_per_order=5000.0)           # absurd cost -> nothing should pass
        feed = m.DemoFeed()
        n = m.Notifier(Path(tempfile.mkdtemp()), quiet=True)
        sig, _ = m.scan_once(feed, cfg, {"open": {}, "last_alert": {}}, n, m.now_ist())
        self.assertEqual(sig, [])


class Parsing(unittest.TestCase):
    def test_quote_keyed_by_token(self):
        q = m.parse_quotes({"MCX_FO:X": {"instrument_token": "MCX_FO|1", "last_price": 5, "oi": 9, "volume": 2,
                            "depth": {"buy": [{"price": 4.9, "quantity": 3}], "sell": [{"price": 5.1, "quantity": 4}]}}})
        self.assertEqual((q["MCX_FO|1"].bid, q["MCX_FO|1"].ask, q["MCX_FO|1"].oi), (4.9, 5.1, 9))

    def test_empty_depth_ok(self):
        q = m.parse_quotes({"A:B": {"instrument_token": "MCX_FO|2", "last_price": 1, "depth": {}}})
        self.assertEqual(q["MCX_FO|2"].bid, 0)


if __name__ == "__main__":
    unittest.main()
