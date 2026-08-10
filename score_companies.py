#!/usr/bin/env python3
"""
STEP 4 -- Score every company in the universe on a 0-100 rules-based scale.

Reads:  data_quarterly.csv, data_monthly.csv, data_snapshot.csv, universe.csv
Writes: company_scores.csv  (one row per ticker, score + every input that fed it)

WHAT THIS IS
  A transparent, rules-based screen. Every point is traceable to a named rule
  with a published threshold -- no regression, no fitted weights, no lookahead.
  The output ranks companies on the financial history we actually hold.

WHAT THIS IS NOT
  It is NOT investment advice and it does not know what a company is worth.
  It scores reported income-statement history, valuation multiples and price
  behaviour. Two blind spots are structural and cannot be closed with the data
  in this repo:

    * NO BALANCE SHEET. We hold no debt, cash, equity or share-count history,
      so leverage, interest coverage, liquidity and ROE/ROIC are invisible. A
      company can score 85 here and still be one covenant from insolvency.
      This is the single most important caveat in the file.
    * NO CASH FLOW. No FCF, no capex, no cash conversion. Earnings quality in
      the accrual sense cannot be tested, so a company converting none of its
      net income to cash looks identical to one converting all of it.

  Treat the score as "rank this list for me", never as "buy this".

THE 3-YEAR WINDOW
  Growth is measured TTM-vs-TTM three years apart, which needs 16 quarters.
  Where a company has only 12-15 quarters the comparison falls back to two
  years and `growth_basis` records which was used -- a 2y CAGR and a 3y CAGR
  are not the same number and the column exists so they are never silently
  mixed. All level metrics (margins, valuation) use the most recent TTM.

SCALE -- why 0-100 and not 1-50
  Five pillars of 20 points each. 0-100 reads as a percentage without
  explanation, and 20 points per pillar divides cleanly across the 4-5 rules
  inside each one without fractional weights. A 1-50 scale would compress
  every pillar into 10 points and force ties.

    Growth        20   is the business getting bigger
    Profitability 20   does it convert revenue into profit
    Quality       20   is that profit stable, improving, and not diluted away
    Valuation     20   what are you paying for it
    Momentum/Risk 20   what the market says, and how violently it says it

  A pillar with no usable data is DROPPED and the total renormalised over the
  pillars that scored -- so a company is never punished for a gap in our data.
  `confidence` reports how much of the rubric actually ran; read any score with
  a confidence below ~0.6 as provisional.

KNOWN BIAS -- THE SCORE TILTS TOWARDS FINANCIALS
  Measured on this universe, median score by sector runs from 72.8
  (Financials) down to 60.8 (Consumer Staples), and Financials are 3.0x
  over-represented in the top 200 while Energy and Materials are absent
  entirely. This is mechanical, not a finding:

    * margin bands are absolute, and median net margin is 21.2% in Financials
      against 6.6% in Consumer Staples -- a grocer at its sector median scores
      mid-band while a bank at its sector median maxes the rule;
    * P/E bands are absolute, and banks structurally trade cheap.

  Fixing it properly needs an industry classification for the whole universe.
  `sector` is populated for only the ~500 S&P 500 names scraped from
  Wikipedia, so `sector_rank_pct` -- percentile within sector -- is emitted
  for those and blank elsewhere. Compare within a sector where you can; the
  absolute score is for ranking the whole list. SEC assigns every filer an SIC
  code, so pulling it in build_universe.py would close this for good.

Usage:
  python3 score_companies.py                    # score everything
  python3 score_companies.py --top 25           # print a leaderboard
  python3 score_companies.py --min-confidence 0.7
  python3 score_companies.py --sp500-only
"""

import argparse
import collections
import csv
import statistics
import sys

csv.field_size_limit(10**7)

Q_FILE = "data_quarterly.csv"
M_FILE = "data_monthly.csv"
S_FILE = "data_snapshot.csv"
U_FILE = "universe.csv"
OUT = "company_scores.csv"

