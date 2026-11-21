"""Score the selector against a labeled set without Docker.

    python tools/evaluate.py SAMPLE_DIR SQLGLOT_ROOT [MAPS]

SAMPLE_DIR holds commits/*.patch, tests.json and answers.json (the kit's sample/ layout).
Prints, per commit, what was selected, its cost and whether it missed.
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from blast.select import Selector  # noqa: E402


def main():
    sample, root = sys.argv[1], sys.argv[2]
    maps = sys.argv[3] if len(sys.argv) > 3 else os.path.join(os.path.dirname(__file__), "..", "maps")
    t0 = time.process_time()
    sel = Selector(maps, root)
    t1 = time.process_time()
    answers = json.load(open(os.path.join(sample, "answers.json")))["commits"]
    total = 0
    misses = 0
    ideal = 0
    rows = []
    for cid in sorted(answers):
        patch = open(os.path.join(sample, "commits", cid + ".patch"), encoding="utf-8").read()
        bits, notes = sel.select(patch)
        workers = sel.pack(bits)
        cost = sel.cost(workers)
        failing = answers[cid]["failing"]
        missed = [f for f in failing if not (bits >> f) & 1]
        fbits = 0
        for f in failing:
            fbits |= 1 << f
        icost = sel.cost(sel.pack(fbits))
        ideal += icost
        total += cost
        if missed:
            misses += 1
        rows.append((cid, answers[cid].get("kind", "?"), bin(bits).count("1"), len(failing), cost / 1000,
                     icost / 1000, "MISS " + str(missed) if missed else "", " ".join(notes)))
    t2 = time.process_time()
    for r in rows:
        print("%s %-8s sel=%4d fail=%3d cost=%7.3f ideal=%7.3f %s  %s" % r)
    print(f"commits={len(rows)} simulated={total/1000:.3f}s misses={misses} "
          f"total={(total/1000) + 300*misses:.3f}s ideal={ideal/1000:.3f}s "
          f"cpu load={t1-t0:.2f}s select={t2-t1:.2f}s")


if __name__ == "__main__":
    main()
