"""Which entries of class-level tables does each test look up? (build time only)

    python tools/key_trace.py TESTS_JSON OUT_DIR JOBS     (inside the scorer image)

sqlglot keeps much of its behaviour in dict tables assigned in class bodies: a Generator's
TRANSFORMS and TYPE_MAPPING, a Parser's FUNCTIONS, a Tokenizer's KEYWORDS and NUMERIC_ESCAPES.
tools/attr_trace.py records which tests load such an attribute, but a patch that changes one
entry only matters to a test that looks that entry up (or reads the table as a whole).

This tracer rewrites, at import, every assignment in a sqlglot class body or at module level,
and every assignment to a class attribute inside a function (the derived tables a metaclass
builds, such as a tokenizer's keyword trie or a dialect's escape tables):

    NAME = <value>        ->  NAME = __blast_table__(<value>, "NAME")
    cls.NAME = <value>    ->  cls.NAME = __blast_attr_table__(cls, "NAME", <value>)

and these return a dict subclass (same content) when the value is a plain dict, or a set /
frozenset subclass when it is a plain set (KEY_SETS=0 turns sets off), or a list subclass for
a plain list (KEY_LISTS=0), provided nothing else
holds the value (otherwise reads through the other reference would go unrecorded). A nested
dict held only by its parent is wrapped as part of the same table, and reads inside it are
recorded by key path; the paths of those nested dicts go to _import.json ("nodes").
The object is tracked wherever it flows (a Tokenizer hands its tables to the tokenizer core,
for example), and records per test:

    value reads    d[k], d.get(k)                     (the key, found or not)
    presence reads k in d                             (depend only on whether the key is there)
    len reads      len(d)                             (depend only on the number of entries)
    truth reads    if d, d or other                   (depend only on whether it is empty)
    whole reads    iteration, keys/values/items, comparisons, merges, mutation

`dict(table)` and `table.copy()` return a linked copy that keeps recording against the table:
that is how the generator builds its dispatch table from TRANSFORMS.

A table that a metaclass replaces after the class body, or that is not a plain dict, is not
tracked and keeps the attribute-level rule. Like the other tracers: a fork server imports
sqlglot once and forks one child per test, collection included.
"""

import ast
import builtins
import gc
import importlib
import importlib.abc
import importlib.machinery
import json
import os
import sys

ROOT = "/opt/sqlglot/"
TABLES: list = []
KEYED: set = set()      # (table, key path) whose value was read: d[k], d.get(k)
HAS: set = set()        # (table, key path) whose presence was read: k in d
NODES: dict = {}        # table -> key paths of the nested dicts wrapped inside it
WHOLE: set = set()
LEN: set = set()        # (table, node path) whose number of entries was read: len(d)
BOOLS: set = set()      # (table, node path) whose truth value was read: `if d`, `d or other`


def keyrepr(k):
    """Process-independent name of a table key; None if it has none."""
    t = type(k)
    # keys that compare equal must get the same name whatever their type: True and 1,
    # 1 and 1.0, a str-valued enum member and its string
    if isinstance(k, str):
        return "s:" + str.__str__(k)
    if isinstance(k, (bool, int)):
        return f"n:{int(k)}"
    if isinstance(k, float):
        return f"n:{int(k)}" if k.is_integer() else f"n:{float(k)!r}"
    if k is None or t is bytes:
        return f"p:{t.__name__}:{k!r}"
    if isinstance(k, type):
        return f"c:{k.__module__}:{k.__qualname__}"
    import enum

    if isinstance(k, enum.Enum):
        return f"e:{t.__module__}:{t.__qualname__}.{k.name}"
    if t in (tuple, frozenset):
        parts = [keyrepr(x) for x in k]
        if any(p is None for p in parts):
            return None
        return ("t:(" if t is tuple else "f:(") + ",".join(parts if t is tuple else sorted(parts)) + ")"
    return None


