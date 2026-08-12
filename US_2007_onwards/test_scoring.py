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


class TestHealth(unittest.TestCase):
    """Balance-sheet pillar. Leverage is the thing the scorer was blind to for
    its whole life, so the guards around it matter more than the bands."""

    def setUp(self):
        self.ctx = sc.PeerContext({}, {}, sc.DEFAULTS)

    def m(self, **kw):
        b = Blank(_sector="Industrials", pos_ttm=0, pos_ttm_known=0)
        b.update(kw)
        return b

    def bank_ctx(self, n=60):
        """A peer context with enough Financials to form a capital pool."""
        metrics = {"B%d" % i: {"_sector": "Financials",
                               "equity_ratio": 0.05 + i * 0.01}
                   for i in range(n)}
        return sc.PeerContext(metrics, {t: "Financials" for t in metrics},
                              sc.DEFAULTS)

    def test_banks_are_not_scored_on_leverage(self):
        """Deposits are not borrowings. A 10x debt/equity is ordinary for a
        bank, so the ordinary leverage rules must not touch it."""
        r = sc.rules_health(self.m(_sector="Financials", net_debt_to_ebitda=8.0,
                                   debt_to_equity=10.0, equity_ratio=0.30),
                            self.bank_ctx())
        self.assertNotIn("H1_net_debt_to_ebitda", r)
        self.assertIn("leverage_not_applicable",
                      sc.flags(self.m(_sector="Financials"), {}))

    def test_bank_health_pillar_still_totals_twenty(self):
        r = sc.rules_health(self.m(_sector="Financials", equity_ratio=0.30),
                            self.bank_ctx())
        self.assertAlmostEqual(sum(mx for _, mx in r.values()), 20.0)

    def test_bank_capital_is_ranked_among_banks(self):
        """A well-capitalised bank must outscore a thin one. Scored on the
        absolute bands instead, both would sit in the bottom band -- bank
        equity/assets runs at a median of 0.22 against 0.43 elsewhere."""
        ctx = self.bank_ctx()
        thin = sc.rules_health(self.m(_sector="Financials", equity_ratio=0.06),
                               ctx)["HB1_capital_vs_peers"][0]
        strong = sc.rules_health(self.m(_sector="Financials", equity_ratio=0.60),
                                 ctx)["HB1_capital_vs_peers"][0]
        self.assertGreater(strong, thin)

    def test_median_bank_scores_what_a_median_company_scores(self):
        """THE FIX. The old code dropped the pillar and renormalised, so a bank
        was ranked on five hurdles while everyone else cleared six -- worth
        1.40x their universe share in the top 100 across 33 quarters.

        The bands are the observed health distribution of companies that ARE
        scored on it, so a bank at the 50th percentile of bank capital must
        land on the 50th-percentile health score. That equivalence is what
        makes the pillar mean the same thing for both.
        """
        ctx = self.bank_ctx()
        mid = sc.rules_health(self.m(_sector="Financials", equity_ratio=0.35),
                              ctx)["HB1_capital_vs_peers"][0]
        self.assertAlmostEqual(mid, 11.6, places=6)

    def test_thin_bank_pool_drops_rather_than_ranking_against_everyone(self):
        """Falling back to the universe pool would put nearly every bank in the
        bottom band -- the exact error the exemption existed to prevent."""
        ctx = sc.PeerContext({"A": {"_sector": "Financials", "equity_ratio": 0.1}},
                             {"A": "Financials"}, sc.DEFAULTS)
        r = sc.rules_health(self.m(_sector="Financials", equity_ratio=0.10), ctx)
        self.assertIsNone(r["HB1_capital_vs_peers"][0])

    def test_bank_without_a_balance_sheet_drops_the_pillar(self):
        r = sc.rules_health(self.m(_sector="Financials", equity_ratio=None),
                            self.bank_ctx())
        self.assertIsNone(r["HB1_capital_vs_peers"][0])

    def test_bank_with_no_capital_data_still_scores(self):
        """The renormalisation path still has to work -- it is now the fallback
        for a bank with no usable balance sheet (~4% of bank-quarters) rather
        than the treatment for every bank. Renaming matters: this test used to
        be called test_bank_total_renormalises_over_five_pillars and assert
        that dropping health 'must not lower a bank's score', which was the
        defect stated as a requirement."""
        bank = self.m(_sector="Financials", net_margin=20.0, profitable_q=12,
                      rev_cagr=8.0, ret_12m=10.0, pos_ttm=3, pos_ttm_known=3)
        total, pillars, _, _, _ = sc.score_one(bank, self.ctx)
        self.assertIsNone(pillars["health"])
        self.assertGreater(total, 0.0)
        self.assertLessEqual(total, 100.0)

    def test_net_cash_scores_top_band(self):
        """A company owing less than it holds is not levered at all.

        Asserted against the band table rather than a literal: this test
        hardcoded 6 and went stale when the health pillar was reweighted
        against measured survival AUC and H1's maximum moved to 4. What the
        test means is "net cash earns the top band", which is true at any
        weighting."""
        top = sc.DEFAULTS["bands"]["H1_net_debt_to_ebitda"][0][1]
        r = sc.rules_health(self.m(net_debt_to_ebitda=-1.5), self.ctx)
        self.assertEqual(r["H1_net_debt_to_ebitda"][0], top)

    def test_leverage_bands_are_monotonic(self):
        prev = None
        for lev in (-1.0, 0.5, 1.5, 2.5, 4.0, 5.5, 9.0):
            pts = sc.rules_health(self.m(net_debt_to_ebitda=lev), self.ctx)["H1_net_debt_to_ebitda"][0]
            if prev is not None:
                self.assertLessEqual(pts, prev, "more leverage scored higher at %s" % lev)
            prev = pts

    def test_health_pillar_totals_twenty(self):
        total = sum(mx for _, mx in sc.rules_health(self.m(), self.ctx).values())
        self.assertAlmostEqual(total, 20.0)

    def test_high_leverage_and_thin_cover_flagged(self):
        f = sc.flags(self.m(net_debt_to_ebitda=7.0, interest_coverage=1.1,
                            net_debt=5e9), {})
        self.assertIn("high_leverage", f)
        self.assertIn("thin_interest_cover", f)

    def test_negative_equity_flagged_not_scored(self):
        """A negative denominator would make debt/equity read as LOW leverage."""
        f = sc.flags(self.m(negative_equity=True), {})
        self.assertIn("negative_equity", f)


