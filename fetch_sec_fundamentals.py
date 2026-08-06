#!/usr/bin/env python3
"""
STEP 2a of 3 -- Pull 5 years of QUARTERLY fundamentals from SEC XBRL companyfacts.

Replaces the fundamentals half of fetch_financials.py, which could only reach
~5 quarters deep via yfinance. SEC XBRL goes back as far as the company has
filed, so a full 20-quarter window is achievable.

Writes data_quarterly.csv with the schema build_excel.py expects:
  ticker, company, period_end, fiscal_quarter, currency, revenue, gross_profit,
  ebit, ebitda, net_income, eps_diluted, gross_margin_pct, ebit_margin_pct,
  ebitda_margin_pct, net_margin_pct

Q4 NOTE
  Companies do not file a 10-Q for their fourth fiscal quarter -- Q4 only
  appears folded into the annual 10-K figure. This script DERIVES Q4 as
  FY - (Q1 + Q2 + Q3) whenever it can match a complete set of three quarters
  inside an annual period. Derived rows are flagged in the `q4_derived` column.

Usage:
  python fetch_sec_fundamentals.py --limit 25     # smoke test
  python fetch_sec_fundamentals.py                # full universe
  python fetch_sec_fundamentals.py --workers 5    # default 5 (SEC caps at 10 req/s)
  python fetch_sec_fundamentals.py --restart      # wipe checkpoints
"""

import argparse
import json
import os
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta

import pandas as pd
import requests

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
UA = "StockPipelineDataCollector/1.0"
BASE = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"

# 6, not 5. A 5-year window starting mid-2021 cannot complete fiscal 2021 for
# any calendar-year filer -- its Q1 and Q2 fall outside the window -- so the
# FY2021 annual column would be empty for almost every company. The extra year
# costs nothing: SEC serves the whole filing history in the same request.
YEARS = 6
Q_FILE = "data_quarterly.csv"
DONE_FILE = "_sec_done.txt"
SHARES_FILE = "data_shares.csv"

REQ_PER_SEC = 8.0          # SEC hard limit is 10/s; stay under it
# Upper bound is 125, not 100. Retailers on a 4-5-4 calendar run one 16-week
# quarter a year alongside three 12-week ones -- Albertsons files 83-day and
# 111-day periods. A 100-day ceiling silently discarded the 16-week quarter,
# costing those companies one quarter EVERY year (Albertsons and Advance Auto
# Parts came back with 13 quarters instead of 24). Nothing legitimate sits
# between 126 and 149 days, and year-to-date figures start at 150.
QUARTER_DAYS = (80, 125)
Q4_DAYS = (80, 125)        # derived-Q4 window; 4-5-4 retailers run a 16-17wk Q4
ANNUAL_DAYS = (350, 380)   # a "fiscal year" duration window
# 20-F / 40-F are the annual reports of foreign private issuers, which do not
# file 10-K/10-Q at all. Without them ~7% of the universe returns nothing.
# 6-K carries their interim figures, but those are usually SEMI-annual, so they
# land in the 150-300 day "ytd" bucket and never masquerade as quarters.
FORMS = ("10-Q", "10-K", "10-K/A", "10-Q/A", "20-F", "20-F/A", "40-F", "40-F/A", "6-K")
ANNUAL_FORMS = ("10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A")

# XBRL concept candidates, tried in order -- filers tag the same line
# item differently depending on industry and filing agent.
CONCEPTS = {
    # Revenue is not a simple candidate list -- see extract_revenue(). The entry
    # here only supplies the generic (non-financial) ordering.
    "revenue": [
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
        "TotalRevenuesAndOtherIncome",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "PremiumsEarnedNet",
    ],
    "gross_profit": ["GrossProfit"],
    "ebit": ["OperatingIncomeLoss"],
    "net_income": [
        "NetIncomeLoss",
        "ProfitLoss",
        "NetIncomeLossAvailableToCommonStockholdersBasic",
    ],
    "eps_diluted": ["EarningsPerShareDiluted", "IncomeLossFromContinuingOperationsPerDilutedShare"],
    "dep_amort": [
        "DepreciationDepletionAndAmortization",
        "DepreciationAmortizationAndAccretionNet",
        "DepreciationAndAmortization",
    ],
}

# Fields where Q4 = FY - (Q1+Q2+Q3) is a valid derivation. All of these are
# flow measures that sum across the year. Balance-sheet items would not be.
ADDITIVE = ("revenue", "gross_profit", "ebit", "net_income", "eps_diluted", "dep_amort")

# Holding-company reorganizations and large mergers create a NEW CIK, and SEC's
# company_tickers.json maps the ticker to that successor. The successor holds
# only post-reorg filings, so the ticker silently returns 2-10 quarters instead
# of 24.
#
# Neither CIK alone is complete -- the successor covers recent quarters, the
# predecessor the earlier ones -- so these are MERGED rather than substituted.
# The primary (successor) CIK wins wherever both report the same period, since
# it reflects the current reporting entity.
CIK_PREDECESSORS = {
    "XOM":  [34088],     # Exxon Mobil Corporation
    "BLK":  [1364742],   # BlackRock Finance, Inc. (pre-2024 holdco reorg)
    "PSKY": [813828],    # Paramount Global (pre-Skydance merger)
}

