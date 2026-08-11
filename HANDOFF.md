# HANDOFF — Fortune 500 / S&P 500 / NASDAQ financial dataset

## 🔁 CLEAN REBUILD — 2026-08-06

Whole pipeline re-run from scratch (universe included) to verify the codebase
end to end. Output: **`Fortune500_SP500_NASDAQ_Financials_5Y_v2.xlsx`**,
3,732 × 974, **30 known full-year revenues at 0.0029%**. The original
`..._5Y.xlsx` was left untouched and re-verified byte-identical by SHA256.

**Two blocking defects were found and fixed during the rebuild:**

1. **`build_universe.py` sent a User-Agent with no contact email.** SEC returns
   **403** for generic agents, so the Fortune 500 step and the CIK map both
   failed — yet the script printed warnings, wrote a universe with **0 Fortune
   500 flags and 0 CIKs**, and exited 0. Every downstream fetch is keyed on the
   CIK, so an automated run would have produced an empty dataset while
   reporting success. The UA now matches the rest of the pipeline, the CIK
   failure is fatal, and a threshold gate refuses to write a universe that is
   obviously short (S&P < 450, Nasdaq < 2,500, Fortune < 300, CIKs < 3,000).

2. **`Canada/build_universe_ca.py` had the same bad User-Agent**, and called
   `.json()` without checking status — so the 403 surfaced as a baffling
   `JSONDecodeError: line 1 column 1`. It now checks the status, retries with
   backoff, and fails with a clear message.

**Expected drift vs the previous workbook** (all verified legitimate, none are
bugs): 7 tickers added and 2 removed as listings changed; a handful of
`market_cap_est` cells moved because Yahoo retroactively adjusted prices for
reverse splits (LBGJ 1:200, WETO 1:100, KWM 1:30 — close × shares stays
self-consistent within each run); and current-month volume differs because
August is still accumulating.

---

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
| "build_excel.py never verified against real data" | ✅ **Now verified** — full chain run on 25 tickers, output opens and reads back cleanly. (`SAMPLE_25tickers.xlsx` removed 2026-08-10; see section 9.) |

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
| `fortune500_unmatched.csv` | Fortune 500 names with no US ticker |
| `score_companies.py` | ✅ **new** — 0-100 rules-based company score, 5 pillars, 3-year window (section 10) |
| `company_scores.csv` | score output, one row per rated ticker with every input that fed it |
| `fetch_prices.py` | ✅ **new** — batched Yahoo prices + snapshot; replaces fetch_financials for prices |
| `pipeline_architecture.svg` | run order and data flow, including step 2's internals |
| `Fortune500_SP500_NASDAQ_Financials_5Y_v2.xlsx` | ✅ **the deliverable** — 3,732 × 974, from the 2026-08-06 clean rebuild |

**Removed 2026-08-10** (recoverable from git history, all superseded): the pre-rebuild
`Fortune500_SP500_NASDAQ_Financials_5Y.xlsx`, `smoke_quarterly.csv`,
`SAMPLE_25tickers.xlsx`, `SAMPLE_100tickers.xlsx`. Note `build_excel.py`'s `OUT`
still writes the un-suffixed name, so the next run recreates it rather than a `v3`.
| `HANDOFF.md` | this file |

---

## 10. `score_companies.py` — the 0-100 company score

`python3 score_companies.py --top 25 --min-confidence 0.7`

Pure stdlib, no pandas — the env that ran the fetchers is gone and this needs
to run anywhere. Reads the four checkpoint CSVs, writes `company_scores.csv`
with the score, the five pillar sub-scores, every flag, and every raw metric
that fed a rule, so any score can be taken apart.

Five pillars of 20: **growth, profitability, quality, valuation,
momentum/risk**. Thresholds are calibrated against this universe's actual
percentiles, not invented — each band carries the percentile it sits at in a
comment. A pillar with no data is dropped and the total renormalised, with
`confidence` reporting how much of the rubric ran. Growth is TTM-vs-TTM three
years apart (16 quarters); 2,578 of 2,784 rated names get that full window,
and `growth_basis` marks the 100 that fall back to two years.

**Two structural blind spots, both unfixable with the data in this repo:** no
balance sheet (so leverage, coverage and ROE are invisible — a company can
score 85 and be one covenant from trouble) and no cash flow (so accrual
quality cannot be tested). Stated at the top of the script too.

