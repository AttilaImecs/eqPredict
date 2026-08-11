#!/usr/bin/env python3
"""
Which fundamentals actually predict that a company will still be there?

The scoring rubric was built to rank companies by attractiveness. This asks a
different and more basic question -- will it survive -- and answers it by
measurement rather than argument.

THE OUTCOME VARIABLE IS REAL, NOT A PROXY
  Point-in-time universes record who filed each quarter. A company present in
  2022 and absent from every quarter after 2023 stopped filing, which for a
  public company means acquired, taken private, delisted or dead. That is an
  observed fact, not a modelled one, and it is available for the delisted
  companies whose PRICES we cannot get -- so this analysis is not limited by
  the survivorship problem that constrains the return work.

  Note what it cannot distinguish: an acquisition at a premium and a
  bankruptcy both end filing. "Ceased" is not "failed". Where the two need
  separating, the direction of the predictor usually tells you which is
  dominating.

CANDIDATES TESTED
  Metrics already in the rubric, plus ones that are not and could be:
    cash_runway     cash / quarterly cash burn -- quarters of life left
    accruals        (net income - operating cash flow) / assets
    asset_turnover  revenue / assets
    equity_ratio    equity / assets
    burn_flag       is free cash flow negative at all
  Each is measured at a base quarter, then compared against whether the
  company was still filing N quarters later.
"""

import argparse
import collections
import csv
import glob
import os
import statistics
import sys

csv.field_size_limit(10**7)


def num(v):
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def ttm(rows, field, n=4):
    vals = [num(r.get(field)) for r in rows[-n:]]
    if len(vals) < n or any(v is None for v in vals):
        return None
    return sum(vals)


def latest(rows, field):
    for r in reversed(rows):
        v = num(r.get(field))
        if v is not None:
            return v
    return None


