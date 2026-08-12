#!/usr/bin/env python3
"""Monthly prices for every ticker appearing in any point-in-time universe.

Batched through yfinance, which performs the cookie/crumb handshake that raw
chart-API requests do not -- without it Yahoo returns HTTP 429 for everything,
including large caps.

Only SURVIVORS return data: Yahoo drops price history when a listing ends (see
README). The delisted names in the point-in-time universes are unpriceable by
construction, which is the documented limitation of this whole approach.
"""
import sys, time, warnings, os
import pandas as pd, yfinance as yf
warnings.filterwarnings("ignore")

OUT = "pit_monthly.csv"
tickers = [l.strip() for l in open("pit_tickers.txt") if l.strip()]
done = set()
if os.path.exists(OUT):
    done = set(pd.read_csv(OUT, usecols=["ticker"])["ticker"])
todo = [t for t in tickers if t not in done]
print("%d tickers, %d already fetched, %d to go" % (len(tickers), len(done), len(todo)), file=sys.stderr)

B = 120
for i in range(0, len(todo), B):
    chunk = todo[i:i+B]
    try:
        df = yf.download(chunk, start="2017-01-01", interval="1mo",
                         auto_adjust=False, progress=False, threads=True, group_by="column")
    except Exception as e:
        print("  batch failed: %s" % str(e)[:60], file=sys.stderr); time.sleep(10); continue
    if df is None or df.empty:
        print("  empty batch at %d" % i, file=sys.stderr); time.sleep(10); continue
    rows = []
    for t in chunk:
        try:
            close = df["Close"][t] if len(chunk) > 1 else df["Close"]
            adj = df["Adj Close"][t] if len(chunk) > 1 else df["Adj Close"]
            vol = df["Volume"][t] if len(chunk) > 1 else df["Volume"]
        except (KeyError, TypeError):
            continue
        close = close.dropna()
        for ts, c in close.items():
            ts = pd.Timestamp(ts).tz_localize(None)
            rows.append({"ticker": t, "month": ts.strftime("%Y-%m"),
                         "close": float(c),
                         "adj_close": (float(adj.get(ts)) if pd.notna(adj.get(ts)) else None),
                         "volume": (float(vol.get(ts)) if pd.notna(vol.get(ts)) else None)})
    if rows:
        pd.DataFrame(rows).to_csv(OUT, mode="a", index=False,
                                  header=not os.path.exists(OUT) or os.path.getsize(OUT) == 0)
    print("  %d/%d  +%d rows" % (min(i+B, len(todo)), len(todo), len(rows)), file=sys.stderr)
    time.sleep(2)
print("done -> %s" % OUT, file=sys.stderr)
