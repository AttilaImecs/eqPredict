#!/usr/bin/env python
# ---------------------------------------------------------
# Script: Documents/Stock_script/validate_universe.py
# Purpose: Runs before all fetching scripts. Ensures the universe.csv file contains only viable, active ticker-CIK pairs with sufficient historical reporting records to proceed.
# Audit Requirement: [Enhancement] Missing Ticker Validation Pre-Run Step

import csv
from typing import Set, Tuple

def is_valid_cik(cik: str) -> bool:
    """Checks if a CIK resembles a standard US/Canadian format (8+ digits)."""
    return isinstance(cik, str) and len(cik) > 7 and all(c.isdigit() for c in cik)

def validate_universe(file_path: str) -> Tuple[Set[str], Set[str]]:
    """
    Reads the universe file and validates tickers based on known rules.
    Returns sets of (valid, invalid) CIKs/Tickers.
    """
    print("--- Starting Universe Validation ---")
    valid_tickers = set()
    invalid_records = set()

    try:
        with open(file_path, mode='r', newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                ticker = row['Ticker'].strip().upper()
                cik = row['CIK'].strip()

                if not is_valid_cik(cik):
                    invalid_records.add(f"Invalid CIK format for Ticker {ticker}: '{cik}'")
                    continue

                # Placeholder: In a real scenario, this would query an external API
                # or database to check for active listing status/last filing date.
                # For now, we enforce basic structural checks only.

                # Structural Check passed (assuming CIK is valid format)
                valid_tickers.add(ticker)

    except FileNotFoundError:
        print(f"ERROR: Universe file not found at {file_path}.")
        exit(1) # Fail fast if input file missing
    except Exception as e:
        print(f"CRITICAL ERROR during file read: {e}")
        exit(2)

    if invalid_records:
        print("\n!!! 🛑 VALIDATION FAILED !!!")
        print("The following records failed structural validation:")
        for msg in invalid_records:
            print(f"- {msg}")
        print("\nPlease clean the 'universe.csv' file before re-running.")
        exit(1)

    print(f"\n✅ Validation Success! Processed {len(valid_tickers)} valid tickers.")
    return valid_tickers, set()


def validate_ticker_completeness(valid_tickers, data_dir="."):
    """Check that tickers in universe.csv actually have data in CSV files."""
    import csv
    
    print("
--- Ticker Completeness Check ---")
    
    # Define expected data files
    data_files = {
        "data_quarterly.csv": "data_quarterly.csv",
        "data_monthly.csv": "data_monthly.csv", 
        "data_snapshot.csv": "data_snapshot.csv"
    }
    
    all_missing = set()
    partially_present = {}
    fully_present = set()
    present_files = {}
    
    for fname, label in data_files.items():
        fpath = os.path.join(data_dir, fname)
        if not os.path.exists(fpath):
            print(f"⚠ {label}: file not found (skipped)")
            continue
        
        try:
            # Read and collect tickers from the CSV
            with open(fpath, mode='r', newline='', encoding='utf-8') as cf:
                reader = csv.DictReader(cf)
                for row in reader:
                    tkr = row.get('Ticker', row.get('ticker', '')).strip().upper()
                    if tkr:
                        all_missing.add(tkr)
                        present_files.setdefault(tkr, set()).add(fname)
        except Exception as e:
            print(f"⚠ Error reading {label}: {e}")
    
    # Determine which universe tickers have data
    ticker_status = {}
    for ticker in valid_tickers:
        if ticker in present_files:
            files_with_data = present_files[ticker]
            n_files = len(files_with_data)
            
            if n_files == 3:
                fully_present.add(ticker)
                status = "present"
            elif n_files >= 1:
                partially_present.setdefault(ticker, []).append(status_str := f"{n_files}/3 files")
                ticker_status[ticker] = status_str
            else:
                ticker_status[ticker] = "missing"
        else:
            ticker_status[ticker] = "missing"
            
            # Check in universe.csv vs data files
            # Also add them to all_missing if they're not anywhere
    
    reported = []
    
    # Count presence counts from tickers that ARE present

    for fname, label in data_files.items():
        fpath = os.path.join(data_dir, fname)
        n_tickers_in_file = 0
        
        if not os.path.exists(fpath):
            print(f"⚠ {label}: file not found (skipped)")
            continue
            
        # Read CSV and count unique tickers
        with open(fpath, mode='r', newline='', encoding='utf-8') as cf:
            reader = csv.DictReader(cf)
            for row in reader:
                tkr = row.get('Ticker', row.get('ticker', '')).strip().upper()
                if tkr and tkr not in present_files:
                    # Only add to reported if it's actually missing from data files
                        reported.append(f"  {tkr}: NOT found in any data file" if not n_tickers_in_file else "")
                    
    n_present = sum(1 for s in ticker_status.values() if s == "present")
    n_partial = sum(1 for s in ticker_status.values() if s is not None and "partial" in s)
    n_missing_from_all_files = sum(1 for tkr, status in ticker_status.items() if status == "missing")

    print(f"
Universe tickers: {len(valid_tickers)}")
    print(f"Present in ALL 3 data files: {n_present} ({n_present/len(valid_tickers)*100:.1f}%)" if valid_tickers else "N/A")
    print(f"Partially present: {n_partial}")

    missing_data = []
    
    # Report the data that is in a file
    print("
Data completeness summary:")
    for fname, label in data_files.items():
        fpath = os.path.join(data_dir, fname)
        if os.path.exists(fpath):
            with open(fpath) as cf:
                n_lines = sum(1 for _ in cf) - 1  # subtract header
            print(f"✓ {label}: {n_lines} rows")

    print("
Validation complete!")
    return set(tkr for tkr, st in ticker_status.items() if st == "present"), partial
    
if __name__ == "__main__":
    # Assuming universe.csv is in the same directory as this script
    universe_file = "universe.csv"
    validate_universe(universe_file)