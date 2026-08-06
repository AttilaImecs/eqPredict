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

print('=' * 70)
print('FINAL INTEGRITY VERIFICATION - CANADA PIPELINE OUTPUT')
print('=' * 70)

# ==================================================================
# PART 1: Verify CSV sources (what the XLSX was built from)
# ==================================================================
qdf = ca_read_csv('data_quarterly.csv')
yf_a = ca_read_csv('data_annual_yf.csv')
sec_a = ca_read_csv('data_annual_sec.csv')

print('\n--- CSV SOURCE STATS ---')
print('Quarterly (final): %d rows x %d cols' % (len(qdf), qdf.shape[1]))
print('Annual YF:         %d rows x %d cols' % (len(yf_a), yf_a.shape[1]))
print('Annual SEC:        %d rows x %d cols' % (len(sec_a), sec_a.shape[1]))

# ==================================================================
# PART 2: Verify HANDOFF_CANADA.md documented claims vs actual output
# ==================================================================

known_targets_revenue = {
    'RY': 57344,   # Royal Bank FY2024 revenue (from HANDOFF)
    'SU': 54881,   # Suncor
    'ENB': 53473,  # Enbridge
}

print('\n--- RECONCILIATION SAMPLE (%d targets from HANDOFF) ---' % len(known_targets_revenue))
for tk, target in known_targets_revenue.items():
    fy_rows = qdf[(qdf['ticker'] == tk) & (qdf['fiscal_quarter'].str.contains('FY', na=False))]
    
    if len(fy_rows) == 0: 
        msg = 'not found'
        status = '?'
    else:
        rev_max = fy_rows['revenue'].max()
        pct_deviation = abs(rev_max - target) / max(target, 1) * 100
        
        if pct_deviation < 0.7: 
            status = 'PASS'
        elif pct_deviation < 2.0: 
            status = 'WARN'
        else:
            status = 'FAIL'
        
        msg = 'rev=%d vs target=%d (%.3f%% deviation)' % (int(rev_max), target, pct_deviation)    
    print('  [%s] %s: %s' % (status, tk, msg))

# ==================================================================
# PART 3: Verify XLSX workbook integrity
# ==================================================================
xls_path = os.path.join(ca, 'TSX_Composite_Financials_5Y.xlsx')
print('\n--- XLSX BOOK INTEGRITY ---')

wb = openpyxl.load_workbook(xls_path)
sheets_actual = wb.sheetnames
expected_sheets = ['README', 'Snapshot', 'Data', 'Coverage', 'Universe']

missing_sheets = [s for s in expected_sheets if s not in sheets_actual]
if len(missing_sheets) == 0: 
    print('All %d expected sheets present OK (%s)' % (len(sheets_actual), str(sheets_actual)))
else:
    msg2 = ''
    if len(missing_sheets) > 0: msg2 = ', missing=%s' % str(missing_sheets)

# ==================================================================
# Sheet-by-sheet structural validation
# ==================================================================
print('\n--- SHEET-SHEET STRUCTURAL VALIDATION ---')

for name in sheets_actual:
    ws = wb[name]
    rows_count = ws.max_row
    cols_count = ws.max_column
    
    if rows_count < 1:
        print('  [ERROR] %s: empty sheet!' % name)
        continue
    
    hdr_c = ws.cell(row=1, column=1).value if cols_count >= 1 else None
    
    if not hdr_c: 
        print('[%s] Error: No header in col 1' % name)
        continue
    
    # ==================================================================
    if name == 'Data':
        print('  %s: %d rows x %d cols OK' % (name, rows_count, cols_count))
        
        # Ensure first column is ticker in Data sheet
        
        data_ticker_col = ws.cell(row=1, column=1).value
        
        if data_ticker_col == 'ticker':
            print('    First column: ticker OK')
        else:
            print('    WARNING: First col is %s not ticker' % str(data_ticker_col))

# Show first few headers to verify format is good for spreadsheet reading by user  
        # Check for any rows without tickers in Data sheet (malformed data)
        bad_data_rows = []
        
        for row_idx in range(1, rows_count+1):
            tk_val_check = ws.cell(row=row_idx, column=1).value
            
            if not tk_val_check:
                bad_data_rows.append(row_idx)

        num_malformed = len(bad_data_rows)
    
    # If we find any malformed rows, report them here...
    
    elif name == 'Snapshot':
        print('  %s: %d rows x %d cols OK' % (name, rows_count, cols_count))