### Three bugs found while validating it — all in the share-count trend

The dilution/buyback signal comes from `net_income / eps_diluted`, since we
hold only a single current share snapshot. Each of these produced confidently
wrong output before it was caught:

1. **Stock splits.** As-filed EPS is not restated backwards, so Nvidia's 10:1
   shows as a ×9.93 jump and read as **+883% dilution**; Walmart, Alphabet and
   Tesla the same. Splits are now chain-linked out by recognising ratios within
   6% of a round integer — which deliberately leaves UMB Financial's ×1.6
   Heartland acquisition intact as the real dilution it is.
2. **Derived Q4 rows.** `eps_diluted` is in `ADDITIVE`, but EPS is only
   additive if the share count held still — the exact thing being measured.
   Nvidia's FY2024 Q4 implied **−49bn shares**. Derived rows are now excluded.
3. **A fixed EPS floor.** `abs(eps) < 0.02` is meaningless for a filer earning
   ~$100/share: a near-breakeven quarter made **Booking read +1928%** and
   Allstate **+731%**. The floor is now relative to the company's own median
   EPS, and Booking correctly shows −14.3% — it has been retiring stock.

### Known bias: the score tilts towards Financials

Median score by sector runs **72.8 (Financials) to 60.8 (Consumer Staples)**,
and Financials are **3.0× over-represented in the top 200** while Energy and
Materials are absent. It is mechanical: margin bands are absolute and median
net margin is 21.2% in Financials vs 6.6% in Consumer Staples, and banks
structurally trade on low P/Es.

`sector_rank_pct` (percentile within sector) is the column to compare on — but
`sector` is only populated for the ~500 S&P names from the Wikipedia scrape.
**The real fix is an SIC code per filer**, which SEC assigns to everyone and
`build_universe.py` could pull in one pass. Until then, read the absolute score
as "rank this whole list" and the sector rank as "rank this against peers".

---

## 11. Industry classification, free cash flow, peer-relative scoring

### `fetch_sic.py` — a sector for the WHOLE universe

`universe.csv` only carries a sector for the ~500 S&P names from the Wikipedia
scrape. SEC assigns every filer an SIC code and serves it from the
`submissions` API, so this fetches one per CIK into **`data_sic.csv`**.

The SIC-to-sector table is ordered **specific-first and first match wins**,
which is load-bearing: 2833-2836 (biologics) must be tested before 2800-2899
(chemicals) or every biotech lands in Materials, and 6798 (REITs) before
6700-6799 (holding offices) or every REIT lands in Financials.

**The sector is re-derived at SCORING time from the raw `sic` code**, not read
from the `sector_sic` column this script writes. The code is the durable fact;
the mapping is a judgement call that will keep being tuned. Deriving late means
a mapping fix is free, where trusting the stored column would mean re-fetching
3,717 filings every time a range moved. Tuning the ranges against the first
2,478 rows cut unclassified from 4.8% to 2.2%.

SIC and GICS agree on **78%** of the names where both exist. They are different
taxonomies and full agreement is not the goal — a usable peer group is. Note
SIC 6770 is "Blank Checks", i.e. SPACs, which are not operating companies.

### Free cash flow — `ocf`, `capex`, `fcf`, `fcf_margin_pct`

`fcf = ocf - capex`, emitted **only when both legs are present**. An untagged
capex is not zero, and treating it as zero would publish operating cash flow as
free cash flow — exactly wrong for the capital-intensive filers where the
distinction matters most.

**These are cash-flow-statement items, so XBRL tags them YEAR-TO-DATE.** They
go through the same `ytd_to_quarterly()` path as `dep_amort` (see
`CASHFLOW_YTD`). A bare ~90-day filter would find only Q1 and silently drop
three quarters in four, putting one quarter of cash flow against a full year of
revenue. `capex` uses `merge="max"` because its candidate tags overlap as
total-vs-component rather than as alternatives.

Validated against known figures: Apple's capex lands at ~$2-3B a quarter
(~$11B/yr), and Microsoft's jump to $30B+ a quarter matches its AI datacenter
build. Coverage on the smoke sample was OCF 99%, capex 87%.

### Two bugs this surfaced