# 16 quarters gives a true 3-year TTM-vs-TTM comparison; 12 is the fallback.
# Below MIN_QUARTERS there is not enough history to say anything and the
# company is returned NOT RATED rather than scored badly on absent data.
FULL_WINDOW = 16
FALLBACK_WINDOW = 12
MIN_QUARTERS = 8

PILLARS = ["growth", "profitability", "quality", "valuation", "momentum"]
PILLAR_MAX = 20.0


def num(v):
    """CSV -> float, treating '' and NaN alike as missing."""
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def band(value, table):
    """Higher is better. table = [(min_value, points), ...] descending."""
    if value is None:
        return None
    for lo, pts in table:
        if value >= lo:
            return pts
    return 0.0


def band_low(value, table):
    """Lower is better. table = [(max_value, points), ...] ascending."""
    if value is None:
        return None
    for hi, pts in table:
        if value <= hi:
            return pts
    return 0.0


# --------------------------------------------------------------------------
# Load
# --------------------------------------------------------------------------

def load_quarters():
    """ticker -> chronologically sorted quarterly rows, deduped on period_end.

    FY rows are excluded here. They exist only for foreign private issuers with
    no quarterly coverage (see HANDOFF.md); summing them into a TTM alongside
    real quarters would double-count a year.
    """
    seen = collections.defaultdict(dict)
    for r in csv.DictReader(open(Q_FILE)):
        if r["period_type"] != "Q":
            continue
        seen[r["ticker"]][r["period_end"]] = r
    return {t: sorted(d.values(), key=lambda r: r["period_end"])
            for t, d in seen.items()}


def load_prices():
    """ticker -> (sorted adj_close series, sorted dollar-volume series, pe series)."""
    px = collections.defaultdict(list)
    dv = collections.defaultdict(list)
    pe = collections.defaultdict(list)
    for r in csv.DictReader(open(M_FILE)):
        m = r["month"]
        a = num(r["adj_close"])
        if a:
            px[r["ticker"]].append((m, a))
        v, c = num(r["volume"]), num(r["close"])
        if v and c:
            dv[r["ticker"]].append((m, v * c))
        p = num(r["pe_trailing_est"])
        if p and p > 0:
            pe[r["ticker"]].append((m, p))
    for d in (px, dv, pe):
        for t in d:
            d[t].sort()
    return px, dv, pe


def load_keyed(path, key="ticker"):
    return {r[key]: r for r in csv.DictReader(open(path))}


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------

def ttm(qs, measure, offset=0):
    """Sum `measure` over the 4 quarters ending `offset` quarters back.

    Returns None unless all four are present -- a partial TTM understates the
    figure, and an understated denominator silently inflates every margin and
    multiple built on it.
    """
    end = len(qs) - offset
    win = qs[end - 4:end]
    if len(win) < 4:
        return None
    vals = [num(r[measure]) for r in win]
    if any(v is None for v in vals):
        return None
    return sum(vals)


def cagr(now, then, years):
    if now is None or then is None or now <= 0 or then <= 0:
        return None
    return ((now / then) ** (1.0 / years) - 1) * 100


# Stock-split ratios to recognise.  See split_adjust().
SPLIT_RATIOS = (2, 3, 4, 5, 6, 7, 8, 10, 15, 20)
SPLIT_TOL = 0.06


