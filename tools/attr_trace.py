"""Which class attributes does each test read? (build time only)

    python tools/attr_trace.py TESTS_JSON OUT_DIR JOBS     (inside the scorer image, cwd anywhere)

sqlglot keeps its behaviour switches as class-level data: Dialect flags, Generator and Parser
settings, Tokenizer tables, expression arg_types. Line coverage cannot say which tests depend
on `Spark.SUPPORTS_ORDER_BY_ALL = True`: the statement runs at import. So this tracer rewrites,
at import, every load of an attribute whose name is assigned in some sqlglot class body

    x.NAME            ->  __blast_attr__(x, "NAME")
    getattr(x, n, *d) ->  __blast_getattr__(x, n, *d)        (also hasattr)
    x.__dict__        ->  __blast_dict__(x)                  (also vars(x))

in sqlglot and in the tests. Each helper performs the original operation and records
(class whose body supplies the value, NAME): the first class in the MRO whose __dict__ has
NAME. A class __dict__ is returned as a proxy that records the keys looked up (and a wildcard
when iterated).

Like the other tracers: a fork server imports everything once (those reads are import-time
reads, which every test inherits) and forks one child per test, collection included.

A change to an existing class-body assignment can only alter a test that reads that
(class, NAME): until the first such read, the run is identical.
"""

import ast
import builtins
import importlib.abc
import importlib.machinery
import json
import os
import sys

ROOT = "/opt/sqlglot/"
NAMES: set = set()          # attribute names assigned in some sqlglot class body
READS: set = set()          # (owner key, name) in the current phase
LOADED: list = []         # files loaded through the rewriting loader
_owner_cache: dict = {}
_key_cache: dict = {}


def class_body_names(root):
    names = set()
    import glob

    for p in glob.glob(os.path.join(root, "sqlglot", "**", "*.py"), recursive=True):
        tree = ast.parse(open(p, encoding="utf-8").read())
        for cls in ast.walk(tree):
            if not isinstance(cls, ast.ClassDef):
                continue
            stack = list(cls.body)
            while stack:
                st = stack.pop()
                if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue
                if isinstance(st, ast.Assign):
                    targets = st.targets
                elif isinstance(st, (ast.AnnAssign, ast.AugAssign)):
                    targets = [st.target]
                else:
                    for f in ("body", "orelse", "finalbody"):
                        stack.extend(getattr(st, f, []) or [])
                    for h in getattr(st, "handlers", []) or []:
                        stack.extend(h.body)
                    continue
                for t in targets:
                    for n in ast.walk(t):
                        if isinstance(n, ast.Name):
                            names.add(n.id)
    return {n for n in names if not (n.startswith("__") and n.endswith("__"))}


def _key(cls):
    k = _key_cache.get(cls)
    if k is None:
        mod = getattr(cls, "__module__", "") or ""
        k = f"{mod}:{cls.__qualname__}" if mod.startswith(("sqlglot", "tests")) else ""
        _key_cache[cls] = k
    return k


def _owner(t, name, is_class):
    ck = (t, name, is_class)
    o = _owner_cache.get(ck)
    if o is None:
        o = ""
        mros = [t.__mro__, type(t).__mro__] if is_class else [t.__mro__]
        for mro in mros:
            for k in mro:
                if name in k.__dict__:
                    o = _key(k)
                    break
            if o:
                break
        _owner_cache[ck] = o
    return o


def _record(obj, name):
    try:
        if isinstance(obj, type):
            o = _owner(obj, name, True)
        elif isinstance(obj, super):
            # value comes from the MRO after __thisclass__
            start = obj.__thisclass__
            mro = (obj.__self_class__ or start).__mro__
            o = ""
            seen = False
            for k in mro:
                if seen and name in k.__dict__:
                    o = _key(k)
                    break
                if k is start:
                    seen = True
        else:
            o = _owner(type(obj), name, False)
        if o:
            READS.add((o, name))
    except Exception:
        READS.add(("*", name))  # could not resolve: every class with that name


def _attr(obj, name):
    _record(obj, name)
    return getattr(obj, name)


def _getattr(obj, name, *default):
    if isinstance(name, str) and name in NAMES:
        _record(obj, name)
    return getattr(obj, name, *default)


def _hasattr(obj, name):
    if isinstance(name, str) and name in NAMES:
        _record(obj, name)
    return hasattr(obj, name)


class _DictProxy:
    """Stands in for a class __dict__ (a mappingproxy): records which keys are consulted."""

    __slots__ = ("_d", "_k")

    def __init__(self, d, key):
        self._d = d
        self._k = key

    def _one(self, name):
        if self._k and isinstance(name, str):
            READS.add((self._k, name))

    def _all(self):
        if self._k:
            READS.add((self._k, "*"))

    def get(self, name, default=None):
        self._one(name)
        return self._d.get(name, default)

    def __getitem__(self, name):
        self._one(name)
        return self._d[name]

    def __contains__(self, name):
        self._one(name)
        return name in self._d

    def __iter__(self):
        self._all()
        return iter(self._d)

    def __len__(self):
        self._all()
        return len(self._d)

    def keys(self):
        self._all()
        return self._d.keys()

    def values(self):
        self._all()
        return self._d.values()

    def items(self):
        self._all()
        return self._d.items()

    def copy(self):
        self._all()
        return dict(self._d)

    def __eq__(self, other):
        self._all()
        return self._d == (other._d if isinstance(other, _DictProxy) else other)

    def __repr__(self):
        self._all()
        return repr(self._d)


