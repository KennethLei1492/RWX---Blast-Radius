"""Re-select patches whose failing tests are known (tools/oracle_exp.py output) with the
current code and maps: any known-failing test left out is a miss.

    python tools/recheck.py EXP_DIR PATCH_DIR MAPS_DIR
"""

import glob
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from blast.select import Selector  # noqa: E402

exp, pdir, maps = sys.argv[1:4]
sel = Selector(maps)
old = new = ideal = 0
misses = []
changed = []
n = 0
for p in sorted(glob.glob(os.path.join(exp, "*.json"))):
    d = json.load(open(p))
    if "failing" not in d or "ideal_ms" not in d:
        continue
    cid = os.path.basename(p)[:-5]
    bits, notes = sel.select(open(os.path.join(pdir, cid + ".patch"), encoding="utf-8").read())
    n += 1
    cost = sel.cost(sel.pack(bits))
    old += d["static_ms"]
    new += cost
    ideal += d["ideal_ms"]
    lost = [f for f in d["failing"] if not bits >> f & 1]
    if lost:
        misses.append((cid, lost[:5], notes))
    if cost != d["static_ms"]:
        changed.append((d["static_ms"] - cost, cid, d["sel"], bin(bits).count("1"), len(d["failing"]), notes))
for c in sorted(changed, reverse=True)[:40]:
    print("saved %7.3fs %s sel %d -> %d fail=%d %s" % (c[0] / 1000, c[1], c[2], c[3], c[4], " ".join(c[5])[:70]))
resid = []
for p in sorted(glob.glob(os.path.join(exp, "*.json"))):
    d = json.load(open(p))
    if "failing" not in d or "ideal_ms" not in d:
        continue
    cid = os.path.basename(p)[:-5]
    patch = open(os.path.join(pdir, cid + ".patch"), encoding="utf-8").read()
    bits, notes = sel.select(patch)
    over = sel.cost(sel.pack(bits)) - d["ideal_ms"]
    if over > 0:
        plines = patch.splitlines()
        minus = [l[1:].strip() for l in plines if l.startswith("-") and not l.startswith("---")]
        plus = [l[1:].strip() for l in plines if l.startswith("+") and not l.startswith("+++")]
        resid.append((over, cid, bin(bits).count("1"), len(d["failing"]), " ".join(notes)[:60],
                      (minus or [""])[0][:44], (plus or [""])[0][:44]))
if os.environ.get("RESIDUAL"):
    print("residual overspend:")
    for r in sorted(resid, reverse=True)[:int(os.environ["RESIDUAL"])]:
        print("  over %6.2fs %s sel=%d fail=%d %s | %s -> %s" % ((r[0] / 1000,) + r[1:]))
for m in misses:
    print("MISS", m)
print(f"{n} patches: before={old/1000:.1f}s now={new/1000:.1f}s ideal={ideal/1000:.1f}s misses={len(misses)}")