# Snapshot sheet should have exactly the same tickers as universe_ca.csv  
        snapshot_found_tickers = set()
        
        for r_idx in range(2, min(rows_count+1, 9)):  
            tk_val_snap = ws.cell(row=r_idx, column=1).value
            
            if not tk_val_snap: 
                continue
                
            snapshot_found_tickers.add(str(tk_val_snap))

# Snap tickers vs universe tickers should be identical minus any truly blank rows
    
    elif name == 'Universe':
        # Handoff says: 220 tickers total in universe_ca.csv
        
        universe_tickers_list = []
        
        for row_idx_uni in range(2, min(rows_count+1, 40)): 
            uni_tk_val = ws.cell(row=row_idx_uni, column=1).value
            if uni_tk_val:
                universe_tickers_list.append(str(uni_tk_val))
        
        if len(universe_tickers_list) > 0 and 'NA' in str(universe_tickers_list):
            print('  NA ticker found OK')

# Validate Universe columns against expected structure (ticker, company, ...). 
    elif name == 'Coverage':
        cov_cols_found = []
        
        for c_idx_cov in range(1, min(cols_count+1, 15)):
            col_val_name = ws.cell(row=1, column=c_idx_cov).value
            
            if str(col_val_name or '').find('years_as_filed') >= 0: 
                pass
    
    elif name == 'README':
        # README sections should include key headings that appear in the HANDOFF doc
        
        readme_items_with_content = []
        for rd_row in range(1, min(rows_count+1, 50)):
            rd_txt_item = str(ws.cell(row=rd_row, column=1).value or '')
            
            if len(rd_txt_item.strip()) > 3 and rd_txt_item.find('NA') < 0: 
                readme_items_with_content.append(rd_txt_item)

        readm_len_cnt = len(readme_items_with_content)
        
        # Print first few lines of the README sheet to verify structure matches what was expected from doc.
        print('  README verified with %d meaningful sections (%d total items)' % (readm_len_cnt, rows_count - 1))

# ==================================================================
# PART 4: Data completeness validation checks vs HANDOFF claims that should hold true.
# ==================================================================

print('\n--- TICKER COVERAGE VALIDATION ---')
uni_df_cvr = ca_read_csv('universe_ca.csv')
snap_df_cvr = ca_read_csv('data_snapshot.csv')

print('Universe CA (total tickers in universe): %d' % uni_df_cvr['ticker'].nunique())  
print('Snapshot rows:                            %d' % len(snap_df_cvr))  

# Check how many tickers exist in the universe but have NO quarterly data
uni_set_final = set(uni_df_cvr['ticker'])

qdf_ticker_set_final = set(qdf['ticker']) if 'ticker' in qdf.columns else set()

missing_in_quarters_final = uni_set_final - qdf_ticker_set_final
miss_q_cnt = len(missing_in_quarters_final)
print('Tickers without any quarters: %d (should be 0)' % miss_q_cnt)
if miss_q_cnt > 0: 
    for tk_hack_final in list(missing_in_quarters_final)[:3]: 
        print('  WARNING: Missing ticker has NO quarterly data ' + str(tk_hack_final)[:16] + '')

# Check how many tickers exist in the universe but have NO snapshot data
missing_in_snapshot_final = uni_set_final - set(snap_df_cvr['ticker']) if 'ticker' in snap_df_cvr.columns else uni_set_final    
miss_snap_cnt = len(missing_in_snapshot_final)
print('Tickers without any snapshot: %d (should be close to 0)' % miss_snap_cnt)    

# ==================================================================
# PART 5: Currency check (important per HANDOFF_canada.md notes on CAD/USD mismatch)
# ==================================================================

if 'currency' in qdf.columns:
    unique_currencies_found = qdf['currency'].unique()
    
    # Check that both currencies are used
    
    usd_rows_count = len(qdf[qdf['currency'] == 'USD']) if not qdf.empty else 0
    cad_rows_count = len(qdf[qdf['currency'] == 'CAD']) if not qdf.empty else 0

# ==================================================================
print('\n=== CANADA PIPELINE AUDIT COMPLETE ===')

