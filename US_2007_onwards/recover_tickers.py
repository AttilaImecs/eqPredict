#!/usr/bin/env python3
"""
Recover a historical ticker for a CIK, including companies that no longer file.

WHY THIS IS NEEDED
  Fundamentals are keyed on CIK and SEC keeps them forever. Prices are keyed on
  TICKER. For a company that stopped filing, the link between the two is cut:

    submissions API `tickers`      blank for 97% of ceased filers
    dei:TradingSymbol in companyfacts   absent for 100% of a 40-company sample
    DERA sub/num/tag.txt           no ticker field exists at all

  So the delisted third of any point-in-time universe can be counted but not
  priced -- which is fatal for a backtest, because those are exactly the
  companies a loser-avoidance screen should be judged on.

  The ticker IS in the filings themselves, on the cover page. This digs it out.

STRATEGIES, in the order tried. Each returns (symbol, confidence).
  1. dei:TradingSymbol in the filing's own XBRL instance. Cover-page tagging
     became mandatory in 2019, so this is exact where it exists and absent
     before then.
  2. The Section 12(b) cover-page table: a "Trading Symbol(s)" column header
     followed by the symbol.
  3. An exchange parenthetical -- "(NASDAQ: ABCD)", "(NYSE: ABCD)".
  4. A quoted symbol near the word "symbol".

VALIDATION IS THE POINT
  Strategies 2-4 are regexes over prose and WILL produce plausible-looking
  wrong answers -- a stray capitalised word reads exactly like a ticker. So
  `--validate` runs the parser against companies whose ticker we already know
  from universe.csv and reports accuracy per strategy. Do not trust a recovered
  ticker for a delisted company until the strategy that produced it has been
  measured on names where the answer is known.

Usage:
  python3 recover_tickers.py --validate 150     # accuracy on known tickers
  python3 recover_tickers.py --ciks 1301501,1085621
"""

import argparse
import collections
import csv
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request

UA = {"User-Agent": "Research Data Collection (attila.imecs@gmail.com)"}
SUBM = "https://data.sec.gov/submissions/CIK{:010d}.json"
ARCH = "https://www.sec.gov/Archives/edgar/data/{}/{}"
RATE = 6.0

# A ticker is 1-5 upper-case letters, optionally with a class suffix. Words
# that pass that shape but are never tickers cause most false positives.
STOP = {
    "THE", "AND", "FOR", "INC", "LLC", "LP", "CO", "CORP", "USA", "US", "SEC",
    "NYSE", "AMEX", "OTC", "GAAP", "IPO", "CEO", "CFO", "SEC", "ACT", "PART",
    "ITEM", "NOTE", "YES", "NO", "NONE", "N", "A", "I", "II", "III", "IV",
    "COMMON", "STOCK", "SHARE", "CLASS", "TITLE", "NAME", "EACH", "OF", "ON",
    "TRUE", "FALSE", "FORM", "FILE", "DATE", "PAGE", "TOTAL", "NET", "NASDAQ",
}
TICKER_RE = r"[A-Z]{1,5}(?:[.\-][A-Z]{1,2})?"

_lk = threading.Lock()
_next = [time.monotonic()]


def throttle():
    with _lk:
        now = time.monotonic()
        if _next[0] > now:
            time.sleep(_next[0] - now)
        _next[0] = max(now, _next[0]) + 1.0 / RATE


def get(url, timeout=45, binary=False):
    throttle()
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as fh:
        raw = fh.read()
    return raw if binary else raw.decode("utf8", errors="replace")


def plausible(sym):
    if not sym:
        return None
    s = sym.strip().upper().strip('.,;:"“”()')
    if not re.fullmatch(TICKER_RE, s) or s in STOP:
        return None
    return s


def strategy_xbrl_instance(cik, acc, files):
    """dei:TradingSymbol from the filing's own XBRL. Exact where present."""
    inst = next((f for f in files
                 if f.lower().endswith(("_htm.xml", ".xml"))
                 and "cal" not in f.lower() and "def" not in f.lower()
                 and "lab" not in f.lower() and "pre" not in f.lower()
                 and not f.lower().endswith("filingsummary.xml")), None)
    if not inst:
        return None
    try:
        txt = get(ARCH.format(int(cik), f"{acc}/{inst}"))
    except Exception:
        return None
    syms = [plausible(x) for x in
            re.findall(r"<dei:TradingSymbol[^>]*>\s*([^<\s]+)\s*</dei:TradingSymbol>", txt)]
    syms = [s for s in syms if s]
    if not syms:
        return None
    # A SPAC tags its UNIT, its share and its warrant all as TradingSymbol --
    # EVOXU / EVOX / EVOXW. The unit and warrant are derivative listings; the
    # common share is what a price series should follow. Where one symbol is a
    # strict prefix of another, the shorter is the share.
    best = min(syms, key=len)
    for s_ in syms:
        if s_ != best and s_.startswith(best) and s_[len(best):] in ("U", "W", "R", "UN"):
            continue
    return best