# RLock, not Lock: the results loop holds this while calling flush_shares(),
# which needs it too. A plain Lock deadlocks the moment the first batch flushes.
_lock = threading.RLock()
_counter = {"n": 0, "ok": 0, "fail": 0, "empty": 0}
_shares = []



def flush_shares(force=False):
    """
    Append accumulated share counts to disk.

    Written incrementally rather than once at the end: a long run is often
    executed in time-boxed chunks, and anything held only in memory is lost
    when the process is interrupted.
    """
    with _lock:
        if not _shares or (len(_shares) < 50 and not force):
            return
        batch, _shares[:] = list(_shares), []
    sh = pd.DataFrame(batch).dropna(subset=["shares_outstanding"])
    if sh.empty:
        return
    hdr = not os.path.exists(SHARES_FILE) or os.path.getsize(SHARES_FILE) == 0
    sh.to_csv(SHARES_FILE, mode="a", header=hdr, index=False)


# --------------------------------------------------------------------------
# Rate limiter -- token bucket shared across all worker threads
# --------------------------------------------------------------------------
class RateLimiter:
    def __init__(self, rate):
        self.rate = rate
        self.tokens = rate
        self.updated = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self):
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.rate, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                wait = (1 - self.tokens) / self.rate
            time.sleep(wait)


_limiter = RateLimiter(REQ_PER_SEC)
_session = threading.local()


def session():
    if not hasattr(_session, "s"):
        s = requests.Session()
        s.headers.update({
            "User-Agent": UA,
            "Accept-Encoding": "gzip, deflate",
            "Accept": "application/json",
        })
        _session.s = s
    return _session.s


def get_facts(cik: int, retries=4):
    url = BASE.format(cik=cik)
    last = None
    for attempt in range(retries):
        _limiter.acquire()
        try:
            r = session().get(url, timeout=60)
            if r.status_code == 404:
                return None                      # no XBRL filings for this CIK
            if r.status_code in (429, 503):      # throttled -- back off hard
                time.sleep(2 ** attempt)
                last = RuntimeError(f"HTTP {r.status_code}")
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:                   # noqa: BLE001
            last = e
            time.sleep(2 ** attempt)
    raise last


# --------------------------------------------------------------------------
# XBRL extraction
# --------------------------------------------------------------------------
def _days(a: str, b: str):
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def pick_unit(concept_obj):
    """Return (unit_name, facts_list). Prefer USD, then USD/shares, then whatever."""
    units = concept_obj.get("units", {})
    for key in ("USD", "USD/shares"):
        if key in units:
            return key, units[key]
    if units:
        k = next(iter(units))
        return k, units[k]
    return None, []


def _collect(facts, cutoff):
    """
    Bucket one concept's fact list into quarterly / annual / ytd dicts.

    Also returns `annual_alts`: every DISTINCT value reported for each annual
    period, not just the latest-filed one. Restatements make a fiscal year
    ambiguous -- IBM reports both 73.620bn and 55.179bn for 2020, the second
    being continuing operations after the Kyndryl separation -- and Q4 can only
    be derived from whichever annual figure matches the vintage of the quarters.
    """
    quarterly, annual, ytd = {}, {}, {}
    annual_alts = defaultdict(set)
    for f in facts:
        start, end, val = f.get("start"), f.get("end"), f.get("val")
        if not start or not end or val is None:
            continue
        if f.get("form") not in FORMS:
            continue
        if date.fromisoformat(end) < cutoff - timedelta(days=400):
            continue
        d = _days(start, end)
        filed = f.get("filed", "")
        key = (start, end)
        if QUARTER_DAYS[0] <= d <= QUARTER_DAYS[1]:
            bucket = quarterly
        elif ANNUAL_DAYS[0] <= d <= ANNUAL_DAYS[1]:
            # Only an annual report delimits a real fiscal year. Some filers tag
            # rolling twelve-month windows inside 10-Qs (Amazon does); treating
            # those as fiscal years mislabels every quarter and, worse, would
            # let Q4 be derived off a period that is not a fiscal year at all.
            if f.get("form") not in ANNUAL_FORMS:
                continue
            bucket = annual
        elif 150 <= d <= 300:
            bucket = ytd          # 6- and 9-month cumulative figures
        else:
            continue
        if bucket is annual:
            annual_alts[key].add(float(val))
        prev = bucket.get(key)
        if prev is None or filed > prev[1]:      # latest filing wins -> restatements
            bucket[key] = (float(val), filed)
    return (
        {k: v[0] for k, v in quarterly.items()},
        {k: v[0] for k, v in annual.items()},
        {k: v[0] for k, v in ytd.items()},
        {k: sorted(v) for k, v in annual_alts.items()},
    )


