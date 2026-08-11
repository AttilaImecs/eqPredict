#!/usr/bin/env python3
"""
Does the score predict anything? -- forward-return validation.

Reads:  scores_asof_YYYY-MM.csv (one per window), data_monthly.csv
Writes: score_validation.csv  (ticker, window, score, forward return)

THE ONLY VALID TEST IS FORWARD RETURN
  A score computed as at date T is compared against the return AFTER T. The
  trailing return is not a test: momentum is 20% of the score and is built from
  trailing prices, so scoring against the past would measure the score against
  its own input and report a correlation that means nothing.

WHAT THIS CANNOT TELL YOU
  This is not a backtest. It is three overlapping windows drawn from one market
  regime in a single dataset:

    * The windows OVERLAP. 2024-12 -> now contains 2025-12 -> now entirely, so
      the three results are not independent observations.
    * One regime. Whatever led over this stretch -- and it was a period when
      loss-making biotech and net-cash balance sheets did well -- is not
      evidence the rubric works across regimes.
    * SURVIVORSHIP. universe.csv is index membership as of collection date, so
      companies that delisted or were acquired between T and now are absent.
      That biases forward returns UPWARD, and it biases them most in exactly
      the low-scoring, distressed bucket that should be dragging the result
      down. Read the bottom decile as flattered.
    * MOMENTUM CIRCULARITY. Even forward-tested, part of any signal may just be
      momentum persisting rather than the fundamental pillars working. The
      per-pillar breakdown below is there to separate the two -- if momentum is
      the only pillar with predictive power, the other 80% of the rubric is
      decoration.

  Spearman (rank) correlation is used rather than Pearson: the score is
  ordinal, forward returns are wildly non-normal, and a handful of 500%
  micro-caps would otherwise dominate a linear fit.

Usage:
  python3 validate_scores.py
  python3 validate_scores.py --min-confidence 0.7
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
OUT = "score_validation.csv"
PILLARS = ["growth", "profitability", "quality", "health", "valuation", "momentum"]


def num(v):
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def load_prices():
    """ticker -> {month: adj_close}. Total-return basis where available."""
    px = collections.defaultdict(dict)
    for r in csv.DictReader(open(M_FILE)):
        v = num(r["adj_close"]) or num(r["close"])
        if v and v > 0:
            px[r["ticker"]][r["month"]] = v
    return px


def spearman(xs, ys):
    """Rank correlation, average ranks for ties."""
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

    n = len(xs)
    if n < 3:
        return None
    rx, ry = ranks(xs), ranks(ys)
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    num_ = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return num_ / den if den else None


def forward_return(prices, start, end):
    """% change between two months, using the last price at or before each."""
    months = sorted(prices)
    if not months:
        return None

    def at(m):
        i = bisect.bisect_right(months, m) - 1
        return prices[months[i]] if i >= 0 else None

    a, b = at(start), at(end)
    if a is None or b is None or a <= 0:
        return None
    return (b / a - 1) * 100


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-confidence", type=float, default=0.0)
    ap.add_argument("--end", default=None,
                    help="measure forward return to this month (default: latest)")
    args = ap.parse_args()

    px = load_prices()
    latest = max(m for p in px.values() for m in p)
    end = args.end or latest
    print(f"forward returns measured to {end}\n", file=sys.stderr)

    rows_out = []
    for w in WINDOWS:
        try:
            scored = list(csv.DictReader(open(f"scores_asof_{w}.csv")))
        except FileNotFoundError:
            print(f"(no scores_asof_{w}.csv -- skipping)", file=sys.stderr)
            continue
        if w >= end:
            print(f"=== {w}: window ends at or after {end}, no forward period ===\n")
            continue

        pairs = []
        for r in scored:
            if float(r["confidence"]) < args.min_confidence:
                continue
            fwd = forward_return(px.get(r["ticker"], {}), w, end)
            if fwd is None:
                continue
            pairs.append((float(r["score"]), fwd, r))
            rows_out.append({"ticker": r["ticker"], "company": r["company"],
                             "window": w, "score": r["score"],
                             "band": r["band"], "confidence": r["confidence"],
                             "forward_return_pct": round(fwd, 2),
                             **{p: r.get(p, "") for p in PILLARS}})

        if len(pairs) < 30:
            print(f"=== {w}: only {len(pairs)} usable -- skipping ===\n")
            continue

        months = (int(end[:4]) - int(w[:4])) * 12 + int(end[5:]) - int(w[5:])
        pairs.sort(key=lambda t: t[0])
        scores = [p[0] for p in pairs]
        fwds = [p[1] for p in pairs]
        rho = spearman(scores, fwds)

        print("=" * 78)
        print(f"AS-OF {w}   ->   {end}   ({months} months forward, n={len(pairs)})")
        print("=" * 78)
        print(f"Spearman rank correlation, score vs forward return: "
              f"{rho:+.3f}" if rho is not None else "n/a")
        print()

        # Deciles, on the score. Median return per bucket, because the mean is
        # hostage to a couple of micro-caps that went up 10x.
        print(f"{'decile':<8}{'n':>5}{'score range':>16}{'median ret':>12}"
              f"{'mean ret':>11}{'% positive':>12}")
        k = len(pairs) // 10
        for d in range(10):
            lo = d * k
            hi = (d + 1) * k if d < 9 else len(pairs)
            chunk = pairs[lo:hi]
            f = [c[1] for c in chunk]
            print(f"{d + 1:<8}{len(chunk):>5}"
                  f"{('%.0f-%.0f' % (chunk[0][0], chunk[-1][0])):>16}"
                  f"{statistics.median(f):>11.1f}%{statistics.fmean(f):>10.1f}%"
                  f"{100 * sum(1 for x in f if x > 0) / len(f):>11.0f}%")

        top, bot = pairs[-k:], pairs[:k]
        print()
        print(f"top decile median {statistics.median([t[1] for t in top]):+.1f}%  vs  "
              f"bottom decile median {statistics.median([b[1] for b in bot]):+.1f}%  "
              f"(spread {statistics.median([t[1] for t in top]) - statistics.median([b[1] for b in bot]):+.1f} pts)")

        # Which pillar, if any, is doing the work.
        print()
        print("per-pillar rank correlation with the same forward return:")
        for p in PILLARS:
            sub = [(num(r.get(p)), fwd) for _, fwd, r in pairs if num(r.get(p)) is not None]
            if len(sub) < 30:
                print(f"   {p:<16} n={len(sub):<5} (too few)")
                continue
            pr = spearman([s for s, _ in sub], [f for _, f in sub])
            print(f"   {p:<16} n={len(sub):<5} rho {pr:+.3f}")
        print()

    if rows_out:
        with open(OUT, "w", newline="") as fh:
            w_ = csv.DictWriter(fh, fieldnames=list(rows_out[0].keys()))
            w_.writeheader()
            w_.writerows(rows_out)
        print(f"wrote {OUT}  ({len(rows_out)} rows)", file=sys.stderr)


if __name__ == "__main__":
    main()
