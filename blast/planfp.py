"""Planning-only check for tests/test_executor.py::TestExecutor::test_execute_tpcds.

That test is by far the dearest one a commit can select (21 s recorded), and it has two
parts: for each TPC-DS query, sqlglot parses, optimizes and plans it (cheap), then the Python
executor runs the plan over the fixture tables and the result is compared with DuckDB's
(nearly all of the time).

For a patch that only edits statements inside functions, the second part behaves exactly as
on the pinned commit when
  * none of the edited statements runs during it or while the test class loads the fixture
    tables (maps/tpcds.bin holds the lines that run in the class set-up and from the moment
    `PythonExecutor.execute` is entered until the query's result has been compared, recorded
    on the pinned commit), and
  * everything else handed to it is the same: the executor object, the plan, and the DuckDB
    SQL the expected result is computed from. (The fixture tables are then the pinned ones.)
The second point is checked here by running the first part only, on the patched tree, with
`PythonExecutor.execute` replaced by a function that fingerprints its arguments. If every
fingerprint equals the pinned one, the test cannot change its outcome and is dropped.

Fingerprints are structural (imstate.Walker) with plan-specific care so that they do not
depend on memory addresses or hash seeds: expressions are hashed top-down, a whole tree in one
pass, without their parent links (an expression held outside its tree also hashes its root and its path in it),
steps without their back links (`dependents`), which are hashed separately as an edge list;
the executor's table mapping, which the pinned code builds from a set, ignoring dict order.
"""

import hashlib
import os
import sys

from .imstate import Walker

TEST_ID = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
# The data file the probe reads as the test does (through tests.helpers.load_sql_fixture_pairs):
# an edit to it is compared like a code edit. Of each entry (meta, sql, expected) the test
# uses meta and sql only (`for args, sql, _ in sqls` in _mp_execute, `_, sql, _ = ...` in
# subtestHelper), so those are what the probe fingerprints.
PROBE_DATA = frozenset({"tests/fixtures/optimizer/tpc-ds/tpc-ds.sql"})


_PRIM = (type(None), bool, int, float, complex, str, bytes)