def ytd_to_quarterly(quarterly: dict, ytd: dict, annual: dict):
    """
    Recover quarterly values from cumulative year-to-date figures.

    Cash-flow-statement items (notably depreciation & amortization) are almost
    always tagged YTD, so a bare ~90-day filter finds only Q1. Differencing
    consecutive cumulative periods that share a fiscal-year start recovers the
    rest. Only fills periods not already present as true quarterly facts.
    """
    out = {}
    by_start = defaultdict(list)
    for (s, e), v in list(ytd.items()) + list(annual.items()) + list(quarterly.items()):
        by_start[s].append((e, v))

    for s, points in by_start.items():
        points.sort()
        prev_end, prev_val = s, 0.0
        for end, val in points:
            key = (prev_end, end)
            d = _days(prev_end, end)
            if QUARTER_DAYS[0] <= d <= QUARTER_DAYS[1] + 10 and key not in quarterly:
                out[key] = val - prev_val
            prev_end, prev_val = end, val
    return out


def extract_series(usgaap: dict, candidates, cutoff: date,
                   use_ytd=False, additive=False, merge="priority"):
    """
    Pull one logical field out of the us-gaap facts blob.

    Candidates are merged rather than first-match-wins: a filer that switches
    tags partway through the window (XOM moved off `Revenues` to
    `RevenueFromContractWithCustomer...` and back) would otherwise lose every
    period tagged with the other concept.

    CRITICAL: Q4 is derived per candidate, BEFORE candidates are merged. Mixing
    an annual figure from one concept with quarters from another produces
    nonsense -- BlackRock tags `Revenues` = $12.8bn for FY2024 while its actual
    total is the $20.4bn `RevenueFromContractWithCustomer...` figure, and
    subtracting the latter's quarters from the former's year gave a NEGATIVE
    fourth-quarter revenue.

    merge="max" picks the largest value per period across candidates instead of
    the first. Revenue uses this: the candidate tags are all revenue, and the
    components are subsets of the total, so the largest is the total. Fixed
    priority cannot work for both American Tower (where `Revenues` 2.7bn is the
    total and contract revenue 0.2bn a component) and BlackRock (where the
    relationship is reversed).

    Returns (quarterly, annual, unit, derived) -- `derived` is the set of
    (start, end) keys whose value was synthesized rather than filed.
    """
    quarterly, annual, derived = {}, {}, set()
    unit = None

    def place(store, k, v):
        if k in store and merge in ("max", "min"):
            store[k] = max(store[k], v) if merge == "max" else min(store[k], v)
        else:
            store.setdefault(k, v)

    for name in candidates:
        obj = usgaap.get(name)
        if not obj:
            continue
        u, facts = pick_unit(obj)
        if not facts:
            continue
        q, a, y, alts = _collect(facts, cutoff)
        if unit is None and (q or a):
            unit = u

        # recover quarters from cumulative YTD figures, within this concept
        if use_ytd and (y or a):
            for k, v in ytd_to_quarterly(q, y, a).items():
                q.setdefault(k, v)

        # derive Q4 from THIS concept's own annual and quarters
        if additive:
            for k, v in derive_q4(q, a, alts).items():
                if k not in q:
                    q[k] = v
                    derived.add(k)

        for k, v in q.items():
            place(quarterly, k, v)
        for k, v in a.items():
            place(annual, k, v)

    return quarterly, annual, unit, derived


def derive_q4(quarterly: dict, annual: dict, annual_alts: dict = None):
    """
    For every annual period, if exactly three quarterly periods sit inside it
    and tile it contiguously from the annual start, synthesize the missing Q4.

    Returns a dict of (start, end) -> value for the derived quarters only.
    """
    derived = {}
    for (a_start, a_end), a_val in annual.items():
        inside = sorted(
            [(s, e, v) for (s, e), v in quarterly.items() if s >= a_start and e <= a_end]
        )
        if len(inside) != 3:
            continue
        # must start at the fiscal year start and be contiguous (allow 3-day slop
        # for 52/53-week fiscal calendars)
        if abs(_days(a_start, inside[0][0])) > 3:
            continue
        contiguous = all(
            abs(_days(inside[i][1], inside[i + 1][0])) <= 4 for i in range(2)
        )
        if not contiguous:
            continue
        q4_start = inside[2][1]
        q4_end = a_end
        d = _days(q4_start, q4_end)
        # Wider than QUARTER_DAYS on purpose: retailers on a 4-5-4 / 52-53 week
        # calendar (Costco, Kroger) run a 16- or 17-week fourth quarter.
        if not (Q4_DAYS[0] <= d <= Q4_DAYS[1]):
            continue
        if (q4_start, q4_end) in quarterly:
            continue

        known = [v for _, _, v in inside]
        base = sum(known)
        candidates = (annual_alts or {}).get((a_start, a_end)) or [a_val]
        if len(candidates) > 1:
            # Restated year: pick the annual figure whose implied Q4 sits
            # closest to the three quarters we already have. Using the
            # latest-filed value blindly mixes a restated year with original
            # quarters -- for IBM 2020 that yielded a Q4 revenue of 1.9bn
            # against an actual 20.4bn.
            ref = base / len(known)
            a_val = min(candidates, key=lambda c: abs((c - base) - ref))
        derived[(q4_start, q4_end)] = a_val - base
    return derived


