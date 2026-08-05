#!/usr/bin/env python3
"""
STEP 2 of 3 -- Pull 5 years of financial data for every ticker in universe.csv.

Writes three checkpoint files, appended batch-by-batch so the run is RESUMABLE.
Kill it and re-run at any time; it picks up where it stopped.

  data_quarterly.csv  quarterly income-statement facts (5y = up to 20 quarters)
  data_monthly.csv    monthly price + market cap + trailing P/E (5y = 60 months)
  data_snapshot.csv   one current row per ticker (forward P/E, div yield, etc.)
  _done.txt           tickers already processed

Usage:
  python fetch_financials.py                # all tickers in universe.csv
  python fetch_financials.py --limit 50     # smoke test on the first 50
  python fetch_financials.py --workers 8    # parallelism (default 6)
  python fetch_financials.py --restart      # wipe checkpoints and start over
"""

import argparse
import os
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import pandas as pd

try:
    import yfinance as yf
except ImportError:
    sys.exit("yfinance is not installed.  Run:  pip install yfinance --break-system-packages")

YEARS = 5
# Quarterly fundamentals go to their OWN file. data_quarterly.csv belongs to
# fetch_sec_fundamentals.py; this yfinance version is only ~5-7 quarters deep and
# exists to backfill the most recent quarter when SEC's XBRL API has not yet
# published a 10-Q that is already filed on EDGAR. See backfill_quarters.py.
# fetch_prices.py now owns data_monthly.csv and data_snapshot.csv -- it gets the
# same data in ~25 batched requests instead of one per ticker, which is the only
# way to cover 3,700 names without Yahoo rate-limiting the run to death. This
# script is kept solely for its QUARTERLY figures, which have no batch endpoint
# and which backfill_quarters.py uses to patch SEC's publishing lag. Its price
# outputs are written to _yf names so they cannot clobber the real ones.
Q_FILE, M_FILE, S_FILE, DONE_FILE = (
    "data_quarterly_yf.csv",
    "data_monthly_yf.csv",
    "data_snapshot_yf.csv",
    "_done.txt",
)

_lock = threading.Lock()
_counter = {"n": 0, "ok": 0, "fail": 0}


# --------------------------------------------------------------------------
# Field lookup helpers -- yfinance row labels vary by company and API version
# --------------------------------------------------------------------------
def _row(df, *candidates):
    """Return the first matching row from a yfinance statement DataFrame."""
    if df is None or df.empty:
        return None
    idx = {str(i).strip().lower(): i for i in df.index}
    for c in candidates:
        key = c.strip().lower()
        if key in idx:
            return df.loc[idx[key]]
    return None


