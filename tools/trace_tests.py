"""Trace every test in its own fresh pytest process (run inside the scorer image).

    python tools/trace_tests.py TESTS_JSON OUT_DIR JOBS

One process per test means nothing cached by an earlier test (dispatch tables, lru
caches, lazily built tries) can hide what a test depends on.
"""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

tests_json, out_dir, jobs = sys.argv[1], sys.argv[2], int(sys.argv[3])
tests = json.load(open(tests_json))["tests"]
os.makedirs(out_dir, exist_ok=True)
env = dict(os.environ, OMP_NUM_THREADS="1", SKIP_INTEGRATION="0", PYTHONPATH="/sqlglot:/proj",
           SQLGLOT_ROOT="/sqlglot")
# longest first so the tail is short
todo = sorted(tests, key=lambda t: -t["seconds"])


def one(t):
    out = os.path.join(out_dir, f"{t['number']}.json")
    if os.path.exists(out):
        return
    r = subprocess.run([sys.executable, "-m", "pytest", "-p", "tools.trace_plugin", t["id"], "-q",
                        "-p", "no:cacheprovider"], cwd="/sqlglot",
                       env=dict(env, SQLGLOT_TRACE_OUT=out + ".tmp"), capture_output=True, text=True)
    last = (r.stdout.strip().splitlines() or [""])[-1]
    if "1 passed" not in last:
        print(f"{t['id']}: {last}", flush=True)
    if os.path.exists(out + ".tmp"):
        os.replace(out + ".tmp", out)


with ThreadPoolExecutor(jobs) as ex:
    list(ex.map(one, todo))
print("traced", len(os.listdir(out_dir)), flush=True)
