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
