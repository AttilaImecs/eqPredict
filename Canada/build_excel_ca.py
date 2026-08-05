#!/usr/bin/env python3
"""
STEP 4b of 4 (Canada) -- Assemble the CSVs into the TSX Composite workbook.

Reads:  universe_ca.csv, data_quarterly.csv, data_monthly.csv, data_snapshot.csv
Writes: TSX_Composite_Financials_5Y.xlsx

Adapted from the US build_excel.py. Three deliberate differences:

  1. M_MEASURES drops market_cap_est / pe_trailing_est. Those need a monthly
     share-count history, which is not available for TSX names; the current
     figures are on the Snapshot sheet instead.
  2. Coverage judges completeness on ANNUAL YEARS, not quarters. The US rule
     wanted 18 of 20 quarters, which no Canadian company can reach, so it
     flagged every single one as incomplete and conveyed nothing.
  3. The README sheet carries Canada-specific caveats: shallow quarterly
     history, mixed CAD/USD reporting, and which rows are as-filed.

Everything else -- the wide one-row-per-ticker layout, annual totals, the
seasonally adjusted FY projection, millions scaling -- is shared with the US
pipeline unchanged.
"""

import os
import sys

import pandas as pd

from ca_io import read_csv as ca_read_csv
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

OUT = "TSX_Composite_Financials_5Y.xlsx"
MAX_ROWS = 1_000_000  # safety margin under Excel's 1,048,576

HEADER_FILL = PatternFill("solid", fgColor="1F3864")
HEADER_FONT = Font(color="FFFFFF", bold=True, size=10)

# Money columns are converted to millions in the DATA (see to_millions), so the
# format is a plain number. The old '#,##0,,"M"' kept full units in the cell and
# only displayed them scaled, which meant every formula referencing a cell got
# 391035000000 while the screen said "391,035M".
MONEY = "#,##0.##"
MONEY_SCALE = 1e6
PCT = "0.00"
NUM = "#,##0.00"

FORMATS = {
    "revenue": MONEY, "gross_profit": MONEY, "ebit": MONEY, "ebitda": MONEY,
    "net_income": MONEY, "market_cap": MONEY, "market_cap_est": MONEY,
    "gross_margin_pct": PCT, "ebit_margin_pct": PCT, "ebitda_margin_pct": PCT,
    "net_margin_pct": PCT, "profit_margin_pct": PCT, "operating_margin_pct": PCT,
    "dividend_yield": PCT, "payout_ratio": PCT,
    "open": NUM, "high": NUM, "low": NUM, "close": NUM, "adj_close": NUM,
    "price": NUM, "eps_diluted": NUM, "eps_trailing": NUM, "eps_forward": NUM,
    "pe_trailing": NUM, "pe_forward": NUM, "pe_trailing_est": NUM,
    "beta": NUM, "ev_to_ebitda": NUM, "price_to_book": NUM,
    "volume": "#,##0", "shares_outstanding": "#,##0",
}

# The combined Timeline sheet prefixes every measure with its source, so the
# format table needs the prefixed names too.
for _base, _fmt in list(FORMATS.items()):
    FORMATS[f"Q_{_base}"] = _fmt
    FORMATS[f"M_{_base}"] = _fmt

Q_MEASURES = [
    "revenue", "gross_profit", "ebit", "ebitda", "net_income", "eps_diluted",
    "gross_margin_pct", "ebit_margin_pct", "ebitda_margin_pct", "net_margin_pct",
    "q4_derived",
]
# open/high/low are intentionally excluded -- monthly OHLC adds three columns
# per month for little analytical value at this frequency. They remain in
# data_monthly.csv if they are ever wanted.
# The Canadian monthly feed carries price and volume only. Market cap and
# trailing P/E live on the Snapshot sheet instead: share counts are available
# for the current date but not as a monthly history, and multiplying an old
# close by today's share count would invent a series.
M_MEASURES = ["close", "adj_close", "volume"]


# number formats are looked up by the metric part of a column name, so
# `revenue_FY2026-Q1` and `market_cap_est_2026-06` both resolve correctly
_FMT_KEYS = sorted(FORMATS, key=len, reverse=True)


def fmt_for(col):
    col = str(col)
    if col in FORMATS:
        return FORMATS[col]
    for k in _FMT_KEYS:
        if col.startswith(k + "_"):
            return FORMATS[k]
    return None


