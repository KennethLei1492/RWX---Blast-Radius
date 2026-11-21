"""Label synthetic patches by running the whole suite on each (inside the scorer image).

    prepare (host):  python3 tools/label.py prepare BASE_CHECKOUT MUT_DIR WORK_DIR
    run (image):     python tools/label.py run WORK_DIR TESTS_JSON JOBS
    collect (any):   python tools/label.py collect WORK_DIR TESTS_JSON MUT_DIR

A test is failing unless pytest reported it passed with no failed phase or subtest; a
crash or a collection error therefore fails every test it covers.
"""

import glob
import json
import os
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

HEAVY = ["tests/test_parser.py", "tests/test_executor.py", "tests/test_optimizer.py",
         "tests/test_generator.py"]


def groups(tests_json):
    files = sorted({t["file"] for t in json.load(open(tests_json))["tests"]})
    rest = [f for f in files if f not in HEAVY]
    chunks = [rest[i::4] for i in range(4)]
    return [[h] for h in HEAVY] + chunks


def prepare(base, mut_dir, work):
    os.makedirs(work, exist_ok=True)
    for p in sorted(glob.glob(os.path.join(mut_dir, "commits", "*.patch"))):
        cid = os.path.basename(p)[:-6]
        ws = os.path.join(work, cid)
        if os.path.exists(os.path.join(ws, ".ready")):
            continue
        shutil.rmtree(ws, ignore_errors=True)
        subprocess.run(["cp", "-al", base, ws], check=True)
        shutil.rmtree(os.path.join(ws, ".git"), ignore_errors=True)
        text = open(p).read()
        for line in text.splitlines():
            if line.startswith("+++ b/") or line.startswith("--- a/"):
                f = os.path.join(ws, line[6:].strip())
                if os.path.isfile(f):
                    tmp = f + ".tmp"
                    shutil.copyfile(f, tmp)
                    os.replace(tmp, f)
        r = subprocess.run(["git", "apply", "--unsafe-paths", "-p1", os.path.abspath(p)], cwd=ws,
                           capture_output=True, text=True)
        if r.returncode != 0:
            print(f"{cid}: apply failed {r.stderr}", file=sys.stderr)
            shutil.rmtree(ws)
            continue
        open(os.path.join(ws, ".ready"), "w").close()


def coarse_superset(sel, patch_text):
    """File-level superset (much coarser than the selector's rules): every test that uses,
    imports or opens any changed file."""
    from blast import diffparse, pyimpact
    b = 0
    for fp in diffparse.parse(patch_text):
        for p in {fp.old_path, fp.new_path} - {None}:
            b |= sel.openers(p)
            d = p.rsplit("/", 1)[0] + "/"
            b |= sel.m["opens"].get(d, 0)
            if p.endswith(".py"):
                b |= sel.module_users(pyimpact.module_name(p))
            if p.startswith("tests/") and p.endswith(".py"):
                b |= sel.all
    return b


