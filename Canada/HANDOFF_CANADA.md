# HANDOFF — S&P/TSX Composite financial dataset

## 🔁 CLEAN REBUILD — 2026-08-06

Whole pipeline re-run from scratch, universe included. Output:
**`TSX_Composite_Financials_5Y_v2.xlsx`**, 220 × 333, **15 known figures at
0.695%**, zero impossible values, National Bank present. The original
`TSX_Composite_Financials_5Y.xlsx` was left untouched and re-verified
byte-identical by SHA256.

**One blocking defect fixed:** `build_universe_ca.py` sent the User-Agent
`StockPipelineDataCollector/1.0`. SEC requires a contact email and returns
**403** for generic agents, so the CIK map fetch failed — and because `.json()`
was called without checking the status, it surfaced as
`JSONDecodeError: line 1 column 1`, which points nowhere near the real cause.
The UA now matches the rest of the pipeline, and `sec_ticker_map()` checks the
status, retries with backoff, and exits with a clear message rather than
writing a universe with no cross-listing data.

---

## ✅ FULL RUN COMPLETE — all 220 constituents

`TSX_Composite_Financials_5Y.xlsx` — 220 rows × 333 columns. Same wide layout as
the US workbook (one row per ticker, `metric_period` columns, values in
millions). **17 known figures reconcile to 0.695%**, zero impossible values,
zero duplicates, 205 of 220 with four or more annual years.

Three issues surfaced only at full scale and are fixed — section 5b.

Built from the learnings in the US `HANDOFF.md`, but the **data architecture is
genuinely different** and the reason is worth understanding before trusting the
numbers.

---

## 1. The central constraint: there is no Canadian XBRL feed

The US pipeline reaches 24 quarters per company because domestic issuers file a
10-Q every quarter with fully tagged XBRL, free, from SEC.

Canada has no equivalent:

