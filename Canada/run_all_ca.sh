#!/usr/bin/env bash
# Full Canadian pipeline. Every step is resumable -- safe to re-run.
set -e
pip install yfinance --break-system-packages -q 2>/dev/null || true

python3 build_universe_ca.py                      # ~10s  S&P/TSX Composite + CIK match
python3 fetch_sec_ca.py     --restart --workers 5  # ~1m   as-filed IFRS annuals
python3 fetch_yf_ca.py      --restart --stage prices --batch 100
python3 fetch_yf_ca.py      --stage funds --workers 4
python3 fetch_yf_ca.py      --stage snapshot
python3 merge_fundamentals_ca.py                   # SEC wins, yfinance fills
python3 build_excel_ca.py                          # -> TSX_Composite_Financials_5Y.xlsx