def to_millions(df):
    """
    Rescale every currency column to millions so the cell value IS the number
    shown. Two decimals are kept rather than whole millions: a small-cap with
    $400k of revenue would otherwise round to 0 and look like a data gap.

    Per-share figures (EPS, prices), percentages and share counts are left
    alone -- they are not currency totals and rescaling them would be wrong.
    """
    out = df.copy()
    for c in out.columns:
        if fmt_for(c) == MONEY and pd.api.types.is_numeric_dtype(out[c]):
            out[c] = (out[c] / MONEY_SCALE).round(2)
    return out


def style(ws, df, freeze="A2"):
    ws.freeze_panes = freeze
    ws.auto_filter.ref = ws.dimensions

    for cell in ws[1]:
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.row_dimensions[1].height = 30

    # Writing a number format cell-by-cell is O(rows x cols). On the full
    # universe the wide sheet is ~3,700 x ~750 = 2.8M cells, which takes minutes
    # and a lot of memory in openpyxl. Past a threshold, keep the column widths
    # and skip the cosmetic formatting.
    light = df.shape[0] * df.shape[1] > 400_000

    for i, col in enumerate(df.columns, start=1):
        letter = get_column_letter(i)
        width = max(len(str(col)) + 3, 11)
        if df[col].dtype == object:
            sample = df[col].astype(str).head(300)
            width = min(max(width, int(sample.str.len().max() or 10) + 2), 42)
        ws.column_dimensions[letter].width = width

        if light:
            continue
        fmt = fmt_for(col)
        if fmt:
            for cell in ws[letter][1:]:
                cell.number_format = fmt


ANNUAL_ADDITIVE = ["revenue", "gross_profit", "ebit", "ebitda", "net_income", "eps_diluted"]
ANNUAL_MARGINS = [
    ("gross_margin_pct", "gross_profit"),
    ("ebit_margin_pct", "ebit"),
    ("ebitda_margin_pct", "ebitda"),
    ("net_margin_pct", "net_income"),
]
PROJ_MIN_SHARE, PROJ_MAX_SHARE = 0.05, 0.95