def auc(pairs):
    """Probability a survivor ranks above a non-survivor. 0.5 = no signal.

    Rank-based, so it is unaffected by outliers and by the metric's units --
    the same reason Spearman was used elsewhere.
    """
    pos = [v for v, y in pairs if y == 1]
    neg = [v for v, y in pairs if y == 0]
    if len(pos) < 20 or len(neg) < 20:
        return None, len(pos), len(neg)
    order = sorted(pairs, key=lambda t: t[0])
    ranks, i = {}, 0
    vals = [t[0] for t in order]
    r = [0.0] * len(order)
    while i < len(order):
        j = i
        while j + 1 < len(order) and vals[j + 1] == vals[i]:
            j += 1
        avg = (i + j) / 2.0 + 1
        for k in range(i, j + 1):
            r[k] = avg
        i = j + 1
    rsum = sum(rk for rk, t in zip(r, order) if t[1] == 1)
    n1, n0 = len(pos), len(neg)
    return (rsum - n1 * (n1 + 1) / 2) / (n1 * n0), n1, n0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="2022q4", help="quarter to measure at")
    ap.add_argument("--horizon", type=int, default=8, help="quarters ahead")
    ap.add_argument("--pit", default="pit")
    ap.add_argument("--dera", default="dera_quarterly.csv")
    args = ap.parse_args()

    qs = sorted(os.path.basename(f)[9:-4]
                for f in glob.glob(os.path.join(args.pit, "universe_*.csv")))
    if args.base not in qs:
        sys.exit("base %s not among %s" % (args.base, qs))
    bi = qs.index(args.base)
    later = qs[bi + args.horizon:]
    if not later:
        sys.exit("horizon runs past the data; latest is %s" % qs[-1])

    base_ciks = {r["cik"] for r in csv.DictReader(
        open(os.path.join(args.pit, "universe_%s.csv" % args.base)))}
    alive = set()
    for q in later:
        for r in csv.DictReader(open(os.path.join(args.pit, "universe_%s.csv" % q))):
            alive.add(r["cik"])

    end_ym = "%s-%02d" % (args.base[:4], {"q1": 3, "q2": 6, "q3": 9, "q4": 12}[args.base[4:]])
    by_cik = collections.defaultdict(list)
    for r in csv.DictReader(open(args.dera)):
        if r["period_end"][:7] <= end_ym:
            by_cik[r["cik"]].append(r)

    feats = collections.defaultdict(list)
    n_used = 0
    for cik in base_ciks:
        rows = sorted(by_cik.get(cik, []), key=lambda r: r["period_end"])
        if len(rows) < 6:
            continue
        y = 1 if cik in alive else 0
        rev, ni = ttm(rows, "revenue"), ttm(rows, "net_income")
        ocf, fcf = ttm(rows, "ocf"), ttm(rows, "fcf")
        assets, cash = latest(rows, "assets"), latest(rows, "cash")
        equity, debt = latest(rows, "equity"), latest(rows, "total_debt")
        ca, cl = latest(rows, "current_assets"), latest(rows, "current_liabilities")
        ebit = ttm(rows, "ebit")
        n_used += 1

        def add(name, v):
            if v is not None:
                feats[name].append((v, y))

        add("net_margin", (ni / rev * 100) if (rev and rev > 0 and ni is not None) else None)
        add("revenue_ttm_log", (rev if rev and rev > 0 else None))
        add("assets_log", assets if assets and assets > 0 else None)
        add("equity_ratio", (equity / assets) if (assets and assets > 0 and equity is not None) else None)
        add("current_ratio", (ca / cl) if (ca is not None and cl and cl > 0) else None)
        add("asset_turnover", (rev / assets) if (assets and assets > 0 and rev is not None) else None)
        add("debt_to_assets", (debt / assets) if (assets and assets > 0 and debt is not None) else None)
        add("roa", (ni / assets * 100) if (assets and assets > 0 and ni is not None) else None)
        add("accruals", ((ni - ocf) / assets) if (assets and assets > 0 and ni is not None and ocf is not None) else None)
        add("interest_cover", (ebit / abs(ttm(rows, "interest_expense")))
            if (ebit is not None and ttm(rows, "interest_expense")) else None)
        # cash runway: quarters of life at the current burn rate. Only defined
        # when the company is actually burning -- for a cash generator it is
        # meaningless, not infinite.
        if fcf is not None and fcf < 0 and cash is not None:
            add("cash_runway_q", cash / (abs(fcf) / 4))
        add("cash_to_assets", (cash / assets) if (assets and assets > 0 and cash is not None) else None)
        add("fcf_margin", (fcf / rev * 100) if (rev and rev > 0 and fcf is not None) else None)
        add("burn_flag", 0.0 if (fcf is not None and fcf < 0) else 1.0)
        add("profitable_flag", 1.0 if (ni is not None and ni > 0) else 0.0)

    print("base %s -> still filing by %s (%d quarters ahead)"
          % (args.base, later[0], args.horizon))
    print("companies with enough history: %d of %d in the base universe"
          % (n_used, len(base_ciks)))
    surv = sum(1 for c in base_ciks if c in alive)
    print("survival rate: %d of %d (%.0f%%)\n"
          % (surv, len(base_ciks), 100 * surv / len(base_ciks)))

    print("%-18s%8s%9s%10s%12s%12s" % ("metric", "n", "AUC", "signal",
                                       "med SURV", "med GONE"))
    print("-" * 70)
    out = []
    for name, pairs in feats.items():
        a, n1, n0 = auc(pairs)
        if a is None:
            continue
        ms = statistics.median([v for v, y in pairs if y == 1])
        mg = statistics.median([v for v, y in pairs if y == 0])
        out.append((abs(a - 0.5), a, name, n1 + n0, ms, mg))
    for strength, a, name, n, ms, mg in sorted(out, reverse=True):
        bar = "#" * int(strength * 100)
        print("%-18s%8d%9.3f  %-10s%12.2f%12.2f" % (name, n, a, bar, ms, mg))
    print("\nAUC 0.5 = no signal. Above 0.5 means a HIGHER value goes with")
    print("survival; below 0.5 means a higher value goes with disappearing.")


if __name__ == "__main__":
    main()
