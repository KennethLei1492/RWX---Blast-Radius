"""Which tests can see an `and` <-> `or` swap? (build time only)

    python tools/bool_trace.py TESTS_JSON OUT_DIR JOBS     (inside the scorer image)

Every `and`/`or` chain inside a function in sqlglot is rewritten to

    __blast_bool__(site, is_and, bool_context, pure_mask, lambda: v1, lambda: v2, ...)

which evaluates the chain exactly as Python would (same operands, same order, same result)
and then asks, for each operator in the chain, what the chain would have produced with that
one operator swapped:

    a1 and .. ai  OR  ai+1 and .. an     (an `and` swapped at position i)
    a1 or .. (ai AND ai+1) or .. an      (an `or` swapped at position i)

The swap is recorded as visible to the running test unless the swapped chain provably
evaluates to the same thing using only operands the original already evaluated: it must not
need an operand the original skipped (unknown value, possible exception), must not skip an
operand with possible side effects (a call) that the original ran, and must give the same
result (same truthiness where only truthiness is used, the same object otherwise).

Chains are left alone (so swaps there fall back to line coverage) when they contain a walrus,
await, yield or zero-argument super(), or sit outside any function, because wrapping
operands in lambdas would change their meaning.

One forked child per test, as in the other tracers; evaluations while importing sqlglot are
kept separately (a swap visible at import is visible to every test).
"""

import ast
import builtins
import importlib.abc
import importlib.machinery
import io
import json
import os
import sys
import tokenize

ROOT = "/opt/sqlglot/"
SITES: list = []        # sid -> (file, [(line, col) per operator], is_and)
DIS: set = set()        # (sid, position) visible in the current phase
DONE: set = set()       # sids whose every position is already visible
_PLAIN = (type(None), bool, int, float, str, bytes)


def _same(a, b, boolctx):
    if a is b:
        return True
    try:
        if boolctx:
            return bool(a) == bool(b)
        return type(a) is type(b) and type(a) in _PLAIN and a == b
    except Exception:
        return False


def _bool(sid, is_and, boolctx, pure, *thunks):
    n = len(thunks)
    vals = []
    truth = []
    res = None
    try:
        for i, th in enumerate(thunks):
            res = th()
            vals.append(res)
            if i < n - 1:
                t = bool(res)
                truth.append(t)
                if t != is_and:      # and: stop on falsy; or: stop on truthy
                    break
    except BaseException:
        # An operand raised. A swapped operator may never evaluate that operand, so every
        # swap in this chain counts as visible to this test.
        for i in range(n - 1):
            DIS.add((sid, i))
        DONE.add(sid)
        raise
    if sid in DONE:
        return res
    k = len(vals)                # operands evaluated; truth known for the first len(truth)
    stopped = k < n              # short-circuited at operand k-1
    all_dis = True
    for i in range(n - 1):
        key = (sid, i)
        if key in DIS:
            continue
        visible = True
        try:
            if is_and:
                # (v0 and .. vi) or (vi+1 and .. vn-1)
                if k - 1 > i or (k - 1 == i and not stopped):
                    # first group fully truthy: result is vi, the rest is skipped
                    skipped_pure = all(pure >> j & 1 for j in range(i + 1, k))
                    if skipped_pure and (k - 1 > i) and _same(vals[i], res, boolctx):
                        visible = False
                # else the original stopped inside the first group: the swap would go on to
                # evaluate operands the original never ran -> visible
            else:
                # v0 or .. or (vi and vi+1) or .. vn-1
                if k - 1 < i:
                    visible = False          # an earlier operand was truthy: identical run
                elif k - 1 == i:
                    # reached vi and stopped there (truthy), or vi is the last operand
                    visible = True           # vi truthy: the swap must evaluate vi+1
                else:
                    # vi was falsy and the original went on to vi+1
                    if pure >> (i + 1) & 1:
                        if k - 1 == i + 1:
                            if stopped:
                                visible = True       # vi+1 truthy: original stops, swap goes on
                            elif i + 1 == n - 1:
                                # vi+1 is the last operand: swap yields vi, original vi+1
                                visible = not _same(vals[i], res, boolctx)
                        else:
                            visible = False  # vi+1 falsy too: both continue identically
        except Exception:
            visible = True
        if visible:
            DIS.add(key)
        else:
            all_dis = False
    if all_dis:
        DONE.add(sid)
    return res


builtins.__blast_bool__ = _bool


