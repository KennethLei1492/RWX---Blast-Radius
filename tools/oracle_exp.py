"""Where does the selector overspend? Measure on many generated patches (run in the image).

    python tools/oracle_exp.py PATCH_DIR TESTS_JSON OUT_DIR SHARD NSHARDS

For each patch: the static selection S, then blast/verify.py runs S on the patched tree
(the verifier reproduced every pytest label it was checked against) to learn which of S
really fail. Writes one JSON per patch: kinds of impact, |S|, cost of printing S, cost of
printing only the failing tests, CPU of the run. Tests outside S are assumed to pass, so
this measures overspend, not misses.
"""

import glob
import json
import os
import resource
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from blast.select import Selector  # noqa: E402
from blast.verify import Verifier  # noqa: E402


def cpu():
    r = resource.getrusage(resource.RUSAGE_CHILDREN)
    return r.ru_utime + r.ru_stime


def main():
    pdir, tests_json, out, shard, n = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), int(sys.argv[5])
    os.makedirs(out, exist_ok=True)
    sel = Selector(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "maps"))
    tests = json.load(open(tests_json))["tests"]
    by = {t["number"]: t for t in tests}
    spawn = sel.m.get("spawners", 0)
    v = Verifier()
    v.start(heavy=True)
    paths = sorted(glob.glob(os.path.join(pdir, "*.patch")))
    for i, p in enumerate(paths):
        if i % n != shard:
            continue
        cid = os.path.basename(p)[:-6]
        dst = os.path.join(out, cid + ".json")
        if os.path.exists(dst):
            continue
        patch = open(p, encoding="utf-8").read()
        try:
            bits, notes = sel.select(patch)
        except Exception as e:
            json.dump({"error": repr(e)}, open(dst, "w"))
            continue
        S = [i for i in range(sel.n) if bits >> i & 1]
        V = [i for i in S if not spawn >> i & 1]
        rec = {"notes": notes, "sel": len(S), "static_ms": sel.cost(sel.pack(bits)),
               "run_ms": sum(sel.ms[i] for i in S)}
        if V:
            c0 = cpu()
            failed = v.run(patch, [by[i]["id"] for i in V], timeout=float(os.environ.get("ORACLE_TIMEOUT", "500")))
            rec["cpu"] = round(cpu() - c0, 3)
            if failed is None:
                rec["unknown"] = True
            else:
                num = {by[i]["id"]: i for i in V}
                F = sorted(num[x] for x in failed if x in num)
                fb = 0
                for f in F:
                    fb |= 1 << f
                # subprocess tests in S cannot be run here: keep them (as the selector would)
                for i in S:
                    if spawn >> i & 1:
                        fb |= 1 << i
                rec["failing"] = F
                rec["ideal_ms"] = sel.cost(sel.pack(fb))
        else:
            rec["failing"] = []
            rec["ideal_ms"] = sel.cost(sel.pack(bits))
        json.dump(rec, open(dst + ".tmp", "w"))
        os.replace(dst + ".tmp", dst)
        print(cid, rec.get("sel"), rec.get("static_ms"), rec.get("ideal_ms"), rec.get("cpu"), flush=True)


if __name__ == "__main__":
    main()