def implied_shares(qs):
    """[(index, share_count)] for quarters where the count can be trusted.

    We hold only a single current shares_outstanding snapshot, so net_income /
    eps_diluted is the only way to see a share-count TREND.

    Two filters, both load-bearing:

    * Derived-Q4 rows are EXCLUDED. fetch_sec_fundamentals.py treats
      eps_diluted as additive when deriving Q4 (FY - Q1-Q2-Q3), but EPS is
      only additive if the share count held still -- precisely what we are
      measuring. Left in, Nvidia's FY2024 Q4 implies -49bn shares.
    * The EPS floor is RELATIVE TO THE COMPANY'S OWN EPS, not a fixed 0.02.
      The ratio explodes as EPS approaches zero, and "near zero" depends on
      scale: a $0.05 quarter is unremarkable for a $0.30 filer and a
      near-breakeven event for Booking at ~$100/share. A fixed floor made
      Booking read as +1928% dilution and Allstate +731%, when Booking in
      fact retired ~14% of its shares over the window.
    """
    eps_all = [abs(v) for v in (num(r["eps_diluted"]) for r in qs) if v is not None]
    if not eps_all:
        return []
    floor = max(0.02, 0.25 * statistics.median(eps_all))
    out = []
    for i, r in enumerate(qs):
        if r.get("q4_derived", "").strip().lower() == "true":
            continue
        ni, eps = num(r["net_income"]), num(r["eps_diluted"])
        if ni is None or eps is None or abs(eps) < floor:
            continue
        s = ni / eps
        if s > 0:
            out.append((i, s))
    return out


def reject_outliers(series):
    """Drop counts more than 3x off the series median.

    A quarter whose earnings cross zero mid-window can clear the EPS floor and
    still imply a wild count. The share base of a real company does not move by
    3x in a quarter, so anything that does is an artefact, not a financing
    event.
    """
    if len(series) < 4:
        return series
    med = statistics.median([s for _, s in series])
    if med <= 0:
        return series
    return [(i, s) for i, s in series if med / 3 <= s <= med * 3]


def split_adjust(series):
    """Chain-link stock splits out of an implied share-count series.

    A split appears as a single-quarter jump by a ROUND factor -- Nvidia 10:1,
    Walmart 3:1, Alphabet 20:1 -- because as-filed EPS is not restated
    backwards. Uncorrected, Nvidia reads as +883% dilution over three years and
    earns a heavy_dilution flag it does not deserve.

    Requiring the ratio to land within 6% of a round integer is what separates
    a split from real dilution: a 10:1 split is x10.0, while UMB Financial's
    Heartland acquisition is x1.6 and correctly survives as genuine dilution.
    """
    if len(series) < 2:
        return series
    adj = [series[0]]
    factor = 1.0
    for k in range(1, len(series)):
        i, s = series[k]
        prev = series[k - 1][1]
        ratio = s / prev if prev else 1.0
        for n in SPLIT_RATIOS:
            if abs(ratio - n) / n < SPLIT_TOL:
                factor *= n
                break
            if abs(ratio - 1.0 / n) * n < SPLIT_TOL:
                factor /= n
                break
        adj.append((i, s / factor))
    return adj


