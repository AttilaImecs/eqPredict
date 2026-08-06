#!/usr/bin/env python3
"""
STEP 2 of 4 (Canada) -- As-filed ANNUAL fundamentals from SEC XBRL, IFRS taxonomy.

WHY THIS IS ANNUAL-ONLY, UNLIKE THE US PIPELINE
  The US script derives 20 quarters because domestic issuers file a 10-Q every
  quarter with fully tagged XBRL. Canadian issuers cross-listed under MJDS file
  a 40-F once a year and put interim results on a 6-K, which carries little or
  no detail tagging. Agnico Eagle's entire XBRL history contains exactly two
  period lengths: 364 and 365 days. There are no quarters to find.

  So SEC supplies the ANNUAL figures here -- as-filed, in IFRS -- and
  fetch_yf_ca.py supplies the quarterly series. See HANDOFF_CANADA.md.

WHY IFRS, NOT US-GAAP
  Canadian filers report under IFRS, so the facts live in the `ifrs-full`
  taxonomy. The US script reads only `facts["us-gaap"]` and would return nothing
  at all for Bank of Nova Scotia or Agnico Eagle. Concept names differ too:
  ProfitLoss rather than NetIncomeLoss, ProfitLossFromOperatingActivities rather
  than OperatingIncomeLoss.

Reads:  universe_ca.csv
Writes: data_annual_sec.csv, _sec_ca_done.txt (resumable)

Usage:
  python fetch_sec_ca.py --limit 25
  python fetch_sec_ca.py --restart --workers 5
"""

import argparse
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

import pandas as pd

from ca_io import read_csv as ca_read_csv
import requests

UA = "StockPipelineDataCollector/1.0"
BASE = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
OUT_FILE = "data_annual_sec.csv"
DONE_FILE = "_sec_ca_done.txt"

YEARS = 6
REQ_PER_SEC = 8.0
ANNUAL_DAYS = (350, 380)
ANNUAL_FORMS = ("40-F", "40-F/A", "20-F", "20-F/A", "10-K", "10-K/A", "6-K")

# Concept candidates spanning BOTH taxonomies.
#
# Not every Canadian issuer reports under IFRS. Enbridge and Canadian Pacific
# Kansas City file under US GAAP and have ZERO ifrs-full concepts, so searching
# only IFRS names returned their operating income (`OperatingIncomeLoss` happens
# to be in both lists) but no revenue at all. Each field therefore lists the
# IFRS name and its us-gaap equivalent, and both taxonomies are searched.
CONCEPTS = {
    "revenue": [
        "Revenue",                                              # IFRS
        "RevenueFromContractsWithCustomers",                    # IFRS
        "RevenueFromSaleOfGoods",                               # IFRS
        "RevenueFromRenderingOfServices",                       # IFRS
        "Revenues",                                             # us-gaap
        "RevenueFromContractWithCustomerExcludingAssessedTax",  # us-gaap
        "RegulatedAndUnregulatedOperatingRevenue",              # us-gaap utilities
        "SalesRevenueNet",
    ],
    # Banks and insurers present revenue net of interest expense.
    "revenue_bank": [
        "RevenueFromInterest",
        "InterestRevenueCalculatedUsingEffectiveInterestMethod",
        "RevenuesNetOfInterestExpense",
    ],
    "cost_of_sales": ["CostOfSales", "CostOfRevenue", "CostOfGoodsAndServicesSold"],
    "gross_profit": ["GrossProfit"],
    "ebit": [
        "ProfitLossFromOperatingActivities",
        "OperatingIncomeLoss",
        "ProfitLossBeforeTax",
    ],
    "net_income": [
        "ProfitLoss",
        "NetIncomeLoss",
        "ProfitLossAttributableToOwnersOfParent",
    ],
    "eps_diluted": [
        "DilutedEarningsLossPerShare",
        "EarningsPerShareDiluted",
        "BasicEarningsLossPerShare",
    ],
    "dep_amort": [
        "DepreciationAndAmortisationExpense",
        "DepreciationDepletionAndAmortization",
        "DepreciationAndAmortization",
    ],
}