BANK_MARKERS = (
    "RevenuesNetOfInterestExpense",
    "InterestIncomeExpenseNet",
    "InterestAndDividendIncomeOperating",
)


def extract_revenue(usgaap: dict, cutoff: date):
    """
    Revenue needs its own selection logic; a single candidate list cannot serve
    both banks and operating companies.

    BANKS. The conventional top line is revenue NET of interest expense. A
    bank's `Revenues` tag is gross: German American Bancorp tags 123.2M, which
    is interest income 106.4 + noninterest 16.7, while the figure every data
    provider shows is 89.9 = net interest income 73.2 + noninterest 16.7. So for
    filers that look like banks we take `RevenuesNetOfInterestExpense`, and
    where that is not tagged we compose net interest income + noninterest
    income ourselves.

    EVERYONE ELSE. `Revenues` and `RevenueFromContractWithCustomer...Excluding`
    are both claimed as totals and neither wins universally -- American Tower's
    `Revenues` (2.7bn) is the total against 0.2bn of contract revenue, while
    BlackRock's `Revenues` (12.8bn) is a component of its 20.4bn contract
    revenue. Taking the larger of the two resolves both, because a component is
    by definition smaller than the total. The comparison is deliberately limited
    to those two tags: including the `...IncludingAssessedTax` variant would
    pick up excise tax and overstate tobacco and fuel filers.
    """
    is_bank = any(k in usgaap for k in BANK_MARKERS)

    if is_bank:
        q, a, unit, der = extract_series(
            usgaap, ["RevenuesNetOfInterestExpense"], cutoff, additive=True)
        if q:
            return q, a, unit, der

        nii_q, nii_a, unit, nii_d = extract_series(
            usgaap, ["InterestIncomeExpenseNet",
                     "InterestIncomeExpenseAfterProvisionForLoanLoss"],
            cutoff, additive=True)
        non_q, non_a, _, non_d = extract_series(
            usgaap, ["NoninterestIncome"], cutoff, additive=True)
        if nii_q and non_q:
            # match on period END; the two tags occasionally disagree by a day
            n_by_end = rekey_by_end(nii_q)
            o_by_end = rekey_by_end(non_q)
            q = {(n_by_end[e][0], e): n_by_end[e][1] + o_by_end[e][1]
                 for e in set(n_by_end) & set(o_by_end)}
            na, oa = rekey_by_end(nii_a), rekey_by_end(non_a)
            a = {(na[e][0], e): na[e][1] + oa[e][1] for e in set(na) & set(oa)}
            der = {k for k in q if k[1] in {d[1] for d in nii_d | non_d}}
            if q:
                return q, a, unit, der

    # EXCISE-TAX FILERS (brewers, distillers, tobacco, fuel). Where a filer
    # tags excise tax AND both ...ExcludingAssessedTax and ...Including-
    # AssessedTax, the two differ by that tax and the smaller is the net figure
    # every data provider reports. The tag NAMES cannot be trusted to say which
    # is which: Molson Coors puts gross sales (3,740.0M) in the "Excluding" tag
    # and net sales (3,200.8M) in the "Including" tag, with 539.2M of excise
    # between them. Taking the minimum sidesteps the naming entirely.
    #
    # This requires BOTH variants to be present. Exxon also tags excise but has
    # only one variant, so it correctly keeps its `Revenues` total.
    excise = any("Excise" in k for k in usgaap)
    both_variants = ("RevenueFromContractWithCustomerExcludingAssessedTax" in usgaap
                     and "RevenueFromContractWithCustomerIncludingAssessedTax" in usgaap)
    if excise and both_variants:
        q, a, unit, der = extract_series(
            usgaap, ["RevenueFromContractWithCustomerExcludingAssessedTax",
                     "RevenueFromContractWithCustomerIncludingAssessedTax"],
            cutoff, additive=True, merge="max")
        ex_q, ex_a, _, _ = extract_series(
            usgaap, ["ExciseAndSalesTaxes"], cutoff, additive=True)
        # net = gross - excise. Taking the max above gets the gross figure
        # whichever tag it was filed under, so the subtraction is well defined
        # and cannot double-count the way picking the smaller tag would.
        for series, ex in ((q, ex_q), (a, ex_a)):
            by_end = rekey_by_end(ex)
            for key in list(series):
                hit = by_end.get(key[1])
                if hit and hit[1] and series[key] > hit[1]:
                    series[key] -= hit[1]
        if q:
            return q, a, unit, der

    # LESSORS THAT DO NOT TAG `Revenues`. A residential REIT books rent under
    # OperatingLeaseLeaseIncome and only management fees under contract revenue:
    # Camden Property Trust tags 395.7M of lease income against 2.6M of contract
    # revenue, so the contract tag alone understates it by 99%. Total revenue is
    # the SUM of the two components. American Tower proves the arithmetic --
    # its lease 2,521 + contract 216 equals its tagged `Revenues` of 2,737
    # exactly -- so the composed figure competes safely with `Revenues` below.
    lease_q, lease_a, lease_u, lease_d = extract_series(
        usgaap, ["OperatingLeaseLeaseIncome"], cutoff, additive=True)
    composed_q, composed_a = {}, {}
    if lease_q:
        con_q, con_a, _, _ = extract_series(
            usgaap, ["RevenueFromContractWithCustomerExcludingAssessedTax"],
            cutoff, additive=True)
        for src, con, dst in ((lease_q, rekey_by_end(con_q), composed_q),
                              (lease_a, rekey_by_end(con_a), composed_a)):
            for key, v in src.items():
                hit = con.get(key[1])
                dst[key] = v + (hit[1] if hit else 0.0)

    # Non-financials: max across the total-revenue tags, then a strict-priority
    # fallback for filers that tag none of them.
    q, a, unit, der = extract_series(
        usgaap, ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax"],
        cutoff, additive=True, merge="max")
    if composed_q:
        unit = unit or lease_u
        der = der | {k for k in lease_d if k not in q}
        for key, v in composed_q.items():
            q[key] = max(q[key], v) if key in q else v
        for key, v in composed_a.items():
            a[key] = max(a[key], v) if key in a else v
    if q:
        return q, a, unit, der
    return extract_series(usgaap, CONCEPTS["revenue"], cutoff, additive=True)