1. **`fetch_sec_fundamentals.py` still had the bad User-Agent.** It carried
   `StockPipelineDataCollector/1.0` — the exact string section 3 records as
   403-rejected. The 2026-08-06 rebuild fixed `build_universe.py` and missed
   this file, leaving the main fundamentals fetcher one SEC policy tightening
   away from silently returning nothing. Now aligned with the rest.

2. **Never run two SEC fetchers at once.** `fetch_sic.py` and
   `fetch_sec_fundamentals.py` both rate-limit themselves to 8 req/s, which is
   under SEC's 10/s cap individually and **16/s together**. Running them
   concurrently earned a sustained `HTTP 429` and both stalled with empty logs
   for minutes — the failure is silent, because each script's backoff hides it.
   Run them **sequentially**. Both are resumable, so a kill costs nothing.

### Peer-relative profitability

Profitability margins are now scored as a **percentile within sector** rather
than against absolute bands, which is what produced the Financials tilt in
section 10: a grocer at its industry median scored mid-band while a bank at its
industry median maxed the rule.

13 of the 20 points are peer-relative; **7 stay absolute on purpose**. In a
sector where everybody loses money the least-bad loss-maker still ranks in the
90th percentile, and "does this company actually earn anything" is a question
no percentile can answer.

A sector needs `MIN_PEERS` (20) members before its own distribution is trusted;
below that the company is ranked against the whole universe. Ranking against
four peers yields percentiles of 0/25/50/75/100 and nothing between — noise
dressed as precision.

**Valuation is still absolute**, and its measured sector spread (8.8 points)
was LARGER than profitability's (5.5). Banks structurally trade on low P/Es.
Making V1/V2 peer-relative is the obvious next step.

### Measured effect of the peer-relative change

Like-for-like on the 480 GICS-labelled names (the same basis the 12.0 baseline
was measured on — the SIC rollout changed the sample, so the universe-wide
number is not comparable):

| | absolute bands | peer-relative |
|---|---|---|
| Financials lift in top 200 | 3.04x | **1.48x** |
| Spread between sector medians | 12.0 pts | **9.2 pts** |

Health Care's low median across the **full** universe (48.1 vs Financials 67.0)
is not residual bias: **29% of Health Care names carry `chronic_losses` against
2% of Financials.** That is unprofitable micro-cap biotech being scored
correctly, not a rubric failure.

### A third data defect: P/E was wrong for 381 tickers

Neither upstream P/E source can be trusted alone:

* **`pe_trailing` = price / eps_trailing.** Yahoo's prices are split-adjusted;
  SEC's as-filed EPS is not. Booking reads **1.16** where the truth is ~20.7.
* **market cap / TTM net income** needs no EPS, so splits cannot touch it — but
  `shares_outstanding` is **missing for ~1,000 tickers** (GOOGL, META, NVDA
  among them) and wrong for others: Mastercard is tagged 122.5M shares against
  an actual ~910M, implying a $70bn company rather than ~$520bn.

The scorer now computes **both and uses them only where they agree** within
`PE_TOLERANCE`. Disagreement means one is broken and we cannot tell which, so
the P/E rules are dropped and the row is flagged `pe_unreliable` (49 tickers)
rather than scored on a figure that may be 20x out. Where market cap is missing
but the reported P/E is usable, market cap is backed out as `pe x earnings`,
which recovers the mega-caps. Missing-valuation rows fell from 18% to 10%.

**The real fix is upstream in `fetch_prices.py`** — reconcile
`shares_outstanding` against the implied diluted count, or take Yahoo's own
market cap.

### Tunable configuration — `--config`

Every threshold, pillar weight, cap and band lives in `DEFAULTS` and nowhere
else. `--config my.json` deep-merges over it, so a file containing only
`{"pillar_weights": {"valuation": 30}}` changes just that; `--dump-config`
prints what is active. Weights need not sum to 100 — the total is renormalised.

