"""Build the selector's maps from the per-test traces of the pinned commit.

    python tools/build_maps.py COV_DIR IMPORT_CLOSURE_JSON SQLGLOT_ROOT TESTS_JSON OUT

Everything here is derived from the pinned commit only: coverage per test, the classes
each test instantiates, the files each test opens, and a static index of the source.
"""

from __future__ import annotations

import ast
import glob
import gzip
import json
import os
import pickle
import sys
import zlib
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from blast.pyimpact import FUNC, SCOPE, _assigned_names, module_name  # noqa: E402


def load_traces(cov_dir, num):
    lines = defaultdict(lambda: defaultdict(int))
    import_lines = defaultdict(set)
    types = defaultdict(int)
    opens = defaultdict(int)
    import_opens = defaultdict(int)
    for path in sorted(glob.glob(os.path.join(cov_dir, "*.json"))):
        d = json.load(open(path))
        file_bits = 0
        cls_setup = defaultdict(lambda: defaultdict(set))
        cls_bits = defaultdict(int)
        per_test = {}
        for nodeid, phases in d.get("tests", {}).items():
            n = num.get(nodeid)
            if n is None:
                continue
            bit = 1 << n
            file_bits |= bit
            cls = nodeid.rsplit("::", 1)[0]
            cls_bits[cls] |= bit
            per_test[n] = phases
            for ph in ("setup", "teardown"):
                for f, ls in phases.get(ph, {}).get("lines", {}).items():
                    cls_setup[cls][f].update(ls)
        for n, phases in per_test.items():
            bit = 1 << n
            for ph, p in phases.items():
                for f, ls in p["lines"].items():
                    m = lines[f]
                    for l in ls:
                        m[l] |= bit
                for t in p["types"]:
                    types[t] |= bit
                for o in p["opens"]:
                    opens[o] |= bit
        # Collection of the test module (one test per process: exactly that test's import).
        for f, ls in d.get("collect_lines", {}).items():
            m = lines[f]
            for l in ls:
                m[l] |= file_bits
        for o in d.get("collect_opens", []):
            import_opens[o] |= file_bits
        for cls, files in cls_setup.items():
            bits = cls_bits[cls]
            for f, ls in files.items():
                m = lines[f]
                for l in ls:
                    m[l] |= bits
        for f, ls in d.get("import_lines", {}).items():
            import_lines[f].update(ls)
        for o in d.get("import_opens", []):
            import_opens[o] |= file_bits
    return lines, import_lines, types, opens, import_opens


def py_files(root):
    out = []
    for base in ("sqlglot", "tests", "sqlglotc"):
        for p in glob.glob(os.path.join(root, base, "**", "*.py"), recursive=True):
            out.append(os.path.relpath(p, root).replace(os.sep, "/"))
    return sorted(out)


def static_imports(root, files):
    """module -> set of modules it imports at import time (not under TYPE_CHECKING)."""
    mods = {module_name(f): f for f in files}
    deps = defaultdict(set)

    def resolve(base, name):
        full = f"{base}.{name}" if base else name
        return full if full in mods else base

    for f in files:
        mod = module_name(f)
        try:
            tree = ast.parse(open(os.path.join(root, f), encoding="utf-8").read())
        except SyntaxError:
            continue
        pkg = mod if f.endswith("__init__.py") else mod.rpartition(".")[0]

        def visit(body):
            for st in body:
                if isinstance(st, ast.If):
                    t = st.test
                    if (isinstance(t, ast.Name) and t.id == "TYPE_CHECKING") or (
                        isinstance(t, ast.Attribute) and t.attr == "TYPE_CHECKING"):
                        visit(st.orelse)
                        continue
                    visit(st.body)
                    visit(st.orelse)
                elif isinstance(st, ast.Try):
                    visit(st.body)
                    for h in st.handlers:
                        visit(h.body)
                    visit(st.orelse)
                    visit(st.finalbody)
                elif isinstance(st, ast.Import):
                    for a in st.names:
                        parts = a.name.split(".")
                        for i in range(1, len(parts) + 1):
                            m = ".".join(parts[:i])
                            if m in mods:
                                deps[mod].add(m)
                elif isinstance(st, ast.ImportFrom):
                    if st.level:
                        base = pkg
                        for _ in range(st.level - 1):
                            base = base.rpartition(".")[0]
                        base = f"{base}.{st.module}" if st.module else base
                    else:
                        base = st.module or ""
                    parts = base.split(".")
                    for i in range(1, len(parts) + 1):
                        m = ".".join(parts[:i])
                        if m in mods:
                            deps[mod].add(m)
                    for a in st.names:
                        r = resolve(base, a.name)
                        if r in mods:
                            deps[mod].add(r)

        visit(tree.body)
    return deps