class PlanWalker(Walker):
    def __init__(self, line_map=None, seed=None, stable=None):
        super().__init__(line_map, 0)
        from sqlglot.executor.table import Table, Tables
        from sqlglot.expressions.core import Expr
        from sqlglot.planner import Plan, Step

        self.Expr, self.Step, self.Plan, self.Table, self.Tables = Expr, Step, Plan, Table, Tables
        self.depth = 0
        self.cmemo = {}
        self.rows = seed if seed is not None else {}
        # fingerprints of long-lived objects (the executor's function table), shared by the
        # walkers of all queries: {id: (object, fingerprint)}, the object kept alive
        self.stable = stable if stable is not None else {}

    def fp(self, o):
        got = self.stable.get(id(o))
        if got is not None and got[0] is o:
            return got[1]
        if self.depth == 0 and isinstance(o, self.Expr) and getattr(o, "parent", None) is not None:
            i = id(o)
            got = self.cmemo.get(i)
            if got is None:
                path = []
                n = o
                while getattr(n, "parent", None) is not None:
                    path.append(f"{getattr(n, 'arg_key', None)}:{getattr(n, 'index', None)}")
                    n = n.parent
                got = self.cmemo[i] = self._h("XC", Walker.fp(self, o), Walker.fp(self, n), *path)
            return got
        return Walker.fp(self, o)

    def _fp(self, o, t):
        if isinstance(o, self.Expr):
            self.depth += 1
            try:
                out = []
                self._ser(o, out)
                return self._h("X", "\x00".join(out))
            finally:
                self.depth -= 1
        if isinstance(o, self.Step):
            parts = ["Step", t.__qualname__]
            d = o.__dict__
            for k in sorted(d):
                if k != "dependents":
                    parts += [k, self.fp(d[k])]
            return self._h(*parts)
        if isinstance(o, self.Plan):
            parts = ["Plan"]
            d = o.__dict__
            for k in sorted(d):
                if k != "_dag":
                    parts += [k, self.fp(d[k])]
            # every step reachable either way, with its back links and the cached dag
            steps = {}
            todo = [o.root]
            for node, deps in d.get("_dag", {}).items():
                todo.append(node)
                todo.extend(deps)
            while todo:
                s = todo.pop()
                if id(s) in steps or not isinstance(s, self.Step):
                    continue
                steps[id(s)] = s
                todo.extend(s.dependencies)
                todo.extend(s.dependents)
            parts.append(self._h(*sorted(
                self._h(self.fp(s), *sorted(self.fp(x) for x in s.dependents)) for s in steps.values())))
            parts.append(self._h(*sorted(
                self._h(self.fp(node), *sorted(self.fp(x) for x in deps))
                for node, deps in d.get("_dag", {}).items())))
            return self._h(*parts)
        if isinstance(o, self.Table):
            rows = o.rows
            r = self.rows.get(id(rows))
            if r is None:
                r = _rows_fp(rows)
            parts = ["Tbl", t.__qualname__, self.fp(o.columns), self.fp(o.column_range), r]
            return self._h(*parts)
        if isinstance(o, self.Tables):
            # built from a set of table names: the order of its dicts varies from process to
            # process on the pinned commit, so it cannot matter
            mode, self.mode = self.mode, 1
            try:
                return Walker._fp(self, o, t)
            finally:
                self.mode = mode
        return Walker._fp(self, o, t)


    def _ser(self, o, out):
        """One expression tree as a flat token list: the same content the generic walk hashes
        (class, args in order, comments, type, meta, any instance dict), in one pass. Tokens
        hold no NUL (repr escapes it) and every container records its length, so the joined
        text identifies the tree."""
        ap = out.append
        t = type(o)
        if t in _PRIM:
            ap(t.__name__)
            ap(repr(o))
        elif isinstance(o, self.Expr):
            ap("X")
            ap(t.__module__)
            ap(t.__qualname__)
            args = o.args
            ap(str(len(args)))
            for k, v in args.items():
                self._ser(k, out)
                self._ser(v, out)
            for slot in ("comments", "_type", "_meta"):
                self._ser(getattr(o, slot, None), out)
            d = getattr(o, "__dict__", None)
            if d:
                ap("I")
                ap(str(len(d)))
                for k in sorted(d):
                    ap(k)
                    self._ser(d[k], out)
        elif t is list or t is tuple:
            ap("L" if t is list else "T")
            ap(str(len(o)))
            for x in o:
                self._ser(x, out)
        elif t is dict:
            ap("D")
            ap(str(len(o)))
            for k, v in o.items():
                self._ser(k, out)
                self._ser(v, out)
        else:
            ap("W")
            ap(Walker.fp(self, o).hex())


def _rows_fp(rows):
    return hashlib.blake2b(repr(rows).encode("utf-8", "surrogatepass"), digest_size=8).digest()


def text_hash(text):
    """A query's text as stored in maps/tpcds.bin ("qtext")."""
    return hashlib.blake2b(text.encode("utf-8", "surrogatepass"), digest_size=8).hexdigest()


def _watch_edited(edited):
    """Start watching for edited code: {file: set of new line numbers}. Returns (hits, on, off):
    while on, the qualified name of every code object that starts and holds an edited line is
    appended to hits."""
    mon = sys.monitoring
    tool = mon.PROFILER_ID
    hits = []

    def start(code, offset):
        lines = edited.get(code.co_filename)
        if lines:
            # anywhere from the def line (and decorators) to the last line of the body: a
            # default on a continuation line of the header belongs to no instruction
            hi = max((ln for _, _, ln in code.co_lines() if ln), default=code.co_firstlineno)
            if any(code.co_firstlineno <= ln <= hi for ln in lines):
                hits.append(code.co_qualname)
        return mon.DISABLE

    mon.use_tool_id(tool, "blast-edited")
    mon.register_callback(tool, mon.events.PY_START, start)
    return hits, (lambda: mon.set_events(tool, mon.events.PY_START)), (lambda: mon.set_events(tool, 0))


_HEAVY = ("duckdb", "numpy", "pandas", "pandas.testing")