def _num(v):
    try:
        f = float(v)
        return None if pd.isna(f) else f
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Per-ticker extraction
# --------------------------------------------------------------------------
def fetch_one(ticker: str, meta: dict):
    t = yf.Ticker(ticker)
    q_rows, m_rows = [], []

    # ---- info snapshot -------------------------------------------------
    try:
        info = t.info or {}
    except Exception:  # noqa: BLE001
        info = {}

    shares = _num(info.get("sharesOutstanding")) or _num(info.get("impliedSharesOutstanding"))

    snap = {
        "ticker": ticker,
        "company": meta.get("company", info.get("longName", "")),
        "sector": meta.get("sector") or info.get("sector", ""),
        "industry": info.get("industry", ""),
        "exchange": info.get("exchange", ""),
        "currency": info.get("financialCurrency", info.get("currency", "")),
        "in_sp500": meta.get("in_sp500", False),
        "in_nasdaq": meta.get("in_nasdaq", False),
        "in_fortune500": meta.get("in_fortune500", False),
        "market_cap": _num(info.get("marketCap")),
        "shares_outstanding": shares,
        "price": _num(info.get("currentPrice")) or _num(info.get("regularMarketPrice")),
        "pe_trailing": _num(info.get("trailingPE")),
        "pe_forward": _num(info.get("forwardPE")),
        "eps_trailing": _num(info.get("trailingEps")),
        "eps_forward": _num(info.get("forwardEps")),
        "dividend_yield": _num(info.get("dividendYield")),
        "dividend_rate": _num(info.get("dividendRate")),
        "payout_ratio": _num(info.get("payoutRatio")),
        "beta": _num(info.get("beta")),
        "ev_to_ebitda": _num(info.get("enterpriseToEbitda")),
        "price_to_book": _num(info.get("priceToBook")),
        "profit_margin_pct": (_num(info.get("profitMargins")) or 0) * 100 or None,
        "operating_margin_pct": (_num(info.get("operatingMargins")) or 0) * 100 or None,
        "retrieved_at": datetime.utcnow().strftime("%Y-%m-%d"),
    }

    # ---- quarterly income statement ------------------------------------
    try:
        inc = t.quarterly_income_stmt
    except Exception:  # noqa: BLE001
        inc = None

    revenue_row = _row(inc, "Total Revenue", "Operating Revenue", "Revenue")
    ebit_row = _row(inc, "EBIT", "Operating Income", "Total Operating Income As Reported")
    ebitda_row = _row(inc, "EBITDA", "Normalized EBITDA")
    net_row = _row(inc, "Net Income", "Net Income Common Stockholders",
                   "Net Income From Continuing Operation Net Minority Interest")
    gross_row = _row(inc, "Gross Profit")
    eps_row = _row(inc, "Diluted EPS", "Basic EPS")

    if inc is not None and not inc.empty:
        for col in inc.columns:
            period = pd.to_datetime(col, errors="coerce")
            if pd.isna(period):
                continue
            rev = _num(revenue_row.get(col)) if revenue_row is not None else None
            ebit = _num(ebit_row.get(col)) if ebit_row is not None else None
            ebitda = _num(ebitda_row.get(col)) if ebitda_row is not None else None
            net = _num(net_row.get(col)) if net_row is not None else None
            gross = _num(gross_row.get(col)) if gross_row is not None else None
            eps = _num(eps_row.get(col)) if eps_row is not None else None

            q_rows.append(
                {
                    "ticker": ticker,
                    "company": snap["company"],
                    "period_end": period.date(),
                    "fiscal_quarter": f"{period.year}-Q{(period.month - 1) // 3 + 1}",
                    "currency": snap["currency"],
                    "revenue": rev,
                    "gross_profit": gross,
                    "ebit": ebit,
                    "ebitda": ebitda,
                    "net_income": net,
                    "eps_diluted": eps,
                    "gross_margin_pct": round(gross / rev * 100, 3) if rev and gross is not None else None,
                    "ebit_margin_pct": round(ebit / rev * 100, 3) if rev and ebit is not None else None,
                    "ebitda_margin_pct": round(ebitda / rev * 100, 3) if rev and ebitda is not None else None,
                    "net_margin_pct": round(net / rev * 100, 3) if rev and net is not None else None,
                }
            )

    # ---- monthly prices -> market cap + trailing P/E --------------------
    try:
        hist = t.history(period=f"{YEARS}y", interval="1mo", auto_adjust=False)
    except Exception:  # noqa: BLE001
        hist = None

    # TTM EPS per month, derived from the quarterly EPS series above
    ttm_eps_by_date = {}
    if q_rows:
        qdf = (
            pd.DataFrame(q_rows)[["period_end", "eps_diluted"]]
            .dropna()
            .sort_values("period_end")
        )
        if len(qdf) >= 4:
            qdf["ttm"] = qdf["eps_diluted"].rolling(4).sum()
            ttm_eps_by_date = {
                pd.Timestamp(r.period_end): r.ttm
                for r in qdf.itertuples()
                if pd.notna(r.ttm)
            }

    def ttm_eps_asof(ts):
        prior = [d for d in ttm_eps_by_date if d <= ts]
        return ttm_eps_by_date[max(prior)] if prior else None

    if hist is not None and not hist.empty:
        for dt, r in hist.iterrows():
            close = _num(r.get("Close"))
            if close is None:
                continue
            ts = pd.Timestamp(dt).tz_localize(None)
            eps_ttm = ttm_eps_asof(ts)
            m_rows.append(
                {
                    "ticker": ticker,
                    "company": snap["company"],
                    "month": ts.strftime("%Y-%m"),
                    "date": ts.date(),
                    "open": _num(r.get("Open")),
                    "high": _num(r.get("High")),
                    "low": _num(r.get("Low")),
                    "close": close,
                    "adj_close": _num(r.get("Adj Close")),
                    "volume": _num(r.get("Volume")),
                    "market_cap_est": round(close * shares, 0) if shares else None,
                    "pe_trailing_est": round(close / eps_ttm, 3) if eps_ttm and eps_ttm > 0 else None,
                }
            )

    if not q_rows and not m_rows:
        raise RuntimeError("no data returned")

    return q_rows, m_rows, snap