def reverse_closure(deps):
    rev = defaultdict(set)
    for m, ds in deps.items():
        for d in ds:
            rev[d].add(m)
    out = {}
    for m in set(rev) | set(deps):
        seen = set()
        stack = [m]
        while stack:
            x = stack.pop()
            for y in rev.get(x, ()):
                if y not in seen:
                    seen.add(y)
                    stack.append(y)
        seen.discard(m)
        out[m] = sorted(seen)
    return out


def index_refs(path, tree):
    """(name, line, kind, info) for every identifier read in the module.

    kind "rt": inside a function or lambda body (runs when called);
    kind "hdr": in a def header at import time (decorator, default): info = function body lines;
    kind "it": class body or module level: info = (module, class qualname, assigned names,
               names mentioned in the access chain, statement line).
    """
    mod = module_name(path)
    refs = []

    def rec(name, node, mentions, ctx):
        in_func, classes, stmt, hdr = ctx
        if in_func:
            refs.append((name, node.lineno, "rt", None))
        elif hdr is not None:
            body = list(range(hdr.body[0].lineno, hdr.end_lineno + 1)) if hdr.body else []
            refs.append((name, node.lineno, "hdr", (path, body)))
        else:
            names = _assigned_names(stmt) if stmt is not None else None
            refs.append((name, node.lineno, "it", (mod, ".".join(classes) or None, names, mentions,
                                                   stmt.lineno if stmt is not None else 0)))

    def walk(node, ctx):
        in_func, classes, stmt, hdr = ctx
        if isinstance(node, ast.stmt) and not in_func:
            stmt = node
            ctx = (in_func, classes, stmt, hdr)
        if isinstance(node, FUNC):
            hctx = (in_func, classes, stmt, None if in_func else node)
            for d in node.decorator_list:
                walk(d, hctx)
            walk(node.args, hctx)
            if node.returns is not None:
                walk(node.returns, hctx)
            for st in node.body:
                walk(st, (True, classes, stmt, None))
            return
        if isinstance(node, ast.Lambda):
            walk(node.args, ctx)
            walk(node.body, (True, classes, stmt, None))
            return
        if isinstance(node, ast.ClassDef):
            for d in node.decorator_list + node.bases + [k.value for k in node.keywords]:
                walk(d, ctx)
            inner = classes if in_func else classes + [node.name]
            for st in node.body:
                walk(st, (in_func, inner, stmt, hdr))
            return
        if isinstance(node, ast.Name):
            rec(node.id, node, (node.id,), ctx)
            return
        if isinstance(node, ast.Attribute):
            chain = []
            v = node
            while isinstance(v, ast.Attribute):
                chain.append(v.attr)
                v = v.value
            if isinstance(v, ast.Name):
                chain.append(v.id)
            rec(node.attr, node, tuple(chain), ctx)
            walk(node.value, ctx)
            return
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.isidentifier():
            rec(node.value, node, (node.value,), ctx)
            return
        for child in ast.iter_child_nodes(node):
            walk(child, ctx)

    for st in tree.body:
        walk(st, (False, [], None, None))
    return refs


def build_refs(root, files, lines, import_lines):
    rt = defaultdict(int)
    it = defaultdict(list)
    imp = set()  # names read by function code that runs at import time (metaclasses, builders)
    for f in files:
        try:
            tree = ast.parse(open(os.path.join(root, f), encoding="utf-8").read())
        except SyntaxError:
            continue
        fl = lines.get(f, {})
        il = import_lines.get(f, ())
        for name, line, kind, info in index_refs(f, tree):
            if kind == "rt" and line in il:
                imp.add(name)
            if kind == "rt":
                b = fl.get(line)
                if b:
                    rt[name] |= b
            elif kind == "hdr":
                it[name].append(("hdr",) + info)
            else:
                it[name].append(("it",) + info)
    return rt, it, imp


