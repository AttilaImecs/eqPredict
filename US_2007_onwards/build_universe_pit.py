#!/usr/bin/env python3
"""
POINT-IN-TIME universes, one per quarter, from SEC's DERA data sets.

WHAT WAS WRONG BEFORE
  Every score window used ONE universe.csv -- today's listings. So a score
  computed "as at 2024-12" was actually evaluated against the companies that
  exist NOW. Anything that delisted in between was absent from every window,
  which is survivorship bias baked in at the universe level, before any
  scoring rule runs.

  This builds the universe from who ACTUALLY FILED in each quarter. A company
  that filed in 2021Q2 and vanished in 2022 appears in the 2021Q2 universe and
  not the 2023 one, which is the truth.

WHY DERA AND NOT THE TICKER APIs
  SEC's company_tickers.json is current-only -- it cannot tell you who was
  listed in 2021. The DERA quarterly financial-statement data sets are
  point-in-time by construction: each ZIP contains the filings SUBMITTED in
  that quarter. sub.txt also carries `sic` (industry) and `afs` (filer size),
  so sector and size come along for free and are themselves point-in-time.

ONLY 3 MB IS DOWNLOADED PER QUARTER
  Each ZIP is ~89 MB but sub.txt is the FIRST entry and only ~2 MB
  decompressed. A ranged GET of the first 3 MB plus a raw inflate gets it
  without pulling num.txt, which is 397 MB of numeric data we do not need here.

THE PRICE GAP IS THE POINT OF THE OUTPUT
  Yahoo does not retain price history for delisted tickers (see README), so
  companies that have since vanished can be scored but not priced. Rather than
  hide that, this reports `priceable` per quarter: how much of each historical
  universe a survivors-only backtest can actually see. That turns an invisible
  bias into a measured one.

Usage:
  python3 build_universe_pit.py --from 2021q1 --to 2026q2
  python3 build_universe_pit.py --from 2021q1 --to 2026q2 --outdir pit
"""

import argparse
import collections
import csv
import json
import os
import struct
import sys
import time
import urllib.request
import zlib

UA = {"User-Agent": "Research Data Collection (attila.imecs@gmail.com)"}
DERA = "https://www.sec.gov/files/dera/data/financial-statement-data-sets/{}.zip"
TICKERS = "https://www.sec.gov/files/company_tickers.json"
FORMS = ("10-K", "10-Q", "20-F", "40-F")


def quarters(a, b):
    ya, qa = int(a[:4]), int(a[5])
    yb, qb = int(b[:4]), int(b[5])
    out = []
    while (ya, qa) <= (yb, qb):
        out.append("%dq%d" % (ya, qa))
        qa += 1
        if qa == 5:
            qa, ya = 1, ya + 1
    return out


def fetch_sub(quarter, mb=4, retries=3):
    """sub.txt for one quarter, via a ranged GET + raw inflate."""
    url = DERA.format(quarter)
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={**UA, "Range": "bytes=0-%d" % (mb * 1_000_000 - 1)})
            with urllib.request.urlopen(req, timeout=120) as fh:
                buf = fh.read()
        except Exception as e:
            if attempt == retries - 1:
                return None, "fetch-failed: %s" % str(e)[:40]
            time.sleep(2 * (attempt + 1))
            continue
        try:
            (_sig, _v, _fl, _m, _mt, _md, _crc,
             _cs, _us, nlen, elen) = struct.unpack("<IHHHHHIIIHH", buf[:30])
            name = buf[30:30 + nlen].decode()
            if name != "sub.txt":
                return None, "unexpected first entry %s" % name
            d = zlib.decompressobj(-15)
            out = d.decompress(buf[30 + nlen + elen:])
            if not d.eof:
                # sub.txt grew past the window; widen once and retry
                if mb < 12:
                    return fetch_sub(quarter, mb + 4, retries)
                return None, "truncated"
            return out.decode("utf8", errors="replace"), "ok"
        except Exception as e:
            return None, "parse-failed: %s" % str(e)[:40]
    return None, "unreachable"


def current_tickers():
    """cik (int) -> ticker, from SEC's CURRENT map. Survivors only, by nature."""
    with urllib.request.urlopen(urllib.request.Request(TICKERS, headers=UA),
                                timeout=60) as fh:
        d = json.load(fh)
    out = {}
    for row in d.values():
        out.setdefault(int(row["cik_str"]), row["ticker"].strip().upper())
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", default="2021q1")
    ap.add_argument("--to", dest="end", default="2026q2")
    ap.add_argument("--outdir", default="pit")
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    print("loading current CIK->ticker map ...", file=sys.stderr)
    cur = current_tickers()
    print("  %d current mappings\n" % len(cur), file=sys.stderr)

    qs = quarters(args.start, args.end)
    print("%-9s %8s %10s %11s %10s  %s"
          % ("quarter", "filers", "priceable", "no ticker", "coverage", "status"))
    print("-" * 72)

    summary = []
    for q in qs:
        txt, st = fetch_sub(q)
        if txt is None:
            print("%-9s %8s %10s %11s %10s  %s" % (q, "-", "-", "-", "-", st))
            continue
        rows = list(csv.DictReader(txt.splitlines(), delimiter="\t"))
        seen = {}
        for r in rows:
            if r.get("form") not in FORMS:
                continue
            try:
                cik = int(r["cik"])
            except (TypeError, ValueError):
                continue
            # keep the richest record per CIK for the quarter
            seen.setdefault(cik, r)

        out_path = os.path.join(args.outdir, "universe_%s.csv" % q)
        priced = 0
        with open(out_path, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["cik", "ticker", "company", "sic", "afs", "form",
                        "period", "priceable"])
            for cik, r in sorted(seen.items()):
                tk = cur.get(cik, "")
                if tk:
                    priced += 1
                w.writerow([cik, tk, r.get("name", ""), r.get("sic", ""),
                            r.get("afs", ""), r.get("form", ""),
                            r.get("period", ""), "Y" if tk else "N"])
        n = len(seen)
        summary.append((q, n, priced))
        print("%-9s %8d %10d %11d %9.0f%%  %s"
              % (q, n, priced, n - priced, 100 * priced / max(n, 1), "ok"))

    if summary:
        print()
        tot = sum(s[1] for s in summary)
        tp = sum(s[2] for s in summary)
        print("TOTAL filer-quarters %d, priceable %d (%.0f%%)"
              % (tot, tp, 100 * tp / tot))
        print()
        print("The %.0f%% without a ticker are companies that have since stopped"
              % (100 - 100 * tp / tot))
        print("filing. They can be SCORED (fundamentals survive at SEC) but not")
        print("PRICED, because Yahoo drops delisted history. A survivors-only")
        print("backtest is blind to them -- this is the size of that blind spot.")
        print()
        print("universes written to %s/" % args.outdir)


if __name__ == "__main__":
    main()