- **SEDAR+** (the Canadian regulator's filing system) publishes no free
  structured-financials API.
- **SEC covers only cross-listed companies** — 108 of the 220 Composite
  constituents — and for those it holds **annual data only**. A Canadian issuer
  under MJDS files a **40-F once a year**; interim results go on a **6-K**, which
  carries little or no detail tagging. Agnico Eagle's entire XBRL history
  contains exactly two period lengths: **364 and 365 days**. There are no
  quarters to find.

**Consequence, stated plainly:**

| | US pipeline | Canada pipeline |
|---|---|---|
| Annual history | 6 years | **5–6 years** ✅ |
| Quarterly history | 24 quarters | **~6 quarters** |
| Quarterly source | SEC, as-filed | Yahoo (vendor) |
| Annual source | SEC, as-filed | SEC as-filed where cross-listed, else Yahoo |

**The annual columns are the reliable part of this dataset.** They are what the
workbook leads with. Treat the quarterly columns as a recent-trend view, not a
history.

---

## 2. Pipeline

```bash
./run_all_ca.sh          # or the six steps individually:

python3 build_universe_ca.py                        # S&P/TSX Composite + CIK match
python3 fetch_sec_ca.py   --restart --workers 5     # as-filed IFRS annuals
python3 fetch_yf_ca.py    --restart --stage prices  # batched monthly prices
python3 fetch_yf_ca.py    --stage funds --workers 4 # per-ticker quarters + annuals
python3 fetch_yf_ca.py    --stage snapshot
python3 merge_fundamentals_ca.py                    # SEC wins, Yahoo fills
python3 build_excel_ca.py
```

Every step checkpoints and is safe to re-run.

**Prices are batched, fundamentals are not.** `yf.download()` takes a list, which
is what rescued the 3,727-name US run after Yahoo rate-limited the per-ticker
approach at ~500 names. Quarterly statements have no batch endpoint, so those go
one at a time — fine for a 220-name index, and the reason this design would
**not** scale to thousands of tickers.

---

## 3. Findings from building it

**Canadian filers are split across two taxonomies.** Royal Bank, Scotiabank and
Agnico Eagle report under **IFRS** (`ifrs-full`); Enbridge and Canadian Pacific
Kansas City report under **US GAAP**. The US script reads only
`facts["us-gaap"]` and would return nothing for the IFRS group. Concept names
differ too — `ProfitLoss` not `NetIncomeLoss`, `ProfitLossFromOperatingActivities`
not `OperatingIncomeLoss`. `fetch_sec_ca.py` merges both taxonomies and lists
both names for every field.

This bit during the build: searching IFRS names only, Enbridge and CP came back
with operating income but **no revenue at all**, because `OperatingIncomeLoss`
happens to appear in both lists while `Revenues` does not.

**Ticker matching across borders is unsafe.** SEC's `SHOP` is *Tremont Mortgage
Trust*, not Shopify. `build_universe_ca.py` matches on **normalised company
name**, accepting a ticker match only when the names corroborate it. 99 of the
108 CIK matches come from an exact name match.

**Currency is genuinely mixed and is NOT converted.** In the 26-ticker sample,
20 report in CAD and 6 in USD — Agnico Eagle, Barrick, Nutrien, Kinaxis and
Dundee Precious Metals all file in USD. **Check the `currency` column before
summing or ranking across companies.** This is a bigger issue here than in the
US dataset, where it was a rounding-error footnote.

**Gross profit is often absent but derivable.** IFRS filers frequently tag
`CostOfSales` without `GrossProfit`, so gross profit is computed as
revenue − cost of sales when the tag is missing.

---

## 4. Reused from the US pipeline without change

These were all learned the hard way over there and carried straight across:

- values scaled to **millions in the data**, not via a display format, so a
  formula referencing a cell gets what the screen shows;
- **annual columns lead the sheet**, blank unless the year is genuinely complete;
- the **seasonally adjusted FY projection** (`revenue_FY2026E`), scaling
  year-to-date by the prior year's seasonal share rather than annualising
  naively;
- **period labels not raw dates** as column headers;
- label ordering **parsed from the label**, never from underlying dates;
- fiscal years ending in the first days of January belong to the **prior year**;
- **gross profit above revenue is dropped** as an incompatible-basis artefact;
- **negative revenue is dropped** outright.

---

## 5. Sanity test — 26 tickers, stratified

Sample spans all 11 GICS sectors and deliberately mixes **14 cross-listed** with
**12 TSX-only** companies, since those two groups take completely different code
paths.

| Check | Result |
|---|---|
| Prices | 26/26, median **60 months** |
| Annual years | 26/26 with **4+ years** of revenue |
| Quarters | median 5 (the known ceiling) |
| Negative revenue | 0 |
| Gross profit > revenue | 0 |
| Duplicate periods / labels | 0 |
| Implausible margins | 0 |
| **15 known figures vs reported** | **worst 0.695%** |

Verified against independently reported results: Royal Bank FY2024 revenue
57,344 and net income 16,240; Suncor 54,881; Enbridge 53,473; CNQ 35,656;
Scotiabank 33,670; Nutrien USD 25,972; CPKC 14,546 with net income 3,713;
Barrick USD 12,922; Agnico Eagle USD 8,285.75.

---

## 5b. Issues found at full scale — all fixed

**1. Pandas silently deleted a Big Six bank.** National Bank of Canada trades as
**`NA`**, which `pd.read_csv` and `pd.read_html` treat as a null by default. The
bank's ticker became NaN on every read, so it vanished from the universe, the
fetches and the workbook — 219 companies instead of 220, with no error anywhere.

Fixed in two places: `keep_default_na=False` when scraping the constituent
table, and a shared `ca_io.read_csv()` that every script uses, where only a
genuinely empty cell counts as null. **Any new script in this folder must use
`ca_io.read_csv` rather than pandas directly**, or the bank disappears again.

**2. A blank row survived into the universe.** The `keep_default_na=False` fix
had a side effect: an empty row in the Wikipedia table became empty *strings*
rather than NaN, so it was no longer dropped and reached the price fetcher as a
ticker-less record, which crashed on a float where a string was expected.
`build_universe_ca.py` now requires a plausible TSX symbol and a non-blank
company name, and reports anything it drops.

**3. Duplicate fiscal-year labels from stub filings.** Denison Mines files
annual periods ending both 30 November and 31 December, the November set being
empty stubs of 0.0. Both mapped to `FY2022`, so a stub could displace the real
figures. `merge_fundamentals_ca.py` now ranks candidate rows by how many
measures are actually populated and non-zero, and keeps the fullest — the same
principle used for fiscal-year changes in the US pipeline.

---

## 6. Known gaps

- **CNR has no FY2021.** SEC's facts cover 2020 and 2022–2025 for that concept,
  and Yahoo's annual history only reaches 2022. The cell is blank rather than
  wrong — which is the right outcome, but it is a gap.
- **FY2020 is sparse for TSX-only names.** Yahoo returns 4–5 annual years, so
  the earliest year is populated mainly for cross-listed companies where SEC
  reaches further back.
- **112 of 220 constituents have no SEC registration**, so every annual figure
  for them is vendor data with no as-filed cross-check. The `years_as_filed`
  column on the Coverage sheet makes this visible per company.
- **The Composite list is scraped from Wikipedia.** It is the only free source
  for constituents; S&P does not publish the list openly. Membership changes
  quarterly, so re-run `build_universe_ca.py` before a fresh full pull.

---

## 7. Files

| File | Purpose |
|---|---|
| `ca_io.py` | shared CSV reader — **required**, keeps ticker `NA` from becoming null |
| `build_universe_ca.py` | S&P/TSX Composite constituents, sector, name-based CIK match |
| `fetch_sec_ca.py` | as-filed annual fundamentals, IFRS **and** us-gaap |
| `fetch_yf_ca.py` | quarters, annual fallback, batched prices, snapshot |
| `merge_fundamentals_ca.py` | combines the three sources; SEC wins, Yahoo fills |
| `build_excel_ca.py` | the workbook |
| `run_all_ca.sh` | all six steps in order |
| `universe_ca.csv` | 220 constituents, 108 with a CIK (incl. National Bank) |
| `universe_sample25.csv` | the stratified sanity-test sample |
| `TSX_Composite_Financials_5Y.xlsx` | **the deliverable** |

The full 220-name run is the default (`universe_ca.csv`) and takes roughly
4 minutes end to end. `universe_sample25.csv` remains for quick smoke tests via
`--universe universe_sample25.csv`.