# --------------------------------------------------------------------------
# Checkpointed append
# --------------------------------------------------------------------------
def append(path, rows):
    if not rows:
        return
    df = pd.DataFrame(rows)
    df.to_csv(path, mode="a", header=not os.path.exists(path), index=False)


def worker(ticker, meta, retries=3):
    last = None
    for attempt in range(retries):
        try:
            return fetch_one(ticker, meta)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep((2 ** attempt) + random.random())
    raise last


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="only process first N tickers")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--restart", action="store_true", help="wipe checkpoints first")
    ap.add_argument("--universe", default="universe.csv")
    args = ap.parse_args()

    if args.restart:
        for f in (Q_FILE, M_FILE, S_FILE, DONE_FILE):
            if os.path.exists(f):
                os.remove(f)
        print("[fetch] checkpoints cleared")

    if not os.path.exists(args.universe):
        sys.exit(f"{args.universe} not found -- run build_universe.py first")

    uni = pd.read_csv(args.universe).fillna("")
    if args.limit:
        uni = uni.head(args.limit)

    done = set()
    if os.path.exists(DONE_FILE):
        done = {ln.strip() for ln in open(DONE_FILE) if ln.strip()}
        print(f"[fetch] resuming -- {len(done)} tickers already done")

    todo = [r for r in uni.to_dict("records") if r["ticker"] not in done]
    total = len(todo)
    print(f"[fetch] {total} tickers to process with {args.workers} workers\n")
    if not total:
        print("[fetch] nothing to do")
        return

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(worker, r["ticker"], r): r["ticker"] for r in todo}
        for fut in as_completed(futures):
            tk = futures[fut]
            with _lock:
                _counter["n"] += 1
                n = _counter["n"]
                try:
                    q, m, s = fut.result()
                    append(Q_FILE, q)
                    append(M_FILE, m)
                    append(S_FILE, [s])
                    with open(DONE_FILE, "a") as fh:
                        fh.write(tk + "\n")
                    _counter["ok"] += 1
                    status = f"ok  {len(q):>2}q {len(m):>2}m"
                except Exception as e:  # noqa: BLE001
                    _counter["fail"] += 1
                    status = f"FAIL {str(e)[:45]}"
                    with open(DONE_FILE, "a") as fh:
                        fh.write(tk + "\n")  # don't retry forever on delisted names

                if n % 10 == 0 or n == total:
                    rate = n / max(time.time() - t0, 1)
                    eta = (total - n) / max(rate, 0.001) / 60
                    print(
                        f"[{n:>5}/{total}] {tk:<6} {status:<55} "
                        f"| {rate:.1f}/s ETA {eta:.0f}m"
                    )

    print(
        f"\n[fetch] DONE  ok={_counter['ok']}  failed={_counter['fail']}  "
        f"elapsed {(time.time() - t0) / 60:.1f}m"
    )
    print("[fetch] next:  python build_excel.py")


if __name__ == "__main__":
    main()
