"""Fingerprint what importing sqlglot leaves behind: every module global and class attribute.

Two imports whose fingerprints agree key by key left the same state, so a patch that does
not change any fingerprint has no import-time effect. Functions are identified by where they
are defined (module, qualified name, line in the pinned file) plus their defaults and closure
contents, not by their bytecode: a changed function body is a run-time matter that line
coverage covers, and must not show up here.

Everything is hashed structurally and deterministically (no ids, no hash()-ordered
iteration: sets are sorted by member fingerprint), so a baseline computed when the maps are
built can be compared with a run in another process, given the same PYTHONHASHSEED-independent
walk.
"""

import hashlib
import sys
import types

_SKIP_ATTRS = {"__doc__", "__annotations__", "__firstlineno__", "__static_attributes__",
               "__dict__", "__weakref__", "__module__", "__qualname__", "__builtins__",
               "__cached__", "__loader__", "__spec__", "__file__", "__path__", "__name__",
               "__package__", "__annotate__", "__annotate_func__", "__annotations_cache__",
               "__type_params__", "__orig_bases__", "__parameters__", "__abstractmethods__",
               "_abc_impl", "__match_args__"}
_PRIM = (type(None), bool, int, float, complex, str, bytes)


class Walker:
    def __init__(self, line_map=None, mode=0):
        """line_map: {source file: function(new line) -> pinned line} for patched files.
        mode 0 compares everything exactly; mode 1 ignores dict insertion order; mode 2 also
        ignores list and tuple order. Some values are built by iterating sets of classes,
        whose order differs from process to process: those keys use the strictest mode that
        is stable across runs of the pinned code (see tools/build_imstate.py)."""
        self.mode = mode
        self.memo = {}
        self.stack = set()
        self.line_map = line_map or {}
        self.classes = {}

    def _h(self, *parts):
        h = hashlib.blake2b(digest_size=8)
        for p in parts:
            h.update(p if isinstance(p, bytes) else str(p).encode("utf-8", "surrogatepass"))
            h.update(b"\x00")
        return h.digest()

    def _line(self, code):
        f = code.co_filename
        m = self.line_map.get(f)
        return m(code.co_firstlineno) if m else code.co_firstlineno

    def fp(self, o):
        t = type(o)
        if t in _PRIM:
            return self._h("p", t.__name__, repr(o))
        i = id(o)
        got = self.memo.get(i)
        if got is not None:
            return got
        if i in self.stack:
            return self._h("cycle", t.__name__)
        self.stack.add(i)
        try:
            r = self._fp(o, t)
        finally:
            self.stack.discard(i)
        self.memo[i] = r
        return r

    def _fp(self, o, t):
        h = self._h
        fp = self.fp
        if isinstance(o, type):
            mod = getattr(o, "__module__", "") or ""
            if mod.startswith("sqlglot"):
                self.classes.setdefault(f"{mod}:{o.__qualname__}", o)
            return h("T", mod, o.__qualname__)
        if t is types.FunctionType:
            c = o.__code__
            return h("F", o.__module__, o.__qualname__, c.co_filename, self._line(c),
                     fp(o.__defaults__), fp(o.__kwdefaults__),
                     *[fp(_cell(x)) for x in (o.__closure__ or ())])
        if t is types.MethodType:
            return h("M", fp(o.__self__), fp(o.__func__))
        if t in (types.BuiltinFunctionType, types.BuiltinMethodType, types.WrapperDescriptorType,
                 types.MethodWrapperType, types.MethodDescriptorType, types.ClassMethodDescriptorType,
                 types.GetSetDescriptorType, types.MemberDescriptorType):
            return h("B", getattr(o, "__qualname__", repr(t)), getattr(o, "__objclass__", ""))
        if t is types.ModuleType:
            return h("mod", o.__name__)
        if t in (classmethod, staticmethod):
            return h(t.__name__, fp(o.__func__))
        if t is property:
            return h("prop", fp(o.fget), fp(o.fset), fp(o.fdel))
        if t in (dict, types.MappingProxyType) or isinstance(o, dict):
            if self.mode == 0:
                return h("D", t.__name__, *[x for k, v in o.items() for x in (fp(k), fp(v))])
            return h("D", t.__name__, *sorted(fp(k) + fp(v) for k, v in o.items()))
        if t in (list, tuple) or isinstance(o, (list, tuple)):
            if self.mode == 2:
                return h("L", t.__name__, *sorted(fp(x) for x in o))
            return h("L", t.__name__, *[fp(x) for x in o])
        if t in (set, frozenset) or isinstance(o, (set, frozenset)):
            return h("S", t.__name__, *sorted(fp(x) for x in o))
        mod = getattr(t, "__module__", "") or ""
        name = f"{mod}.{t.__qualname__}"
        if name == "re.Pattern":
            return h("re", o.pattern, o.flags)
        if name == "functools.partial":
            return h("partial", fp(o.func), fp(o.args), fp(o.keywords))
        if isinstance(o, types.CodeType):
            return h("code", o.co_filename, o.co_qualname, self._line(o))
        import enum

        if isinstance(o, enum.Enum):
            return h("E", mod, t.__qualname__, o.name)
        if mod.startswith(("typing", "collections.abc", "types")) or name in (
                "builtins.ellipsis", "builtins.NotImplementedType"):
            return h("typing", repr(o))
        if name in ("_thread.RLock", "_thread.lock", "logging.Logger", "logging.RootLogger",
                    "threading.local", "_thread._local", "weakref.ReferenceType",
                    "_abc._abc_data", "functools._lru_cache_wrapper", "_contextvars.ContextVar"):
            if name == "functools._lru_cache_wrapper":
                return h("lru", fp(o.__wrapped__))
            return h("opaque", name)
        # any other object: its type and its attributes
        parts = ["I", mod, t.__qualname__]
        d = getattr(o, "__dict__", None)
        if isinstance(d, dict):
            for k in sorted(d):
                parts += [k, fp(d[k])]
        slots = []
        for k in t.__mro__:
            s = k.__dict__.get("__slots__", ())
            slots.extend([s] if isinstance(s, str) else s)
        for s in slots:
            if s in ("__dict__", "__weakref__"):
                continue
            try:
                v = getattr(o, s)
            except AttributeError:
                continue
            parts += [s, fp(v)]
        if d is None and not slots:
            parts.append(repr(o) if mod in ("builtins", "decimal", "datetime", "fractions") else "noattrs")
        return h(*parts)


