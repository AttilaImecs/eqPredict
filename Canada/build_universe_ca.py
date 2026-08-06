#!/usr/bin/env python3
"""
STEP 1 of 4 (Canada) -- Build the S&P/TSX Composite ticker universe.

Writes universe_ca.csv:
  ticker        TSX symbol as listed (e.g. RY, CNQ, BAM.A)
  yahoo         Yahoo symbol, i.e. ticker with '.TO' and '.' -> '-' (BAM.A -> BAM-A.TO)
  company, sector, industry
  cik           SEC CIK for cross-listed filers, else blank
  cross_listed  True when a US SEC registration was found

WHY THE CIK MATTERS -- AND WHY IT IS MATCHED BY NAME
  Roughly half the Composite is cross-listed in the US under the MJDS regime and
  files a 40-F. Those filings carry XBRL, which is as-filed data and better than
  any vendor feed.

  Matching on TICKER alone is unsafe across borders: SEC's `SHOP` is Tremont
  Mortgage Trust, not Shopify. So the match is made on NORMALISED COMPANY NAME,
  with a ticker match accepted only when the names also agree.
"""

import io
import re
import sys
import difflib

import pandas as pd
import requests

UA = {"User-Agent": "StockPipelineDataCollector/1.0"}
WIKI = "https://en.wikipedia.org/wiki/S%26P/TSX_Composite_Index"
SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"
OUT = "universe_ca.csv"

SUFFIXES = r"\b(inc|ltd|limited|corp|corporation|company|co|plc|the|holdings|group|" \
           r"sa|nv|trust|reit|lp|incorporated|enterprises|international)\b"


def norm(s):
    s = re.sub(r"[’']", "", str(s).lower())
    s = re.sub(SUFFIXES, " ", s)
    return re.sub(r"[^a-z0-9]", "", s)


def yahoo_symbol(tsx_ticker):
    """TSX symbol -> Yahoo symbol. Yahoo writes share classes with a hyphen."""
    return tsx_ticker.strip().upper().replace(".", "-") + ".TO"


def fetch_constituents():
    r = requests.get(WIKI, headers=UA, timeout=45)
    r.raise_for_status()
    # keep_default_na=False is essential, not cosmetic: National Bank of Canada
    # trades as "NA", which pandas otherwise parses as NaN and silently drops --
    # losing one of the Big Six banks from the index.
    tables = pd.read_html(io.StringIO(r.text), keep_default_na=False)
    # the constituent table is the only one with a Ticker column and ~200 rows
    for t in tables:
        cols = [str(c).lower() for c in t.columns]
        if any(c.startswith("ticker") for c in cols) and len(t) > 100:
            t = t.copy()
            t.columns = ["ticker", "company", "sector", "industry"][:len(t.columns)]
            return t
    sys.exit("Could not find the constituent table on the Wikipedia page")


def attach_ciks(uni):
    j = requests.get(SEC_TICKERS, headers=UA, timeout=45).json()
    recs = [(v["ticker"].upper(), v["cik_str"], v["title"]) for v in j.values()]
    by_ticker, by_name = {}, {}
    for tk, cik, title in recs:
        by_ticker.setdefault(tk, (cik, title))
        by_name.setdefault(norm(title), (cik, title))

    ciks, how = [], []
    for r in uni.itertuples():
        tk = str(r.ticker).upper()
        nm = norm(r.company)
        hit = by_name.get(nm)
        if hit:
            ciks.append(f"{hit[0]:010d}"); how.append("name")
            continue
        hit = by_ticker.get(tk)
        # ticker match only counts if the names corroborate it
        if hit and difflib.SequenceMatcher(None, norm(hit[1]), nm).ratio() > 0.60:
            ciks.append(f"{hit[0]:010d}"); how.append("ticker+name")
            continue
        ciks.append(""); how.append("")
    uni["cik"] = ciks
    uni["cik_match"] = how
    uni["cross_listed"] = uni["cik"] != ""
    return uni


def main():
    uni = fetch_constituents()
    uni["ticker"] = uni["ticker"].astype(str).str.strip().str.upper()
    # keep_default_na=False leaves blank cells as "" rather than NaN, so an
    # empty row in the Wikipedia table survives as a ticker-less record and
    # breaks every downstream join. Require a plausible TSX symbol.
    before = len(uni)
    uni = uni[uni["ticker"].str.fullmatch(r"[A-Z0-9]{1,6}(\.[A-Z]{1,3})?")]
    uni = uni[uni["company"].astype(str).str.strip() != ""]
    if before != len(uni):
        print(f"[universe] dropped {before - len(uni)} row(s) with no valid ticker")
    uni = uni.drop_duplicates("ticker")
    uni["yahoo"] = uni["ticker"].map(yahoo_symbol)
    uni = attach_ciks(uni)
    uni = uni[["ticker", "yahoo", "company", "sector", "industry", "cik",
               "cik_match", "cross_listed"]]
    uni.to_csv(OUT, index=False)

    print(f"[universe] {len(uni)} S&P/TSX Composite constituents -> {OUT}")
    print(f"[universe] cross-listed with a SEC CIK: {uni['cross_listed'].sum()}")
    print("[universe] by sector:")
    for sec, n in uni["sector"].value_counts().items():
        print(f"[universe]   {sec:<26} {n}")


if __name__ == "__main__":
    main()
