#!/usr/bin/env python3
"""
STEP 2b of 3 -- Monthly prices for the whole universe, and a valuation snapshot.

WHY NOT fetch_financials.py
  That script calls yfinance once per ticker, including `.info`, which is a
  separate API request each time. Yahoo rate-limits hard: on the full 3,727
  ticker universe it died after roughly 500 names with
  "Too Many Requests", and every subsequent call failed instantly.

  yf.download() takes a LIST of tickers and returns them in one request. At 150
  per batch the whole universe is ~25 requests instead of 3,727.

  Market cap and trailing P/E are then computed rather than fetched:
    market_cap_est   = monthly close x shares outstanding (from SEC, data_shares.csv)
    pe_trailing_est  = monthly close / trailing-4-quarter diluted EPS (from SEC)
  Both inputs are as-filed SEC data, so no extra Yahoo calls are needed and the
  share count is a filed figure rather than a vendor estimate.

Reads:  universe.csv, data_shares.csv, data_quarterly.csv
Writes: data_monthly.csv, data_snapshot.csv, _px_done.txt (resumable)

Usage:
  python fetch_prices.py                 # resume
  python fetch_prices.py --restart
  python fetch_prices.py --batch 150
"""

import argparse
import os
import sys
import time
import warnings

import pandas as pd

warnings.filterwarnings("ignore")

try:
    import yfinance as yf
except ImportError:
    sys.exit("yfinance is not installed.  pip install yfinance --break-system-packages")

YEARS = 5
M_FILE, S_FILE, DONE_FILE = "data_monthly.csv", "data_snapshot.csv", "_px_done.txt"


def ttm_eps_series(qdf):
    """Trailing-four-quarter diluted EPS by period end, per ticker."""
    out = {}
    if qdf is None or qdf.empty:
        return out
    d = qdf.dropna(subset=["eps_diluted"]).sort_values(["ticker", "period_end"])
    for tk, g in d.groupby("ticker"):
        if len(g) < 4:
            continue
        ttm = g["eps_diluted"].rolling(4).sum()
        out[tk] = [(p, v) for p, v in zip(g["period_end"], ttm) if pd.notna(v)]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=150)
    ap.add_argument("--restart", action="store_true")
    ap.add_argument("--universe", default="universe.csv")
    ap.add_argument("--pause", type=float, default=1.0, help="seconds between batches")
    args = ap.parse_args()

    if args.restart:
        for f in (M_FILE, S_FILE, DONE_FILE):
            if os.path.exists(f):
                try:
                    os.remove(f)
                except OSError:
                    open(f, "w").close()
        print("[px] checkpoints cleared")

    uni = pd.read_csv(args.universe, dtype={"cik": str})
    shares = {}
    if os.path.exists("data_shares.csv"):
        sh = pd.read_csv("data_shares.csv").dropna(subset=["shares_outstanding"])
        shares = dict(zip(sh["ticker"], sh["shares_outstanding"]))
        print(f"[px] {len(shares)} share counts loaded from SEC")

    eps = {}
    if os.path.exists("data_quarterly.csv"):
        q = pd.read_csv("data_quarterly.csv")
        q["period_end"] = pd.to_datetime(q["period_end"], errors="coerce")
        eps = ttm_eps_series(q[q.get("period_type", "Q") == "Q"])
        print(f"[px] TTM EPS available for {len(eps)} tickers")

    done = set()
    if os.path.exists(DONE_FILE):
        done = {l.strip() for l in open(DONE_FILE) if l.strip()}
    todo = [t for t in uni["ticker"] if t not in done]
    meta = uni.set_index("ticker")
    print(f"[px] {len(todo)} tickers to fetch in batches of {args.batch}\n")
    if not todo:
        print("[px] nothing to do")
        return

    t0 = time.time()
    for i in range(0, len(todo), args.batch):
        chunk = todo[i:i + args.batch]
        try:
            df = yf.download(chunk, period=f"{YEARS}y", interval="1mo",
                             auto_adjust=False, progress=False, threads=True,
                             group_by="column")
        except Exception as e:                                  # noqa: BLE001
            print(f"[px] batch failed: {str(e)[:70]}")
            break
        if df is None or df.empty:
            print("[px] empty batch -- likely rate limited, stopping")
            break

        m_rows, s_rows = [], []
        for tk in chunk:
            try:
                close = df["Close"][tk] if len(chunk) > 1 else df["Close"]
            except (KeyError, TypeError):
                continue
            close = close.dropna()
            if close.empty:
                continue
            try:
                adj = df["Adj Close"][tk] if len(chunk) > 1 else df["Adj Close"]
                vol = df["Volume"][tk] if len(chunk) > 1 else df["Volume"]
            except (KeyError, TypeError):
                adj = vol = None

            n_sh = shares.get(tk)
            series = eps.get(tk, [])

            def eps_asof(ts):
                prior = [v for p, v in series if p <= ts]
                return prior[-1] if prior else None

            for ts, c in close.items():
                ts = pd.Timestamp(ts).tz_localize(None)
                e = eps_asof(ts)
                m_rows.append({
                    "ticker": tk,
                    "company": meta.at[tk, "company"] if tk in meta.index else tk,
                    "month": ts.strftime("%Y-%m"),
                    "date": ts.date(),
                    "close": float(c),
                    "adj_close": float(adj.get(ts)) if adj is not None and pd.notna(adj.get(ts)) else None,
                    "volume": float(vol.get(ts)) if vol is not None and pd.notna(vol.get(ts)) else None,
                    "market_cap_est": round(float(c) * n_sh, 0) if n_sh else None,
                    "pe_trailing_est": round(float(c) / e, 3) if e and e > 0 else None,
                })

            last_close = float(close.iloc[-1])
            last_eps = eps_asof(pd.Timestamp(close.index[-1]).tz_localize(None))
            s_rows.append({
                "ticker": tk,
                "company": meta.at[tk, "company"] if tk in meta.index else tk,
                "sector": meta.at[tk, "sector"] if tk in meta.index else "",
                "in_sp500": bool(meta.at[tk, "in_sp500"]) if tk in meta.index else False,
                "in_nasdaq": bool(meta.at[tk, "in_nasdaq"]) if tk in meta.index else False,
                "in_fortune500": bool(meta.at[tk, "in_fortune500"]) if tk in meta.index else False,
                "price": last_close,
                "shares_outstanding": n_sh,
                "market_cap": round(last_close * n_sh, 0) if n_sh else None,
                "eps_trailing": round(last_eps, 4) if last_eps else None,
                "pe_trailing": round(last_close / last_eps, 3) if last_eps and last_eps > 0 else None,
                "price_asof": str(pd.Timestamp(close.index[-1]).date()),
            })

        if m_rows:
            pd.DataFrame(m_rows).to_csv(
                M_FILE, mode="a", index=False,
                header=not os.path.exists(M_FILE) or os.path.getsize(M_FILE) == 0)
        if s_rows:
            pd.DataFrame(s_rows).to_csv(
                S_FILE, mode="a", index=False,
                header=not os.path.exists(S_FILE) or os.path.getsize(S_FILE) == 0)
        with open(DONE_FILE, "a") as fh:
            fh.write("\n".join(chunk) + "\n")

        n = i + len(chunk)
        print(f"[px] {n}/{len(todo)}  +{len(s_rows)} tickers  "
              f"({time.time() - t0:.0f}s elapsed)")
        time.sleep(args.pause)

    print(f"\n[px] done in {(time.time() - t0) / 60:.1f}m")


if __name__ == "__main__":
    main()
