"""Comparison agreement profile of the pinned commit (build time only).

    python tools/cmp_trace.py TESTS_JSON OUT_DIR JOBS     (inside the scorer image)

Every single-operator comparison in sqlglot (==, !=, is, is not, <, <=, >, >=) that sits on
one line is rewritten to call a recorder that evaluates the original operator (whose
result and exceptions are returned unchanged) and also every alternative operator. For
each test, run alone in a forked child, it writes which (file, line, column, alternative)
ever produced a different result. A test whose comparisons all agree under an
alternative operator runs identically if that operator is swapped in.

Like the line trace, a fork server imports everything once and forks one child per test,
so no cache built by another test hides an evaluation.
"""

import ast
import builtins
import importlib.abc
import importlib.machinery
import json
import operator
import os
import sys

ROOT = "/sqlglot/"

OPS = {
    ast.Eq: "==", ast.NotEq: "!=", ast.Is: "is", ast.IsNot: "is not",
    ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=",
}
FN = {
    "==": operator.eq, "!=": operator.ne, "is": operator.is_, "is not": operator.is_not,
    "<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge,
}
import enum

EQ = ("==", "!=", "is", "is not")
ORD = ("<", "<=", ">", ">=")
NEG = {"==": "!=", "!=": "==", "is": "is not", "is not": "is", "<": ">=", ">=": "<", ">": "<=", "<=": ">"}
# Plausible swaps only: within the equality/identity group, within ordering, and between
# ordering and equality.
ALTS = {op: [a for a in (EQ if op in EQ else ORD + ("==", "!=")) if a != op] for op in FN}
for _op in ("==", "!="):
    ALTS[_op] = ALTS[_op] + list(ORD)

_SAFE = (type(None), bool, int, float, str, bytes)


def _plain(x):
    """Comparing x with == has no side effects and costs nothing."""
    t = type(x)
    return t in _SAFE or t.__eq__ is object.__eq__ or isinstance(x, enum.Enum)


DIS: set = set()     # (fid, line, col, alt) that disagreed in this test
EVAL: set = set()    # (fid, line, col) evaluated in this test
DONE: set = set()    # sites whose every alternative already disagreed in this test
FILES: list = []     # fid -> relative path
_phase = {"import": True}
IMPORT_DIS: set = set()
IMPORT_DONE: set = set()


def _expr_eq(x):
    """sqlglot's Expr.__eq__: `self is other or (same type and equal hashes)`. Against None or
    another type it returns False at once; between nodes it only caches each node's hash
    (reset on mutation). Evaluating it changes nothing a test can see."""
    f = getattr(type(x), "__eq__", None)
    return getattr(f, "__qualname__", "") in ("Expr.__eq__", "Expression.__eq__") and \
        getattr(f, "__module__", "") == "sqlglot.expressions.core"


def _eq_ok(x):
    return _plain(x) or _expr_eq(x)


def _cmp(fid, line, col, op, a, b):
    try:
        r = FN[op](a, b)
    except BaseException:
        # The original raised: any alternative that would not raise the same way differs.
        _record_raise(fid, line, col, op, a, b)
        raise
    key = (fid, line, col)
    imp = _phase["import"]
    dis = IMPORT_DIS if imp else DIS
    done = IMPORT_DONE if imp else DONE
    EVAL.add(key)
    if key in done:
        return r
    rb = type(r) is bool
    all_dis = True
    for alt in ALTS[op]:
        k = (fid, line, col, alt)
        if k in dis:
            continue
        if alt == NEG[op] and rb:
            dis.add(k)  # the negation always differs
            continue
        if op in ("is", "is not") and alt in ("==", "!=") and not (_eq_ok(a) and _eq_ok(b)):
            dis.add(k)  # calling an unknown __eq__ where the original did not: assume it differs
            continue
        if op in ("is", "is not") and alt in ORD and not (_plain(a) and _plain(b)):
            dis.add(k)
            continue
        if alt in ORD and op not in ORD and not (
                (type(a) in (int, float) and type(b) in (int, float))
                or (type(a) is str and type(b) is str)):
            dis.add(k)
            continue
        try:
            ra = FN[alt](a, b)
        except Exception:
            dis.add(k)
            continue
        if not (rb and type(ra) is bool and ra == r) and ra is not r:
            dis.add(k)
            continue
        all_dis = False
    if all_dis and all((fid, line, col, alt) in dis for alt in ALTS[op]):
        done.add(key)
    return r


def _record_raise(fid, line, col, op, a, b):
    dis = IMPORT_DIS if _phase["import"] else DIS
    EVAL.add((fid, line, col))
    for alt in ALTS[op]:
        dis.add((fid, line, col, alt))  # conservative: the swap may stop the exception


builtins.__blast_cmp__ = _cmp


def _op_col(src_line, left, right, op):
    """Column of the operator token between the two operands on one line."""
    seg = src_line[left.end_col_offset:right.col_offset]
    i = 0
    while i < len(seg) and seg[i] in " )\t":
        i += 1
    if not seg[i:].startswith(op.split()[0]):
        return None
    return left.end_col_offset + i


