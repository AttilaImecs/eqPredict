#!/usr/bin/env python3
"""
Top 20 by score each quarter, and what those picks actually did.

Writes top20_by_quarter.csv -- every pick, every quarter, with forward returns
at 3, 6 and 12 months -- and prints a summary comparing the cohort against the
universe it was drawn from.

THE COMPARISON IS THE POINT
  A top-20 list that returned +8% tells you nothing on its own. What matters is
  +8% against WHAT: if the median company in the same universe over the same
  months returned +12%, the selection destroyed value. Every return here is
  therefore shown beside the universe median for the identical window, and the
  EXCESS is the column to read.
"""

import argparse
import bisect
import collections
import csv
import glob
import os
import statistics
import sys

csv.field_size_limit(10**7)
QE = {"q1": "03", "q2": "06", "q3": "09", "q4": "12"}


def num(v):
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def shift(m, k):
    y, mo = int(m[:4]), int(m[5:])
    t = y * 12 + mo - 1 + k
    return "%04d-%02d" % (t // 12, t % 12 + 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--min-confidence", type=float, default=0.6)
    ap.add_argument("--detail", default="", help="print one quarter in full")
    args = ap.parse_args()

    px = collections.defaultdict(dict)
    for r in csv.DictReader(open("data_monthly.csv")):
        v = num(r["adj_close"]) or num(r["close"])
        if v and v > 0:
            px[r["ticker"]][r["month"]] = v
    months = {t: sorted(p) for t, p in px.items()}
    hi = max(m for p in px.values() for m in p)

    def ret(t, s, e):
        ms = months.get(t, [])
        if not ms:
            return None
        def at(m):
            i = bisect.bisect_right(ms, m) - 1
            return (ms[i], px[t][ms[i]]) if i >= 0 else None
        a, b = at(s), at(e)
        if a is None or b is None or a[1] <= 0 or a[0] < shift(s, -2):
            return None
        return (b[1] / a[1] - 1) * 100

    qs = sorted(os.path.basename(f)[11:-4]
                for f in glob.glob("scores_pit_*.csv"))
    rows_out, summary = [], []

    for q in qs:
        w = "%s-%s" % (q[:4], QE[q[4:]])
        sc = [r for r in csv.DictReader(open("scores_pit_%s.csv" % q))
              if float(r["confidence"]) >= args.min_confidence]
        if not sc:
            continue
        sc.sort(key=lambda r: -float(r["score"]))

        # universe baseline: every scored company, same horizons
        base = {}
        for h in (3, 6, 12):
            e = shift(w, h)
            if e > hi:
                continue
            vals = [ret(r["ticker"], w, e) for r in sc]
            vals = [v for v in vals if v is not None]
            if vals:
                base[h] = statistics.median(vals)

        seen, picks = set(), []
        for r in sc:
            key = r["company"].split(" (Class")[0].strip()
            if key in seen:
                continue                       # collapse dual share classes
            seen.add(key)
            picks.append(r)
            if len(picks) >= args.n:
                break

        cohort = collections.defaultdict(list)
        for rank, r in enumerate(picks, 1):
            row = {"quarter": q, "as_of": w, "rank": rank,
                   "ticker": r["ticker"], "company": r["company"][:40],
                   "score": r["score"], "confidence": r["confidence"],
                   "sector": r.get("sector", ""), "flags": r.get("flags", "")}
            for h in (3, 6, 12):
                e = shift(w, h)
                v = ret(r["ticker"], w, e) if e <= hi else None
                row["ret_%dm" % h] = round(v, 2) if v is not None else ""
                if v is not None:
                    cohort[h].append(v)
            rows_out.append(row)

        summary.append((q, w, len(sc),
                        {h: (statistics.median(v) if v else None)
                         for h, v in cohort.items()},
                        base))

        if args.detail == q:
            print("=" * 96)
            print("TOP %d BY SCORE, %s  (of %d scored at confidence >= %.1f)"
                  % (args.n, w, len(sc), args.min_confidence))
            print("=" * 96)
            print("%-4s %-7s %-32s %6s %8s %8s %9s"
                  % ("#", "TICKER", "COMPANY", "SCORE", "+3m", "+6m", "+12m"))
            print("-" * 96)
            for rank, r in enumerate(picks, 1):
                vals = []
                for h in (3, 6, 12):
                    e = shift(w, h)
                    v = ret(r["ticker"], w, e) if e <= hi else None
                    vals.append("%+.0f%%" % v if v is not None else "-")
                print("%-4d %-7s %-32s %6s %8s %8s %9s"
                      % (rank, r["ticker"], r["company"][:32], r["score"], *vals))
            print()

    print("=" * 104)
    print("TOP %d COHORT vs THE UNIVERSE IT WAS DRAWN FROM   (median %%, and EXCESS over universe median)"
          % args.n)
    print("=" * 104)
    print("%-9s %7s | %8s %8s %8s | %8s %8s %8s | %8s %8s %8s"
          % ("quarter", "scored", "top+3m", "top+6m", "top+12m",
             "uni+3m", "uni+6m", "uni+12m", "exc+3m", "exc+6m", "exc+12m"))
    print("-" * 104)
    exc = collections.defaultdict(list)
    for q, w, n, coh, base in summary:
        f = lambda d, h: ("%+.1f" % d[h]) if d.get(h) is not None else "-"
        e3 = e6 = e12 = "-"
        for h, lab in ((3, "3"), (6, "6"), (12, "12")):
            if coh.get(h) is not None and base.get(h) is not None:
                d = coh[h] - base[h]
                exc[h].append(d)
                if h == 3: e3 = "%+.1f" % d
                elif h == 6: e6 = "%+.1f" % d
                else: e12 = "%+.1f" % d
        print("%-9s %7d | %8s %8s %8s | %8s %8s %8s | %8s %8s %8s"
              % (q, n, f(coh, 3), f(coh, 6), f(coh, 12),
                 f(base, 3), f(base, 6), f(base, 12), e3, e6, e12))
    print("-" * 104)
    print("%-9s %7s | %8s %8s %8s | %8s %8s %8s | %8s %8s %8s"
          % ("MEAN EXCESS", "", "", "", "", "", "", "",
             ("%+.1f" % statistics.fmean(exc[3])) if exc[3] else "-",
             ("%+.1f" % statistics.fmean(exc[6])) if exc[6] else "-",
             ("%+.1f" % statistics.fmean(exc[12])) if exc[12] else "-"))
    for h in (3, 6, 12):
        if exc[h]:
            wins = sum(1 for x in exc[h] if x > 0)
            print("   +%2dm: beat the universe in %d of %d quarters (%.0f%%), "
                  "median excess %+.1f pts"
                  % (h, wins, len(exc[h]), 100 * wins / len(exc[h]),
                     statistics.median(exc[h])))

    if rows_out:
        keys = ["quarter", "as_of", "rank", "ticker", "company", "score",
                "confidence", "sector", "flags", "ret_3m", "ret_6m", "ret_12m"]
        with open("top20_by_quarter.csv", "w", newline="") as fh:
            wr = csv.DictWriter(fh, fieldnames=keys)
            wr.writeheader()
            for r in rows_out:
                wr.writerow({k: r.get(k, "") for k in keys})
        print("\nall %d picks -> top20_by_quarter.csv" % len(rows_out))


if __name__ == "__main__":
    main()
