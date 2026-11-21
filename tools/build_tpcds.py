"""Build maps/tpcds.bin for the planning-only check (blast/planfp.py).

  python -I -B tools/build_tpcds.py fp OUT.json        one baseline fingerprint run
  python -I -B tools/build_tpcds.py trace OUT.json     lines that run in the execution part
  python -I -B tools/build_tpcds.py qcost OUT.json     each query's share of the execution part
  QCOST=Q.json[,Q.json] python -I -B tools/build_tpcds.py merge OUT.bin TRACE.json[,...] FP.json [FP.json ...]

`fp` is run several times under different PYTHONHASHSEED values; `merge` refuses to write
anything unless all runs agree, so a fingerprint that depends on the process is never stored.
Run inside the scorer's image, where the pinned tree is /opt/sqlglot.
"""
import json
import marshal
import os
import sys
import zlib

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
ROOT = os.environ.get("BLAST_SQLGLOT", "/opt/sqlglot")
os.environ.setdefault("SKIP_INTEGRATION", "0")
os.environ.setdefault("OMP_NUM_THREADS", "1")


def run_fp(out):
    from blast import planfp, verify

    v = verify.Verifier(ROOT)
    v.start(heavy=True)
    got = v._fork(lambda: planfp.probe(ROOT, {}, {}), 600)
    json.dump(got, open(out, "w"))
    print("queries", len(got["plans"]), "executed", sum(1 for p in got["plans"] if p != "-"))


def run_trace(out):
    """Run the real test in this process (pools inline, as when verifying) and record every
    line of the tree that runs in the class set-up, from the entry of PythonExecutor.execute
    until the next query is parsed, and inside subtestHelper except while transpiling the
    query for DuckDB."""
    from blast import verify

    verify._install_overlay(ROOT, {})
    verify._inline_process_pools()
    import tests.test_executor as te
    from sqlglot.executor import python as py

    mon = sys.monitoring
    tool = mon.COVERAGE_ID
    mon.use_tool_id(tool, "tpcds-phase")
    seen = set()
    prefix = ROOT.rstrip("/") + "/"

    def on_line(code, line):
        f = code.co_filename
        if f.startswith(prefix):
            seen.add((f[len(prefix):], line))
        return mon.DISABLE

    mon.register_callback(tool, mon.events.LINE, on_line)

    def on():
        mon.set_events(tool, mon.events.LINE)

    def off():
        mon.set_events(tool, 0)

    real_execute = py.PythonExecutor.execute
    real_parse = te.parse_one
    real_transpile = te.transpile
    real_helper = te.TestExecutor.subtestHelper
    calls = [0]

    def execute(self, plan, outer_scope=None):
        calls[0] += 1
        on()
        return real_execute(self, plan, outer_scope)

    def parse_one(*a, **k):
        off()
        return real_parse(*a, **k)

    def transpile(*a, **k):
        off()
        try:
            return real_transpile(*a, **k)
        finally:
            on()

    def helper(self, *a, **k):
        on()
        return real_helper(self, *a, **k)

    cls = te.TestExecutor
    on()   # the class set-up loads the fixture tables: the probe skips it, so its lines count
    try:
        cls.setUpClass()
    finally:
        off()
    py.PythonExecutor.execute = execute
    te.parse_one = parse_one
    te.transpile = transpile
    cls.subtestHelper = helper
    tc = cls("test_execute_tpcds")
    try:
        tc._mp_execute(te.TPCDS_SCHEMA, cls.tpcds_tables, cls.tpcds_sqls, False)
    finally:
        off()
    files = {}
    for f, l in seen:
        files.setdefault(f, []).append(l)
    json.dump({f: sorted(ls) for f, ls in sorted(files.items())}, open(out, "w"))
    print("execute calls", calls[0], "files", len(files), "lines", len(seen))


def run_qcost(out):
    """Each query's share of the execution part: CPU of running its plan and comparing the
    result with DuckDB's, as fractions of the sum (the selector scales them by the test's
    recorded seconds to budget running only the queries whose plans changed)."""
    import pickle
    import time

    from blast import verify

    verify._install_overlay(ROOT, {})
    import tests.test_executor as te

    cls = te.TestExecutor
    cls.setUpClass()
    te.initializer(te.TPCDS_SCHEMA, cls.tpcds_tables)
    tc = cls("test_execute_tpcds")
    cost = []
    for i, (meta, sql, _) in enumerate(cls.tpcds_sqls):
        expression = te.parse_one(sql)
        fn, args = pickle.loads(pickle.dumps((te.mp_execute, (expression, meta))))
        t = time.process_time()
        table = fn(*args)
        if table is not None:
            tc.subtestHelper(i, table, tpch=False)
        cost.append(time.process_time() - t)
    total = sum(cost)
    json.dump([c / total for c in cost], open(out, "w"))
    print("queries", len(cost), "total cpu %.1fs" % total, "largest share %.3f" % max(c / total for c in cost))


def merge(out, trace, fps):
    runs = [json.load(open(p)) for p in fps]
    for r in runs[1:]:
        if r != runs[0]:
            bad = [i for i, (a, b) in enumerate(zip(r["plans"], runs[0]["plans"])) if a != b]
            sys.exit(f"fingerprints differ between runs: setup {r['setup'] == runs[0]['setup']}, "
                     f"plans {bad[:10]}, sql {r['sql'] == runs[0]['sql']}")
    lines = {}
    for path in trace.split(","):   # the union of several traced runs
        for f, ls in json.load(open(path)).items():
            lines.setdefault(f, set()).update(ls)
    blob = {"base": runs[0], "lines": {f: frozenset(ls) for f, ls in lines.items()}}
    qcost = os.environ.get("QCOST")
    if qcost:   # per-query shares of the execution part (run_qcost), averaged over runs
        runs_q = [json.load(open(p)) for p in qcost.split(",")]
        blob["qshare"] = [max(q[i] for q in runs_q) for i in range(len(runs_q[0]))]
    with open(out, "wb") as fh:
        fh.write(zlib.compress(marshal.dumps(blob), 9))
    print("wrote", out, os.path.getsize(out), "bytes;", len(runs), "agreeing runs;",
          sum(len(v) for v in lines.values()), "lines in", len(lines), "files")


if __name__ == "__main__":
    mode = sys.argv[1]
    if mode == "fp":
        run_fp(sys.argv[2])
    elif mode == "trace":
        run_trace(sys.argv[2])
    elif mode == "qcost":
        run_qcost(sys.argv[2])
    else:
        merge(sys.argv[2], sys.argv[3], sys.argv[4:])
