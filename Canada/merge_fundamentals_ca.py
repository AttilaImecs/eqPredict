#!/usr/bin/env python3
"""
STEP 4a of 4 (Canada) -- Merge the three fundamentals sources into one file.

PRECEDENCE, AND WHY
  1. data_annual_sec.csv   FY rows, SEC XBRL, as-filed from the 40-F. Authoritative.
  2. data_annual_yf.csv    FY rows, yfinance. Fills years SEC has no filing for,
                           and every TSX-only company SEC never covers.
  3. data_quarterly_yf.csv Q rows, yfinance. The only quarterly source available.

  SEC wins on any fiscal year both cover, because it is the filed figure rather
  than a vendor's reconstruction. A one-line `source` column on every row records
  which feed it came from, so a reader can filter or audit.

FISCAL-YEAR LABELS ARE RECONCILED, NOT ASSUMED
  yfinance dates a fiscal year by its period end; so does the SEC path. But
  yfinance rounds to month end where the filing may say the 27th, so the two can
  disagree by a few weeks on the same year. Matching is on the FY label, and
  where labels collide the SEC row wins outright.

Reads:  data_annual_sec.csv, data_annual_yf.csv, data_quarterly_yf.csv
Writes: data_quarterly.csv  (Q and FY rows together -- the schema build_excel_ca
        expects, identical to the US pipeline)
"""

import os

import pandas as pd

from ca_io import read_csv as ca_read_csv

SEC_A, YF_A, YF_Q = "data_annual_sec.csv", "data_annual_yf.csv", "data_quarterly_yf.csv"
OUT = "data_quarterly.csv"

COLUMNS = ["ticker", "company", "period_end", "fiscal_quarter", "currency",
           "revenue", "gross_profit", "ebit", "ebitda", "net_income", "eps_diluted",
           "gross_margin_pct", "ebit_margin_pct", "ebitda_margin_pct",
           "net_margin_pct", "period_start", "period_type", "q4_derived", "source"]


def load(path):
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return pd.DataFrame(columns=COLUMNS)
    d = ca_read_csv(path)
    for c in COLUMNS:
        if c not in d.columns:
            d[c] = None
    return d[COLUMNS]


def main():
    sec_a, yf_a, yf_q = load(SEC_A), load(YF_A), load(YF_Q)
    print(f"[merge] SEC annual {len(sec_a)} rows / {sec_a.ticker.nunique()} tickers")
    print(f"[merge] yf  annual {len(yf_a)} rows / {yf_a.ticker.nunique()} tickers")
    print(f"[merge] yf  quarters {len(yf_q)} rows / {yf_q.ticker.nunique()} tickers")

    # annual: SEC first, then yfinance only for (ticker, FY) pairs SEC lacks
    have = set(zip(sec_a["ticker"], sec_a["fiscal_quarter"]))
    yf_extra = yf_a[~yf_a.apply(
        lambda r: (r["ticker"], r["fiscal_quarter"]) in have, axis=1)]
    print(f"[merge] yf annual rows kept (SEC had no filing): {len(yf_extra)}")

    out = pd.concat([sec_a, yf_extra, yf_q], ignore_index=True)
    # a company cannot have two rows for one period
    out = out.drop_duplicates(["ticker", "period_end", "period_type"], keep="first")

    # Nor two rows for one fiscal-year LABEL. Denison Mines files annual periods
    # ending both 30 November and 31 December, the November set being empty
    # stubs, which produced two FY2022 rows. Rank by how much data each row
    # carries and keep the fullest, so a stub can never displace real figures.
    measures = ["revenue", "gross_profit", "ebit", "ebitda", "net_income", "eps_diluted"]
    filled = out[measures].apply(pd.to_numeric, errors="coerce")
    out["_filled"] = (filled.notna() & (filled != 0)).sum(axis=1)
    out = (out.sort_values(["ticker", "fiscal_quarter", "_filled"])
              .drop_duplicates(["ticker", "fiscal_quarter", "period_type"], keep="last")
              .drop(columns="_filled"))

    out = out.sort_values(["ticker", "period_type", "period_end"])
    out.to_csv(OUT, index=False)

    print()
    print(f"[merge] wrote {OUT}: {len(out)} rows, {out.ticker.nunique()} tickers")
    print("[merge] by source:")
    for s, n in out["source"].value_counts().items():
        print(f"[merge]   {s:<22} {n}")
    print("[merge] by period type:", out["period_type"].value_counts().to_dict())


if __name__ == "__main__":
    main()
