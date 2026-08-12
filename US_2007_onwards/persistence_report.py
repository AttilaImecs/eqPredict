#!/usr/bin/env python3
"""
Do the top 25 STAY good? Cohort persistence and long-run performance.

For each cohort quarter, take the top 25 by score and then, in every later
quarter, ask three things:

  how many of the original 25 are still in that quarter's top 25   (persistence)
  what the original 25 now score on average                        (decay)
  what the universe scores on average over the same period         (baseline)

plus the cumulative return of the cohort from its formation date, against the
median company in the universe it was drawn from.

WHY PERSISTENCE IS THE INTERESTING NUMBER
  A screen that reshuffles completely every quarter is either finding real
  change or measuring noise, and the two are indistinguishable from a single
  quarter's list. If the same names keep appearing, the score is picking up
  something durable about the business. If they scatter, it is tracking
  whatever moved last quarter -- and the turnover alone would eat any edge in
  trading costs.

  The universe average score is the control. Scores drift for everyone as the
  peer distribution shifts, so a cohort falling from 84 to 79 means nothing
  until you know the universe went from 50 to 48.
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


def top_n(rows, n, min_conf):
    rows = [r for r in rows if float(r["confidence"]) >= min_conf]
    rows.sort(key=lambda r: -float(r["score"]))
    seen, out = set(), []
    for r in rows:
        k = r["company"].split(" (Class")[0].strip()
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
        if len(out) >= n:
            break
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=25)
    ap.add_argument("--min-confidence", type=float, default=0.6)
    ap.add_argument("--cohorts", default="2023q1,2023q2,2023q3,2023q4,"
                                          "2024q1,2024q2,2024q3,2024q4")
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

    qs = sorted(os.path.basename(f)[11:-4] for f in glob.glob("scores_pit_*.csv"))
    scores = {q: list(csv.DictReader(open("scores_pit_%s.csv" % q))) for q in qs}
    tops = {q: {r["ticker"] for r in top_n(scores[q], args.n, args.min_confidence)}
            for q in qs}
    byq_score = {q: {r["ticker"]: float(r["score"]) for r in scores[q]} for q in qs}
    uni_mean = {q: statistics.fmean([float(r["score"]) for r in scores[q]
                                     if float(r["confidence"]) >= args.min_confidence])
                for q in qs}

    rows_csv, cohort_summary = [], []
    for c in args.cohorts.split(","):
        c = c.strip()
        if c not in qs:
            continue
        cohort = top_n(scores[c], args.n, args.min_confidence)
        tickers = [r["ticker"] for r in cohort]
        base_score = statistics.fmean([float(r["score"]) for r in cohort])
        w0 = "%s-%s" % (c[:4], QE[c[4:]])
        later = [q for q in qs if q > c]

        print("=" * 104)
        print("COHORT %s  --  top %d by score, average score %.1f (universe average %.1f)"
              % (c, args.n, base_score, uni_mean[c]))
        print("=" * 104)
        print("%-9s %8s %11s %11s %11s   %12s %12s %11s"
              % ("quarter", "qtrs on", "still top25", "cohort avg", "universe avg",
                 "cohort ret", "universe ret", "excess"))
        print("-" * 104)

        pers, cret, uret = [], [], []
        for i, q in enumerate(later, 1):
            still = sum(1 for t in tickers if t in tops[q])
            cs = [byq_score[q][t] for t in tickers if t in byq_score[q]]
            cavg = statistics.fmean(cs) if cs else None
            wq = "%s-%s" % (q[:4], QE[q[4:]])
            if wq > hi:
                continue
            cr = [ret(t, w0, wq) for t in tickers]
            cr = [v for v in cr if v is not None]
            ur = [ret(r["ticker"], w0, wq) for r in scores[q]
                  if float(r["confidence"]) >= args.min_confidence]
            ur = [v for v in ur if v is not None]
            cm = statistics.median(cr) if cr else None
            um = statistics.median(ur) if ur else None
            pers.append(still)
            if cm is not None and um is not None:
                cret.append(cm)
                uret.append(um)
            print("%-9s %8d %6d /%3d %11s %11.1f   %11s %12s %10s"
                  % (q, i, still, args.n,
                     ("%.1f" % cavg) if cavg is not None else "-",
                     uni_mean[q],
                     ("%+.1f%%" % cm) if cm is not None else "-",
                     ("%+.1f%%" % um) if um is not None else "-",
                     ("%+.1f" % (cm - um)) if (cm is not None and um is not None) else "-"))
            rows_csv.append({"cohort": c, "quarter": q, "quarters_on": i,
                             "still_top25": still, "cohort_avg_score":
                             round(cavg, 1) if cavg is not None else "",
                             "universe_avg_score": round(uni_mean[q], 1),
                             "cohort_cum_return": round(cm, 2) if cm is not None else "",
                             "universe_cum_return": round(um, 2) if um is not None else ""})
        if pers:
            cohort_summary.append((c, base_score, pers, cret, uret, tickers, later))
            print("-" * 104)
            print("  persistence: %.1f of %d on average (%.0f%%), %d of %d still there in the last quarter"
                  % (statistics.fmean(pers), args.n,
                     100 * statistics.fmean(pers) / args.n, pers[-1], args.n))
            if cret:
                print("  average cumulative return across all later quarters: cohort %+.1f%%  "
                      "universe %+.1f%%  excess %+.1f pts"
                      % (statistics.fmean(cret), statistics.fmean(uret),
                         statistics.fmean(cret) - statistics.fmean(uret)))
        print()

    if cohort_summary:
        print("=" * 104)
        print("SUMMARY ACROSS COHORTS")
        print("=" * 104)
        print("%-9s %10s %12s %12s %14s %14s %10s"
              % ("cohort", "base score", "avg persist", "persist %",
                 "avg coh ret", "avg uni ret", "excess"))
        print("-" * 104)
        allp, alle = [], []
        for c, bs, pers, cret, uret, _, _ in cohort_summary:
            p = statistics.fmean(pers)
            allp.append(100 * p / args.n)
            e = (statistics.fmean(cret) - statistics.fmean(uret)) if cret else None
            if e is not None:
                alle.append(e)
            print("%-9s %10.1f %9.1f/%-2d %11.0f%% %13s %14s %9s"
                  % (c, bs, p, args.n, 100 * p / args.n,
                     ("%+.1f%%" % statistics.fmean(cret)) if cret else "-",
                     ("%+.1f%%" % statistics.fmean(uret)) if uret else "-",
                     ("%+.1f" % e) if e is not None else "-"))
        print("-" * 104)
        print("  MEAN persistence %.0f%% of the original %d;  MEAN excess %+.1f pts"
              % (statistics.fmean(allp), args.n,
                 statistics.fmean(alle) if alle else 0))

    if rows_csv:
        with open("persistence_report.csv", "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows_csv[0].keys()))
            w.writeheader()
            w.writerows(rows_csv)
        print("\n  detail -> persistence_report.csv")


if __name__ == "__main__":
    main()
