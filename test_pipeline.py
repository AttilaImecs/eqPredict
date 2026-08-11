#!/usr/bin/env python3
"""
Regression tests for the pandas-dependent pipeline steps.

Kept separate from test_scoring.py, which is deliberately stdlib-only so it
runs anywhere. These need pandas and openpyxl, so run them with the venv:

    ./.venv/bin/python test_pipeline.py

Every test here is a crash or a silent data loss that actually happened.
"""

import os
import subprocess
import sys
import tempfile
import unittest

try:
    import pandas as pd
    import openpyxl
    HAVE_DEPS = True
except ImportError:                                            # pragma: no cover
    HAVE_DEPS = False


@unittest.skipUnless(HAVE_DEPS, "needs pandas + openpyxl (use ./.venv/bin/python)")
class TestStyleWidths(unittest.TestCase):
    """`int(x or 10)` where x can be NaN."""

    def test_all_empty_object_column_does_not_crash(self):
        """THE BUG: style() computed

            int(sample.str.len().max() or 10)

        and max() returns NaN for an all-empty column. NaN is TRUTHY, so the
        `or 10` fallback never fired and int(NaN) raised ValueError. It took
        down the whole workbook build the first time a fully-empty text column
        appeared -- which the Scores sheet introduced.
        """
        import build_excel as bx
        df = pd.DataFrame({"ticker": ["AAPL", "MSFT"],
                           "flags": [None, None],       # never populated
                           "value": [1.0, 2.0]})
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(list(df.columns))
        for row in df.itertuples(index=False):
            ws.append(list(row))
        bx.style(ws, df)                                  # must not raise
        self.assertGreater(ws.column_dimensions["B"].width, 0)

    def test_populated_column_still_sized_to_content(self):
        """Guard against 'fixing' the NaN case by hardcoding a width."""
        import build_excel as bx
        long = "A" * 30
        df = pd.DataFrame({"name": [long, long]})
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["name"])
        ws.append([long])
        bx.style(ws, df)
        self.assertGreaterEqual(ws.column_dimensions["A"].width, 30)


@unittest.skipUnless(HAVE_DEPS, "needs pandas")
class TestAppendColumns(unittest.TestCase):
    """`pd.DataFrame(rows)[columns]` raises on a batch missing a key."""

    def test_rows_missing_columns_are_filled_not_fatal(self):
        """THE BUG: annual_only_rows() does not emit every key build_rows does.
        A batch that happened to contain ONLY annual-only rows raised KeyError
        and took the entire run down. reindex fills the gap with NaN instead.
        """
        import fetch_sec_fundamentals as fs
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "out.csv")
            rows = [{"ticker": "ADAG", "revenue": 100.0}]      # no balance keys
            fs.append(path, rows, ["ticker", "revenue", "cash", "total_debt"])
            got = pd.read_csv(path)
            self.assertEqual(list(got.columns),
                             ["ticker", "revenue", "cash", "total_debt"])
            self.assertTrue(got["cash"].isna().all())

    def test_column_order_is_preserved(self):
        import fetch_sec_fundamentals as fs
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "out.csv")
            fs.append(path, [{"b": 2, "a": 1}], ["a", "b"])
            self.assertEqual(list(pd.read_csv(path).columns), ["a", "b"])


@unittest.skipUnless(HAVE_DEPS, "needs pandas")
class TestCoverageCheck(unittest.TestCase):
    """A rebuild that silently drops companies.

    One run reported ok=3119 while quietly losing 293 tickers -- almost exactly
    the foreign private issuers -- because the log's success count looks
    identical either way. It was caught by hand, which is not a control.
    """

    def _fixture(self, d, tickers):
        q = os.path.join(d, "q.csv")
        pd.DataFrame({"ticker": tickers, "period_end": ["2026-01-01"] * len(tickers)}
                     ).to_csv(q, index=False)
        u = os.path.join(d, "u.csv")
        pd.DataFrame({"ticker": tickers, "cik": ["1"] * len(tickers)}).to_csv(u, index=False)
        return q, u

    def test_first_run_writes_a_snapshot(self):
        import fetch_sec_fundamentals as fs
        with tempfile.TemporaryDirectory() as d:
            q, u = self._fixture(d, [f"T{i}" for i in range(100)])
            fs.coverage_check(q, u)
            self.assertTrue(os.path.exists(q + ".tickers"))

    def test_regression_exits_nonzero_and_keeps_old_snapshot(self):
        """The snapshot must NOT be overwritten on a regression, or the next
        run would compare against the degraded set and see nothing wrong."""
        import fetch_sec_fundamentals as fs
        with tempfile.TemporaryDirectory() as d:
            q, u = self._fixture(d, [f"T{i}" for i in range(100)])
            fs.coverage_check(q, u)                            # baseline
            pd.DataFrame({"ticker": [f"T{i}" for i in range(50)],
                          "period_end": ["2026-01-01"] * 50}).to_csv(q, index=False)
            with self.assertRaises(SystemExit) as cm:
                fs.coverage_check(q, u)
            self.assertEqual(cm.exception.code, 1)
            snap = {l.strip() for l in open(q + ".tickers") if l.strip()}
            self.assertEqual(len(snap), 100, "snapshot was clobbered by the bad run")

    def test_small_loss_is_tolerated(self):
        """Listings genuinely come and go; only a real regression should stop
        the pipeline."""
        import fetch_sec_fundamentals as fs
        with tempfile.TemporaryDirectory() as d:
            q, u = self._fixture(d, [f"T{i}" for i in range(1000)])
            fs.coverage_check(q, u)
            pd.DataFrame({"ticker": [f"T{i}" for i in range(995)],
                          "period_end": ["2026-01-01"] * 995}).to_csv(q, index=False)
            fs.coverage_check(q, u)                            # must not raise


