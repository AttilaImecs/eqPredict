#!/usr/bin/env python3
"""
Which holding period does the score actually predict? -- horizon sweep.

For every (window_end, price-period) pair: Spearman rank correlation between
the score and the price change over that period, plus the top-decile minus
bottom-decile spread.

TRAILING vs FORWARD -- THE DISTINCTION THAT DECIDES WHETHER A NUMBER MEANS
ANYTHING
  The score at window_end W is computed from data up to W, and momentum is 20%
  of it built directly from the trailing 6- and 12-month return. So a period
  that starts before W measures the score partly against its own input:

    fully trailing   (W-1y -> W)      circular. A high rho here is arithmetic,
                                      not prediction.
    half trailing    (W-1y -> W+1y)   contaminated in proportion to overlap.
    forward only     (W -> W+n)       the only honest test, and the only one
                                      that answers "when should I buy".

  All are reported, because the CONTRAST is the useful part: if trailing rho is
  high and forward rho is not, the score is describing the past rather than
  anticipating the future.

EXCESS RETURN
  Raw return confounds stock picking with market direction: +20% in a market
  that rose 25% is underperformance. The `excess` columns subtract the
  universe median return over the identical period, which removes the common
  move and leaves what the score can actually claim credit for.

Usage:
  python3 sweep_horizons.py
  python3 sweep_horizons.py --min-confidence 0.7
"""

import argparse
import bisect
import collections
import csv
import statistics
import sys

csv.field_size_limit(10**7)

M_FILE = "data_monthly.csv"
WINDOWS = ["2024-12", "2025-12", "2026-06"]

# (label, months from window_end to start, months to end)
SPECS = [
    ("1  W -> W+3m",        0,   3),
    ("2  W-1y -> W+1y",   -12,  12),
    ("3  W-1y -> W",      -12,   0),
    ("4  W-6m -> W+6m",    -6,   6),
    ("5  W -> W+1y",        0,  12),
    ("6  W+3m -> W+1y",     3,  12),
    ("7  W+6m -> W+1y",     6,  12),
    # added: the decay profile, and the longest forward run available
    ("8  W -> W+6m",        0,   6),
    ("9  W+6m -> W+18m",    6,  18),
    ("10 W -> W+20m",       0,  20),
]


def num(v):
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def shift(month, k):
    y, m = int(month[:4]), int(month[5:])
    t = y * 12 + m - 1 + k
    return "%04d-%02d" % (t // 12, t % 12 + 1)


def load_prices():
    px = collections.defaultdict(dict)
    for r in csv.DictReader(open(M_FILE)):
        v = num(r["adj_close"]) or num(r["close"])
        if v and v > 0:
            px[r["ticker"]][r["month"]] = v
    return px


def spearman(xs, ys):
    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        out = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1
            for k in range(i, j + 1):
                out[order[k]] = avg
            i = j + 1
        return out
    if len(xs) < 3:
        return None
    rx, ry = ranks(xs), ranks(ys)
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    n_ = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    d = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return n_ / d if d else None


def ret(prices, months_sorted, start, end):
    """Price change between two months, using the last quote at or before each.

    Requires the start quote to be at or after (start - 2 months): otherwise a
    ticker that only listed later would silently anchor on its first ever price
    and report the whole listing history as the period's return.
    """
    if not months_sorted:
        return None

    def at(m):
        i = bisect.bisect_right(months_sorted, m) - 1
        return (months_sorted[i], prices[months_sorted[i]]) if i >= 0 else None

    a, b = at(start), at(end)
    if a is None or b is None or a[1] <= 0:
        return None
    if a[0] < shift(start, -2):
        return None
    return (b[1] / a[1] - 1) * 100


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-confidence", type=float, default=0.6)
    args = ap.parse_args()

    px = load_prices()
    months = {t: sorted(p) for t, p in px.items()}
    data_lo = min(m for p in px.values() for m in p)
    data_hi = max(m for p in px.values() for m in p)

    scores = {}
    for w in WINDOWS:
        try:
            scores[w] = [r for r in csv.DictReader(open(f"scores_asof_{w}.csv"))
                         if float(r["confidence"]) >= args.min_confidence]
        except FileNotFoundError:
            pass

    print(f"price data {data_lo} .. {data_hi}   confidence >= {args.min_confidence}\n")
    print(f"{'period':<20}{'window':<10}{'kind':<10}{'n':>6}"
          f"{'rho raw':>9}{'rho excess':>12}{'top-bot spread':>16}")
    print("-" * 83)

    for label, a, b in SPECS:
        kind = ("trailing" if b <= 0 else
                "forward" if a >= 0 else "mixed")
        any_row = False
        for w in WINDOWS:
            if w not in scores:
                continue
            s_m, e_m = shift(w, a), shift(w, b)
            if s_m < data_lo or e_m > data_hi:
                continue
            pairs = []
            for r in scores[w]:
                t = r["ticker"]
                v = ret(px.get(t, {}), months.get(t, []), s_m, e_m)
                if v is not None:
                    pairs.append((float(r["score"]), v))
            if len(pairs) < 50:
                continue
            any_row = True
            med = statistics.median([p[1] for p in pairs])
            sc = [p[0] for p in pairs]
            raw = [p[1] for p in pairs]
            exc = [p[1] - med for p in pairs]
            rho_r, rho_e = spearman(sc, raw), spearman(sc, exc)
            order = sorted(pairs, key=lambda p: p[0])
            k = max(1, len(order) // 10)
            spread = (statistics.median([x[1] for x in order[-k:]])
                      - statistics.median([x[1] for x in order[:k]]))
            print(f"{label:<20}{w:<10}{kind:<10}{len(pairs):>6}"
                  f"{rho_r:>+9.3f}{rho_e:>+12.3f}{spread:>+15.1f}p")
        if not any_row:
            print(f"{label:<20}{'--':<10}{kind:<10}{'':>6}"
                  f"{'':>9}{'':>12}{'  no window fits the data':>16}")
        print()


if __name__ == "__main__":
    main()
