#!/usr/bin/env python3
"""
STEP 3 of 4 (Canada) -- Quarterly fundamentals, annual fallback, and prices.

WHY YFINANCE CARRIES THE QUARTERLY SERIES HERE
  There is no free XBRL feed for SEDAR+, and SEC holds only ANNUAL data for
  Canadian issuers (see fetch_sec_ca.py). Yahoo covers TSX symbols with a `.TO`
  suffix and returns roughly 5-7 quarters plus 5 annual years, which is the
  deepest quarterly history obtainable without a paid vendor.

  Consequence, stated plainly: Canadian QUARTERLY history is ~6 quarters deep,
  not the 24 the US pipeline reaches. The ANNUAL columns are the reliable part
  and go back 5-6 years.

PRICES ARE BATCHED, FUNDAMENTALS ARE NOT
  yf.download() takes a list and returns every ticker in one request, which is
  what made the 3,727-name US run possible after Yahoo rate-limited the
  per-ticker approach. Quarterly statements have no batch endpoint, so those are
  fetched per ticker -- tolerable for a 220-name index, and the reason this
  design would NOT scale to thousands.

Reads:  universe_ca.csv, data_annual_sec.csv (optional, for share counts)
Writes: data_quarterly_yf.csv, data_annual_yf.csv, data_monthly.csv,
        data_snapshot.csv, _yf_ca_done.txt / _px_ca_done.txt (resumable)

Usage:
  python fetch_yf_ca.py --stage prices     # batched, fast
  python fetch_yf_ca.py --stage funds      # per ticker, slower
  python fetch_yf_ca.py --stage all --restart
"""

import argparse
import os
import sys
import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

from ca_io import read_csv as ca_read_csv

warnings.filterwarnings("ignore")

try:
    import yfinance as yf
except ImportError:
    sys.exit("yfinance is not installed.  pip install yfinance --break-system-packages")

YEARS = 5
Q_FILE = "data_quarterly_yf.csv"
A_FILE = "data_annual_yf.csv"
M_FILE = "data_monthly.csv"
S_FILE = "data_snapshot.csv"
FUND_DONE = "_yf_ca_done.txt"
PX_DONE = "_px_ca_done.txt"

_lock = threading.RLock()

# yfinance row labels vary by company and library version
REV = ("Total Revenue", "Operating Revenue", "Revenue")
COGS = ("Cost Of Revenue", "Cost Of Goods Sold")
GROSS = ("Gross Profit",)
EBIT = ("EBIT", "Operating Income", "Total Operating Income As Reported")
EBITDA = ("EBITDA", "Normalized EBITDA")
NET = ("Net Income", "Net Income Common Stockholders",
       "Net Income From Continuing Operation Net Minority Interest")
EPS = ("Diluted EPS", "Basic EPS")

COLUMNS = ["ticker", "company", "period_end", "fiscal_quarter", "currency",
           "revenue", "gross_profit", "ebit", "ebitda", "net_income", "eps_diluted",
           "gross_margin_pct", "ebit_margin_pct", "ebitda_margin_pct",
           "net_margin_pct", "period_start", "period_type", "q4_derived", "source"]


def _row(df, labels):
    if df is None or df.empty:
        return None
    idx = {str(i).strip().lower(): i for i in df.index}
    for l in labels:
        if l.strip().lower() in idx:
            return df.loc[idx[l.strip().lower()]]
    return None


def _num(v):
    try:
        f = float(v)
        return None if pd.isna(f) else f
    except (TypeError, ValueError):
        return None


def statement_rows(tk, company, currency, df, period_type, source):
    """Turn a yfinance income statement (quarterly or annual) into our schema."""
    if df is None or df.empty:
        return []
    rev_r, gp_r = _row(df, REV), _row(df, GROSS)
    cogs_r, ebit_r = _row(df, COGS), _row(df, EBIT)
    ebitda_r, net_r, eps_r = _row(df, EBITDA), _row(df, NET), _row(df, EPS)

    rows = []
    for col in df.columns:
        pe = pd.to_datetime(col, errors="coerce")
        if pd.isna(pe):
            continue
        rev = _num(rev_r.get(col)) if rev_r is not None else None
        gross = _num(gp_r.get(col)) if gp_r is not None else None
        cogs = _num(cogs_r.get(col)) if cogs_r is not None else None
        if gross is None and rev is not None and cogs is not None:
            gross = rev - cogs
        if rev is not None and rev < 0:
            rev = None
        if gross is not None and rev is not None and gross > rev * 1.005:
            gross = None
        ebit = _num(ebit_r.get(col)) if ebit_r is not None else None
        ebitda = _num(ebitda_r.get(col)) if ebitda_r is not None else None
        net = _num(net_r.get(col)) if net_r is not None else None
        eps = _num(eps_r.get(col)) if eps_r is not None else None
        if all(v is None for v in (rev, gross, ebit, net, eps)):
            continue

        def margin(v):
            return round(v / rev * 100, 3) if (rev and v is not None) else None

        if period_type == "FY":
            fy = pe.year - 1 if (pe.month == 1 and pe.day <= 7) else pe.year
            label = f"FY{fy}"
            start = (pe - pd.DateOffset(years=1) + pd.Timedelta(days=1)).date().isoformat()
        else:
            label = f"{pe.year}-Q{(pe.month - 1) // 3 + 1}"
            start = (pe - pd.Timedelta(days=91)).date().isoformat()

        rows.append({
            "ticker": tk, "company": company,
            "period_end": pe.date().isoformat(), "fiscal_quarter": label,
            "currency": currency,
            "revenue": rev, "gross_profit": gross, "ebit": ebit, "ebitda": ebitda,
            "net_income": net, "eps_diluted": eps,
            "gross_margin_pct": margin(gross), "ebit_margin_pct": margin(ebit),
            "ebitda_margin_pct": margin(ebitda), "net_margin_pct": margin(net),
            "period_start": start, "period_type": period_type,
            "q4_derived": False, "source": source,
        })
    return rows