def _dict(obj):
    d = obj.__dict__
    if isinstance(obj, type):
        return _DictProxy(d, _key(obj))
    return d


def _vars(*args):
    if len(args) == 1 and isinstance(args[0], type):
        return _DictProxy(vars(args[0]), _key(args[0]))
    return vars(*args)


builtins.__blast_attr__ = _attr
builtins.__blast_getattr__ = _getattr
builtins.__blast_hasattr__ = _hasattr
builtins.__blast_dict__ = _dict
builtins.__blast_vars__ = _vars


class _Rewrite(ast.NodeTransformer):
    def visit_Attribute(self, node):
        self.generic_visit(node)
        if not isinstance(node.ctx, ast.Load):
            return node
        if node.attr == "__dict__":
            return ast.copy_location(
                ast.Call(func=ast.Name(id="__blast_dict__", ctx=ast.Load()), args=[node.value], keywords=[]), node)
        if node.attr in NAMES:
            return ast.copy_location(
                ast.Call(func=ast.Name(id="__blast_attr__", ctx=ast.Load()),
                         args=[node.value, ast.Constant(node.attr)], keywords=[]), node)
        return node

    def visit_Call(self, node):
        self.generic_visit(node)
        f = node.func
        if isinstance(f, ast.Name) and not node.keywords:
            if f.id == "getattr" and len(node.args) in (2, 3):
                node.func = ast.copy_location(ast.Name(id="__blast_getattr__", ctx=ast.Load()), f)
            elif f.id == "hasattr" and len(node.args) == 2:
                node.func = ast.copy_location(ast.Name(id="__blast_hasattr__", ctx=ast.Load()), f)
            elif f.id == "vars" and len(node.args) == 1:
                node.func = ast.copy_location(ast.Name(id="__blast_vars__", ctx=ast.Load()), f)
        return node


class _Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        path = self.get_filename(fullname)
        return self.source_to_code(self.get_data(path), path)

    def source_to_code(self, data, path, *, _optimize=-1):
        src = data.decode("utf-8") if isinstance(data, bytes) else data
        LOADED.append(path[len(ROOT):])
        tree = _Rewrite().visit(ast.parse(src, path))
        ast.fix_missing_locations(tree)
        return compile(tree, path, "exec", dont_inherit=True, optimize=_optimize)


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if not (name in ("sqlglot", "tests") or name.startswith(("sqlglot.", "tests."))):
            return None
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        if spec is None or not isinstance(spec.loader, importlib.machinery.SourceFileLoader):
            return spec
        if not (spec.origin or "").startswith(ROOT):
            return spec
        spec.loader = _Loader(spec.loader.name, spec.loader.path)
        return spec


def main():
    global READS
    tests_json, out_dir, jobs = sys.argv[1], sys.argv[2], int(sys.argv[3])
    os.environ.update(OMP_NUM_THREADS="1", SKIP_INTEGRATION="0", PYTHONDONTWRITEBYTECODE="1")
    sys.dont_write_bytecode = True
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path[:0] = [ROOT.rstrip("/"), here]
    os.chdir(ROOT)
    os.makedirs(out_dir, exist_ok=True)
    NAMES.update(class_body_names(ROOT))
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
    json.dump({"reads": sorted(READS), "names": sorted(NAMES)}, open(os.path.join(out_dir, "_import.json"), "w"))
    print(f"{len(NAMES)} class-body names; {len(READS)} import-time reads", flush=True)

    tests = json.load(open(tests_json))["tests"]
    skip = set(filter(None, os.environ.get("ATTR_SKIP", "").split(",")))
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

                signal.alarm(int(os.environ.get("ATTR_TIMEOUT", "1800")))
                devnull = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull, 1)
                os.dup2(devnull, 2)
                READS = set()
                globals()["READS"] = READS
                # --assert=plain: pytest's assertion rewriter would otherwise load the test
                # modules itself and their attribute reads would go unrecorded
                code = pytest.main([t["id"], "-q", "-p", "no:cacheprovider", "--assert=plain"])
                out = os.path.join(out_dir, f"{t['number']}.json")
                if int(code) == 0:
                    with open(out + ".tmp", "w") as fh:
                        json.dump({"reads": sorted(READS),
                                   "test_files": sorted(f for f in LOADED if f.startswith("tests/"))}, fh)
                    os.replace(out + ".tmp", out)
                os._exit(int(code))
            except BaseException:
                os._exit(99)
        running[pid] = t
    while running:
        reap()
    print(f"attr-traced {len(todo)} tests, {bad} non-zero exits", flush=True)


if __name__ == "__main__":
    main()