This matters as macro conditions move. A P/E of 20 is expensive at 8% policy
rates and ordinary at 2%. Two defences: the peer-relative rules re-baseline
themselves every run (a market-wide de-rating shifts everyone's multiple and
nobody's score), and everything still absolute is editable in one place.

### `--as-of YYYY-MM` — scoring a past date

Discards every quarter and price after the given month. Valuation then comes
from `data_monthly.csv` rather than `data_snapshot.csv`, which is a CURRENT
snapshot and would leak the future into a historical score.

**Caveat:** `market_cap_est` is monthly close x *current* shares outstanding
(section 8), so historical market caps are distorted by any buyback or issuance
since, and P/S inherits that. `pe_trailing_est` is genuinely historical.
`valuation_basis` records which basis a row used.

Three windows scored, 1,872 companies common to all three:

| as-of | rated | median score | 3y growth basis |
|---|---|---|---|
| 2024-12 | 1,942 | 54.0 | 98% |
| 2025-12 | 2,018 | 55.2 | 98% |
| 2026-06 | 2,012 | 57.0 | 98% |

Median absolute score change across the ~18 months is **7.5 points** (p90 20.9)
— stable enough to be meaningful, responsive enough to be useful.

---

## 12. The share-basis fix — `shares_reconcile.py`

**The upstream cause of the P/E defect in section 11.** Three inputs sat on
three different share bases and the arithmetic combining them was wrong:

| input | basis |
|---|---|
| Yahoo `close` | split-adjusted to today |
| SEC `eps_diluted` | **as filed, never restated backwards** |
| SEC `shares_outstanding` | current dei tag, when it is right |

`fetch_prices.py` computed `pe = close / eps_trailing` (mixing the first two)
and `market_cap = close x shares` (trusting the third).

### The fix

The most recent quarters' as-filed EPS is already on today's basis, so
`net_income / eps_diluted` yields a current-basis share count derived
**independently of the dei tag** — and the two cross-check each other. TTM EPS
is then `TTM net income / shares` rather than a sum of four as-filed EPS
figures, because summing across a split boundary adds two different bases.

Everything is consistent by construction:
`market_cap = price x shares`, `eps = ni_ttm / shares`, `pe = market_cap / ni_ttm`.

| | before | after |
|---|---|---|
| `eps x shares` disagreeing with net income by >50% | **474 of 2,163 (21.9%)** | **0 of 2,815 (0.0%)** |
| Booking | P/E 1.16, $6bn | **P/E 20.1, $145bn** |
| Mastercard | 122.5M shares, $70bn | **907M shares, $518bn** |
| GOOGL / META / NVDA | no market cap at all | **$4,664bn / $1,523bn / $5,152bn** |
| rows with no valuation pillar | 18% | **1%** |
| `pe_unreliable` flags | 49 | **0** |

`shares_source` records `dei` (corroborated, 2,511), `implied_override` (dei
rejected, 562), `implied` (no dei tag, 238).

### Two wrong turns worth remembering

1. **A magnitude floor cannot identify a bad EPS.** The first attempt screened
   out "small" EPS as unreliable. Booking carries *two* bases at once — 0.40 /
   1.36 / 2.53 (true, ~775M shares: 333M/775M = $0.43) alongside 74.34 / 84.41
   (corrupt, ~34M) — so the filter kept precisely the corrupt values and
   returned a $6bn market cap for a $145bn company. `current_shares()` now
   **clusters** the estimates and takes the largest group, assuming nothing
   about scale.
2. **A median across two bases returns their midpoint** — a share count no
   quarter ever reported. Hence clustering rather than a robust average.

`fetch_prices.py --recompute` rebuilds the derived columns from the CSVs
already on disk. The prices were never wrong, only the share basis, so
repairing the arithmetic needs no second Yahoo run. `yfinance` is imported
lazily so `--recompute` works without it.

## 13. `test_scoring.py`

`python3 test_scoring.py` — 31 tests, pure stdlib, no fixtures on disk.

**Every test is a bug that shipped.** This code failed six times during
development and never once raised: each failure was a plausible score computed
from a corrupt input. A crash would have been easier. The assertions carry the
real values (Booking's two bases, Nvidia's 10:1, Mastercard's dei tag) so a
regression breaks a test rather than a workbook.

The suite was **mutation-tested**: ten deliberate reversions of the fixes above
(disable split detection, always trust the dei tag, score missing data as zero,
stop inverting multiples, allow partial TTM windows, …) and **all ten are
caught**. Two gaps found that way and closed:

* the clustering test passed against a plain median, because the fixture never
  produced a midpoint — now it does;
* `TestRules` never exercised `score_one`, so "score missing data as zero"
  survived — now covered by a test asserting two companies identical on
  scorable rules come out equal when one lacks the rest.

When adding a rule, add the case that would have caught its absence, and re-run
the mutation check rather than trusting a green suite.

---

## 14. Balance sheet, annual-basis scoring, and the two workbooks

### Balance sheet — INSTANT facts need their own path

`cash, short_term_investments, total_debt, net_debt, equity, assets,
liabilities, current_assets, current_liabilities` (+ durational
`interest_expense`). Coverage: equity 99%, cash 96%, assets 96%, total_debt 55%.

These carry an `end` and **no `start`** — a position at a moment, not a flow
over a period. `_collect()` skips them for exactly that reason, so they go
through `extract_instant()` and must **never** touch `derive_q4()` or
`ytd_to_quarterly()`: adding four cash balances does not give you a year's cash.
The same rule governs `build_excel.py`, where they are kept out of
`ANNUAL_ADDITIVE`.

**Coca-Cola reported $4.5bn of debt against an actual $43.6bn.** KO tags
`LongTermDebtAndCapitalLeaseObligations`, which was not in the candidate list,
so composition fell through to the current portion alone. `total_debt` now
takes the **max** of the composed components and the all-in tag — whether
`LongTermDebt` includes current maturities differs by filer, so neither is
reliably larger, and a component can never exceed the total. Verified after:
KO $43.6bn, Verizon $165.2bn, Home Depot's $13.9bn equity (buyback-driven),
Tesla net *cash* of -$35.8bn, JPMorgan's $5.0tn of assets.

### New Health pillar (6th, 20 points)

net debt/EBITDA, interest coverage, current ratio, debt/equity, ROE. Bands
calibrated against the real distribution — median net debt/EBITDA 1.52,
debt/equity 0.49, ROE 9.2% — and only 5% of companies max the pillar.

**Banks and insurers are exempt** (`LEVERAGE_EXEMPT`). Deposits are not
borrowings; a 10x debt/equity is ordinary for a bank, so scoring it would
penalise the sector for existing. The pillar is dropped, the total renormalised
over the other five, and `leverage_not_applicable` flagged so it is never
mistaken for a clean bill of health.

### Annual-basis scoring — 327 companies that were silently never rated

Foreign private issuers file 20-F/40-F and never a 10-Q.
`fetch_sec_fundamentals.py` captures them via `annual_only_rows()`, but
`load_quarters()` dropped every `period_type == "FY"` row, so they could not
reach `MIN_QUARTERS` and were **never scored at all** — the fetcher's work was
thrown away downstream.

`compute()` is now parameterised by `ppy` (periods per year): 4 for a quarterly
filer, 1 for an annual one. Every window is a multiple of it, so "3-year
growth" means 12 quarters or 3 years without any rule branching.
`period_basis` and `growth_basis` (`annual-3y` / `annual-2y`) record which was
used so the two are never silently compared.

| | before | after |
|---|---|---|
| Companies scored | 2,781 (75%) | **3,117 (84%)** |
| of which annual-basis | 0 | **301** |

The remaining 610 genuinely have nothing: no CIK, warrants and units, or
listings too recent to have filed.

**A transient-failure trap:** the full run reported `failed=331`, and those 331
were almost exactly the FY-only filers — they retried clean. Always diff the
ticker set against the previous run before trusting a rebuild; `ok=` in the log
is not enough.

### Two workbooks

| file | shape | purpose |
|---|---|---|
| `Fortune500_SP500_NASDAQ_Financials_5Y.xlsx` | 3,727 x **1,462** | everything per ticker on one row |
| `Company_Scores.xlsx` | 3,117 x 92 | the ranking, readable |

The **Data** sheet carries it all side by side: 60 months each of `close`,
`adj_close`, `volume`, `market_cap_est`, `pe_trailing_est`; 38 periods of
revenue/EBIT/EBITDA/net income/EPS/gross profit; cash flow (`ocf`, `capex`,
`fcf`); and 27 quarters of balance sheet (`cash`, `total_debt`, `net_debt`,
`equity`, `assets`). Verified on Apple: FY2025 revenue $416.2bn, FCF $98.8bn,
net debt $19.9bn, market cap $4.51tn, P/E 35.0.

`Company_Scores.xlsx` is separate because the Data sheet is 1,462 columns wide —
the right shape for analysis, the wrong shape for reading a ranking. It has a
`Scores` sheet and a `Ranked by sector` sheet.

**A latent crash fixed on the way:** `style()` did `int(sample.str.len().max() or 10)`,
and `max()` returns NaN for an all-empty column. NaN is **truthy**, so the `or 10`
fallback never fired and `int(NaN)` raised. Only `pd.isna` tests this correctly.
Also `append()` now uses `reindex` rather than `[columns]`, which raised
KeyError on a batch containing only annual-only rows.

---

## 15. Making silent failures loud

Two things in section 14 were flagged but not actually fixed. Both are now.

### 1. A failed ticker was checkpointed as done

`DONE_FILE` was written in the `except` branch as well as on success, so **any
transient error became permanent**: the ticker was marked complete and every
subsequent resume skipped it. That is how one run lost 293 tickers — almost
exactly the foreign private issuers — while reporting a healthy `ok=3119`.
They retried clean the moment they were asked again.

Three changes:

* **Checkpoint only on success.** A failure stays un-done so a re-run reaches it.
* **An automatic retry pass.** Failures are collected and retried once, serially,
  after a `RETRY_PAUSE` — enough to let a 429 clear. `--no-retry` disables it.
* **Failures always print.** They used to be swallowed by the every-25th-ticker
  progress throttle, which is how 331 of them went unseen in a live run.

### 2. A shrinking rebuild looked identical to a good one

The log's `ok=` count is the same whether or not the run quietly dropped a
tenth of the universe. The only thing that caught it was diffing the ticker set
by hand, which is not a control.

`coverage_check()` now runs at the end of every fetch: it keeps a
`data_quarterly.csv.tickers` snapshot beside the output, diffs against it, and
**exits non-zero** when losses exceed 2% so a scripted pipeline stops instead of
building a workbook on thin data. On a regression the snapshot is deliberately
**not** updated — otherwise the next run would compare against the degraded set
and see nothing wrong. Small losses are tolerated, because listings genuinely
come and go.

### `test_pipeline.py` — 11 tests, pandas-dependent

Kept separate from `test_scoring.py`, which stays stdlib-only so it runs
anywhere. Run with `./.venv/bin/python test_pipeline.py`. Mutation-tested:
five reversions (restore the NaN-truthy width bug, restore the pandas-2-only
dtype test, restore the append KeyError, always overwrite the snapshot,
checkpoint failures as done) and **all five are caught**.

### A third bug the tests found on their way in

Writing the width test surfaced a live regression: `style()` selected text
columns with `df[col].dtype == object`, and **pandas 3 gives string columns a
dedicated `str` dtype**. The check silently stopped matching, so every text
column in both workbooks quietly fell back to the default width. No error, no
symptom beyond a slightly worse-looking sheet — the kind of thing only a test
finds. Now uses `is_object_dtype or is_string_dtype`.

### Rescore after the fixes — and what the retry recovered

The retry fix immediately proved itself. 271 tickers were still missing;
running the fetcher with retry enabled recovered the ones that had been
transiently failed and permanently checkpointed (ADAG, ABLV, AFRI, AIFU and
others — every one of them fetches cleanly on request).

| | before | after |
|---|---|---|
| Tickers with data | 3,446 | **3,450** |
| Companies scored | 3,117 | **3,121** (84% of universe) |

Median score 48.9, median confidence 0.72; 2,816 quarterly-basis and 305
annual-basis. Spot-checks behave: Nvidia and Alphabet score 20/20 on health
(net cash), Verizon 5.3 (heavily indebted), Booking is flagged
`negative_equity` — which is correct, its buybacks have taken equity negative.

### THE NEXT REAL GAP: ~133 companies file under IFRS, not US-GAAP

Sampling the 267 that still return empty: **10 of 20 have full financials
under `facts["ifrs-full"]`**, and `build_rows()` only ever reads
`facts["us-gaap"]`. Thomson Reuters (398 IFRS concepts), ProQR (203), Lanvin
(156), UROY (124) are not thin filers — they are complete, in the wrong
namespace, and invisible.

Of the rest: 4 of 20 have no facts at all, 5 have a us-gaap namespace with
1-35 concepts (genuinely too thin), and 1 returns 404.

Closing this needs an IFRS concept map (`Revenue`, `ProfitLoss`, `Assets`,
`Equity`, `CashAndCashEquivalents`, …) alongside the existing US-GAAP one. It
would take coverage from 84% to roughly 87-88%.

---

## 16. IFRS support — a second taxonomy, not a variant of the first

`build_rows()` read only `facts["us-gaap"]`, so foreign private issuers
reporting under IFRS came back **"empty (no XBRL)"** while holding complete
financials. Sampling the 267 that returned nothing, **10 of 20 filed under
`facts["ifrs-full"]`**: Thomson Reuters tags 398 IFRS concepts, ProQR 203,
Lanvin 156, UROY 124. Roughly 133 companies were invisible for that reason.

The concept names are genuinely different, not merely spelled differently:

| | US-GAAP | IFRS |
|---|---|---|
| net income | `NetIncomeLoss` | `ProfitLoss` |
| operating income | `OperatingIncomeLoss` | `ProfitLossFromOperatingActivities` |
| operating cash flow | `NetCashProvidedByUsedInOperatingActivities` | `CashFlowsFromUsedInOperatingActivities` |
| equity | `StockholdersEquity` | `Equity` |
| D&A | `DepreciationDepletionAndAmortization` | `DepreciationAndAmortisation**Expense**` |

Note the British spelling. Getting it wrong fails **silently** — the column is
just blank — which is why there is a test pinning it.

### Choosing the taxonomy by matched concepts, not by presence

`pick_taxonomy()` counts how many probe concepts (revenue, net income, assets,
equity, OCF) each namespace actually carries and takes the winner. Selecting on
"is us-gaap non-empty" would be wrong: several filers carry a **vestigial**
us-gaap namespace of 1-35 concepts beside a complete IFRS one, and presence
would pick the empty half — exactly the old behaviour. Ties go to US-GAAP,
because `extract_revenue()`'s bank / lessor / excise rules only exist there.

A `taxonomy` column records which path produced each row.

Verified: Thomson Reuters $6,786M revenue / $17,966M assets, ProQR in **EUR**,
UROY in **CAD** — figures are in the filing currency and are NOT FX-converted,
same caveat as always. AAPL and KO are unchanged on us-gaap, so the fallback
did not disturb the existing path.

`test_pipeline.py` covers it: pure-US, pure-IFRS, the vestigial-namespace case,
tie-breaking, the British spelling, and an invariant that both concept maps
cover the same field set (a field in one and not the other silently blanks that
column for every filer on the other taxonomy). Mutation-tested 4/4.

### Result, and the currency bug IFRS exposed

| | before IFRS | after |
|---|---|---|
| Tickers with data | 3,450 | **3,618** (97% of universe with a CIK) |
| Companies scored | 3,121 | **3,246** (87%) |
| Annual-basis | 305 | **409** |
| Failed in the fetch | — | **0** |

**IFRS filers are disproportionately non-USD**, and that broke valuation.
Prices and market cap are USD; the financials are as filed. A P/S computed as
a USD market cap over EUR revenue is meaningless, and **75 tickers report in a
non-USD currency** (CAD 32, EUR 18, BRL 6, AUD 4, CNY 3, …).

`compute()` now gates on currency and drops P/E, P/S and P/FCF together for
those filers, flagging `non_usd_reporting`; the valuation pillar drops and the
total renormalises over five. Margins, growth and every ratio are unaffected —
they are currency-neutral.

**The gate has to run BEFORE any multiple is derived.** The first attempt
nulled the market cap only where P/S was computed, which is after the P/E
reconciliation — so ASML still read a P/E of 68.4 and Kaspi 0.02, a USD market
cap over tenge earnings. `test_scoring.py` asserts the ordering structurally,
because it is not observable from the rules alone.

---

## 17. Does the score predict anything? — `validate_scores.py`

`python3 validate_scores.py --min-confidence 0.6`

Compares each window's score against the **forward** return from that date.
Trailing return is not a test: momentum is 20% of the score and is built from
trailing prices, so scoring against the past measures the score against its own
input.

| window | months fwd | n | Spearman rho | top decile | bottom decile | spread |
|---|---|---|---|---|---|---|
| 2024-12 | 20 | 2,148 | **+0.262** | +27.5% | −56.1% | 83.3 pts |
| 2025-12 | 8 | 2,281 | **+0.255** | +15.1% | −26.7% | 41.8 pts |
| 2026-06 | 2 | 2,300 | **+0.256** | +4.9% | −6.5% | 11.3 pts |

Deciles are close to monotonic and the hit rate climbs steadily — share of
companies with a positive return runs 29% in the bottom decile to 74% in the
top over the 20-month window.

### It is not just momentum

Rebuilding the score from the five fundamental pillars with momentum removed:

| window | full score | fundamentals only |
|---|---|---|
| 2024-12 | +0.262 | **+0.220** |
| 2025-12 | +0.255 | **+0.207** |

Momentum contributes, but roughly 80% of the signal survives without it.

### Per-pillar rho — the growth pillar does nothing

| pillar | 2024-12 | 2025-12 | 2026-06 |
|---|---|---|---|
| growth | **−0.014** | **+0.003** | **+0.042** |
| profitability | +0.256 | +0.247 | +0.269 |
| quality | +0.156 | +0.172 | +0.290 |
| health | +0.116 | +0.090 | +0.042 |
| valuation | +0.197 | +0.106 | +0.196 |
| momentum | +0.268 | +0.289 | +0.009 |

**Growth has no measurable relationship with forward return in any window** —
20 of the 120 points are, on this evidence, noise. Profitability and quality
carry the load. Momentum is strong at 8-20 months and worthless at 2, which is
what momentum is generally understood to do.

The scores were deliberately NOT re-tuned against these results. Fitting the
rubric to the returns it is being tested on is overfitting, and the sample
below does not support it.

### What this evidence cannot bear

* **The windows overlap.** 2024-12 → now fully contains 2025-12 → now. Three
  results, not three independent observations.
* **One regime**, and one that favoured loss-making biotech and net-cash
  balance sheets.
* **Survivorship.** `universe.csv` is index membership as of collection, so
  companies delisted or acquired between T and now are absent. That flatters
  the bottom decile most — the true spread is probably wider, and −56% is if
  anything understated.
* rho ≈ 0.25 is modest in absolute terms. It is a useful ranking, not a
  forecast, and it says nothing about any individual company.

### The +0.26 is entirely downside — the score avoids losers, it does not pick winners

Ranking every ticker by forward return and comparing that ranking to the score
ranking splits the correlation cleanly in two.

**At the top, the score has no skill at all:**

| overlap | 2024-12 | random | 2025-12 | random |
|---|---|---|---|---|
| top 50 by score ∩ top 50 by return | 2% | 2% | 2% | 2% |
| top 100 ∩ top 100 | 5% | 5% | 2% | 4% |
| top 250 ∩ top 250 | 14% | 12% | 8% | 11% |

**At the bottom, it is 7-12x better than chance:**

| overlap | 2024-12 | random | 2025-12 | random |
|---|---|---|---|---|
| bottom 50 ∩ bottom 50 | **24%** | 2% | **14%** | 2% |
| bottom 100 ∩ bottom 100 | **38%** | 5% | **33%** | 4% |
| bottom 250 ∩ bottom 250 | **44%** | 12% | **36%** | 11% |

The clearest statement of it: **the 100 best-performing stocks had a median
score of 45.0, against a universe median of 54.0.** The biggest winners scored
BELOW average. The 100 worst had a median of 26.9.

Head-to-head on the 20-month window, the top performers were ranked by the
score at the 35th percentile (AXT, +2,957%), 6th (XMAX, +1,255%), 48th
(Western Digital, +1,134%), 47th (Micron, +968%) and 20th (Lumentum, +926%).
Those are memory, storage and photonics — deep cyclicals coming off a trough,
plus turnarounds. A rubric built on trailing profitability, margin quality and
recent momentum ranks a company at the bottom of its cycle *low*, which is
exactly when its forward return is highest.

**What the score is, therefore:** a filter that reliably identifies businesses
likely to keep deteriorating, and a poor instrument for finding the next
multi-bagger. Its top decile beat its bottom decile by 83 points over 20 months
almost entirely by not owning the bottom. Used as a screen to exclude, it earns
its keep; used as a buy list, it will systematically miss cyclical recoveries —
by construction, not by accident.