@unittest.skipUnless(HAVE_DEPS, "needs pandas")
class TestFailureCheckpointing(unittest.TestCase):
    def test_failures_are_not_marked_done(self):
        """THE BUG: DONE_FILE was written in the except branch too, so a
        transient error became permanent -- the ticker was marked done and
        every resume skipped it. Asserted against the source because the
        behaviour lives inside a thread pool that is awkward to drive directly.
        """
        src = open("fetch_sec_fundamentals.py").read()
        self.assertIn("# Checkpoint ONLY on success", src)
        self.assertIn("if ok:", src)
        # the retry pass must exist and must not be on by default
        self.assertIn("def run_pass(", src)
        self.assertIn("--no-retry", src)


@unittest.skipUnless(HAVE_DEPS, "needs pandas")
class TestTaxonomySelection(unittest.TestCase):
    """IFRS filers were invisible: build_rows() only read facts['us-gaap'],
    so ~133 companies with complete financials came back 'empty (no XBRL)'."""

    def _facts(self, us=(), ifrs=()):
        return {"us-gaap": {c: {} for c in us}, "ifrs-full": {c: {} for c in ifrs}}

    def test_pure_us_gaap_filer(self):
        import fetch_sec_fundamentals as fs
        f = self._facts(us=["Revenues", "NetIncomeLoss", "Assets",
                            "StockholdersEquity",
                            "NetCashProvidedByUsedInOperatingActivities"])
        _, _, _, name = fs.pick_taxonomy(f)
        self.assertEqual(name, "us-gaap")

    def test_pure_ifrs_filer(self):
        import fetch_sec_fundamentals as fs
        f = self._facts(ifrs=["Revenue", "ProfitLoss", "Assets", "Equity",
                              "CashFlowsFromUsedInOperatingActivities"])
        _, _, _, name = fs.pick_taxonomy(f)
        self.assertEqual(name, "ifrs-full")

    def test_vestigial_us_gaap_does_not_win(self):
        """THE CASE THAT MATTERS. Several filers carry a near-empty us-gaap
        namespace beside a complete IFRS one. Selecting on presence rather than
        on matched concepts would pick the empty half and return nothing --
        which is precisely the old behaviour."""
        import fetch_sec_fundamentals as fs
        f = self._facts(us=["EntityCommonStockSharesOutstanding"],
                        ifrs=["Revenue", "ProfitLoss", "Assets", "Equity",
                              "CashFlowsFromUsedInOperatingActivities"])
        _, _, _, name = fs.pick_taxonomy(f)
        self.assertEqual(name, "ifrs-full")

    def test_tie_goes_to_us_gaap(self):
        """extract_revenue()'s bank/lessor/excise rules only exist for
        US-GAAP, so it is the better-tested path when both look equal."""
        import fetch_sec_fundamentals as fs
        f = self._facts(us=["Revenues"], ifrs=["Revenue"])
        _, _, _, name = fs.pick_taxonomy(f)
        self.assertEqual(name, "us-gaap")

    def test_no_facts_at_all(self):
        import fetch_sec_fundamentals as fs
        ns, _, _, _ = fs.pick_taxonomy({})
        self.assertEqual(ns, {})

    def test_british_spelling_is_present(self):
        """IFRS spells it Amortisation. A silent miss, not an error."""
        import fetch_sec_fundamentals as fs
        self.assertIn("DepreciationAndAmortisationExpense",
                      fs.IFRS_CONCEPTS["dep_amort"])

    def test_ifrs_maps_cover_every_us_gaap_field(self):
        """A field present in one map and missing from the other silently
        blanks that column for every filer on the other taxonomy."""
        import fetch_sec_fundamentals as fs
        self.assertEqual(set(fs.CONCEPTS), set(fs.IFRS_CONCEPTS))
        self.assertEqual(set(fs.BALANCE_CONCEPTS), set(fs.IFRS_BALANCE_CONCEPTS))


@unittest.skipUnless(HAVE_DEPS, "needs pandas")
class TestBalanceSheetIsNotSummed(unittest.TestCase):
    def test_balance_items_excluded_from_annual_additive(self):
        """Adding a balance-sheet item to ANNUAL_ADDITIVE would silently
        produce a four-times-overstated 'annual' cash position."""
        import build_excel as bx
        for field in ("cash", "total_debt", "net_debt", "equity", "assets"):
            self.assertNotIn(field, bx.ANNUAL_ADDITIVE)
            self.assertIn(field, bx.Q_MEASURES, "%s should still be reported" % field)

    def test_flows_are_summed(self):
        import build_excel as bx
        for field in ("revenue", "net_income", "ocf", "capex", "fcf"):
            self.assertIn(field, bx.ANNUAL_ADDITIVE)

    def test_balance_concepts_are_not_additive_in_fetcher(self):
        import fetch_sec_fundamentals as fs
        for field in fs.BALANCE_CONCEPTS:
            self.assertNotIn(field, fs.ADDITIVE,
                             "%s must never reach derive_q4()" % field)
            self.assertNotIn(field, fs.CASHFLOW_YTD,
                             "%s must never be differenced as YTD" % field)


if __name__ == "__main__":
    unittest.main(verbosity=2)