def run(work, tests_json, jobs, patches_dir=None):
    jobs = int(jobs)
    gs = groups(tests_json)
    tests = json.load(open(tests_json))["tests"]
    sel = None
    if patches_dir:
        sys.path.insert(0, "/proj")
        from blast.select import Selector
        sel = Selector("/proj/maps", "/sqlglot")
    todo = []
    for ws in sorted(glob.glob(os.path.join(work, "*", ".ready"))):
        ws = os.path.dirname(ws)
        sup = None
        if sel is not None:
            cid = os.path.basename(ws)
            sup = coarse_superset(sel, open(os.path.join(patches_dir, cid + ".patch")).read())
        for gi, g in enumerate(gs):
            out = os.path.join(ws, f".outcome{gi}.json")
            if os.path.exists(out):
                continue
            if sup is not None:
                ids = [t for t in tests if t["file"] in g]
                inside = [t["id"] for t in ids if (sup >> t["number"]) & 1]
                outside = [t["id"] for t in ids if not (sup >> t["number"]) & 1]
                if not inside:
                    json.dump(outside, open(out, "w"))  # cannot be affected
                    continue
                json.dump(outside, open(out + ".skip", "w"))
                todo.append((ws, gi, inside, out))
                continue
            todo.append((ws, gi, g, out))
    # interleave: at most a couple of heavy groups run at once (test_parser needs ~1.3 GB)
    todo.sort(key=lambda x: (x[0], x[1]))
    heavy_sem = threading.Semaphore(int(os.environ.get("HEAVY", "2")))
    ncpu = os.cpu_count()
    slots = list(range(jobs))
    lock = threading.Lock()

    def one(item):
        ws, gi, g, out = item
        heavy = gi < len(HEAVY) - 1
        if heavy:
            heavy_sem.acquire()
        try:
            _one(item)
        finally:
            if heavy:
                heavy_sem.release()

    def _one(item):
        ws, gi, g, out = item
        with lock:
            slot = slots.pop()
        try:
            per = max(1, ncpu // jobs)
            cpus = {(slot * per + i) % ncpu for i in range(per)}
            code = ("import os,sys; os.sched_setaffinity(0, %r); import pytest; "
                    "sys.exit(pytest.main(sys.argv[1:]))" % (cpus,))
            env = dict(os.environ, SKIP_INTEGRATION="0", PYTHONPATH=f"{ws}:/proj", OMP_NUM_THREADS="1",
                       OUTCOME_OUT=out + ".tmp")
            r = None
            try:
                r = subprocess.run([sys.executable, "-c", code, "-p", "tools.outcome_plugin", "-q",
                                "-p", "no:cacheprovider", "--timeout", "3000", *g], cwd=ws, env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3600)
            except subprocess.TimeoutExpired:
                pass
            if r is not None and r.returncode < 0:
                return  # killed: do not record anything
            if not os.path.exists(out + ".tmp"):
                json.dump([], open(out + ".tmp", "w"))
            if os.path.exists(out + ".skip"):
                passed = json.load(open(out + ".tmp")) + json.load(open(out + ".skip"))
                json.dump(passed, open(out + ".tmp", "w"))
            os.replace(out + ".tmp", out)
            print(f"done {os.path.basename(ws)} g{gi}", flush=True)
        finally:
            with lock:
                slots.append(slot)

    nheavy = int(os.environ.get("HEAVY", "2"))
    heavy_items = [t for t in todo if t[1] < len(HEAVY) - 1]
    light_items = [t for t in todo if t[1] >= len(HEAVY) - 1]
    with ThreadPoolExecutor(nheavy) as hx, ThreadPoolExecutor(max(1, jobs - nheavy)) as lx:
        fs = [hx.submit(_one, t) for t in heavy_items] + [lx.submit(_one, t) for t in light_items]
        for f in fs:
            f.result()


def collect(work, tests_json, mut_dir):
    tests = json.load(open(tests_json))["tests"]
    gs = groups(tests_json)
    answers = {}
    kinds = json.load(open(os.path.join(mut_dir, "kinds.json")))
    for ws in sorted(glob.glob(os.path.join(work, "*", ".ready"))):
        ws = os.path.dirname(ws)
        cid = os.path.basename(ws)
        passed = set()
        complete = True
        for gi in range(len(gs)):
            out = os.path.join(ws, f".outcome{gi}.json")
            if not os.path.exists(out):
                complete = False
                break
            passed.update(json.load(open(out)))
        if not complete:
            continue
        failing = sorted(t["number"] for t in tests if t["id"] not in passed)
        answers[cid] = {"failing": failing, "kind": kinds.get(cid, "?")}
    json.dump({"commits": answers}, open(os.path.join(mut_dir, "answers.json"), "w"), indent=1)
    print(f"{len(answers)} labeled", file=sys.stderr)


if __name__ == "__main__":
    {"prepare": prepare, "run": run, "collect": collect}[sys.argv[1]](*sys.argv[2:])
