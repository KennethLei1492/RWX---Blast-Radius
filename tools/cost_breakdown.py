"""Per commit: static cost of the selection, CPU to run the candidates, and the ideal.

    python tools/cost_breakdown.py SAMPLE_DIR [MAPS]
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from blast.select import Selector  # noqa: E402

sample = sys.argv[1]
maps = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(__file__), "..", "maps")
sel = Selector(maps)
ans = json.load(open(os.path.join(sample, "answers.json")))["commits"]
tests = {t["number"]: t for t in json.load(open(os.path.join(sample, "tests.json")))["tests"]}
tot_static = tot_ideal = 0
rows = []
for cid in sorted(ans):
    bits, notes = sel.select(open(os.path.join(sample, "commits", cid + ".patch")).read())
    cands = [i for i in range(sel.n) if bits >> i & 1]
    static = sel.cost(sel.pack(bits)) / 1000
    fb = 0
    for f in ans[cid]["failing"]:
        fb |= 1 << f
    ideal = sel.cost(sel.pack(fb)) / 1000
    run = sum(sel.ms[i] for i in cands) / 1000
    files = sorted({tests[i]["file"].split("/")[-1] for i in cands})
    tot_static += static
    tot_ideal += ideal
    rows.append((cid, ans[cid]["kind"], len(cands), static, run, ideal, len(ans[cid]["failing"]), ",".join(files)[:70]))
for r in rows:
    print("%s %-8s n=%4d static=%7.3f run_sum=%7.3f ideal=%7.3f fail=%3d %s" % r)
print(f"static={tot_static:.3f} ideal={tot_ideal:.3f}")