def build_annual(q):
    """
    Full-year columns per fiscal year, plus a seasonally-adjusted projection for
    the year in progress.

    Full years (`revenue_FY2024`) are the sum of the four fiscal quarters, and
    are emitted ONLY when all four are present for that metric. A partial sum
    would silently understate the year, which is worse than a blank.

    The projection (`revenue_FY2026E`) scales year-to-date actuals by the share
    those same quarters represented in the prior full year:

        FY26E = (Q1+Q2 actual) / [(Q1+Q2 prior) / (full prior year)]

    That removes seasonality, which naive annualising does not. A retailer that
    earns 40% of its year in Q4 would be badly understated by YTD x 4/n.

    Margins are recomputed from the annual totals, never averaged across
    quarters -- an average of four quarterly margins is not the annual margin
    unless every quarter happens to be the same size.
    """
    d = q.copy()

    # Directly reported full years, from filers with no quarterly data at all --
    # foreign private issuers filing a 20-F. These are as-filed annual figures,
    # so they are preferred over anything summed.
    reported = pd.DataFrame()
    if "period_type" in d.columns:
        fy_rows = d[d["period_type"].astype(str).str.upper() == "FY"].copy()
        d = d[d["period_type"].astype(str).str.upper() != "FY"]
        if not fy_rows.empty:
            fy_rows["fy"] = pd.to_numeric(
                fy_rows["fiscal_quarter"].str.extract(r"FY(\d{4})")[0], errors="coerce")
            reported = fy_rows.dropna(subset=["fy"])
            reported["fy"] = reported["fy"].astype(int)

    d["fy"] = pd.to_numeric(d["fiscal_quarter"].str.extract(r"(?:FY)?(\d{4})-Q")[0],
                            errors="coerce")
    d["qn"] = pd.to_numeric(d["fiscal_quarter"].str.extract(r"-Q(\d)")[0], errors="coerce")
    d = d.dropna(subset=["fy", "qn"])
    if d.empty and reported.empty:
        return pd.DataFrame()
    d["fy"] = d["fy"].astype(int)
    d["qn"] = d["qn"].astype(int)
    d = d[d["qn"].between(1, 4)]

    if d.empty:
        # nothing but as-filed annual figures: no sums, no projections
        blocks = []
        for meas in ANNUAL_ADDITIVE + [n for n, _ in ANNUAL_MARGINS]:
            if meas not in reported.columns:
                continue
            t = reported.pivot_table(index="ticker", columns="fy", values=meas,
                                     aggfunc="first")
            t = t.reindex(sorted(t.columns), axis=1)
            t.columns = [f"{meas}_FY{int(c)}" for c in t.columns]
            blocks.append(t)
        out = pd.concat(blocks, axis=1) if blocks else pd.DataFrame()
        out.index.name = "ticker"
        return out.dropna(axis=1, how="all")

    totals, projections = {}, {}

    # Project ONLY the fiscal year currently in progress -- the ticker's latest.
    # Without this, a past year that happens to be sparse for one metric (EBITDA
    # is often missing a quarter) gets a projection alongside its own completed
    # neighbours, e.g. a stray ebitda_FY2025E next to ebitda_FY2025.
    last_fy = d.groupby("ticker")["fy"].max()

    for meas in ANNUAL_ADDITIVE:
        if meas not in d.columns:
            continue
        piv = (d.pivot_table(index=["ticker", "fy"], columns="qn", values=meas, aggfunc="first")
                 .reindex(columns=[1, 2, 3, 4]))
        filled = piv.notna().sum(axis=1)

        summed = piv.sum(axis=1, min_count=1).where(filled == 4).unstack("fy")
        # As-filed annual figures win wherever both exist. For domestic filers
        # there is no overlap -- an FY row is only emitted for a fiscal year with
        # no quarterly coverage at all -- so this is purely additive.
        if not reported.empty and meas in reported.columns:
            rep = reported.pivot_table(index="ticker", columns="fy", values=meas,
                                       aggfunc="first")
            summed = summed.reindex(index=summed.index.union(rep.index),
                                    columns=summed.columns.union(rep.columns))
            summed = rep.reindex_like(summed).combine_first(summed)
        totals[meas] = summed

        proj = {}
        for key, n in filled.items():
            if n == 0 or n == 4:
                continue                       # nothing to project, or already complete
            tk, fy = key
            if fy != last_fy.get(tk):
                continue                       # only the year in progress
            prior_key = (tk, fy - 1)
            if prior_key not in piv.index:
                continue
            prior = piv.loc[prior_key]
            if prior.notna().sum() != 4:
                continue                       # need a complete prior year to get the shape
            have = piv.loc[key].dropna()
            prior_total = prior.sum()
            if prior_total <= 0:
                continue                       # loss-making prior year: ratio is meaningless
            share = prior[have.index].sum() / prior_total
            # A share outside these bounds means the prior-year mix is too
            # lopsided (or sign-flipped) for the scaling to be trustworthy.
            if not (PROJ_MIN_SHARE <= share <= PROJ_MAX_SHARE):
                continue
            proj[key] = have.sum() / share
        projections[meas] = (pd.Series(proj).unstack() if proj else pd.DataFrame())

    for name, numer in ANNUAL_MARGINS:
        for store in (totals, projections):
            if numer in store and "revenue" in store and not store[numer].empty:
                store[name] = store[numer] / store["revenue"].replace(0, pd.NA) * 100

    blocks = []
    for meas in ANNUAL_ADDITIVE + [n for n, _ in ANNUAL_MARGINS]:
        t = totals.get(meas)
        if t is not None and not t.empty:
            t = t.reindex(sorted(t.columns), axis=1)
            t.columns = [f"{meas}_FY{int(c)}" for c in t.columns]
            blocks.append(t)
        p = projections.get(meas)
        if p is not None and not p.empty:
            p = p.reindex(sorted(p.columns), axis=1)
            p.columns = [f"{meas}_FY{int(c)}E" for c in p.columns]
            blocks.append(p)

    if not blocks:
        return pd.DataFrame()
    out = pd.concat(blocks, axis=1)
    out = out.dropna(axis=1, how="all")     # drop year columns nobody has data for

    # Provenance for the projection: which year it covers and how many quarters
    # of actuals it rests on. A projection built on one quarter is a far weaker
    # claim than one built on three, and the reader cannot tell them apart from
    # the value alone.
    rev = (d.pivot_table(index=["ticker", "fy"], columns="qn", values="revenue",
                         aggfunc="first").reindex(columns=[1, 2, 3, 4]).notna().sum(axis=1))
    meta = {}
    for tk, fy in last_fy.items():
        n = int(rev.get((tk, fy), 0))
        if 0 < n < 4:
            meta[tk] = {"fy_projected": f"FY{fy}E", "proj_quarters_actual": n}
    if meta:
        out = pd.DataFrame(meta).T.join(out, how="outer")
    return out


