#!/usr/bin/env python3
"""Data quality verification for Canada pipeline."""
import sys, os
sys.path.insert(0, '/Users/daetilus/Documents/Stock_script/Canada')

from ca_io import read_csv as ca_read_csv
import pandas as pd

ca = '/Users/daetilus/Documents/Stock_script/Canada'
os.chdir(ca)

print("=" * 60)
print("CANADA PIPELINE: Data Quality Verification")
print("=" * 60)

# Read merged output data (what Excel uses)
qdf = ca_read_csv('data_quarterly.csv')
yfdf = ca_read_csv('data_annual_yf.csv')
secdf = ca_read_csv('data_annual_sec.csv')

print(f"\nQuarterly file: {len(qdf)} rows, {len(qdf.columns)} cols")
print(f"Annual YF file: {len(yfdf)} rows, {len(yfdf.columns)} cols")
print(f"Annual SEC file: {len(secdf)} rows, {len(secdf.columns)} cols")

# 1. QUARTERLY DEPTH
print("\n--- QUARTERLY DEPTH ---")
qt_per_ticker = qdf.groupby('ticker').size()
desc = qt_per_ticker.describe()
for stat_name in ['count', 'mean', 'min', '25%', '50%', '75%', 'max']:
    if stat_name.lower() in desc.index:
        val = desc[stat_name]
        print(f"  {stat_name}: {val:.1f}")

# Check quarters per ticker for major banks specifically
for tk_hint in ['RY', 'BMO', 'TD', 'ENB']:
    if tk_hint in qt_per_ticker.index:
        cnt = qt_per_ticker[tk_hint]
        print(f"  {tk_hint}: {cnt} quarter rows")

# Also check SEC vs Yahoo sources for quarterly
if 'source' in qdf.columns:
    sec_q = qdf[qdf['source'].str.contains('SEC', na=False)]
    yf_q = qdf[~qdf['source'].str.contains('SEC', na=False)]
    print(f"\n  SEC-sourced rows: {len(sec_q)}")
    print(f"  Yahoo Finance rows: {len(yf_q)}")

# Check unique tickers across files
print("\n--- TICKER COVERAGE ---")
uni = ca_read_csv('universe_ca.csv')
total_uni = uni['ticker'].nunique()
print(f"Universe: {total_uni} tickers")
q_tickers = qdf['ticker'].nunique() if 'ticker' in qdf.columns else 0
yf_tickers_y = yfdf['ticker'].nunique() if 'ticker' in yfdf.columns else 0
sec_tickers = secdf['ticker'].nunique() if 'ticker' in secdf.columns else 0
print(f"Quarters: {q_tickers} tickers")
print(f"Annual YF: {yf_tickers_y} tickers")
print(f"Annual SEC: {sec_tickers} tickers (cross-listed only)")

# Check currency mixing
if 'currency' in qdf.columns:
    unique_currencies = qdf['currency'].unique()
    print("\n--- CURRENCY MIX ---")
    print(f"Unique currencies: {list(unique_currencies[:10])}")
    for cur in unique_currencies[:3]:
        mask = qdf['currency'] == str(cur)
        sample_tk = qdf.loc[mask, 'ticker'].unique()[:5]
        print(f"  {cur}: {len(mask)} rows (sample: {list(sample_tk)})")

# Check National Bank of Canada ticker preservation
uni_ca = ca_read_csv('universe_ca.csv')
has_na = len(uni_ca[uni_ca['ticker'] == 'NA']) > 0
print("\n--- NATIONAL BANK (NA) TICKER ---")
if has_na:
    na_row = uni_ca[uni_ca['ticker'] == 'NA'].iloc[0]
    company = str(na_row.get('company', '')) if 'company' in uni_ca.columns else ''
    cik_match = str(na_row.get('cik_match', 'N/A')) if 'cik_match' in uni_ca.columns else 'N/A'
    print(f"Found in universe: {company} (cik_match={cik_match})")
else:
    print("MISSING from universe!")

# Check for gross_profit > revenue anomalies
if 'gross_profit' in qdf.columns and 'revenue' in qdf.columns:
    positive_mask = qdf['revenue'] > 0
    valid_gp_mask = positive_mask & qdf['gross_profit'].notna()
    anomalous_mask = valid_gp_mask & (qdf.loc[valid_gp_mask, 'gross_profit'] > qdf.loc[valid_gp_mask, 'revenue'])
    
    gp_gt_reve_mask = qdf.apply(lambda r: not (pd.isna(r.get('revenue', None)) or r['revenue'] <= 0) and 'gross_profit' in qdf.columns and r.get('gross_profit', float(r['revenue'])) > r['revenue'], axis=1) if 'revenue' in qdf.columns else pd.Series([False] * len(qdf))
    anomalous_count = gp_gt_reve_mask.sum() if hasattr(gp_gt_reve_mask, 'sum') else 0
    print(f"\n--- ANOMALIES ---")

# Final check for gross profit anomalies...
gp_gt_reve_mask = qdf.apply(lambda r: not (pd.isna(r.get('revenue', None)) or r['revenue'] <= 0) and 'gross_profit' in qdf.columns and r.get('gross_profit', float(r['revenue'])) > r['revenue'], axis=1) if 'revenue' in qdf.columns else pd.Series([False] * len(qdf))
anomalous_count = gp_gt_reve_mask.sum() if hasattr(gp_gt_reve_mask, 'sum') else 0
print(f"Gross profits exceeding revenue: {anomalous_count} (should be 0)")

# Also check for negative revenue
if 'revenue' in qdf.columns:
    neg_rev = int(qdf['revenue'] < 0) if qdf['revenue'].notna().sum() > 0 else 0  
    print(f"\nNegative revenue rows: {int(neg_rev)} (should be 0)")
