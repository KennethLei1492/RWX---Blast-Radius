"""Which tests call a function relying on a parameter's default? (build time only)

    python tools/defaults_trace.py TESTS_JSON OUT_DIR JOBS     (inside the scorer image)

A changed default (`copy: bool = True` -> `False`) can only matter to a call that does not
supply that parameter. Every `def` in sqlglot with defaults is rewritten so that

    def f(a, p=<expr>):            def f(a, p=__blast_dflt__(site, 0, <expr>)):
        body                ->         __blast_entry__(site, (p,))
                                       body

__blast_dflt__ remembers the default object (and returns it, so nothing changes), and
__blast_entry__ records (site, i) for the running test whenever the parameter's value at
entry is that very object. A caller that passes an identical object explicitly is counted
too, which only errs on the safe side; a value that is not the default object was certainly
supplied by the caller.

One forked child per test, as in the other tracers; calls made while importing sqlglot are
kept separately (a default used at import can shape state every test sees).
"""

import ast
import builtins
import importlib.abc
import importlib.machinery
import json
import os
import sys

ROOT = "/opt/sqlglot/"
SITES: list = []      # site -> (file, def line, [(param, line, col, end_line, end_col) per default])
DEFAULTS: dict = {}   # (site, i) -> default object
USED: set = set()     # (site, i) used by the current phase


OVERFLOW: set = set()


def _dflt(site, i, value):
    # a nested def runs many times and may build a fresh default object each time: keep
    # every one (up to a bound; beyond it the default counts as always used)
    lst = DEFAULTS.setdefault((site, i), [])
    for d in lst:
        if value is d:
            return value
    if len(lst) < 32:
        lst.append(value)
    else:
        OVERFLOW.add((site, i))
    return value


def _entry(site, values):
    for i, v in enumerate(values):
        k = (site, i)
        if k in USED:
            continue
        if k in OVERFLOW:
            USED.add(k)
            continue
        for d in DEFAULTS.get(k, ()):
            if v is d:
                USED.add(k)
                break


builtins.__blast_dflt__ = _dflt
builtins.__blast_entry__ = _entry


def default_params(node):
    """[(param name, default expr)] for positional and keyword-only defaults."""
    a = node.args
    pos = a.posonlyargs + a.args
    out = list(zip([p.arg for p in pos[len(pos) - len(a.defaults):]], a.defaults))
    out += [(p.arg, d) for p, d in zip(a.kwonlyargs, a.kw_defaults) if d is not None]
    return out


class _Rewrite(ast.NodeTransformer):
    def __init__(self, rel, record=True):
        self.rel = rel
        self.record = record
        self.found = []

    def visit_FunctionDef(self, node):
        self.generic_visit(node)
        params = default_params(node)
        if not params:
            return node
        spans = [(name, d.lineno, d.col_offset, d.end_lineno, d.end_col_offset) for name, d in params]
        self.found.append((node.lineno, spans))
        if not self.record:
            return node
        site = len(SITES)
        SITES.append((self.rel, node.lineno, spans))
        a = node.args
        npos = len(a.defaults)
        for i in range(npos):
            a.defaults[i] = ast.copy_location(
                ast.Call(func=ast.Name(id="__blast_dflt__", ctx=ast.Load()),
                         args=[ast.Constant(site), ast.Constant(i), a.defaults[i]], keywords=[]), a.defaults[i])
        j = npos
        for k, d in enumerate(a.kw_defaults):
            if d is not None:
                a.kw_defaults[k] = ast.copy_location(
                    ast.Call(func=ast.Name(id="__blast_dflt__", ctx=ast.Load()),
                             args=[ast.Constant(site), ast.Constant(j), d], keywords=[]), d)
                j += 1
        call = ast.Expr(ast.Call(
            func=ast.Name(id="__blast_entry__", ctx=ast.Load()),
            args=[ast.Constant(site),
                  ast.Tuple(elts=[ast.Name(id=name, ctx=ast.Load()) for name, _ in params], ctx=ast.Load())],
            keywords=[]))
        body = node.body
        k = 1 if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
                  and isinstance(body[0].value.value, str)) else 0
        ast.copy_location(call, body[0])
        node.body = body[:k] + [call] + body[k:]
        return node

    visit_AsyncFunctionDef = visit_FunctionDef


class _Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        path = self.get_filename(fullname)
        return self.source_to_code(self.get_data(path), path)

    def source_to_code(self, data, path, *, _optimize=-1):
        src = data.decode("utf-8") if isinstance(data, bytes) else data
        tree = _Rewrite(path[len(ROOT):]).visit(ast.parse(src, path))
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
    global USED
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
    json.dump({"sites": SITES, "used": sorted(USED)}, open(os.path.join(out_dir, "_import.json"), "w"))
    print(f"{len(SITES)} functions with defaults; {len(USED)} defaults used at import", flush=True)

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

                signal.alarm(int(os.environ.get("DFLT_TIMEOUT", "4000")))
                devnull = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull, 1)
                os.dup2(devnull, 2)
                USED = set()
                globals()["USED"] = USED
                code = pytest.main([t["id"], "-q", "-p", "no:cacheprovider"])
                if int(code) == 0:
                    out = os.path.join(out_dir, f"{t['number']}.json")
                    with open(out + ".tmp", "w") as fh:
                        json.dump({"used": sorted(USED)}, fh)
                    os.replace(out + ".tmp", out)
                os._exit(int(code))
            except BaseException:
                os._exit(99)
        running[pid] = t
    while running:
        reap()
    print(f"defaults-traced {len(todo)} tests, {bad} non-zero exits", flush=True)


if __name__ == "__main__":
    main()
