# US_2007_onwards — deep-history experiment

An isolated copy of the pipeline for pushing the data back toward 2007. Nothing
here writes outside this folder; the working pipeline in the parent directory is
untouched and stays the reference.

## Why a separate folder

The parent dataset starts **2020-07** and its analysis rests on ~8 distinct
calendar quarters (see `../HANDOFF.md` §19). Extending history is the only way
to get independent evidence, but it means re-running every fetcher with
different settings and re-deriving every score — changes that would otherwise
overwrite results the parent analysis depends on.

## What was copied, and what deliberately was not

| copied | why |
|---|---|
| every `.py` — fetchers, scorer, `build_excel`, the three analysis tools | the pipeline itself |
| `test_scoring.py`, `test_pipeline.py` | 63 tests, all passing here |
| `universe.csv` | the ticker seed. **Will be replaced** — see the survivorship note |
| `data_sic.csv` | 3,717 SEC calls, and industry classification does not change with history depth |

**Not copied:** `data_quarterly.csv`, `data_monthly.csv`, `data_snapshot.csv`,
the workbooks, and every `scores_asof_*.csv`. All are regenerated, and
regenerating them with deeper history is the entire point.

The parent `.gitignore` already covers this folder, so generated artifacts here
stay untracked automatically — verified for all six filename patterns.

## The step-by-step plan

Deliberately incremental. Each step is a measurement, and the answer decides
whether the next one is worth doing.

1. **Probe, do not fetch.** `companyfacts` returns a filer's ENTIRE history in
   one request — the pipeline's `YEARS = 6` only filters client-side. So
   coverage by year can be measured from a stratified sample of ~300 tickers
   without a full pull.
2. **Measure per calendar year**: what fraction of companies have ≥4 quarterly
   periods, and what fraction have ≥1 annual period.
3. **Decide per era, on the 75% rule**: quarterly where quarterly coverage
   holds up, annual where it does not. These need not be the same decision for
   2009 and 2019.
4. Only then extend the fetchers and re-derive scores.

## Two constraints known before starting

**XBRL was phased in, so 2007 may be unreachable.** SEC mandated it for large
accelerated filers from mid-2009, accelerated filers mid-2010, and everyone
else mid-2011. Before that there is no XBRL to fetch at any depth, whatever
`YEARS` is set to. Pre-2009 would need a different source entirely
(the financial statement data sets, or parsing the filings). Step 1 exists to
confirm this empirically rather than assume it.

**Survivorship is the harder problem, and more history makes it worse.**
`universe.csv` is index membership *as of collection date*, so every company
that failed, delisted or was acquired between 2007 and now is simply absent.
That is precisely the population a loser-avoidance screen must be judged
against (`../HANDOFF.md` §17), so a backtest on today's survivors would flatter
the score by construction — and flatter it more, the further back it reaches.

A point-in-time universe is the real work of this project. Deeper price history
without it produces a longer backtest that is *more* biased, not less.

## Settings that gate history depth

| file | constant | now |
|---|---|---|
| `fetch_sec_fundamentals.py` | `YEARS` | 6 |
| `fetch_prices.py` | `YEARS` | 5 |

Raising these is necessary and nowhere near sufficient.

---

## Ticker recovery — `recover_tickers.py` (built, measured, NOT sufficient)

The blocker found by probing: fundamentals for a ceased filer are complete and
keyed on CIK, but prices are keyed on ticker and **the link is cut**:

| source | ticker for a ceased filer |
|---|---|
| submissions API `tickers` | blank, 97% of 120 sampled |
| `dei:TradingSymbol` in companyfacts | absent, 40 of 40 |
| DERA `sub.txt` / `num.txt` / `tag.txt` | **no ticker field exists** |

So the parser digs it out of the filings themselves. Four strategies, tried in
order: the filing's own XBRL instance (`dei:TradingSymbol`), the Section 12(b)
cover-page table, an exchange parenthetical, and a quoted symbol.

### Measured, on companies whose ticker we already know

