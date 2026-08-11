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