def _cell(c):
    try:
        return c.cell_contents
    except ValueError:
        return "<empty cell>"


def snapshot(line_map=None, prefix="sqlglot", mode=0, registry=None):
    """{key: 8-byte fingerprint}. Keys: "module:name" for module globals, "module:Class.attr"
    for class attributes (every class of the package reachable from the modules).
    registry, if given, receives {key: object} so single keys can be re-fingerprinted."""
    w = Walker(line_map, mode)
    out = {}
    mods = sorted(m for m in sys.modules if (m == prefix or m.startswith(prefix + ".")) and sys.modules[m] is not None)
    out["<modules>"] = w._h(*mods)
    for m in mods:
        g = vars(sys.modules[m])
        for k in list(g):
            if k in _SKIP_ATTRS:
                continue
            out[f"{m}:{k}"] = w.fp(g[k])
            if registry is not None:
                registry[f"{m}:{k}"] = g[k]
    done = set()
    while True:
        todo = [k for k in w.classes if k not in done]
        if not todo:
            break
        for ck in sorted(todo):
            done.add(ck)
            cls = w.classes[ck]
            for k, v in list(vars(cls).items()):
                if k in _SKIP_ATTRS:
                    continue
                out[f"{ck}.{k}"] = w.fp(v)
                if registry is not None:
                    registry[f"{ck}.{k}"] = v
            out[f"{ck}.<bases>"] = w._h(*[f"{b.__module__}:{b.__qualname__}" for b in cls.__mro__])
    return out


def diff(baseline, line_map=None):
    """Keys whose fingerprint differs from `baseline` ({"fp": {key: (mode, fp)}, "unstable":
    keys}) in the modules currently imported. Added keys are reported as "+key"."""
    registry = {}
    strict = snapshot(line_map, registry=registry)
    fps = baseline["fp"]
    out = []
    walkers = {}
    for k, (mode, want) in fps.items():
        if mode == 0:
            if strict.get(k) != want:
                out.append(k)
            continue
        if k not in registry:
            out.append(k)
            continue
        w = walkers.get(mode)
        if w is None:
            w = walkers[mode] = Walker(line_map, mode)
        if w.fp(registry[k]) != want:
            out.append(k)
    unstable = set(baseline["unstable"])
    out += ["+" + k for k in strict if k not in fps and k not in unstable]
    return sorted(out)
