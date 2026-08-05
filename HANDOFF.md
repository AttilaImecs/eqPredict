# HANDOFF — Fortune 500 / S&P 500 / NASDAQ financial dataset

## ✅ FULL RUN COMPLETE — 2026-08-04

**`Fortune500_SP500_NASDAQ_Financials_5Y.xlsx` (17.5 MB) is built** from the
full 3,727-ticker universe. 3,444 tickers carry fundamentals, all 3,727 carry
prices, and 29 known full-year revenues reconcile to **0.0029%**.

Five issues were found at scale and fixed — see section 3c. Two of them
(IBM's restatement and the 16-week retail quarter) were producing silently
wrong numbers for major companies, not crashes.

### Pipeline changed during the full run

`fetch_prices.py` is NEW and replaces `fetch_financials.py` for prices and the
snapshot. Yahoo rate-limited the per-ticker approach dead at ~500 of 3,727
names. `yf.download()` takes a list, so the universe is ~25 requests instead of
3,727. Market cap and trailing P/E are now computed from SEC share counts and
SEC EPS rather than fetched, which removes the dependency entirely.

```bash
python build_universe.py                                # ~2s   optional
python fetch_sec_fundamentals.py --restart --workers 6  # ~8m   fundamentals + shares
python fetch_prices.py --restart --batch 150            # ~6m   monthly prices + snapshot
python fetch_financials.py --workers 4                  # opt.  yf quarters for the backfill
python backfill_quarters.py                             # ~5s
python build_excel.py                                   # ~1m
```

Both fetchers are **resumable** — re-run without `--restart` to continue. This
matters: the run was executed in 45-second slices and picked up cleanly each
time.

---

**Last updated:** 2026-08-04 (session 3)
**Status:** unblocked and working. `data.sec.gov` is allowlisted. The SEC XBRL
fundamentals fetcher is written, smoke-tested on 25 tickers, and validated
against an independent source. The whole chain has been proven end-to-end.
**Remaining work: run the full 3,727-ticker fetch.** No code is outstanding.

---

## 1. Network — resolved

`data.sec.gov` returns `200`. Nothing further is needed from Ben. All previously
verified domains still work.

---

## 2. What the next session must do

The pipeline is complete and tested. Run it:

```bash
pip install yfinance --break-system-packages

python build_universe.py                                # ~2s  (optional; universe.csv exists)
python fetch_financials.py --restart --workers 6        # ~15m prices, snapshot, yf quarters
python fetch_sec_fundamentals.py --restart --workers 5  # ~8m  fundamentals (6y window)
python backfill_quarters.py                             # ~5s  fill SEC's publishing lag
python build_excel.py                                   # ~1m
```

**Order matters.** `fetch_financials.py` writes prices, the snapshot and its own
shallow `data_quarterly_yf.csv`. `fetch_sec_fundamentals.py` writes the deep
`data_quarterly.csv`. `backfill_quarters.py` then merges the two. Running the
SEC step after the backfill would discard the filled rows.

Output: **`Fortune500_SP500_NASDAQ_Financials_5Y.xlsx`**

Both fetchers are resumable — kill and re-run without `--restart` to continue.
`fetch_financials.py` checkpoints to `_done.txt`, the SEC fetcher to
`_sec_done.txt`.

---

## 3c. Issues found at 1,000 and 3,727 scale — all fixed

Each was invisible on smaller samples.

**1. Restated fiscal years corrupted every derived Q4.** IBM reports *two*
values for 2020 — 73.620bn as filed and 55.179bn restated after the Kyndryl
separation. Taking the latest-filed annual and subtracting the three
*original* quarters gave a Q4 revenue of **1.9bn against an actual 20.4bn**.
`_collect()` now returns every distinct annual value and `derive_q4()` picks the
one whose implied Q4 sits closest to the quarters actually on hand.

**2. A 100-day quarter ceiling deleted one quarter a year from every 4-5-4
retailer.** Albertsons files 83-day and 111-day periods; the 16-week quarter
fell outside the window and vanished. Albertsons and Advance Auto Parts came
back with **13 quarters instead of 24**. `QUARTER_DAYS` is now (80, 125).

**3. Filer scale errors produced a $12 trillion company.** MacKenzie Realty
tags lease income of 4,594,058,000,000 where the real figure is 4,594,058 — out
by exactly a million — which made it the largest company in the dataset.
`drop_impossible_revenue()` discards any quarter exceeding its own fiscal year
total, using the annual figure as a free upper bound.

**4. Dual-tagged periods inflated quarter counts.** Waters tags the same quarter
as ending both 2020-09-26 and 2020-09-30 with identical revenue, reaching **38
quarters in a 24-quarter window**. `collapse_near_duplicates()` merges period
ends within 12 days, keeping the fullest row and filling its gaps from the
duplicate.

**5. Lessors that never tag `Revenues`.** Camden Property Trust books rent under
`OperatingLeaseLeaseIncome` (395.7M) and only fees under contract revenue
(2.6M), so it read as a **$2.5M-a-quarter** company. Revenue is now composed as
lease + contract for such filers; American Tower validates the arithmetic, its
lease 2,521 + contract 216 equalling its tagged `Revenues` of 2,737 exactly.

Two smaller ones: a `threading.Lock` deadlock (the results loop held it while
calling `flush_shares()`, which wanted it too — now an `RLock`), and share
counts being flushed only at process exit, so a time-boxed run lost them.

---

## 3b. 100-ticker smoke test (session 3)

Sample was stratified, not the first 100 alphabetically: the original 25, the 8
suspicious new-CIK S&P names, three tickers from each of 12 sectors, and random
Nasdaq fill. 60 Nasdaq, 66 S&P 500, 36 Fortune 500, 10 with post-2024 CIKs.

| Check | Result |
|---|---|
| Fundamentals returned | **97 / 100** (91 before 20-F support) |
| Quarters per ticker | median 23, max 24 |
| revenue populated | 94.8% |
| net_income populated | 100% |
| Negative / impossible revenue | 0 |
| Known full-year revenues (19) | **0.0000% deviation** |
| Cross-check vs yfinance (382 values, 40 tickers) | 97.1% within 1%, 97.9% within 10% |

### Foreign private issuers — 20-F support (added)

Foreign private issuers file **20-F / 40-F annually and 6-K for interim**, never
10-K or 10-Q. They were returning nothing at all — about 7% of the universe.

`ANNUAL_FORMS` now accepts 20-F/40-F, and `annual_only_rows()` emits a full-year
row for **any fiscal year with no quarterly coverage**, tagged
`period_type = "FY"` (quarters are `"Q"`). Coverage went **91 → 97 of 100**.

Scoped per fiscal year, not per company, so a filer reporting quarterly recently
and annually earlier gets the right treatment for each. Domestic output is
byte-identical — an FY row only appears where no quarter exists.

`build_annual()` prefers as-filed FY rows over summed quarters. There is no
overlap by construction, so it is purely additive. `build_wide()` must exclude
FY rows from the quarterly pivot: their `FY2024` label collides with the annual
column of the same name and the join fails outright.

Their true quarterly columns stay empty because that data does not exist — the
annual columns are the point. Note some report in **non-USD** (U Power in CNY);
the `currency` column carries this and figures are **not** FX-converted.

**3 still empty, all correct:** FedEx Freight and Honeywell Aerospace are
spin-offs that have not reported under any form, and OIO has no tagged financial
data in the window.

**Three tickers still differ from yfinance by >10%,** all understood:
`XXII` (excise tagged without a matching quarterly period, so it cannot be
netted — a $4M micro-cap), `TBCH` (differs in both directions by quarter, which
points to yfinance period mislabelling rather than a fetch error; SEC is
as-filed), and `Q` (`NetIncomeLoss` includes noncontrolling interests where
yfinance reports income attributable to the parent).

**A bug only a larger sample could expose:** the outer join in `build_wide()`
silently dropped the index name once some tickers had prices but no
fundamentals, crashing the build. Invisible at 25 tickers where every ticker had
both.

---

## 3. Verification already done (25-ticker smoke test)

Sample: AAPL MSFT JPM XOM KO NVDA AMZN GOOGL META TSLA BRK-B UNH V WMT PG BAC
COST HD CVX MRK PFE T PLD AMT GS — deliberately mixed to include banks, REITs,
insurers, offset fiscal years and a 4-5-4 retail calendar.

| Check | Result |
|---|---|
| Quarters per ticker | min 19, median 20, max 20 |
| Duplicate `(ticker, period_end)` | 0 |
| Duplicate `(ticker, fiscal_quarter)` | 0 |
| `revenue` populated | 100.0% |
| `net_income` populated | 100.0% |
| `eps_diluted` populated | 92.2% |
| `ebit` populated | 68.2% (banks/insurers have no EBIT line — correct) |
| `ebitda` populated | 55.7% |
| `gross_profit` populated | 24.4% (most non-manufacturers don't report it) |
| Impossible margins (`|net margin|` > 200%) | 0 |
| End-to-end `build_excel.py` | ✅ 25/25 companies "fully complete" |

**Independent cross-check against yfinance** — 140 overlapping values across 15
tickers: **92.1% within 1%, 98.6% within 5%.**

**The Q4 derivation was validated, not assumed.** 28 derived Q4 values were
compared against Yahoo's independently reported Q4 figures; max difference
2.83%, most exactly 0.00%. Spot checks against known reported results also
tie out (AMZN Q4 2022 revenue $149.204B / net income $278M; AAPL FQ4 2024 net
income $14.736B reflecting the EU State Aid charge).

The only two differences above 5% were REIT net income (AMT, PLD), where
`NetIncomeLoss` includes noncontrolling interests and Yahoo reports income
attributable to common. That is a definitional difference, not an error.

---

## 4. `fetch_sec_fundamentals.py` — what it does and why

Pulls quarterly fundamentals from `https://data.sec.gov/api/xbrl/companyfacts/`,
rate-limited to 8 req/s by a shared token bucket (SEC's hard cap is 10/s).
Observed throughput ~11 tickers/s on the smoke run, so the full universe is
roughly 6–10 minutes.

Six non-obvious problems surfaced during the smoke test. Each fix is commented
in the source; **do not "simplify" these away**:

1. **Q4 does not exist in any filing.** Companies file no 10-Q for their fourth
   quarter — it only appears folded into the annual 10-K. A naive ~90-day filter
   yields 15 quarters per company, not 20. Q4 is derived as `FY − (Q1+Q2+Q3)`
   whenever three contiguous quarters can be matched inside an annual period.
   Derived rows are flagged in the **`q4_derived`** column (~24% of rows).

2. **Concept candidates must be merged, not first-match-wins.** Filers switch
   XBRL tags mid-window. XOM used `Revenues` for 28 quarters but
   `RevenueFromContractWithCustomer...` for only 15 — taking the first
   non-empty candidate silently truncated the series at 2023.

3. **`Revenues` must be tried before `RevenueFromContractWithCustomer...`.**
   `Revenues` is the GAAP total; contract revenue is only an ASC 606 component.
   For lessors the gap is enormous — American Tower reports $0.2B of contract
   revenue against $2.7B of total revenue, the rest being lease income. Getting
   this backwards produced net margins over 500%.

4. **Banks have no revenue line.** Their top line is `RevenuesNetOfInterestExpense`.
   Without it, every financial in the S&P 500 returned zero revenue quarters —
   GS and JPM initially came back with 20 quarters of net income and no revenue.

5. **Period keys must be the end date alone.** AT&T tags the same economic
   quarter as starting `2021-09-30` for one concept and `2021-10-01` for
   another; keying on `(start, end)` split it into two half-empty rows and
   inflated AT&T to 23 quarters.

6. **Only a 10-K delimits a fiscal year.** Amazon tags rolling twelve-month
   windows inside its 10-Qs. Treating those as fiscal years labelled every
   Amazon quarter "Q4" and — more dangerously — would have allowed Q4 to be
   derived from a period that is not a fiscal year at all.

Also handled:

- **EBITDA** = `OperatingIncomeLoss + DepreciationDepletionAndAmortization`.
  D&A is a cash-flow-statement item tagged year-to-date, so a bare quarterly
  filter finds only Q1. Consecutive YTD figures are differenced to recover the
  rest, which lifted EBITDA coverage from 42% to 56%.
- **`fiscal_quarter`** is labelled `FY2025-Q3` style from the company's own
  fiscal calendar, not the calendar quarter. Costco's quarters ending
  2026-02-15 and 2026-05-10 both fall in calendar Q1. Where the current fiscal
  year has no 10-K yet, the prior year's calendar is projected forward.
- **Costco-style 4-5-4 calendars** run a 16–17 week Q4, so the derived-Q4
  duration window is wider (`Q4_DAYS`) than the regular quarter window.
- **Restatements** win: facts are deduplicated on period keeping the latest
  `filed` date.

### Revenue selection — no single candidate list works

`extract_revenue()` is deliberately not a priority list. Four filer types
contradict each other and each was found by cross-checking a real sample:

| Filer | Problem | Rule |
|---|---|---|
| **Banks** (GABC, JPM, GS, BAC) | `Revenues` is GROSS of interest expense. German American tags 123.2M; every provider shows 89.9M = net interest income 73.2 + noninterest 16.7. | Use `RevenuesNetOfInterestExpense`; if untagged, compose NII + noninterest income. |
| **Lessors / REITs** (AMT) | `Revenues` 2.7bn is the total; contract revenue 0.2bn is a component. | — |
| **Asset managers** (BLK) | Reversed: `Revenues` 12.8bn is a component of 20.4bn contract revenue. | Take the **larger** of `Revenues` and `...ExcludingAssessedTax` — a component is by definition smaller than the total, which resolves AMT and BLK together. |
| **Excise filers** (TAP, XXII) | Molson Coors files gross sales 3,740.0M in the tag *named* `...ExcludingAssessedTax` and net sales 3,200.8M in `...IncludingAssessedTax`. **The tag names are backwards.** | Where excise is tagged AND both variants exist: net = max(variants) − excise. Naming is never trusted. |

Exxon also tags excise but has only one variant, so it correctly keeps its
`Revenues` total — hence the both-variants condition.

**Q4 is derived within a single concept, before candidates are merged.** Mixing
an annual figure from one tag with quarters from another produced a *negative*
Q4 revenue for BlackRock. A guard now drops any negative revenue outright rather
than letting it poison every margin computed from it.

### SEC's XBRL API lags EDGAR — `backfill_quarters.py`

A 10-Q can be public on EDGAR days before it appears in the `companyfacts` API.
**The delay is not a uniform lag.** On 2026-08-04, Goldman Sachs' 3 August filing
had been published while Meta's 30 July filing had not.

On the 100-ticker sample, 54 tickers had no quarter after 2026-03-31 — but only
**9 had actually filed one**. The rest simply had not filed yet, which is the
correct and expected state. Checking EDGAR's `submissions` API distinguishes the
two; do not assume a missing quarter means a bug.

`backfill_quarters.py` fills those gaps from yfinance, which reads the earnings
release rather than the XBRL pipeline. Deliberately narrow:

- only quarters **newer** than the ticker's latest SEC quarter,
- only within `--max-age-days` (default 200, about two quarters) — this is a
  filing-lag fill, not a licence to rebuild old history with a second source,
- never overwrites an SEC row,
- every filled row tagged `source = "yfinance (SEC pending)"`.

**Date tolerance is essential.** yfinance rounds period ends to month end:
Apple's quarter ending 2026-06-27 appears as 2026-06-30, and Costco's 2026-05-10
as 2026-05-31 with an identical revenue figure. Matching exactly would have
added both as duplicate quarters. The 35-day window catches this and still
cannot reach the neighbouring quarter 90 days away.

Result on the sample: quarters ending Apr–Jun 2026 went from **38 to 46 of 92**,
recovering Meta, Visa, PayPal, Prologis, NXP, Omnicom, PPG, Lennox and Coca-Cola.

**Re-running later is worth it.** The straggler filings appear in the API within
days, and both fetchers are resumable.

### 52/53-week fiscal years ending in early January

L3Harris' fiscal 2020 ends **2021-01-01**. Naming a fiscal year after its ending
calendar year put it in FY2021, colliding with the real FY2021 and producing two
sets of quarters under one label. `fy_year()` treats a year ending in the first
seven days of January as belonging to the prior year. Retailers ending 31 January
(Walmart's FY2026) are untouched — they genuinely are named for the ending year.

### Successor-CIK trap

`XOM` maps in `company_tickers.json` to CIK 2115436 — a post-reorg holding
company with no filing history — and returned **2 quarters instead of 19**. It
is corrected via the `CIK_OVERRIDES` table in the script.

Predecessors are now **merged, not substituted** — neither CIK alone is complete.
BlackRock's successor holds 10 quarters and its predecessor 12; together 23.
`CIK_PREDECESSORS` currently covers XOM, BLK and PSKY. The successor wins on any
overlapping period.

The fetcher writes **`sec_short_history.csv`** listing every ticker with under 18
quarters and prints the S&P 500 / Fortune 500 members among them, so new cases
surface without hunting. On the 100-ticker sample the five flagged (FDXF, HONA,
Q, SNDK, SW) are all genuine new entities — spin-offs and mergers with no
predecessor as a standalone filer — so no override is appropriate.

**This is not fully solved.** 477 universe tickers have a CIK above 2,000,000.
Most are genuine recent IPOs and correctly have short histories, but 8 are S&P
500 members where a short history is suspicious:

`BLK, FDXF, HONA, PSKY, Q, SNDK, SW, XOM`

Only XOM is currently overridden. Some of the others are genuinely new entities
(Qnity, Smurfit Westrock) where a short history is correct. **After the full
run, check the `Coverage` sheet for large caps with fewer than 18 quarters and
add any real predecessors to `CIK_OVERRIDES`.**

---

## 4b. Workbook layout — one wide `Data` sheet

`build_excel.py` no longer emits separate `Quarterly` and `Monthly Prices`
sheets. Both are pivoted into a single **`Data`** sheet: **one row per ticker**,
with every metric-period pair as its own column, named `<metric>_<period>`.

```
ticker | company | currency | revenue_FY2021-Q3 ... revenue_FY2027-Q1
                            | gross_profit_FY2021-Q3 ... | ebit_... | ebitda_...
                            | open_2021-09 ... | close_2021-09 ... close_2026-08
```

Columns are grouped by metric and run chronologically inside each group, so any
one line item is a contiguous block you can slice as a single range.

**Periods are LABELS, not literal dates.** Fiscal quarter ends differ by
company — Apple's Q3 ends 2026-06-27, Microsoft's 2026-06-30 — so literal dates
as headers would give thousands of near-empty columns, one set per company.
`FY2026-Q3` puts both companies in the same column. Months use `YYYY-MM`.

**Label order is parsed from the label, never from the underlying dates.**
A date-derived sort puts Costco's `FY2021-Q4` (ends 2021-08-29) *before* a
calendar filer's `FY2021-Q3` (ends 2021-09-30). `period_sort_key()` handles this.

### Units

**All currency totals are in MILLIONS of the filing currency** — revenue, gross
profit, EBIT, EBITDA, net income, market cap. The cell value *is* the number
shown, so a formula referencing it gets millions too.

This replaced the old `'#,##0,,"M"'` number format, which kept full units in the
cell (`391035000000`) and only *displayed* them as `391,035M`. Anything built on
top of those cells silently got the unscaled figure. `to_millions()` now rescales
the data itself and the format is a plain `#,##0.##`.

Values keep two decimals rather than whole millions — a micro-cap with $400k of
revenue would otherwise round to `0` and read as a data gap.

**Not scaled:** diluted EPS, share prices, margin percentages, volume. A `units`
column on the `Data` sheet states this per row so nobody has to find the README.

### Full-year and projected columns

The sheet leads with annual columns — the seasonality-free view — before the
quarterly detail:

| Column | Meaning |
|---|---|
| `revenue_FY2024` | fiscal year total, the sum of its four quarters |
| `revenue_FY2026E` | **estimate** for the year still in progress (trailing `E`) |
| `fy_projected` | which fiscal year the `E` columns cover |
| `proj_quarters_actual` | how many quarters of real data the projection rests on |

A full-year column is **blank unless all four quarters are present.** A partial
sum understates the year, which is worse than an empty cell.

The projection scales year-to-date actuals by the share those same quarters made
up of the prior full year:

```
FY26E = (Q1+Q2 actual) / [ (Q1+Q2 prior year) / (full prior year) ]
```

This corrects for seasonality; naive annualising (`YTD × 4/n`) does not. Costco
projects **302bn** this way against **277bn** annualised naively, and Nvidia
**400bn** against **327bn** — a 9% and 22% difference respectively. No projection
is emitted where the prior year was loss-making or its quarterly mix falls
outside 5–95%, since the scaling stops being meaningful there.

**`proj_quarters_actual` matters.** Many companies have only one quarter of the
new fiscal year reported, so their projection is a seasonally-adjusted
extrapolation from a single data point. Treat those very differently from a
three-quarter projection.

Note for offset fiscal years: Walmart's `FY2027` ends January 2027, so it is
mostly calendar 2026. Three retailers with January/February year ends (HD, NVDA,
WMT) have no `FY2021` column value, because their fiscal 2021 began in early
2020, before even the extended window.

**Validation:** 554 complete-year totals reconcile exactly to their quarters, all
margin columns reconcile to numerator ÷ revenue, and 15 full-year revenues spot-
checked against independently reported figures (Apple FY2022–24, Microsoft
FY2024, Alphabet, Amazon, Tesla, Coca-Cola, Walmart, Costco, Meta, Nvidia, Exxon,
P&G, UnitedHealth) match to **0.0000%**.

### Monthly columns

Monthly metrics are `close`, `adj_close`, `volume`, `market_cap_est` and
`pe_trailing_est`. **Monthly `open`/`high`/`low` are deliberately excluded** —
three more columns per month for little analytical value at this frequency. They
are still in `data_monthly.csv` if anyone wants them back; re-add them to
`M_MEASURES` in `build_excel.py`.

Verified on the 25-ticker sample: **7,327 values round-tripped against the
source CSVs — 0 mismatches.** Sheet is 25 rows × 556 columns.

Full-universe projection: worst case ~875 columns against Excel's 16,384
limit, so there is comfortable headroom, and `build_excel.py` hard-exits if that
is ever exceeded. At ~3.9M cells the styling drops to a "light" path (column
widths only, no per-cell number formats) — writing 3.9M number formats through
openpyxl costs minutes and a lot of memory for no analytical value.

The row-per-observation form is still in `data_quarterly.csv` and
`data_monthly.csv` if you want it for a database load.

---

## 5. Output schema

`data_quarterly.csv` keeps every column `build_excel.py` expects and adds three:

`ticker, company, period_end, fiscal_quarter, currency, revenue, gross_profit,
ebit, ebitda, net_income, eps_diluted, gross_margin_pct, ebit_margin_pct,
ebitda_margin_pct, net_margin_pct` **+ `period_start`, `q4_derived`, `source`**

The additions are non-breaking — they flow into the Quarterly sheet as extra
columns. `q4_derived` is worth keeping visible so a reader can tell a filed
figure from a derived one.

---

## 6. Corrections to earlier handoffs

| Old claim | Reality |
|---|---|
| "AAPL should have ~20 quarterly rows" | ✅ **Now true via SEC XBRL** (20 quarters, 2021-09 → 2026-06). It was false via yfinance, which caps at 5–7 quarters. |
| "`dividend_yield` may be off by 100×" | ✅ No fix needed. KO 2.42% reported vs 2.46% implied. Already in percent — don't "fix" it. |
| "~4,000–5,000 tickers, 45–90 min" | 3,727 tickers; ~15 min for prices, ~8 min for fundamentals. |
| "`ebit` blank for banks/insurers/REITs is correct" | ✅ Confirmed — 68% populated overall, and the blanks are concentrated exactly where expected. |
| "~60 monthly rows per ticker" | ✅ Confirmed — median exactly 60, 0% null closes. |
| "build_excel.py never verified against real data" | ✅ **Now verified** — full chain run on 25 tickers, output opens and reads back cleanly. See `SAMPLE_25tickers.xlsx`. |

---

## 7. Known minor issues (not blocking)

- **`gross_profit` is only ~24% populated.** Most non-manufacturers simply do
  not report a gross profit line. Not recoverable from XBRL.
- **A few mega-caps miss the `in_fortune500` flag** from name-normalization
  mismatches (XOM, COST). Cosmetic — one boolean column, no financial data
  affected.
- **127 Fortune 500 names don't map to tickers** — correct; they're private
  (Cargill, State Farm, Publix) or mutuals. See `fortune500_unmatched.csv`.
- **Fortune 500 list is the 2023 edition** with a Wikipedia top-100 freshness
  overlay. No free current-year full list exists.
- **REIT net income includes noncontrolling interests** (see section 3).

---

## 8. Caveats Ben has already accepted

- **Forward P/E is a current snapshot only.** Historical forward P/E needs paid
  analyst-estimate data (Bloomberg/FactSet/Capital IQ).
- **`market_cap_est`** = monthly close × *current* shares outstanding, so older
  months drift where there have been buybacks or issuance. The `Snapshot`
  sheet's `market_cap` is the accurate current figure.
- **Survivorship bias** — index membership is as of collection date.
- **Currency** is each company's filing currency, not FX-converted.
- Free data has gaps. The `Coverage` sheet makes them visible per company
  rather than hiding them.

---

## 9. File inventory

| File | State |
|---|---|
| `build_universe.py` | ✅ verified — 3,727 tickers, CIKs for 3,717 |
| `fetch_sec_fundamentals.py` | ✅ **new this session** — SEC XBRL quarterly fundamentals, smoke-tested and cross-validated |
| `fetch_financials.py` | ✅ prices, snapshot, and `data_quarterly_yf.csv` used only by the backfill |
| `backfill_quarters.py` | ✅ **new** — fills the most recent quarter where SEC's API lags EDGAR |
| `build_excel.py` | ✅ **verified against real data, then reworked** to emit the wide one-row-per-ticker `Data` sheet (section 4b) |
| `universe.csv` | current build artifact (3,727 rows) |
| `smoke_quarterly.csv` | 25-ticker smoke output, 488 rows — inspect before committing to the full run |
| `SAMPLE_25tickers.xlsx` | end-to-end proof: the real workbook, built from 25 tickers |
| `fortune500_unmatched.csv` | Fortune 500 names with no US ticker |
| `fetch_prices.py` | ✅ **new** — batched Yahoo prices + snapshot; replaces fetch_financials for prices |
| `pipeline_architecture.svg` | run order and data flow, including step 2's internals |
| `Fortune500_SP500_NASDAQ_Financials_5Y.xlsx` | ✅ **the deliverable** — 3,727 × 974 |
| `HANDOFF.md` | this file |
