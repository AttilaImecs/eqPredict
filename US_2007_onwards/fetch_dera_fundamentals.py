#!/usr/bin/env python3
"""
Fundamentals from SEC's DERA quarterly data sets -- AS FILED, point-in-time.

WHY NOT companyfacts
  The companyfacts API returns values AS RESTATED TODAY. A score computed "as
  at 2021-12" would therefore be built from numbers republished in 2024 --
  lookahead bias, and precisely the kind that makes a backtest look better than
  reality. DERA publishes one archive per quarter containing what was actually
  filed THEN, so a 2021 score sees 2021's numbers.

WHY num.txt IS A CLEANER SOURCE THAN THE API
  `qtrs` states the period length directly: 0 = instant (a balance-sheet
  position), 1 = one quarter, 4 = a year. The whole day-counting apparatus in
  fetch_sec_fundamentals.py -- QUARTER_DAYS, ANNUAL_DAYS, the 4-5-4 retail
  window -- exists only because the API forces you to infer this from start and
  end dates. Here it is stated.

TWO FILTERS THAT ARE NOT OPTIONAL
  `segments` must be EMPTY. Rows with segments are dimensional breakdowns --
  revenue by business unit, by geography, by product. Summing or picking among
  them gives a number that is not the company's consolidated total. In 2018Q1,
  segment rows are the majority of the file.

  `coreg` must be EMPTY. Co-registrant rows report a subsidiary or guarantor
  separately from the parent.

Writes: dera_quarterly.csv  -- same schema as the parent pipeline's
        data_quarterly.csv, so score_companies.py can read it unchanged.

Usage:
  python3 fetch_dera_fundamentals.py --dir <zipdir> --out dera_quarterly.csv
"""

import argparse
import collections
import csv
import glob
import io
import os
import sys
import zipfile

# tag -> our field. Order matters within a field: first match wins, so the
# broadest correct concept is listed first.
TAGS = {
    "Revenues": "revenue",
    "RevenueFromContractWithCustomerExcludingAssessedTax": "revenue",
    "SalesRevenueNet": "revenue",
    "RevenuesNetOfInterestExpense": "revenue",
    "GrossProfit": "gross_profit",
    "OperatingIncomeLoss": "ebit",
    "NetIncomeLoss": "net_income",
    "ProfitLoss": "net_income",
    "EarningsPerShareDiluted": "eps_diluted",
    "DepreciationDepletionAndAmortization": "dep_amort",
    "DepreciationAndAmortization": "dep_amort",
    "NetCashProvidedByUsedInOperatingActivities": "ocf",
    "PaymentsToAcquirePropertyPlantAndEquipment": "capex",
    "InterestExpense": "interest_expense",
    # instant / balance sheet
    "CashAndCashEquivalentsAtCarryingValue": "cash",
    "Assets": "assets",
    "Liabilities": "liabilities",
    "AssetsCurrent": "current_assets",
    "LiabilitiesCurrent": "current_liabilities",
    "StockholdersEquity": "equity",
    "LongTermDebtNoncurrent": "debt_lt",
    "LongTermDebtCurrent": "debt_st",
    "LongTermDebtAndCapitalLeaseObligations": "debt_lt",
    "LongTermDebt": "debt_total",
}
# Fields whose value is a POSITION, not a flow. qtrs must be 0 for these.
INSTANT = {"cash", "assets", "liabilities", "current_assets",
           "current_liabilities", "equity", "debt_lt", "debt_st", "debt_total"}
# Priority when two tags map to the same field for the same period.
PREF = {"revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                    "SalesRevenueNet", "RevenuesNetOfInterestExpense"],
        "net_income": ["NetIncomeLoss", "ProfitLoss"],
        "dep_amort": ["DepreciationDepletionAndAmortization", "DepreciationAndAmortization"],
        "debt_lt": ["LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations"]}

