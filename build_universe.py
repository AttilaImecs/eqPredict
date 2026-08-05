#!/usr/bin/env python3
"""
STEP 1 of 3 -- Build the company universe.

Produces: universe.csv  (ticker, company, source flags, sector, cik)

Universe = union of:
  * S&P 500 constituents        (Wikipedia)
  * All NASDAQ-listed companies (Nasdaq Trader symbol directory)
  * Fortune 500                 (Wikipedia list -> mapped to tickers via SEC)

Requires network access to:
  en.wikipedia.org, ftp.nasdaqtrader.com, www.sec.gov
"""

import io
import re
import sys
import time

import pandas as pd
import requests

UA = {"User-Agent": "Research Data Collection (attila.imecs@gmail.com)"}
TIMEOUT = 45


def _get(url, **kw):
    r = requests.get(url, headers=UA, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r


# --------------------------------------------------------------------------
# S&P 500
# --------------------------------------------------------------------------
def sp500():
    print("[universe] S&P 500 from Wikipedia ...")
    html = _get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies").text
    tables = pd.read_html(io.StringIO(html))
    df = None
    for t in tables:
        cols = {str(c).strip().lower() for c in t.columns}
        if "symbol" in cols and ("security" in cols or "company" in cols):
            df = t
            break
    if df is None:
        raise RuntimeError("Could not locate the S&P 500 constituents table")

    df.columns = [str(c).strip() for c in df.columns]
    name_col = "Security" if "Security" in df.columns else "Company"
    out = pd.DataFrame(
        {
            "ticker": df["Symbol"].astype(str).str.strip().str.replace(".", "-", regex=False),
            "company": df[name_col].astype(str).str.strip(),
            "sector": df.get("GICS Sector", pd.Series([""] * len(df))).astype(str).str.strip(),
            "cik": df.get("CIK", pd.Series([""] * len(df))).astype(str).str.strip(),
        }
    )
    out["in_sp500"] = True
    print(f"[universe]   -> {len(out)} S&P 500 constituents")
    return out


# --------------------------------------------------------------------------
# NASDAQ-listed
# --------------------------------------------------------------------------
def nasdaq_listed():
    """
    Nasdaq Trader publishes a pipe-delimited directory of every Nasdaq-listed
    security. We keep common stock only: drop test issues, ETFs, and anything
    whose symbol carries a suffix (warrants, units, preferreds).
    """
    print("[universe] Nasdaq-listed securities from Nasdaq Trader ...")
    urls = [
        "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
        "ftp://ftp.nasdaqtrader.com/SymbolDirectory/nasdaqlisted.txt",
    ]
    txt = None
    for u in urls:
        if u.startswith("ftp://"):
            continue  # requests can't do ftp; https mirror is the primary
        try:
            txt = _get(u).text
            break
        except Exception as e:  # noqa: BLE001
            print(f"[universe]   ! {u} failed: {e}")
    if txt is None:
        print("[universe]   ! Nasdaq directory unavailable -- skipping this source")
        return pd.DataFrame(columns=["ticker", "company", "sector", "cik", "in_nasdaq"])

    lines = [ln for ln in txt.splitlines() if ln and not ln.startswith("File Creation Time")]
    df = pd.read_csv(io.StringIO("\n".join(lines)), sep="|", dtype=str).fillna("")
    df.columns = [c.strip() for c in df.columns]

    if "Test Issue" in df.columns:
        df = df[df["Test Issue"].str.upper() != "Y"]
    if "ETF" in df.columns:
        df = df[df["ETF"].str.upper() != "Y"]

    df = df[df["Symbol"].str.fullmatch(r"[A-Z]{1,5}")]

    # The Symbol regex alone does NOT exclude warrants/units/rights/preferreds --
    # those are ordinary 5-letter symbols. Filter on the Security Name suffix,
    # which is what Nasdaq actually uses to label them.
    _NOT_COMMON = (
        r"-\s*(warrants?|units?|rights?|"
        r"(class\s+\w+\s+)?(depositary|preferred|preference)\b.*|"
        r".*\b(note|notes|debenture|debentures|subordinated)\b.*)\s*$"
    )
    before = len(df)
    df = df[~df["Security Name"].str.contains(_NOT_COMMON, case=False, regex=True, na=False)]
    df = df[~df["Security Name"].str.contains(
        r"\b(depositary shares?|preferred stock|% (note|debenture))\b",
        case=False, regex=True, na=False)]
    print(f"[universe]   -> dropped {before - len(df)} warrants/units/rights/preferreds")

    out = pd.DataFrame(
        {
            "ticker": df["Symbol"].str.strip(),
            "company": df["Security Name"].str.split(" - ").str[0].str.strip(),
            "sector": "",
            "cik": "",
        }
    )
    out["in_nasdaq"] = True
    print(f"[universe]   -> {len(out)} Nasdaq common stocks")
    return out


# --------------------------------------------------------------------------
# Fortune 500  (names -> tickers via the SEC company_tickers.json map)
# --------------------------------------------------------------------------
_SUFFIXES = re.compile(
    r"\b(inc|incorporated|corp|corporation|co|company|companies|holdings?|"
    r"group|plc|ltd|limited|llc|lp|the|&|and|international|worldwide)\b",
    re.I,
)


def _norm(name: str) -> str:
    s = re.sub(r"[^a-z0-9 ]", " ", str(name).lower())
    s = _SUFFIXES.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


# Primary: scraped full Fortune 500 (2023 edition) -- the only free full-500
# list still reachable. Wikipedia's Fortune_500 page was cut down to a top-20
# summary table and no longer works as a source (checked 2026-08).
F500_CSV = (
    "https://raw.githubusercontent.com/EatMoreOranges/Fortune-500-Dataset/"
    "main/data/2023-fortune-500-data.csv"
)
# Secondary: current top-100 by revenue, to catch recent entrants the 2023
# list would miss.
F500_TOP100_WIKI = (
    "https://en.wikipedia.org/wiki/"
    "List_of_largest_companies_in_the_United_States_by_revenue"
)


def _f500_names():
    """Return (names, source_note). Falls back through several sources."""
    names, notes = [], []

    try:
        df = pd.read_csv(io.StringIO(_get(F500_CSV).text))
        got = df["Company"].astype(str).str.strip().tolist()
        names += got
        notes.append(f"Fortune 500 2023 list ({len(got)})")
        print(f"[universe]   -> {len(got)} names from Fortune 500 2023 dataset")
    except Exception as e:  # noqa: BLE001
        print(f"[universe]   ! Fortune 500 2023 dataset failed: {e}")

    try:
        tables = pd.read_html(io.StringIO(_get(F500_TOP100_WIKI).text))
        for t in tables:
            if "Name" in t.columns and len(t) >= 50:
                got = t["Name"].astype(str).str.strip().tolist()
                names += got
                notes.append(f"Wikipedia top-{len(got)} by revenue")
                print(f"[universe]   -> {len(got)} names from Wikipedia top-100")
                break
    except Exception as e:  # noqa: BLE001
        print(f"[universe]   ! Wikipedia top-100 failed: {e}")

    seen, uniq = set(), []
    for n in names:
        if n and n.lower() not in seen:
            seen.add(n.lower())
            uniq.append(n)
    return uniq, "; ".join(notes)


def fortune500():
    print("[universe] Fortune 500 ...")
    names, note = _f500_names()
    if not names:
        print("[universe]   ! No Fortune 500 source available -- skipping")
        return pd.DataFrame(columns=["ticker", "company", "sector", "cik", "in_fortune500"])
    print(f"[universe]   -> {len(names)} unique names from: {note}")
    print("[universe]   -> mapping names to tickers via SEC ...")
    sec = _get("https://www.sec.gov/files/company_tickers.json").json()
    lookup = {}
    for rec in sec.values():
        lookup.setdefault(_norm(rec["title"]), (rec["ticker"], rec["title"], str(rec["cik_str"]).zfill(10)))

    rows, unmatched = [], []
    for n in names:
        hit = lookup.get(_norm(n))
        if hit:
            rows.append({"ticker": hit[0], "company": hit[1], "sector": "", "cik": hit[2]})
        else:
            unmatched.append(n)

    print(f"[universe]   -> matched {len(rows)}, unmatched {len(unmatched)} (private or name mismatch)")
    if unmatched:
        pd.Series(unmatched, name="company").to_csv("fortune500_unmatched.csv", index=False)
        print("[universe]   -> unmatched names written to fortune500_unmatched.csv")

    out = pd.DataFrame(rows)
    if not out.empty:
        out["in_fortune500"] = True
    return out


# --------------------------------------------------------------------------
# CIK attachment -- required by the SEC XBRL fundamentals fetch.
# company_tickers.json maps ticker -> CIK directly, so this is exact (no fuzzy
# name matching) for every ticker SEC knows about.
# --------------------------------------------------------------------------
def attach_ciks(df):
    print("[universe] attaching CIKs from SEC ticker map ...")
    try:
        sec = _get("https://www.sec.gov/files/company_tickers.json").json()
    except Exception as e:  # noqa: BLE001
        print(f"[universe]   ! SEC ticker map failed: {e} -- CIKs left blank")
        return df

    by_ticker = {
        str(r["ticker"]).upper().strip(): str(r["cik_str"]).zfill(10)
        for r in sec.values()
    }
    # Ticker classes: SEC uses BRK-B, Nasdaq/Yahoo use BRK-B or BRK.B.
    filled = df["ticker"].str.upper().map(by_ticker)
    alt = df["ticker"].str.upper().str.replace(".", "-", regex=False).map(by_ticker)
    df["cik"] = filled.fillna(alt).fillna(df["cik"]).fillna("")

    have = (df["cik"].astype(str).str.strip() != "").sum()
    print(f"[universe]   -> CIK found for {have}/{len(df)} tickers "
          f"({len(df) - have} missing: foreign/ETF/recent listings)")
    return df


# --------------------------------------------------------------------------
def main():
    frames = []
    for fn in (sp500, nasdaq_listed, fortune500):
        try:
            f = fn()
            if not f.empty:
                frames.append(f)
        except Exception as e:  # noqa: BLE001
            print(f"[universe] ! {fn.__name__} failed hard: {e}")

    if not frames:
        sys.exit("[universe] FATAL: no sources succeeded -- check network access")

    df = pd.concat(frames, ignore_index=True)
    for flag in ("in_sp500", "in_nasdaq", "in_fortune500"):
        if flag not in df.columns:
            df[flag] = False
        df[flag] = df[flag].fillna(False).astype(bool)

    agg = {
        "company": "first",
        "sector": lambda s: next((x for x in s if x), ""),
        "cik": lambda s: next((x for x in s if x), ""),
        "in_sp500": "max",
        "in_nasdaq": "max",
        "in_fortune500": "max",
    }
    df = df.groupby("ticker", as_index=False).agg(agg)
    df = df[df["ticker"].str.fullmatch(r"[A-Z\-\.]{1,6}")].sort_values("ticker")

    df = attach_ciks(df)

    df.to_csv("universe.csv", index=False)
    print(f"\n[universe] DONE -- {len(df)} unique tickers -> universe.csv")
    print(
        f"[universe]   S&P 500: {int(df.in_sp500.sum())} | "
        f"Nasdaq: {int(df.in_nasdaq.sum())} | "
        f"Fortune 500: {int(df.in_fortune500.sum())}"
    )


if __name__ == "__main__":
    t0 = time.time()
    main()
    print(f"[universe] elapsed {time.time() - t0:.1f}s")