class _Rewrite(ast.NodeTransformer):
    def __init__(self, fid, lines):
        self.fid = fid
        self.lines = lines

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) != 1 or type(node.ops[0]) not in OPS:
            return node
        left, right = node.left, node.comparators[0]
        if not (left.end_lineno == right.lineno == node.lineno):
            return node
        op = OPS[type(node.ops[0])]
        col = _op_col(self.lines[node.lineno - 1], left, right, op)
        if col is None:
            return node
        call = ast.Call(
            func=ast.Name(id="__blast_cmp__", ctx=ast.Load()),
            args=[ast.Constant(self.fid), ast.Constant(node.lineno), ast.Constant(col),
                  ast.Constant(op), left, right],
            keywords=[],
        )
        return ast.copy_location(call, node)


class _Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        # never use cached bytecode: always rewrite the source
        path = self.get_filename(fullname)
        return self.source_to_code(self.get_data(path), path)

    def source_to_code(self, data, path, *, _optimize=-1):
        src = data.decode("utf-8") if isinstance(data, bytes) else data
        rel = path[len(ROOT):]
        fid = len(FILES)
        FILES.append(rel)
        tree = ast.parse(src, path)
        tree = _Rewrite(fid, src.split("\n")).visit(tree)
        ast.fix_missing_locations(tree)
        return compile(tree, path, "exec", dont_inherit=True, optimize=_optimize)


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if not (name == "sqlglot" or name.startswith("sqlglot.")):
            return None
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        if spec is None or not isinstance(spec.loader, importlib.machinery.SourceFileLoader):
            return spec
        if not spec.origin.startswith(ROOT):
            return spec
        spec.loader = _Loader(spec.loader.name, spec.loader.path)
        return spec


def main():
    tests_json, out_dir, jobs = sys.argv[1], sys.argv[2], int(sys.argv[3])
    os.environ.update(OMP_NUM_THREADS="1", SKIP_INTEGRATION="0", PYTHONDONTWRITEBYTECODE="1")
    sys.dont_write_bytecode = True
    sys.path[:0] = ["/sqlglot", "/proj"]
    os.chdir("/sqlglot")
    os.makedirs(out_dir, exist_ok=True)
    sys.meta_path.insert(0, _Finder())

    import pkgutil

    import pytest

    import sqlglot
    import tools.trace_plugin as tp  # for the inline process pool only

    tp._inline_process_pools()
    for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
        if info.name != "sqlglot.__main__":
            try:
                importlib.import_module(info.name)
            except Exception as e:
                print(f"cannot import {info.name}: {e}", file=sys.stderr)
    json.dump({"files": FILES, "import_dis": sorted(IMPORT_DIS)},
              open(os.path.join(out_dir, "_import.json"), "w"))

    class Plugin:
        @pytest.hookimpl(tryfirst=True)
        def pytest_runtest_setup(self, item):
            # collection is over: everything from here belongs to the test
            _phase["import"] = False

        def pytest_collection_finish(self, session):
            _phase["import"] = False

    tests = json.load(open(tests_json))["tests"]
    skip = set(filter(None, os.environ.get("CMP_SKIP", "").split(",")))
    todo = [t for t in sorted(tests, key=lambda t: -t["seconds"])
            if t["id"] not in skip and not os.path.exists(os.path.join(out_dir, f"{t['number']}.json"))]
    running = {}
    bad = 0
    for t in todo:
        while len(running) >= jobs:
            pid, status = os.waitpid(-1, 0)
            tt = running.pop(pid)
            if os.waitstatus_to_exitcode(status) != 0:
                bad += 1
                print(f"{tt['id']}: exit {os.waitstatus_to_exitcode(status)}", flush=True)
        pid = os.fork()
        if pid == 0:
            try:
                import signal

                signal.alarm(int(os.environ.get("CMP_TIMEOUT", "1500")))
                devnull = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull, 1)
                os.dup2(devnull, 2)
                # Collection of the test module counts for this test too (one test per process).
                _phase["import"] = False
                code = pytest.main([t["id"], "-q", "-p", "no:cacheprovider"], plugins=[Plugin()])
                out = os.path.join(out_dir, f"{t['number']}.json")
                with open(out + ".tmp", "w") as fh:
                    json.dump({"dis": sorted(DIS), "eval": sorted(EVAL), "files": FILES}, fh)
                os.replace(out + ".tmp", out)
                os._exit(int(code))
            except BaseException:
                os._exit(99)
        running[pid] = t
    while running:
        pid, status = os.waitpid(-1, 0)
        tt = running.pop(pid)
        if os.waitstatus_to_exitcode(status) != 0:
            bad += 1
            print(f"{tt['id']}: exit {os.waitstatus_to_exitcode(status)}", flush=True)
    print(f"cmp-traced {len(todo)} tests, {bad} non-zero exits", flush=True)


if __name__ == "__main__":
    main()