def _flatten(html):
    t = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    t = re.sub(r"<[^>]+>", " ", t)
    t = t.replace("&nbsp;", " ").replace("&amp;", "&").replace("&#160;", " ")
    return re.sub(r"\s+", " ", t)


def strategy_cover_table(text):
    """`Trading Symbol(s)` column on the Section 12(b) cover-page table."""
    m = re.search(r"Trading\s*Symbol\(?s?\)?", text, re.I)
    if not m:
        return None
    tail = text[m.end():m.end() + 120]
    for cand in re.findall(TICKER_RE, tail):
        p = plausible(cand)
        # A single letter is far more often a stray capital than a ticker; the
        # cover-table strategy produced "C" for Defi Technologies that way.
        if p and len(p) >= 2:
            return p
    return None


def strategy_item5(text):
    """Item 5, "Market for Registrant's Common Equity".

    THE STRATEGY THAT MATTERS FOR PRE-2019 FILINGS. The cover-page "Trading
    Symbol" column did not exist until the 2019 cover-page rule, so for older
    filings the ticker is not on the cover at all -- it is in the body, in the
    market-information section, phrased as prose. Anchoring on that section
    first avoids matching the word "symbol" somewhere in a footnote.
    """
    m = re.search(r"Market\s+for\s+(?:the\s+)?Registrant.{0,60}?Common\s+(?:Equity|Stock)",
                  text, re.I)
    window = text[m.start():m.start() + 4000] if m else text
    pats = [
        r"(?:under|with)\s+the\s+(?:trading\s+|ticker\s+)?symbols?\s*[:\s]*[\"\u201c']\s*(" + TICKER_RE + r")\s*[\"\u201d']",
        r"(?:under|with)\s+the\s+(?:trading\s+|ticker\s+)?symbols?\s*[:\s]+(" + TICKER_RE + r")\b",
        r"(?:ticker|trading)\s+symbols?\s*[:\s]*[\"\u201c']?\s*(" + TICKER_RE + r")\b",
        r"symbols?\s*[:\s]*[\"\u201c]\s*(" + TICKER_RE + r")\s*[\"\u201d]",
    ]
    for pat in pats:
        for mm in re.finditer(pat, window, re.I):
            v = plausible(mm.group(1))
            if v and len(v) >= 2:
                return v
    return None


def strategy_exchange_context(text):
    """A symbol stated beside an exchange name, in either order.

    Replaces the old `exchange-paren`, which scored 0/2 in validation by
    matching the first ticker-shaped word after any parenthesis. This requires
    the exchange name and the symbol to sit within a few words of each other
    AND the word "symbol" to be present, which is what made the difference.
    """
    pat = (r"(?:NASDAQ|New\s+York\s+Stock\s+Exchange|NYSE(?:\s+American|\s+MKT)?|"
           r"AMEX|OTCQB|OTCQX)[^.]{0,80}?symbols?[\s:]*[\"\u201c']?\s*("
           + TICKER_RE + r")\b")
    m = re.search(pat, text, re.I)
    if m:
        v = plausible(m.group(1))
        if v and len(v) >= 2:
            return v
    return None


def strategy_quoted_symbol(text):
    """`under the symbol "ABCD"` anywhere in the document -- loosest, so last."""
    m = re.search(r"symbol[\s:]*[\"\u201c]\s*(" + TICKER_RE + r")\s*[\"\u201d]", text, re.I)
    if m:
        v = plausible(m.group(1))
        if v and len(v) >= 2:
            return v
    return None