class TDict(dict):
    """A tracked dict: the whole table (_p == ()) or a nested dict inside it at key path _p."""

    __slots__ = ("_i", "_p")
    __hash__ = None

    def __getitem__(self, k):
        KEYED.add((self._i, self._p + (k,)))
        return dict.__getitem__(self, k)

    def get(self, k, default=None):
        KEYED.add((self._i, self._p + (k,)))
        return dict.get(self, k, default)

    def __contains__(self, k):
        HAS.add((self._i, self._p + (k,)))
        return dict.__contains__(self, k)

    def __len__(self):
        LEN.add((self._i, self._p))
        return dict.__len__(self)

    def __bool__(self):
        BOOLS.add((self._i, self._p))
        return dict.__len__(self) > 0

    def __iter__(self):
        WHOLE.add(self._i)
        return dict.__iter__(self)

    def __reversed__(self):
        WHOLE.add(self._i)
        return dict.__reversed__(self)

    def keys(self):
        WHOLE.add(self._i)
        return dict.keys(self)

    def values(self):
        WHOLE.add(self._i)
        return dict.values(self)

    def items(self):
        WHOLE.add(self._i)
        return dict.items(self)

    def copy(self):
        return _copy(self)

    def __eq__(self, other):
        WHOLE.add(self._i)
        return dict.__eq__(self, other)

    def __ne__(self, other):
        WHOLE.add(self._i)
        return dict.__ne__(self, other)

    def __or__(self, other):
        WHOLE.add(self._i)
        return dict.__or__(self, other)

    def __ror__(self, other):
        WHOLE.add(self._i)
        return dict.__ror__(self, other)

    def __ior__(self, other):
        WHOLE.add(self._i)
        return dict.__ior__(self, other)

    def __repr__(self):
        WHOLE.add(self._i)
        return dict.__repr__(self)

    def __reduce_ex__(self, protocol):
        WHOLE.add(self._i)
        return (dict, (dict.copy(self),))

    def __reduce__(self):
        WHOLE.add(self._i)
        return (dict, (dict.copy(self),))

    # a test that changes a table depends on all of it
    def __setitem__(self, k, v):
        WHOLE.add(self._i)
        return dict.__setitem__(self, k, v)

    def __delitem__(self, k):
        WHOLE.add(self._i)
        return dict.__delitem__(self, k)

    def pop(self, *a):
        WHOLE.add(self._i)
        return dict.pop(self, *a)

    def popitem(self):
        WHOLE.add(self._i)
        return dict.popitem(self)

    def setdefault(self, *a):
        WHOLE.add(self._i)
        return dict.setdefault(self, *a)

    def update(self, *a, **k):
        WHOLE.add(self._i)
        return dict.update(self, *a, **k)

    def clear(self):
        WHOLE.add(self._i)
        return dict.clear(self)


class TCopy(TDict):
    """A copy of a tracked table made with dict(table) or table.copy(), as the generator's
    dispatch table is. What a lookup in the copy returns still comes from the table's entry
    for that key (or its absence), so reads are recorded against the table; writes change
    the private copy only."""

    __slots__ = ()

    def __setitem__(self, k, v):
        return dict.__setitem__(self, k, v)

    def __delitem__(self, k):
        return dict.__delitem__(self, k)

    def pop(self, *a):
        if a:
            KEYED.add((self._i, self._p + (a[0],)))
        return dict.pop(self, *a)

    def setdefault(self, *a):
        if a:
            KEYED.add((self._i, self._p + (a[0],)))
        return dict.setdefault(self, *a)

    def update(self, *a, **k):
        return dict.update(self, *a, **k)

    def popitem(self):
        WHOLE.add(self._i)
        return dict.popitem(self)

    def clear(self):
        return dict.clear(self)

    def __ior__(self, other):
        dict.update(self, other)
        return self


def _copy(table):
    c = TCopy()
    # through the base class's own view, so that copying records no reads (dict.copy on a
    # subclass that overrides __iter__ would go through keys() and __getitem__)
    dict.update(c, dict.items(table))
    c._i = table._i
    c._p = table._p
    return c


def _dictcopy(*args, **kwargs):
    if len(args) == 1 and not kwargs and isinstance(args[0], TDict):
        return _copy(args[0])
    return dict(*args, **kwargs)


def _whole(self):
    WHOLE.add(self._i)


def _recording(base, names, record=_whole):
    """Methods of `base` that first record a read of the whole table."""
    out = {}
    for n in names:
        def method(self, *a, _f=getattr(base, n), **k):
            record(self)
            for x in a:   # the other operand is read by C code, without a call to its methods
                _seen(x)
            return _f(self, *a, **k)
        method.__name__ = n
        out[n] = method
    return out


