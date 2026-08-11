#!/usr/bin/env python3
"""
STEP 2d of 4 -- Industry classification for the WHOLE universe, from SEC.

WHY THIS EXISTS
  universe.csv carries a `sector` for only the ~500 S&P 500 names, because it
  comes from the Wikipedia constituent scrape. The other 3,200 tickers have
  nothing. That gap makes peer-relative scoring impossible: score_companies.py
  had to use absolute margin bands, which handed Financials a 3.0x
  over-representation in its top 200 (median net margin 21.2% in Financials
  against 6.6% in Consumer Staples -- a grocer at its sector median scores
  mid-band while a bank at its sector median maxes the rule).

  SEC assigns an SIC code to every filer and serves it from the submissions
  API, so classification is available for all 3,717 tickers that have a CIK.

Writes: data_sic.csv  (ticker, cik, sic, sic_description, sector, exchange)
        _sic_done.txt (resumable checkpoint)

SIC IS NOT GICS
  SIC is a 1930s-era scheme and maps onto modern sectors imperfectly. The
  ranges in SIC_SECTORS below are ordered SPECIFIC-FIRST and the first match
  wins, which is load-bearing: 2833-2836 (biologics) must be tested before
  2800-2899 (chemicals) or every biotech lands in Materials, and 6798 (REITs)
  before 6700-6799 (holding offices) or every REIT lands in Financials.

  Where universe.csv already has a Wikipedia sector we keep BOTH -- the
  `sector` column here is the SIC-derived one, and score_companies.py prefers
  the Wikipedia value when present, since GICS is what the S&P names are
  actually indexed on.

Usage:
  python3 fetch_sic.py                 # resume
  python3 fetch_sic.py --restart
  python3 fetch_sic.py --limit 50      # smoke test
"""

import argparse
import csv
import json
import os
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

UA = {"User-Agent": "Research Data Collection (attila.imecs@gmail.com)"}
BASE = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
TIMEOUT = 30

U_FILE = "universe.csv"
OUT = "data_sic.csv"
DONE = "_sic_done.txt"
REQ_PER_SEC = float(os.environ.get("SEC_REQ_PER_SEC", 6.0))
# SEC's hard cap is 10/s. 6 rather than 8 because a sustained 429 block
# takes ~10 minutes to clear and costs far more than the throughput saved.
# NEVER run two SEC fetchers at once: 8+8 exceeds the cap and blocks both
# silently, since each script's own backoff hides the rejections.

# Ordered specific-first; first match wins. See the docstring.
SIC_SECTORS = [
    # --- Health Care (must precede Chemicals and Industrials) ---
    ((2833, 2836), "Health Care"),          # drugs, biologics
    ((3826, 3826), "Health Care"),          # lab analytical instruments
    ((3841, 3851), "Health Care"),          # medical devices, ophthalmic
    ((8000, 8099), "Health Care"),          # health services
    ((8731, 8731), "Health Care"),          # commercial biological research
    # --- Information Technology (must precede Industrials/Machinery) ---
    ((3571, 3579), "Information Technology"),   # computers & office equipment
    ((3600, 3629), "Information Technology"),   # electrical/electronic equipment
    ((3660, 3669), "Information Technology"),   # communications equipment
    ((3670, 3689), "Information Technology"),   # semiconductors, peripherals
    ((3820, 3825), "Information Technology"),   # instruments
    ((3827, 3829), "Information Technology"),
    ((7370, 7379), "Information Technology"),   # software & data processing
    # --- Communication Services ---
    ((2700, 2799), "Communication Services"),   # publishing
    ((4800, 4899), "Communication Services"),   # telecom, broadcasting
    ((7310, 7319), "Communication Services"),   # advertising
    ((7812, 7841), "Communication Services"),   # motion pictures, video
    # --- Energy ---
    ((1200, 1399), "Energy"),               # coal, oil & gas extraction
    ((2900, 2999), "Energy"),               # petroleum refining
    ((4610, 4619), "Energy"),               # pipelines
    ((5171, 5172), "Energy"),               # petroleum wholesale
    # --- Utilities ---
    ((4900, 4999), "Utilities"),
    # --- Real Estate (6798 REITs must precede Financials) ---
    ((6798, 6798), "Real Estate"),
    ((6500, 6599), "Real Estate"),
    ((1531, 1531), "Real Estate"),          # operative builders
    # --- Financials ---
    ((6000, 6411), "Financials"),           # banks, brokers, insurance
    ((6700, 6799), "Financials"),           # holding & investment offices
    # --- Consumer Staples ---
    ((100, 999), "Consumer Staples"),       # agriculture
    ((2000, 2199), "Consumer Staples"),     # food, beverages, tobacco
    ((2840, 2844), "Consumer Staples"),     # soap, cosmetics
    ((5140, 5149), "Consumer Staples"),     # groceries wholesale
    ((5400, 5499), "Consumer Staples"),     # food stores
    ((5912, 5912), "Consumer Staples"),     # drug stores
    # --- Consumer Discretionary ---
    ((2300, 2399), "Consumer Discretionary"),   # apparel
    ((2500, 2599), "Consumer Discretionary"),   # furniture
    ((3000, 3199), "Consumer Discretionary"),   # rubber, leather
    ((3630, 3639), "Consumer Discretionary"),   # household appliances
    ((3710, 3716), "Consumer Discretionary"),   # motor vehicles
    ((3940, 3949), "Consumer Discretionary"),   # toys, sporting goods
    ((5200, 5399), "Consumer Discretionary"),   # retail
    ((5500, 5911), "Consumer Discretionary"),
    ((5913, 5999), "Consumer Discretionary"),
    ((7000, 7099), "Consumer Discretionary"),   # hotels
    ((7900, 7999), "Consumer Discretionary"),   # recreation
    # --- Materials ---
    ((1000, 1099), "Materials"),            # metal mining
    ((1400, 1499), "Materials"),            # nonmetallic minerals
    ((2600, 2699), "Materials"),            # paper
    ((2800, 2899), "Materials"),            # chemicals (after drugs above)
    ((3200, 3399), "Materials"),            # stone, clay, glass, primary metals
    # Gaps found empirically on the first 2,478 rows fetched -- these SIC
    # codes fell through every range above and came back unclassified.
    ((3640, 3649), "Industrials"),          # electric lighting & wiring
    ((3650, 3659), "Consumer Discretionary"),   # household audio & video
    ((3690, 3699), "Industrials"),          # misc electrical machinery
    ((3810, 3819), "Industrials"),          # search/detection/navigation (A&D)
    ((3900, 3989), "Consumer Discretionary"),   # jewellery, toys, sporting goods
    ((3990, 3999), "Industrials"),          # misc manufacturing
    ((7360, 7369), "Industrials"),          # staffing, help supply
    ((8200, 8299), "Consumer Discretionary"),   # educational services
    # --- Industrials (broad catch-alls last) ---
    ((1500, 1799), "Industrials"),          # construction
    ((3400, 3599), "Industrials"),          # fabricated metal, machinery
    ((3700, 3799), "Industrials"),          # transportation equipment
    ((4000, 4599), "Industrials"),          # rail, trucking, air
    ((4700, 4799), "Industrials"),          # transportation services
    ((5000, 5139), "Industrials"),          # wholesale
    ((5150, 5170), "Industrials"),
    ((5173, 5199), "Industrials"),
    ((7200, 7299), "Industrials"),          # services
    ((7380, 7699), "Industrials"),
    ((8700, 8730), "Industrials"),          # engineering, accounting
    ((8732, 8999), "Industrials"),
]