def compute(t, qs, snap, px, dv, pe_hist):
    """Everything the rules need, or None where the data will not support it."""
    m = {"quarters": len(qs)}

    # --- growth basis: prefer a true 3-year comparison ---
    if len(qs) >= FULL_WINDOW:
        lag, years, m["growth_basis"] = 12, 3.0, "3y"
    elif len(qs) >= FALLBACK_WINDOW:
        lag, years, m["growth_basis"] = 8, 2.0, "2y"
    else:
        lag, years, m["growth_basis"] = None, None, "insufficient"

    rev0 = ttm(qs, "revenue")
    ni0 = ttm(qs, "net_income")
    m["revenue_ttm"] = rev0
    m["net_income_ttm"] = ni0

    rev_prior = ttm(qs, "revenue", 4)
    m["rev_yoy"] = ((rev0 / rev_prior - 1) * 100
                    if rev0 and rev_prior and rev_prior > 0 else None)

    if lag:
        m["rev_cagr"] = cagr(rev0, ttm(qs, "revenue", lag), years)
        m["eps_cagr"] = cagr(ttm(qs, "eps_diluted"), ttm(qs, "eps_diluted", lag), years)
    else:
        m["rev_cagr"] = m["eps_cagr"] = None

    # acceleration: is the latest year faster than the multi-year trend
    m["accel"] = (m["rev_yoy"] - m["rev_cagr"]
                  if m["rev_yoy"] is not None and m["rev_cagr"] is not None else None)

    # --- margins ---
    ebit0, gp0 = ttm(qs, "ebit"), ttm(qs, "gross_profit")
    m["net_margin"] = ni0 / rev0 * 100 if rev0 and rev0 > 0 and ni0 is not None else None
    m["ebit_margin"] = ebit0 / rev0 * 100 if rev0 and rev0 > 0 and ebit0 is not None else None
    m["gross_margin"] = gp0 / rev0 * 100 if rev0 and rev0 > 0 and gp0 is not None else None

    # margin trend in percentage points across the window
    m["margin_delta"] = None
    if lag and rev0 and rev0 > 0 and ni0 is not None:
        rev_then, ni_then = ttm(qs, "revenue", lag), ttm(qs, "net_income", lag)
        if rev_then and rev_then > 0 and ni_then is not None:
            m["margin_delta"] = m["net_margin"] - ni_then / rev_then * 100

    # --- consistency ---
    window = qs[-FALLBACK_WINDOW:]
    ni_vals = [num(r["net_income"]) for r in window]
    known = [v for v in ni_vals if v is not None]
    m["profitable_q"] = sum(1 for v in known if v > 0) if known else None
    m["window_q"] = len(known)

    growth_pts = []
    for i in range(max(4, len(qs) - 8), len(qs)):
        a, b = num(qs[i]["revenue"]), num(qs[i - 4]["revenue"])
        if a is not None and b and b > 0:
            growth_pts.append((a / b - 1) * 100)
    m["growth_sd"] = statistics.pstdev(growth_pts) if len(growth_pts) >= 4 else None

    # positive earnings across the three TTM slices
    ttms = [ttm(qs, "net_income", k) for k in (0, 4, 8)]
    m["pos_ttm"] = sum(1 for v in ttms if v is not None and v > 0)
    m["pos_ttm_known"] = sum(1 for v in ttms if v is not None)

    # --- share count trend (dilution / buyback) ---
    m["share_change"] = None
    if lag:
        adj = reject_outliers(split_adjust(implied_shares(qs)))
        n = len(qs)
        now_sh = [s for i, s in adj if i >= n - 4]
        then_sh = [s for i, s in adj if n - 4 - lag <= i < n - lag]
        if now_sh and then_sh:
            a, b = statistics.median(now_sh), statistics.median(then_sh)
            if b > 0:
                m["share_change"] = (a / b - 1) * 100

    # --- valuation ---
    mc = num(snap["market_cap"]) if snap else None
    m["market_cap"] = mc
    m["pe"] = num(snap["pe_trailing"]) if snap else None
    m["ps"] = mc / rev0 if mc and rev0 and rev0 > 0 else None
    m["peg"] = (m["pe"] / m["rev_cagr"]
                if m["pe"] and m["pe"] > 0 and m["rev_cagr"] and m["rev_cagr"] > 0 else None)

    hist = [p for _, p in pe_hist.get(t, [])][-36:]
    m["pe_vs_own"] = (m["pe"] / statistics.median(hist)
                      if m["pe"] and m["pe"] > 0 and len(hist) >= 12 else None)

    # --- price behaviour ---
    closes = [c for _, c in px.get(t, [])]
    m["ret_12m"] = ((closes[-1] / closes[-13] - 1) * 100
                    if len(closes) >= 13 and closes[-13] > 0 else None)
    m["ret_6m"] = ((closes[-1] / closes[-7] - 1) * 100
                   if len(closes) >= 7 and closes[-7] > 0 else None)
    if len(closes) >= 24:
        hi = max(closes[-36:])
        m["drawdown"] = (closes[-1] / hi - 1) * 100 if hi > 0 else None
        rets = [(closes[i] / closes[i - 1] - 1) * 100
                for i in range(1, len(closes)) if closes[i - 1] > 0]
        m["volatility"] = statistics.pstdev(rets[-36:]) if len(rets) >= 12 else None
    else:
        m["drawdown"] = m["volatility"] = None

    vols = [v for _, v in dv.get(t, [])][-12:]
    m["dollar_volume"] = statistics.median(vols) if vols else None
    return m