_SET_READS = ("__iter__", "__or__", "__ror__", "__and__", "__rand__", "__sub__", "__rsub__", "__xor__",
              "__rxor__", "__eq__", "__ne__", "__le__", "__lt__", "__ge__", "__gt__", "union",
              "intersection", "difference", "symmetric_difference", "issubset", "issuperset",
              "isdisjoint", "copy", "__repr__")
_SET_WRITES = ("add", "discard", "remove", "pop", "clear", "update", "intersection_update",
               "difference_update", "symmetric_difference_update", "__ior__", "__iand__", "__isub__",
               "__ixor__")


def _set_ns(base, extra=()):
    ns = _recording(base, _SET_READS + tuple(extra))

    def __contains__(self, k, _f=base.__contains__):
        HAS.add((self._i, (k,)))
        return _f(self, k)

    def __len__(self, _f=base.__len__):
        LEN.add((self._i, ()))
        return _f(self)

    def __bool__(self, _f=base.__len__):
        BOOLS.add((self._i, ()))
        return _f(self) > 0

    def __reduce_ex__(self, protocol, _it=base.__iter__):
        WHOLE.add(self._i)
        return (base, (list(_it(self)),))

    def __reduce__(self, _it=base.__iter__):
        WHOLE.add(self._i)
        return (base, (list(_it(self)),))

    ns.update(__contains__=__contains__, __len__=__len__, __bool__=__bool__,
              __reduce_ex__=__reduce_ex__, __reduce__=__reduce__, __slots__=("_i",))
    return ns


# Sets: membership is a presence read of that element; everything else reads the whole set.
# C code that is handed one of these as an argument (set(x), a.update(x), a.issubset(x), {*x},
# a |= x) reads it without calling any method: the import rewriter marks those places.
TSet = type("TSet", (set,), {**_set_ns(set, _SET_WRITES), "__hash__": None})
_fz = _set_ns(frozenset)
_fz["__hash__"] = _recording(frozenset, ("__hash__",))["__hash__"]
TFrozen = type("TFrozen", (frozenset,), _fz)
_TRACKED_SETS = (TSet, TFrozen)


class TList(list):
    """A tracked list: an element read by index records that index (a negative index also
    records the length), the length and the truth value are recorded as such, and everything
    else (iteration, slices, searching, comparison, copying, any change) reads the whole list.
    C code that is handed a list subclass reads it through these methods except in `x + lst`
    (and `x += lst`), whose right operand the import rewriter marks."""

    __slots__ = ("_i",)
    __hash__ = None

    def __getitem__(self, k):
        if type(k) is int:
            if k < 0:
                LEN.add((self._i, ()))
                k += list.__len__(self)
            KEYED.add((self._i, (k,)))
        else:
            WHOLE.add(self._i)
        return list.__getitem__(self, k)

    def __len__(self):
        LEN.add((self._i, ()))
        return list.__len__(self)

    def __bool__(self):
        BOOLS.add((self._i, ()))
        return list.__len__(self) > 0

    def __reduce_ex__(self, protocol):
        WHOLE.add(self._i)
        return (list, (list(list.__iter__(self)),))

    def __reduce__(self):
        WHOLE.add(self._i)
        return (list, (list(list.__iter__(self)),))


for _n in ("__iter__", "__reversed__", "__contains__", "__eq__", "__ne__", "__lt__", "__le__", "__gt__",
           "__ge__", "__add__", "__mul__", "__rmul__", "__iadd__", "__imul__", "__repr__",
           "copy", "index", "count", "append", "extend", "insert", "remove", "pop", "clear", "sort",
           "reverse", "__setitem__", "__delitem__"):
    setattr(TList, _n, _recording(list, (_n,))[_n])
del _n
_TRACKED = (TSet, TFrozen, TDict, TCopy, TList)
LISTS = os.environ.get("KEY_LISTS", "1") != "0"
SETS = os.environ.get("KEY_SETS", "1") != "0"
MODULE = os.environ.get("KEY_MODULE", "1") != "0"   # tables assigned at module level too
DERIVED = os.environ.get("KEY_DERIVED", "1") != "0"  # tables a class gets assigned inside a function


def _seen(x):
    """Marks a tracked set that is about to be read by C code as read whole; returns x."""
    t = type(x)
    if t in _TRACKED:
        WHOLE.add(x._i)
    elif t in (list, tuple):
        for e in x:
            if type(e) in _TRACKED:
                WHOLE.add(e._i)
    return x


