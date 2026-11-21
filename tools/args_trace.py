"""Which tests validate an expression with a given argument missing? (build time only)

    python tools/args_trace.py TESTS_JSON OUT_DIR JOBS     (inside the scorer image)

sqlglot expression classes declare `arg_types = {"this": True, "expression": False, ...}`.
The True/False values are read in exactly one place: when the class is created they become
`cls.required_args`, and `error_messages()` reports each required argument that is missing
(None or an empty list). So flipping one value for key k in class C can only change a test
that calls error_messages() on an instance whose arg_types is C's dict while k is missing.

This tracer wraps error_messages and records, per test, every (class that owns arg_types, k)
seen missing. One forked child per test, as in the other tracers; validations that happen
while importing sqlglot are kept separately.
"""

import importlib
import json
import os
import sys

ROOT = "/opt/sqlglot/"
MISSING: set = set()
_info: dict = {}


def _key(cls):
    return f"{cls.__module__}:{cls.__qualname__}"


def _wrap(orig):
    def error_messages(self, args=None):
        t = type(self)
        info = _info.get(t)
        if info is None:
            owner = next((k for k in t.__mro__ if "arg_types" in k.__dict__), None)
            info = (_key(owner) if owner is not None else "", tuple(t.arg_types))
            _info[t] = info
        a = self.args
        owner = info[0]
        for k in info[1]:
            v = a.get(k)
            if v is None or (isinstance(v, list) and not v):
                MISSING.add((owner, k))
        return orig(self, args)

    return error_messages


def main():
    global MISSING
    tests_json, out_dir, jobs = sys.argv[1], sys.argv[2], int(sys.argv[3])
    os.environ.update(OMP_NUM_THREADS="1", SKIP_INTEGRATION="0", PYTHONDONTWRITEBYTECODE="1")
    sys.dont_write_bytecode = True
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path[:0] = [ROOT.rstrip("/"), here]
    os.chdir(ROOT)
    os.makedirs(out_dir, exist_ok=True)

    import pkgutil

    import pytest

    import sqlglot
    import sqlglot.expressions.core as core
    import tools.trace_plugin as tp  # for the inline process pool only

    # wrap before anything else is imported so import-time validations are seen too
    wrapped = 0
    for obj in vars(core).values():
        if isinstance(obj, type) and "error_messages" in obj.__dict__ and "arg_types" in dir(obj):
            setattr(obj, "error_messages", _wrap(obj.__dict__["error_messages"]))
            wrapped += 1
    tp._inline_process_pools()
    for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
        if info.name != "sqlglot.__main__":
            try:
                importlib.import_module(info.name)
            except Exception as e:
                print(f"cannot import {info.name}: {e}", file=sys.stderr)
    # any other class that overrides error_messages
    seen = set()
    for name, module in list(sys.modules.items()):
        if not name.startswith("sqlglot") or module is None:
            continue
        for obj in list(vars(module).values()):
            if isinstance(obj, type) and obj not in seen and obj.__module__.startswith("sqlglot"):
                seen.add(obj)
                f = obj.__dict__.get("error_messages")
                if f is not None and getattr(f, "__name__", "") == "error_messages" and \
                        getattr(f, "__closure__", None) is None:
                    setattr(obj, "error_messages", _wrap(f))
                    wrapped += 1
    json.dump({"missing": sorted(MISSING)}, open(os.path.join(out_dir, "_import.json"), "w"))
    print(f"wrapped {wrapped} error_messages; {len(MISSING)} (class, arg) missing at import", flush=True)

    tests = json.load(open(tests_json))["tests"]
    todo = [t for t in sorted(tests, key=lambda t: -t["seconds"])
            if not os.path.exists(os.path.join(out_dir, f"{t['number']}.json"))]
    running = {}
    bad = 0

    def reap():
        nonlocal bad
        pid, status = os.waitpid(-1, 0)
        tt = running.pop(pid)
        code = os.waitstatus_to_exitcode(status)
        if code != 0:
            bad += 1
            print(f"{tt['id']}: exit {code}", flush=True)

    for t in todo:
        while len(running) >= jobs:
            reap()
        pid = os.fork()
        if pid == 0:
            try:
                import signal

                signal.alarm(int(os.environ.get("ARGS_TIMEOUT", "3000")))
                devnull = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull, 1)
                os.dup2(devnull, 2)
                MISSING = set()
                globals()["MISSING"] = MISSING
                code = pytest.main([t["id"], "-q", "-p", "no:cacheprovider"])
                if int(code) == 0:
                    out = os.path.join(out_dir, f"{t['number']}.json")
                    with open(out + ".tmp", "w") as fh:
                        json.dump({"missing": sorted(MISSING)}, fh)
                    os.replace(out + ".tmp", out)
                os._exit(int(code))
            except BaseException:
                os._exit(99)
        running[pid] = t
    while running:
        reap()
    print(f"args-traced {len(todo)} tests, {bad} non-zero exits", flush=True)


if __name__ == "__main__":
    main()
