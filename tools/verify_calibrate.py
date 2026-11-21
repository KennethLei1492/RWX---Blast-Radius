"""Measure the fixed CPU of verifying tests from each test file (run in the image).

    python tools/verify_calibrate.py TESTS_JSON OUT_JSON

For every test file, the verifier runs that file's cheapest test on the unpatched tree in a
forked child: the CPU covers the fork, a fresh sqlglot import, the test module import and
any setUpClass. blast/policy.py adds these per file (beyond the common part) to a run's
estimate. Values are this machine's milliseconds; policy.SCALE converts to the scorer's.
"""

import json
import os
import resource
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from blast.verify import Verifier  # noqa: E402


def cpu():
    r = resource.getrusage(resource.RUSAGE_CHILDREN)
    return r.ru_utime + r.ru_stime


tests = json.load(open(sys.argv[1]))["tests"]
cheapest = {}
for t in tests:
    if t["file"] not in cheapest or t["seconds"] < cheapest[t["file"]]["seconds"]:
        cheapest[t["file"]] = t
v = Verifier()
v.start(heavy=True)
costs = {}
for f, t in sorted(cheapest.items()):
    samples = []
    for _ in range(2):
        c0 = cpu()
        failed = v.run("", [t["id"]], timeout=300)
        samples.append(cpu() - c0)
    costs[f] = round(1000 * min(samples) - 1000 * t["seconds"])
    print(f"{f}: {costs[f]} ms (cheapest test {t['seconds']}s, failed={failed})", flush=True)
common = min(costs.values())
json.dump({"common_ms": common, "file_ms": costs}, open(sys.argv[2], "w"), indent=1, sort_keys=True)
print(f"common {common} ms", flush=True)
