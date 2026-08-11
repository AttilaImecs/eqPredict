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