def period_sort_key(label):
    """
    Chronological sort key for a period label.

    Handles `FY2026-Q3` (fiscal quarter), `2026-Q3` (calendar fallback used when
    a company's current fiscal year has no 10-K yet) and `2026-06` (month).
    """
    s = str(label)
    if s.startswith("FY"):
        s = s[2:]
    head, _, tail = s.partition("-")
    try:
        year = int(head)
    except ValueError:
        return (9999, 99)
    if tail.startswith("Q"):
        try:
            return (year, int(tail[1:]))
        except ValueError:
            return (year, 99)
    try:
        return (year, int(tail))
    except ValueError:
        return (year, 99)


def build_wide(q, m):
    """
    One row per ticker, every metric-period pair its own column.

    Column names are `<metric>_<period>` -- `revenue_FY2026-Q1`, `close_2026-06`
    -- grouped by metric and running chronologically within each metric, so a
    single line item is a contiguous block of columns.

    Periods are LABELS, not raw dates, on purpose. Fiscal quarter ends differ by
    company (Apple's Q3 ends 2026-06-27, Microsoft's 2026-06-30); keying columns
    on raw dates would produce thousands of near-empty columns instead of one
    aligned set. `FY2026-Q3` puts both companies in the same column.
    """
    def pivot(df, period_col, date_col, measures, prefixes):
        if df.empty:
            return pd.DataFrame(), []
        # A handful of companies change their fiscal year-end mid-window, so two
        # genuinely different periods can carry the same label (Amerityre moving
        # from a July to a June quarter-end). Rather than keep an arbitrary one,
        # rank by how much data each row actually has and keep the fullest.
        d = df.copy()
        present = [c for c in measures if c in d.columns]
        d["_filled"] = d[present].notna().sum(axis=1) if present else 0
        d = (d.sort_values(["ticker", period_col, "_filled"])
               .drop_duplicates(["ticker", period_col], keep="last")
               .drop(columns="_filled"))
        # Order the period labels by parsing the label, NOT by the underlying
        # dates. Companies on offset fiscal years break a date-derived sort:
        # Costco's FY2021-Q4 ends 2021-08-29, earlier than a calendar filer's
        # FY2021-Q3 ending 2021-09-30, which would put Q4 before Q3.
        order = sorted(d[period_col].dropna().unique(), key=period_sort_key)
        blocks, names = [], []
        for meas in measures:
            if meas not in d.columns:
                continue
            p = d.pivot(index="ticker", columns=period_col, values=meas)
            p = p.reindex(columns=[c for c in order if c in p.columns])
            p.columns = [f"{prefixes}{meas}_{c}" for c in p.columns]
            blocks.append(p)
            names.extend(p.columns)
        return (pd.concat(blocks, axis=1) if blocks else pd.DataFrame()), names

    # Full-year rows belong only in the annual block. Left in the quarterly
    # pivot, their `FY2024` label collides with the annual column of the same
    # name and the join fails outright.
    q_only = (q[q["period_type"].astype(str).str.upper() != "FY"]
              if "period_type" in q.columns else q)

    qw, _ = pivot(q_only, "fiscal_quarter", "period_end", Q_MEASURES, "")
    mw, _ = pivot(m, "month", "date", M_MEASURES, "")

    ident = (
        pd.concat([q[["ticker", "company", "currency"]],
                   m[["ticker", "company"]]], ignore_index=True)
        .drop_duplicates("ticker")
        .set_index("ticker")
    )
    # State the scale on the sheet itself. Anyone reading revenue_FY2024 =
    # 391,035 needs to know that is millions without hunting through the README,
    # and EPS/prices are the exception that a blanket note would obscure.
    ident["units"] = "financials in millions; EPS & prices actual"

    # Annual totals lead the sheet: they are the seasonality-free view and the
    # first thing you want when comparing companies, so they sit immediately
    # after the identifiers rather than behind ~250 quarterly columns.
    ann = build_annual(q)

    wide = ident.join(ann, how="outer").join(qw, how="outer").join(mw, how="outer")
    # An outer join between indexes that are not identical drops the index name,
    # which only happens once some tickers have prices but no fundamentals --
    # invisible on a clean sample, fatal on a real one.
    wide.index.name = "ticker"
    return wide.reset_index().sort_values("ticker").reset_index(drop=True)