# --------------------------------------------------------------------------
# Rules.  Each returns (points, max_points) or None when the input is missing.
# Thresholds are calibrated against the actual universe distribution, not
# invented -- the percentile each band sits at is noted in the comments.
# --------------------------------------------------------------------------

def rules_growth(m):
    r = {}
    # universe: median 6% CAGR, p75 14%, p90 30%
    r["G1_revenue_cagr"] = (band(m["rev_cagr"], [
        (25, 8), (15, 6.5), (8, 5), (3, 3.5), (0, 2), (-5, 1)]), 8)
    # median yoy 7.7%, p75 17.8%
    r["G2_revenue_yoy"] = (band(m["rev_yoy"], [
        (20, 5), (10, 4), (5, 3), (0, 2), (-10, 1)]), 5)
    # median eps CAGR 9.4%, p75 25%
    r["G3_eps_cagr"] = (band(m["eps_cagr"], [
        (20, 4), (10, 3), (0, 2), (-15, 1)]), 4)
    # is the most recent year outrunning the multi-year trend
    r["G4_acceleration"] = (band(m["accel"], [(5, 3), (0, 2), (-5, 1)]), 3)
    return r


def rules_profitability(m):
    r = {}
    # median net margin 4.9%, p75 16%, p90 29%
    r["P1_net_margin"] = (band(m["net_margin"], [
        (20, 7), (12, 6), (6, 4.5), (2, 3), (0, 1.5), (-10, 0.5)]), 7)
    # median ebit margin 3.9%, p75 14.5%
    r["P2_ebit_margin"] = (band(m["ebit_margin"], [
        (18, 5), (10, 4), (5, 3), (0, 1.5)]), 5)
    # median gross margin 39%, p75 60% -- only ~31% of filers tag it
    r["P3_gross_margin"] = (band(m["gross_margin"], [
        (60, 4), (40, 3), (25, 2), (15, 1)]), 4)
    # bimodal in this universe: median 7 of 12, but p25 is 1
    r["P4_profitable_quarters"] = (band(m["profitable_q"], [
        (12, 4), (10, 3), (7, 2), (4, 1)]), 4)
    return r


def rules_quality(m):
    r = {}
    # median margin change +0.7pp, p75 +7pp
    r["Q1_margin_trend"] = (band(m["margin_delta"], [
        (5, 6), (2, 5), (0, 3.5), (-2, 2), (-5, 1)]), 6)
    # median stdev of yoy growth 9.0, p25 4.4, p75 21.5 -- lower is steadier
    r["Q2_growth_stability"] = (band_low(m["growth_sd"], [
        (5, 5), (10, 4), (20, 3), (35, 2), (60, 1)]), 5)
    # buybacks reward, dilution penalises; coarse bands because the implied
    # share count is noisy (see diluted_shares)
    r["Q3_share_count"] = (band_low(m["share_change"], [
        (-3, 5), (0, 4), (2, 3), (10, 2), (25, 1)]), 5)
    if m["pos_ttm_known"] == 0:
        r["Q4_earnings_streak"] = (None, 4)
    else:
        r["Q4_earnings_streak"] = ({3: 4, 2: 2.5, 1: 1.5}.get(m["pos_ttm"], 0.0), 4)
    return r


def rules_valuation(m):
    r = {}
    # median pe 19.8, p25 12.7, p75 35.3.  A negative pe means losses: it is
    # scored 0 rather than dropped, because "no earnings to value" is a real
    # answer to "is this cheap", not missing data.
    pe = m["pe"]
    r["V1_pe"] = ((band(pe, [(0.01, 0)]) if pe is not None and pe <= 0 else
                   band_low(pe, [(10, 8), (15, 7), (20, 5.5), (30, 4), (45, 2.5), (70, 1)])), 8)
    # median ps 2.5, p25 0.9, p75 4.9 -- works for loss-makers, unlike pe
    r["V2_price_to_sales"] = (band_low(m["ps"], [
        (0.75, 6), (1.5, 5), (3, 3.5), (6, 2), (12, 1)]), 6)
    # growth-adjusted: pe per point of revenue growth
    r["V3_peg"] = (band_low(m["peg"], [(1, 4), (1.5, 3), (2.5, 2), (4, 1)]), 4)
    # cheap or dear against its OWN 3-year median multiple
    r["V4_pe_vs_history"] = (band_low(m["pe_vs_own"], [
        (0.7, 2), (0.9, 1.5), (1.1, 1), (1.4, 0.5)]), 2)
    return r