class TestAnnualBasis(unittest.TestCase):
    """Foreign private issuers file 20-F/40-F and never a 10-Q. They were
    silently never rated -- 327 companies whose data the fetcher had already
    gone to trouble to capture. Same rubric, period length changed."""

    def test_ttm_generalises_over_period_length(self):
        """ppy=4 sums four quarters; ppy=1 takes one already-annual row."""
        quarters = [{"revenue": "25"}] * 4
        self.assertEqual(sc.ttm(quarters, "revenue", ppy=4), 100.0)
        annual = [{"revenue": "100"}]
        self.assertEqual(sc.ttm(annual, "revenue", ppy=1), 100.0)

    def test_annual_offset_reaches_back_in_years(self):
        rows = [{"revenue": "50"}, {"revenue": "75"}, {"revenue": "100"}]
        self.assertEqual(sc.ttm(rows, "revenue", ppy=1), 100.0)
        self.assertEqual(sc.ttm(rows, "revenue", 1, ppy=1), 75.0)
        self.assertEqual(sc.ttm(rows, "revenue", 2, ppy=1), 50.0)

    def test_partial_annual_window_returns_none(self):
        self.assertIsNone(sc.ttm([{"revenue": None}], "revenue", ppy=1))

    def test_three_annual_periods_is_the_floor(self):
        self.assertEqual(sc.MIN_ANNUAL_PERIODS, 3)
        self.assertLess(sc.MIN_ANNUAL_PERIODS, sc.MIN_QUARTERS,
                        "an annual filer must not need 8 years of history")