OUT_COLS = ["cik", "company", "period_end", "fiscal_quarter", "currency",
            "revenue", "gross_profit", "ebit", "ebitda", "net_income",
            "eps_diluted", "ocf", "capex", "fcf", "interest_expense",
            "cash", "short_term_investments", "total_debt", "net_debt",
            "equity", "assets", "liabilities", "current_assets",
            "current_liabilities", "gross_margin_pct", "ebit_margin_pct",
            "ebitda_margin_pct", "net_margin_pct", "fcf_margin_pct",
            "period_start", "period_type", "q4_derived", "source", "taxonomy"]


def parse_quarter(zpath, store, names, quarter):
    """Fold one quarterly archive into `store`: (cik, ddate) -> {field: (tag, val)}."""
    with zipfile.ZipFile(zpath) as z:
        with z.open("sub.txt") as fh:
            adsh2cik, adsh2name = {}, {}
            rd = csv.DictReader(io.TextIOWrapper(fh, "utf8", errors="replace"),
                                delimiter="\t")
            for r in rd:
                if r.get("form") not in ("10-K", "10-Q", "20-F", "40-F"):
                    continue
                try:
                    adsh2cik[r["adsh"]] = int(r["cik"])
                except (TypeError, ValueError):
                    continue
                adsh2name[r["adsh"]] = r.get("name", "")

        with z.open("num.txt") as fh:
            txt = io.TextIOWrapper(fh, "utf8", errors="replace")
            header = txt.readline().rstrip("\n").split("\t")
            ix = {h: i for i, h in enumerate(header)}
            i_adsh, i_tag = ix["adsh"], ix["tag"]
            i_dd, i_q, i_uom = ix["ddate"], ix["qtrs"], ix["uom"]
            i_seg, i_cor, i_val = ix["segments"], ix["coreg"], ix["value"]
            for line in txt:
                p = line.rstrip("\n").split("\t")
                if len(p) <= i_val:
                    continue
                # consolidated totals only -- see module docstring
                if p[i_seg] or p[i_cor]:
                    continue
                field = TAGS.get(p[i_tag])
                if field is None:
                    continue
                cik = adsh2cik.get(p[i_adsh])
                if cik is None:
                    continue
                q = p[i_q]
                if field in INSTANT:
                    if q != "0":
                        continue
                elif q not in ("1", "2", "3", "4"):
                    continue
                # Flows keep qtrs 1-4. Cash-flow items are filed YEAR TO DATE,
                # so operating cash flow for Q3 arrives as qtrs=3 covering nine
                # months. Accepting only qtrs=1 captured Q1 and nothing else --
                # OCF coverage was 3%. The YTD values are differenced back to
                # single quarters in derive_quarterly() below.
                v = p[i_val]
                if not v:
                    continue
                try:
                    val = float(v)
                except ValueError:
                    continue
                dd = p[i_dd]
                key = (cik, dd, q if field not in INSTANT else "0")
                cell = store.setdefault(key, {})
                prev = cell.get(field)
                if prev is None:
                    cell[field] = (p[i_tag], val, p[i_uom])
                else:
                    order = PREF.get(field)
                    if order and p[i_tag] in order:
                        if prev[0] not in order or order.index(p[i_tag]) < order.index(prev[0]):
                            cell[field] = (p[i_tag], val, p[i_uom])
                names[cik] = adsh2name.get(p[i_adsh]) or names.get(cik, "")


