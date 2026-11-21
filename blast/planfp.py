"""Planning-only check for tests/test_executor.py::TestExecutor::test_execute_tpcds.

That test is by far the dearest one a commit can select (21 s recorded), and it has two
parts: for each TPC-DS query, sqlglot parses, optimizes and plans it (cheap), then the Python
executor runs the plan over the fixture tables and the result is compared with DuckDB's
(nearly all of the time).

For a patch that only edits statements inside functions, the second part behaves exactly as
on the pinned commit when
  * none of the edited statements runs during it (maps/tpcds.bin holds the lines that run from
    the moment `PythonExecutor.execute` is entered until the query's result has been compared,
    recorded on the pinned commit), and
  * everything handed to it is the same: the fixture tables, the executor object, the plan,
    and the DuckDB SQL the expected result is computed from.
The second point is checked here by running the first part only, on the patched tree, with
`PythonExecutor.execute` replaced by a function that fingerprints its arguments. If every
fingerprint equals the pinned one, the test cannot change its outcome and is dropped.

Fingerprints are structural (imstate.Walker) with plan-specific care so that they do not
depend on memory addresses or hash seeds: expressions are hashed top-down without their
parent links (an expression held outside its tree also hashes its root and its path in it),
steps without their back links (`dependents`), which are hashed separately as an edge list;
the executor's table mapping, which the pinned code builds from a set, ignoring dict order.
"""

import hashlib
import os
import sys

from .imstate import Walker

TEST_ID = "tests/test_executor.py::TestExecutor::test_execute_tpcds"


class PlanWalker(Walker):
    def __init__(self, line_map=None, seed=None):
        super().__init__(line_map, 0)
        from sqlglot.executor.table import Table, Tables
        from sqlglot.expressions.core import Expr
        from sqlglot.planner import Plan, Step

        self.Expr, self.Step, self.Plan, self.Table, self.Tables = Expr, Step, Plan, Table, Tables
        self.depth = 0
        self.cmemo = {}
        self.rows = seed if seed is not None else {}

    def fp(self, o):
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
                parts = ["X", t.__module__, t.__qualname__]
                for s in ("args", "comments", "_type", "_meta"):
                    parts.append(self.fp(getattr(o, s, None)))
                d = getattr(o, "__dict__", None)
                if d:
                    for k in sorted(d):
                        parts += [k, self.fp(d[k])]
                return self._h(*parts)
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


def _rows_fp(rows):
    return hashlib.blake2b(repr(rows).encode("utf-8", "surrogatepass"), digest_size=8).digest()


class _NoDb:
    """Stands in for the duckdb module while the test class is set up."""

    def connect(self, *a, **k):
        return self

    def execute(self, *a, **k):
        return self

    def close(self):
        pass


def probe(root, files, line_maps, baseline=None):
    """Runs in a forked child with nothing of sqlglot imported yet. Without a baseline:
    {"setup": hex, "plans": [hex or "-" per query], "sql": [hex or "-" per query]}.
    With one: {"same": bool, "at": where it first differs}."""
    import pickle

    from . import verify

    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    sys.stdout = open(os.devnull, "w")
    sys.stderr = sys.stdout
    verify._install_overlay(root, files)
    import tests.test_executor as te
    from sqlglot.executor import python as py

    cls = te.TestExecutor
    # The class set-up as the test has it, minus what this check never uses: the DuckDB
    # connections (they only compute expected results) and the TPC-H tables.
    real = te.duckdb, te.TPCH_SCHEMA
    te.duckdb, te.TPCH_SCHEMA = _NoDb(), {}
    try:
        cls.setUpClass()
    finally:
        te.duckdb, te.TPCH_SCHEMA = real
    tables, sqls, schema = cls.tpcds_tables, cls.tpcds_sqls, te.TPCDS_SCHEMA
    # while planning, a fixture table is identified by name; its content is hashed once at
    # the end (it is the slowest part, and most patches that differ do so in the first plans)
    seed = {id(tables[name].rows): ("fixture table " + name).encode() for name in tables}
    te.initializer(schema, tables)
    got = []

    def stub(self, plan, outer_scope=None):
        got.append(PlanWalker(line_maps, seed).fp((plan, outer_scope, self.__dict__)).hex())
        return None

    py.PythonExecutor.execute = stub
    plans = []
    want = baseline["plans"] if baseline is not None else None
    if want is not None and len(want) != len(sqls):
        return {"same": False, "at": "count"}
    for i, (meta, sql, _) in enumerate(sqls):
        n = len(got)
        try:
            expression = te.parse_one(sql)
            fn, args = pickle.loads(pickle.dumps((te.mp_execute, (expression, meta))))
            fn(*args)
            cur = "-" if len(got) == n else ",".join(got[n:])
        except Exception as e:  # noqa: BLE001
            cur = f"error:{type(e).__name__}"
        plans.append(cur)
        if want is not None and cur != want[i]:
            return {"same": False, "at": i}
    out_sql = []
    wsql = baseline["sql"] if baseline is not None else None
    for i, (meta, sql, _) in enumerate(sqls):
        if plans[i] == "-":
            cur = "-"
        else:
            try:
                text = te.transpile(sql, write="duckdb")[0]
                cur = hashlib.blake2b(text.encode("utf-8", "surrogatepass"), digest_size=8).hexdigest()
            except Exception as e:  # noqa: BLE001
                cur = f"error:{type(e).__name__}"
        out_sql.append(cur)
        if wsql is not None and cur != wsql[i]:
            return {"same": False, "at": f"sql {i}"}
    content = {id(tables[name].rows): _rows_fp(tables[name].rows) for name in tables}
    setup = PlanWalker(line_maps, content).fp((tables, schema, sqls)).hex()
    if baseline is not None:
        return {"same": setup == baseline["setup"], "at": None if setup == baseline["setup"] else "setup"}
    return {"setup": setup, "plans": plans, "sql": out_sql}
