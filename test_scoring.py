#!/usr/bin/env python3
"""
Regression tests for the scoring chain. Pure stdlib -- `python3 test_scoring.py`.

EVERY TEST HERE IS A BUG THAT SHIPPED.

The scorer produced confidently wrong numbers six times during development, and
each failure was silent: a plausible score computed from a corrupt input. None
of them raised. That is the argument for this file -- the failure mode of this
code is not a crash, it is a number that looks fine and is not.

The real values in the assertions (Booking's two EPS bases, Nvidia's 10:1
split, Mastercard's dei tag) are taken from the actual data, so a future change
that reintroduces any of these breaks a test rather than a workbook.
"""

import unittest

import score_companies as sc
import shares_reconcile as sr


def q(period_end, ni, eps, derived=False):
    return {"period_end": period_end, "net_income": ni, "eps_diluted": eps,
            "q4_derived": "True" if derived else "False"}


class TestImpliedShares(unittest.TestCase):
    """net_income / eps_diluted, and the three ways it goes wrong."""

    def test_derived_q4_excluded(self):
        """Derived Q4 rows imply nonsense: Nvidia's FY2024 Q4 gave -49bn shares.

        eps_diluted is treated as additive when Q4 is derived, but EPS is only
        additive if the share count held still -- the thing being measured.

        The derived row here implies a POSITIVE 17.9M shares -- Booking's real
        2025-12-31 figures. That matters: a negative implied count would be
        caught by the sign guard anyway, so only a plausible-looking wrong
        value actually tests the exclusion.
        """
        rows = [q("2026-03-31", 1083e6, 1.36),                 # -> 796M
                q("2026-06-30", 1950e6, 2.53),                 # -> 771M
                q("2025-12-31", 1428e6, 79.66, derived=True)]  # -> 17.9M, wrong
        periods = [p for p, _ in sr.implied_shares(rows)]
        self.assertNotIn("2025-12-31", periods)
        self.assertEqual(len(periods), 2)

    def test_negative_implied_count_dropped(self):
        """Nvidia's FY2024 derived Q4 implied -49bn shares."""
        rows = [q("2024-03-31", 1000.0, 1.0), q("2024-06-30", -4914.0, 0.1)]
        self.assertEqual([p for p, _ in sr.implied_shares(rows)], ["2024-03-31"])

    def test_negative_share_counts_dropped(self):
        rows = [q("2024-03-31", -500.0, 1.0), q("2024-06-30", 1000.0, 1.0)]
        self.assertEqual([p for p, _ in sr.implied_shares(rows)], ["2024-06-30"])

    def test_booking_two_share_bases(self):
        """THE BUG THIS FILE EXISTS FOR.

        Booking's EPS series interleaves two share bases: 0.40/1.36/2.53
        (~775M shares) with 74.34/84.41 (~34M). Magnitude does not say which is
        right, and an earlier version screened out "small" EPS as unreliable --
        keeping precisely the corrupt values and returning 34M shares, a $6bn
        market cap for a $145bn company, and a P/E of 0.89 against ~20.

        Verbatim from data_quarterly.csv -- the derived-Q4 rows matter, because
        it is their large EPS that drags a whole-history median floor (17.06 ->
        floor 4.27) above every legitimate recent value.
        """
        rows = [q("2024-09-30", 2517e6, 74.34),                 # corrupt basis
                q("2024-12-31", 1068e6, 31.60, derived=True),
                q("2025-03-31", 333e6, 0.40),                   # true -> 832M
                q("2025-06-30", 895e6, 1.10),                   # -> 813M
                q("2025-09-30", 2748e6, 84.41),                 # corrupt basis
                q("2025-12-31", 1428e6, 79.66, derived=True),
                q("2026-03-31", 1083e6, 1.36),                  # -> 796M
                q("2026-06-30", 1950e6, 2.53)]                  # -> 771M
        shares = sr.current_shares(rows)
        self.assertGreater(shares, 700e6, "picked the corrupt minority basis")
        self.assertLess(shares, 900e6)
        # And the consequence that reached the workbook: a $145bn company.
        price = 193.125
        self.assertGreater(price * shares, 100e9)

    def test_no_relative_floor_by_default(self):
        """Directly pins the mechanism the Booking case depends on: without an
        explicit floor_from, only the absolute divide-by-zero guard applies."""
        rows = [q("2025-03-31", 333e6, 0.40), q("2025-06-30", 2748e6, 84.41)]
        self.assertEqual(len(sr.implied_shares(rows)), 2)
        self.assertEqual(len(sr.implied_shares(rows, floor_from=rows)), 1)

    def test_never_returns_a_basis_nobody_reported(self):
        """A plain median across two bases returns their MIDPOINT -- a share
        count no quarter ever implied, and wrong by orders of magnitude in both
        directions. Clustering must return one of the two real bases.
        """
        rows = [q("2025-03-31", 100e6, 10.0),    # -> 10M shares
                q("2025-06-30", 100e6, 10.0),    # -> 10M
                q("2025-09-30", 1000e6, 1.0),    # -> 1000M
                q("2026-03-31", 1000e6, 1.0)]    # -> 1000M
        shares = sr.current_shares(rows)
        self.assertTrue(
            abs(shares - 10e6) < 1e6 or abs(shares - 1000e6) < 1e6,
            "got %.1fM -- a midpoint between bases, not a real one" % (shares / 1e6))

    def test_majority_basis_wins_regardless_of_magnitude(self):
        """The mirror image: if the LARGE values are the majority they win.

        Guards against 'fixing' Booking by simply always preferring small EPS.
        """
        rows = [q("2025-03-31", 1000e6, 50.0), q("2025-06-30", 1000e6, 50.0),
                q("2025-09-30", 1000e6, 50.0), q("2026-03-31", 1000e6, 0.5)]
        self.assertAlmostEqual(sr.current_shares(rows) / 1e6, 20.0, places=1)