def rules_momentum(m):
    r = {}
    # median 12m return 1.5%, p75 34%
    r["M1_return_12m"] = (band(m["ret_12m"], [
        (40, 6), (15, 5), (0, 3.5), (-20, 2), (-40, 1)]), 6)
    r["M2_return_6m"] = (band(m["ret_6m"], [(20, 4), (5, 3), (-10, 2), (-30, 1)]), 4)
    # median drawdown -32%: this universe is mostly well off its highs
    r["M3_drawdown"] = (band(m["drawdown"], [(-10, 4), (-25, 3), (-45, 2), (-70, 1)]), 4)
    # median monthly stdev 15% -- micro-caps dominate, so this is punishing
    r["M4_volatility"] = (band_low(m["volatility"], [
        (8, 3), (14, 2.5), (22, 1.5), (35, 0.75)]), 3)
    # median monthly dollar volume $59M; below ~$1M you cannot get a fill
    r["M5_liquidity"] = (band(m["dollar_volume"], [
        (500e6, 3), (100e6, 2.5), (25e6, 2), (5e6, 1)]), 3)
    return r


RULESETS = {
    "growth": rules_growth,
    "profitability": rules_profitability,
    "quality": rules_quality,
    "valuation": rules_valuation,
    "momentum": rules_momentum,
}


def flags(m, pillars):
    """Conditions a points total can hide.  Some cap the score outright."""
    f = []
    if m["profitable_q"] == 0 and m["window_q"] >= 8:
        f.append("chronic_losses")
    if m["share_change"] is not None and m["share_change"] > 25:
        f.append("heavy_dilution")
    if m["rev_cagr"] is not None and m["rev_cagr"] < -20:
        f.append("revenue_collapse")
    if m["dollar_volume"] is not None and m["dollar_volume"] < 1e6:
        f.append("illiquid")
    if m["market_cap"] is not None and m["market_cap"] < 50e6:
        f.append("micro_cap")
    if m["volatility"] is not None and m["volatility"] > 35:
        f.append("very_volatile")
    if m["pe"] is not None and m["pe"] > 70:
        f.append("expensive_pe")
    return f


# Caps applied AFTER the total, because a high pillar average should not be
# able to carry a company that cannot be traded or has never earned anything.
CAPS = {"chronic_losses": 45.0, "illiquid": 50.0, "revenue_collapse": 55.0}

BANDS = [(80, "Strong"), (65, "Above average"), (50, "Average"),
         (35, "Below average"), (0, "Weak")]