| regime | symbol found | of those, matches today's ticker |
|---|---|---|
| modern filings (any date) | 98% | **100%** |
| **pre-2019 only** | **54%** | **76%** |

The modern number is the easy case and not the one that matters: 95% of those
successes come from `dei:TradingSymbol`, which cover-page tagging only made
mandatory in **2019**. Ceased filers stopped before that, so the pre-2019 row
is the real forecast — **roughly half get a symbol at all**.

### Two findings that change how the output must be read

**Most "mismatches" are correct.** Of 9 disagreements, nearly all are renames
where the parser returned the ticker that was RIGHT AT THE TIME: Elevance was
`ANTM`, Harrow was `IMMY`, aTyr was `LIFE`, Usio was `PYDS`. For a backtest the
historical ticker is what we WANT — the price series as it actually traded. The
"truth" being compared against (today's ticker) is the wrong benchmark, so 76%
understates accuracy. Only Fidelity National→`STC` and Service Properties→`WYND`
look like genuine regex failures, both from `exchange-paren`, which scored 0/2
and should probably be dropped.

**Two bugs found and fixed by validation, not by reading code:** SPACs tag
unit, share and warrant all as `TradingSymbol`, and the parser took the unit
(`EVOXU` for `EVOX`); and a bare capital letter passed as a ticker (`C` for
Defi Technologies). Both invisible without a labelled test set.

### Where this leaves the backtest

At ~54% recovery, roughly **half the delisted companies still cannot be
priced** — and there is no reason to think the recoverable half is a random
sample of the other. A company that wound down quietly leaves thinner filings
than one acquired at a premium, so the recovered subset likely skews toward
better outcomes. That is survivorship bias returning through the back door, in
a form that is harder to see.

Options, in increasing order of honesty:

1. Improve the pre-2019 strategies. `exchange-paren` is actively harmful (0/2);
   old filings are often plain text rather than HTML and need different
   handling. Might push recovery to 70-80%, not to 100%.
2. Use a paid point-in-time database (CRSP, WRDS). This is precisely the
   problem they exist to solve.
3. Restrict the backtest to a period and universe where coverage is near
   complete, and state the limitation rather than paper over it.

---

## Improvements made, and the scope they justify

### (1) Pre-2019 parsing — improved, then hit diminishing returns

Three changes, in the order they mattered:

* **Load the older submission files.** `filings.recent` in the submissions API
  holds only the last ~1,000 filings; everything older sits in
  `filings.files[]` and must be fetched separately. Not doing so hid every
  pre-2019 filing for an active filer — 10 of 70 validation companies returned
  "no-annual-filing" for that reason alone. **This was the single biggest win.**
* **Replaced `exchange-paren` with `item5` and `exchange-context`.** The old
  strategy scored 0/2 by taking the first ticker-shaped word after any
  parenthesis. The replacements anchor on Item 5 ("Market for Registrant's
  Common Equity"), which is where pre-2019 filings actually name the ticker —
  the cover-page "Trading Symbol" column did not exist until the 2019 rule.
* **Read 2.5 MB of the document instead of 600 KB**, and fall back to the full
  `{accession}.txt` submission when `primaryDocument` is empty, as it often is
  for older filings.

| | before | after |
|---|---|---|
| symbol found (pre-2019) | 54% | **60%** |
| match rate | 76% | **81%** |

Honest assessment: **short of the 70-80% recovery I predicted.** The remaining
28 of 70 reach the document and match no pattern. Further regex work looked
like poor value against the alternative of narrowing scope.

### (2) Recovery depends sharply on WHEN a company stopped filing

| last filed | population | recovered |
|---|---|---|
| 2018 | 59 | 58% |
| 2019 | 46 | 50% |
| **2020** | 38 | **83%** |
| **2021** | 30 | **83%** |
| **2022** | 43 | **83%** |
| **2023** | 35 | **75%** |

| cohort | recovered |
|---|---|
| ceased 2018-2020 | **64%** |
| ceased 2021+ | **81%** |

The break sits exactly where the 2019 cover-page rule starts showing up in
filings, which put `dei:TradingSymbol` in the XBRL.

### (3) Scope decision: start the backtest at 2020, not 2007

**2007 is not reachable and 2018 is not defensible.** At ~50-58% recovery for
companies that stopped filing in 2018-2019, half the failures are unpriceable,
and the recovered half is not a random sample of them — a company wound down
quietly leaves thinner filings than one acquired at a premium, so what survives
recovery skews toward better outcomes. A backtest there would be biased in a
way that looks complete.

**From 2020 the picture holds up:** ~83% of ceased filers recoverable, on top of
92-98% quarterly fundamentals coverage and full DERA point-in-time universes.

The residual limitation, to be stated in any result rather than buried:
**roughly one delisted company in five still cannot be priced even from 2020**,
and those are more likely to be the quiet failures than the clean exits.

A 2020-2026 point-in-time backtest is ~6 years and 24 quarters — against the 8
distinct quarters the current analysis rests on, a threefold improvement with
survivorship largely, though not entirely, addressed.

---

## STOP — the price probe kills the free-data path

Prices were probed first precisely so this would surface before the universe
and fundamentals were built. It did.

### Yahoo does not serve delisted price history

Eight well-known delistings, all large and recently traded:

| ticker | event | `download()` | `Ticker.history(max)` |
|---|---|---|---|
| XLNX | Xilinx, acquired by AMD 2022 | EMPTY | EMPTY |
| ATVI | Activision, acquired by Microsoft 2023 | EMPTY | EMPTY |
| TWTR | Twitter, taken private 2022 | EMPTY | EMPTY |
| VMW | VMware, acquired 2023 | EMPTY | EMPTY |
| SGEN | Seagen, acquired by Pfizer 2023 | EMPTY | EMPTY |
| FRC | First Republic, failed 2023 | EMPTY | EMPTY |
| SIVB | SVB Financial, failed 2023 | EMPTY | EMPTY |
| BBBY | Bed Bath & Beyond, liquidated 2023 | 60 rows | 292 rows |

Survivors are fine — AAPL and MSFT return full history, and `yfinance`'s
cookie/crumb handshake also fixes the HTTP 429 that raw requests hit. The
problem is specific and total: **when a listing ends, the history goes with it.**

### The one "success" is worse than the failures

BBBY returned 292 monthly rows spanning 2002-05 to **2026-08**. Bed Bath &
Beyond was liquidated in 2023. The series continues because Overstock bought
the brand and took the ticker — so those rows are a **different company**.

A recovered ticker can therefore return prices belonging to whoever inherited
the symbol. That is worse than an empty series, because it looks like data.
Any ticker-based price lookup for a delisted company needs a listing-date guard
before it can be trusted.

### What this means

`recover_tickers.py` is not wasted — it still identifies companies — but
recovering a ticker at 60-83% does not help when the price series behind it no
longer exists at any recovery rate. **The binding constraint was never the
CIK-to-ticker link; it is that free price sources do not retain delisted
history.** stooq is also unusable (JavaScript proof-of-work bot check).

So a survivorship-corrected backtest is not reachable from SEC + Yahoo alone,
at 2020 or any other start date. The remaining routes:

1. **A paid source that retains delisted history** — CRSP, Norgate, EODHD,
   Polygon. This is the specific thing they sell, and it is the only route that
   actually solves it.
2. **Survivors-only, bias stated plainly.** Still ~24 quarters, still a
   threefold improvement on the current 8 — but it cannot test the
   loser-avoidance claim, since the losers are what is missing.
3. **Treat delisted names as a total loss (-100%).** Wrong often enough to be
   dangerous: acquisitions frequently close at a PREMIUM, and roughly half the
   disappearances here are acquisitions rather than failures.

Option 2 is honest and cheap. Option 1 is correct. Option 3 should not be used.

---

## Point-in-time universes — `build_universe_pit.py`

**The flaw it fixes:** every score window used one `universe.csv` — today's
listings. A score computed "as at 2024-12" was evaluated against the companies
that exist *now*, so anything delisted in between was absent from **every**
window. Survivorship was baked in at the universe level, before any scoring
rule ran.

These are built from who actually filed each quarter, per SEC's DERA data sets.
`sub.txt` also carries `sic` and `afs`, so industry and filer size come along
and are themselves point-in-time.

Only ~3 MB is downloaded per quarter: `sub.txt` is the first entry in each
89 MB ZIP, so a ranged GET plus a raw inflate gets it without touching
`num.txt` (397 MB of numeric data not needed here).

### 21 quarters, 2021 Q1 – 2026 Q1

| quarter | filers | priceable | coverage |
|---|---|---|---|
| 2021q1 | 5,648 | 3,701 | **66%** |
| 2021q4 | 6,455 | 4,048 | 63% |
| 2022q4 | 6,452 | 4,279 | 66% |
| 2023q4 | 5,985 | 4,417 | 74% |
| 2024q4 | 5,643 | 4,603 | 82% |
| 2025q4 | 5,565 | 4,919 | 88% |
| 2026q1 | 5,605 | 5,089 | **91%** |

126,563 filer-quarters, 74% priceable overall. 2026 Q2 is not published yet.

### The gradient is the finding, not the average

**Coverage climbs monotonically from 66% to 91%** — because the further back a
window sits, the more of its companies have since vanished. So a survivors-only
backtest is **most biased exactly where it has the most forward data**:

* a 2021 Q1 window has 20 quarters of forward returns and is blind to ~34% of
  its own universe;
* a 2026 Q1 window sees 91% of its universe and has almost no forward data.

Any result computed across these windows mixes those two regimes. Comparing an
early window against a late one is comparing different degrees of blindness,
not different market conditions.

### Decomposing the gap (2021 Q1)

Of 1,947 filers with no current ticker:

| | |
|---|---|
| still filing in 2026 Q1 — alive, just not exchange-listed | 224 |
| absent by 2026 Q1 — genuinely stopped filing | **1,723** |

So the true delisting blind spot for 2021 Q1 is **31%**, not the headline 34%.
And it is not size-neutral: 1,103 of the disappearances are non-accelerated
(small) filers against 335 large accelerated. Small companies vanish far more
often — which is precisely the population a loser-avoidance screen is supposed
to be catching.

---

## RESULTS — point-in-time, as-filed, 21 windows

Everything below is computed on point-in-time universes with as-filed
fundamentals. Neither was true of the parent analysis.

| | parent analysis | this |
|---|---|---|
| universe | today's listings, all windows | **per-quarter, who actually filed** |
| fundamentals | companyfacts, as restated today | **DERA, as filed then** |
| score windows | 9 | **21** |
| distinct calendar quarters | 8 | **17** |
| companies per window | ~2,000-3,200 | 2,057-4,667 |

### Entry timing — confirmed, and now on 21 windows

| start \ end | 6m | 9m | 12m | 18m | 21m |
|---|---|---|---|---|---|
| **W+0** | +0.163 | +0.165 | +0.162 | +0.180 | **+0.183** |
| W+3 | +0.102 | +0.122 | +0.124 | +0.146 | +0.152 |
| W+6 | | +0.083 | +0.100 | +0.126 | +0.132 |
| W+9 | | | +0.071 | +0.116 | +0.119 |
| W+12 | | | | +0.107 | +0.118 |

Reading down any column, rho falls monotonically with every quarter of delay —
the same result as the survivor-anchored run, now on more than twice the
windows. **Buy at window_end.** Delayed recognition remains contradicted.

### The 2021 windows are thin, not weak

DERA archives begin 2021q1, so a window dated 2021-03 has about one quarter of
history behind it. Those windows score 350-592 companies against 2,500+ later,
and their rho is noise. Excluding them:

| block | n_win | mean rho | min | max |
|---|---|---|---|---|
| W+0 → W+3 | 17 | +0.151 | +0.005 | +0.246 |
| W+0 → W+9 | 15 | +0.199 | +0.128 | +0.277 |
| W+0 → W+12 | 14 | +0.207 | +0.131 | +0.262 |
| **W+0 → W+18** | 12 | **+0.228** | +0.168 | +0.269 |
| **W+0 → W+21** | 11 | **+0.230** | +0.186 | +0.270 |

**Every one of those windows is positive.** The floor on W+0 → W+21 is +0.186 —
better than the mean of the 3-month block. Longer holding is both stronger and
far more stable, exactly as the survivor-anchored run suggested, but now with a
minimum that never approaches zero.

**On this evidence: buy at window_end, hold 18-21 months.**

### Removing the bias made the signal SMALLER, then time made it larger

W+0 → W+9 was +0.208 in the parent analysis and is **+0.199** here on
comparable windows — slightly lower, in the direction expected once
restatement lookahead and survivor-anchored universes are removed. The parent
numbers were mildly flattered. The gain came from length, not from the fix:
extending to 18-21 months reaches +0.23.

### Market direction — the effect keeps shrinking as the sample grows

| claim | sample | rank corr |
|---|---|---|
| first estimate | 8 blocks | **−0.810** |
| de-duplicated by quarter | 8 quarters | **−0.595** |
| **point-in-time** | **17 quarters** | **−0.429** |

Falling quarters mean +0.144, rising +0.081. The direction has survived every
enlargement of the sample and the effect size has fallen every time. Treat it
as a real but modest tendency: the score does more work in falling markets, and
it is not the dominant factor it first appeared to be.

### What this still cannot tell you

Prices come from Yahoo, which drops delisted history, so **only survivors are
priced**. The point-in-time universes make the gap visible and measurable — 66%
priceable in 2021q1 rising to 91% in 2026q1 — but not closed. The
loser-avoidance claim from the parent analysis therefore remains untested: the
losers are still the missing population.

---

## What predicts SURVIVAL — `survival_test.py`

A different question from the return work, and one the data answers directly:
point-in-time universes record who filed each quarter, so "still filing N
quarters later" is an observed outcome, not a model. Crucially it is available
for the delisted companies whose PRICES we cannot get — so unlike the return
analysis, this is **not survivorship-limited**.

Measured as AUC: the probability a survivor ranks above a non-survivor. 0.5 is
no signal.

### Raw metrics (base 2022q4, 8 quarters ahead, 6,452 companies, 79% survived)

| metric | AUC | median SURVIVOR | median GONE |
|---|---|---|---|
| **equity / assets** | **0.712** | 0.37 | **−0.03** |
| revenue (size) | 0.679 | $600M | $79M |
| assets (size) | 0.679 | $747M | $179M |
| current ratio | 0.669 | 1.81 | 0.80 |
| net margin | 0.660 | 1.7% | −34.0% |
| FCF margin | 0.642 | 1.1% | −14.5% |
| interest coverage | 0.633 | 1.15 | −3.90 |
| cash runway (quarters) | 0.613 | 3.75 | 2.30 |
| ROA | 0.562 | 0.5% | −0.8% |
| profitable (yes/no) | 0.536 | | |
| asset turnover | 0.526 | | |
| **debt / assets** | **0.517** | 0.22 | 0.18 |

Stable across base quarters (2021q4 and 2023q2 reproduce the ordering).

### Four things this says

**Negative equity is the signature of a company about to disappear.** The
median company that vanished had equity of **−3% of assets**. `equity/assets`
is the strongest single raw predictor and the rubric does not score it at all —
there is a `negative_equity` flag that affects nothing.

**Leverage is not the killer; insolvency is.** `debt/assets` scores 0.517 —
essentially no signal — while the rubric spends 3 points on debt/equity. Debt
is survivable. Negative book equity is not.

**Size matters enormously and is barely used.** Survivors are 7-10x larger by
revenue and assets. The rubric uses size only as a `micro_cap` flag and inside
liquidity.

**Being profitable barely predicts survival (0.536), but HOW profitable does
(0.660).** A company at −34% net margin is dying; one at breakeven is not
meaningfully safer than one at +2%. The binary throws away what matters.

### The existing rubric, tested on the same outcome

| | AUC | median SURVIVOR | median GONE |
|---|---|---|---|
| **health pillar** | **0.818** | 11.7 | 0.0 |
| momentum pillar | 0.769 | 9.2 | 2.0 |
| **total score** | **0.765** | 47.2 | 23.6 |
| confidence | 0.724 | 0.6 | 0.5 |
| profitability | 0.674 | 7.5 | 1.4 |
| quality | 0.668 | 9.5 | 2.8 |
| **growth** | **0.544** | 13.1 | 10.0 |

The score works — 0.765, and the **health pillar at 0.818 beats every raw
metric tested**, which vindicates adding it.

And **growth fails for the second time**. It showed no relationship with
forward return (−0.014 / +0.003 / +0.042 across windows) and now shows almost
none with survival. Twenty of the hundred points are, on two independent tests,
doing nothing.

`confidence` scoring 0.724 is worth noting too: how much data a company files
is itself a survival signal. Thin filers disappear.

---

## Rubric v2 — reweighted against measured outcomes

Every change below is derived from `survival_test.py` on ONE window (2022q4)
and then validated on windows that had no part in deriving it.

| change | why | evidence |
|---|---|---|
| **added** `equity/assets`, 5 pts | strongest single raw predictor | AUC **0.712** |
| **removed** `debt/equity` | no signal, and cannot express insolvency — a negative denominator flips the ratio's sign | AUC **0.517** |
| **added** `cash_runway`, 2 pts | quarters of life at the current burn | AUC 0.613-0.647 |
| **added** `size_floor`, 2 pts | survivors are 7-10x larger, but as a FLOOR not a ladder — rewarding size outright would tilt to mega-caps and give up the small-cap return premium | AUC 0.679 |
| **removed** return on equity | 2 points on no evidence | AUC 0.562 |
| `current_ratio` 4 pts, weight up | | AUC 0.669 |
| **growth halved**, 20 → 10 | no relationship with forward return (−0.014/+0.003/+0.042) and almost none with survival | AUC **0.544** |

### Validated out of sample — it improves BOTH objectives

**Survival** (AUC, still filing 6 quarters later):

| window | v1 | v2 | delta | |
|---|---|---|---|---|
| 2022q4 | 0.771 | 0.788 | +0.017 | *in-sample* |
| 2023q2 | 0.786 | 0.805 | +0.019 | out-of-sample |
| 2023q4 | 0.796 | 0.808 | +0.012 | out-of-sample |
| 2024q2 | 0.754 | 0.781 | +0.027 | out-of-sample |
| **mean out-of-sample** | **0.774** | **0.792** | **+0.019** | |

The in-sample gain (+0.017) is SMALLER than the out-of-sample mean (+0.019),
which is the signature of a change that generalises rather than one fitted to
its own test.

**Forward return** (Spearman rho), same windows, unchanged blocks:

| block | v1 | v2 | delta |
|---|---|---|---|
| W+0 → W+9 | +0.196 | **+0.228** | +0.032 |
| W+0 → W+12 | +0.199 | **+0.238** | +0.039 |

Positive in 12 of 14 window/block combinations; the two negatives are −0.002
and −0.006. **There was no trade-off** — the feared tension between predicting
survival and predicting return did not materialise, because the added metrics
(solvency, liquidity, runway) identify companies that are about to do badly,
and doing badly shows up in both outcomes.

The largest gains are in 2021q4 (+0.107, +0.132), the data-thinnest window,
where balance-sheet position substitutes for the history the growth and
momentum rules do not yet have.

### What remains untested

Still only survivors are priced, so the loser-avoidance claim is still
unverified on returns. It IS now supported on survival — v2 separates
companies that disappear from companies that do not at AUC 0.79 — which is
the closest thing to a test that free data allows.

---

## EXTENDED TO 2018 Q1 — and the result is a correction, not a confirmation

33 point-in-time windows (2018q1-2026q1), 267,950 fundamental rows across
11,576 CIKs, prices from 2017-01. The motivation was one question: **does the
score survive a real drawdown?**

### Adding 2018-2020 LOWERED the measured signal substantially

| block | 21 windows (2021+) | 33 windows (2018+) |
|---|---|---|
| W+0 → W+9 | +0.204 | **+0.145** |
| W+0 → W+12 | +0.204 | **+0.150** |
| W+0 → W+21 | +0.229 | **+0.176** |

**The earlier numbers came from a favourable stretch.** Everything previously
reported was measured on 2021-2026 and was, on this evidence, roughly 30%
too high. This is the value of extending the window: it did not confirm the
result, it corrected it.

### By era — these cannot be pooled

| era | windows | W+0→W+9 | W+0→W+12 |
|---|---|---|---|
| 2018-2019 (~55-59% priceable) | 8 | +0.099 | +0.092 |
| **2020 (COVID year)** | 4 | **−0.151** | **−0.106** |
| 2021-2022 (~63-66%) | 8 | +0.256 | +0.266 |
| 2023-2026 (~72-91%) | 11 | +0.220 | +0.225 |

**2020 is negative.** For a whole year the score was not merely useless but
inverted — high-scored companies underperformed.

### The stress test, quarter by quarter

| quarter from | market | rho | |
|---|---|---|---|
| 2018-09 | **−16.8%** | **+0.008** | Q4-2018 selloff |
| 2019-12 | **−29.4%** | **+0.121** | COVID crash |
| 2022-03 | −16.3% | +0.238 | rate shock |
| 2020-03 | +23.6% | **−0.091** | COVID rebound |
| 2020-09 | +23.3% | **−0.127** | |
| 2020-12 | +13.6% | **−0.184** | |

Falling quarters mean +0.130, rising +0.068, crash quarters +0.122.

**The drawdown claim is now materially weaker than reported.** Of the three
genuine crashes, one (+0.238) supported it, one (+0.121) partly did, and one
(**+0.008, the Q4-2018 selloff**) showed nothing at all. "Works in drawdowns"
survives as a tendency and dies as a rule.

**The sharp-recovery result is the sharpest new finding.** In the three
quarters when the market rose 13-24% off the COVID low, rho was **−0.091,
−0.127 and −0.184** — consistently and substantially negative. The screen does
not merely lag a violent rebound; it points the wrong way, because the
companies that rebound hardest are the beaten-down cyclicals it ranks last.
That is the same mechanism as the "cannot pick winners" finding, now visible as
a sustained negative rather than an absence of signal.

Market-direction rank correlation is now **−0.379**, continuing to shrink with
every enlargement of the sample: −0.810 → −0.595 → −0.429 → **−0.379**.

### What this means for using it

The score is a **regime-dependent quality screen**. It works in ordinary and
falling markets and it works against you in a sharp recovery. Anyone using it
needs to know which of those they are in — and that is not something the score
itself can tell them.

---

## Top 20 per quarter — `top20_report.py`

660 picks across 33 quarters, with forward returns at 3, 6 and 12 months, in
`top20_by_quarter.csv`. Every return is shown beside the **median of the
universe it was drawn from** over the identical months, because +8% means
nothing until you know whether the alternative was +12%.

### The concentrated selection does considerably better than rho implied

| horizon | beat the universe | mean excess | median excess |
|---|---|---|---|
| +3m | **23 of 33 (70%)** | +2.5 pts | +2.2 pts |
| +6m | 21 of 32 (66%) | +5.7 pts | +6.4 pts |
| **+12m** | **23 of 30 (77%)** | **+10.4 pts** | **+13.1 pts** |

This looks inconsistent with an overall rho of +0.145, and is not. Rho scores
the WHOLE ranking, where the noisy middle and the lottery-ticket bottom
dominate: decile 1 has a median of −28% and a mean of +104%, and no rank
statistic handles that gracefully. Taking only the top 20 discards all of it.
**The score is far better at identifying a small, good cohort than at ordering
4,000 companies**, and the two questions have different answers.

### Where it fails is unchanged, and now unmistakable

| quarter | excess +3m | excess +12m | |
|---|---|---|---|
| 2020q1 | +0.5 | **−46.1** | COVID rebound |
| 2020q2 | −4.2 | **−23.4** | |
| 2020q3 | **−22.6** | **−29.3** | |

Against the best stretch:

| quarter | excess +12m |
|---|---|
| 2023q1 | **+48.4** |
| 2023q3 | **+45.9** |
| 2022q3 | **+40.2** |

A swing of roughly 90 points between the worst and best years. **The top-20
list is not a portfolio you can hold blindly through a regime change** — it
lost ~30-46 points to a simple universe median through the COVID rebound.

### Top 20 as at 2026-03 (latest, forward returns still accruing)

| # | ticker | company | score | +3m |
|---|---|---|---|---|
| 1 | MNST | Monster Beverage | 90.0 | +33% |
| 2 | INCY | Incyte | 89.1 | +20% |
| 3 | AUPH | Aurinia Pharmaceuticals | 86.9 | +15% |
| 4 | KLAC | KLA Corp | 86.6 | **+105%** |
| 5 | TR | Tootsie Roll | 85.6 | −7% |
| 6 | SEZL | Sezzle | 85.4 | **+171%** |
| 7 | EXEL | Exelixis | 85.0 | +27% |
| 10 | ANET | Arista Networks | 84.2 | +38% |
| 14 | POWL | Powell Industries | 83.5 | +59% |
| 16 | TDW | Tidewater | 83.4 | −20% |

Median +19.5% against a universe median of +8.8% — an excess of +10.7 points,
with 15 of 20 positive. One quarter, so read it as an illustration and not as
evidence.

---

## Why the top-20 list fails when it fails

600 individual picks with 12-month returns (`top20_tickers_12m.csv`): 367 beat
the universe, 233 did not. At quarter level, 23 of 30 won, with an average win
of +19.6 points and an average loss of −19.8 — **symmetric in size, lopsided in
frequency**.

### It is one factor, and it is the market

| following year | picks | win rate | median excess |
|---|---|---|---|
| universe **fell** | 220 | 67% | **+12.6** |
| universe +0-15% | 280 | 65% | **+17.4** |
| universe **rose >15%** | 100 | **38%** | **−13.7** |

Six of the seven losing quarters are 2018q1-q3, 2019q4 and 2020q1-q3 — the
run-up into 2018 and the COVID melt-up. **Every loss clusters in a strong
market.** The screen is structurally short the beaten-down cyclicals that lead
a rally, so it gives back in a melt-up roughly what it earns in calmer periods.

### What is NOT the pattern — four hypotheses that died

| dimension | result |
|---|---|
| rank within the top 20 | 57% / 67% / 62% / 59% for ranks 1-5 / 6-10 / 11-15 / 16-20 |
| flags on the pick | flagged 62%, unflagged 62% |
| health pillar score | 19-20 wins 59%, 16-19 wins 66% — no gradient |
| market cap | $1-10bn 61%, >$10bn 64% |

**Rank #1 is no better than rank #15.** Concentrating the list buys nothing,
which is the same "cannot pick winners" finding in yet another form — the score
identifies a good cohort but cannot order within it.

### The one company-level signal

| sector | picks | win rate | median excess |
|---|---|---|---|
| Consumer Discretionary | 47 | 74% | +16.0 |
| Materials | 43 | 65% | +23.5 |
| Information Technology | 142 | 65% | +14.7 |
| Industrials | 114 | 64% | +9.5 |
| **Health Care** | **113** | **48%** | **−3.0** |

Health Care is the weak spot and it is a large share of the picks — the
net-cash biotechs that max the health pillar. Half of them underperform. Worth
either a sector cap or extra scrutiny before acting on a Health Care pick.