def append(path, rows):
    if not rows:
        return
    df = pd.DataFrame(rows)[COLUMNS]
    hdr = not os.path.exists(path) or os.path.getsize(path) == 0
    df.to_csv(path, mode="a", header=hdr, index=False)


# --------------------------------------------------------------------------
def fetch_fundamentals(uni, workers, retries=2):
    done = set()
    if os.path.exists(FUND_DONE):
        done = {l.strip() for l in open(FUND_DONE) if l.strip()}
    todo = [r for r in uni.to_dict("records") if r["ticker"] not in done]
    print(f"[yf-ca] fundamentals for {len(todo)} tickers")
    if not todo:
        return

    shares_rows = []

    def one(rec):
        y = rec["yahoo"]
        t = yf.Ticker(y)
        try:
            info = t.info or {}
        except Exception:                                  # noqa: BLE001
            info = {}
        cur = info.get("financialCurrency") or info.get("currency") or "CAD"
        q = statement_rows(rec["ticker"], rec["company"], cur,
                           getattr(t, "quarterly_income_stmt", None), "Q", "yfinance")
        a = statement_rows(rec["ticker"], rec["company"], cur,
                           getattr(t, "income_stmt", None), "FY", "yfinance")
        sh = _num(info.get("sharesOutstanding")) or _num(info.get("impliedSharesOutstanding"))
        return q, a, {"ticker": rec["ticker"], "shares_outstanding": sh,
                      "currency": cur, "sector": rec.get("sector", ""),
                      "company": rec.get("company", ""),
                      "market_cap_info": _num(info.get("marketCap")),
                      "pe_forward": _num(info.get("forwardPE")),
                      "dividend_yield": _num(info.get("dividendYield")),
                      "beta": _num(info.get("beta"))}

    n = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(one, r): r["ticker"] for r in todo}
        for fut in as_completed(futs):
            tk = futs[fut]
            with _lock:
                n += 1
                try:
                    q, a, sh = fut.result()
                    append(Q_FILE, q)
                    append(A_FILE, a)
                    shares_rows.append(sh)
                    status = f"ok {len(q)}q {len(a)}y"
                except Exception as e:                     # noqa: BLE001
                    status = f"FAIL {str(e)[:38]}"
                with open(FUND_DONE, "a") as fh:
                    fh.write(tk + "\n")
                if n % 10 == 0 or n == len(todo):
                    print(f"[yf-ca] {n}/{len(todo)} {tk:<8} {status}")
                if shares_rows and (len(shares_rows) >= 25 or n == len(todo)):
                    df = pd.DataFrame(shares_rows)
                    hdr = not os.path.exists("data_meta_yf.csv") or \
                        os.path.getsize("data_meta_yf.csv") == 0
                    df.to_csv("data_meta_yf.csv", mode="a", header=hdr, index=False)
                    shares_rows = []


