# 🚀 FINANCIAL DATA PIPELINE (US & Canada)
## Overview
This repository contains a best-practice, hardened pipeline for ingesting and normalizing financial data for major US and Canadian index constituents. The goal is to produce comprehensive workbooks that allow side-by-side comparison of metrics across different jurisdictions.

## 📁 Structure:
- `Canada/`: Contains all modules related to the TSX Composite Index build.
- Core Scripts (Root): Contains the master logic for US S&P/F500 data processing.

## ▶️ Execution Steps (The Master Workflow)
1.  **Ensure Dependencies:** Run `pip install -r requirements.txt` (A new file will be created).
2.  **Run Orchestrator:** Execute the pipeline via the dedicated shell script: `./run_all_ca.sh` OR a comparable master script for US runs.

## ✨ Audit Status & Source of Truth
The entire system has been rigorously audited and hardened according to the principles outlined in the following documentation files, which should be reviewed before production deployment:
*   **US:** `AUDIT_SIGN_OFF.md` (Documents US fixes)
*   **Canada:** `AUDIT_SIGN_OFF_CANADA.md` (Documents Canadian adaptations)

## 🛠️ Key Architectural Mandates (DO NOT DEVIATE):
1.  **Unit Scale:** All financial calculations must use explicit scaling factors (e.g., dividing by $10^6$ for millions). Never trust the displayed number format.
2.  **Failure Handling:** The pipeline must always proceed even if one source fails, logging the failure point while continuing with available data.

*Developed and audited using defensive programming patterns.*