def test_class_types(root, num, ids):
    """Test classes as 'types': tests of the class and of its subclasses."""
    by_class = defaultdict(int)
    for nodeid, n in num.items():
        parts = nodeid.split("::")
        if len(parts) >= 3:
            by_class[(parts[0], parts[1])] |= 1 << n
    # class -> bases by simple name, per test module
    bases = {}
    files = sorted({k[0] for k in by_class} | {
        os.path.relpath(p, root).replace(os.sep, "/") for p in glob.glob(os.path.join(root, "tests", "**", "*.py"), recursive=True)})
    for f in files:
        try:
            tree = ast.parse(open(os.path.join(root, f), encoding="utf-8").read())
        except Exception:
            continue
        for st in tree.body:
            if isinstance(st, ast.ClassDef):
                bs = []
                for b in st.bases:
                    if isinstance(b, ast.Name):
                        bs.append(b.id)
                    elif isinstance(b, ast.Attribute):
                        bs.append(b.attr)
                bases[(f, st.name)] = bs
    # closure: a class's tests include every subclass's tests (matched by simple name)
    out = defaultdict(int)
    children = defaultdict(set)
    for (f, c), bs in bases.items():
        for b in bs:
            children[b].add((f, c))
    for (f, c) in bases:
        bits = by_class.get((f, c), 0)
        stack = [c]
        seen = {c}
        while stack:
            x = stack.pop()
            for (f2, c2) in children.get(x, ()):
                bits |= by_class.get((f2, c2), 0)
                if c2 not in seen:
                    seen.add(c2)
                    stack.append(c2)
        if bits:
            out[f"{module_name(f)}:{c}"] = bits
    return out


def main():
    cov_dir, closure_path, root, tests_json, out = sys.argv[1:6]
    tj = json.load(open(tests_json))
    tests = tj["tests"]
    num = {t["id"]: t["number"] for t in tests}
    ms = [0] * len(tests)
    for t in tests:
        ms[t["number"]] = round(t["seconds"] * 1000)
    lines, import_lines, types, opens, import_opens = load_traces(cov_dir, num)
    files = py_files(root)

    # tests per test file
    file_tests = defaultdict(int)
    for t in tests:
        file_tests[t["file"]] |= 1 << t["number"]

    closure = json.load(open(closure_path))
    testfile_imports = defaultdict(int)
    for tf, mods in closure.items():
        for m in mods:
            testfile_imports[m] |= file_tests.get(tf, 0)

    # every test file is "imported" by its own tests
    for tf, bits in file_tests.items():
        testfile_imports[module_name(tf)] |= bits

    for k, v in test_class_types(root, num, tests).items():
        types[k] |= v

    file_users = {}
    for f in set(lines) | set(files):
        b = 0
        for v in lines.get(f, {}).values():
            b |= v
        file_users[f] = b
    mod_types = defaultdict(int)
    for name, bits in types.items():
        mod_types[name.split(":", 1)[0]] |= bits
    for f in files:
        file_users[f] = file_users.get(f, 0) | mod_types.get(module_name(f), 0)

    deps = static_imports(root, files)
    rdeps = reverse_closure(deps)
    rt, it, imp = build_refs(root, files, lines, import_lines)

    import hashlib
    from blast.pyimpact import file_info

    os.makedirs(os.path.join(out, "files"), exist_ok=True)
    file_index = {}
    for f in sorted(set(lines) | set(files) | set(import_lines)):
        info = None
        src = None
        if f.endswith(".py") and os.path.exists(os.path.join(root, f)):
            src = open(os.path.join(root, f), encoding="utf-8").read()
            try:
                info = file_info(f, src)
            except SyntaxError:
                info = None
        blob = {"lines": dict(lines.get(f, {})), "import_lines": frozenset(import_lines.get(f, ())),
                "info": info, "src": src}
        name = hashlib.sha1(f.encode()).hexdigest()[:16] + ".pkl"
        with open(os.path.join(out, "files", name), "wb") as fh:
            fh.write(zlib.compress(pickle.dumps(blob, protocol=pickle.HIGHEST_PROTOCOL), 9))
        file_index[f] = name

    core = {
        "n": len(tests),
        "ms": ms,
        "ids": [t["id"] for t in sorted(tests, key=lambda t: t["number"])],
        "file_index": file_index,
        "types": dict(types),
        "opens": dict(opens),
        "import_opens": dict(import_opens),
        "file_users": file_users,
        "rdeps": rdeps,
        "testfile_imports": dict(testfile_imports),
        "modules": {module_name(f): f for f in files},
        "spawners": opens.get("<spawn>", 0),
    }
    with open(os.path.join(out, "core.pkl"), "wb") as fh:
        fh.write(zlib.compress(pickle.dumps(core, protocol=pickle.HIGHEST_PROTOCOL), 9))
    with open(os.path.join(out, "refs.pkl"), "wb") as fh:
        fh.write(zlib.compress(pickle.dumps({"refs_rt": dict(rt), "refs_it": dict(it), "refs_imp": imp},
                                            protocol=pickle.HIGHEST_PROTOCOL), 9))
    total = sum(os.path.getsize(os.path.join(dp, x)) for dp, _, fs in os.walk(out) for x in fs)
    print(f"wrote {out}: {total} bytes; files={len(file_index)} types={len(types)} "
          f"opens={len(opens)} refs_rt={len(rt)} refs_it={len(it)}", file=sys.stderr)


if __name__ == "__main__":
    main()