def _lazy_heavy():
    """tests/test_executor.py imports duckdb, numpy and pandas at module level (about two
    seconds of CPU), but planning never touches them: only loading the fixture tables and
    comparing results with DuckDB's do. Stand-ins take their place while the test module is
    imported; any later attribute read swaps in the real modules (all of them at once, so that
    pandas finds the real numpy). Returns a function that ends the import phase, or None when
    the real modules are already loaded."""
    import importlib
    import types

    if any(m in sys.modules for m in _HEAVY):
        return None
    importing = [True]
    stubs = {}

    def real(name):
        for m in _HEAVY:
            if m in stubs and sys.modules.get(m) is stubs[m]:
                del sys.modules[m]
        return importlib.import_module(name)

    class Lazy(types.ModuleType):
        def __getattr__(self, attr):
            if attr.startswith("__"):
                raise AttributeError(attr)
            name = self.__name__
            if importing[0]:
                # `from pandas.testing import assert_frame_equal` while the module loads
                def call(*a, **k):
                    return getattr(real(name), attr)(*a, **k)
                return call
            return getattr(real(name), attr)

    for m in _HEAVY:
        stubs[m] = sys.modules[m] = Lazy(m)
    stubs["pandas"].__dict__["testing"] = stubs["pandas.testing"]

    def done():
        importing[0] = False
    return done


