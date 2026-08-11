#!/usr/bin/env python3
"""
STEP 2c of 3 -- Fill the most recent quarter where SEC's XBRL API lags EDGAR.

WHY THIS EXISTS
  A 10-Q can be filed and public on EDGAR days before it appears in the
  companyfacts API that fetch_sec_fundamentals.py reads. The delay is not a
  uniform lag: on 2026-08-04, Goldman Sachs' 3 August filing had been published
  while Meta's 30 July filing had not. On a 100-ticker sample, 9 companies had
  filed a quarter that the API did not yet carry -- Meta, Visa, PayPal,
  Prologis, NXP, Omnicom, PPG, Lennox and American Tower.

  yfinance carries those figures, because it reads the earnings release rather
  than the XBRL pipeline. This script uses it to fill ONLY that gap.

WHAT IT WILL AND WILL NOT DO
  - Fills only quarters NEWER than the ticker's latest SEC quarter. Gaps in the
    middle of the history are left alone: they are usually a concept the filer
    does not tag, and yfinance would paper over them with a different
    definition rather than the same number.
  - Never overwrites an SEC row. SEC is as-filed and always wins.
  - Marks every filled row `source = "yfinance (SEC pending)"` so the figures
    can be found, audited or excluded with one filter.

Reads:  data_quarterly.csv (SEC), data_quarterly_yf.csv (yfinance)
Writes: data_quarterly.csv (in place, with the extra rows appended)

Usage:
  python backfill_quarters.py
  python backfill_quarters.py --dry-run     # report what would be added
"""

import argparse
import os
import sys

import pandas as pd

SEC_FILE = "data_quarterly.csv"
YF_FILE = "data_quarterly_yf.csv"
TAG = "yfinance (SEC pending)"

MEASURES = ["revenue", "gross_profit", "ebit", "ebitda", "net_income", "eps_diluted"]


def bump_fy(label):
    """`FY2025-Q2` -> `FY2026-Q2`; `2025-Q2` -> `2026-Q2`."""
    s = str(label)
    prefix = "FY" if s.startswith("FY") else ""
    body = s[2:] if prefix else s
    year, _, tail = body.partition("-")
    try:
        return f"{prefix}{int(year) + 1}-{tail}"
    except ValueError:
        return None