def rekey_by_end(quarterly: dict):
    """
    Collapse a {(start, end): value} dict to {end: (start, value)}.

    Filers are not internally consistent about period starts -- AT&T tags the
    same economic quarter as starting 2021-09-30 for one concept and 2021-10-01
    for another. Keying on (start, end) splits that into two half-populated
    rows. The end date is the reliable identifier; where a field has two
    candidate starts for one end, keep the one closest to a 91-day quarter.
    """
    out = {}
    for (s, e), v in quarterly.items():
        prev = out.get(e)
        if prev is None or abs(_days(s, e) - 91) < abs(_days(prev[0], e) - 91):
            out[e] = (s, v)
    return out


def fy_year(d: date) -> int:
    """
    The calendar year a fiscal year should be NAMED for.

    Normally the year the fiscal year ends in. The exception is a 52/53-week
    calendar anchored on 31 December, which drifts a day or two past midnight:
    L3Harris' fiscal 2020 ends 2021-01-01, and naming it FY2021 collides with
    its actual FY2021 and produces two sets of quarters under one label.

    Only the first days of January are treated this way. Retailers ending
    31 January (Walmart's FY2026) genuinely are named for the ending year.
    """
    return d.year - 1 if (d.month == 1 and d.day <= 7) else d.year


def fiscal_label(start, end, fiscal_years):
    """
    Label a quarter as FY-Qn using the company's own fiscal calendar.

    A calendar rule mislabels any filer whose year does not end in December:
    Costco's quarters ending 2026-02-15 and 2026-05-10 both fall in calendar Q1.
    Locating the annual period that contains the quarter gives the true fiscal
    year and the quarter's ordinal position within it. Falls back to a calendar
    label when no annual period covers the quarter (common at the window edge).
    """
    pe = date.fromisoformat(end)
    if start:
        for a_start, a_end in fiscal_years:
            if a_start <= start and end <= a_end:
                idx = min(4, max(1, round(_days(a_start, end) / 91.31)))
                return f"FY{fy_year(date.fromisoformat(a_end))}-Q{idx}"

        # The most recent quarters sit in a fiscal year whose 10-K is not filed
        # yet, so no annual period covers them. Project the last known fiscal
        # year forward a year at a time until it does.
        if fiscal_years:
            a_start, a_end = fiscal_years[-1]
            s, e = date.fromisoformat(a_start), date.fromisoformat(a_end)
            for _ in range(3):
                s, e = s + timedelta(days=364), e + timedelta(days=364)
                if s <= pe <= e + timedelta(days=7):
                    idx = min(4, max(1, round((pe - s).days / 91.31)))
                    return f"FY{fy_year(e)}-Q{idx}"

    mid = (date.fromisoformat(start) + (pe - date.fromisoformat(start)) / 2
           if start else pe - timedelta(days=45))
    return f"{mid.year}-Q{(mid.month - 1) // 3 + 1}"


def shares_outstanding(facts):
    """
    Latest reported common shares outstanding, from the `dei` taxonomy.

    Taken from SEC rather than Yahoo because market cap needs a share count for
    every one of 3,700 tickers, and Yahoo rate-limits per-ticker `.info` calls
    long before that. This figure is as-filed and comes free with the
    companyfacts document already being downloaded.
    """
    dei = (facts.get("facts") or {}).get("dei") or {}
    best_end, best_val = "", None
    for name in ("EntityCommonStockSharesOutstanding",
                 "EntityCommonStockSharesOutstandingCommonClassA"):
        obj = dei.get(name)
        if not obj:
            continue
        for unit, fl in obj.get("units", {}).items():
            if unit != "shares":
                continue
            for f in fl:
                end, val = f.get("end", ""), f.get("val")
                if val and end > best_end:
                    best_end, best_val = end, float(val)
    return best_val, (best_end or None)