_lock = threading.RLock()
_counter = {"n": 0, "ok": 0, "empty": 0, "fail": 0}


class RateLimiter:
    def __init__(self, rate):
        self.rate, self.tokens = rate, rate
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
        s.headers.update({"User-Agent": UA, "Accept-Encoding": "gzip, deflate"})
        _session.s = s
    return _session.s


def get_facts(cik, retries=4):
    last = None
    for attempt in range(retries):
        _limiter.acquire()
        try:
            r = session().get(BASE.format(cik=cik), timeout=60)
            if r.status_code == 404:
                return None
            if r.status_code in (429, 503):
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:                        # noqa: BLE001
            last = e
            time.sleep(2 ** attempt)
    raise last


def _days(a, b):
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def pick_unit(units):
    """
    Prefer the company's own reporting currency.

    Canadian issuers are split: Agnico Eagle reports in USD, Royal Bank in CAD.
    Neither is 'wrong', so the currency is recorded per row and NOT converted.
    """
    for key in ("CAD", "USD", "CAD/shares", "USD/shares"):
        if key in units:
            return key, units[key]
    if units:
        k = next(iter(units))
        return k, units[k]
    return None, []


def annual_series(taxonomy, candidates, cutoff):
    """{end_date: (start, value)} for annual periods, latest filing wins."""
    for name in candidates:
        obj = taxonomy.get(name)
        if not obj:
            continue
        unit, facts = pick_unit(obj.get("units", {}))
        if not facts:
            continue
        out = {}
        for f in facts:
            start, end, val = f.get("start"), f.get("end"), f.get("val")
            if not start or not end or val is None:
                continue
            if f.get("form") not in ANNUAL_FORMS:
                continue
            if date.fromisoformat(end) < cutoff:
                continue
            if not (ANNUAL_DAYS[0] <= _days(start, end) <= ANNUAL_DAYS[1]):
                continue
            prev = out.get(end)
            filed = f.get("filed", "")
            if prev is None or filed > prev[2]:
                out[end] = (start, float(val), filed)
        if out:
            return {k: (v[0], v[1]) for k, v in out.items()}, unit
    return {}, None


def build_rows(rec, facts, cutoff):
    fa = facts.get("facts") or {}
    # Search BOTH taxonomies. A filer uses one or the other, and a few tag a
    # handful of concepts in each; merging costs nothing and avoids missing a
    # us-gaap filer entirely. IFRS wins a name collision (GrossProfit,
    # ProfitLoss) since those mean the same thing in both.
    tax = {**(fa.get("us-gaap") or {}), **(fa.get("ifrs-full") or {})}
    if not tax:
        return []

    series, units = {}, {}
    for field in ("revenue", "cost_of_sales", "gross_profit", "ebit",
                  "net_income", "eps_diluted", "dep_amort"):
        series[field], units[field] = annual_series(tax, CONCEPTS[field], cutoff)

    # Banks: the generic Revenue tag is often absent; fall back to interest revenue.
    if not series["revenue"]:
        series["revenue"], units["revenue"] = annual_series(
            tax, CONCEPTS["revenue_bank"], cutoff)

    currency = units.get("revenue") or units.get("net_income") or ""
    if currency.endswith("/shares"):
        currency = currency.split("/")[0]

    rows = []
    for end in sorted({k for s in series.values() for k in s}):
        pe = date.fromisoformat(end)

        def val(f):
            hit = series[f].get(end)
            return hit[1] if hit else None

        rev, gross, cos = val("revenue"), val("gross_profit"), val("cost_of_sales")
        # IFRS filers frequently tag cost of sales but not gross profit.
        if gross is None and rev is not None and cos is not None:
            gross = rev - cos
        if gross is not None and rev is not None and gross > rev * 1.005:
            gross = None                      # incompatible bases -- see US handoff
        if rev is not None and rev < 0:
            rev = None

        ebit, da, net = val("ebit"), val("dep_amort"), val("net_income")
        ebitda = (ebit + da) if (ebit is not None and da is not None) else None
        start = next((series[f][end][0] for f in series if end in series[f]), None)

        def margin(v):
            return round(v / rev * 100, 3) if (rev and v is not None) else None

        if all(v is None for v in (rev, gross, ebit, net, val("eps_diluted"))):
            continue

        rows.append({
            "ticker": rec["ticker"],
            "company": rec.get("company", ""),
            "period_end": end,
            # fiscal years ending in the first days of January belong to the
            # prior year -- same 52/53-week issue as the US pipeline
            "fiscal_quarter": f"FY{pe.year - 1 if (pe.month == 1 and pe.day <= 7) else pe.year}",
            "currency": currency,
            "revenue": rev, "gross_profit": gross, "ebit": ebit, "ebitda": ebitda,
            "net_income": net, "eps_diluted": val("eps_diluted"),
            "gross_margin_pct": margin(gross), "ebit_margin_pct": margin(ebit),
            "ebitda_margin_pct": margin(ebitda), "net_margin_pct": margin(net),
            "period_start": start,
            "period_type": "FY",
            "q4_derived": False,
            "source": "SEC XBRL (IFRS)",
        })
    return rows