def recover(cik, want_forms=("10-K", "10-Q", "20-F", "40-F"), before=None):
    """(symbol, strategy) or (None, reason)."""
    try:
        sub = json.loads(get(SUBM.format(int(cik))))
    except Exception as e:
        return None, "submissions-fail"
    rec = sub.get("filings", {}).get("recent", {})
    forms = list(rec.get("form", []))
    accs = list(rec.get("accessionNumber", []))
    docs = list(rec.get("primaryDocument", []))
    dates = list(rec.get("filingDate", []))

    # `recent` holds only the most recent ~1000 filings. Everything older sits
    # in filings.files[], as separate JSON documents that must be fetched
    # explicitly. Skipping them silently hid every pre-2019 filing for an
    # active filer -- 10 of 70 companies in validation returned
    # "no-annual-filing" for that reason alone, not because none existed.
    if before and not any(f in want_forms and d < before
                          for f, d in zip(forms, dates)):
        for extra in sub.get("filings", {}).get("files", []):
            try:
                old = json.loads(get("https://data.sec.gov/submissions/"
                                     + extra["name"]))
            except Exception:
                continue
            forms += old.get("form", [])
            accs += old.get("accessionNumber", [])
            docs += old.get("primaryDocument", [])
            dates += old.get("filingDate", [])
    idxs = [i for i, f in enumerate(forms)
            if f in want_forms and (not before or (i < len(dates) and dates[i] < before))][:3]
    if not idxs:
        return None, "no-annual-filing"

    for i in idxs:
        acc = accs[i].replace("-", "")
        # file list for this accession
        try:
            idx = json.loads(get(ARCH.format(int(cik), f"{acc}/index.json")))
            files = [it["name"] for it in idx.get("directory", {}).get("item", [])]
        except Exception:
            files = []
        if files:
            s = strategy_xbrl_instance(cik, acc, files)
            if s:
                return s, "xbrl-instance"
        if not docs[i]:
            # Old filings often have no primaryDocument. The full submission
            # text file is always present at {accession}.txt.
            try:
                raw = get(ARCH.format(int(cik), f"{acc}/{accs[i]}.txt"))[:2500000]
            except Exception:
                continue
            text = _flatten(raw)
            for fn, nm in ((strategy_cover_table, "cover-table"),
                           (strategy_item5, "item5"),
                           (strategy_exchange_context, "exchange-context"),
                           (strategy_quoted_symbol, "quoted-symbol")):
                sy = fn(text)
                if sy:
                    return sy, nm + "-txt"
            continue
        try:
            html = get(ARCH.format(int(cik), f"{acc}/{docs[i]}"))[:2500000]
        except Exception:
            continue
        text = _flatten(html)
        for fn, nm in ((strategy_cover_table, "cover-table"),
                       (strategy_item5, "item5"),
                       (strategy_exchange_context, "exchange-context"),
                       (strategy_quoted_symbol, "quoted-symbol")):
            s = fn(text)
            if s:
                return s, nm
    return None, "not-found"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--validate", type=int, default=0,
                    help="test against N companies with a known ticker")
    ap.add_argument("--ciks", default="")
    ap.add_argument("--universe", default="universe.csv")
    ap.add_argument("--before", default=None,
                    help="only use filings before this date (YYYY-MM-DD) -- "
                         "simulates the pre-2019 regime ceased filers are in")
    args = ap.parse_args()

    if args.ciks:
        for c in args.ciks.split(","):
            sym, how = recover(c.strip())
            print(f"  CIK {c.strip():<10} -> {sym or '-':<8} ({how})")
        return

    if not args.validate:
        ap.error("give --validate N or --ciks")

    import random
    rows = [r for r in csv.DictReader(open(args.universe))
            if r["cik"].strip() and r["ticker"].strip()]
    random.seed(4)
    sample = random.sample(rows, min(args.validate, len(rows)))
    print(f"validating on {len(sample)} companies with a KNOWN ticker\n", file=sys.stderr)

    by_strat = collections.defaultdict(lambda: [0, 0])
    miss, wrong = 0, []
    for i, r in enumerate(sample, 1):
        sym, how = recover(r["cik"], before=args.before)
        truth = r["ticker"].strip().upper()
        if sym is None:
            miss += 1
        else:
            ok = sym == truth or sym == truth.replace("-", ".") or sym == truth.split("-")[0]
            by_strat[how][1] += 1
            if ok:
                by_strat[how][0] += 1
            else:
                wrong.append((r["ticker"], sym, how, r["company"][:28]))
        if i % 25 == 0:
            print(f"   {i}/{len(sample)}", file=sys.stderr)

    n = len(sample)
    print(f"\n{'strategy':<18}{'used':>7}{'correct':>9}{'accuracy':>10}")
    print("-" * 44)
    tot_ok = tot_used = 0
    for s, (ok, used) in sorted(by_strat.items(), key=lambda x: -x[1][1]):
        tot_ok += ok
        tot_used += used
        print(f"{s:<18}{used:>7}{ok:>9}{100*ok/used:>9.0f}%")
    print("-" * 44)
    print(f"{'ALL':<18}{tot_used:>7}{tot_ok:>9}{100*tot_ok/max(tot_used,1):>9.0f}%")
    print(f"\nno symbol found: {miss} of {n} ({100*miss/n:.0f}%)")
    print(f"end-to-end (correct / all attempted): {100*tot_ok/n:.0f}%")
    if wrong:
        print(f"\nWRONG ANSWERS ({len(wrong)}) -- these are the dangerous ones:")
        for t, s, how, nm in wrong[:12]:
            print(f"   truth {t:<7} got {s:<7} via {how:<15} {nm}")


if __name__ == "__main__":
    main()
