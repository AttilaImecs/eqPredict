#!/usr/bin/env python3
"""
Turn the point-in-time sources into the schema score_companies.py already reads.

INPUTS
  dera_quarterly.csv     as-filed fundamentals, keyed on CIK
  pit/universe_*.csv     one point-in-time universe per quarter (cik, ticker, sic)
  pit_monthly.csv        monthly prices for every ticker that appears in any of them

OUTPUTS (the scorer's expected filenames)
  data_quarterly.csv     same rows, keyed on TICKER
  data_monthly.csv       prices + market_cap_est + pe_trailing_est
  data_sic.csv           industry, from the point-in-time `sic` field
  pit/uni_<q>.csv        per-quarter universe in universe.csv's column shape

WHY market_cap_est HAS TO BE BUILT HERE
  In --as-of mode the scorer takes market cap and P/E from data_monthly.csv,
  not from the snapshot, precisely so a historical score cannot read today's
  valuation. DERA has no share count, so it is derived the same way the parent
  pipeline does it -- net_income / eps_diluted, split-adjusted and clustered
  via shares_reconcile -- and multiplied by that month's close.

A CIK CAN HAVE HELD DIFFERENT TICKERS
  The mapping used is the one from the LATEST quarter in which the CIK appears,
  because that is the symbol the price series was fetched under. Where a company
  renamed mid-window the earlier quarters are therefore labelled with the later
  symbol; the CIK is carried through as `cik` so the join stays auditable.
"""

import argparse
import collections
import csv
import glob
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shares_reconcile import current_shares

csv.field_size_limit(10**7)


def num(v):
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pit", default="pit")
    ap.add_argument("--dera", default="dera_quarterly.csv")
    ap.add_argument("--prices", default="pit_monthly.csv")
    args = ap.parse_args()

    # ---- cik -> ticker / sic / company, from the latest quarter seen ----
    cik2t, cik2sic, cik2name = {}, {}, {}
    qfiles = sorted(glob.glob(os.path.join(args.pit, "universe_*.csv")))
    for f in qfiles:                       # ascending, so later overwrites
        for r in csv.DictReader(open(f)):
            c = r["cik"]
            if r["ticker"]:
                cik2t[c] = r["ticker"]
            if r.get("sic"):
                cik2sic[c] = r["sic"]
            if r.get("company"):
                cik2name[c] = r["company"]
    print("cik->ticker mappings: %d" % len(cik2t), file=sys.stderr)

    # ---- fundamentals, keyed on ticker ----
    by_cik = collections.defaultdict(list)
    for r in csv.DictReader(open(args.dera)):
        by_cik[r["cik"]].append(r)

    cols = None
    out_rows = []
    for c, rows in by_cik.items():
        t = cik2t.get(c)
        if not t:
            continue                       # unpriceable; excluded by design
        rows.sort(key=lambda r: r["period_end"])
        for r in rows:
            r = dict(r)
            r["ticker"] = t
            r["company"] = cik2name.get(c, r.get("company", ""))
            out_rows.append(r)
            if cols is None:
                cols = ["ticker"] + [k for k in r if k != "ticker"]
    out_rows.sort(key=lambda r: (r["ticker"], r["period_end"]))
    with open("data_quarterly.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(out_rows)
    print("data_quarterly.csv: %d rows, %d tickers"
          % (len(out_rows), len({r["ticker"] for r in out_rows})), file=sys.stderr)

    # ---- implied share count per ticker, for market cap ----
    shares = {}
    per_t = collections.defaultdict(list)
    for r in out_rows:
        per_t[r["ticker"]].append(r)
    for t, rows in per_t.items():
        s = current_shares(rows, lookback=12)
        if s and s > 0:
            shares[t] = s
    print("implied share counts: %d tickers" % len(shares), file=sys.stderr)

    # ---- TTM net income by month, for a historical P/E ----
    ttm_ni = {}
    for t, rows in per_t.items():
        rows = [r for r in rows if num(r["net_income"]) is not None]
        rows.sort(key=lambda r: r["period_end"])
        series = []
        for i in range(3, len(rows)):
            window = rows[i - 3:i + 1]
            series.append((window[-1]["period_end"][:7],
                           sum(num(x["net_income"]) for x in window)))
        if series:
            ttm_ni[t] = series

    # ---- monthly prices with derived valuation ----
    n_out = 0
    with open("data_monthly.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ticker", "company", "month", "date", "close", "adj_close",
                    "volume", "market_cap_est", "pe_trailing_est"])
        for r in csv.DictReader(open(args.prices)):
            t = r["ticker"]
            c = num(r["close"])
            if c is None:
                continue
            sh = shares.get(t)
            mc = round(c * sh, 0) if sh else None
            pe = ""
            if sh and t in ttm_ni:
                prior = [v for m, v in ttm_ni[t] if m <= r["month"]]
                if prior and prior[-1] > 0:
                    pe = round(c / (prior[-1] / sh), 3)
            w.writerow([t, "", r["month"], r["month"] + "-01", c,
                        r.get("adj_close", ""), r.get("volume", ""),
                        mc if mc else "", pe])
            n_out += 1
    print("data_monthly.csv: %d rows" % n_out, file=sys.stderr)

    # ---- sic ----
    with open("data_sic.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ticker", "cik", "sic", "sic_description", "sector_sic", "exchange"])
        for c, sic in cik2sic.items():
            t = cik2t.get(c)
            if t:
                w.writerow([t, c, sic, "", "", ""])

    # ---- per-quarter universes in universe.csv's shape ----
    for f in qfiles:
        q = os.path.basename(f).replace("universe_", "").replace(".csv", "")
        out = os.path.join(args.pit, "uni_%s.csv" % q)
        with open(out, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["ticker", "company", "sector", "cik",
                        "in_sp500", "in_nasdaq", "in_fortune500"])
            for r in csv.DictReader(open(f)):
                if not r["ticker"]:
                    continue
                w.writerow([r["ticker"], r["company"], "", r["cik"],
                            "False", "False", "False"])
    print("wrote %d per-quarter universes" % len(qfiles), file=sys.stderr)


if __name__ == "__main__":
    main()