def build_rows(ticker, meta, facts, cutoff: date):
    usgaap = (facts.get("facts") or {}).get("us-gaap") or {}
    if not usgaap:
        return []

    series, annual_series, units = {}, {}, {}
    q4_flags = defaultdict(bool)
    annual_periods = set()

    for field, candidates in CONCEPTS.items():
        if field == "revenue":
            q, a, unit, derived = extract_revenue(usgaap, cutoff)
        else:
            q, a, unit, derived = extract_series(
                usgaap, candidates, cutoff,
                use_ytd=(field == "dep_amort"),
                additive=(field in ADDITIVE),
            )
        annual_periods |= set(a)
        for k in derived:
            q4_flags[k[1]] = True          # flag by end date
        series[field] = rekey_by_end(q)
        annual_series[field] = rekey_by_end(a)
        units[field] = unit

    fiscal_years = sorted(annual_periods)

    # union of every period end we saw across all fields
    periods = sorted({k for f in series.values() for k in f})
    currency = units.get("revenue") or units.get("net_income") or ""
    if currency == "USD/shares":
        currency = "USD"

    rows = []
    for end in periods:
        pe = date.fromisoformat(end)
        if pe < cutoff:
            continue

        def val(field):
            hit = series[field].get(end)
            return hit[1] if hit else None

        # period start: prefer revenue's, else whichever field has one
        start = next(
            (series[f][end][0] for f in ("revenue", "net_income", "ebit", "eps_diluted")
             if end in series[f]),
            None,
        )

        rev = val("revenue")
        # Revenue is never negative. If one appears, the underlying concepts
        # disagreed (typically an annual figure from one tag against quarters
        # from another) and the number is not salvageable -- drop it rather than
        # publish it and let it poison every margin computed from it.
        if rev is not None and rev < 0:
            rev = None
        gross = val("gross_profit")
        # Gross profit cannot exceed revenue. When it does, the two came from
        # incompatible bases -- brokers like StoneX tag `GrossProfit` on gross
        # dealings while their top line is net of interest expense. Publishing
        # it would imply a >100% gross margin, so drop it.
        if gross is not None and rev is not None and gross > rev * 1.005:
            gross = None
        ebit = val("ebit")
        net = val("net_income")
        eps = val("eps_diluted")
        da = val("dep_amort")
        ebitda = (ebit + da) if (ebit is not None and da is not None) else None

        def margin(num):
            if rev and num is not None and rev != 0:
                return round(num / rev * 100, 3)
            return None

        fq = fiscal_label(start, end, fiscal_years)

        rows.append({
            "ticker": ticker,
            "company": meta.get("company", ""),
            "period_end": pe.isoformat(),
            "fiscal_quarter": fq,
            "currency": currency,
            "revenue": rev,
            "gross_profit": gross,
            "ebit": ebit,
            "ebitda": ebitda,
            "net_income": net,
            "eps_diluted": eps,
            "gross_margin_pct": margin(gross),
            "ebit_margin_pct": margin(ebit),
            "ebitda_margin_pct": margin(ebitda),
            "net_margin_pct": margin(net),
            "period_start": start,
            "period_type": "Q",
            "q4_derived": bool(q4_flags.get(end, False)),
            "source": "SEC XBRL",
        })

    # drop rows that carry no financial content at all
    money = ("revenue", "gross_profit", "ebit", "net_income", "eps_diluted")
    rows = [r for r in rows if any(r[c] is not None for c in money)]

    rows = drop_impossible_revenue(rows, annual_series.get("revenue", {}), fiscal_years)
    rows = collapse_near_duplicates(rows)
    rows += annual_only_rows(ticker, meta, annual_series, units, rows, cutoff)
    return rows