class TestReconcile(unittest.TestCase):
    def test_agreement_prefers_dei(self):
        shares, source, agree = sr.reconcile(1000e6, 1010e6)
        self.assertEqual(source, "dei")
        self.assertTrue(agree)

    def test_mastercard_bad_dei_tag_overridden(self):
        """Mastercard is tagged 122.5M shares against ~907M actual, implying a
        $70bn company rather than ~$518bn."""
        shares, source, agree = sr.reconcile(122530193.0, 907087660.0)
        self.assertEqual(source, "implied_override")
        self.assertFalse(agree)
        self.assertAlmostEqual(shares / 1e6, 907.1, places=0)

    def test_missing_dei_falls_back(self):
        """~1,000 tickers have no dei tag at all, including GOOGL/META/NVDA."""
        shares, source, agree = sr.reconcile(None, 12.27e9)
        self.assertEqual(source, "implied")
        self.assertIsNone(agree, "unchecked values must be distinguishable")

    def test_no_estimate_at_all(self):
        self.assertEqual(sr.reconcile(None, None), (None, "", None))


class TestSplitAdjust(unittest.TestCase):
    """Splits in the share-count TREND, which is a different problem: here the
    whole series must be put on ONE basis, not just the latest quarter."""

    def test_nvidia_ten_for_one(self):
        """Uncorrected, Nvidia's 10:1 read as +883% dilution and earned a
        heavy_dilution flag it did not deserve."""
        series = [(0, 2481e6), (1, 2491e6), (2, 24752e6), (3, 24981e6)]
        adj = sc.split_adjust(series)
        last = adj[-1][1]
        self.assertLess(abs(last / 2481e6 - 1), 0.05,
                        "10:1 split not chain-linked out")

    def test_real_dilution_survives(self):
        """UMB Financial's Heartland acquisition is x1.6 -- not a round split
        factor, so it must be preserved as the genuine dilution it is."""
        series = [(0, 100e6), (1, 160e6)]
        adj = sc.split_adjust(series)
        self.assertAlmostEqual(adj[-1][1] / 1e6, 160.0, places=1)

    def test_outlier_rejection(self):
        series = [(0, 100e6), (1, 101e6), (2, 5000e6), (3, 99e6)]
        kept = [v for _, v in sc.reject_outliers(series)]
        self.assertNotIn(5000e6, kept)