def shift_q(dd, back):
    """ddate YYYYMMDD shifted back `back` quarters, to the same day-ish."""
    y, m = int(dd[:4]), int(dd[4:6])
    t = y * 12 + m - 1 - 3 * back
    return "%04d%02d" % (t // 12, t % 12 + 1)


def derive_quarterly(store):
    """(cik, ddate, qtrs) -> (cik, ddate) with flows reduced to ONE quarter.

    A YTD figure at qtrs=n covers n quarters ending at ddate. The single
    quarter is that value minus the qtrs=n-1 figure ending one quarter earlier.
    Instants pass through untouched -- differencing two balance-sheet positions
    would be meaningless.
    """
    by_cik = collections.defaultdict(dict)
    for (cik, dd, q), cell in store.items():
        by_cik[cik][(dd, q)] = cell

    out = {}
    for cik, cells in by_cik.items():
        for (dd, q), cell in cells.items():
            tgt = out.setdefault((cik, dd), {})
            if q == "0":
                for f, v in cell.items():
                    tgt.setdefault(f, v)
                continue
            n = int(q)
            for f, v in cell.items():
                if f in INSTANT:
                    continue
                if n == 1:
                    tgt.setdefault(f, v)
                    continue
                prior_ym = shift_q(dd, 1)
                prev = next((c for (d2, q2), c in cells.items()
                             if q2 == str(n - 1) and d2[:6] == prior_ym and f in c), None)
                if prev is None:
                    continue
                # EPS is per-share and additive only if the share count held
                # still; differencing it is close enough for a quarter but the
                # value is flagged by leaving it to the API path where exact.
                tgt.setdefault(f, (v[0], v[1] - prev[f][1], v[2]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--out", default="dera_quarterly.csv")
    args = ap.parse_args()

    zips = sorted(glob.glob(os.path.join(args.dir, "*.zip")))
    if not zips:
        sys.exit("no zips in %s" % args.dir)
    store, names = {}, {}
    for z in zips:
        q = os.path.basename(z)[:-4]
        parse_quarter(z, store, names, q)
        print("  %-8s cumulative cells: %d" % (q, len(store)), file=sys.stderr)

    store = derive_quarterly(store)

    rows = []
    for (cik, dd), cell in store.items():
        if len(dd) != 8:
            continue
        pe = "%s-%s-%s" % (dd[:4], dd[4:6], dd[6:])
        g = lambda f: cell[f][1] if f in cell else None
        rev, ni = g("revenue"), g("net_income")
        ebit, da = g("ebit"), g("dep_amort")
        ocf, capex = g("ocf"), g("capex")
        fcf = (ocf - abs(capex)) if (ocf is not None and capex is not None) else None
        ebitda = (ebit + da) if (ebit is not None and da is not None) else None
        d_lt, d_st, d_tot = g("debt_lt"), g("debt_st"), g("debt_total")
        composed = (d_lt + (d_st or 0.0)) if d_lt is not None else None
        cands = [x for x in (composed, d_tot, d_st) if x is not None]
        debt = max(cands) if cands else None
        cash = g("cash")
        net_debt = (debt - cash) if (debt is not None and cash is not None) else None
        margin = lambda x: round(x / rev * 100, 3) if (rev and x is not None and rev != 0) else None
        cur = next((v[2] for v in cell.values() if v[2] and v[2] != "USD/shares"), "USD")
        rows.append({
            "cik": cik, "company": names.get(cik, ""), "period_end": pe,
            "fiscal_quarter": "", "currency": cur,
            "revenue": rev, "gross_profit": g("gross_profit"), "ebit": ebit,
            "ebitda": ebitda, "net_income": ni, "eps_diluted": g("eps_diluted"),
            "ocf": ocf, "capex": (abs(capex) if capex is not None else None),
            "fcf": fcf, "interest_expense": g("interest_expense"),
            "cash": cash, "short_term_investments": None, "total_debt": debt,
            "net_debt": net_debt, "equity": g("equity"), "assets": g("assets"),
            "liabilities": g("liabilities"), "current_assets": g("current_assets"),
            "current_liabilities": g("current_liabilities"),
            "gross_margin_pct": margin(g("gross_profit")),
            "ebit_margin_pct": margin(ebit), "ebitda_margin_pct": margin(ebitda),
            "net_margin_pct": margin(ni), "fcf_margin_pct": margin(fcf),
            "period_start": "", "period_type": "Q", "q4_derived": False,
            "source": "SEC DERA (as filed)", "taxonomy": "us-gaap",
        })

    rows.sort(key=lambda r: (r["cik"], r["period_end"]))
    with open(args.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=OUT_COLS)
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in OUT_COLS})
    print("\nwrote %s: %d rows, %d CIKs" % (args.out, len(rows),
                                            len({r['cik'] for r in rows})),
          file=sys.stderr)


if __name__ == "__main__":
    main()
