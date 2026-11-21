"""Check maps/cmp.bin directly: swap a sample of "safe" comparison operators and run every
test that executes the swapped line. Any failure means the map claimed too much.

    host:  python3 tools/check_cmp.py prepare CMP_BIN COV_T_DIR SQLGLOT WORK N SEED
    image: python tools/check_cmp.py run WORK JOBS
    any:   python3 tools/check_cmp.py report WORK
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


def prepare(cmp_bin, cov_dir, base, work, n, seed):
    safe = marshal.loads(zlib.decompress(open(cmp_bin, "rb").read()))
    want = {(f, l) for f, lines in safe.items() for l in lines}
    cover = {}
    for p in glob.glob(os.path.join(cov_dir, "[0-9]*.json")):
        d = json.load(open(p))
        for nid, phases in d["tests"].items():
            groups = [ph["lines"] for ph in phases.values()] + [d.get("collect_lines", {})]
            for g in groups:
                for f, ls in g.items():
                    if f in safe:
                        for l in ls:
                            if (f, l) in want:
                                cover.setdefault((f, l), set()).add(nid)
    cands = []
    for (f, l), tests in cover.items():
        for col, (op, ok) in safe[f][l]:
            for alt in sorted(ok):
                cands.append((f, l, col, op, alt, sorted(tests)))
    rng = random.Random(int(seed))
    rng.shuffle(cands)
    seen_sites = set()
    picked = []
    for c in cands:
        if (c[0], c[1], c[2]) in seen_sites:
            continue
        seen_sites.add((c[0], c[1], c[2]))
        picked.append(c)
        if len(picked) >= int(n):
            break
    os.makedirs(work, exist_ok=True)
    print(f"{len(cands)} covered safe swaps; checking {len(picked)}", file=sys.stderr)
    for i, (f, l, col, op, alt, tests) in enumerate(picked):
        ws = os.path.join(work, f"c{i:03d}")
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
        json.dump({"file": f, "line": l, "col": col, "op": op, "alt": alt, "old": old,
                   "new": lines[l - 1], "tests": tests}, open(os.path.join(ws, ".check.json"), "w"))


def run(work, jobs):
    from concurrent.futures import ThreadPoolExecutor

    def one(ws):
        out = os.path.join(ws, ".result.json")
        if os.path.exists(out):
            return
        c = json.load(open(os.path.join(ws, ".check.json")))
        env = dict(os.environ, SKIP_INTEGRATION="0", PYTHONPATH=f"{ws}:/proj", OMP_NUM_THREADS="1",
                   OUTCOME_OUT=out + ".tmp", PYTHONDONTWRITEBYTECODE="1")
        subprocess.run([sys.executable, "-B", "-m", "pytest", "-p", "tools.outcome_plugin", "-q",
                        "-p", "no:cacheprovider", "--timeout", "900", *c["tests"]], cwd=ws, env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3600)
        passed = set(json.load(open(out + ".tmp"))) if os.path.exists(out + ".tmp") else set()
        failed = [t for t in c["tests"] if t not in passed]
        json.dump({"failed": failed}, open(out, "w"))
        print(f"{os.path.basename(ws)} {c['file']}:{c['line']} {c['op']}->{c['alt']} "
              f"tests={len(c['tests'])} failed={len(failed)}", flush=True)

    wss = sorted(os.path.dirname(p) for p in glob.glob(os.path.join(work, "*", ".check.json")))
    with ThreadPoolExecutor(int(jobs)) as ex:
        list(ex.map(one, wss))


def report(work):
    bad = 0
    total = 0
    for p in sorted(glob.glob(os.path.join(work, "*", ".result.json"))):
        total += 1
        r = json.load(open(p))
        if r["failed"]:
            bad += 1
            c = json.load(open(os.path.join(os.path.dirname(p), ".check.json")))
            print("WRONG:", c["file"], c["line"], c["old"].strip(), "->", c["new"].strip(), r["failed"][:5])
    print(f"{total} swaps checked, {bad} broke a test")


if __name__ == "__main__":
    {"prepare": prepare, "run": run, "report": report}[sys.argv[1]](*sys.argv[2:])
