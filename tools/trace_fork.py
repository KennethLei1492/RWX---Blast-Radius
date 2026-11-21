"""Trace every test in its own process, cheaply: set up once, fork once per test.

    python tools/trace_fork.py TESTS_JSON OUT_DIR JOBS     (inside the scorer image, cwd /sqlglot)

The parent imports pytest and every sqlglot module under the tracer (those lines are
import-time lines) and never runs a test. Each child is a pristine copy that collects and
runs exactly one test, so no cache built by another test can hide a dependency.
"""

import json
import os
import sys

tests_json, out_dir, jobs = sys.argv[1], sys.argv[2], int(sys.argv[3])
os.environ.update(OMP_NUM_THREADS="1", SKIP_INTEGRATION="0", SQLGLOT_ROOT="/sqlglot")
sys.path[:0] = ["/sqlglot", "/proj"]
os.chdir("/sqlglot")
os.makedirs(out_dir, exist_ok=True)

import pytest  # noqa: E402

import tools.trace_plugin as tp  # noqa: E402

tp.global_setup()
tp.mon.set_events(tp.TOOL, 0)
json.dump({"import_lines": tp._lines_to_dict(tp._import_lines)}, open(os.path.join(out_dir, "_import.json"), "w"))

tests = json.load(open(tests_json))["tests"]
todo = [t for t in sorted(tests, key=lambda t: -t["seconds"])
        if not os.path.exists(os.path.join(out_dir, f"{t['number']}.json"))]
running = {}
failed = []


def reap(block):
    pid, status = os.waitpid(-1, 0 if block else os.WNOHANG)
    if pid == 0:
        return False
    t = running.pop(pid)
    out = os.path.join(out_dir, f"{t['number']}.json")
    if os.path.exists(out + ".tmp"):
        os.replace(out + ".tmp", out)
    code = os.waitstatus_to_exitcode(status)
    if code != 0:
        failed.append((t["id"], code))
        print(f"{t['id']}: exit {code}", flush=True)
    return True


for t in todo:
    while len(running) >= jobs:
        reap(True)
    pid = os.fork()
    if pid == 0:
        try:
            out = os.path.join(out_dir, f"{t['number']}.json")
            os.environ["SQLGLOT_TRACE_OUT"] = out + ".tmp"
            tp.OUT = out + ".tmp"
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, 1)
            os.dup2(devnull, 2)
            code = pytest.main([t["id"], "-q", "-p", "no:cacheprovider", "-p", "tools.trace_plugin"])
            os._exit(int(code))
        except BaseException:
            os._exit(99)
    running[pid] = t
while running:
    reap(True)
print(f"traced {len(todo)} tests, {len(failed)} non-zero exits", flush=True)
