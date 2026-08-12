#!/usr/bin/env python3
"""
One table for the whole study: every window, every block, both objectives.

Sections
  1  per-window inventory and survival AUC
  2  the full forward-return block matrix
  3  blocks ranked, with the min across windows -- the floor matters more than
     the mean for a decision rule
  4  decile behaviour of the best block
  5  the caveats that bound every number above

Writes summary_report.csv alongside the printed report.
"""

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
    n = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    d = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return n / d if d else None


def auc(pairs):
    pos = [v for v, y in pairs if y == 1]
    neg = [v for v, y in pairs if y == 0]
    if len(pos) < 20 or len(neg) < 20:
        return None
    order = sorted(pairs, key=lambda t: t[0])
    vals = [t[0] for t in order]
    r = [0.0] * len(order)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and vals[j + 1] == vals[i]:
            j += 1
        for k in range(i, j + 1):
            r[k] = (i + j) / 2.0 + 1
        i = j + 1
    rsum = sum(rk for rk, t in zip(r, order) if t[1] == 1)
    n1, n0 = len(pos), len(neg)
    return (rsum - n1 * (n1 + 1) / 2) / (n1 * n0)


def main():
    qs = sorted(os.path.basename(f)[9:-4] for f in glob.glob("pit/universe_*.csv"))
    px = collections.defaultdict(dict)
    for r in csv.DictReader(open("data_monthly.csv")):
        v = num(r["adj_close"]) or num(r["close"])
        if v and v > 0:
            px[r["ticker"]][r["month"]] = v
    months = {t: sorted(p) for t, p in px.items()}
    data_hi = max(m for p in px.values() for m in p)

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

    uni_ciks, alive_after = {}, {}
    for q in qs:
        uni_ciks[q] = {r["ticker"]: r["cik"] for r in
                       csv.DictReader(open("pit/universe_%s.csv" % q)) if r["ticker"]}
    for q in qs:
        later = qs[qs.index(q) + 6:]
        s = set()
        for l in later:
            for r in csv.DictReader(open("pit/universe_%s.csv" % l)):
                s.add(r["cik"])
        alive_after[q] = s if later else None

    scores = {}
    for q in qs:
        f = "scores_pit_%s.csv" % q
        if os.path.exists(f):
            scores[q] = list(csv.DictReader(open(f)))

    rows_csv = []

    # ---------- 1 ----------
    print("=" * 100)
    print("1.  PER-WINDOW INVENTORY   (point-in-time universes, as-filed fundamentals, rubric v2)")
    print("=" * 100)
    print("%-8s %8s %8s %9s %8s %9s %10s %11s"
          % ("window", "filers", "scored", "priceable", "median", "conf", "surv AUC", "6q surv %"))
    print("-" * 100)
    for q in qs:
        if q not in scores:
            continue
        sc = scores[q]
        uni = uni_ciks[q]
        nf = sum(1 for _ in csv.DictReader(open("pit/universe_%s.csv" % q)))
        med = statistics.median([float(r["score"]) for r in sc])
        conf = statistics.median([float(r["confidence"]) for r in sc])
        a, surv = "", ""
        if alive_after[q]:
            pairs = [(float(r["score"]), 1 if uni.get(r["ticker"]) in alive_after[q] else 0)
                     for r in sc if r["ticker"] in uni]
            v = auc(pairs)
            if v:
                a = "%.3f" % v
                surv = "%.0f%%" % (100 * sum(y for _, y in pairs) / len(pairs))
        print("%-8s %8d %8d %9d %8.1f %8.2f %10s %11s"
              % (q, nf, len(sc), len(uni), med, conf, a or "-", surv or "-"))
        rows_csv.append({"section": "window", "window": q, "filers": nf,
                         "scored": len(sc), "priceable": len(uni),
                         "median_score": round(med, 1), "median_conf": round(conf, 2),
                         "survival_auc": a, "survival_rate": surv})

    # ---------- 2 ----------
    offs = list(range(0, 25, 3))
    print("\n" + "=" * 100)
    print("2.  FORWARD-RETURN BLOCK MATRIX   mean Spearman rho across windows (n_windows)")
    print("    rows = months after window_end when you BUY, cols = when you SELL")
    print("=" * 100)
    cells = {}
    for a in offs:
        for b in offs:
            if b <= a:
                continue
            per = []
            for q in qs:
                if q not in scores:
                    continue
                w = "%s-%s" % (q[:4], QE[q[4:]])
                s_, e_ = shift(w, a), shift(w, b)
                if e_ > data_hi:
                    continue
                pr = []
                for r in scores[q]:
                    if float(r["confidence"]) < 0.6:
                        continue
                    v = ret(r["ticker"], s_, e_)
                    if v is not None:
                        pr.append((float(r["score"]), v))
                if len(pr) < 100:
                    continue
                rho = spearman([p[0] for p in pr], [p[1] for p in pr])
                if rho is not None:
                    per.append((q, rho, len(pr),
                                statistics.median([p[1] for p in pr])))
            if per:
                cells[(a, b)] = per
    hdr = "buy\\sell" + "".join("%13dm" % b for b in offs if b > 0)
    print(hdr)
    print("-" * len(hdr))
    for a in offs:
        line = "%6dm  " % a
        for b in offs:
            if b <= a:
                line += " " * 14
                continue
            c = cells.get((a, b))
            line += ("%+.3f(%d)" % (statistics.fmean([x[1] for x in c]), len(c))).rjust(14) if c else "--".rjust(14)
        print(line)

    # ---------- 3 ----------
    print("\n" + "=" * 100)
    print("3.  BLOCKS RANKED   (>=5 windows; MIN is what a decision rule actually lives with)")
    print("=" * 100)
    print("%-16s %8s %10s %9s %9s %9s %10s"
          % ("block", "n_win", "mean rho", "min", "max", "mean n", "mean mkt"))
    print("-" * 100)
    ranked = []
    for (a, b), c in cells.items():
        if len(c) < 5:
            continue
        rhos = [x[1] for x in c]
        ranked.append((statistics.fmean(rhos), a, b, rhos, c))
    for mean, a, b, rhos, c in sorted(ranked, reverse=True)[:16]:
        print("%-16s %8d %+10.3f %+9.3f %+9.3f %9.0f %9.1f%%"
              % ("W+%d -> W+%d" % (a, b), len(c), mean, min(rhos), max(rhos),
                 statistics.fmean([x[2] for x in c]),
                 statistics.fmean([x[3] for x in c])))
        rows_csv.append({"section": "block", "block": "W+%d->W+%d" % (a, b),
                         "n_windows": len(c), "mean_rho": round(mean, 4),
                         "min_rho": round(min(rhos), 4), "max_rho": round(max(rhos), 4)})

    # ---------- 4 ----------
    if ranked:
        best = max(ranked, key=lambda t: min(t[3]))
        _, a, b, _, _ = best
        print("\n" + "=" * 100)
        print("4.  DECILES on the most RELIABLE block (W+%d -> W+%d, highest floor across windows)" % (a, b))
        print("=" * 100)
        pooled = []
        for q in qs:
            if q not in scores:
                continue
            w = "%s-%s" % (q[:4], QE[q[4:]])
            s_, e_ = shift(w, a), shift(w, b)
            if e_ > data_hi:
                continue
            for r in scores[q]:
                if float(r["confidence"]) < 0.6:
                    continue
                v = ret(r["ticker"], s_, e_)
                if v is not None:
                    pooled.append((float(r["score"]), v))
        pooled.sort()
        k = len(pooled) // 10
        print("%-8s %8s %14s %12s %12s %12s"
              % ("decile", "n", "score range", "median ret", "mean ret", "% positive"))
        print("-" * 74)
        for d in range(10):
            lo, hi = d * k, (d + 1) * k if d < 9 else len(pooled)
            ch = pooled[lo:hi]
            f = [c[1] for c in ch]
            print("%-8d %8d %14s %11.1f%% %11.1f%% %11.0f%%"
                  % (d + 1, len(ch), "%.0f-%.0f" % (ch[0][0], ch[-1][0]),
                     statistics.median(f), statistics.fmean(f),
                     100 * sum(1 for x in f if x > 0) / len(f)))
            rows_csv.append({"section": "decile", "decile": d + 1, "n": len(ch),
                             "median_return": round(statistics.median(f), 2),
                             "pct_positive": round(100 * sum(1 for x in f if x > 0) / len(f))})
        top = pooled[-k:]
        bot = pooled[:k]
        print("\n  top decile median %+.1f%%   bottom decile median %+.1f%%   spread %+.1f pts"
              % (statistics.median([t[1] for t in top]),
                 statistics.median([b_[1] for b_ in bot]),
                 statistics.median([t[1] for t in top]) - statistics.median([b_[1] for b_ in bot])))

    # ---------- 4b: eras and the crash ----------
    print("\n" + "=" * 100)
    print("4b. BY ERA -- these must NOT be pooled")
    print("=" * 100)
    print("""  Priceable coverage runs ~55% for 2018 windows against ~91% for 2026. A mean
  across all of them averages together very different degrees of blindness, so
  the eras are reported apart.\n""")
    eras = [("2018q1", "2019q4", "2018-2019   (~55-59% priceable)"),
            ("2020q1", "2020q4", "2020        (~63%, INCLUDES THE COVID CRASH)"),
            ("2021q1", "2022q4", "2021-2022   (~63-66%)"),
            ("2023q1", "2026q1", "2023-2026   (~72-91%)")]
    print("%-44s %8s %10s %10s %10s" % ("era", "windows", "W+0->W+9", "W+0->W+12", "mean n"))
    print("-" * 90)
    for lo, hi, label in eras:
        for (a, b) in [(0, 9)]:
            pass
        line_vals = []
        for (a, b) in [(0, 9), (0, 12)]:
            c = cells.get((a, b), [])
            sel = [x for x in c if lo <= x[0] <= hi]
            line_vals.append((statistics.fmean([x[1] for x in sel]) if sel else None, len(sel),
                              statistics.fmean([x[2] for x in sel]) if sel else 0))
        nwin = max(v[1] for v in line_vals)
        if not nwin:
            continue
        print("%-44s %8d %10s %10s %10.0f"
              % (label, nwin,
                 ("%+.3f" % line_vals[0][0]) if line_vals[0][0] is not None else "-",
                 ("%+.3f" % line_vals[1][0]) if line_vals[1][0] is not None else "-",
                 max(v[2] for v in line_vals)))
        rows_csv.append({"section": "era", "era": label, "n_windows": nwin,
                         "rho_W0_W9": round(line_vals[0][0], 4) if line_vals[0][0] is not None else "",
                         "rho_W0_W12": round(line_vals[1][0], 4) if line_vals[1][0] is not None else ""})

    print("\n" + "=" * 100)
    print("4c. THE STRESS TEST -- did the score help when the market actually fell?")
    print("=" * 100)
    tri = []
    for (a, b), c in cells.items():
        if b - a != 3:
            continue
        for q, rho, n, med in c:
            w = "%s-%s" % (q[:4], QE[q[4:]])
            tri.append((shift(w, a), rho, n, med))
    agg = collections.defaultdict(list)
    for start, rho, n, med in tri:
        agg[start].append((rho, med))
    uniq = sorted((k, statistics.fmean([x[0] for x in v]),
                   statistics.fmean([x[1] for x in v])) for k, v in agg.items())
    print("%-12s %10s %10s   %s" % ("quarter from", "market", "rho", ""))
    print("-" * 60)
    for k, rho, mkt in uniq:
        tag = ""
        if mkt <= -12:
            tag = "  <<< CRASH"
        elif mkt < 0:
            tag = "  <- falling"
        print("%-12s %9.1f%% %+10.3f%s" % (k, mkt, rho, tag))
    dn = [r for _, r, m in uniq if m < 0]
    up = [r for _, r, m in uniq if m >= 0]
    crash = [r for _, r, m in uniq if m <= -12]
    if dn and up:
        print()
        print("  falling quarters (n=%2d): mean rho %+.3f" % (len(dn), statistics.fmean(dn)))
        print("  rising  quarters (n=%2d): mean rho %+.3f" % (len(up), statistics.fmean(up)))
        if crash:
            print("  CRASH quarters  (n=%2d): mean rho %+.3f   <- the previously untested case"
                  % (len(crash), statistics.fmean(crash)))
        print("  rank corr(market, rho) = %+.3f" % (spearman([m for _, _, m in uniq],
                                                             [r for _, r, _ in uniq]) or 0))

    # ---------- 5 ----------
    print("\n" + "=" * 100)
    print("5.  WHAT BOUNDS EVERY NUMBER ABOVE")
    print("=" * 100)
    print("""  SURVIVORS ONLY. Yahoo drops price history when a listing ends, so every
  return figure is computed on companies that still exist. Coverage runs 66%
  of the 2021q1 universe to 91% of 2026q1 -- and it is WORST in the windows
  with the MOST forward data, so early and late windows are not comparable.

  The survival columns do NOT share this limit: they are computed from who
  filed, which is known for the companies that vanished.

  ONE REGIME. 2021-2026. Nothing here has been tested through a credit event
  or a prolonged bear market.

  2021 WINDOWS ARE THIN, NOT WEAK. DERA archives begin 2021q1, so early
  windows score 2,057 companies against 4,600+ later, on one to four quarters
  of history.

  market_cap_est IS DERIVED, from implied share count x price -- DERA carries
  no share count. Valuation rules inherit that.

  NOT INVESTMENT ADVICE. A ranking of reported history, not a forecast.""")

    if rows_csv:
        keys = sorted({k for r in rows_csv for k in r})
        with open("summary_report.csv", "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=keys)
            w.writeheader()
            w.writerows(rows_csv)
        print("\n  full table -> summary_report.csv")


if __name__ == "__main__":
    main()