def probe(root, files, line_maps, baseline=None, collect=0, run_limit=0.0, qshare=None,
          test_seconds=0.0, edited=None, only=None, force=None):
    """Runs in a forked child with nothing of sqlglot imported yet. Without a baseline:
    {"setup": hex, "plans": [hex or "-" per query], "sql": [hex or "-" per query]}.
    With one: {"same": bool, "at": where it first differs}.

    With `collect` > 0, planning goes on past a differing query, up to `collect` of them and
    while running them is estimated (`qshare` x `test_seconds`) to cost at most `run_limit`
    seconds; the differing queries are then run for real on the fixture tables and compared
    with DuckDB's results, as the test does. Every other query has the pinned plan and runs
    only pinned code on the pinned tables (no executor state outlives a query except a dispatch
    cache keyed by class), so it passes as it does on the pinned commit; the test then passes
    exactly when the differing queries do. Should an edited function (`edited`: {file: new
    line numbers}) start while those queries execute, the answer is "differs": that code never
    ran in the pinned execution part, so nothing is known about what it leaves behind.

    With `only` (a bitmask of queries, from the per-query planning lines of maps/tpcds.bin),
    the other queries are not planned: on the pinned commit no edited statement ran while any
    of them was parsed, planned or transpiled (each traced alone in a fresh process after the
    imports, so lazily built state is credited to every query that needs it, and all in order
    in one process; edits to lines that only run after other queries are never gated), the
    caller has established that the imports leave the pinned state, and their text is the
    pinned text; so they reach the executor exactly as on the pinned commit. Their pinned
    fingerprints stand.

    With `force` (a bitmask), those queries are run for real as well, whatever their plans:
    on the pinned commit their execution ran an edited statement (and no other query's did),
    so the edited code is expected to run while they execute."""
    import pickle

    from . import verify

    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    sys.stdout = open(os.devnull, "w")
    sys.stderr = sys.stdout
    verify._install_overlay(root, files)
    done = _lazy_heavy()
    import tests.test_executor as te
    if done is not None:
        done()
    from sqlglot.executor import env as executor_env
    from sqlglot.executor import python as py
    from sqlglot.executor.table import Table

    # The fixture tables are not loaded: planning never reads their rows (the schema is given),
    # and the caller only comes here when no edited function runs while the test class loads
    # them (those lines are part of maps/tpcds.bin), so they hold what they hold on the pinned
    # commit. Each stands here as an empty table with its columns, identified by name.
    schema = te.TPCDS_SCHEMA
    tables = {name: Table(columns=columns, rows=[]) for name, columns in schema.items()}
    sqls = list(te.load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))
    seed = {id(tables[name].rows): ("fixture table " + name).encode() for name in tables}
    te.initializer(schema, tables)
    got = []
    stable = {}
    w0 = PlanWalker(line_maps, seed)
    for v in executor_env.ENV.values():
        stable[id(v)] = (v, w0.fp(v))

    def stub(self, plan, outer_scope=None):
        got.append(PlanWalker(line_maps, seed, stable).fp((plan, outer_scope, self.__dict__)).hex())
        return None

    # Each query's text is compared per query below (it is parsed for every query, and planned
    # and handed to DuckDB for those that execute): the set-up holds what applies to all.
    setup = PlanWalker(line_maps, seed).fp((tables, schema, [meta for meta, _, _ in sqls])).hex()
    if baseline is not None and setup != baseline["setup"]:
        return {"same": False, "at": "setup"}

    real_execute = py.PythonExecutor.execute
    py.PythonExecutor.execute = stub
    plans = []
    differ = []   # with collect: the queries whose plan or DuckDB SQL differs
    want = baseline["plans"] if baseline is not None else None
    if want is not None and len(want) != len(sqls):
        return {"same": False, "at": "count"}
    budget = {"cost": 0.0}
    qtext = baseline.get("qtext") if baseline is not None else None
    if only is not None and (want is None or qtext is None or len(qtext) != len(sqls)):
        only = None
    skip = set()
    if only is not None:
        skip = {i for i, (_, sql, _) in enumerate(sqls) if not only >> i & 1 and text_hash(sql) == qtext[i]}

    def too_many(i, cur):
        """Record query i as differing; True when running the differing ones is out of reach."""
        differ.append(i)
        if not collect or len(differ) > collect or cur.startswith("error:"):
            return True   # an error fails the test outright
        share = qshare[i] if qshare and i < len(qshare) else 1.0
        budget["cost"] += share * test_seconds
        return budget["cost"] > run_limit

    for i, (meta, sql, _) in enumerate(sqls):
        if i in skip:
            plans.append(want[i])
            continue
        n = len(got)
        try:
            expression = te.parse_one(sql)
            fn, args = pickle.loads(pickle.dumps((te.mp_execute, (expression, meta))))
            fn(*args)
            cur = "-" if len(got) == n else ",".join(got[n:])
        except Exception as e:  # noqa: BLE001
            cur = f"error:{type(e).__name__}"
        plans.append(cur)
        if want is not None and cur != want[i] and too_many(i, cur):
            return {"same": False, "at": i}
    out_sql = []
    wsql = baseline["sql"] if baseline is not None else None
    for i, (meta, sql, _) in enumerate(sqls):
        if i in skip:
            out_sql.append(wsql[i])
            continue
        if plans[i] == "-":
            cur = "-"
        else:
            try:
                text = te.transpile(sql, write="duckdb")[0]
                cur = hashlib.blake2b(text.encode("utf-8", "surrogatepass"), digest_size=8).hexdigest()
            except Exception as e:  # noqa: BLE001
                cur = f"error:{type(e).__name__}"
        out_sql.append(cur)
        if wsql is not None and cur != wsql[i] and i not in differ and too_many(i, cur):
            return {"same": False, "at": f"sql {i}"}
    if baseline is None:
        return {"setup": setup, "plans": plans, "sql": out_sql}
    forced = {i for i in range(len(sqls)) if force and force >> i & 1}
    if not differ and not forced:
        return {"same": True, "at": None}

    # Run the differing queries as the test does: their plans on the real fixture tables,
    # each result compared with DuckDB's for the same query.
    py.PythonExecutor.execute = real_execute
    cls = te.TestExecutor
    cls.setUpClass()
    te.initializer(te.TPCDS_SCHEMA, cls.tpcds_tables)
    hits, watch_on, watch_off = _watch_edited(edited or {})
    depth = [0]

    def watched(self, plan, outer_scope=None):
        if depth[0] == 0:
            watch_on()
        depth[0] += 1
        try:
            return real_execute(self, plan, outer_scope)
        finally:
            depth[0] -= 1
            if depth[0] == 0:
                watch_off()

    py.PythonExecutor.execute = watched
    tc = cls("test_execute_tpcds")
    differ = sorted(set(differ) | forced)
    for i in differ:
        meta, sql, _ = cls.tpcds_sqls[i]
        try:
            expression = te.parse_one(sql)
            fn, args = pickle.loads(pickle.dumps((te.mp_execute, (expression, meta))))
            table = fn(*args)
            if table is not None:
                tc.subtestHelper(i, table, tpch=False)
        except BaseException:  # noqa: BLE001  -- the test fails
            return {"same": False, "at": i, "ran": sorted(differ)}
        if i in forced:
            hits.clear()   # expected: the pinned execution of this query ran the edited code too
        elif hits:
            return {"same": False, "at": f"edited code ran while executing query {i}"}
    return {"same": True, "at": None, "ran": sorted(differ)}
