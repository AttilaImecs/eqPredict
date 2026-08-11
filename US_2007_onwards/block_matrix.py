#!/usr/bin/env python3
"""
Every forward holding period, against every score window -- the full grid.

For each (start, end) offset in months from window_end, computes the Spearman
correlation between the score and the price change over that block, in every
score window that has the data, and reports the MEAN across windows.

WHY MEAN-ACROSS-WINDOWS RATHER THAN POOLING EVERYTHING
  Pooling all windows into one big correlation would count the same calendar
  quarter many times over -- W+3..W+6 measured from a 2024-06 window and
  W+0..W+3 from a 2024-12 window are the SAME three months of market history.
  Pooling treats them as independent evidence and overstates confidence.
  Averaging per-window rho keeps each window as one observation, and `n_win`
  reports how many there were, so a cell backed by one window is visibly
  weaker than one backed by eight.

  Even so the windows overlap in calendar time. `n_win` is an upper bound on
  independence, not a measure of it.

ONLY FORWARD BLOCKS
  start >= 0 always: every block begins at or after window_end. A block that
  started earlier would overlap the trailing window the momentum pillar is
  built from and measure the score against its own input.

Usage:
  python3 block_matrix.py
  python3 block_matrix.py --min-confidence 0.7 --step 3 --max 24
"""

import argparse
import bisect
import collections
import csv
import glob
import statistics
import sys

csv.field_size_limit(10**7)
M_FILE = "data_monthly.csv"


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


def spearman(xs, ys):
    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        out = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            for k in range(i, j + 1):
                out[order[k]] = (i + j) / 2.0 + 1
            i = j + 1
        return out
    if len(xs) < 3:
        return None
    rx, ry = ranks(xs), ranks(ys)
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    n_ = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    d = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return n_ / d if d else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-confidence", type=float, default=0.6)
    ap.add_argument("--step", type=int, default=3)
    ap.add_argument("--max", type=int, default=24)
    ap.add_argument("--out", default="block_matrix.csv")
    args = ap.parse_args()

    px = collections.defaultdict(dict)
    for r in csv.DictReader(open(M_FILE)):
        v = num(r["adj_close"]) or num(r["close"])
        if v and v > 0:
            px[r["ticker"]][r["month"]] = v
    months = {t: sorted(p) for t, p in px.items()}
    data_hi = max(m for p in px.values() for m in p)

    windows = sorted(f.split("scores_asof_")[1][:-4]
                     for f in glob.glob("scores_asof_*.csv"))
    scores = {}
    for w in windows:
        scores[w] = [(r["ticker"], float(r["score"]))
                     for r in csv.DictReader(open(f"scores_asof_{w}.csv"))
                     if float(r["confidence"]) >= args.min_confidence]
    print(f"{len(windows)} score windows: {', '.join(windows)}", file=sys.stderr)
    print(f"prices to {data_hi}, confidence >= {args.min_confidence}\n", file=sys.stderr)

    def block_return(t, s, e):
        ms = months.get(t, [])
        if not ms:
            return None
        def at(m):
            i = bisect.bisect_right(ms, m) - 1
            return (ms[i], px[t][ms[i]]) if i >= 0 else None
        a, b = at(s), at(e)
        if a is None or b is None or a[1] <= 0:
            return None
        if a[0] < shift(s, -2):      # ticker had not listed yet
            return None
        return (b[1] / a[1] - 1) * 100

    offs = list(range(0, args.max + 1, args.step))
    cells, rows_out = {}, []
    for a in offs:
        for b in offs:
            if b <= a:
                continue
            per_win = []
            for w in windows:
                s, e = shift(w, a), shift(w, b)
                if e > data_hi:
                    continue
                pairs = [(sc, block_return(t, s, e)) for t, sc in scores[w]]
                pairs = [(x, y) for x, y in pairs if y is not None]
                if len(pairs) < 100:
                    continue
                rho = spearman([p[0] for p in pairs], [p[1] for p in pairs])
                if rho is None:
                    continue
                med = statistics.median([p[1] for p in pairs])
                per_win.append((w, rho, len(pairs), med))
                rows_out.append({"start_m": a, "end_m": b, "window": w,
                                 "n": len(pairs), "rho": round(rho, 4),
                                 "median_return_pct": round(med, 2)})
            if per_win:
                cells[(a, b)] = per_win

    # ---- grid ----
    print("MEAN Spearman rho across score windows   (n_win in parentheses)")
    print("rows = block START, cols = block END, both in months after window_end\n")
    hdr = "start\\end" + "".join(f"{b:>13}m" for b in offs if b > 0)
    print(hdr)
    print("-" * len(hdr))
    for a in offs:
        line = f"{a:>6}m   "
        for b in offs:
            if b <= a:
                line += f"{'':>14}"
                continue
            c = cells.get((a, b))
            if not c:
                line += f"{'--':>14}"
            else:
                m = statistics.fmean([x[1] for x in c])
                line += f"{('%+.3f(%d)' % (m, len(c))):>14}"
        print(line)

    # ---- ranked ----
    print("\n\nBLOCKS RANKED BY MEAN rho  (only those backed by >=3 windows)")
    print(f"{'block':<18}{'n_win':>7}{'mean rho':>11}{'min':>9}{'max':>9}"
          f"{'mean n':>9}{'mean mkt':>10}")
    print("-" * 73)
    ranked = []
    for (a, b), c in cells.items():
        if len(c) < 3:
            continue
        rhos = [x[1] for x in c]
        ranked.append((statistics.fmean(rhos), a, b, c, rhos))
    for m, a, b, c, rhos in sorted(ranked, reverse=True):
        print(f"{'W+%d -> W+%d' % (a, b):<18}{len(c):>7}{m:>+11.3f}"
              f"{min(rhos):>+9.3f}{max(rhos):>+9.3f}"
              f"{statistics.fmean([x[2] for x in c]):>9.0f}"
              f"{statistics.fmean([x[3] for x in c]):>9.1f}%")

    if rows_out:
        with open(args.out, "w", newline="") as fh:
            w_ = csv.DictWriter(fh, fieldnames=list(rows_out[0].keys()))
            w_.writeheader()
            w_.writerows(rows_out)
        print(f"\nwrote {args.out}  ({len(rows_out)} window/block rows)", file=sys.stderr)


if __name__ == "__main__":
    main()