def _probe2(value, name):
    fresh = sys.getrefcount(value)
    return fresh


def _probe3(obj, name, value):
    fresh = sys.getrefcount(value)
    return fresh


def _probe_nested(value):
    for k in list(value):
        v = dict.__getitem__(value, k)
        refs = sys.getrefcount(v)
        return refs


# References a freshly built value has at the point where it is checked (the caller's stack,
# the argument, the check itself): one more means something else holds it, and wrapping a copy
# would let reads through that other reference go unrecorded.
_FRESH2 = _probe2({}, "x")
_FRESH3 = _probe3(None, "x", {})
_FRESH_NESTED = _probe_nested({0: {}})


def _wrap_dict(value, i, path):
    """A TDict with value's entries; a nested plain dict held by value alone is wrapped too,
    as part of the same table, so that reads inside it are recorded by key path (a keyword
    trie is a nest of dicts, walked one character at a time)."""
    new = TDict()
    new._i = i
    new._p = path
    for k in list(value):
        v = dict.__getitem__(value, k)
        refs = sys.getrefcount(v)   # measured as _probe_nested measures it
        if type(v) is dict and refs <= _FRESH_NESTED and keyrepr(k) is not None:
            NODES.setdefault(i, set()).add(path + (k,))
            v = _wrap_dict(v, i, path + (k,))
        dict.__setitem__(new, k, v)
    return new


def _wrap(value, fresh):
    t = type(value)
    if t is dict:
        if not fresh:
            return value
        new = _wrap_dict(value, len(TABLES), ())
    elif t is TCopy:
        # `NAME = OTHER.copy()`: a table of its own (its nested values stay shared)
        new = TDict(dict.items(value))
        new._p = ()
    elif LISTS and t is list and fresh:
        new = TList(value)
    elif SETS and t is set and fresh:
        new = TSet(value)
    elif SETS and t is frozenset and fresh:
        new = TFrozen(value)
    else:
        return value
    new._i = len(TABLES)
    TABLES.append(new)
    return new


def _table(value, name):
    fresh = sys.getrefcount(value) <= _FRESH2   # measured as _probe2 measures it
    return _wrap(value, fresh)


def _attr_table(obj, name, value):
    """`Name.ATTR = value` inside a function (a metaclass building a class's derived tables,
    such as a tokenizer's keyword trie): tracked like a class-body table when obj is a class."""
    if not isinstance(obj, type) or name == "arg_types" or (name.startswith("__") and name.endswith("__")):
        return value
    fresh = sys.getrefcount(value) <= _FRESH3   # measured as _probe3 measures it
    return _wrap(value, fresh)


builtins.__blast_table__ = _table
builtins.__blast_attr_table__ = _attr_table
builtins.__blast_dictcopy__ = _dictcopy
builtins.__blast_seen__ = _seen


_SET_ARG_METHODS = {"update", "union", "intersection", "difference", "symmetric_difference", "issubset",
                    "issuperset", "isdisjoint", "intersection_update", "difference_update",
                    "symmetric_difference_update"}


