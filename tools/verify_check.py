"""Does blast/verify.py reproduce the labels? Run it on each labeled commit's static selection.

    python tools/verify_check.py DATA_DIR [MAPS_DIR]      (inside the scorer image, /opt/sqlglot present)

DATA_DIR holds commits/*.patch, tests.json, answers.json. For every commit whose static
selection is non-empty, the verifier runs those tests (excluding process-pool and subprocess
tests, which the selector never verifies) and its failing set is compared with the label.
A test the verifier calls passing but the label says fails would be a miss.
"""

import json
import os
import resource
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from blast.select import Selector  # noqa: E402
from blast.verify import Verifier  # noqa: E402

UNVERIFIABLE_FILES = set(filter(None, os.environ.get("UNVERIFIABLE", "").split(",")))


def cpu_children():
    r = resource.getrusage(resource.RUSAGE_CHILDREN)
    return r.ru_utime + r.ru_stime


def main():
    data = sys.argv[1]
    maps = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(__file__), "..", "maps")
    sel = Selector(maps)
    tests = json.load(open(os.path.join(data, "tests.json")))["tests"]
    by_num = {t["number"]: t for t in tests}
    ans = json.load(open(os.path.join(data, "answers.json")))["commits"]
    spawn = sel.m.get("spawners", 0)
    v = Verifier()
    t0 = cpu_children()
    v.start()
    bad = 0
    checked = 0
    for cid in sorted(ans):
        patch = open(os.path.join(data, "commits", cid + ".patch"), encoding="utf-8").read()
        bits, _ = sel.select(patch)
        nums = [i for i in range(sel.n) if bits >> i & 1 and not spawn >> i & 1
                and by_num[i]["file"] not in UNVERIFIABLE_FILES]
        if not nums:
            continue
        ids = [by_num[i]["id"] for i in nums]
        c0 = cpu_children()
        w0 = time.time()
        got = v.run(patch, ids, timeout=120)
        c1 = cpu_children()
        truth = {by_num[i]["id"] for i in ans[cid]["failing"]} & set(ids)
        checked += 1
        if got is None:
            status = "UNKNOWN (falls back to static)"
        elif got == truth:
            status = "match"
        else:
            missing = truth - got
            extra = got - truth
            status = f"MISMATCH would-miss={sorted(missing)[:3]} extra={sorted(extra)[:3]}"
            if missing:
                bad += 1
        dur = sum(by_num[i]["seconds"] for i in nums)
        print(f"{cid} tests={len(ids)} recorded={dur:.3f}s cpu={c1 - c0:.3f}s wall={time.time() - w0:.2f}s "
              f"fail={len(truth)} {status}", flush=True)
    print(f"checked={checked} would-miss={bad} total child cpu={cpu_children() - t0:.2f}s")


if __name__ == "__main__":
    main()
