"""Which tests read the fixtures a test class loads in its set-up? (build time only)

    python -I -B tools/setup_trace.py TESTS_JSON OUT.json      (inside the scorer image)

For every `cls.ATTR = list(load_sql_fixture_pairs("..."))` in a setUpClass
(blast/fixtures.py finds them), each test of that class is run in its own forked child,
after the set-up, with ATTR replaced by a recording list (tools/key_trace.py's TList: any
read of it, by index, length, truth, iteration or otherwise, is recorded). OUT.json:
{"module.py::Class": {ATTR: [ids of tests that read it]}}. A test missing from a list never
read that attribute in its run; blast/fixtures.py uses this instead of line coverage, which
cannot tell `a if flag else b` apart.
"""
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
ROOT = os.environ.get("BLAST_SQLGLOT", "/opt/sqlglot")
os.environ.setdefault("SKIP_INTEGRATION", "0")
os.environ.setdefault("OMP_NUM_THREADS", "1")

spec = importlib.util.spec_from_file_location("key_trace", os.path.join(HERE, "tools", "key_trace.py"))
kt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kt)


def run_one(test_file, cls_name, method, attrs):
    """In a forked child: True/False per attribute (was it read?), or None on failure."""
    import unittest

    from blast import verify

    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    verify._install_overlay(ROOT, {})
    verify._inline_process_pools()
    mod = importlib.import_module(test_file[:-3].replace("/", "."))
    cls = getattr(mod, cls_name)
    cls.setUpClass()
    ids = {}
    for a in attrs:
        t = kt.TList(getattr(cls, a))
        t._i = len(ids) + 1000
        ids[a] = t._i
        setattr(cls, a, t)
    for store in (kt.KEYED, kt.HAS, kt.WHOLE, kt.LEN, kt.BOOLS):
        store.clear()
    result = unittest.TestResult()
    cls(method).run(result)
    seen = {i for i, *_ in kt.KEYED | kt.HAS} | set(kt.WHOLE) | {i for i, _ in kt.LEN | kt.BOOLS}
    return {a: (i in seen) for a, i in ids.items()}


def main():
    from blast import fixtures, verify

    tests = json.load(open(sys.argv[1]))["tests"]
    by_class = {}
    for t in tests:
        f, _, rest = t["id"].partition("::")
        parts = rest.split("::")
        if len(parts) == 2:
            by_class.setdefault((f, parts[0]), []).append(parts[1])
    out = {}
    v = verify.Verifier(ROOT)
    v.start(heavy=True)
    for f in sorted({f for f, _ in by_class}):
        loads, tree = fixtures._module_facts(ROOT, f)
        for cls_name in sorted({c for c, *_ in loads}):
            attrs = sorted({a for c, a, *_ in loads if c == cls_name})
            readers = {a: [] for a in attrs}
            failed = 0
            for m in by_class.get((f, cls_name), ()):
                got = v._fork(lambda: run_one(f, cls_name, m, attrs), 600)
                if got is None:
                    failed += 1
                    for a in attrs:   # unknown: count it as a reader
                        readers[a].append(f"{f}::{cls_name}::{m}")
                    continue
                for a, r in got.items():
                    if r:
                        readers[a].append(f"{f}::{cls_name}::{m}")
            out[f"{f}::{cls_name}"] = readers
            print(f, cls_name, {a: len(r) for a, r in readers.items()}, "failed runs", failed, flush=True)
    json.dump(out, open(sys.argv[2], "w"), indent=1, sort_keys=True)


if __name__ == "__main__":
    main()
