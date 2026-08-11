#!/usr/bin/env python3
"""
Reconcile share counts so that price, earnings and market cap share one basis.

WHY THIS EXISTS
  Three inputs in this pipeline sit on three DIFFERENT share bases, and the
  arithmetic that combines them was silently wrong:

    * Yahoo `close`            -- split-ADJUSTED to today's basis
    * SEC `eps_diluted`        -- AS FILED, never restated backwards
    * SEC `shares_outstanding` -- the current dei tag, when it is right

  fetch_prices.py computed `pe_trailing = close / eps_trailing`, mixing the
  first two. Any company that has split reads wrong by the split factor:
  Booking came out at a P/E of 1.16 against a true ~20.7, and 381 tickers show
  the same inconsistency. It also computed `market_cap = close x shares`, which
  is wrong whenever the dei tag is wrong -- Mastercard is tagged 122.5M shares
  against an actual ~910M, implying a $70bn company rather than ~$520bn.

THE FIX
  The MOST RECENT quarter's as-filed EPS is already on today's basis, because
  nothing has split since it was filed. So

      implied_shares = net_income / eps_diluted   (latest reliable quarter)

  is a current-basis share count derived independently of the dei tag, and the
  two can cross-check each other. Where they agree, both are sound. Where they
  disagree the implied count wins: it is the one guaranteed to be consistent
  with the earnings we are about to divide by.

  TTM EPS is then recomputed as `net_income_ttm / shares`, NOT as a sum of four
  as-filed quarterly EPS figures. Summing across a split boundary adds numbers
  from two different bases -- this is what made Booking's TTM EPS $166.53 when
  its current-basis figure is nearer $7.

  The result is internally consistent by construction:
      market_cap = price x shares
      eps        = net_income_ttm / shares
      pe         = price / eps  ==  market_cap / net_income_ttm
"""

import statistics

# An EPS this close to zero makes net_income/eps explode. The floor is relative
# to the company's own EPS because "near zero" depends on scale: $0.05 is
# unremarkable for a $0.30 filer and a near-breakeven event for Booking at
# ~$100/share.
EPS_FLOOR_FRACTION = 0.25
EPS_FLOOR_MIN = 0.02

# How far the dei tag and the implied count may differ before the dei tag is
# rejected. 25% is wide enough for ordinary timing drift between a quarter-end
# diluted average and a cover-page count taken weeks later.
AGREE_TOLERANCE = 0.25


def _num(v):
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def implied_shares(rows, eps_key="eps_diluted", ni_key="net_income",
                   derived_key="q4_derived", floor_from=None):
    """[(period_end, share_count)] for quarters where the count can be trusted.

    Derived-Q4 rows are excluded: fetch_sec_fundamentals.py treats eps_diluted
    as additive when deriving Q4, but EPS is only additive if the share count
    held still -- the very thing being measured. Left in, Nvidia's FY2024 Q4
    implies -49bn shares.

    `floor_from` supplies the rows the near-zero EPS floor is calibrated on.
    It MUST be the same window the caller intends to use. Calibrating on the
    full history breaks any company whose EPS scale has changed: Booking's
    older quarters sit on a pre-split basis, so a whole-history median put the
    floor above every recent EPS (0.40, 1.10, 1.36, 2.53) and admitted only two
    corrupt outliers (84.41, 79.66) -- yielding 34M shares against a true 775M.
    """
    if floor_from is None:
        # Absolute guard only: no assumption about what "small" means.
        floor = EPS_FLOOR_MIN
    else:
        eps_all = [abs(v) for v in (_num(r.get(eps_key)) for r in floor_from)
                   if v is not None]
        if not eps_all:
            return []
        floor = max(EPS_FLOOR_MIN, EPS_FLOOR_FRACTION * statistics.median(eps_all))
    out = []
    for r in rows:
        if str(r.get(derived_key, "")).strip().lower() == "true":
            continue
        ni, eps = _num(r.get(ni_key)), _num(r.get(eps_key))
        if ni is None or eps is None or abs(eps) < floor:
            continue
        s = ni / eps
        if s > 0:
            out.append((r.get("period_end"), s))
    return out


CLUSTER_RATIO = 2.0


def current_shares(rows, lookback=8):
    """Current-basis diluted share count from the most recent quarters.

    The most recent quarters' as-filed EPS is already on today's share basis,
    because nothing has split since they were filed -- that is what makes this
    an independent check on the dei tag.

    WHY CLUSTERING RATHER THAN A MEDIAN WITH A MAGNITUDE FLOOR
      A filer's EPS series can carry values on TWO share bases at once. Booking
      interleaves 0.40 / 1.36 / 2.53 (consistent with ~775M shares: its Q1 net
      income of 333M over 775M is $0.43) with 74.34 / 84.41 (implying ~34M).
      Only one basis can be right, and magnitude does not say which -- an
      earlier version screened out "small" EPS as unreliable and thereby kept
      precisely the corrupt values, returning 34M shares against a true ~775M
      and a P/E of 0.89 against ~20.

      Clustering makes no assumption about scale. Values are grouped where
      consecutive ones sit within CLUSTER_RATIO of each other and the LARGEST
      group wins, so the basis the filer used most often is the one adopted.
      Ties go to whichever cluster holds the most recent quarter, since that is
      the basis today's price is quoted on.
    """
    recent = rows[-lookback:]
    if not recent:
        return None
    # No relative floor here -- only the absolute guard against dividing by a
    # near-zero EPS. See the docstring for why magnitude cannot be trusted.
    series = implied_shares(recent, floor_from=None)
    if not series:
        return None

    ordered = sorted(series, key=lambda kv: kv[1])
    clusters = [[ordered[0]]]
    for pe, val in ordered[1:]:
        if val <= clusters[-1][-1][1] * CLUSTER_RATIO:
            clusters[-1].append((pe, val))
        else:
            clusters.append([(pe, val)])

    latest_period = max(p for p, _ in series if p)
    best = max(clusters,
               key=lambda c: (len(c), any(p == latest_period for p, _ in c)))
    return statistics.median([v for _, v in best])


def reconcile(dei_shares, implied):
    """(shares, source, agree) -- one authoritative current share count.

    `agree` is None when only one estimate existed, so callers can tell an
    unchecked number from a corroborated one.
    """
    dei = _num(dei_shares)
    if dei is not None and dei <= 0:
        dei = None
    if dei and implied:
        agree = abs(dei - implied) / max(dei, implied) <= AGREE_TOLERANCE
        # The implied count wins a disagreement: it is the one guaranteed
        # consistent with the earnings the caller is about to divide by.
        return (dei if agree else implied,
                "dei" if agree else "implied_override",
                agree)
    if implied:
        return implied, "implied", None
    if dei:
        return dei, "dei", None
    return None, "", None


def ttm_net_income(rows, ni_key="net_income", n=4):
    """Sum of the last `n` quarters of net income, or None if any is missing."""
    vals = [_num(r.get(ni_key)) for r in rows[-n:]]
    if len(vals) < n or any(v is None for v in vals):
        return None
    return sum(vals)
