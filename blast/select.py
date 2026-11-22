"""Choose the tests a patch can break and pack them onto workers.

Map formats:
  * packed (maps/core.bin, written by tools/pack_maps.py): marshal + zlib, each file split
    into pages of lines and the name index split into buckets, so a commit loads only the
    few pages it touches;
  * plain (maps/core.pkl, written by tools/build_maps.py): pickled whole files.
"""

import marshal
import os
import sys
import zlib

from . import diffparse, equiv, pyimpact

SHARED_CONFIG = {"setup.cfg", "pyproject.toml", "tox.ini"}
CONFIG_FILES = {"setup.cfg", "pyproject.toml", "tox.ini", "pytest.ini", "conftest.py",
                "tests/__init__.py", "tests/dialects/__init__.py"}

_EMPTY = frozenset()


def _load(path):
    with open(path, "rb") as fh:
        data = zlib.decompress(fh.read())
    try:
        return marshal.loads(data)
    except (ValueError, EOFError, TypeError):
        import pickle

        return pickle.loads(data)


def _cmpswap():
    from . import cmpswap

    return cmpswap


class _NoAlt(Exception):
    """The import-diff selection cannot be used for this patch."""


def log(*a):
    print(*a, file=sys.stderr)


class PagedView:
    """Per-line facts about one pinned file, loaded page by page (packed maps)."""

    has_chains = True
    has_lam_info = True

    def __init__(self, sel, path, fid):
        self.sel = sel
        self.path = path
        self.fid = fid
        self.present = sel.m["pages"].get(fid, _EMPTY) if fid is not None else _EMPTY
        self.size = sel.m["page_size"]
        self._pages = {}
        self._src = False

    def _page(self, l):
        p = l // self.size
        pg = self._pages.get(p)
        if pg is None:
            if p in self.present:
                pg = _load(os.path.join(self.sel.dir, "f", f"{self.fid}_{p}.bin"))
            else:
                pg = {}
            self._pages[p] = pg
        return pg

    @property
    def src(self):
        if self._src is False:
            self._src = None
            if self.fid is not None and self.fid in self.sel.m["srcs"]:
                try:
                    self._src = _load(os.path.join(self.sel.dir, "f", f"{self.fid}.src"))
                except OSError:
                    self._src = None   # not shipped: Selector.read takes it from the pinned tree
        return self._src

    def bits(self, l):
        return self._page(l).get("bits", {}).get(l, 0)

    def import_line(self, l):
        return l in self._page(l).get("il", _EMPTY)

    def is_code(self, l):
        return l in self._page(l).get("code", _EMPTY)

    def cont(self, l):
        return l in self._page(l).get("cont", _EMPTY)

    def impacts(self, l):
        return self._page(l).get("imp", {}).get(l, ())

    def lam(self, l):
        return self._page(l).get("lam", {}).get(l)

    def chain(self, l):
        return self._page(l).get("chain", {}).get(l)


class BlobView:
    """The same questions answered from a whole-file pickled blob (plain maps)."""

    def __init__(self, blob):
        self.b = blob
        info = blob.get("info") or {}
        self.info = info
        self.has_chains = "chain" in info
        self.has_lam_info = "lam" in info
        self.src = blob.get("src")

    def bits(self, l):
        return self.b["lines"].get(l, 0)

    def import_line(self, l):
        return l in self.b["import_lines"]

    def is_code(self, l):
        return l in self.info.get("code", _EMPTY)

    def cont(self, l):
        return l in self.info.get("instr", _EMPTY)

    def impacts(self, l):
        return self.info.get("impacts", {}).get(l, ())

    def lam(self, l):
        return self.info.get("lam", {}).get(l)

    def chain(self, l):
        return self.info.get("chain", {}).get(l)