class TestPeerRelative(unittest.TestCase):
    """The sector-tilt fix: Financials' top-200 lift fell 3.04x -> 1.48x."""

    def setUp(self):
        metrics = {}
        for i in range(30):                       # bank-like, ~21% net margin
            metrics["BANK%d" % i] = {"_sector": "Financials",
                                     "net_margin": 15.0 + i * 0.4}
        for i in range(30):                       # grocer-like, ~6%
            metrics["FOOD%d" % i] = {"_sector": "Consumer Staples",
                                     "net_margin": 3.0 + i * 0.2}
        metrics["TINY"] = {"_sector": "Nano", "net_margin": 21.0}
        self.ctx = sc.PeerContext(
            metrics, {t: m["_sector"] for t, m in metrics.items()}, sc.DEFAULTS)

    def test_sector_median_companies_score_alike(self):
        """A grocer at its industry median and a bank at its industry median
        must score the same, though their margins differ ~3x. Under absolute
        bands the bank scored 7.0 and the grocer 3.0."""
        bm = self.ctx.median("Financials", "net_margin")
        fm = self.ctx.median("Consumer Staples", "net_margin")
        pb = self.ctx.pctile({"_sector": "Financials", "net_margin": bm}, "net_margin")
        pf = self.ctx.pctile({"_sector": "Consumer Staples", "net_margin": fm}, "net_margin")
        self.assertLess(abs(pb - pf), 6)
        self.assertGreater(bm, fm * 2, "test fixture no longer models the tilt")

    def test_small_sector_falls_back_to_universe(self):
        """Ranking against 4 peers yields 0/25/50/75/100 and nothing between."""
        p = self.ctx.pctile({"_sector": "Nano", "net_margin": 21.0}, "net_margin")
        self.assertIsNotNone(p)

    def test_multiples_are_inverted(self):
        """Lower P/E is BETTER, so a cheap company must rank high."""
        metrics = {"C%d" % i: {"_sector": "X", "pe": float(i + 1)} for i in range(40)}
        ctx = sc.PeerContext(metrics, {t: "X" for t in metrics}, sc.DEFAULTS)
        cheap = ctx.pctile({"_sector": "X", "pe": 2.0}, "pe")
        dear = ctx.pctile({"_sector": "X", "pe": 39.0}, "pe")
        self.assertGreater(cheap, dear, "cheap must outrank expensive")

    def test_negative_pe_excluded_from_distribution(self):
        """A negative P/E is not 'cheap', it means there are no earnings."""
        metrics = {"A": {"_sector": "X", "pe": -5.0}, "B": {"_sector": "X", "pe": 10.0}}
        ctx = sc.PeerContext(metrics, {t: "X" for t in metrics}, sc.DEFAULTS)
        self.assertIsNone(ctx.pctile({"_sector": "X", "pe": -5.0}, "pe"))


class Blank(dict):
    def __missing__(self, k):
        return None


