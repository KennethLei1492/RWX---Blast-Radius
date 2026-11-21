"""Fixtures a test class loads into a class attribute in its set-up.

tests/test_executor.py loads the TPC-H and TPC-DS query files in `setUpClass`:

    cls.tpch_sqls = list(load_sql_fixture_pairs("optimizer/tpc-h/tpc-h.sql"))
    cls.tpcds_sqls = list(load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))

so every one of its 60 tests opens both files and counts as a reader of them, although
only a few tests ever read those attributes. An edit to such a file can change another test
of the class in one way only: by making the set-up fail. For a patch that edits a fixture
loaded this way (and no test code), the tests of that class that never execute a line
reading the attribute are dropped, provided the patched file still loads with the pinned
loader; everything else stays as it was.

The facts come from the pinned test source (an assignment in `setUpClass` from a call of a
tests.helpers loader with a literal path, the fixture's name appearing nowhere else in that
module) and from line coverage (which tests executed a line that reads the attribute).
"""

import ast
import io
import os

_LOADERS = {"load_sql_fixture_pairs", "load_sql_fixtures"}
_cache = {}


def _module_facts(root, test_file):
    """[(class name, attribute, fixture path under tests/fixtures, loader name)] and the
    module's AST, for set-up loads of fixtures in a test module."""
    key = (root, test_file)
    if key in _cache:
        return _cache[key]
    try:
        with open(os.path.join(root, test_file), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
    except (OSError, SyntaxError, ValueError):
        _cache[key] = ([], None)
        return _cache[key]
    loads = []
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef):
            continue
        for fn in cls.body:
            if not (isinstance(fn, ast.FunctionDef) and fn.name == "setUpClass"):
                continue
            for st in ast.walk(fn):
                if not (isinstance(st, ast.Assign) and len(st.targets) == 1):
                    continue
                t = st.targets[0]
                if not (isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)):
                    continue
                call = st.value
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "list" \
                        and len(call.args) == 1:
                    call = call.args[0]
                if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                        and call.func.id in _LOADERS and len(call.args) == 1 and not call.keywords
                        and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str)):
                    continue
                loads.append((cls.name, t.attr, "tests/fixtures/" + call.args[0].value, call.func.id))
    _cache[key] = (loads, tree)
    return _cache[key]


def _mentions(tree, text):
    """How many string constants in the module contain `text`."""
    return sum(1 for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
               and text in n.value)


def _attr_load_lines(tree, attr):
    """Lines that read attribute `attr` (of anything), or name it in a string (getattr)."""
    lines = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute) and n.attr == attr and isinstance(n.ctx, ast.Load):
            lines.update(range(n.lineno, (n.end_lineno or n.lineno) + 1))
        elif isinstance(n, ast.Constant) and n.value == attr:
            return None   # read by name: not followed
    return lines


def _loads_ok(root, loader, rel, text):
    """Does the pinned tests.helpers loader accept the patched text of the fixture?"""
    try:
        with open(os.path.join(root, "tests", "helpers.py"), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
    except (OSError, SyntaxError, ValueError):
        return False
    wanted = {"_filter_comments", "_extract_meta", loader}
    fns = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in wanted]
    if {f.name for f in fns} != wanted:
        return False
    mod = ast.Module(body=fns, type_ignores=[])

    def _open(path, *a, **k):
        if os.path.normpath(path) == os.path.normpath(os.path.join("FIX", rel)):
            return io.StringIO(text)
        raise OSError(path)

    env = {"os": os, "open": _open, "FIXTURES_DIR": "FIX"}
    try:
        exec(compile(mod, "tests/helpers.py", "exec"), env)
        list(env[loader](rel))
    except Exception:  # noqa: BLE001  -- the set-up would fail: no reduction
        return False
    return True


_traced = {}


def _traced_readers(sel, test_file, cls_name, attr):
    """Bits of the tests that read the attribute in their own run (maps/setupreads.json,
    tools/setup_trace.py), or None when not traced: line coverage is used then."""
    if "map" not in _traced:
        try:
            import json

            with open(os.path.join(sel.dir, "setupreads.json"), encoding="utf-8") as fh:
                _traced["map"] = json.load(fh)
        except (OSError, ValueError):
            _traced["map"] = {}
    names = _traced["map"].get(f"{test_file}::{cls_name}", {}).get(attr)
    if names is None:
        return None
    index = {t: i for i, t in enumerate(sel.m["ids"])}
    b = 0
    for n in names:
        if n not in index:
            return None
        b |= 1 << index[n]
    return b


def setup_only_readers(sel, fixture, new_text, openers_bits):
    """Bits of the openers of `fixture` that depend on it only through a test class's set-up
    loading it into an attribute they never read; 0 if there are none or anything is unsure."""
    root = sel.root
    if root is None or new_text is None:
        return 0
    ids = sel.m["ids"]
    by_file = {}
    for i in range(openers_bits.bit_length()):
        if openers_bits >> i & 1:
            f, _, rest = ids[i].partition("::")
            by_file.setdefault(f, []).append((i, rest.split("::")[0]))
    drop = 0
    for test_file, members in by_file.items():
        loads, tree = _module_facts(root, test_file)
        if tree is None:
            continue
        for cls_name, attr, path, loader in loads:
            if path != fixture:
                continue
            if _mentions(tree, os.path.basename(fixture)) != 1:
                continue   # the file is named somewhere else in the module too
            lines = _attr_load_lines(tree, attr)
            if lines is None:
                continue
            rel = fixture[len("tests/fixtures/"):]
            if not _loads_ok(root, loader, rel, new_text):
                continue
            traced = _traced_readers(sel, test_file, cls_name, attr)
            readers = traced if traced is not None else sel.lines_users(test_file, sorted(lines))
            for i, c in members:
                if c == cls_name and not readers >> i & 1:
                    drop |= 1 << i
    return drop
