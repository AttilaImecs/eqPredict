#!/usr/bin/env python3
"""
PRE-FLIGHT CHECK -- validate a universe file before spending an hour fetching.

Run this before the fetch steps. It catches the failure modes that are cheap to
find here and expensive to find later:

  * a missing or empty universe file
  * duplicate tickers, which silently double-count downstream
  * tickers pandas will destroy on read (see NULL_LIKE below)
  * malformed symbols
  * CIK coverage, reported but NOT treated as fatal

Optionally (--data DIR) it also reports how many universe tickers actually
appear in the fetched CSVs, which is the quickest way to spot a fetch that
stopped early.

Works for both pipelines:
    python validate_universe.py                       # universe.csv
    python validate_universe.py Canada/universe_ca.csv --data Canada

EXIT CODES
    0  fine, or warnings only
    1  fatal -- do not start the fetch

A blank CIK is NOT fatal. Ten US tickers and 112 TSX constituents legitimately
have none: they are not SEC registrants. Treating that as an error would block
every run.
"""

import argparse
import os
import sys

import pandas as pd

# Strings pandas turns into NaN by default. A ticker that is one of these is
# destroyed on every read unless keep_default_na=False is used -- National Bank
# of Canada trades as "NA" and vanished from an entire pipeline run this way.
NULL_LIKE = {
    "NA", "N/A", "NAN", "NULL", "NONE", "NIL", "", "-1.#IND", "1.#QNAN",
    "#N/A", "#N/A N/A", "#NA", "<NA>",
}

TICKER_RE = r"^[A-Z0-9]{1,6}([.\-][A-Z]{1,3})?$"


def read_universe(path):
    """Read without letting pandas null-coerce a real ticker."""
    return pd.read_csv(path, dtype=str, keep_default_na=False, na_values=[""])


def col(df, *names):
    """Find a column case-insensitively; the two pipelines differ in casing."""
    lower = {c.lower(): c for c in df.columns}
    for n in names:
        if n.lower() in lower:
            return lower[n.lower()]
    return None


def validate(path, data_dir=None):
    problems, warnings = [], []

    if not os.path.exists(path):
        print(f"FATAL: {path} not found")
        return 1
    if os.path.getsize(path) == 0:
        print(f"FATAL: {path} is empty")
        return 1

    df = read_universe(path)
    tcol = col(df, "ticker", "symbol")
    ccol = col(df, "cik")
    if tcol is None:
        print(f"FATAL: no ticker column in {path}; found {list(df.columns)}")
        return 1

    print(f"--- validating {path} ---")
    print(f"rows: {len(df)}   columns: {', '.join(df.columns)}")

    if len(df) == 0:
        problems.append("universe has no rows")

    tickers = df[tcol].fillna("").str.strip()

    # 1. tickers pandas would eat on a later read
    at_risk = sorted(t for t in tickers if t.upper() in NULL_LIKE and t != "")
    if at_risk:
        warnings.append(
            f"{len(at_risk)} ticker(s) pandas treats as null by default: {at_risk}. "
            "Every read of this file MUST use keep_default_na=False "
            "(the Canada pipeline does this via ca_io.read_csv)."
        )

    # 2. genuinely blank tickers -- these break joins outright
    blank = int((tickers == "").sum())
    if blank:
        problems.append(f"{blank} row(s) have no ticker at all")

    # 3. duplicates
    dupes = sorted(tickers[tickers.duplicated() & (tickers != "")].unique())
    if dupes:
        problems.append(f"{len(dupes)} duplicate ticker(s): {dupes[:10]}")

    # 4. malformed symbols
    bad = sorted(t for t in tickers if t and not pd.Series([t]).str.match(TICKER_RE).iloc[0])
    if bad:
        warnings.append(f"{len(bad)} unusual symbol(s): {bad[:10]}")

    # 5. CIK coverage -- informational only
    if ccol is not None:
        cik = df[ccol].fillna("").str.strip()
        have = int((cik != "").sum())
        malformed = sorted(c for c in cik if c and not c.isdigit())
        print(f"CIK present: {have}/{len(df)} ({have/max(len(df),1)*100:.0f}%) "
              f"-- blanks are expected for non-SEC filers")
        if malformed:
            warnings.append(f"{len(malformed)} non-numeric CIK(s): {malformed[:5]}")
    else:
        print("no CIK column (fine for a prices-only universe)")

    # 6. optional: did the fetches actually cover the universe?
    if data_dir:
        print(f"\n--- data coverage in {data_dir} ---")
        universe_set = set(t for t in tickers if t)
        for fname in ("data_quarterly.csv", "data_monthly.csv", "data_snapshot.csv"):
            fpath = os.path.join(data_dir, fname)
            if not os.path.exists(fpath):
                print(f"  {fname:<22} not found (skipped)")
                continue
            d = pd.read_csv(fpath, dtype={"ticker": str},
                            keep_default_na=False, na_values=[""], usecols=["ticker"])
            present = set(d["ticker"].dropna())
            missing = universe_set - present
            pct = (len(universe_set) - len(missing)) / max(len(universe_set), 1) * 100
            print(f"  {fname:<22} {len(present):>5} tickers  "
                  f"covers {pct:5.1f}% of universe"
                  + (f"  ({len(missing)} missing)" if missing else ""))

    print()
    for w in warnings:
        print(f"WARNING: {w}")
    for p in problems:
        print(f"PROBLEM: {p}")

    if problems:
        print(f"\nFAILED -- {len(problems)} problem(s). Fix before fetching.")
        return 1
    print(f"OK -- {len(df)} tickers validated"
          + (f", {len(warnings)} warning(s)" if warnings else ""))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("universe", nargs="?", default="universe.csv")
    ap.add_argument("--data", default=None,
                    help="directory holding the fetched CSVs, to check coverage")
    args = ap.parse_args()
    sys.exit(validate(args.universe, args.data))


if __name__ == "__main__":
    main()
