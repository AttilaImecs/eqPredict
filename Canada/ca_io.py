#!/usr/bin/env python3
"""
Shared CSV reader for the Canadian pipeline.

WHY THIS EXISTS
  National Bank of Canada trades as "NA". Pandas treats that as a null by
  default, so `pd.read_csv` turns the ticker of one of the Big Six banks into
  NaN -- on every read, in every script, silently. The bank then vanishes from
  joins, groupbys and the final workbook.

  `keep_default_na=False` with `na_values=[""]` means only a genuinely empty
  cell is null. Every column in this pipeline is written as either a number or
  an empty string, so nothing else changes.

  Any script in this folder that reads a CSV containing tickers should use this
  rather than pandas directly.
"""

import pandas as pd

# pandas' default null strings, minus "NA" and the bare "" we still want
_KEEP_AS_TEXT = {"NA", "N/A", "NAN", "NULL", "NONE"}


def read_csv(path, **kw):
    kw.setdefault("keep_default_na", False)
    kw.setdefault("na_values", [""])
    return pd.read_csv(path, **kw)


def is_ticker_safe(df, col="ticker"):
    """True when no ticker was lost to null-coercion. Cheap guard for tests."""
    return col not in df.columns or df[col].isna().sum() == 0
