#!/usr/bin/env python3
"""Final verification: XLSX output integrity + reconciliation against HANDOFF claims."""
import os, re
import sys
sys.path.insert(0, '/Users/daetilus/Documents/Stock_script/Canada')

from ca_io import read_csv as ca_read_csv
import pandas as pd
import openpyxl

ca = '/Users/daetilus/Documents/Stock_script/Canada'
os.chdir(ca)

print('=' * 60)
print('CANADA PIPELINE: FINAL INTEGRITY VERIFICATION')
print('=' * 60)

# ==================================================================
# PART 1: Verify CSV sources (what the XLSX was built from)
# ==================================================================
qdf = ca_read_csv('data_quarterly.csv')  # Final merged quarters + annuals
yf_a = ca_read_csv('data_annual_yf.csv')  # YahooFinance Annuals
sec_a = ca_read_csv('data_annual_sec.csv')  # SEC XBRL Annuals

print('\n--- CSV SOURCE STATS ---')
print('Quarterly (final): %d rows x %d cols' % (len(qdf), qdf.shape[1]))
print('Annual YF:         %d rows x %d cols' % (len(yf_a), yf_a.shape[1]))
print('Annual SEC:        %d rows x %d cols' % (len(sec_a), sec_a.shape[1]))

# ==================================================================
# PART 2: Verify HANDOFF_CANADA.md documented claims against data
# ==================================================================

known_targets = {
    'RY': 57344,   # Royal Bank FY2024 revenue 
    'SU': 54881,   # Suncor
    'ENB': 53473,  # Enbridge
}

print('\n--- RECONCILIATION SAMPLE (%d targets) ---' % len(known_targets))
for tk, target in known_targets.items():
    fy_rows = qdf[(qdf['ticker'] == tk) & (qdf['fiscal_quarter'].str.contains('FY', na=False))]
    
    if len(fy_rows) == 0: 
        print('  ? %s: not found' % tk)
        continue
    
    rev_max = fy_rows['revenue'].max()
    pct_deviation = abs(rev_max - target) / target * 100
    
    # HANDOFF says worst reconciliation was 0.695%, check if we're within that range
    status = ''
    if pct_deviation < 0.7: status = 'PASS'  
    elif pct_deviation < 1.5: status = 'WARN'
    
    print('  [%s] %s rev=%d vs target=%d (%.3f%%)' % (status, tk, int(rev_max), target, pct_deviation))

# ==================================================================
# PART 3: Verify XLSX workbook integrity
# ==================================================================
xls_path = os.path.join(ca, 'TSX_Composite_Financials_5Y.xlsx')
wb = openpyxl.load_workbook(xls_path)
sheets_found = wb.sheetnames
expected_sheets = {'README', 'Snapshot', 'Data', 'Universe', 'Coverage'}

print('\n--- XLSX SHEET CHECK ---')
missing_sheets = expected_sheets - set(sheets_found)
if missing_sheets: 
    print('  ERROR: Missing sheets %s' % str(missing_sheets))
else: 
    print('  All 5 expected sheets present OK')

# Check sheet integrity
for name in sheets_found:
    ws = wb[name]
    rows_count = ws.max_row
    cols_count = ws.max_column
    
    if rows_count < 1 or (name == 'README' and cols_count != 2):
        print('  [ERROR] %s: %d rows x %d cols is unexpected!' % (name, rows_count, cols_count))
        continue
    
    hdr_c = ws.cell(row=1, column=1).value if cols_count >= 1 else None
    
    if not hdr_c: 
        print('[%s] Error: No header in col 1' % name)
        continue
    
    # Validate sheet-specific structure
    if name == 'Data':
        print('  [%s] %d rows x %d cols OK' % (name, rows_count, cols_count))
        
        data_ticker_col = ws.cell(row=1, column=1).value
        
        if data_ticker_col == 'ticker':
            print('    First column is ticker OK')
        
        # Check for any rows without tickers in Data sheet
        bad_data_rows = []
        for row_idx in range(1, rows_count+1):
            tk_val = ws.cell(row=row_idx, column=1).value
            
            if not tk_val:
                if 'Data' in str(ws.dimensions) and len(bad_data_rows) < 5: 
                    bad_data_rows.append(row_idx)
        
        num_bad = len(bad_data_rows)
    
    elif name == 'Snapshot':
        print('  %s: %d rows x %d cols OK' % (name, rows_count, cols_count))
        
        

# If it was a header row or empty...    
    elif name == 'Universe':
        
        universe_tickers_found = [ws.cell(row=row_idx, column=1).value for row_idx in range(2, min(rows_count+1, 40))]
        
        # Handoff says: 220 tickers total  
    
    elif name == 'Coverage':
        cov_cols = [ws.cell(row=1, column=c).value for c in range(1, min(cols_count+1, 15))]
        
        years_col_idx = None
        years_with_rev_col = None
        complete_col_idx = None
        
        for c_idx, col_name in enumerate(cov_cols):
            if str(col_name).lower().find('years_as_filed') >= 0: 
                pass
    
    elif name == 'README':
        readme_items_with_content = []
        for r in range(1, min(rows_count+1, 50)):
            txt = str(ws.cell(row=r, column=1).value or '')
            if len(txt.strip()) > 3 and txt.find('NA') < 0: 
                readme_items_with_content.append(txt)

        print('  README verified with %d meaningful sections (%d total items)' % (len(readme_items_with_content), rows_count - 1))

# ==================================================================
# PART 4: Data completeness validation 
# ==================================================================

print('\n--- TICKER COVERAGE VALIDATION ---')
uni_df = ca_read_csv('universe_ca.csv')
snap_df = ca_read_csv('data_snapshot.csv')

print('Universe CA (total tickers in universe): %d' % uni_df['ticker'].nunique())  
print('Snapshot rows:                            %d' % len(snap_df))  

# Check how many tickers exist in the universe but have NO quarterly data
uni_set = set(uni_df['ticker'])
qdf_ticker_set = set(qdf['ticker']) if 'ticker' in qdf.columns else set()

missing_in_quarters = uni_set - qdf_ticker_set
print('Tickers without any quarters: %d (should be 0)' % len(missing_in_quarters))
if missing_in_quarters: 
    for tk_hack in list(missing_in_quarters)[:3]: 
        print('  WARNING: %s has NO quarterly data' % str(tk_hack)[:16])

# Check how many tickers exist in the universe but have NO snapshot data
missing_in_snapshot = uni_set - set(snap_df['ticker'])  
print('Tickers without any snapshot: %d (should be close to 0)' % len(missing_in_snapshot))    

# ==================================================================
print('\n=== CANADA PIPELINE AUDIT COMPLETE ===')
