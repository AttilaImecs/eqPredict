#!/usr/bin/env python3
"""Score every point-in-time universe, one quarter at a time.

score_companies.py reads `universe.csv` from the working directory, so each
quarter's universe is swapped in before its run. That is the whole point: a
score for 2022Q3 must be computed against the companies that existed in
2022Q3, not against today's listings.
"""
import glob, os, shutil, subprocess, sys, re

pit = "pit"
qs = sorted(glob.glob(os.path.join(pit, "uni_*.csv")))
if not qs:
    sys.exit("no per-quarter universes -- run prep_pit_inputs.py first")

backup = "universe.csv.orig"
if os.path.exists("universe.csv") and not os.path.exists(backup):
    shutil.copy("universe.csv", backup)

QEND = {"q1": "03", "q2": "06", "q3": "09", "q4": "12"}
print("%-9s %-9s %8s %10s %9s" % ("quarter", "as-of", "scored", "annual", "not rated"))
print("-" * 50)
for f in qs:
    q = os.path.basename(f)[4:-4]                 # e.g. 2021q1
    asof = "%s-%s" % (q[:4], QEND[q[4:]])
    shutil.copy(f, "universe.csv")
    out = "scores_pit_%s.csv" % q
    r = subprocess.run([sys.executable, "score_companies.py", "--as-of", asof,
                        "--out", out], capture_output=True, text=True)
    m = re.search(r"scored (\d+) \((\d+) on an annual basis\), not rated (\d+)", r.stderr)
    if m:
        print("%-9s %-9s %8s %10s %9s" % (q, asof, m.group(1), m.group(2), m.group(3)))
    else:
        print("%-9s %-9s  FAILED: %s" % (q, asof, (r.stderr or "").strip().splitlines()[-1][:60]))

if os.path.exists(backup):
    shutil.copy(backup, "universe.csv")
print("\nrestored universe.csv")
