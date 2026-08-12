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
    * CASH FLOW IS PARTIAL. ocf/capex/fcf now come from the cash-flow
      statement, so accrual quality IS tested (rule Q5, cash conversion).
      What is still missing is the financing and investing detail: buybacks
      funded by debt, acquisitions, and dividends are all invisible.

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

PEER-RELATIVE PROFITABILITY
  Margin rules are scored as a PERCENTILE WITHIN SECTOR, not against absolute
  bands. Absolute bands were the main driver of a measured tilt: median net
  margin is 21.2% in Financials against 6.6% in Consumer Staples, so a grocer
  performing exactly at its industry median scored mid-band while a bank at
  its industry median maxed the rule. Financials came out 3.0x
  over-represented in the top 200 with Energy and Materials absent entirely.

  13 of profitability's 20 points are peer-relative; 7 stay absolute on
  purpose. In a sector where everybody loses money the least-bad loss-maker
  still ranks in the 90th percentile, and "does this company actually earn
  anything" is a question no percentile can answer.

  Peer groups come from `sector`: the Wikipedia GICS value where it exists
  (~500 S&P names) and otherwise the SIC-derived sector from data_sic.csv,
  which fetch_sic.py pulls from SEC for the whole universe. A sector needs
  MIN_PEERS members before its own distribution is trusted; below that the
  company is ranked against the universe. Run fetch_sic.py first -- without
  data_sic.csv only the S&P names have peers and everything else falls back.

  VALUATION IS PEER-RELATIVE TOO. P/E and P/S are percentile-ranked within
  sector for the same reason, plus one specific to them: absolute P/E bands
  rot as rates move, while a percentile re-baselines itself every run.

  MEASURED EFFECT, like-for-like on the 480 GICS-labelled names:
  Financials' over-representation in the top 200 fell from 3.04x to 1.48x,
  and the spread between the highest and lowest sector median fell from 12.0
  to 9.2 points. Health Care's low median across the FULL universe (48.1) is
  not bias -- 29% of those names are flagged chronic_losses against 2% of
  Financials, i.e. unprofitable micro-cap biotech scoring correctly.

TIME TRAVEL
  `--as-of YYYY-MM` scores each company as it looked that month, discarding
  every later quarter and price. Valuation then comes from data_monthly.csv
  instead of the current snapshot, which would otherwise leak the future into
  a historical score; `valuation_basis` records which was used.

Usage:
  python3 score_companies.py                    # score everything
  python3 score_companies.py --as-of 2024-12    # score as at Dec 2024
  python3 score_companies.py --config tuning.json --dump-config
  python3 score_companies.py --top 25           # print a leaderboard
  python3 score_companies.py --min-confidence 0.7
  python3 score_companies.py --sp500-only
