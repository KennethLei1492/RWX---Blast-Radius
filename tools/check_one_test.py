"""Check the swap map's claims about one slow test (run in the image).

    python tools/check_one_test.py CMPT_BIN LINE_TRACE_JSON TEST_ID TEST_NUMBER COUNT SEED SHARD NSHARDS

Picks swaps on lines the test executes that the map says the test cannot see, applies each
to the pinned tree (through blast/verify.py's overlay) and runs just that test: it must pass.
"""

import difflib
import json
import marshal
import os
import random
import sys
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from blast.verify import Verifier  # noqa: E402

cmpt, trace, test_id, num, count, seed, shard, nshards = sys.argv[1:9]
num, count, shard, nshards = int(num), int(count), int(shard), int(nshards)
table = marshal.loads(zlib.decompress(open(cmpt, "rb").read()))
d = json.load(open(trace))
lines = set()
for phases in d.get("tests", {}).values():
    for ph in phases.values():
        for f, ls in ph["lines"].items():
            lines.update((f, l) for l in ls)
cands = []
for f, bl in table.items():
    for l, sites in bl.items():
        if (f, l) not in lines:
            continue
        for col, op, alts in sites:
            for alt, bits in alts.items():
                if not bits >> num & 1:
                    cands.append((f, l, col, op, alt))
random.Random(int(seed)).shuffle(cands)
print(f"{len(cands)} swaps on lines the test runs that the map says it cannot see", flush=True)
v = Verifier()
v.start(heavy=False)
bad = 0
for i, (f, l, col, op, alt) in enumerate(cands[:count]):
    if i % nshards != shard:
        continue
    src = open(os.path.join(v.root, f), encoding="utf-8").read()
    ls = src.split("\n")
    old = ls[l - 1]
    assert old.startswith(op, col)
    ls[l - 1] = old[:col] + alt + old[col + len(op):]
    body = "".join(difflib.unified_diff(src.splitlines(True), "\n".join(ls).splitlines(True), f"a/{f}", f"b/{f}", n=3))
    patch = f"diff --git a/{f} b/{f}\n" + body
    failed = v.run(patch, [test_id], timeout=1500)
    ok = failed is not None and not failed
    bad += 0 if ok else 1
    print(f"{f}:{l} {op}->{alt}: {'pass' if ok else 'FAILED or unknown: ' + str(failed)}", flush=True)
print(f"shard {shard}: {bad} wrong", flush=True)
