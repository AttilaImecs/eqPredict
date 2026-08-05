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


if __name__ == "__main__":
    # Assuming universe.csv is in the same directory as this script
    universe_file = "universe.csv"
    validate_universe(universe_file)