def score_one(m):
    pillar_scores, detail, scored, possible = {}, {}, 0.0, 0.0
    for name, fn in RULESETS.items():
        got = 0.0
        cap = 0.0
        for rule, (pts, mx) in fn(m).items():
            detail[rule] = pts
            if pts is not None:
                got += pts
                cap += mx
        if cap > 0:
            pillar_scores[name] = got / cap * PILLAR_MAX
            scored += got / cap * PILLAR_MAX
            possible += PILLAR_MAX
        else:
            pillar_scores[name] = None

    if possible == 0:
        return None, pillar_scores, detail, 0.0, []

    total = scored / possible * 100
    have = sum(1 for v in detail.values() if v is not None)
    confidence = have / len(detail)

    fl = flags(m, pillar_scores)
    for f in fl:
        if f in CAPS:
            total = min(total, CAPS[f])
    return total, pillar_scores, detail, confidence, fl


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=0, help="print an N-name leaderboard")
    ap.add_argument("--min-confidence", type=float, default=0.0)
    ap.add_argument("--sp500-only", action="store_true")
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()

    quarters = load_quarters()
    px, dv, pe_hist = load_prices()
    snaps = load_keyed(S_FILE)
    uni = load_keyed(U_FILE)
    print("loaded %d tickers with quarterly history" % len(quarters), file=sys.stderr)

    rows, not_rated = [], 0
    for t, u in uni.items():
        qs = quarters.get(t, [])
        snap = snaps.get(t)
        if len(qs) < MIN_QUARTERS:
            not_rated += 1
            continue
        m = compute(t, qs, snap, px, dv, pe_hist)
        total, pillars, detail, conf, fl = score_one(m)
        if total is None:
            not_rated += 1
            continue
        rows.append({
            "ticker": t,
            "company": u["company"],
            "sector": u["sector"],
            "score": round(total, 1),
            "band": next(b for lo, b in BANDS if total >= lo),
            "confidence": round(conf, 2),
            "quarters": m["quarters"],
            "growth_basis": m["growth_basis"],
            **{p: (round(pillars[p], 1) if pillars[p] is not None else "")
               for p in PILLARS},
            "flags": "|".join(fl),
            **{k: (round(m[k], 2) if isinstance(m.get(k), float) else m.get(k, ""))
               for k in ["rev_cagr", "rev_yoy", "eps_cagr", "net_margin",
                         "ebit_margin", "gross_margin", "margin_delta",
                         "profitable_q", "growth_sd", "share_change", "pe", "ps",
                         "peg", "pe_vs_own", "ret_12m", "ret_6m", "drawdown",
                         "volatility", "dollar_volume", "market_cap"]},
            **{k: (round(v, 2) if isinstance(v, float) else ("" if v is None else v))
               for k, v in detail.items()},
        })

    rows = [r for r in rows if r["confidence"] >= args.min_confidence]
    if args.sp500_only:
        rows = [r for r in rows if uni[r["ticker"]]["in_sp500"] == "True"]
    rows.sort(key=lambda r: -r["score"])

    # Percentile within the company's own sector. The absolute score carries a
    # measured sector tilt (see the module docstring), so for anyone comparing
    # like with like this is the column to sort on -- but sector is populated
    # for only the ~500 S&P names, so it is blank for most of the universe.
    by_sector = collections.defaultdict(list)
    for r in rows:
        if r["sector"]:
            by_sector[r["sector"]].append(r)
    for sec, group in by_sector.items():
        group.sort(key=lambda r: r["score"])
        for i, r in enumerate(group):
            r["sector_rank_pct"] = round(100.0 * (i + 1) / len(group))
            r["sector_n"] = len(group)
    for r in rows:
        r.setdefault("sector_rank_pct", "")
        r.setdefault("sector_n", "")

    # Dual share classes (GOOG/GOOGL, BRK-A/BRK-B) are one company scored
    # twice. Left unmarked they occupy two leaderboard slots and overstate
    # any sector count built from the output.
    name_counts = collections.Counter(r["company"].split(" (Class")[0].strip()
                                      for r in rows)
    for r in rows:
        base = r["company"].split(" (Class")[0].strip()
        if name_counts[base] > 1:
            r["flags"] = "|".join(filter(None, [r["flags"], "dual_class"]))

    with open(args.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if v is None else v) for k, v in r.items()})

    print("scored %d, not rated %d -> %s" % (len(rows), not_rated, args.out),
          file=sys.stderr)
    dist = collections.Counter(r["band"] for r in rows)
    for _, b in BANDS:
        print("   %-15s %5d" % (b, dist[b]), file=sys.stderr)

    if args.top:
        print("\n%-7s %-34s %6s %5s  %s" % ("TICKER", "COMPANY", "SCORE", "CONF", "PILLARS G/P/Q/V/M"))
        for r in rows[:args.top]:
            print("%-7s %-34s %6.1f %5.2f  %s  %s" % (
                r["ticker"], r["company"][:34], r["score"], r["confidence"],
                "/".join(("%4.1f" % r[p]) if r[p] != "" else " n/a" for p in PILLARS),
                r["flags"]))


if __name__ == "__main__":
    main()