def sector_for(sic):
    """SIC code -> sector name, or '' when unclassifiable."""
    try:
        code = int(sic)
    except (TypeError, ValueError):
        return ""
    for (lo, hi), name in SIC_SECTORS:
        if lo <= code <= hi:
            return name
    return ""


class RateLimiter:
    """Token bucket shared across worker threads."""

    def __init__(self, per_sec):
        self.interval = 1.0 / per_sec
        self.lock = threading.Lock()
        self.next_at = time.monotonic()

    def wait(self):
        with self.lock:
            now = time.monotonic()
            if self.next_at > now:
                time.sleep(self.next_at - now)
                self.next_at += self.interval
            else:
                self.next_at = now + self.interval


limiter = RateLimiter(REQ_PER_SEC)
_lock = threading.Lock()
_stats = {"ok": 0, "fail": 0, "nosic": 0}


def fetch_one(rec):
    """(ticker, cik) -> row dict, or None."""
    ticker, cik = rec
    url = BASE.format(cik=int(cik))
    for attempt in range(4):
        limiter.wait()
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as fh:
                if fh.status != 200:
                    raise OSError("HTTP %s" % fh.status)
                d = json.load(fh)
            sic = (d.get("sic") or "").strip()
            return {
                "ticker": ticker,
                "cik": cik,
                "sic": sic,
                "sic_description": (d.get("sicDescription") or "").strip(),
                "sector_sic": sector_for(sic),
                "exchange": "|".join(d.get("exchanges") or []),
            }
        except Exception:
            # SEC 403s and throttles under load; back off rather than give up.
            if attempt == 3:
                return None
            time.sleep(1.5 * (attempt + 1))
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--restart", action="store_true")
    args = ap.parse_args()

    if args.restart:
        for f in (OUT, DONE):
            if os.path.exists(f):
                os.remove(f)

    done = set()
    if os.path.exists(DONE):
        done = {l.strip() for l in open(DONE) if l.strip()}

    todo = []
    for r in csv.DictReader(open(U_FILE)):
        if not r["cik"] or r["ticker"] in done:
            continue
        todo.append((r["ticker"], r["cik"]))
    if args.limit:
        todo = todo[:args.limit]

    print("universe with CIK: %d, already done: %d, to fetch: %d"
          % (len(todo) + len(done), len(done), len(todo)), file=sys.stderr)
    if not todo:
        print("nothing to do", file=sys.stderr)
        return

    cols = ["ticker", "cik", "sic", "sic_description", "sector_sic", "exchange"]
    new = not os.path.exists(OUT)
    out_fh = open(OUT, "a", newline="")
    w = csv.DictWriter(out_fh, fieldnames=cols)
    if new:
        w.writeheader()
    done_fh = open(DONE, "a")

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(fetch_one, rec): rec for rec in todo}
        for i, fut in enumerate(as_completed(futures), 1):
            rec = futures[fut]
            row = fut.result()
            with _lock:
                if row is None:
                    _stats["fail"] += 1
                else:
                    if not row["sic"]:
                        _stats["nosic"] += 1
                    _stats["ok"] += 1
                    w.writerow(row)
                    done_fh.write(rec[0] + "\n")
                if i % 250 == 0:
                    out_fh.flush()
                    done_fh.flush()
                    rate = i / max(1e-9, time.time() - t0)
                    print("  %d/%d  %.1f/s  ok=%d fail=%d no-sic=%d"
                          % (i, len(todo), rate, _stats["ok"], _stats["fail"],
                             _stats["nosic"]), file=sys.stderr)

    out_fh.close()
    done_fh.close()
    print("done: ok=%d fail=%d no-sic=%d in %.0fs"
          % (_stats["ok"], _stats["fail"], _stats["nosic"], time.time() - t0),
          file=sys.stderr)


if __name__ == "__main__":
    main()