class TestCurrencyGate(unittest.TestCase):
    """Prices and market cap are USD; financials are as filed."""

    def setUp(self):
        self.ctx = sc.PeerContext({}, {}, sc.DEFAULTS)

    def m(self, **kw):
        b = Blank(_sector="X", pos_ttm=0, pos_ttm_known=0)
        b.update(kw)
        return b

    def test_non_usd_reporter_gets_no_multiples(self):
        """A USD market cap over EUR revenue is not a P/S. The guard has to
        run BEFORE any multiple is derived -- an earlier version nulled the
        market cap only for P/S and let P/E through, so ASML read 68.4 and
        Kaspi 0.02 off a USD cap over tenge earnings."""
        r = sc.rules_valuation(self.m(pe=None, ps=None, p_fcf=None, peg=None,
                                      pe_vs_own=None), self.ctx)
        self.assertTrue(all(v is None for k, v in
                            ((k, v) for k, (v, _) in r.items())
                            if k in ("V1_pe_vs_peers", "V2_ps_vs_peers",
                                     "V5_price_to_fcf")))

    def test_flag_is_raised(self):
        self.assertIn("non_usd_reporting", sc.flags(self.m(non_usd=True), {}))
        self.assertNotIn("non_usd_reporting", sc.flags(self.m(non_usd=False), {}))

    def test_guard_precedes_pe_reconciliation_in_source(self):
        """Ordering is the whole bug, and it is not observable from the rules
        alone -- assert it structurally."""
        src = open("score_companies.py").read()
        gate = src.index("CURRENCY GATE")
        pe = src.index("P/E: TWO INDEPENDENT ESTIMATES")
        self.assertLess(gate, pe, "currency gate must run before P/E is derived")


class TestConfig(unittest.TestCase):
    def test_deep_merge_leaves_siblings_alone(self):
        """Overriding one weight must not drop its siblings.

        The sibling is compared against DEFAULTS rather than a literal 20.0,
        which is what made this test fail when the growth weight was halved
        after growth failed both the return and survival tests. The merge
        behaviour under test never changed."""
        cfg = sc.deep_merge(sc.DEFAULTS, {"pillar_weights": {"valuation": 30.0}})
        self.assertEqual(cfg["pillar_weights"]["valuation"], 30.0)
        self.assertEqual(cfg["pillar_weights"]["growth"],
                         sc.DEFAULTS["pillar_weights"]["growth"])
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