def readme_frame(counts):
    rows = [
        ("UNITS", ""),
        ("", "All currency totals - revenue, gross profit, EBIT, EBITDA, net income,"),
        ("", "market cap - are in MILLIONS of the filing currency. The cell value is the"),
        ("", "number you see; there is no hidden scaling factor, so formulas referencing"),
        ("", "these cells get millions too."),
        ("", "NOT scaled: diluted EPS, share prices, margins (already %), volume."),
        ("", ""),
        ("WHAT THIS IS", ""),
        ("", "Financial data for the S&P/TSX Composite Index constituents."),
        ("", "Annual figures 5-6 years deep; quarterly ~6 quarters (see caveats)."),
        ("", ""),
        ("SHEETS", ""),
        ("Snapshot", "Current valuation per company: market cap, price, trailing & forward P/E,"),
        ("", "dividend yield, TTM margins, beta, EV/EBITDA. One row per ticker."),
        ("Data", "ONE ROW PER TICKER. Every metric-period pair is its own column, named"),
        ("", "<metric>_<period>, e.g. revenue_FY2026-Q1, ebit_FY2025-Q4, close_2026-06."),
        ("", ""),
        ("  FULL-YEAR cols", "revenue_FY2024 (no -Qn suffix) = the fiscal year total, the sum of its"),
        ("", "four quarters. These are the seasonality-free view - use them to compare"),
        ("", "companies or years. BLANK means a quarter was missing; a partial sum would"),
        ("", "understate the year, so nothing is written rather than something wrong."),
        ("  PROJECTED cols", "revenue_FY2026E - trailing E - is an ESTIMATE for the year still in"),
        ("", "progress, not reported data. Year-to-date actuals are scaled by the share"),
        ("", "those same quarters made up of the PRIOR full year, which corrects for"),
        ("", "seasonality: FY26E = (Q1+Q2 actual) / [(Q1+Q2 prior) / (full prior year)]."),
        ("", "Naive annualising (YTD x 4/n) would understate a Q4-heavy retailer badly -"),
        ("", "Costco projects 302bn this way vs 277bn annualised naively."),
        ("", "fy_projected and proj_quarters_actual say which year is projected and how"),
        ("", "many quarters of real data it rests on. ONE quarter is a weak basis - treat"),
        ("", "those projections with much more caution than a three-quarter one."),
        ("", "No projection is made where the prior year was loss-making or its quarterly"),
        ("", "mix too lopsided for the scaling to mean anything."),
        ("", ""),
        ("", "Columns are grouped by metric and run chronologically inside each group,"),
        ("", "so one line item is a contiguous block you can slice in one range."),
        ("", "Quarterly metrics: revenue, gross_profit, ebit, ebitda, net_income,"),
        ("", "eps_diluted, the four *_margin_pct, and q4_derived."),
        ("", "Monthly metrics: close, adj_close, volume, market_cap_est, pe_trailing_est."),
        ("", "close = actual traded price; use it for market cap and P/E. adj_close is"),
        ("", "restated for dividends and splits; use it for returns and volatility."),
        ("", "Monthly open/high/low are omitted here - see data_monthly.csv if needed."),
        ("", "Periods are LABELS, not raw dates - fiscal quarter ends differ by company"),
        ("", "(Apple's Q3 ends 06-27, Microsoft's 06-30), so raw dates would give"),
        ("", "thousands of near-empty columns. FY2026-Q3 aligns them."),
        ("", "The row-per-observation form is still in data_quarterly.csv/data_monthly.csv."),
        ("Universe", "Every ticker collected, flagged by index membership."),
        ("Coverage", "Per-company completeness so you can see where free data has gaps."),
        ("", ""),
        ("SOURCES (all free)", ""),
        ("Yahoo Finance", "via the yfinance library - prices, fundamentals, valuation ratios"),
        ("Wikipedia", "S&P 500 constituent list, Fortune 500 list"),
        ("Nasdaq Trader", "official Nasdaq-listed symbol directory"),
        ("SEC EDGAR", "company_tickers.json, used to map Fortune 500 names to tickers"),
        ("", ""),
        ("IMPORTANT CAVEATS", ""),
        ("QUARTERLY DEPTH", "Canadian quarterly history is only ~6 quarters deep, NOT 20."),
        ("", "There is no free XBRL feed for SEDAR+, and the SEC holds only ANNUAL data"),
        ("", "for Canadian issuers (a 40-F once a year; interim results go on a 6-K with"),
        ("", "little tagging). Quarters come from Yahoo, which reaches ~6 back."),
        ("", "THE ANNUAL COLUMNS ARE THE RELIABLE PART - they go back 5-6 years."),
        ("CURRENCY", "MIXED. Most report in CAD, but Agnico Eagle and Nutrien report in USD."),
        ("", "Figures are NOT FX-converted. Check the currency column before summing"),
        ("", "or ranking across companies."),
        ("SOURCE COLUMN", "'SEC XBRL (IFRS)' = as-filed from the 40-F. 'yfinance' = vendor data,"),
        ("", "used for all quarters and for annual years the SEC does not cover."),
        ("CROSS-LISTING", "Only about half the Composite files with the SEC. TSX-only names"),
        ("", "rely entirely on Yahoo and have no as-filed annual figures."),
        ("Monthly fundamentals", "Do not exist. Companies report revenue/EBIT/net income QUARTERLY."),
        ("", "Only price, market cap and P/E are genuinely monthly. In Timeline the Q_"),
        ("", "columns are blank on month rows ON PURPOSE - they are not forward-filled,"),
        ("", "because repeating a quarterly figure across three months would invent data."),
        ("Q_q4_derived", "TRUE means that quarter was DERIVED as FY minus Q1+Q2+Q3, not filed."),
        ("", "Companies file no 10-Q for their fourth quarter, so Q4 exists only inside"),
        ("", "the annual 10-K. Validated against independent data: max difference 2.8%."),
        ("Forward P/E", "Is a CURRENT snapshot only. Historical forward P/E requires paid"),
        ("", "analyst-estimate data (FactSet, Capital IQ, Bloomberg) - not available free."),
        ("market_cap_est", "= monthly close x CURRENT shares outstanding. Historical buybacks and"),
        ("", "issuance are not reflected, so older months drift from true market cap."),
        ("pe_trailing_est", "= monthly close / trailing-4-quarter diluted EPS as known at that date."),
        ("EBIT gaps", "Banks, insurers and REITs often do not report an EBIT line. Blank is"),
        ("", "correct for them, not an error."),
        ("Fortune 500", "Roughly a quarter of the list is private or foreign-owned and has no"),
        ("", "US ticker. See fortune500_unmatched.csv for those."),
        ("Survivorship", "Index membership is as of the collection date. Companies that left an"),
        ("", "index during the 5y window are not included."),
        ("Currency", "Reported in each company's filing currency - see the currency column."),
        ("", "Not FX-converted."),
        ("", ""),
        ("COLLECTION SUMMARY", ""),
    ]
    rows += [(k, str(v)) for k, v in counts.items()]
    return pd.DataFrame(rows, columns=["Item", "Detail"])


