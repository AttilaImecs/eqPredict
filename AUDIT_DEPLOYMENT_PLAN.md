# 🚀 DEPLOYMENT PLAN: US & Canada Financial Pipeline Release v2.0

***
**Target Environment:** Staging / Production Data Warehouse Ingestion Pipeline (e.g., Airflow DAG, Jenkins Job)
**Date of Plan Draft:** August 5, 2026
**Goal:** To execute the entire data pipeline reliably and report status/errors to a centralized logging system without manual intervention.

## ⚙️ I. Prerequisite Checks & Environment Setup

Before any script can run, the following must be verified:

1.  **Dependencies:** Python environment must have all necessary packages installed (minimum versions noted in `requirements.txt`):
    ```bash
    pip install pandas yfinance openpyxl # Add other dependencies here
    ```
2.  **Credentials/Secrets Management:** Environment variables or a secure vault access is mandatory for:
    *   `SECRET_API_KEY`: (For any external API calls beyond SEC/Yahoo).
    *   `CACHE_SECRETS`: For accessing rate-limited resources if needed outside of the core scripts.
3.  **Output Directory:** A dedicated, write-accessible directory (`/data/financial_exports/<DATE>/`) must exist for daily runs.

## 🔑 II. Pipeline Execution Sequence (The Master Workflow)

The entire process must run in a strict, sequential order to prevent overwriting or missing necessary foundational data.

**Run Script:** `run_all_ca.sh` (or equivalent orchestrator script)
**Failure Protocol:** If any step exits with code > 0, the entire pipeline MUST halt, and an alert must be triggered immediately via the monitoring system.

### **A. US S&P/F500 Pipeline Execution (The Anchor)**

1.  `python build_universe.py`: Runs first to ensure `universe.csv` is fresh (Optional: Run only if date drift detected).
2.  `python fetch_sec_fundamentals.py --workers 6 --restart`: Fetches the authoritative annual fundamentals data, respecting rate limits and using IFRS/USGAAP merge logic. **(Critical Step)**
3.  `python fetch_prices.py --workers 150 --restart`: Batched fetching of monthly prices and snapshot data.
4.  `python backfill_quarters.py`: Merges the SEC-provided annual history with vendor quarterly data to fill gaps.
5.  `python build_excel.py`: Generates the final `Fortune500_SP500_NASDAQ_Financials_5Y.xlsx`.

### **B. Canada TSX Composite Pipeline Execution**

1.  `python build_universe_ca.py`: Builds the constituent list from Wikipedia/TSX feeds, using name normalization matching against CIK where possible.
2.  `python fetch_sec_ca.py --workers 5 --restart`: Fetches annual fundamentals for cross-listed stocks, primarily relying on IFRS taxonomy and acknowledging the lack of structured quarterly data. **(Critical Step)**
3.  `python fetch_yf_ca.py --workers 4 --stage funds`: Runs vendor fetches to patch gaps for quarters (the necessary fallback).
4.  `python merge_fundamentals_ca.py`: The core merging step. This script must execute the confidence scoring and currency checks defined in **V2.0**.
5.  `python build_excel_ca.py`: Generates the final `TSX_Composite_Financials_5Y.xlsx`.

## ⚠️ III. Operational Monitoring & Error Handling (The Contract)

Every script failure must be logged to a central system (e.g., Splunk, ELK). Logs MUST include:
1.  **Failure Code:** The exit code and the specific module that failed.
2.  **Context Variables:** The current date, last run timestamp, and affected batch/ticker list segment.
3.  **Debugging Artifacts:** If a script fails (e.g., `fetch_sec_ca.py`), the full standard error output (`stderr`) must be captured for manual review.

## 🎯 Conclusion

By following this sequence, we guarantee data integrity from source to final workbook, minimizing assumptions and maximizing verifiable data points across both markets.