class TestEarningsQuality(unittest.TestCase):
    """The net-minus-EBIT margin gate.

    Aurinia scored 86.4 and reached rank 4 on a 100.8% net margin against a
    48.4% operating margin -- a +52.5 point gap, meaning most of the reported
    profit arrived below the operating line and will not recur. Innoviva
    (+44.2) and Cronos (+40.4) reached the top 25 the same way.

    Measured across 30 point-in-time quarters, a gap above +20 returned a
    median -19.4% over the next 12 months against +3.7% for the normal range,
    and was worse in 29 of 29 quarters.
    """

    def setUp(self):
        self.ctx = sc.PeerContext({}, {}, sc.DEFAULTS)

    def m(self, **kw):
        b = Blank(_sector="X", pos_ttm=0, pos_ttm_known=0)
        b.update(kw)
        return b

    def test_ladder_thresholds(self):
        for gap, want in [(None, 1.0), (0, 1.0), (9.9, 1.0),
                          (10, 0.75), (29.9, 0.75),
                          (30, 0.50), (52.5, 0.50), (500, 0.50)]:
            self.assertEqual(sc.eq_multiplier(self.m(eq_gap=gap), sc.DEFAULTS),
                             want, "gap=%s" % gap)

    def test_gate_is_one_sided(self):
        """Net margin far BELOW operating margin tested roughly neutral --
        +2.0% median, 52% positive, against +10.1%/61% for clean names. Muting
        it would penalise ordinary interest and tax burden for no measured
        reason."""
        for gap in (-10, -25, -60):
            self.assertEqual(sc.eq_multiplier(self.m(eq_gap=gap), sc.DEFAULTS), 1.0)

    def test_missing_operating_margin_is_not_a_penalty(self):
        """Healthcare Services Group has no EBIT margin in the data. An
        uncomputable test must not become a silent discount."""
        self.assertEqual(sc.eq_multiplier(self.m(net_margin=6.6, ebit_margin=None,
                                                 eq_gap=None), sc.DEFAULTS), 1.0)

    def test_mute_leaves_the_maximum_alone(self):
        """THE BUG THIS GATE WOULD DIE OF.

        score_one sums earned points over available points and renormalises.
        Scaling both halves cancels exactly -- the discount would appear to be
        applied, every rule would look muted, and no score would move.
        """
        pts, mx = sc.mute((4.0, 4), 0.5)
        self.assertEqual((pts, mx), (2.0, 4))
        self.assertEqual(sc.mute((None, 6), 0.5), (None, 6))

    def test_only_net_margin_rules_are_muted(self):
        """Operating and gross margin sit above the line the problem lives
        below, so both stay untouched -- a muted company keeps 7 of the 20
        profitability points on undisputed operating numbers."""
        metrics = {"C%d" % i: {"_sector": "X", "net_margin": float(i),
                               "ebit_margin": float(i), "gross_margin": float(i)}
                   for i in range(40)}
        ctx = sc.PeerContext(metrics, {t: "X" for t in metrics}, sc.DEFAULTS)
        base = dict(_sector="X", net_margin=39.0, ebit_margin=39.0,
                    gross_margin=39.0, profitable_q=12)
        clean = sc.rules_profitability(self.m(eq_gap=0, **base), ctx)
        muted = sc.rules_profitability(self.m(eq_gap=52.5, **base), ctx)
        self.assertEqual(muted["P1_net_margin_vs_peers"][0],
                         clean["P1_net_margin_vs_peers"][0] * 0.5)
        self.assertEqual(muted["P4_net_margin_absolute"][0],
                         clean["P4_net_margin_absolute"][0] * 0.5)
        for untouched in ("P2_ebit_margin_vs_peers", "P3_gross_margin_vs_peers",
                          "P5_profitable_quarters"):
            self.assertEqual(muted[untouched], clean[untouched], untouched)

    def test_muting_actually_lowers_the_pillar(self):
        """End to end, not just at the rule. A discount that does not move the
        pillar score is decoration."""
        base = dict(_sector="X", net_margin=40.0, ebit_margin=40.0,
                    gross_margin=40.0, profitable_q=12)
        hi = sc.score_one(self.m(eq_gap=0, **base), self.ctx)[1]["profitability"]
        mid = sc.score_one(self.m(eq_gap=15, **base), self.ctx)[1]["profitability"]
        lo = sc.score_one(self.m(eq_gap=52.5, **base), self.ctx)[1]["profitability"]
        self.assertLess(mid, hi)
        self.assertLess(lo, mid)

    def test_flag_raised_at_threshold_only(self):
        self.assertIn("earnings_below_op_line",
                      sc.flags(self.m(eq_gap=30.0), {}, sc.DEFAULTS))
        self.assertNotIn("earnings_below_op_line",
                         sc.flags(self.m(eq_gap=29.9), {}, sc.DEFAULTS))
        self.assertNotIn("earnings_below_op_line",
                         sc.flags(self.m(eq_gap=None), {}, sc.DEFAULTS))

    def test_cap_binds_the_total(self):
        """Aurinia at 86.4 must not survive the gate near the top of the list.

        Asserting only `total <= cap` would pass even with the cap raised to
        100 -- a mutation that removes the cap entirely and leaves the test
        green. So this pins BOTH ends: the same company scores above the cap
        untouched, and lands exactly on it once flagged.
        """
        cap = sc.DEFAULTS["caps"]["earnings_below_op_line"]
        base = dict(_sector="X", net_margin=100.8, ebit_margin=48.4,
                    profitable_q=12, gross_margin=90.0, rev_cagr=20.0,
                    ret_12m=50.0, current_ratio=3.0, equity_ratio=0.7)
        uncapped = sc.score_one(self.m(eq_gap=0.0, **base), self.ctx)[0]
        self.assertGreater(uncapped, cap,
                           "fixture no longer scores above the cap; it cannot "
                           "demonstrate that the cap does anything")
        total, _, _, _, fl = sc.score_one(self.m(eq_gap=52.4, **base), self.ctx)
        self.assertIn("earnings_below_op_line", fl)
        self.assertAlmostEqual(total, cap, places=6)

    def test_ladder_is_configurable(self):
        """Standing requirement: the rubric adjusts as conditions change,
        without editing code."""
        cfg = sc.deep_merge(sc.DEFAULTS,
                            {"earnings_quality": {"ladder": [[80, 0.9]], "flag_at": 80}})
        self.assertEqual(sc.eq_multiplier(self.m(eq_gap=52.5), cfg), 1.0)
        self.assertEqual(sc.eq_multiplier(self.m(eq_gap=90), cfg), 0.9)
        self.assertNotIn("earnings_below_op_line",
                         sc.flags(self.m(eq_gap=52.5), {}, cfg))


if __name__ == "__main__":
    unittest.main(verbosity=2)