def infer_label(sec_rows, end):
    """
    Name the new quarter by looking one year back in the SAME company's SEC
    history and incrementing the fiscal year. Deriving it from the calendar
    would mislabel every filer whose fiscal year is offset.
    """
    for r in sec_rows.itertuples():
        delta = (end - r.period_end).days
        if 340 <= delta <= 390:
            lab = bump_fy(r.fiscal_quarter)
            if lab:
                return lab
    mid = end - pd.Timedelta(days=45)
    return f"{mid.year}-Q{(mid.month - 1) // 3 + 1}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--sec", default=SEC_FILE)
    ap.add_argument("--yf", default=YF_FILE)
    ap.add_argument("--max-age-days", type=int, default=200,
                    help="only fill quarters ending within this many days "
                         "(default 200, about two quarters)")
    args = ap.parse_args()
    today = pd.Timestamp.today().normalize()

    for f in (args.sec, args.yf):
        if not os.path.exists(f):
            sys.exit(f"{f} not found -- run the fetch steps first")

    sec = pd.read_csv(args.sec)
    yf = pd.read_csv(args.yf)
    sec["period_end"] = pd.to_datetime(sec["period_end"], errors="coerce")
    yf["period_end"] = pd.to_datetime(yf["period_end"], errors="coerce")
    yf = yf.dropna(subset=["period_end"])

    if "period_type" not in sec.columns:
        sec["period_type"] = "Q"
    if "source" not in sec.columns:
        sec["source"] = "SEC XBRL"

    latest = sec[sec["period_type"] == "Q"].groupby("ticker")["period_end"].max()
    # Existing period ends per ticker, matched with tolerance rather than
    # exactly: yfinance rounds Apple's quarter to 2026-06-30 where the filing
    # says 2026-06-27, and an exact-match test would add that as a SECOND
    # copy of a quarter already present.
    have_by_ticker = sec.groupby("ticker")["period_end"].apply(list).to_dict()
    # 35 days: yfinance rounds a period end to month end, so Costco's quarter
    # ending 2026-05-10 in the filing appears as 2026-05-31 -- 21 days out, with
    # an identical revenue figure. Anything under ~45 days cannot collide with
    # the neighbouring quarter, which is a full 90 days away.
    TOL_DAYS = 35

    added, tickers = [], set()
    for tk, grp in yf.groupby("ticker"):
        cutoff = latest.get(tk)
        if pd.isna(cutoff):
            continue                      # no SEC history: not our gap to fill
        sec_rows = sec[(sec["ticker"] == tk) & (sec["period_type"] == "Q")]
        currency = sec_rows["currency"].dropna()
        currency = currency.iloc[-1] if len(currency) else ""
        company = sec_rows["company"].dropna()
        company = company.iloc[-1] if len(company) else tk

        existing = have_by_ticker.get(tk, [])
        for r in grp.itertuples():
            end = r.period_end
            if end <= cutoff:
                continue                  # only newer than SEC's latest quarter
            if any(abs((end - e).days) <= TOL_DAYS for e in existing):
                continue                  # same quarter, just dated differently
            if (today - end).days > args.max_age_days:
                continue                  # this is a filing-lag fill, not a
                                          # licence to rebuild old history
            vals = {m: getattr(r, m, None) for m in MEASURES}
            # A row with neither revenue nor net income is not worth the mixed
            # provenance -- yfinance often carries a near-empty column for the
            # most recent period.
            if pd.isna(vals.get("revenue")) and pd.isna(vals.get("net_income")):
                continue
            rev = vals["revenue"]
            rev = None if (pd.isna(rev) or rev < 0) else rev

            def margin(name):
                v = vals[name]
                if rev and v is not None and not pd.isna(v):
                    return round(v / rev * 100, 3)
                return None

            added.append({
                "ticker": tk,
                "company": company,
                "period_end": end.date().isoformat(),
                "fiscal_quarter": infer_label(sec_rows, end),
                "currency": currency,
                **{m: (None if pd.isna(vals[m]) else vals[m]) for m in MEASURES},
                "gross_margin_pct": margin("gross_profit"),
                "ebit_margin_pct": margin("ebit"),
                "ebitda_margin_pct": margin("ebitda"),
                "net_margin_pct": margin("net_income"),
                "period_start": (end - pd.Timedelta(days=91)).date().isoformat(),
                "period_type": "Q",
                "q4_derived": False,
                "source": TAG,
            })
            tickers.add(tk)

    print(f"[backfill] {len(added)} quarters to add across {len(tickers)} tickers")
    for r in sorted(added, key=lambda x: x["ticker"])[:25]:
        rev = r["revenue"]
        rev = f"{rev/1e6:,.0f}M" if rev else "n/a"
        print(f"[backfill]   {r['ticker']:<6} {r['period_end']}  {r['fiscal_quarter']:<12} revenue {rev}")

    if args.dry_run:
        print("[backfill] dry run -- nothing written")
        return
    if not added:
        print("[backfill] nothing to do")
        return

    sec["period_end"] = sec["period_end"].dt.date.astype(str)
    out = pd.concat([sec, pd.DataFrame(added)], ignore_index=True)
    out = out.drop_duplicates(["ticker", "period_end"], keep="first")
    out = out.sort_values(["ticker", "period_end"])
    out.to_csv(args.sec, index=False)
    print(f"[backfill] wrote {args.sec} -- {len(out)} rows "
          f"({(out['source'] == TAG).sum()} from yfinance)")


if __name__ == "__main__":
    main()