"""

import argparse
import bisect
import collections
import csv
import json
import os
import statistics
import sys

# Only for sector_for(): fetch_sic is stdlib-only (urllib), so importing it
# here does not drag a dependency into the scorer.
from fetch_sic import sector_for

csv.field_size_limit(10**7)

Q_FILE = "data_quarterly.csv"
M_FILE = "data_monthly.csv"
S_FILE = "data_snapshot.csv"
U_FILE = "universe.csv"
SIC_FILE = "data_sic.csv"
OUT = "company_scores.csv"

# 16 quarters gives a true 3-year TTM-vs-TTM comparison; 12 is the fallback.
# Below MIN_QUARTERS there is not enough history to say anything and the
# company is returned NOT RATED rather than scored badly on absent data.
FULL_WINDOW = 16
FALLBACK_WINDOW = 12
MIN_QUARTERS = 8

# Foreign private issuers file a 20-F/40-F annually and never a 10-Q, so they
# have no quarterly rows at all -- 327 companies. fetch_sec_fundamentals.py
# goes to real trouble to capture them (annual_only_rows), and dropping them
# here threw that away: they were silently never rated.
#
# They are scored on the SAME rubric with the period length changed. `ppy`
# (periods per year) is 4 for a quarterly filer and 1 for an annual one, and
# every window below is expressed as a multiple of it, so a "3-year growth
# rate" means 12 quarters or 3 years without any rule needing to know which.
# `growth_basis` records the basis so an annual score is never silently
# compared against a quarterly one.
MIN_ANNUAL_PERIODS = 3

# How far the two independent P/E estimates may differ before both are
# distrusted. 50% is deliberately loose: it catches the 20x split errors this
# is aimed at without discarding ordinary timing noise between a month-end
# price snapshot and a TTM earnings window.
PE_TOLERANCE = 0.5

PILLARS = ["growth", "profitability", "quality", "health", "valuation", "momentum"]

# Leverage is not comparable for banks and insurers. Their liabilities ARE the
# business -- deposits, policy reserves and repo funding are not borrowings in
# the sense debt/equity assumes, and a 10x ratio is ordinary rather than
# distressed. Scoring them on it would penalise the whole sector for existing.
#
# THE PILLAR IS NO LONGER DROPPED. It was, and that turned out to be a free
# pass rather than neutrality: score_one renormalises over the pillars that
# scored, so a bank was ranked on five hurdles while everyone else cleared six.
# Pooled over 33 quarters that put Financials at 1.40x their universe share in
# the top 100, and all 34 Financials in the Q2-2026 top 100 carried the flag.
# Dropping a hurdle cannot be neutral when the ranking is against companies
# that had to clear it.
#
# Instead banks are scored on the one balance-sheet measure that IS meaningful
# for them -- equity/assets, the regulatory leverage ratio in all but name --
# ranked WITHIN the sector, then placed on the same 0-20 health scale everyone
# else lands on (see BANK_HEALTH_BANDS). A median bank scores what a median
# company scores; a thinly capitalised one scores badly. The sector is neither
# rewarded nor punished, and banks are still told apart from each other.
#
# HONEST LIMIT: this cannot be validated the way the other health rules were.
# Survival AUC needs failures, and in 2018-2026 only 6 Financials in this
# universe stopped filing against 3594 that did not -- the window contains no
# banking crisis. equity/assets is used because it is the correct measure of
# bank solvency, not because it was measured to predict one here.
LEVERAGE_EXEMPT = ("Financials",)

# --------------------------------------------------------------------------
# TUNABLE CONFIGURATION
#
# Everything a judgement call depends on lives here and nowhere else, so the
# rubric can be re-tuned without touching the scoring code. Override any subset
# with `--config my.json`; the file is deep-merged over these defaults, so a
# file containing only {"pillar_weights": {"valuation": 30}} changes just that.
# `--dump-config` prints the active values.
#
# WHY THIS MATTERS AS MACRO CONDITIONS MOVE
#   Absolute thresholds rot. A P/E of 20 is expensive at 8% policy rates and
#   ordinary at 2%; "revenue growth above 15%" means different things either
#   side of an inflation shock. Two defences are built in:
#
#   1. The peer-relative rules (margins, and now P/E and P/S) are scored as
#      PERCENTILES, which re-baseline themselves every run. If the whole
#      market de-rates, the cheap half of every sector is still the cheap
#      half -- no retuning needed, and this is why valuation moved to
#      percentiles rather than getting new fixed numbers.
#   2. Everything still absolute is in `bands` below, editable in one place.
#      Absolute rules are kept deliberately (see rules_profitability) because
#      a percentile cannot tell you whether a company earns anything at all.
#
#   `pillar_weights` need not sum to 100 -- the total is renormalised -- so
#   weighting valuation higher in an expensive market is a one-line change.
# --------------------------------------------------------------------------
DEFAULTS = {
    "pillar_weights": {
        # Halved. Growth showed no relationship with forward return (-0.014 /
        # +0.003 / +0.042 across windows) and almost none with survival
        # (AUC 0.544). Two independent tests, same answer. Not removed
        # outright: it is one regime's evidence, and a business that stops
        # growing is not irrelevant -- just far less informative than the
        # original equal weighting assumed.
        "growth": 10.0,
        "profitability": 20.0,
        "quality": 20.0,
        "health": 20.0,
        "valuation": 20.0,
        "momentum": 20.0,
    },
    # A sector needs this many members before its own distribution is trusted;
    # below it the company is ranked against the whole universe. Ranking
    # against 4 peers yields percentiles of 0/25/50/75/100 and nothing between.
    "min_peers": 20,
    # Percentile -> fraction of the rule's points. 50 is the sector median by
    # construction, so a typical company scores mid-band.
    "peer_bands": [[90, 1.00], [75, 0.80], [60, 0.65], [50, 0.50],
                   [35, 0.35], [20, 0.20]],
    # Absolute bands. Higher-is-better rules list [min_value, points]
    # descending; lower-is-better rules list [max_value, points] ascending.
    # The percentile each threshold sits at in this universe is in the comment
    # beside the rule -- they were calibrated, not invented.
    "bands": {
        "G1_revenue_cagr":      [[25, 8], [15, 6.5], [8, 5], [3, 3.5], [0, 2], [-5, 1]],
        "G2_revenue_yoy":       [[20, 5], [10, 4], [5, 3], [0, 2], [-10, 1]],
        "G3_eps_cagr":          [[20, 4], [10, 3], [0, 2], [-15, 1]],
        "G4_acceleration":      [[5, 3], [0, 2], [-5, 1]],
        "P4_net_margin_absolute": [[15, 4], [8, 3], [3, 2], [0, 1]],
        "P5_profitable_quarters": [[12, 3], [10, 2.25], [7, 1.5], [4, 0.75]],
        "Q1_margin_trend":      [[5, 5], [2, 4], [0, 3], [-2, 1.5], [-5, 0.75]],
        "Q2_growth_stability":  [[5, 4], [10, 3.2], [20, 2.4], [35, 1.6], [60, 0.8]],
        "Q3_share_count":       [[-3, 4], [0, 3.2], [2, 2.4], [10, 1.6], [25, 0.8]],
        "Q5_cash_conversion":   [[90, 4], [70, 3.2], [50, 2.4], [30, 1.4], [10, 0.6]],
        # Leverage. Net cash (<=0) is the top band: a company owing less than
        # it holds is not levered at all. Above ~4x, refinancing risk starts to
        # dominate the equity story regardless of how good the business is.
        "H1_net_debt_to_ebitda": [[0, 4], [1, 3.6], [2, 3], [3, 2], [4.5, 1], [6, 0.3]],
        "H2_interest_coverage": [[15, 3], [8, 2.4], [4, 1.8], [2, 0.9], [1, 0.3]],
        "H3_current_ratio":     [[2.0, 4], [1.5, 3.2], [1.2, 2.4], [1.0, 1.6], [0.8, 0.6]],
        # equity/assets. Median survivor 0.37; median company that vanished
        # -0.03. Anything at or below zero is technically insolvent.
        "H4_equity_ratio":      [[0.5, 5], [0.35, 4], [0.2, 3], [0.1, 2], [0.0, 1]],
        # quarters of cash left at the current burn rate
        "H5_cash_runway":       [[12, 2], [8, 1.5], [4, 1], [2, 0.5]],
        # TTM revenue. A floor, not a ladder -- see rules_health.
        "H6_size_floor":        [[100e6, 2], [25e6, 1.5], [5e6, 1], [1e6, 0.5]],
        "V3_peg":               [[1, 3], [1.5, 2.25], [2.5, 1.5], [4, 0.75]],
        "V4_pe_vs_history":     [[0.7, 2], [0.9, 1.5], [1.1, 1], [1.4, 0.5]],
        "V5_price_to_fcf":      [[12, 4], [18, 3.2], [28, 2.2], [45, 1.2], [70, 0.5]],
        "M1_return_12m":        [[40, 6], [15, 5], [0, 3.5], [-20, 2], [-40, 1]],
        "M2_return_6m":         [[20, 4], [5, 3], [-10, 2], [-30, 1]],
        "M3_drawdown":          [[-10, 4], [-25, 3], [-45, 2], [-70, 1]],
        "M4_volatility":        [[8, 3], [14, 2.5], [22, 1.5], [35, 0.75]],
        "M5_liquidity":         [[500e6, 3], [100e6, 2.5], [25e6, 2], [5e6, 1]],
    },
    # Points available to each rule. Peer-relative rules take their maximum
    # from here; changing one re-weights within its pillar automatically.
    "rule_max": {
        "P1_net_margin_vs_peers": 6, "P2_ebit_margin_vs_peers": 4,
        "P3_gross_margin_vs_peers": 3,
        "V1_pe_vs_peers": 6, "V2_ps_vs_peers": 5,
    },
    # Earnings quality. `ladder` maps net-minus-EBIT margin gap (in percentage
    # points, descending) to the multiplier applied to the net-margin rules;
    # the first threshold met wins. `flag_at` raises earnings_below_op_line,
    # which the caps below then bind. See `eq_multiplier` and `m["eq_gap"]`.
    #
    # Adjustable like everything else: in a period when one-off gains are
    # widespread and genuinely repeatable -- a tax-law change, say -- raise the
    # thresholds rather than editing code.
    "earnings_quality": {"ladder": [[30, 0.50], [10, 0.75]], "flag_at": 30},
    # Bank health. [within-sector percentile on equity/assets, points out of 20].
    #
    # These are not invented thresholds. They are calibrated so the health
    # scores of banks that actually get ranked reproduce the observed health
    # distribution of the 64,705 non-bank company-quarters they are ranked
    # against -- the median ranked bank lands on the median company's health
    # score. That equivalence is the point: it makes the pillar mean the same
    # thing for a bank as for anyone else, so the renormalisation free pass
    # disappears without inventing a penalty.
    #
    # WHY THE CURVE IS NOT A STRAIGHT PERCENTILE MAP. Banks that clear the
    # confidence gate are not a uniform sample of banks -- the median one sits
    # at the 71.7th percentile of bank capital, not the 50th, because thin
    # balance sheets and thin disclosure travel together. Mapping percentile
    # straight onto the reference curve therefore handed the median ranked
    # bank a 17.1 where the companies beside it averaged 14.9, and pushed
    # Financials to 1.81x their universe share in the top 100 -- worse than
    # the 1.40x defect it replaced. This curve is the corrected one.
    #
    # Recalibrate if the health rules are reweighted or the confidence gate
    # moves: the curve is a photograph of both, so changing either dates it.
    "bank_health_bands": [[95, 20.0], [90, 19.6], [85, 18.2], [80, 17.1],
                          [75, 15.6], [70, 14.3], [65, 13.1], [60, 12.3],
                          [55, 12.0], [50, 11.6], [45, 11.3], [40, 10.9],
                          [35, 10.7], [30, 10.1], [25, 9.7], [20, 8.7],
                          [15, 8.2], [10, 6.7], [5, 3.7], [0, 0.0]],
    # Conditions that cap the total no matter how the pillars scored.
    "caps": {"chronic_losses": 45.0, "illiquid": 50.0, "revenue_collapse": 55.0,
             # 70 leaves room to be a good company, not a top pick, while the
             # profit is arriving from below the operating line.
             "earnings_below_op_line": 70.0},
    "score_bands": [[80, "Strong"], [65, "Above average"], [50, "Average"],
                    [35, "Below average"], [0, "Weak"]],
}


def deep_merge(base, override):
    """Recursively overlay `override` on a copy of `base`."""
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path=None):
    cfg = DEFAULTS
    if path:
        with open(path) as fh:
            cfg = deep_merge(cfg, json.load(fh))
    return cfg


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


def eq_multiplier(m, cfg):
    """Earnings-quality multiplier for the net-margin rules. 1.0 = no discount.

    Graduated rather than a cliff because the underlying effect is graduated:
    a +5 to +20 gap already returned a median -7.2% against +3.7% for the
    normal range, so a single threshold at +30 would let the milder half
    through untouched.

    A missing operating margin means the gap cannot be computed at all, and an
    uncomputable test must not become a silent penalty -- those return 1.0 and
    are scored as before.
    """
    gap = m.get("eq_gap")
    if gap is None:
        return 1.0
    for lo, mul in cfg["earnings_quality"]["ladder"]:
        if gap >= lo:
            return mul
    return 1.0


def mute(rule, mul):
    """Scale a rule's EARNED points, leaving its maximum intact.

    The maximum must not move. score_one sums earned over available and
    renormalises, so scaling both halves would cancel exactly and the discount
    would silently do nothing -- the rule would look applied and change no
    score. Only the numerator is touched.
    """
    pts, mx = rule
    return (pts if pts is None else round(pts * mul, 3), mx)


# --------------------------------------------------------------------------
# Load
# --------------------------------------------------------------------------

def load_periods():
    """(quarterly rows, annual rows) per ticker, each sorted and deduped.

    The two are kept SEPARATE and never concatenated: summing an FY row into a
    TTM alongside four quarters double-counts a year. A company is scored on
    one basis or the other, never a mixture -- see MIN_ANNUAL_PERIODS.
    """
    q = collections.defaultdict(dict)
    a = collections.defaultdict(dict)
    for r in csv.DictReader(open(Q_FILE)):
        (q if r["period_type"] == "Q" else a)[r["ticker"]][r["period_end"]] = r
    srt = lambda d: {t: sorted(v.values(), key=lambda r: r["period_end"])
                     for t, v in d.items()}
    return srt(q), srt(a)


def load_prices():
    """ticker -> (sorted adj_close series, sorted dollar-volume series, pe series)."""
    px = collections.defaultdict(list)
    dv = collections.defaultdict(list)
    pe = collections.defaultdict(list)
    mc = collections.defaultdict(list)
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
        k = num(r["market_cap_est"])
        if k and k > 0:
            mc[r["ticker"]].append((m, k))
    for d in (px, dv, pe, mc):
        for t in d:
            d[t].sort()
    return px, dv, pe, mc


def load_keyed(path, key="ticker"):
    return {r[key]: r for r in csv.DictReader(open(path))}


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------

def ttm(qs, measure, offset=0, ppy=4):
    """Sum `measure` over the `ppy` periods ending `offset` periods back.

    ppy=4 sums four quarters into a trailing year; ppy=1 takes a single
    already-annual row. Same arithmetic, so no rule needs to branch.

    Returns None unless all four are present -- a partial TTM understates the
    figure, and an understated denominator silently inflates every margin and
    multiple built on it.
    """
    end = len(qs) - offset
    win = qs[end - ppy:end]
    if len(win) < ppy:
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


def compute(t, qs, snap, px, dv, pe_hist, mc_hist, as_of=None, ppy=4):
    """Everything the rules need, or None where the data will not support it.

    `as_of` ("YYYY-MM") scores the company as it looked at that month: every
    quarter ending after it, and every price after it, is discarded.

    In as-of mode the valuation inputs come from data_monthly.csv rather than
    data_snapshot.csv, which is a CURRENT snapshot and would leak the future
    into a historical score. Two caveats follow from that swap and are flagged
    in the output as `valuation_basis`:
      * `market_cap_est` is monthly close x CURRENT shares outstanding (see
        HANDOFF section 8), so a historical market cap is distorted by any
        buyback or issuance since. P/S inherits that distortion.
      * `pe_trailing_est` is genuinely historical -- close over trailing EPS as
        filed at the time -- so P/E is the sounder of the two.
    """
    if as_of:
        qs = [r for r in qs if r["period_end"][:7] <= as_of]
    m = {"quarters": len(qs), "ppy": ppy}
    if len(qs) < (MIN_QUARTERS if ppy == 4 else MIN_ANNUAL_PERIODS):
        return None
    ann = "annual-" if ppy == 1 else ""

    # --- growth basis: prefer a true 3-year comparison ---
    if len(qs) >= 4 * ppy:
        lag, years, m["growth_basis"] = 3 * ppy, 3.0, ann + "3y"
    elif len(qs) >= 3 * ppy:
        lag, years, m["growth_basis"] = 2 * ppy, 2.0, ann + "2y"
    else:
        lag, years, m["growth_basis"] = None, None, "insufficient"

    rev0 = ttm(qs, "revenue", ppy=ppy)
    ni0 = ttm(qs, "net_income", ppy=ppy)
    m["revenue_ttm"] = rev0
    m["net_income_ttm"] = ni0

    rev_prior = ttm(qs, "revenue", 4, ppy=ppy)
    m["rev_yoy"] = ((rev0 / rev_prior - 1) * 100
                    if rev0 and rev_prior and rev_prior > 0 else None)

    if lag:
        m["rev_cagr"] = cagr(rev0, ttm(qs, "revenue", lag, ppy=ppy), years)
        m["eps_cagr"] = cagr(ttm(qs, "eps_diluted", ppy=ppy), ttm(qs, "eps_diluted", lag, ppy=ppy), years)
    else:
        m["rev_cagr"] = m["eps_cagr"] = None

    # acceleration: is the latest year faster than the multi-year trend
    m["accel"] = (m["rev_yoy"] - m["rev_cagr"]
                  if m["rev_yoy"] is not None and m["rev_cagr"] is not None else None)

    # --- margins ---
    ebit0, gp0 = ttm(qs, "ebit", ppy=ppy), ttm(qs, "gross_profit", ppy=ppy)
    m["net_margin"] = ni0 / rev0 * 100 if rev0 and rev0 > 0 and ni0 is not None else None
    m["ebit_margin"] = ebit0 / rev0 * 100 if rev0 and rev0 > 0 and ebit0 is not None else None
    m["gross_margin"] = gp0 / rev0 * 100 if rev0 and rev0 > 0 and gp0 is not None else None

    # Earnings quality: how far net margin sits ABOVE operating margin.
    #
    # Operating income is what the business earns from doing what it does. Net
    # income adds everything below that line -- tax-valuation-allowance
    # releases, investment and fair-value gains, one-off milestone and
    # settlement payments. When net margin runs far above EBIT margin, most of
    # the reported profit did not come from operations and will not recur, yet
    # every margin rule reads it as ongoing.
    #
    # Measured on 30 point-in-time quarters, companies with a gap above +20
    # returned a median -19.4% over the next 12 months against +3.7% for the
    # normal range, and did worse in 29 of 29 quarters -- the only thing in
    # this study that was 100% directional.
    #
    # ONE-SIDED ON PURPOSE. The mirror case (net margin far BELOW EBIT margin:
    # heavy interest, non-operating losses) tested roughly neutral -- +2.0%
    # median, 52% positive, against +10.1%/61% for clean names -- so it is not
    # muted. Only the flattering direction is.
    m["eq_gap"] = (m["net_margin"] - m["ebit_margin"]
                   if m["net_margin"] is not None and m["ebit_margin"] is not None
                   else None)

    # margin trend in percentage points across the window
    m["margin_delta"] = None
    if lag and rev0 and rev0 > 0 and ni0 is not None:
        rev_then, ni_then = ttm(qs, "revenue", lag, ppy=ppy), ttm(qs, "net_income", lag, ppy=ppy)
        if rev_then and rev_then > 0 and ni_then is not None:
            m["margin_delta"] = m["net_margin"] - ni_then / rev_then * 100

    # --- consistency ---
    window = qs[-3 * ppy:]
    ni_vals = [num(r["net_income"]) for r in window]
    known = [v for v in ni_vals if v is not None]
    m["profitable_q"] = sum(1 for v in known if v > 0) if known else None
    m["window_q"] = len(known)

    growth_pts = []
    for i in range(max(ppy, len(qs) - 2 * ppy), len(qs)):
        a, b = num(qs[i]["revenue"]), num(qs[i - ppy]["revenue"])
        if a is not None and b and b > 0:
            growth_pts.append((a / b - 1) * 100)
    m["growth_sd"] = (statistics.pstdev(growth_pts)
                      if len(growth_pts) >= (4 if ppy == 4 else 2) else None)

    # positive earnings across the three TTM slices
    ttms = [ttm(qs, "net_income", k, ppy=ppy) for k in (0, ppy, 2 * ppy)]
    m["pos_ttm"] = sum(1 for v in ttms if v is not None and v > 0)
    m["pos_ttm_known"] = sum(1 for v in ttms if v is not None)

    # --- share count trend (dilution / buyback) ---
    m["share_change"] = None
    if lag:
        adj = reject_outliers(split_adjust(implied_shares(qs)))
        n = len(qs)
        now_sh = [s for i, s in adj if i >= n - ppy]
        then_sh = [s for i, s in adj if n - ppy - lag <= i < n - lag]
        if now_sh and then_sh:
            a, b = statistics.median(now_sh), statistics.median(then_sh)
            if b > 0:
                m["share_change"] = (a / b - 1) * 100

    # --- cash flow ---
    # Present only if fetch_sec_fundamentals.py was run with the ocf/capex
    # concepts; older data_quarterly.csv files have no such columns and every
    # cash rule below degrades to "no data" rather than erroring.
    fcf0 = ttm(qs, "fcf", ppy=ppy) if "fcf" in qs[-1] else None
    m["fcf_ttm"] = fcf0
    m["fcf_margin"] = fcf0 / rev0 * 100 if fcf0 is not None and rev0 and rev0 > 0 else None
    # Cash conversion: how much of reported profit arrived as cash. This is the
    # accrual-quality test -- a company booking profit it never collects shows
    # a high net margin and a conversion near zero. Only meaningful against
    # POSITIVE earnings; against a loss the ratio is not interpretable.
    m["fcf_conversion"] = (fcf0 / ni0 * 100
                           if fcf0 is not None and ni0 is not None and ni0 > 0 else None)

    # Reporting currency. Prices and market cap are USD; the financials are
    # as filed. Any multiple that divides one by the other is meaningless for
    # a non-USD reporter -- P/S would be a USD market cap over EUR revenue --
    # so those rules are dropped rather than scored on a mixed-currency
    # number. 75 tickers are affected, mostly the IFRS filers.
    m["currency"] = (qs[-1].get("currency") or "").strip().upper()
    m["non_usd"] = bool(m["currency"] and m["currency"] != "USD")

    # --- balance sheet ---
    # These are INSTANT facts: a position at the latest period end, not a sum
    # over the window. Taking the most recent quarter that reports each one,
    # because a company's leverage today is what matters, not its average.
    def latest(field):
        if field not in qs[-1]:
            return None
        for r in reversed(qs):
            v = num(r.get(field))
            if v is not None:
                return v
        return None

    equity = latest("equity")
    m["equity"] = equity
    m["total_debt"] = latest("total_debt")
    m["net_debt"] = latest("net_debt")
    m["cash"] = latest("cash")
    m["assets"] = latest("assets")

    ebitda_ttm = ttm(qs, "ebitda", ppy=ppy)
    ebit_ttm = ttm(qs, "ebit", ppy=ppy)
    m["ebitda_ttm"] = ebitda_ttm
    # Leverage against earnings power. Undefined when EBITDA is zero or
    # negative -- "how many years of profit to repay the debt" has no answer
    # for a company with no profit, and a negative denominator would flip the
    # sign and score a distressed borrower as conservatively financed.
    m["net_debt_to_ebitda"] = (m["net_debt"] / ebitda_ttm
                               if m["net_debt"] is not None and ebitda_ttm and ebitda_ttm > 0
                               else None)
    interest = ttm(qs, "interest_expense", ppy=ppy)
    interest = abs(interest) if interest is not None else None
    m["interest_expense_ttm"] = interest
    m["interest_coverage"] = (ebit_ttm / interest
                              if ebit_ttm is not None and interest and interest > 0 else None)
    ca, cl = latest("current_assets"), latest("current_liabilities")
    m["current_ratio"] = ca / cl if ca is not None and cl and cl > 0 else None
    # Negative equity makes debt/equity meaningless (a large negative ratio
    # would read as low leverage), so it is dropped and flagged instead.
    m["debt_to_equity"] = (m["total_debt"] / equity
                           if m["total_debt"] is not None and equity and equity > 0 else None)
    m["roe"] = (ni0 / equity * 100
                if ni0 is not None and equity and equity > 0 else None)
    m["negative_equity"] = bool(equity is not None and equity < 0)

    # MEASURED SURVIVAL PREDICTORS (see survival_test.py / README).
    #
    # equity/assets is the strongest single raw predictor tested, AUC 0.712:
    # the median company that stopped filing had equity of -3% of assets. It
    # replaces debt/equity, which measured 0.517 -- no signal at all. Debt is
    # survivable; negative book equity is not, and debt/equity cannot even
    # express that (a negative denominator flips the ratio's sign).
    m["equity_ratio"] = (equity / m["assets"]
                         if equity is not None and m["assets"] and m["assets"] > 0
                         else None)
    # Quarters of life at the current burn. Defined ONLY while burning -- for a
    # cash generator it is meaningless rather than infinite. AUC 0.613-0.647.
    m["cash_runway_q"] = (m["cash"] / (abs(fcf0) / 4)
                          if (fcf0 is not None and fcf0 < 0
                              and m["cash"] is not None and fcf0 != 0)
                          else None)
    m["revenue_size"] = rev0

    # --- valuation ---
    if as_of:
        mseries = [(mo, v) for mo, v in mc_hist.get(t, []) if mo <= as_of]
        mc = mseries[-1][1] if mseries else None
        reported_pe = next((v for mo, v in reversed(pe_hist.get(t, [])) if mo <= as_of), None)
        m["valuation_basis"] = "monthly_est"
    else:
        mc = num(snap["market_cap"]) if snap else None
        reported_pe = num(snap["pe_trailing"]) if snap else None
        m["valuation_basis"] = "snapshot"
    # CURRENCY GATE -- must run BEFORE any multiple is derived.
    #
    # Prices and market cap are USD; the financials are as filed. Every
    # multiple that divides one by the other is meaningless for a non-USD
    # reporter: ASML read a P/E of 68.4 and Kaspi 0.02 (a USD market cap over
    # tenge earnings) when this guard ran too late and only caught P/S.
    # Nulling both inputs here drops P/E, P/S and P/FCF together.
    if m["non_usd"]:
        mc = None
        reported_pe = None

    m["market_cap"] = mc

    # ------------------------------------------------------------------
    # P/E: TWO INDEPENDENT ESTIMATES, USED ONLY WHERE THEY AGREE.
    #
    # Neither upstream source is trustworthy on its own:
    #
    #   pe_reported = price / eps_trailing. Yahoo's prices are split-adjusted
    #     but SEC's as-filed EPS is not, so any company that has split reads
    #     wrong. Booking: $193.13 against an as-filed $166.53 EPS gives 1.16
    #     where the truth is ~20.7. 381 tickers show this inconsistency.
    #
    #   pe_mcap = market_cap / TTM net income. Needs no EPS, so splits cannot
    #     touch it -- but market_cap is price x shares_outstanding, and that
    #     share count is missing for ~1,000 tickers (including GOOGL, META and
    #     NVDA) and plainly wrong for others (Mastercard: 122.5M against an
    #     actual ~910M, implying a $70bn company rather than ~$520bn).
    #
    # So they cross-check each other. Agreement within PE_TOLERANCE means both
    # share bases line up and the number is sound. Disagreement means one of
    # them is broken and we cannot tell which, so the P/E rules are DROPPED and
    # the company is flagged rather than scored on a figure that might be 20x
    # out. Where only one estimate exists it is used unchecked, which is the
    # honest limit of what this data supports.
    # ------------------------------------------------------------------
    m["pe_reported"] = reported_pe
    pe_mcap = mc / ni0 if (mc and ni0 and ni0 > 0) else None
    pe_rep = reported_pe if (reported_pe and reported_pe > 0) else None
    m["pe_mcap"] = pe_mcap

    if pe_mcap and pe_rep:
        agree = abs(pe_mcap - pe_rep) / max(pe_mcap, pe_rep) <= PE_TOLERANCE
        m["pe"] = pe_mcap if agree else None
        m["pe_unreliable"] = not agree
    else:
        m["pe"] = pe_mcap or pe_rep
        m["pe_unreliable"] = False

    # Market cap drives P/S and P/FCF too, so where it is missing but the
    # reported P/E is usable, back it out: mcap = pe x earnings. This recovers
    # the mega-caps whose share count SEC never tagged.
    if not mc and pe_rep and ni0 and ni0 > 0:
        mc = pe_rep * ni0
        m["market_cap_source"] = "derived_from_pe"
    elif mc:
        m["market_cap_source"] = "snapshot" if not as_of else "monthly_est"
    else:
        m["market_cap_source"] = ""
    # A market cap we could not corroborate should not drive a valuation
    # multiple either.
    if m["pe_unreliable"]:
        mc = None
    m["market_cap"] = mc
    m["ps"] = mc / rev0 if mc and rev0 and rev0 > 0 else None
    m["p_fcf"] = mc / fcf0 if mc and fcf0 and fcf0 > 0 else None
    m["peg"] = (m["pe"] / m["rev_cagr"]
                if m["pe"] and m["pe"] > 0 and m["rev_cagr"] and m["rev_cagr"] > 0 else None)

    pe_series = [(mo, v) for mo, v in pe_hist.get(t, []) if not as_of or mo <= as_of]
    hist = [p for _, p in pe_series][-36:]
    m["pe_vs_own"] = (m["pe"] / statistics.median(hist)
                      if m["pe"] and m["pe"] > 0 and len(hist) >= 12 else None)

    # --- price behaviour ---
    closes = [c for mo, c in px.get(t, []) if not as_of or mo <= as_of]
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

    vols = [v for mo, v in dv.get(t, []) if not as_of or mo <= as_of][-12:]
    m["dollar_volume"] = statistics.median(vols) if vols else None
    m["window_end"] = qs[-1]["period_end"] if qs else ""
    return m


# --------------------------------------------------------------------------
# Rules.  Each returns (points, max_points) or None when the input is missing.
# Thresholds are calibrated against the actual universe distribution, not
# invented -- the percentile each band sits at is noted in the comments.
# --------------------------------------------------------------------------

def rules_growth(m, ctx):
    B = ctx.cfg["bands"]
    return {
        "G1_revenue_cagr":  (band(m["rev_cagr"], B["G1_revenue_cagr"]), 8),
        "G2_revenue_yoy":   (band(m["rev_yoy"], B["G2_revenue_yoy"]), 5),
        "G3_eps_cagr":      (band(m["eps_cagr"], B["G3_eps_cagr"]), 4),
        # is the most recent year outrunning the multi-year trend
        "G4_acceleration":  (band(m["accel"], B["G4_acceleration"]), 3),
    }


def rules_profitability(m, ctx):
    """Profitability is scored PEER-RELATIVE, not on absolute margin bands.

    Absolute bands were the main driver of the Financials tilt: median net
    margin is 21.2% in Financials against 6.6% in Consumer Staples, so a
    grocer performing exactly at its industry median scored mid-band while a
    bank at its industry median maxed the rule. Ranking within sector removes
    that by construction.

    13 of the 20 points are peer-relative and 7 stay absolute. The absolute
    floor is deliberate: "does this company actually earn money" is a real
    question that a percentile cannot answer -- in a sector where everyone
    loses money, the least-bad loss-maker still ranks in the 90th percentile.
    """
    B = ctx.cfg["bands"]
    # Only the two net-margin rules are muted. P2 reads operating margin and P3
    # gross margin -- both sit above the line the problem lives below, so both
    # are already clean and are left alone. That also means a company muted
    # here keeps 7 of the 20 profitability points on undisputed operating
    # numbers, which is the intent: this discounts the flattered part of the
    # picture, it does not erase the company.
    mul = eq_multiplier(m, ctx.cfg)
    p1 = ctx.peer_points(m, "net_margin", "P1_net_margin_vs_peers")
    p4 = (band(m["net_margin"], B["P4_net_margin_absolute"]), 4)
    return {
        "P1_net_margin_vs_peers":   mute(p1, mul),
        "P2_ebit_margin_vs_peers":  ctx.peer_points(m, "ebit_margin", "P2_ebit_margin_vs_peers"),
        "P3_gross_margin_vs_peers": ctx.peer_points(m, "gross_margin", "P3_gross_margin_vs_peers"),
        # Absolute floor -- sector-neutral, and the guard against the "best of
        # a uniformly unprofitable industry" failure mode above.
        "P4_net_margin_absolute":   mute(p4, mul),
        "P5_profitable_quarters":   (band(m["profitable_q"], B["P5_profitable_quarters"]), 3),
    }


def rules_quality(m, ctx):
    B = ctx.cfg["bands"]
    r = {
        "Q1_margin_trend":     (band(m["margin_delta"], B["Q1_margin_trend"]), 5),
        "Q2_growth_stability": (band_low(m["growth_sd"], B["Q2_growth_stability"]), 4),
        # buybacks reward, dilution penalises; coarse bands because the implied
        # share count is noisy (see implied_shares)
        "Q3_share_count":      (band_low(m["share_change"], B["Q3_share_count"]), 4),
        # Cash conversion. 100% means every dollar of reported profit arrived
        # as free cash. Comfortably above 100 is normal for asset-light
        # compounders (D&A exceeds capex); persistently below ~40 says the
        # profit is accruals. Scored only against positive earnings.
        "Q5_cash_conversion":  (band(m["fcf_conversion"], B["Q5_cash_conversion"]), 4),
    }
    if m["pos_ttm_known"] == 0:
        r["Q4_earnings_streak"] = (None, 3)
    else:
        r["Q4_earnings_streak"] = ({3: 3, 2: 2, 1: 1}.get(m["pos_ttm"], 0.0), 3)
    return r


def rules_health(m, ctx):
    """Balance-sheet strength, reweighted against measured survival outcomes.

    Every weight here now reflects an AUC measured on whether the company was
    still filing 8 quarters later (survival_test.py), rather than on what
    looked sensible:

      equity/assets    0.712   <- strongest raw predictor of anything tested
      current ratio    0.669
      interest cover   0.633
      cash runway      0.613
      debt/assets      0.517   <- REMOVED; no signal

    Dropped for the same reason: return on equity (0.562), which was carrying
    2 points on no evidence.

    Banks and insurers get a different rule, not an exemption -- see
    LEVERAGE_EXEMPT and rules_health_bank.
    """
    B = ctx.cfg["bands"]
    if (m.get("_sector") or "") in LEVERAGE_EXEMPT:
        return rules_health_bank(m, ctx)
    return {
        "H1_net_debt_to_ebitda": (band_low(m["net_debt_to_ebitda"], B["H1_net_debt_to_ebitda"]), 4),
        "H2_interest_coverage":  (band(m["interest_coverage"], B["H2_interest_coverage"]), 3),
        "H3_current_ratio":      (band(m["current_ratio"], B["H3_current_ratio"]), 4),
        "H4_equity_ratio":       (band(m["equity_ratio"], B["H4_equity_ratio"]), 5),
        "H5_cash_runway":        (band(m["cash_runway_q"], B["H5_cash_runway"]), 2),
        # Size as a FLOOR, not a reward. Survivors are 7-10x larger, but
        # rewarding size outright would just tilt the whole score to mega-caps
        # and give up the return premium small companies carry. This penalises
        # only the genuinely tiny, where survival risk is concentrated.
        "H6_size_floor":         (band(m["revenue_size"], B["H6_size_floor"]), 2),
    }


def rules_health_bank(m, ctx):
    """Health for banks and insurers: capital adequacy, ranked among peers.

    One rule carrying the whole 20, because a bank has exactly one
    balance-sheet question that means anything here -- is it capitalised --
    and padding it out with rules that do not apply (current ratio, net debt
    to EBITDA, interest coverage, where interest is REVENUE) would dilute the
    only real signal with noise.

    The percentile is taken within LEVERAGE_EXEMPT companies alone. Bank
    equity/assets runs at a median of 0.22 against 0.43 for everyone else, so
    ranking a bank against the whole universe on the absolute bands would put
    almost every one of them in the bottom band -- which is the error the old
    exemption was written to avoid, and it remains an error.

    Missing equity/assets returns None, which drops the pillar and renormalises
    exactly as before. That is the old free pass, now confined to the ~4% of
    bank-quarters with no usable balance sheet rather than applied to all.
    """
    pct = ctx.pctile(m, "equity_ratio", pool=LEVERAGE_EXEMPT)
    if pct is None:
        return {"HB1_capital_vs_peers": (None, 20)}
    return {"HB1_capital_vs_peers":
            (band(pct, ctx.cfg["bank_health_bands"]), 20)}


def rules_valuation(m, ctx):
    """Valuation multiples are scored PEER-RELATIVE for the same reason
    margins are, plus one specific to this pillar: absolute P/E bands rot as
    rates move. A P/E of 20 is expensive at 8% policy rates and unremarkable
    at 2%, so a fixed table needs re-cutting after every macro regime change.
    A percentile does not -- it re-baselines itself on every run.

    Its measured sector spread was 8.8 points, LARGER than profitability's
    5.5, because banks structurally trade cheap on earnings.

    A loss-making company is excluded from the P/E distribution and scored
    zero on that rule rather than dropped: "no earnings to value" is a real
    answer to "is this cheap", not missing data.
    """
    B = ctx.cfg["bands"]
    pe_rule = ctx.peer_points(m, "pe", "V1_pe_vs_peers")
    if m["pe"] is not None and m["pe"] <= 0:
        pe_rule = (0.0, ctx.cfg["rule_max"]["V1_pe_vs_peers"])
    return {
        "V1_pe_vs_peers": pe_rule,
        "V2_ps_vs_peers": ctx.peer_points(m, "ps", "V2_ps_vs_peers"),
        # growth-adjusted: pe per point of revenue growth
        "V3_peg":            (band_low(m["peg"], B["V3_peg"]), 3),
        # cheap or dear against its OWN 3-year median multiple -- the one rule
        # that is time-relative rather than cross-sectional
        "V4_pe_vs_history":  (band_low(m["pe_vs_own"], B["V4_pe_vs_history"]), 2),
        # Price to free cash flow. Harder to manipulate than P/E -- cash is
        # cash -- and it prices the capital intensity P/E ignores. A negative
        # FCF yields no multiple, so the rule drops rather than scoring 0.
        "V5_price_to_fcf":   (band_low(m["p_fcf"], B["V5_price_to_fcf"]), 4),
    }


def rules_momentum(m, ctx):
    B = ctx.cfg["bands"]
    return {
        "M1_return_12m":  (band(m["ret_12m"], B["M1_return_12m"]), 6),
        "M2_return_6m":   (band(m["ret_6m"], B["M2_return_6m"]), 4),
        "M3_drawdown":    (band(m["drawdown"], B["M3_drawdown"]), 4),
        "M4_volatility":  (band_low(m["volatility"], B["M4_volatility"]), 3),
        "M5_liquidity":   (band(m["dollar_volume"], B["M5_liquidity"]), 3),
    }


RULESETS = {
    "growth": rules_growth,
    "profitability": rules_profitability,
    "quality": rules_quality,
    "health": rules_health,
    "valuation": rules_valuation,
    "momentum": rules_momentum,
}

# A sector needs this many members with a value before its own distribution is
# trusted; below it the company is ranked against the whole universe instead.
# Ranking against 4 peers produces percentiles of 0/25/50/75/100 and nothing in
# between, which would be noise dressed as precision.
MIN_PEERS = 20


class PeerContext:
    """Percentile-rank lookups within a company's own sector.

    Built in one pass over every company's metrics, then queried while scoring.
    That ordering is why main() is two-pass: a peer-relative rule cannot be
    evaluated until every peer has been measured.

    This is also the mechanism that keeps the score honest as macro conditions
    move -- percentiles re-baseline every run, so a market-wide de-rating
    shifts everybody's multiples without shifting anybody's score.
    """

    # Margins: higher is better. Multiples: LOWER is better, so their
    # percentile is inverted at query time.
    PEER_METRICS = ("net_margin", "ebit_margin", "gross_margin", "pe", "ps",
                    "equity_ratio")
    LOWER_IS_BETTER = ("pe", "ps")
    # Banks and insurers are pooled into one distribution under this key rather
    # than ranked sector by sector -- an insurer's capital ratio is comparable
    # to a bank's, and splitting them would leave each pool thin.
    EXEMPT_POOL = "__EXEMPT__"

    def __init__(self, metrics_by_ticker, sector_of, cfg):
        self.cfg = cfg
        self.sector_of = sector_of
        self.dists = collections.defaultdict(list)
        for t, m in metrics_by_ticker.items():
            sec = sector_of.get(t) or ""
            for key in self.PEER_METRICS:
                v = m.get(key)
                # A negative P/E is not "cheap", it means there are no
                # earnings. Excluding it keeps the distribution a ranking of
                # actual valuations; those companies score 0 on the rule.
                if v is None or (key in self.LOWER_IS_BETTER and v <= 0):
                    continue
                self.dists[(sec, key)].append(v)
                self.dists[("__ALL__", key)].append(v)
                if sec in LEVERAGE_EXEMPT:
                    self.dists[(self.EXEMPT_POOL, key)].append(v)
        for k in self.dists:
            self.dists[k].sort()

    def _pool(self, sector, key):
        vals = self.dists.get((sector, key), [])
        if len(vals) < self.cfg["min_peers"]:
            vals = self.dists.get(("__ALL__", key), [])
        return vals

    def pctile(self, m, key, pool=None):
        """Where this company sits among peers, 0-100, already oriented so
        that HIGHER always means better.

        `pool` overrides the sector pool with the pooled LEVERAGE_EXEMPT
        distribution. It deliberately does NOT fall back to the whole universe
        the way _pool does: ranking a bank's 0.22 equity/assets against a
        universe whose median is 0.43 puts nearly every bank in the bottom
        band, which is the exact error the exemption existed to prevent. If the
        pool is too thin the rule is dropped instead.
        """
        v = m.get(key)
        if v is None:
            return None
        if key in self.LOWER_IS_BETTER and v <= 0:
            return None
        if pool is not None:
            vals = self.dists.get((self.EXEMPT_POOL, key), [])
            if len(vals) < self.cfg["min_peers"]:
                return None
        else:
            vals = self._pool(m.get("_sector") or "", key)
        if not vals:
            return None
        lo = bisect.bisect_left(vals, v)
        hi = bisect.bisect_right(vals, v)
        pct = 100.0 * ((lo + hi) / 2.0) / len(vals)
        return (100.0 - pct) if key in self.LOWER_IS_BETTER else pct

    def median(self, sector, key):
        vals = self._pool(sector, key)
        return statistics.median(vals) if vals else None

    def peer_points(self, m, key, rule):
        """Percentile -> points for `rule`, using the configured bands."""
        mx = self.cfg["rule_max"][rule]
        pct = self.pctile(m, key)
        if pct is None:
            return (None, mx)
        for lo, frac in self.cfg["peer_bands"]:
            if pct >= lo:
                return (round(frac * mx, 3), mx)
        return (0.0, mx)


def flags(m, pillars, cfg=DEFAULTS):
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
    if m.get("pe_unreliable"):
        f.append("pe_unreliable")
    if (m.get("_sector") or "") in LEVERAGE_EXEMPT:
        f.append("leverage_not_applicable")
    if m.get("negative_equity"):
        f.append("negative_equity")
    if m.get("non_usd"):
        f.append("non_usd_reporting")
    if (m.get("eq_gap") is not None
            and m["eq_gap"] >= cfg["earnings_quality"]["flag_at"]):
        f.append("earnings_below_op_line")
    if m.get("net_debt_to_ebitda") is not None and m["net_debt_to_ebitda"] > 5:
        f.append("high_leverage")
    if (m.get("interest_coverage") is not None and m["interest_coverage"] < 1.5
            and m.get("net_debt") and m["net_debt"] > 0):
        f.append("thin_interest_cover")
    return f




def score_one(m, ctx):
    cfg = ctx.cfg
    weights = cfg["pillar_weights"]
    pillar_scores, detail, scored, possible = {}, {}, 0.0, 0.0
    for name, fn in RULESETS.items():
        w = float(weights.get(name, 0.0))
        got = cap = 0.0
        for rule, (pts, mx) in fn(m, ctx).items():
            detail[rule] = pts
            if pts is not None:
                got += pts
                cap += mx
        if cap > 0 and w > 0:
            pillar_scores[name] = got / cap * w
            scored += pillar_scores[name]
            possible += w
        else:
            pillar_scores[name] = None

    if possible == 0:
        return None, pillar_scores, detail, 0.0, []

    total = scored / possible * 100
    have = sum(1 for v in detail.values() if v is not None)
    confidence = have / len(detail)

    fl = flags(m, pillar_scores, cfg)
    for f in fl:
        if f in cfg["caps"]:
            total = min(total, cfg["caps"][f])
    return total, pillar_scores, detail, confidence, fl


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=0, help="print an N-name leaderboard")
    ap.add_argument("--min-confidence", type=float, default=0.0)
    ap.add_argument("--sp500-only", action="store_true")
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--config", default=None,
                    help="JSON file deep-merged over DEFAULTS (weights, bands, caps)")
    ap.add_argument("--dump-config", action="store_true",
                    help="print the active configuration and exit")
    ap.add_argument("--as-of", default=None, metavar="YYYY-MM",
                    help="score as the company looked at this month; all later "
                         "quarters and prices are discarded")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.dump_config:
        json.dump(cfg, sys.stdout, indent=2)
        print()
        return

    quarters, annuals = load_periods()
    px, dv, pe_hist, mc_hist = load_prices()
    # Optional. In --as-of mode valuation comes from data_monthly.csv on
    # purpose -- a snapshot is TODAY's prices and would leak the future into a
    # historical score -- so a point-in-time run has no snapshot to give.
    snaps = load_keyed(S_FILE) if os.path.exists(S_FILE) else {}
    uni = load_keyed(U_FILE)
    sic = load_keyed(SIC_FILE) if os.path.exists(SIC_FILE) else {}
    annual_only = set(annuals) - set(quarters)
    print("loaded %d tickers with quarterly history, %d annual-only (20-F/40-F "
          "filers)" % (len(quarters), len(annual_only)), file=sys.stderr)

    # Sector resolution: the Wikipedia GICS sector wins where it exists, since
    # the S&P names are actually indexed on it, and the SIC-derived sector
    # fills the other ~3,200. Without data_sic.csv only ~500 names are
    # rankable against peers and the peer rules fall back to the whole universe.
    # The sector is re-derived HERE from the raw SIC code rather than read from
    # the sector_sic column data_sic.csv already carries. The code is the
    # durable fact and the mapping is a judgement call that will keep being
    # tuned -- deriving at scoring time means a mapping fix costs nothing,
    # where trusting the stored column would mean re-fetching 3,717 filings
    # from SEC every time a range moved.
    sector_of, src = {}, collections.Counter()
    for t, u in uni.items():
        gics = (u.get("sector") or "").strip()
        rec = sic.get(t, {})
        sic_sec = sector_for(rec.get("sic")) or (rec.get("sector_sic") or "").strip()
        sector_of[t] = gics or sic_sec
        src["gics" if gics else ("sic" if sic_sec else "none")] += 1
    print("sector source: %d GICS, %d SIC, %d unclassified"
          % (src["gics"], src["sic"], src["none"]), file=sys.stderr)

    # PASS 1 -- measure everyone. Peer-relative rules cannot be evaluated
    # until every peer has been measured, which is why this is split in two.
    metrics, not_rated, on_annual = {}, 0, 0
    for t in uni:
        qrows = quarters.get(t, [])
        # Quarterly wins where it exists -- it is the finer measurement. The
        # annual path is a fallback for filers who never report quarterly, not
        # an alternative for those who do.
        if len(qrows) >= MIN_QUARTERS:
            m = compute(t, qrows, snaps.get(t), px, dv, pe_hist, mc_hist,
                        args.as_of, ppy=4)
        else:
            m = compute(t, annuals.get(t, []), snaps.get(t), px, dv, pe_hist,
                        mc_hist, args.as_of, ppy=1)
            if m is not None:
                on_annual += 1
        if m is None:
            not_rated += 1
            continue
        m["_sector"] = sector_of.get(t, "")
        metrics[t] = m

    ctx = PeerContext(metrics, sector_of, cfg)

    # PASS 2 -- score against the peer distributions built above.
    rows = []
    for t, m in metrics.items():
        u = uni[t]
        total, pillars, detail, conf, fl = score_one(m, ctx)
        if total is None:
            not_rated += 1
            continue
        rows.append({
            "ticker": t,
            "company": u["company"],
            "sector": sector_of.get(t, ""),
            "sector_src": ("gics" if (u.get("sector") or "").strip() else
                           ("sic" if sector_of.get(t) else "")),
            "sic": sic.get(t, {}).get("sic", ""),
            "sic_description": sic.get(t, {}).get("sic_description", ""),
            "score": round(total, 1),
            "band": next(b for lo, b in cfg["score_bands"] if total >= lo),
            "confidence": round(conf, 2),
            "periods": m["quarters"],
            "period_basis": "annual" if m.get("ppy") == 1 else "quarterly",
            "growth_basis": m["growth_basis"],
            "as_of": args.as_of or "latest",
            "window_end": m.get("window_end", ""),
            "valuation_basis": m.get("valuation_basis", ""),
            **{p: (round(pillars[p], 1) if pillars[p] is not None else "")
               for p in PILLARS},
            "flags": "|".join(fl),
            **{k: (round(m[k], 2) if isinstance(m.get(k), float) else m.get(k, ""))
               for k in ["rev_cagr", "rev_yoy", "eps_cagr", "net_margin",
                         "ebit_margin", "eq_gap", "gross_margin", "margin_delta",
                         "profitable_q", "growth_sd", "share_change", "pe", "ps",
                         "peg", "pe_vs_own", "ret_12m", "ret_6m", "drawdown",
                         "volatility", "dollar_volume", "market_cap",
                         "fcf_ttm", "fcf_margin", "fcf_conversion", "p_fcf",
                         "pe_reported", "pe_mcap", "market_cap_source",
                         "currency", "equity", "total_debt", "net_debt", "cash", "assets",
                         "net_debt_to_ebitda", "interest_coverage",
                         "current_ratio", "debt_to_equity", "roe",
                         "equity_ratio", "cash_runway_q", "revenue_size"]},
            "peer_net_margin_median": (
                round(ctx.median(m["_sector"], "net_margin"), 2)
                if ctx.median(m["_sector"], "net_margin") is not None else ""),
            "peer_pctile_net_margin": (
                round(ctx.pctile(m, "net_margin"))
                if ctx.pctile(m, "net_margin") is not None else ""),
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

    # Not rows[0].keys(). Banks carry HB1_capital_vs_peers where everyone else
    # carries H1..H6, so whichever company happens to sort first would decide
    # the header and DictWriter would raise on the first row of the other kind.
    # First-seen order across all rows keeps the layout stable and complete.
    fieldnames = list(dict.fromkeys(k for r in rows for k in r))
    for r in rows:
        for k in fieldnames:
            r.setdefault(k, "")

    with open(args.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if v is None else v) for k, v in r.items()})

    print("scored %d (%d on an annual basis), not rated %d  (as-of %s) -> %s"
          % (len(rows), sum(1 for r in rows if str(r["growth_basis"]).startswith("annual")),
             not_rated, args.as_of or "latest", args.out),
          file=sys.stderr)
    dist = collections.Counter(r["band"] for r in rows)
    for _, b in cfg["score_bands"]:
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