def _is_pure(node):
    """No calls (or anything else that could have an effect) inside the operand."""
    for n in ast.walk(node):
        if isinstance(n, (ast.Call, ast.NamedExpr, ast.Await, ast.Yield, ast.YieldFrom, ast.Lambda,
                          ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            return False
    return True


def _unsafe(node):
    for n in ast.walk(node):
        if isinstance(n, (ast.NamedExpr, ast.Await, ast.Yield, ast.YieldFrom)):
            return True
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "super" and not n.args:
            return True
        if isinstance(n, ast.Name) and n.id in ("__class__", "locals", "vars", "eval", "exec"):
            return True
    return False


def operator_tokens(src):
    """(line, col) of every `and` / `or` keyword token."""
    out = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.NAME and tok.string in ("and", "or"):
                out.append((tok.start[0], tok.start[1], tok.string))
    except (tokenize.TokenError, SyntaxError, IndentationError):
        pass
    return out


class _Rewrite(ast.NodeTransformer):
    def __init__(self, rel, src, record=True):
        self.rel = rel
        self.ops = operator_tokens(src)
        self.depth = 0          # inside a function or lambda
        self.record = record
        self.found = []         # (operator positions, is_and) for build tools

    def _ctx(self, node, boolctx):
        node._blast_boolctx = boolctx
        return node

    def visit_FunctionDef(self, node):
        # decorators, defaults and annotations run in the enclosing scope
        node.decorator_list = [self.visit(d) for d in node.decorator_list]
        self.visit(node.args)
        self.depth += 1
        node.body = [self.visit(s) for s in node.body]
        self.depth -= 1
        return node

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        self.visit(node.args)
        self.depth += 1
        node.body = self.visit(node.body)
        self.depth -= 1
        return node

    def visit_ClassDef(self, node):
        d, self.depth = self.depth, 0   # a class body is not a function scope
        self.generic_visit(node)
        self.depth = d
        return node

    def _mark(self, test):
        if isinstance(test, ast.BoolOp):
            test._blast_boolctx = True
        elif isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
            self._mark(test.operand)

    def visit_If(self, node):
        self._mark(node.test)
        return self.generic_visit(node)

    visit_While = visit_If
    visit_IfExp = visit_If
    visit_Assert = visit_If

    def visit_UnaryOp(self, node):
        if isinstance(node.op, ast.Not):
            self._mark(node.operand)
        return self.generic_visit(node)

    def visit_comprehension(self, node):
        for t in node.ifs:
            self._mark(t)
        return self.generic_visit(node)

    def visit_BoolOp(self, node):
        boolctx = getattr(node, "_blast_boolctx", False)
        if boolctx:
            for v in node.values:
                self._mark(v)       # an operand's value only matters as a truth value too
        positions = []
        ok = True
        for a, b in zip(node.values, node.values[1:]):
            lo = (a.end_lineno, a.end_col_offset)
            hi = (b.lineno, b.col_offset)
            want = "and" if isinstance(node.op, ast.And) else "or"
            cands = [(l, c) for l, c, s in self.ops if s == want and lo <= (l, c) < hi]
            if len(cands) != 1:
                ok = False
                break
            positions.append(cands[0])
        unsafe = self.depth == 0 or not ok or _unsafe(node)
        pure = 0
        for i, v in enumerate(node.values):
            if _is_pure(v):
                pure |= 1 << i
        self.generic_visit(node)
        if unsafe:
            return node
        is_and = isinstance(node.op, ast.And)
        self.found.append((positions, is_and))
        if not self.record:
            return node
        sid = len(SITES)
        SITES.append((self.rel, positions, is_and))
        thunks = [ast.Lambda(args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
                             body=v) for v in node.values]
        call = ast.Call(func=ast.Name(id="__blast_bool__", ctx=ast.Load()),
                        args=[ast.Constant(sid), ast.Constant(is_and), ast.Constant(boolctx), ast.Constant(pure)] + thunks,
                        keywords=[])
        return ast.copy_location(call, node)


class _Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        path = self.get_filename(fullname)
        return self.source_to_code(self.get_data(path), path)

    def source_to_code(self, data, path, *, _optimize=-1):
        src = data.decode("utf-8") if isinstance(data, bytes) else data
        tree = _Rewrite(path[len(ROOT):], src).visit(ast.parse(src, path))
        ast.fix_missing_locations(tree)
        return compile(tree, path, "exec", dont_inherit=True, optimize=_optimize)


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if not (name == "sqlglot" or name.startswith("sqlglot.")):
            return None
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        if spec is None or not isinstance(spec.loader, importlib.machinery.SourceFileLoader):
            return spec
        if not (spec.origin or "").startswith(ROOT):
            return spec
        spec.loader = _Loader(spec.loader.name, spec.loader.path)
        return spec


def main():
    global DIS, DONE
    tests_json, out_dir, jobs = sys.argv[1], sys.argv[2], int(sys.argv[3])
    os.environ.update(OMP_NUM_THREADS="1", SKIP_INTEGRATION="0", PYTHONDONTWRITEBYTECODE="1")
    sys.dont_write_bytecode = True
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path[:0] = [ROOT.rstrip("/"), here]
    os.chdir(ROOT)
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
    json.dump({"sites": SITES, "dis": sorted(DIS)}, open(os.path.join(out_dir, "_import.json"), "w"))
    print(f"{len(SITES)} and/or chains instrumented; {len(DIS)} swaps visible at import", flush=True)

    tests = json.load(open(tests_json))["tests"]
    skip = set(filter(None, os.environ.get("BOOL_SKIP", "").split(",")))
    todo = [t for t in sorted(tests, key=lambda t: -t["seconds"])
            if t["id"] not in skip and not os.path.exists(os.path.join(out_dir, f"{t['number']}.json"))]
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

                signal.alarm(int(os.environ.get("BOOL_TIMEOUT", "1800")))
                devnull = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull, 1)
                os.dup2(devnull, 2)
                DIS = set()
                DONE = set()
                g = globals()
                g["DIS"], g["DONE"] = DIS, DONE
                code = pytest.main([t["id"], "-q", "-p", "no:cacheprovider"])
                if int(code) == 0:
                    out = os.path.join(out_dir, f"{t['number']}.json")
                    with open(out + ".tmp", "w") as fh:
                        json.dump({"dis": sorted(DIS)}, fh)
                    os.replace(out + ".tmp", out)
                os._exit(int(code))
            except BaseException:
                os._exit(99)
        running[pid] = t
    while running:
        reap()
    print(f"bool-traced {len(todo)} tests, {bad} non-zero exits", flush=True)


if __name__ == "__main__":
    main()