class Selector:
    def __init__(self, maps_dir, source_root=None):
        self.dir = maps_dir
        self.packed = os.path.exists(os.path.join(maps_dir, "core.bin"))
        self.m = _load(os.path.join(maps_dir, "core.bin" if self.packed else "core.pkl"))
        self._views = {}
        self._refs = None
        self._buckets = {}
        self.refine_constants = True
        # comparison-swap map (blast/cmpswap.py): imported only when its map exists, since
        # cmpswap's own imports cost ~0.1 s of CPU
        self._attr = False
        self._keys = False         # maps/keys.bin, loaded when a class-level table is edited
        self._has_keys = os.path.exists(os.path.join(maps_dir, "keys.bin"))
        self.table_edit = False    # last select() touched a traced dict table
        self._alt = False          # import-diff mode (select_alt)
        self._expected = set()     # fingerprint keys the patch changes directly
        self.uncertain = False     # last select() used an import-time fallback
        self._exact = False
        self._class_exact = False
        self._args = os.path.exists(os.path.join(maps_dir, "args.bin"))
        self._dflt = os.path.exists(os.path.join(maps_dir, "dflt.bin"))
        self._cmpt = os.path.exists(os.path.join(maps_dir, "cmpt.bin"))
        self._cmp = not self._cmpt and any(
            os.path.exists(os.path.join(maps_dir, n)) for n in ("cmp.pkl", "cmp.bin"))
        # the pinned tree, for the text of the files a patch touches (the image has it)
        self.root = source_root if source_root is not None else os.environ.get("BLAST_SQLGLOT", "/opt/sqlglot")
        self.n = self.m["n"]
        self.all = (1 << self.n) - 1
        self.ms = self.m["ms"]

    # ---------------------------------------------------------------- lazy map parts
    def view(self, path):
        v = self._views.get(path)
        if v is None:
            if self.packed:
                v = PagedView(self, path, self.m["file_index"].get(path))
            else:
                name = self.m["file_index"].get(path)
                if name is None:
                    blob = {"lines": {}, "import_lines": _EMPTY, "info": None, "src": None}
                else:
                    blob = _load(os.path.join(self.dir, "files", name))
                v = BlobView(blob)
            self._views[path] = v
        return v

    def _bucket(self, name):
        b = zlib.crc32(name.encode()) % self.m["nbuckets"]
        d = self._buckets.get(b)
        if d is None:
            d = _load(os.path.join(self.dir, "r", f"{b}.bin"))
            self._buckets[b] = d
        return d

    def _plain_refs(self):
        if self._refs is None:
            self._refs = _load(os.path.join(self.dir, "refs.pkl"))
        return self._refs

    def ref_rt(self, name):
        if self.packed:
            return self._bucket(name)["rt"].get(name, 0)
        return self._plain_refs()["refs_rt"].get(name, 0)

    def ref_it(self, name):
        if self.packed:
            return self._bucket(name)["it"].get(name, ())
        return self._plain_refs()["refs_it"].get(name, ())

    def ref_imp(self, name):
        if self.packed:
            return name in self.m["refs_imp"]
        return name in self._plain_refs().get("refs_imp", _EMPTY)

    # ---------------------------------------------------------------- primitives
    def lines_users(self, path, lines):
        v = self.view(path)
        b = 0
        for l in lines:
            b |= v.bits(l)
        return b

    def file_users(self, path):
        return self.m["file_users"].get(path, 0)

    def module_users(self, mod, seen=None):
        """Everything that loads or uses `mod`, directly or through an importer."""
        mods = [mod] + list(self.m["rdeps"].get(mod, ()))
        b = 0
        files = self.m["modules"]
        ti = self.m["testfile_imports"]
        for x in mods:
            f = files.get(x)
            if f:
                b |= self.file_users(f)
            b |= ti.get(x, 0)
        return b

    _ENUM_INTERNALS = frozenset({"_member_map_", "_member_names_", "_value2member_map_",
                                 "_hashable_values_", "_unhashable_values_", "_value_repr_",
                                 "_member_type_", "__members__", "_last_values_"})

    def _pinned_member_span(self, mod, clspath, attr):
        """The (first, last) pinned line of `attr` when it is a method, nested class, or
        property/static/classmethod assignment defined directly in the pinned class `clspath`
        of module `mod`; else None. Such a member's import-state fingerprint changes only
        because the patch (directly or through a decorator) altered that code, and the tests
        that run it are exactly the tests that cover its body. None on any read error."""
        import ast

        key = (mod, clspath, attr)
        cache = self.__dict__.setdefault("_memspan", {})
        if key in cache:
            return cache[key]
        src = self.read(mod.replace(".", "/") + ".py")
        res = None
        if src is not None:
            try:
                node = ast.parse(src)
                for part in clspath.split("."):
                    node = next((c for c in getattr(node, "body", [])
                                 if isinstance(c, ast.ClassDef) and c.name == part), None)
                    if node is None:
                        break
                if node is not None:
                    for st in node.body:
                        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and st.name == attr:
                            res = (st.lineno, st.end_lineno or st.lineno)
                            break
                        if isinstance(st, ast.Assign) and isinstance(st.value, ast.Call) \
                                and isinstance(st.value.func, ast.Name) \
                                and st.value.func.id in ("property", "staticmethod", "classmethod") \
                                and any(isinstance(t, ast.Name) and t.id == attr for t in st.targets):
                            res = (st.lineno, st.end_lineno or st.lineno)
                            break
            except (SyntaxError, ValueError):
                res = None
        cache[key] = res
        return res

    def _enum_not_iterated(self, enumname):
        """True when no pinned test iterates the enum named `enumname` (so adding a member
        cannot change a test's outcome through its internal member maps). Checked by searching
        the pinned tests for `in <EnumName>` and `list(<EnumName>)`; any uncertainty -> False."""
        short = enumname.rsplit(".", 1)[-1].rsplit(":", 1)[-1]
        if not short or not short.isidentifier():
            return False
        cache = self.__dict__.setdefault("_enumiter", {})
        if short in cache:
            return cache[short]
        root = self.root
        if not root or not os.path.isdir(os.path.join(root, "tests")):
            cache[short] = False
            return False
        import subprocess

        hit = True
        try:
            r = subprocess.run(["grep", "-rqE", "--include=*.py",
                                rf"(\bin +{short}\b|\b{short}\.__members__|list\( *{short} *\)|\[ *{short} *\])",
                                os.path.join(root, "tests")],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
            hit = (r.returncode == 0)
        except Exception:  # noqa: BLE001
            hit = True
        cache[short] = not hit
        return cache[short]

    def _name_absent_from_pinned(self, name):
        """True when identifier `name` occurs nowhere in the pinned sqlglot/ and tests/ source.
        Then no pinned code or test can reference it (by attribute access or by name for
        getattr/hasattr), so a symbol the patch adds under that name is invisible to every
        pinned test. Conservative: any read error or odd name returns False (not absent)."""
        if not name or not name.isidentifier():
            return False
        cache = self.__dict__.setdefault("_absent", {})
        if name in cache:
            return cache[name]
        root = self.root
        if not root or not os.path.isdir(root):
            cache[name] = False
            return False
        import subprocess

        found = True
        try:
            # whole-word search; return code 1 means "no match" i.e. absent
            r = subprocess.run(["grep", "-rwqF", "--include=*.py", name,
                                os.path.join(root, "sqlglot"), os.path.join(root, "tests")],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
            found = (r.returncode == 0)
        except Exception:  # noqa: BLE001  -- cannot be sure: treat as present
            found = True
        cache[name] = not found
        return cache[name]

    def _id_index(self):
        idx = getattr(self, "_idx", None)
        if idx is None:
            idx = self._idx = {t: i for i, t in enumerate(self.m["ids"])}
        return idx

    def _only_new_test_methods(self, mod, qualname, names):
        """True when every `name` added to class `qualname` of test module `mod` is a new
        `test_*` method (name starts with "test", and that test id is not in the map, so it did
        not exist on the pinned commit). A new method cannot shadow a fixture or an existing
        test, and a brand-new test is not in tests.json, so it is never scored."""
        path = mod.replace(".", "/") + ".py"
        idx = self._id_index()
        for n in names:
            if not n.startswith("test"):
                return False   # setUp/tearDown/helpers can change siblings
            if f"{path}::{qualname}::{n}" in idx:
                return False   # an existing test of that name: it is being modified, not added
        return True

    def class_users(self, mod, qualname, names, seen, existing=False):
        key = ("class", mod, qualname, tuple(names) if names else None)
        if key in seen:
            return 0
        seen.add(key)
        self._exact = False
        if existing:
            exact = self.attr_readers(mod, qualname, names)
            if exact is not None:
                self._exact = True
                return exact
        b = self.m["types"].get(f"{mod}:{qualname}", 0)
        if b and names and self.refine_constants and all(
                x.isupper() and not self.ref_imp(x) for x in names):
            # A setting such as WEEK_OFFSET that only code running during tests reads: the
            # class's users that actually executed a read of it.
            readers = 0
            for x in names:
                readers |= self.ref_rt(x)
            b &= readers
        if names:
            for x in names:
                b |= self.derived(x, mod, qualname or "", seen)
        if not self.m["types"].get(f"{mod}:{qualname}", 0) and not b:
            # Never instantiated while testing: fall back to everything that reads the names,
            # and failing that, to the whole module.
            if names:
                for x in names:
                    b |= self.ref_rt(x)
            if not b:
                b = self.module_users(mod)
        return b

    def const_users(self, mod, names, seen):
        b = 0
        for x in names:
            key = ("const", mod, x)
            if key in seen:
                continue
            seen.add(key)
            b |= self.ref_rt(x)
            b |= self.derived(x, mod, None, seen)
        return b

    def _derived_refs(self, name, mod, qualname):
        """Import-time references to `name` that derive something from it (class bodies,
        module constants, def headers), excluding its own definition.

        For a class attribute (qualname given), only accesses that name the whole class path
        (PRQL.Tokenizer.QUOTES for PRQL.Tokenizer, not tokens.Tokenizer.QUOTES) or bare reads
        inside that class's own body count: subclasses are already covered by instance MROs.
        """
        parts = set(qualname.split(".")) if qualname else None
        for e in self.ref_it(name):
            if e[0] == "hdr":
                yield e
                continue
            _, emod, ecls, enames, mentions, eline = e
            if enames and name in enames and emod == mod:
                continue  # the definition itself
            if parts is not None:
                # names visible from the referencing class body count too (inside class
                # Snowflake, `Tokenizer.X` is Snowflake.Tokenizer.X)
                scope = set(ecls.split(".")) if (emod == mod and ecls) else set()
                if len(mentions) > 1 and not parts <= (set(mentions[1:]) | scope):
                    continue
                if len(mentions) == 1 and not (emod == mod and ecls and (
                        ecls == qualname or ecls.startswith(qualname + "."))):
                    continue
            yield e

    def derived(self, name, mod, qualname, seen):
        """Users of everything derived from `name` at import time."""
        b = 0
        for e in self._derived_refs(name, mod, qualname):
            if e[0] == "hdr":
                _, path, body = e
                b |= self.lines_users(path, body)
                continue
            _, emod, ecls, enames, mentions, eline = e
            if ecls:
                b |= self.class_users(emod, ecls, enames, seen)
            elif enames:
                b |= self.const_users(emod, enames, seen)
            else:
                b |= self.module_users(emod)
        return b

    # ---------------------------------------------------------------- attribute reads
    def _attr_map(self):
        if self._attr is False:
            try:
                import marshal

                with open(os.path.join(self.dir, "attr.bin"), "rb") as fh:
                    self._attr = marshal.loads(zlib.decompress(fh.read()))
            except OSError:
                self._attr = None
        return self._attr

    def attr_readers(self, mod, qualname, names):
        """Tests that read (class, name) for an existing class-body assignment, from
        maps/attr.bin (tools/attr_trace.py). None when the exact answer is not available:
        the attribute is read while importing (values derived from it may live anywhere), is
        copied by another class body, or was not instrumented."""
        a = self._attr_map()
        if a is None or not names or not qualname:
            return None
        owner = f"{mod}:{qualname}"
        imp = a["imp"].get(owner, ())
        star_imp = a["imp"].get("*", ())
        if "*" in imp:
            return None
        reads = a["reads"].get(owner, {})
        star = a["reads"].get("*", {})
        b = reads.get("*", 0)
        for x in names:
            if x not in a["names"] or x in imp or x in star_imp:
                return None
            for _ in self._derived_refs(x, mod, qualname):
                return None
            b |= reads.get(x, 0) | star.get(x, 0)
        # tests that could not be profiled count as readers if they use the class at all
        users = self.m["types"].get(owner, 0)
        return b | (a["unprofiled"] & (users if users else self.all))

    def builder_users(self, path, qualname, seen):
        self.uncertain = True
        """A function whose body runs at import time: whoever holds what it built."""
        name = qualname.split(".")[-1] if qualname else None
        mod = pyimpact.module_name(path)
        b = 0
        found = False
        if name:
            for e in self.ref_it(name):
                found = True
                if e[0] == "hdr":
                    b |= self.lines_users(e[1], e[2])
                    continue
                _, emod, ecls, enames, mentions, eline = e
                if ecls:
                    b |= self.class_users(emod, ecls, enames, seen)
                elif enames:
                    b |= self.const_users(emod, enames, seen)
                else:
                    b |= self.module_users(emod)
        if not found:
            b |= self.module_users(mod)
        return b

    # ---------------------------------------------------------------- impacts
    def impact_users(self, imp, seen):
        if self._alt:
            kind = imp[0]
            if kind == "broken":
                raise _NoAlt()
            if kind == "lines":
                return self.lines_users(imp[1], imp[2])
            if kind == "defbody":
                q = imp[3] if len(imp) > 3 else None
                if q and q != "<display-lambda>":
                    mod = pyimpact.module_name(imp[1])
                    self._expected.add(f"{mod}:{q}")
                return self.lines_users(imp[1], imp[2])
            return 0  # class / const / module / none: their effect is the fingerprint diff
        self._class_exact = False
        b = self._impact_users(imp, seen)
        if imp[0] == "class" and self._class_exact:
            return b  # nothing reads it at import, so no import-time observer can differ
        # Tests that run `python -c "import sqlglot ..."` in a subprocess see every
        # import-time change to a module that `import sqlglot` loads.
        if imp[0] in ("class", "const", "module", "broken") or (
            imp[0] in ("lines", "defbody") and self._import_time(imp)
        ):
            mod = imp[1] if imp[0] not in ("lines", "defbody") else pyimpact.module_name(imp[1])
            if mod == "sqlglot" or "sqlglot" in self.m["rdeps"].get(mod, ()):
                b |= self.m.get("spawners", 0)
        return b

    def _import_time(self, imp):
        if imp[0] == "defbody" and len(imp) > 3 and imp[3] == "<display-lambda>":
            return False
        v = self.view(imp[1])
        return any(v.import_line(l) for l in imp[2])

    def _impact_users(self, imp, seen):
        kind = imp[0]
        if kind == "lines":
            path, lines = imp[1], imp[2]
            b = self.lines_users(path, lines)
            v = self.view(path)
            if any(v.import_line(l) for l in lines):
                fn = imp[3] if len(imp) > 3 else self._enclosing_function(path, lines[0])
                b |= self.builder_users(path, fn, seen)
            return b
        if kind == "defbody":
            _, path, lines, qualname = imp
            b = self.lines_users(path, lines)
            v = self.view(path)
            body = lines[:-1]
            if qualname != "<display-lambda>" and any(v.import_line(l) for l in body):
                b |= self.builder_users(path, qualname, seen)
            return b
        if kind in ("const", "module", "broken"):
            self.uncertain = True
        if kind == "class":
            mod, qualname, names = imp[1], imp[2], imp[3]
            if len(imp) == 5 and mod.startswith("tests.") and qualname and names and \
                    self._only_new_test_methods(mod, qualname, names):
                # new test methods added to a test class: a new test is not in tests.json so it
                # is never scored, and adding a method cannot change a sibling test's outcome
                return 0
            # a statement added to a class (5th element "new") may shadow a base attribute:
            # only an existing assignment has an exact reader set
            b = self.class_users(mod, qualname, names, seen, existing=len(imp) == 4)
            self._class_exact = self._exact
            if not self._exact:
                self.uncertain = True
            if self._has_keys and qualname and names:
                km = self.key_map()
                if km and any(f"{mod}:{qualname}.{n}" in km["tables"] for n in names):
                    # entry-level readers exist for this table: worth an import diff
                    self.table_edit = True
            return b
        if kind == "const":
            _, mod, names = imp
            return self.const_users(mod, names, seen)
        if kind == "none":
            return 0
        if kind in ("module", "broken"):
            return self.module_users(imp[1])
        raise ValueError(imp)

    def _enclosing_function(self, path, line):
        tree = pyimpact.parse(self.read(path) or "")
        chain = pyimpact.locate(tree, line)
        fns = [n for n in chain if isinstance(n, pyimpact.SCOPE)]
        return ".".join(n.name for n in fns) if fns else None

    def read(self, path):
        src = self.view(path).src
        if src is not None:
            return src
        if self.root is None:
            return None
        p = os.path.join(self.root, path)
        try:
            with open(p, encoding="utf-8") as fh:
                return fh.read()
        except (FileNotFoundError, IsADirectoryError, UnicodeDecodeError):
            v = self.view(path)
            if v.fid is not None and v.fid in self.m["srcs"]:
                # a file of the pinned tree that cannot be read: never guess (the caller
                # then selects every test for this commit)
                raise RuntimeError(f"pinned source of {path} is not readable under {self.root}")
            return None

    # ---------------------------------------------------------------- patches
    def select(self, patch_text):
        if "\r\n" in patch_text:
            patch_text = patch_text.replace("\r\n", "\n")
        files = diffparse.parse(patch_text)
        b = 0
        notes = []
        self.uncertain = False
        self.table_edit = False
        for fp in files:
            fb, why = self.select_file(fp)
            b |= fb
            notes.append(f"{fp.path}:{why}:{fb.bit_count()}")
        return b, notes

    def key_map(self):
        """maps/keys.bin (blast/keytab.py), or None."""
        if self._keys is False:
            try:
                import marshal

                with open(os.path.join(self.dir, "keys.bin"), "rb") as fh:
                    self._keys = marshal.loads(zlib.decompress(fh.read()))
            except OSError:
                self._keys = None
        return self._keys

    def select_alt(self, patch_text, diff_keys, entry=None):
        """Selection given the import-state diff of the patched tree (blast/imstate.py,
        Verifier.import_diff): the tests that run the changed lines, plus the recorded readers
        of every class attribute and module global whose fingerprint changed. None when some
        changed key has no reliable reader set; the ordinary selection then stands.
        `entry` ({key: {"keys": [...], "gone": [...], "len": bool}}, Verifier.entry) names the
        entries that differ in a changed dict table; its readers are then the tests that read
        one of those entries or read the table as a whole (maps/keys.bin)."""
        if "\r\n" in patch_text:
            patch_text = patch_text.replace("\r\n", "\n")
        self._alt = True
        self._expected = set()
        try:
            b = 0
            for fp in diffparse.parse(patch_text):
                fb, why = self.select_file(fp)
                b |= fb
            keys = [k for k in diff_keys if k not in self._expected]
            a = self._attr_map()
            km = self.key_map() if entry else None
            for k in keys:
                # A structural change to an existing symbol (the module set, or a class's bases)
                # cannot be analysed key by key; the ordinary selection stands. But a brand-new
                # class the patch adds also gets a "+...<bases>" key, and that one is handled with
                # the other added keys below, not treated as a structural change here.
                if not k.startswith("+") and (k == "<modules>" or k.endswith(".<bases>")):
                    return None
                if k.startswith("+"):
                    # a symbol the patched import added. A pinned test can only observe it by
                    # naming it (an attribute access or a string for getattr) or by iterating its
                    # class dict. When the name occurs nowhere in the pinned tree and no test
                    # reads that class's dict as a whole, no pinned test's outcome depends on it,
                    # so it contributes no readers; anything derived from it that existing code
                    # reads shows up as a separate, non-added changed key handled below.
                    mod, _, sym = k[1:].partition(":")   # "module" : "Class.attr" or "name"
                    if not sym:
                        return None
                    # The outermost added name: the class or global itself. When the patch adds a
                    # whole new class, its bases and each of its attributes arrive as separate
                    # "+Class.<bases>" / "+Class.attr" keys, all part of that one new symbol, so if
                    # its name is nowhere in the pinned tree no pinned test can observe any of them.
                    top = sym.split(".", 1)[0]
                    if self._name_absent_from_pinned(top):
                        continue
                    nm = sym.rsplit(".", 1)[-1]          # the added identifier
                    clspath = sym.rsplit(".", 1)[0] if "." in sym else None
                    owner = f"{mod}:{clspath}" if clspath else None
                    a2 = self._attr_map() if owner else None
                    wild = bool(a2 and (a2["reads"].get(owner, {}).get("*") or "*" in a2["imp"].get(owner, ())))
                    if not wild and nm.isidentifier() and self._name_absent_from_pinned(nm):
                        continue   # a brand-new symbol no pinned test can name or observe
                    return None
                e = entry.get(k) if entry else None
                t = km["tables"].get(k) if (km and e is not None) else None
                if t is not None:
                    b |= t["whole"] | km["unprofiled"]
                    if e["len"]:
                        b |= t["len"]
                    if e["len"] and (e.get("empty", True) or "bool" not in t):
                        b |= t.get("bool", 0)   # truth value: only emptiness matters
                    if "lenp" in t:
                        for pn in e.get("parents", ()):   # nested nodes that gained or lost an entry
                            b |= t["lenp"].get(pn, 0) | t["boolp"].get(pn, 0)
                    elif e["gone"] and t.get("nodes"):
                        b |= t.get("len", 0) | t.get("bool", 0)
                    for name in e["keys"]:
                        b |= t["keyed"].get(name, 0)
                    for name in e["gone"]:
                        b |= t["keyed"].get(name, 0) | t["has"].get(name, 0)
                    continue
                mod, _, rest = k.partition(":")
                if "." in rest:
                    cls, _, attr = rest.rpartition(".")
                    if a is None or attr not in a["names"]:
                        # not a traced data attribute.
                        span = self._pinned_member_span(mod, cls, attr)
                        if span is not None:
                            # a method/nested class/property: its fingerprint changed because
                            # the patch altered that code; select exactly the tests that run it
                            b |= self.lines_users(mod.replace(".", "/") + ".py", range(span[0], span[1] + 1))
                            continue
                        if attr in self._ENUM_INTERNALS and self._enum_not_iterated(cls):
                            # a member was added to an enum no test iterates: its internal
                            # member maps changed, but the added member (a separate added key)
                            # is the only observable effect and reaches tests through the code
                            continue
                        return None
                    owner = f"{mod}:{cls}"
                    reads = a["reads"].get(owner, {})
                    b |= reads.get(attr, 0) | reads.get("*", 0) | a["reads"].get("*", {}).get(attr, 0)
                    b |= a["unprofiled"]
                else:
                    b |= self.ref_rt(rest)
            if keys:
                # A test that runs `python -c "import sqlglot ..."` in a subprocess sees the
                # import state of the modules that bare import loads, and only those: a patch
                # to a module outside that closure (most dialects load lazily) is never
                # loaded there, whatever it changes elsewhere once it is imported.
                for fp in diffparse.parse(patch_text):
                    path = fp.old_path or fp.new_path
                    mod = pyimpact.module_name(path) if path and path.endswith(".py") else None
                    if mod is None or mod == "sqlglot" or "sqlglot" in self.m["rdeps"].get(mod, ()):
                        b |= self.m.get("spawners", 0)
                        break
            return b
        except _NoAlt:
            return None
        finally:
            self._alt = False

    def import_only(self, patch_text, bit):
        """Does the patch, in every file whose own selection holds test `bit`, only change
        statements that run while modules are imported (class bodies, module constants), all
        in existing sqlglot modules? Their whole effect is then the import-state difference
        an import diff reports."""
        if "\r\n" in patch_text:
            patch_text = patch_text.replace("\r\n", "\n")
        any_file = False
        for fp in diffparse.parse(patch_text):
            was = self.uncertain
            try:
                fb, _ = self.select_file(fp)
            finally:
                self.uncertain = was
            if not fb >> bit & 1:
                continue
            path = fp.old_path
            if path is None or fp.new_path != path or fp.binary or fp.is_new or fp.is_deleted:
                return False
            if not (path.startswith("sqlglot/") and path.endswith(".py")):
                return False
            old_src = self.read(path)
            if old_src is None:
                return False
            applied = diffparse.apply(old_src, fp)
            if not applied.ok:
                return False
            imps = pyimpact.analyze(path, self.view(path), old_src, "\n".join(applied.new_lines), applied)
            if any(imp[0] not in ("class", "const", "module", "none") for imp in imps):
                return False
            any_file = True
        return any_file

    def body_lines(self, patch_text, bit=None, allow_import=False):
        """For a patch that only edits statements inside functions of sqlglot modules, or the
        values of their parameter defaults: {path: [(first, last) line of each edited outermost
        function]}. None for any other patch. Sets self.body_at_import when one of those
        functions also runs while modules are imported.
        With `bit` (a test's index), files whose own selection does not hold that test are
        left out: by the ordinary rules they cannot affect it.
        With `allow_import`, class-body and module-level statements may be edited too (their
        effect is then what an import diff reports): self.body_import says whether any was."""
        self.body_import = False
        self.body_at_import = False
        self.body_spans = None
        self.body_expected = set()   # import-state keys that differ by design: functions whose defaults changed
        if "\r\n" in patch_text:
            patch_text = patch_text.replace("\r\n", "\n")
        out = {}
        # pinned lines that run exactly when an edited statement does: the edited statements'
        # regions, and for a changed parameter default the function's body without its header
        bodies = {}
        for fp in diffparse.parse(patch_text):
            if bit is not None:
                was = self.uncertain
                try:
                    fb, _ = self.select_file(fp)
                finally:
                    self.uncertain = was
                if not fb >> bit & 1:
                    continue
            path = fp.old_path
            if path is None or fp.new_path != path or fp.binary or fp.is_new or fp.is_deleted:
                return None
            from .planfp import PROBE_DATA

            if path in PROBE_DATA:
                continue   # a data file the probe itself reads from the patched tree
            if not (path.startswith("sqlglot/") and path.endswith(".py")):
                return None
            old_src = self.read(path)
            if old_src is None:
                return None
            applied = diffparse.apply(old_src, fp)
            if not applied.ok:
                return None
            v = self.view(path)
            imps = pyimpact.analyze(path, v, old_src, "\n".join(applied.new_lines), applied)
            for imp in imps:
                if imp[0] == "none":
                    continue
                if imp[0] == "defbody":
                    # a parameter default replaced by another value: it matters only where
                    # the function is called, so the function's span is what counts
                    node = next((n for n in v.chain(imp[2][-1]) or () if n[4] == "D"), None)
                    if node is None or (len(imp) > 3 and imp[3] == "<display-lambda>"):
                        return None
                    if any(v.import_line(x) for x in range(node[3] + 1, node[2] + 1)):
                        self.body_at_import = True
                    if len(imp) > 3 and imp[3]:
                        self.body_expected.add(f"{pyimpact.module_name(path)}:{imp[3]}")
                    ns, ne = applied.new_range(node[1], node[2])
                    was = applied.old_lines if applied.old_lines is not None else old_src.split("\n")
                    if not equiv.only_defaults_differ(was[node[1] - 1:node[2]], applied.new_lines[ns - 1:ne]):
                        # not purely a default value: the header or signature changed too. It runs
                        # when the module is imported, so the function's own fingerprint will differ
                        # by design; mark it expected and let the planning check verify the plans and
                        # run any query that reaches the function (it cannot miss -- it runs them).
                        self.body_at_import = True
                        if len(imp) > 3 and imp[3]:
                            self.body_expected.add(f"{pyimpact.module_name(path)}:{imp[3]}")
                    out.setdefault(path, set()).add((node[1], node[2]))
                    bodies.setdefault(path, set()).add((node[3] + 1, node[2]))
                    continue
                if imp[0] in ("class", "const", "module") and allow_import:
                    self.body_import = True
                    continue
                if imp[0] != "lines":
                    return None
                for l in imp[2]:
                    node = next((n for n in v.chain(l) or () if n[4] == "D"), None)
                    if node is None:
                        # a comment or blank line carries no statement, so an edit to it runs
                        # nothing and selects nothing; only a real statement with no enclosing
                        # function (a class body or module level) forces the import-diff route
                        if not v.is_code(l):
                            continue
                        return None
                    if l <= node[3]:
                        # an edit to the def header, a decorator or the signature: it runs when the
                        # module is imported and changes the whole function, so the function's own
                        # fingerprint differs by design. Mark it expected and take the function's
                        # full body as the edited region; the planning check then verifies the plans
                        # and runs any query that reaches the function (it cannot miss).
                        self.body_at_import = True
                        if len(imp) > 3 and imp[3]:
                            self.body_expected.add(f"{pyimpact.module_name(path)}:{imp[3]}")
                        out.setdefault(path, set()).add((node[1], node[2]))
                        bodies.setdefault(path, set()).add((node[3] + 1, node[2]))
                        continue
                    # the header runs when the module is imported; if the body does too, the
                    # caller must also establish that the import leaves the same state
                    if any(v.import_line(x) for x in range(node[3] + 1, node[2] + 1)):
                        self.body_at_import = True
                    out.setdefault(path, set()).add((node[1], node[2]))
                    # the statement's own lines (its region, as coverage selects tests by)
                    bodies.setdefault(path, set()).add((l, l))
        self.body_spans = {k: sorted(v) for k, v in bodies.items()}
        return {k: sorted(v) for k, v in out.items()}

    def openers(self, path):
        if path.endswith(".py"):
            # Reading a module's source while importing it (pytest's assertion rewriting,
            # the import system) is what line coverage already captures. Only reads while a
            # test runs (test_typing reading sqlglot/typing/*.py, runpy of sqlglotc/setup.py)
            # depend on the file's text.
            return self.m["opens"].get(path, 0)
        return self.m["opens"].get(path, 0) | self.m["import_opens"].get(path, 0)

    def select_file(self, fp):
        old_path, new_path = fp.old_path, fp.new_path
        b = 0
        for p in {old_path, new_path} - {None}:
            b |= self.openers(p)
            if fp.is_new or fp.is_deleted or old_path != new_path:
                d = p.rsplit("/", 1)[0] + "/" if "/" in p else "/"
                b |= self.m["opens"].get(d, 0)
        path = old_path or new_path
        base = path.rsplit("/", 1)[-1]
        if path in SHARED_CONFIG:
            # The pinned pyproject.toml and setup.cfg configure packaging, mypy and ruff, not
            # pytest: an edit reaches pytest only if it brings pytest settings in.
            text = "\n".join(t for h in fp.hunks for tag, t in h.lines if tag in "+-")
            if "pytest" in text or fp.is_new:
                return self.all, "config"
            # pytest reads these files while collecting (looking for its own section): only
            # reads made while a test runs depend on their content.
            b = 0
            for p in {old_path, new_path} - {None}:
                b |= self.m["opens"].get(p, 0)
            return b, "config-nonpytest"
        if path in CONFIG_FILES or base == "conftest.py":
            return self.all, "config"
        if fp.binary:
            return b, "binary"
        if not path.endswith(".py"):
            if (path.startswith("tests/fixtures/") and old_path == new_path and not fp.is_new
                    and not fp.is_deleted and b):
                # a fixture a test class loads in its set-up: its tests that never read the
                # attribute holding it only need the set-up to succeed (blast/fixtures.py)
                old_text = self.read(path)
                applied = diffparse.apply(old_text, fp) if old_text is not None else None
                if applied is not None and applied.ok:
                    from . import fixtures

                    drop = fixtures.setup_only_readers(self, path, "\n".join(applied.new_lines), b)
                    if drop:
                        return b & ~drop, "data-setup"
            return b, "data"
        old_src = self.read(old_path) if old_path else None
        if old_path and old_src is None:
            return b, "missing-old"
        applied = diffparse.apply(old_src, fp)
        if not applied.ok:
            if self._alt:
                raise _NoAlt()
            log(f"warning: hunks did not apply cleanly to {path}; using every user of the module")
            return b | self.module_users(pyimpact.module_name(path)), "unapplied"
        new_src = None if fp.is_deleted else "\n".join(applied.new_lines)
        if old_path != new_path and old_path and new_path:
            # rename: the old module disappears
            if self._alt:
                raise _NoAlt()
            return b | self.module_users(pyimpact.module_name(old_path)), "rename"
        if new_src is not None and old_src is not None and equiv.equivalent(
                self.view(path), old_src.split("\n"), applied.new_lines, applied):
            # every touched function compiles to the same code: formatting, hints, local names
            return b, "equivalent"
        if self._args and new_src is not None and applied.removed \
                and len(applied.removed) == len(applied.new_changed):
            got = _cmpswap().argflip_users(path, old_src.split("\n"), applied.new_lines, applied, self.dir)
            if got is not None:
                # only arg_types True/False flips: the tests that validated the class with
                # that argument missing
                return b | got, "argflip"
        if self._dflt and new_src is not None and applied.removed \
                and len(applied.removed) == len(applied.new_changed):
            got = _cmpswap().default_users(path, old_src.split("\n"), applied.new_lines, applied, self.dir,
                                           after_import=self._alt)
            if got is not None and not self._alt:
                # only parameter defaults changed: the tests that relied on those defaults
                return b | got, "default"
            if got is not None:
                # After an import diff: the import leaves the pinned state except for what it
                # lists, so the changed defaults matter only to the calls that relied on them
                # afterwards. The functions themselves differ (their defaults): expected keys.
                # Subprocess tests import and run the patched code where nothing is traced.
                quals = []
                for imp in pyimpact.analyze(path, self.view(path), old_src, new_src, applied):
                    if imp[0] == "none":
                        continue
                    if imp[0] != "defbody" or len(imp) < 4 or not imp[3] or imp[3] == "<display-lambda>":
                        quals = None
                        break
                    quals.append(imp[3])
                if quals:
                    mod = pyimpact.module_name(path)
                    self._expected.update(f"{mod}:{q}" for q in quals)
                    if mod == "sqlglot" or "sqlglot" in self.m["rdeps"].get(mod, ()):
                        got |= self.m.get("spawners", 0)
                    return b | got, "default"
        if self._cmpt and new_src is not None and applied.removed \
                and len(applied.removed) == len(applied.new_changed):
            got = _cmpswap().swap_users(path, old_src.split("\n"), applied.new_lines, applied, self.dir)
            if got is not None and not (self._alt and got == self.all):
                # only comparison operators swapped: the tests that ever saw a different result
                if got == self.all:
                    self.uncertain = True   # visible while importing
                return b | got, "cmpswap"
        if self._cmp and new_src is not None and applied.removed \
                and len(applied.removed) == len(applied.new_changed) and _cmpswap().swap_only(path, old_src.split("\n"), applied.new_lines, applied, self.dir):
            # only comparison operators swapped, at sites where the suite never tells them apart
            return b, "cmpswap"
        imps = pyimpact.analyze(path, self.view(path), old_src, new_src, applied)
        seen = set()
        for imp in imps:
            b |= self.impact_users(imp, seen)
        return b, ",".join(sorted({i[0] for i in imps})) or "noop"

    # ---------------------------------------------------------------- packing
    def pack(self, bits):
        tests = []
        while bits:
            low = bits & -bits
            tests.append(low.bit_length() - 1)
            bits ^= low
        if not tests:
            return []
        tests.sort(key=lambda t: (-self.ms[t], t))
        best = None
        for k in range(1, 9):
            if k > len(tests):
                break
            loads = [0] * k
            ws = [[] for _ in range(k)]
            for t in tests:
                i = loads.index(min(loads))
                ws[i].append(t)
                loads[i] += self.ms[t]
            cost = max(loads) + 2000 * k
            if best is None or cost < best[0]:
                best = (cost, ws)
        return best[1]

    def cost(self, workers):
        if not workers:
            return 0
        return max(sum(self.ms[t] for t in w) for w in workers) + 2000 * len(workers)