COLUMNS = ["ticker", "company", "period_end", "fiscal_quarter", "currency",
           "revenue", "gross_profit", "ebit", "ebitda", "net_income", "eps_diluted",
           "gross_margin_pct", "ebit_margin_pct", "ebitda_margin_pct",
           "net_margin_pct", "period_start", "period_type", "q4_derived", "source"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--restart", action="store_true")
    ap.add_argument("--universe", default="universe_ca.csv")
    ap.add_argument("--out", default=OUT_FILE)
    ap.add_argument("--tickers", default="")
    args = ap.parse_args()

    if args.restart:
        for f in (args.out, DONE_FILE):
            if os.path.exists(f):
                try:
                    os.remove(f)
                except OSError:
                    open(f, "w").close()
        print("[sec-ca] checkpoints cleared")

    if not os.path.exists(args.universe):
        sys.exit(f"{args.universe} not found -- run build_universe_ca.py first")

    uni = ca_read_csv(args.universe, dtype={"cik": str})
    uni = uni[uni["cik"].notna() & (uni["cik"] != "")]
    if args.tickers:
        want = {t.strip().upper() for t in args.tickers.split(",")}
        uni = uni[uni["ticker"].str.upper().isin(want)]
    if args.limit:
        uni = uni.head(args.limit)

    done = set()
    if os.path.exists(DONE_FILE):
        done = {l.strip() for l in open(DONE_FILE) if l.strip()}
    todo = [r for r in uni.to_dict("records") if r["ticker"] not in done]
    cutoff = date.today() - timedelta(days=YEARS * 365 + 30)
    print(f"[sec-ca] {len(todo)} cross-listed tickers, cutoff {cutoff}\n")
    if not todo:
        print("[sec-ca] nothing to do")
        return

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(lambda r: build_rows(r, get_facts(int(r["cik"])) or {}, cutoff), r):
                r["ticker"] for r in todo}
        for fut in as_completed(futs):
            tk = futs[fut]
            with _lock:
                _counter["n"] += 1
                try:
                    rows = fut.result()
                    if rows:
                        df = pd.DataFrame(rows)[COLUMNS]
                        hdr = not os.path.exists(args.out) or os.path.getsize(args.out) == 0
                        df.to_csv(args.out, mode="a", header=hdr, index=False)
                        _counter["ok"] += 1
                        status = f"ok {len(rows)} years"
                    else:
                        _counter["empty"] += 1
                        status = "no IFRS annual data"
                except Exception as e:                 # noqa: BLE001
                    _counter["fail"] += 1
                    status = f"FAIL {str(e)[:40]}"
                with open(DONE_FILE, "a") as fh:
                    fh.write(tk + "\n")
                print(f"[{_counter['n']:>4}/{len(todo)}] {tk:<8} {status}")

    print(f"\n[sec-ca] ok={_counter['ok']} empty={_counter['empty']} "
          f"failed={_counter['fail']}  {(time.time() - t0)/60:.1f}m")
    print(f"[sec-ca] wrote {args.out}")


if __name__ == "__main__":
    main()