# --------------------------------------------------------------------------
def fetch_prices(uni, batch, pause):
    done = set()
    if os.path.exists(PX_DONE):
        done = {l.strip() for l in open(PX_DONE) if l.strip()}
    todo = [r for r in uni.to_dict("records") if r["ticker"] not in done]
    print(f"[yf-ca] prices for {len(todo)} tickers in batches of {batch}")
    if not todo:
        return

    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        syms = [r["yahoo"] for r in chunk]
        try:
            df = yf.download(syms, period=f"{YEARS}y", interval="1mo",
                             auto_adjust=False, progress=False, threads=True)
        except Exception as e:                              # noqa: BLE001
            print(f"[yf-ca] batch failed: {str(e)[:60]}")
            return
        if df is None or df.empty:
            print("[yf-ca] empty batch -- likely rate limited, stopping")
            return

        rows = []
        for rec in chunk:
            y = rec["yahoo"]
            try:
                close = df["Close"][y] if len(syms) > 1 else df["Close"]
                adj = df["Adj Close"][y] if len(syms) > 1 else df["Adj Close"]
                vol = df["Volume"][y] if len(syms) > 1 else df["Volume"]
            except (KeyError, TypeError):
                continue
            close = close.dropna()
            if close.empty:
                continue
            for ts, c in close.items():
                ts = pd.Timestamp(ts).tz_localize(None)
                rows.append({
                    "ticker": rec["ticker"], "company": rec["company"],
                    "month": ts.strftime("%Y-%m"), "date": ts.date(),
                    "close": float(c),
                    "adj_close": float(adj.get(ts)) if pd.notna(adj.get(ts)) else None,
                    "volume": float(vol.get(ts)) if pd.notna(vol.get(ts)) else None,
                })
        if rows:
            pd.DataFrame(rows).to_csv(
                M_FILE, mode="a", index=False,
                header=not os.path.exists(M_FILE) or os.path.getsize(M_FILE) == 0)
        with open(PX_DONE, "a") as fh:
            fh.write("\n".join(r["ticker"] for r in chunk) + "\n")
        print(f"[yf-ca] prices {min(i + batch, len(todo))}/{len(todo)}")
        time.sleep(pause)


# --------------------------------------------------------------------------
def build_snapshot(uni):
    """
    Market cap and trailing P/E computed from the monthly close, share counts
    and quarterly EPS -- the same approach the US pipeline moved to, so no extra
    per-ticker Yahoo calls are needed.
    """
    if not os.path.exists(M_FILE):
        print("[yf-ca] no prices yet, skipping snapshot")
        return
    m = ca_read_csv(M_FILE)
    m["date"] = pd.to_datetime(m["date"])
    last = m.sort_values("date").groupby("ticker").tail(1).set_index("ticker")

    meta = pd.DataFrame()
    if os.path.exists("data_meta_yf.csv"):
        meta = ca_read_csv("data_meta_yf.csv").drop_duplicates("ticker").set_index("ticker")

    eps_ttm = {}
    if os.path.exists(Q_FILE):
        q = ca_read_csv(Q_FILE)
        q["period_end"] = pd.to_datetime(q["period_end"])
        for tk, g in q.dropna(subset=["eps_diluted"]).sort_values("period_end").groupby("ticker"):
            if len(g) >= 4:
                eps_ttm[tk] = g["eps_diluted"].tail(4).sum()

    uni_i = uni.set_index("ticker")
    rows = []
    for tk, r in last.iterrows():
        sh = meta.at[tk, "shares_outstanding"] if tk in meta.index else None
        e = eps_ttm.get(tk)
        price = float(r["close"])
        rows.append({
            "ticker": tk,
            "company": uni_i.at[tk, "company"] if tk in uni_i.index else tk,
            "sector": uni_i.at[tk, "sector"] if tk in uni_i.index else "",
            "cross_listed": bool(uni_i.at[tk, "cross_listed"]) if tk in uni_i.index else False,
            "currency": meta.at[tk, "currency"] if tk in meta.index else "CAD",
            "price": price,
            "price_asof": str(r["date"].date()),
            "shares_outstanding": sh,
            "market_cap": round(price * sh, 0) if pd.notna(sh) and sh else None,
            "eps_trailing": round(e, 4) if e else None,
            "pe_trailing": round(price / e, 3) if e and e > 0 else None,
            "pe_forward": meta.at[tk, "pe_forward"] if tk in meta.index else None,
            "dividend_yield": meta.at[tk, "dividend_yield"] if tk in meta.index else None,
            "beta": meta.at[tk, "beta"] if tk in meta.index else None,
        })
    pd.DataFrame(rows).to_csv(S_FILE, index=False)
    print(f"[yf-ca] snapshot -> {S_FILE} ({len(rows)} rows)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=["all", "prices", "funds", "snapshot"], default="all")
    ap.add_argument("--restart", action="store_true")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--batch", type=int, default=100)
    ap.add_argument("--pause", type=float, default=1.0)
    ap.add_argument("--universe", default="universe_ca.csv")
    ap.add_argument("--tickers", default="")
    args = ap.parse_args()

    if args.restart:
        for f in (Q_FILE, A_FILE, M_FILE, S_FILE, FUND_DONE, PX_DONE, "data_meta_yf.csv"):
            if os.path.exists(f):
                try:
                    os.remove(f)
                except OSError:
                    open(f, "w").close()
        print("[yf-ca] checkpoints cleared")

    uni = ca_read_csv(args.universe, dtype={"cik": str})
    if args.tickers:
        want = {t.strip().upper() for t in args.tickers.split(",")}
        uni = uni[uni["ticker"].str.upper().isin(want)]

    if args.stage in ("all", "prices"):
        fetch_prices(uni, args.batch, args.pause)
    if args.stage in ("all", "funds"):
        fetch_fundamentals(uni, args.workers)
    if args.stage in ("all", "snapshot"):
        build_snapshot(uni)


if __name__ == "__main__":
    main()