def main():
    missing = [f for f in ("data_quarterly.csv", "data_monthly.csv", "data_snapshot.csv")
               if not os.path.exists(f)]
    if missing:
        sys.exit(f"Missing {missing} -- run fetch_financials.py first")

    print("[excel] loading checkpoints ...")
    q = ca_read_csv("data_quarterly.csv").drop_duplicates(["ticker", "period_end"])
    m = ca_read_csv("data_monthly.csv").drop_duplicates(["ticker", "month"])
    s = ca_read_csv("data_snapshot.csv").drop_duplicates("ticker")
    uni = ca_read_csv("universe_ca.csv") if os.path.exists("universe_ca.csv") else pd.DataFrame()

    # Fundamentals keep 6 years, prices 5. The extra year of quarters exists so
    # the earliest fiscal year can be completed -- a 5-year window starting
    # mid-2021 has no Q1/Q2 2021 for a calendar-year filer, leaving the FY2021
    # annual column empty.
    q["period_end"] = pd.to_datetime(q["period_end"], errors="coerce")
    m["date"] = pd.to_datetime(m["date"], errors="coerce")
    q_cutoff = pd.Timestamp.today() - pd.DateOffset(years=6)
    m_cutoff = pd.Timestamp.today() - pd.DateOffset(years=5)
    q = q[q["period_end"] >= q_cutoff].sort_values(["ticker", "period_end"])
    m = m[m["date"] >= m_cutoff].sort_values(["ticker", "date"])
    q["period_end"] = q["period_end"].dt.date
    m["date"] = m["date"].dt.date

    s = s.sort_values("market_cap", ascending=False, na_position="last")

    # coverage report
    print("[excel] building coverage report ...")
    # Completeness is judged on ANNUAL years, not quarters. The US rule asked
    # for 18 of 20 quarters, which no Canadian company can reach -- SEDAR+ has
    # no free XBRL feed and Yahoo only goes ~6 quarters back, so that rule
    # reported every single company as incomplete and told the reader nothing.
    qq = q[q.get("period_type", "Q") == "Q"] if "period_type" in q.columns else q
    fy = q[q["period_type"] == "FY"] if "period_type" in q.columns else q.iloc[0:0]

    cov = (
        s[["ticker", "company"]]
        .merge(qq.groupby("ticker").agg(
            quarters=("period_end", "count"),
            quarters_with_revenue=("revenue", "count"),
        ), on="ticker", how="left")
        .merge(fy.groupby("ticker").agg(
            annual_years=("period_end", "count"),
            years_with_revenue=("revenue", "count"),
            years_as_filed=("source", lambda x: int((x == "SEC XBRL (IFRS)").sum())),
        ), on="ticker", how="left")
        .merge(m.groupby("ticker").agg(months=("month", "count")), on="ticker", how="left")
        .fillna(0)
    )
    for c in ("quarters", "quarters_with_revenue", "annual_years",
              "years_with_revenue", "years_as_filed", "months"):
        cov[c] = cov[c].astype(int)
    cov["complete"] = (cov["years_with_revenue"] >= 4) & (cov["months"] >= 55)

    print("[excel] pivoting to one row per ticker ...")
    wide = build_wide(q, m)
    if len(wide.columns) > 16_384:
        sys.exit(f"Wide sheet needs {len(wide.columns)} columns; Excel allows 16,384")

    counts = {
        "Tickers with any data": s["ticker"].nunique(),
        "Data rows (1 per ticker)": len(wide),
        "Data columns": len(wide.columns),
        "Companies with 4+ annual years": int(cov["complete"].sum()),
        "Built on": pd.Timestamp.today().strftime("%Y-%m-%d"),
    }

    print(f"[excel] writing {OUT} ...")
    with pd.ExcelWriter(OUT, engine="openpyxl") as xw:
        sheets = [
            ("README", readme_frame(counts)),
            ("Snapshot", s),
        ]
        sheets.append(("Data", wide))
        if not uni.empty:
            sheets.append(("Universe", uni))
        sheets.append(("Coverage", cov))

        for name, df in sheets:
            df = to_millions(df)
            df.to_excel(xw, sheet_name=name[:31], index=False)
            # freeze the ticker/company/currency block so labels stay visible
            # while scrolling across several hundred period columns
            style(xw.sheets[name[:31]], df, freeze="D2" if name == "Data" else "A2")

        rm = xw.sheets["README"]
        rm.column_dimensions["A"].width = 24
        rm.column_dimensions["B"].width = 95
        for cell in rm["A"][1:]:
            cell.font = Font(bold=True, size=10)
        for cell in rm["B"][1:]:
            cell.alignment = Alignment(wrap_text=False, vertical="top")

    size = os.path.getsize(OUT) / 1e6
    print(f"\n[excel] DONE -> {OUT}  ({size:.1f} MB)")
    for k, v in counts.items():
        print(f"[excel]   {k}: {v}")


if __name__ == "__main__":
    main()
