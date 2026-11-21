"""Simulate the verify-or-not policy on labeled sets (no tests are run).

    python tools/policy_sim.py MAPS_DIR DATA_DIR [DATA_DIR...]

For each commit: static selection S, split into V (verifiable) and U (process-pool or
subprocess tests). Verifying costs est_cpu(V) and outputs failing(V) + U. The policy verifies
when p_benign * (pack(S) - pack(U)) > est_cpu(V).
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from blast.select import Selector  # noqa: E402
from blast.policy import decide, costs, UNVERIFIABLE_FILES  # noqa: E402


def lpt(nums, ms):
    if not nums:
        return 0
    best = None
    order = sorted(nums, key=lambda t: -ms[t])
    for k in range(1, 9):
        loads = [0] * k
        for t in order:
            i = loads.index(min(loads))
            loads[i] += ms[t]
        c = max(loads) + 2000 * k
        best = c if best is None else min(best, c)
    return best


def main():
    sel = Selector(sys.argv[1])
    table = costs(sys.argv[1])
    spawn = sel.m.get("spawners", 0)
    for factor in (0.8, 1.0, 1.5):
        tot_static = tot_policy = tot_ideal = 0
        nver = 0
        for data in sys.argv[2:]:
            tests = json.load(open(os.path.join(data, "tests.json")))["tests"]
            ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
            files = {t["number"]: t["file"] for t in tests}
            ans = json.load(open(os.path.join(data, "answers.json")))["commits"]
            for cid in sorted(ans):
                patch = open(os.path.join(data, "commits", cid + ".patch"), encoding="utf-8").read()
                bits, _ = sel.select(patch)
                S = {i for i in range(sel.n) if bits >> i & 1}
                F = set(ans[cid]["failing"])
                U = {i for i in S if spawn >> i & 1 or files[i] in UNVERIFIABLE_FILES}
                V = S - U
                static = lpt(S, ms)
                tot_static += static
                tot_ideal += lpt(F, ms)
                go, est = decide(patch, S, V, U, ms, lambda s: lpt(s, ms), files, table)
                if go:
                    nver += 1
                    actual_cpu = est * factor  # true CPU relative to the estimate
                    tot_policy += actual_cpu + lpt((F & V) | U, ms)
                else:
                    tot_policy += static
        print(f"cpu factor {factor}: static={tot_static/1000:.1f}s policy={tot_policy/1000:.1f}s "
              f"ideal={tot_ideal/1000:.1f}s verified={nver}")


if __name__ == "__main__":
    main()
