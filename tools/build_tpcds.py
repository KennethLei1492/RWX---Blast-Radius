"""Build maps/tpcds.bin for the planning-only check (blast/planfp.py).

  python -I -B tools/build_tpcds.py fp OUT.json        one baseline fingerprint run
  python -I -B tools/build_tpcds.py trace OUT.json     lines that run in the execution part
  python -I -B tools/build_tpcds.py qcost OUT.json     each query's share of the execution part
  python -I -B tools/build_tpcds.py qcov OUT.json      the lines each query's planning runs, alone
  python -I -B tools/build_tpcds.py qseq OUTseq.json   the same, all queries in order in one process
  QCOST=Q.json[,Q.json] QCOV=C.json python -I -B tools/build_tpcds.py merge OUT.bin TRACE.json[,...] FP.json [FP.json ...]

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


def _qcov_child(i):
    """In a fresh forked child: the lines of the tree that run while query i alone goes through
    what the probe checks (parse, the pickle round trip, optimize, plan, the executor object up
    to PythonExecutor.execute, and the DuckDB transpilation)."""
    import pickle

    from blast import verify

    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    verify._install_overlay(ROOT, {})
    import tests.test_executor as te
    from sqlglot.executor import python as py
    from sqlglot.executor.table import Table

    schema = te.TPCDS_SCHEMA
    tables = {name: Table(columns=columns, rows=[]) for name, columns in schema.items()}
    sqls = list(te.load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))
    te.initializer(schema, tables)
    py.PythonExecutor.execute = lambda self, plan, outer_scope=None: None
    mon = sys.monitoring
    tool = mon.COVERAGE_ID
    mon.use_tool_id(tool, "tpcds-query")
    seen = set()
    prefix = ROOT.rstrip("/") + "/"

    def on_line(code, line):
        f = code.co_filename
        if f.startswith(prefix):
            seen.add((f[len(prefix):], line))
        return mon.DISABLE

    mon.register_callback(tool, mon.events.LINE, on_line)
    meta, sql, _ = sqls[i]
    mon.set_events(tool, mon.events.LINE)
    try:
        expression = te.parse_one(sql)
        fn, args = pickle.loads(pickle.dumps((te.mp_execute, (expression, meta))))
        fn(*args)
        if meta.get("execute"):
            te.transpile(sql, write="duckdb")
    except Exception:  # noqa: BLE001  -- recorded as far as it ran
        pass
    finally:
        mon.set_events(tool, 0)
    files = {}
    for f, ln in seen:
        files.setdefault(f, []).append(ln)
    return {f: sorted(v) for f, v in files.items()}


def run_qcov(out):
    """Per query, the lines its planning runs (each query in its own fresh process, so that
    lazily built state is credited to every query that needs it)."""
    from blast import verify

    v = verify.Verifier(ROOT)
    v.start(heavy=True)
    n = len(list(_count_queries()))
    res = []
    for i in range(n):
        got = v._fork(lambda: _qcov_child(i), 600)
        if got is None:
            sys.exit(f"query {i}: trace failed")
        res.append(got)
    json.dump(res, open(out, "w"))
    print("queries", n, "lines per query: min", min(sum(len(x) for x in r.values()) for r in res),
          "max", max(sum(len(x) for x in r.values()) for r in res))


def run_qseq(out):
    """Per query, the lines of the tree that run in its planning and comparison when the test
    runs every query in one process, in order, with the real executor (pools inline, as when
    verifying): state left by earlier queries (caches) can take a query down paths it never
    takes alone. Lines are not recorded inside PythonExecutor.execute (maps/tpcds.bin "lines"
    covers that part)."""
    import pickle

    from blast import verify

    verify._install_overlay(ROOT, {})
    verify._inline_process_pools()
    import tests.test_executor as te
    from sqlglot.executor import python as py

    mon = sys.monitoring
    tool = mon.COVERAGE_ID
    mon.use_tool_id(tool, "tpcds-seq")
    seen = set()
    prefix = ROOT.rstrip("/") + "/"

    def on_line(code, line):
        f = code.co_filename
        if f.startswith(prefix):
            seen.add((f[len(prefix):], line))
        return mon.DISABLE

    mon.register_callback(tool, mon.events.LINE, on_line)
    real_execute = py.PythonExecutor.execute
    depth = [0]

    def execute(self, plan, outer_scope=None):
        if depth[0] == 0:
            mon.set_events(tool, 0)
        depth[0] += 1
        try:
            return real_execute(self, plan, outer_scope)
        finally:
            depth[0] -= 1
            if depth[0] == 0:
                mon.set_events(tool, mon.events.LINE)

    cls = te.TestExecutor
    cls.setUpClass()
    te.initializer(te.TPCDS_SCHEMA, cls.tpcds_tables)
    py.PythonExecutor.execute = execute
    tc = cls("test_execute_tpcds")
    res = []
    for i, (meta, sql, _) in enumerate(cls.tpcds_sqls):
        seen.clear()
        mon.restart_events()
        mon.set_events(tool, mon.events.LINE)
        try:
            expression = te.parse_one(sql)
            fn, args = pickle.loads(pickle.dumps((te.mp_execute, (expression, meta))))
            table = fn(*args)
            if table is not None:
                tc.subtestHelper(i, table, tpch=False)
        finally:
            mon.set_events(tool, 0)
        files = {}
        for f, ln in seen:
            files.setdefault(f, []).append(ln)
        res.append({f: sorted(v) for f, v in files.items()})
    json.dump(res, open(out, "w"))
    print("queries", len(res))


def run_qexec(out):
    """Per query, the lines of the tree that run in its execution part (from the entry of
    PythonExecutor.execute until its result has been compared with DuckDB's, outside the DuckDB
    transpilation), all queries in order in one process with pools inline as when verifying;
    plus, under "setup", the lines the class set-up runs."""
    import pickle

    from blast import verify

    verify._install_overlay(ROOT, {})
    verify._inline_process_pools()
    import tests.test_executor as te
    from sqlglot.executor import python as py

    mon = sys.monitoring
    tool = mon.COVERAGE_ID
    mon.use_tool_id(tool, "tpcds-exec")
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

    def grab():
        files = {}
        for f, ln in seen:
            files.setdefault(f, []).append(ln)
        seen.clear()
        return {f: sorted(v) for f, v in files.items()}

    cls = te.TestExecutor
    on()
    try:
        cls.setUpClass()
    finally:
        off()
    setup = grab()
    te.initializer(te.TPCDS_SCHEMA, cls.tpcds_tables)
    real_execute = py.PythonExecutor.execute
    real_transpile = te.transpile

    def execute(self, plan, outer_scope=None):
        on()
        try:
            return real_execute(self, plan, outer_scope)
        finally:
            off()

    def transpile(*a, **k):
        off()
        try:
            return real_transpile(*a, **k)
        finally:
            on()

    py.PythonExecutor.execute = execute
    te.transpile = transpile
    tc = cls("test_execute_tpcds")
    res = []
    for i, (meta, sql, _) in enumerate(cls.tpcds_sqls):
        mon.restart_events()
        expression = te.parse_one(sql)
        fn, args = pickle.loads(pickle.dumps((te.mp_execute, (expression, meta))))
        table = fn(*args)
        if table is not None:
            on()
            try:
                tc.subtestHelper(i, table, tpch=False)
            finally:
                off()
        res.append(grab())
    json.dump({"setup": setup, "queries": res}, open(out, "w"))
    print("queries", len(res), "lines per query: max", max(sum(len(x) for x in r.values()) for r in res))


def _count_queries():
    with open(os.path.join(ROOT, "tests", "fixtures", "optimizer", "tpc-ds", "tpc-ds.sql"), encoding="utf-8") as fh:
        text = fh.read()
    lines = [ln for ln in text.splitlines() if ln and not ln.startswith("--")]
    statements = "\n".join(lines).split(";")
    return range(0, len(statements) - 1, 2)


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
    qcov = os.environ.get("QCOV")
    if qcov:   # per-query planning lines (run_qcov, run_qseq): {file: {line: bitmask of queries}}
        qlines = {}
        alone = {}   # (query, file) -> lines it runs alone (run_qcov)
        traces = [(path, json.load(open(path))) for path in qcov.split(",")]
        for path, res in traces:
            if len(res) != len(runs[0]["plans"]):
                sys.exit(f"{path}: query count does not match the fingerprints")
            for i, files in enumerate(res):
                for f, ls in files.items():
                    d = qlines.setdefault(f, {})
                    for ln in ls:
                        d[ln] = d.get(ln, 0) | (1 << i)
                    if "seq" not in os.path.basename(path):
                        alone.setdefault((i, f), set()).update(ls)
        # lines some query runs only after others ran before it in the same process: an edit
        # there is planned for every query (which queries reach it depends on the order)
        warm = {}
        for path, res in traces:
            if "seq" in os.path.basename(path):
                for i, files in enumerate(res):
                    for f, ls in files.items():
                        extra = set(ls) - alone.get((i, f), set())
                        if extra:
                            warm.setdefault(f, set()).update(extra)
        blob["qlines"] = qlines
        blob["qwarm"] = {f: frozenset(v) for f, v in warm.items()}
        print("warm-only lines", sum(len(v) for v in warm.values()), "in", len(warm), "files")
        # each query's pinned text: a query whose text is edited is planned whatever it runs
        from blast.planfp import text_hash

        sys.path.insert(0, ROOT)
        from tests.helpers import load_sql_fixture_pairs

        qtext = [text_hash(sql) for _, sql, _ in load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql")]
        if len(qtext) != len(runs[0]["plans"]):
            sys.exit("query count of the fixture does not match the fingerprints")
        blob["base"] = dict(runs[0], qtext=qtext)
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
    elif mode == "qcov":
        run_qcov(sys.argv[2])
    elif mode == "qseq":
        run_qseq(sys.argv[2])
    elif mode == "qexec":
        run_qexec(sys.argv[2])
    else:
        merge(sys.argv[2], sys.argv[3], sys.argv[4:])