def drop_impossible_revenue(rows, annual_rev, fiscal_years):
    """
    Discard a quarter whose revenue exceeds the fiscal year that contains it.

    Filers occasionally publish facts at the wrong scale. MacKenzie Realty tags
    lease income of 4,594,058,000,000 where the real figure is 4,594,058 -- out
    by exactly a million -- which produced a quarter of $8 TRILLION and made the
    company the largest in the dataset.

    A quarter cannot be bigger than its own year, so the annual figure is a free
    and reliable upper bound. Where no annual figure exists, fall back to the
    company's own median quarter: a value 50x that is not a real quarter.
    """
    if not rows:
        return rows

    def year_total(end):
        for a_start, a_end in fiscal_years:
            if a_start < end <= a_end:
                hit = annual_rev.get(a_end)
                if hit:
                    return hit[1]
        return None

    vals = sorted(r["revenue"] for r in rows if r.get("revenue"))
    median = vals[len(vals) // 2] if vals else None

    for r in rows:
        rev = r.get("revenue")
        if rev is None or rev <= 0:
            continue
        cap = year_total(r["period_end"])
        bogus = (rev > cap * 1.5) if cap else (median and rev > median * 50)
        if bogus:
            r["revenue"] = None
            for k in ("gross_margin_pct", "ebit_margin_pct",
                      "ebitda_margin_pct", "net_margin_pct"):
                r[k] = None
    return rows


def collapse_near_duplicates(rows, tol_days=12):
    """
    Merge rows whose period ends are only days apart -- they are one quarter.

    Some filers tag both their 52/53-week period and a calendar-aligned one.
    Waters reports the same quarter as ending 2020-09-26 AND 2020-09-30 with an
    identical revenue of 593.784M, which inflated it to 38 quarters in a 24
    quarter window.

    The row with the most populated fields wins, and any field it lacks is
    filled from the duplicate, so no data is lost in the merge.
    """
    if len(rows) < 2:
        return rows
    fields = ("revenue", "gross_profit", "ebit", "ebitda", "net_income", "eps_diluted")
    ordered = sorted(rows, key=lambda r: r["period_end"])

    out, group = [], [ordered[0]]
    for r in ordered[1:]:
        if _days(group[-1]["period_end"], r["period_end"]) <= tol_days:
            group.append(r)
        else:
            out.append(_merge_group(group, fields))
            group = [r]
    out.append(_merge_group(group, fields))
    return out


def _merge_group(group, fields):
    if len(group) == 1:
        return group[0]
    best = max(group, key=lambda r: sum(r.get(f) is not None for f in fields))
    for other in group:
        if other is best:
            continue
        for f in fields:
            if best.get(f) is None and other.get(f) is not None:
                best[f] = other[f]
    rev = best.get("revenue")
    for name, num in (("gross_margin_pct", "gross_profit"), ("ebit_margin_pct", "ebit"),
                      ("ebitda_margin_pct", "ebitda"), ("net_margin_pct", "net_income")):
        v = best.get(num)
        best[name] = round(v / rev * 100, 3) if (rev and v is not None) else None
    return best


def annual_only_rows(ticker, meta, annual_series, units, quarterly_rows, cutoff):
    """
    Emit a full-year row for any fiscal year with NO quarterly coverage.

    Foreign private issuers file a 20-F annually and, at most, semi-annual
    figures on a 6-K -- there is no quarterly data to be had. Without this they
    return nothing at all, which is roughly 7% of the universe.

    Scoped per fiscal year rather than per company: a filer that reports
    quarterly for recent years and only annually for older ones gets the right
    treatment for each. Domestic filers have quarterly coverage everywhere, so
    this produces nothing for them and their output is unchanged.
    """
    covered = {r["period_end"] for r in quarterly_rows}
    currency = units.get("revenue") or units.get("net_income") or ""
    if currency == "USD/shares":
        currency = "USD"

    out = []
    for end, (start, _) in sorted(annual_series.get("revenue", {}).items()
                                  or annual_series.get("net_income", {}).items()):
        pe = date.fromisoformat(end)
        if pe < cutoff:
            continue
        # skip if any quarter of this fiscal year was already emitted
        if any(start < c <= end for c in covered):
            continue

        def val(field):
            hit = annual_series.get(field, {}).get(end)
            return hit[1] if hit else None

        rev = val("revenue")
        if rev is not None and rev < 0:
            rev = None
        ebit, da = val("ebit"), val("dep_amort")
        gross, net, eps = val("gross_profit"), val("net_income"), val("eps_diluted")
        if gross is not None and rev is not None and gross > rev * 1.005:
            gross = None          # same incompatible-basis guard as quarterly rows
        ebitda = (ebit + da) if (ebit is not None and da is not None) else None
        if all(v is None for v in (rev, gross, ebit, net, eps)):
            continue

        def margin(n):
            return round(n / rev * 100, 3) if (rev and n is not None) else None

        out.append({
            "ticker": ticker,
            "company": meta.get("company", ""),
            "period_end": end,
            "fiscal_quarter": f"FY{fy_year(pe)}",
            "currency": currency,
            "revenue": rev, "gross_profit": gross, "ebit": ebit, "ebitda": ebitda,
            "net_income": net, "eps_diluted": eps,
            "gross_margin_pct": margin(gross), "ebit_margin_pct": margin(ebit),
            "ebitda_margin_pct": margin(ebitda), "net_margin_pct": margin(net),
            "period_start": start,
            "period_type": "FY",
            "q4_derived": False,
            "source": "SEC XBRL",
        })
    return out


# --------------------------------------------------------------------------
def fetch_one(rec, cutoff):
    ticker = rec["ticker"]
    cik = rec.get("cik")
    if cik in (None, "", float("nan")) or (isinstance(cik, float) and pd.isna(cik)):
        raise RuntimeError("no CIK")

    ciks = [int(str(cik).strip())] + CIK_PREDECESSORS.get(ticker.upper(), [])

    by_end, shares, shares_asof = {}, None, None
    for c in ciks:                      # priority order: successor first
        facts = get_facts(c)
        if facts is None:
            continue
        if shares is None:
            shares, shares_asof = shares_outstanding(facts)
        for r in build_rows(ticker, rec, facts, cutoff):
            by_end.setdefault(r["period_end"], r)
    rows = [by_end[k] for k in sorted(by_end)]
    with _lock:
        _shares.append({"ticker": ticker, "shares_outstanding": shares,
                        "shares_asof": shares_asof})
    return rows


def append(path, rows, columns):
    if not rows:
        return
    df = pd.DataFrame(rows)[columns]
    need_header = (not os.path.exists(path)) or os.path.getsize(path) == 0
    df.to_csv(path, mode="a", header=need_header, index=False)


COLUMNS = [
    "ticker", "company", "period_end", "fiscal_quarter", "currency",
    "revenue", "gross_profit", "ebit", "ebitda", "net_income", "eps_diluted",
    "gross_margin_pct", "ebit_margin_pct", "ebitda_margin_pct", "net_margin_pct",
    "period_start", "period_type", "q4_derived", "source",
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--restart", action="store_true")
    ap.add_argument("--universe", default="universe.csv")
    ap.add_argument("--out", default=Q_FILE)
    ap.add_argument("--tickers", default="", help="comma-separated ticker subset")
    args = ap.parse_args()

    if args.restart:
        for f in (args.out, DONE_FILE, SHARES_FILE):
            if os.path.exists(f):
                try:
                    os.remove(f)
                except OSError:
                    open(f, "w").close()   # some mounts disallow unlink
        # a zero-byte file would make the header check below think a header exists
        for f in (args.out, DONE_FILE):
            if os.path.exists(f) and os.path.getsize(f) == 0:
                try:
                    os.remove(f)
                except OSError:
                    pass
        print("[sec] checkpoints cleared")

    if not os.path.exists(args.universe):
        sys.exit(f"{args.universe} not found -- run build_universe.py first")

    uni = pd.read_csv(args.universe, dtype={"cik": str})
    uni = uni[uni["cik"].notna() & (uni["cik"] != "")]

    if args.tickers:
        want = {t.strip().upper() for t in args.tickers.split(",")}
        uni = uni[uni["ticker"].str.upper().isin(want)]
    if args.limit:
        uni = uni.head(args.limit)

    done = set()
    if os.path.exists(DONE_FILE):
        done = {ln.strip() for ln in open(DONE_FILE) if ln.strip()}
        print(f"[sec] resuming -- {len(done)} tickers already done")

    todo = [r for r in uni.to_dict("records") if r["ticker"] not in done]
    total = len(todo)
    cutoff = date.today() - timedelta(days=YEARS * 365 + 30)
    print(f"[sec] {total} tickers, {args.workers} workers, cutoff {cutoff}\n")
    if not total:
        print("[sec] nothing to do")
        return

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(fetch_one, r, cutoff): r["ticker"] for r in todo}
        for fut in as_completed(futures):
            tk = futures[fut]
            with _lock:
                _counter["n"] += 1
                n = _counter["n"]
                try:
                    rows = fut.result()
                    append(args.out, rows, COLUMNS)
                    if rows:
                        _counter["ok"] += 1
                        nq4 = sum(1 for r in rows if r["q4_derived"])
                        status = f"ok  {len(rows):>2}q ({nq4} derived)"
                    else:
                        _counter["empty"] += 1
                        status = "empty (no XBRL)"
                except Exception as e:                     # noqa: BLE001
                    _counter["fail"] += 1
                    status = f"FAIL {str(e)[:45]}"
                with open(DONE_FILE, "a") as fh:
                    fh.write(tk + "\n")

                flush_shares()
                if n % 25 == 0 or n == total or total <= 30:
                    rate = n / max(time.time() - t0, 1)
                    eta = (total - n) / max(rate, 0.001) / 60
                    print(f"[{n:>5}/{total}] {tk:<6} {status:<40} | {rate:.1f}/s ETA {eta:.0f}m")

    print(
        f"\n[sec] DONE  ok={_counter['ok']}  empty={_counter['empty']}  "
        f"failed={_counter['fail']}  elapsed {(time.time() - t0) / 60:.1f}m"
    )
    print(f"[sec] wrote {args.out}")

    flush_shares(force=True)
    if os.path.exists(SHARES_FILE):
        print(f"[sec] share counts -> {SHARES_FILE}")

    # Flag short histories so successor-CIK cases can be found without hunting.
    # A recent IPO legitimately has few quarters; an S&P 500 member does not.
    try:
        got = pd.read_csv(args.out, usecols=["ticker", "period_end"])
        n = got.groupby("ticker").size()
        uni_all = pd.read_csv(args.universe, dtype={"cik": str})
        short = uni_all[
            uni_all["ticker"].isin(n[n < 18].index)
            | ~uni_all["ticker"].isin(n.index)
        ].copy()
        short["quarters"] = short["ticker"].map(n).fillna(0).astype(int)
        short = short.sort_values(["in_sp500", "quarters"], ascending=[False, True])
        short.to_csv("sec_short_history.csv", index=False)
        big = short[short["in_sp500"] | short["in_fortune500"]]
        print(f"[sec] {len(short)} tickers with <18 quarters -> sec_short_history.csv")
        if len(big):
            print(f"[sec]   {len(big)} of them are S&P 500 / Fortune 500 members --")
            print("[sec]   check these for successor-CIK cases to add to CIK_PREDECESSORS:")
            for r in big.head(20).itertuples():
                print(f"[sec]     {r.ticker:<6} {r.quarters:>2}q  cik {r.cik}  {str(r.company)[:38]}")
    except Exception as e:                                     # noqa: BLE001
        print(f"[sec] (short-history report skipped: {e})")


if __name__ == "__main__":
    main()
