"""Check maps/cmpt.bin against reality: apply random comparison swaps at sites tests reach,
run every test that executes the swapped line, and confirm each failing test is one the map
says can see the swap.

    host:  python3 tools/check_cmpt.py prepare CMPT_BIN COV_T_DIR TESTS_JSON SQLGLOT WORK N SEED
    image: python tools/check_cmpt.py run WORK JOBS
    any:   python3 tools/check_cmpt.py report WORK

Sites are drawn mostly where the map prunes (it selects fewer tests than run the line),
since that is where a wrong claim would cause a miss.
"""

import glob
import json
import marshal
import os
import random
import shutil
import subprocess
import sys
import zlib

SKIP = {"tests/test_parser.py::TestParser::test_max_nodes"}  # unprofiled: always selected


def prepare(cmpt_bin, cov_dir, tests_json, base, work, n, seed, baseline=None):
    table = marshal.loads(zlib.decompress(open(cmpt_bin, "rb").read()))
    base_t = marshal.loads(zlib.decompress(open(baseline, "rb").read())) if baseline else None
    tests = json.load(open(tests_json))["tests"]
    ids = {t["number"]: t["id"] for t in tests}
    cover = {}
    for t in tests:
        p = os.path.join(cov_dir, f"{t['number']}.json")
        if not os.path.exists(p):
            continue
        d = json.load(open(p))
        groups = [ph["lines"] for phases in d.get("tests", {}).values() for ph in phases.values()]
        groups.append(d.get("collect_lines", {}))
        for g in groups:
            for f, ls in g.items():
                if f in table:
                    for l in ls:
                        if l in table[f]:
                            cover.setdefault((f, l), set()).add(t["number"])
    only_ops = set(filter(None, os.environ.get("ONLY_OPS", "").split(",")))
    prune, keep = [], []
    for (f, l), nums in cover.items():
        for col, op, alts in table[f][l]:
            if only_ops and op not in only_ops:
                continue
            for alt, bits in alts.items():
                sel = {i for i in nums if bits >> i & 1}
                if base_t is not None and l in base_t.get(f, {}):
                    # only swaps the new map prunes further than the baseline map did
                    old_bits = next((a[alt] for c2, o2, a in base_t[f][l] if c2 == col), bits)
                    if {i for i in nums if old_bits >> i & 1} <= sel:
                        continue
                c = (f, l, col, op, alt, sorted(nums), sorted(sel))
                (prune if len(sel) < len(nums) else keep).append(c)
    rng = random.Random(int(seed))
    rng.shuffle(prune)
    rng.shuffle(keep)
    n = int(n)
    picked, seen = [], set()
    for c in prune + keep:
        if (c[0], c[1], c[2]) in seen:
            continue
        seen.add((c[0], c[1], c[2]))
        picked.append(c)
        if len(picked) >= n:
            break
    os.makedirs(work, exist_ok=True)
    print(f"{len(prune)} pruning swaps, {len(keep)} non-pruning; checking {len(picked)}", file=sys.stderr)
    for i, (f, l, col, op, alt, nums, sel) in enumerate(picked):
        ws = os.path.join(work, f"k{i:03d}")
        shutil.rmtree(ws, ignore_errors=True)
        subprocess.run(["cp", "-al", base, ws], check=True)
        shutil.rmtree(os.path.join(ws, ".git"), ignore_errors=True)
        path = os.path.join(ws, f)
        lines = open(path, encoding="utf-8").read().split("\n")
        old = lines[l - 1]
        assert old.startswith(op, col), (f, l, col, op, old)
        lines[l - 1] = old[:col] + alt + old[col + len(op):]
        os.remove(path)
        open(path, "w", encoding="utf-8").write("\n".join(lines))
        run_ids = [ids[x] for x in nums if ids[x] not in SKIP]
        json.dump({"file": f, "line": l, "col": col, "op": op, "alt": alt, "old": old,
                   "new": lines[l - 1], "run": run_ids, "selected": [ids[x] for x in sel]},
                  open(os.path.join(ws, ".check.json"), "w"))


def run(work, jobs):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    jobs = int(jobs)
    slots = list(range(jobs))
    lock = threading.Lock()
    ncpu = os.cpu_count()

    def one(ws):
        out = os.path.join(ws, ".result.json")
        if os.path.exists(out):
            return
        c = json.load(open(os.path.join(ws, ".check.json")))
        if not c["run"]:
            json.dump({"failed": []}, open(out, "w"))
            return
        with lock:
            slot = slots.pop()
        try:
            cpus = {(slot * 2) % ncpu, (slot * 2 + 1) % ncpu}
            code = ("import os,sys; os.sched_setaffinity(0, %r); import pytest; "
                    "sys.exit(pytest.main(sys.argv[1:]))" % (cpus,))
            env = dict(os.environ, SKIP_INTEGRATION="0", PYTHONPATH=f"{ws}:/proj", OMP_NUM_THREADS="1",
                       OUTCOME_OUT=out + ".tmp", PYTHONDONTWRITEBYTECODE="1")
            r = subprocess.run([sys.executable, "-B", "-c", code, "-p", "tools.outcome_plugin", "-q",
                                "-p", "no:cacheprovider", "--timeout", "1200", *c["run"]], cwd=ws, env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3600)
            if r.returncode < 0 or not os.path.exists(out + ".tmp"):
                print(f"{os.path.basename(ws)}: runner died ({r.returncode})", flush=True)
                return
            passed = set(json.load(open(out + ".tmp")))
            failed = [t for t in c["run"] if t not in passed]
            json.dump({"failed": failed}, open(out, "w"))
            print(f"{os.path.basename(ws)} {c['file']}:{c['line']} {c['op']}->{c['alt']} "
                  f"run={len(c['run'])} selected={len(c['selected'])} failed={len(failed)}", flush=True)
        finally:
            with lock:
                slots.append(slot)

    wss = sorted(os.path.dirname(p) for p in glob.glob(os.path.join(work, "*", ".check.json")))
    with ThreadPoolExecutor(jobs) as ex:
        list(ex.map(one, wss))


def report(work):
    bad = total = pruned_tests = 0
    for p in sorted(glob.glob(os.path.join(work, "*", ".result.json"))):
        total += 1
        r = json.load(open(p))
        c = json.load(open(os.path.join(os.path.dirname(p), ".check.json")))
        sel = set(c["selected"])
        pruned_tests += len(set(c["run"]) - sel)
        wrong = [t for t in r["failed"] if t not in sel]
        if wrong:
            bad += 1
            print("MISS:", c["file"], c["line"], c["old"].strip(), "->", c["new"].strip(), wrong[:5])
    print(f"{total} swaps checked, {pruned_tests} test runs pruned by the map, {bad} would have missed")


if __name__ == "__main__":
    {"prepare": prepare, "run": run, "report": report}[sys.argv[1]](*sys.argv[2:])