class _Rewrite(ast.NodeTransformer):
    """Wrap the value of every assignment made directly in a sqlglot class body, and mark the
    places where C code reads a set it is handed."""

    tables = True

    def visit_ClassDef(self, node):
        self.generic_visit(node)   # nested classes first
        if self.tables:
            self._block(node.body)
        return node

    def visit_Module(self, node):
        self.generic_visit(node)
        if self.tables and MODULE:
            self._block(node.body)
        return node

    def visit_Assign(self, node):
        self.generic_visit(node)
        t = node.targets[0] if len(node.targets) == 1 else None
        if (self.tables and DERIVED and isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)
                and isinstance(t.ctx, ast.Store)):
            node.value = ast.copy_location(
                ast.Call(func=ast.Name(id="__blast_attr_table__", ctx=ast.Load()),
                         args=[ast.Name(id=t.value.id, ctx=ast.Load()), ast.Constant(t.attr), node.value],
                         keywords=[]), node.value)
        return node

    def visit_Call(self, node):
        self.generic_visit(node)
        f = node.func
        if (isinstance(f, ast.Name) and f.id == "dict" and len(node.args) == 1 and not node.keywords
                and not isinstance(node.args[0], ast.Starred)):
            node.func = ast.copy_location(ast.Name(id="__blast_dictcopy__", ctx=ast.Load()), f)
        elif SETS and ((isinstance(f, ast.Name) and f.id in ("set", "frozenset"))
                       or (isinstance(f, ast.Attribute) and (f.attr in _SET_ARG_METHODS or (
                           f.attr.startswith("__") and f.attr.endswith("__"))))):
            # C code will read a set passed here without calling its methods (an explicit
            # dunder call such as [].__add__(x) reads a list argument the same way)
            node.args = [self._mark(a) for a in node.args]
        return node

    def visit_Set(self, node):
        self.generic_visit(node)
        if SETS:
            node.elts = [self._mark(e) if isinstance(e, ast.Starred) else e for e in node.elts]
        return node

    def visit_BinOp(self, node):
        self.generic_visit(node)
        # `plain | table`, `table_a - table_b`: Python only calls the right operand's method
        # when its type is a subclass of the left one's, so a set against a frozenset, or two
        # tables, would be read by C code unseen
        if SETS and isinstance(node.op, (ast.BitOr, ast.BitAnd, ast.Sub, ast.BitXor, ast.Add)) \
                and not isinstance(node.right, ast.Constant):
            node.right = self._mark(node.right)
        return node

    def visit_Compare(self, node):
        self.generic_visit(node)
        if SETS:
            node.comparators = [
                self._mark(c) if isinstance(op, (ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE))
                and not isinstance(c, ast.Constant) else c
                for op, c in zip(node.ops, node.comparators)]
        return node

    def visit_AugAssign(self, node):
        self.generic_visit(node)
        if SETS and isinstance(node.op, (ast.BitOr, ast.BitAnd, ast.Sub, ast.BitXor, ast.Add)):
            node.value = self._mark(node.value)
        return node

    @staticmethod
    def _mark(node):
        if isinstance(node, ast.Starred):
            node.value = _Rewrite._mark(node.value)
            return node
        return ast.copy_location(
            ast.Call(func=ast.Name(id="__blast_seen__", ctx=ast.Load()), args=[node], keywords=[]), node)

    def _block(self, body):
        for st in body:
            if isinstance(st, ast.Assign) and all(isinstance(t, ast.Name) for t in st.targets):
                st.value = self._wrap(st.value, st.targets[0].id)
            elif isinstance(st, ast.AnnAssign) and st.value is not None and isinstance(st.target, ast.Name):
                st.value = self._wrap(st.value, st.target.id)
            elif isinstance(st, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
                for f in ("body", "orelse", "finalbody"):
                    self._block(getattr(st, f, []) or [])
                for h in getattr(st, "handlers", []) or []:
                    self._block(h.body)

    @staticmethod
    def _wrap(value, name):
        if name == "arg_types" or (name.startswith("__") and name.endswith("__")):
            return value   # read on every node; maps/args.bin already knows its readers
        return ast.copy_location(
            ast.Call(func=ast.Name(id="__blast_table__", ctx=ast.Load()),
                     args=[value, ast.Constant(name)], keywords=[]), value)


class _Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        path = self.get_filename(fullname)
        return self.source_to_code(self.get_data(path), path)

    def source_to_code(self, data, path, *, _optimize=-1):
        src = data.decode("utf-8") if isinstance(data, bytes) else data
        rw = _Rewrite()
        rw.tables = path.startswith(ROOT + "sqlglot/")   # test modules: only the set marks
        tree = rw.visit(ast.parse(src, path))
        ast.fix_missing_locations(tree)
        return compile(tree, path, "exec", dont_inherit=True, optimize=_optimize)


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if not (name == "sqlglot" or name.startswith("sqlglot.")
                or (SETS and (name == "tests" or name.startswith("tests.")))):
            return None
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        if spec is None or not isinstance(spec.loader, importlib.machinery.SourceFileLoader):
            return spec
        if not (spec.origin or "").startswith(ROOT):
            return spec
        spec.loader = _Loader(spec.loader.name, spec.loader.path)
        return spec


def table_index():
    """{table number: ["module:Class.ATTR" or "module:NAME", ...]} for tracked tables that are
    class attributes or module globals."""
    index = {}
    for mname, mod in sorted(sys.modules.items()):
        if mod is None or not (mname == "sqlglot" or mname.startswith("sqlglot.")):
            continue
        for attr, v in list(vars(mod).items()):
            if (type(v) is TDict and v._p == ()) or type(v) in _TRACKED_SETS or type(v) is TList:
                index.setdefault(v._i, []).append(f"{mname}:{attr}")
    for cls in [o for o in gc.get_objects() if isinstance(o, type)]:
        if not (getattr(cls, "__module__", "") or "").startswith("sqlglot"):
            continue
        for attr, v in list(vars(cls).items()):
            if (type(v) is TDict and v._p == ()) or type(v) in _TRACKED_SETS or type(v) is TList:
                index.setdefault(v._i, []).append(f"{cls.__module__}:{cls.__qualname__}.{attr}")
    return index


def pathname(path):
    """Name of a key path: the key's own name for a top-level key; for a key inside a nested
    dict, "P:" and the names of the keys along the path joined by \x1f, so that the name of a
    path's parent node is the name with its last part cut off (see parent_name). None if a key
    has no stable name. (Same function in tools/key_trace.py and blast/keytab.py.)"""
    parts = [keyrepr(x) for x in path]
    if any(p is None for p in parts):
        return None
    if len(parts) == 1:
        return parts[0]   # a top-level name is never split, whatever it holds
    if any("\x1f" in p for p in parts):
        return None
    return "P:" + "\x1f".join(parts)


def parent_name(name):
    """Name of the node that holds the entry `name`: "" for the table itself."""
    if not name.startswith("P:"):
        return ""
    head = name[2:].rsplit("\x1f", 1)[0]
    return head if "\x1f" not in head else "P:" + head


def dump_reads():
    whole = set(WHOLE)
    out = {}
    for label, pairs in (("keyed", KEYED), ("has", HAS)):
        got = set()
        for i, path in pairs:
            r = pathname(path)
            if r is None:
                whole.add(i)   # a key without a stable name: count the table as read whole
            else:
                got.add((i, r))
        out[label] = sorted(got)
    for label, pairs in (("len", LEN), ("bool", BOOLS)):
        got = set()
        for i, path in pairs:
            r = pathname(path) if path else ""
            if r is None:
                whole.add(i)
            else:
                got.add((i, r))
        out[label] = sorted(got)
    out["whole"] = sorted(whole)
    return out


def main():
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
    index = table_index()
    imp = dump_reads()
    nodes = {str(i): sorted(filter(None, (pathname(x) for x in NODES.get(i, ())))) for i in index}
    json.dump({"tables": {str(i): v for i, v in sorted(index.items())}, "import": imp, "created": len(TABLES),
               "nodes": {i: v for i, v in nodes.items() if v}},
              open(os.path.join(out_dir, "_import.json"), "w"))
    print(f"{len(TABLES)} tables wrapped ({sum(1 for x in TABLES if type(x) is TDict)} dicts), "
          f"{len(index)} are class attributes "
          f"({sum(len(v) for v in index.values())} names)", flush=True)

    tests = json.load(open(tests_json))["tests"]
    only = set(filter(None, os.environ.get("KEY_ONLY", "").split(",")))
    todo = [t for t in sorted(tests, key=lambda t: -t["seconds"])
            if (not only or str(t["number"]) in only)
            and not os.path.exists(os.path.join(out_dir, f"{t['number']}.json"))]
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

                signal.alarm(int(os.environ.get("KEY_TIMEOUT", "1500")))
                devnull = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull, 1)
                os.dup2(devnull, 2)
                KEYED.clear()
                HAS.clear()
                WHOLE.clear()
                LEN.clear()
                BOOLS.clear()
                # --assert=plain: pytest's assertion rewriter would otherwise load the test
                # modules itself, without the set marks
                code = pytest.main([t["id"], "-q", "-p", "no:cacheprovider"] + (["--assert=plain"] if SETS else []))
                out = os.path.join(out_dir, f"{t['number']}.json")
                if int(code) == 0:
                    with open(out + ".tmp", "w") as fh:
                        json.dump(dump_reads(), fh)
                    os.replace(out + ".tmp", out)
                os._exit(int(code))
            except BaseException:
                os._exit(99)
        running[pid] = t
    while running:
        reap()
    print(f"key-traced {len(todo)} tests, {bad} non-zero exits", flush=True)


if __name__ == "__main__":
    main()