class TestRules(unittest.TestCase):
    def setUp(self):
        self.ctx = sc.PeerContext({}, {}, sc.DEFAULTS)

    def m(self, **kw):
        b = Blank(_sector="X", pos_ttm=0, pos_ttm_known=0)
        b.update(kw)
        return b

    def test_pillars_total_twenty(self):
        for name, fn in sc.RULESETS.items():
            total = sum(mx for _, mx in fn(self.m(), self.ctx).values())
            self.assertAlmostEqual(total, 20.0, msg="%s does not total 20" % name)

    def test_missing_data_drops_rule_rather_than_scoring_zero(self):
        """The difference between 'we don't know' and 'it's bad'. Scoring a
        gap as zero would punish companies for OUR missing data."""
        r = sc.rules_quality(self.m(fcf_conversion=None), self.ctx)
        self.assertIsNone(r["Q5_cash_conversion"][0])
        r = sc.rules_valuation(self.m(p_fcf=None), self.ctx)
        self.assertIsNone(r["V5_price_to_fcf"][0])

    def test_cash_conversion_bands(self):
        self.assertEqual(
            sc.rules_quality(self.m(fcf_conversion=110), self.ctx)["Q5_cash_conversion"][0], 4)
        self.assertEqual(
            sc.rules_quality(self.m(fcf_conversion=15), self.ctx)["Q5_cash_conversion"][0], 0.6)

    def test_score_one_renormalises_over_scored_rules_only(self):
        """A gap in OUR data must not lower a company's score.

        Two companies identical on every rule that COULD be scored, one of them
        missing the inputs for the rest, must come out equal. If a dropped rule
        counted as zero the second would be punished for our coverage.
        """
        full = Blank(_sector="X", pos_ttm=3, pos_ttm_known=3,
                     net_margin=10.0, profitable_q=12, rev_cagr=10.0,
                     rev_yoy=10.0, ret_12m=10.0, fcf_conversion=95.0)
        sparse = Blank(_sector="X", pos_ttm=3, pos_ttm_known=3,
                       net_margin=10.0, profitable_q=12, rev_cagr=10.0,
                       rev_yoy=10.0, ret_12m=10.0)      # no cash-flow data
        a, _, _, ca, _ = sc.score_one(full, self.ctx)
        b, _, _, cb, _ = sc.score_one(sparse, self.ctx)
        self.assertGreater(ca, cb, "confidence must report the missing inputs")
        self.assertGreater(b, 0.0)
        self.assertLess(abs(a - b), 12.0,
                        "sparse company punished for absent data: %.1f vs %.1f" % (a, b))

    def test_score_one_never_exceeds_100(self):
        m = Blank(_sector="X", pos_ttm=3, pos_ttm_known=3, net_margin=50.0,
                  profitable_q=12, rev_cagr=100.0, rev_yoy=100.0, accel=50.0,
                  ret_12m=100.0, ret_6m=100.0, drawdown=-1.0, volatility=5.0,
                  dollar_volume=1e9, fcf_conversion=200.0, margin_delta=20.0,
                  growth_sd=1.0, share_change=-10.0, eps_cagr=50.0)
        total, _, _, _, _ = sc.score_one(m, self.ctx)
        self.assertLessEqual(total, 100.0)
        self.assertGreaterEqual(total, 0.0)

    def test_caps_bind_regardless_of_pillars(self):
        """A company that has never earned anything must not ride a strong
        growth/momentum profile into the top of the list."""
        m = Blank(_sector="X", pos_ttm=0, pos_ttm_known=3, profitable_q=0,
                  window_q=12, rev_cagr=100.0, rev_yoy=100.0, ret_12m=100.0,
                  ret_6m=100.0, drawdown=-1.0, volatility=5.0, dollar_volume=1e9)
        total, _, _, _, flags = sc.score_one(m, self.ctx)
        self.assertIn("chronic_losses", flags)
        self.assertLessEqual(total, sc.DEFAULTS["caps"]["chronic_losses"])

    def test_loss_maker_scores_zero_on_pe_not_dropped(self):
        """'No earnings to value' is a real answer to 'is this cheap', not
        missing data -- so it scores 0 rather than dropping the rule."""
        r = sc.rules_valuation(self.m(pe=-4.0), self.ctx)
        self.assertEqual(r["V1_pe_vs_peers"][0], 0.0)


class TestConfig(unittest.TestCase):
    def test_deep_merge_leaves_siblings_alone(self):
        cfg = sc.deep_merge(sc.DEFAULTS, {"pillar_weights": {"valuation": 30.0}})
        self.assertEqual(cfg["pillar_weights"]["valuation"], 30.0)
        self.assertEqual(cfg["pillar_weights"]["growth"], 20.0)
        self.assertIn("bands", cfg)

    def test_defaults_not_mutated(self):
        sc.deep_merge(sc.DEFAULTS, {"pillar_weights": {"valuation": 99.0}})
        self.assertEqual(sc.DEFAULTS["pillar_weights"]["valuation"], 20.0)

    def test_weights_need_not_sum_to_100(self):
        """The total is renormalised, so re-weighting is a one-line change."""
        cfg = sc.deep_merge(sc.DEFAULTS, {"pillar_weights": {
            "growth": 1.0, "profitability": 1.0, "quality": 1.0,
            "valuation": 1.0, "momentum": 1.0}})
        ctx = sc.PeerContext({}, {}, cfg)
        m = Blank(_sector="X", pos_ttm=3, pos_ttm_known=3, net_margin=10.0,
                  rev_cagr=10.0, ret_12m=10.0)
        total, _, _, _, _ = sc.score_one(m, ctx)
        self.assertGreaterEqual(total, 0.0)
        self.assertLessEqual(total, 100.0)


class TestTTM(unittest.TestCase):
    def test_partial_window_returns_none(self):
        """A partial TTM understates the figure, and an understated denominator
        silently inflates every margin and multiple built on it."""
        rows = [{"revenue": "100"}, {"revenue": "100"},
                {"revenue": None}, {"revenue": "100"}]
        self.assertIsNone(sc.ttm(rows, "revenue"))

    def test_full_window_sums(self):
        rows = [{"revenue": "100"}] * 4
        self.assertEqual(sc.ttm(rows, "revenue"), 400.0)

    def test_offset_reaches_back(self):
        rows = [{"revenue": "1"}] * 4 + [{"revenue": "2"}] * 4
        self.assertEqual(sc.ttm(rows, "revenue"), 8.0)
        self.assertEqual(sc.ttm(rows, "revenue", 4), 4.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
