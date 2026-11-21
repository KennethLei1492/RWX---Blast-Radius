"""Run a commit's candidate tests on the patched sqlglot and report which fail.

The scorer allows running sqlglot's tests while deciding (the CPU is charged). For a cheap
candidate set, running it and printing only the tests that really fail is cheaper than
printing the whole set: a commit that breaks nothing then costs its CPU, not 2 s of worker.

Design:
  * a fork server: the parent imports the heavy third-party modules once (never sqlglot);
    each commit runs in a forked child that imports sqlglot fresh, so patched modules load
    from the patched text through an import hook, and patched data files (fixtures) are
    served by redirecting open() to a copy under /tmp;
  * tests are sqlglot's unittest classes, run with unittest directly (pytest's per-run start
    up costs more than most of these tests): a failure, an error, a failing subtest, a class
    or module set-up error, or a module that cannot be imported fails the test; a skip passes,
    as it does under pytest;
  * any doubt (timeout, crash, a hunk that does not apply, a deleted or binary file) returns
    None, and the caller keeps its static selection.
"""

from __future__ import annotations

import json
import os
import sys

SRC = os.environ.get("BLAST_SQLGLOT", "/opt/sqlglot")


class Verifier:
    def __init__(self, src_root: str = SRC):
        self.root = src_root
        self.ready = False
        self.cpu_children = 0.0

    def start(self, heavy=True):
        """Import what every child needs but must not re-import (no sqlglot here). The
        heavy third-party modules (numpy, pandas, duckdb: about half a second of CPU) are only
        worth importing once here when several runs need them; otherwise each child imports
        them itself if its tests do."""
        if self.ready:
            return True
        self.heavy = heavy
        if not os.path.isdir(os.path.join(self.root, "sqlglot")):
            return False
        os.environ.setdefault("SKIP_INTEGRATION", "0")
        os.environ.setdefault("OMP_NUM_THREADS", "1")
        sys.dont_write_bytecode = True
        try:
            import unittest  # noqa: F401
            import importlib.abc  # noqa: F401
            import importlib.util  # noqa: F401
            import io  # noqa: F401
            import builtins  # noqa: F401
            for mod in (("numpy", "pandas", "duckdb") if heavy else ()):
                try:
                    __import__(mod)
                except Exception:
                    pass
        except Exception:
            return False
        self.ready = True
        return True

    # ------------------------------------------------------------------ patch overlay
    def overlay(self, patch_text: str):
        """{relative path: new text} for every file the patch touches, or None if unsure."""
        from . import diffparse

        if "\r\n" in patch_text:
            patch_text = patch_text.replace("\r\n", "\n")
        out = {}
        self.applied = {}
        for fp in diffparse.parse(patch_text):
            if fp.binary or fp.is_deleted or fp.is_new or (
                    fp.old_path and fp.new_path and fp.old_path != fp.new_path):
                # added, deleted and renamed files change directory listings, which the
                # overlay does not reproduce
                return None
            path = fp.new_path or fp.old_path
            old = None
            if fp.old_path:
                try:
                    with open(os.path.join(self.root, fp.old_path), encoding="utf-8") as fh:
                        old = fh.read()
                except (OSError, UnicodeDecodeError):
                    return None
            applied = diffparse.apply(old, fp)
            if not applied.ok:
                return None
            out[path] = "\n".join(applied.new_lines)
            self.applied[path] = applied
        return out

    # ------------------------------------------------------------------ running
    def run(self, patch_text: str, test_ids: list, timeout: float):
        """Set of failing test ids, or None when the answer cannot be trusted."""
        if not self.start():
            return None
        files = self.overlay(patch_text)
        if files is None:
            return None
        got = self._fork(lambda: sorted(_child(self.root, files, test_ids)), timeout)
        return None if got is None else set(got)

    def import_diff(self, patch_text: str, baseline: dict, timeout: float = 60, tables=None):
        """Keys of the import-state fingerprint (blast/imstate.py) that differ between the
        pinned tree and the patched one after importing every sqlglot module, or None if that
        cannot be established (the import fails, a file is added or removed, a timeout).
        With `tables` (maps/keys.bin), self.entry then holds, for each differing key that is a
        traced dict table, which of its entries differ (blast/keytab.py)."""
        self.entry = {}
        if not self.start(heavy=False):
            return None
        files = self.overlay(patch_text)
        if files is None:
            return None
        maps = {os.path.join(self.root, rel): _line_map(ap) for rel, ap in self.applied.items()
                if rel.endswith(".py")}
        got = self._fork(lambda: _import_child(self.root, files, maps, baseline, tables), timeout)
        if tables is None or got is None:
            return got
        if not isinstance(got, dict) or "diff" not in got:
            return None
        self.entry = {k: v for k, v in got.get("entry", {}).items() if v is not None}
        return got["diff"]

    def plan_same(self, patch_text: str, baseline: dict, timeout: float = 60, run_limit: float = 0.0,
                  qshare=None, test_seconds: float = 0.0, collect: int = 6):
        """True when test_execute_tpcds cannot change its outcome on the patched tree: everything
        it hands to the Python executor equals the pinned fingerprints, or the few queries whose
        plans differ (running them estimated at most `run_limit` seconds) pass when run
        (blast/planfp.py); False when it may fail; None if that cannot be established."""
        self.plan_at = self.plan_ran = None
        if not self.start():
            return None
        files = self.overlay(patch_text)
        if files is None:
            return None
        from . import planfp

        maps = {os.path.join(self.root, rel): _line_map(ap) for rel, ap in self.applied.items()
                if rel.endswith(".py")}
        edited = {}
        for rel, ap in self.applied.items():
            if rel.endswith(".py"):
                edited[os.path.join(self.root, rel)] = set(ap.new_changed) | {n for _, n, _ in ap.inserted}
        got = self._fork(lambda: planfp.probe(self.root, files, maps, baseline,
                                              collect=collect if run_limit > 0 else 0,
                                              run_limit=run_limit, qshare=qshare,
                                              test_seconds=test_seconds, edited=edited), timeout)
        if not isinstance(got, dict) or "same" not in got:
            return None
        self.plan_at = got.get("at")
        self.plan_ran = got.get("ran")
        return bool(got["same"])

    def _fork(self, job, timeout: float):
        """Run job() in a forked child; its JSON-serialisable result, or None."""
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:  # child
            code = 1
            try:
                os.close(r)
                data = json.dumps(job()).encode()
                while data:
                    n = os.write(w, data)
                    data = data[n:]
                code = 0
            except BaseException:
                code = 3
            finally:
                os._exit(code)
        os.close(w)
        import select
        import signal
        import time

        chunks = []
        deadline = time.monotonic() + timeout
        timed_out = False
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                timed_out = True
                break
            ready, _, _ = select.select([r], [], [], left)
            if not ready:
                continue
            b = os.read(r, 1 << 16)
            if not b:
                break
            chunks.append(b)
        if timed_out:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        os.close(r)
        _, status = os.waitpid(pid, 0)
        if timed_out or os.waitstatus_to_exitcode(status) != 0:
            return None
        try:
            return json.loads(b"".join(chunks).decode())
        except ValueError:
            return None


def preload_core(root=SRC):
    """Import sqlglot and tests.helpers into this process from the pinned tree, as a child
    would first do (same path and working directory), so that children forked afterwards for
    patches that change none of these files skip that import. Returns the set of source files
    (relative to root) it loaded, or None if there is no tree."""
    if not os.path.isdir(os.path.join(root, "sqlglot")):
        return None
    # The child's environment first: start() sets SKIP_INTEGRATION (tests.helpers reads it) and
    # imports unittest, which sqlglot.expressions checks at import (its UNITTEST flag).
    if not Verifier(root).start(heavy=False):
        return None
    before = set(sys.modules)
    sys.path.insert(0, root)
    os.chdir(root)
    import sqlglot  # noqa: F401
    import tests.helpers  # noqa: F401

    out = set()
    prefix = root.rstrip("/") + "/"
    for name in set(sys.modules) - before:
        f = getattr(sys.modules[name], "__file__", None) or ""
        if f.startswith(prefix):
            out.add(f[len(prefix):])
    return out


def _line_map(applied):
    """new line number -> pinned line number for a patched file."""
    table = {}
    bounds = []
    for ostart, nstart, tags in applied.segs:
        o, n = ostart, nstart
        pending = []
        last = o
        for tag in tags:
            if tag == " ":
                table[n] = o
                last = o
                o += 1
                n += 1
                pending = []
            elif tag == "-":
                pending.append(o)
                last = o
                o += 1
            else:
                table[n] = pending.pop(0) if pending else last
                n += 1
        bounds.append((n, o - n))

    def f(nl):
        got = table.get(nl)
        if got is not None:
            return got
        delta = 0
        for end, d in bounds:
            if nl >= end:
                delta = d
            else:
                break
        return nl + delta

    return f


def _import_child(root, files, line_maps, baseline, tables=None):
    """Runs in the forked child: import everything from the patched tree and diff fingerprints."""
    import importlib
    import pkgutil

    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    sys.stdout = open(os.devnull, "w")
    sys.stderr = sys.stdout
    _install_overlay(root, files)
    import sqlglot

    for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
        if info.name != "sqlglot.__main__":
            importlib.import_module(info.name)   # an import error aborts the child: unknown
    from . import imstate

    diff = imstate.diff(baseline, line_maps)
    if tables is None:
        return diff
    from . import keytab

    entry = {}
    for k in diff:
        t = tables.get(k)
        if t is not None:
            try:
                entry[k] = keytab.entry_diff(k, t["entries"], line_maps, t.get("nodes"))
            except Exception:  # noqa: BLE001  -- not comparable: the attribute rule stands
                entry[k] = None
    return {"diff": diff, "entry": entry}


def _install_overlay(root, files):
    """Serve patched modules through an import hook and patched files through open()."""
    import builtins
    import importlib.abc
    import importlib.machinery
    import io

    sys.path.insert(0, root)
    os.chdir(root)
    py = {rel: text for rel, text in files.items() if rel.endswith(".py")}

    class Overlay(importlib.abc.MetaPathFinder, importlib.abc.Loader):
        def find_spec(self, name, path, target=None):
            rel = name.replace(".", "/")
            for cand, pkg in ((rel + ".py", False), (rel + "/__init__.py", True)):
                if cand in py:
                    origin = os.path.join(root, cand)
                    spec = importlib.machinery.ModuleSpec(name, self, origin=origin, is_package=pkg)
                    if pkg:
                        spec.submodule_search_locations = [os.path.dirname(origin)]
                    spec.has_location = True
                    return spec
            return None

        def create_module(self, spec):
            return None

        def exec_module(self, module):
            origin = module.__spec__.origin
            rel = os.path.relpath(origin, root).replace(os.sep, "/")
            module.__file__ = origin
            code = compile(py[rel], origin, "exec", dont_inherit=True)
            exec(code, module.__dict__)

    sys.meta_path.insert(0, Overlay())
    tmp = os.path.join("/tmp", f"blast_ov_{os.getpid()}")
    redirect = {}
    for rel, text in files.items():
        dst = os.path.join(tmp, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with io.open(dst, "w", encoding="utf-8") as fh:
            fh.write(text)
        redirect[os.path.normpath(os.path.join(root, rel))] = dst
    real_open = io.open

    def _open(file, *args, **kwargs):
        if isinstance(file, (str, os.PathLike)):
            p = os.path.normpath(os.path.join(os.getcwd(), os.fspath(file)))
            if p in redirect:
                file = redirect[p]
        return real_open(file, *args, **kwargs)

    builtins.open = _open
    io.open = _open


def _inline_process_pools():
    """test_optimizer and test_executor send work to spawn-context process pools, whose
    workers would import the unpatched tree. Run that work in this process instead, with the
    same pickle round trips a pool makes (sqlglot expressions pickle through sqlglot.serde)."""
    import concurrent.futures as cf
    import pickle

    class _SyncExecutor(cf.Executor):
        def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
            if initializer is not None:
                initializer(*initargs)

        def submit(self, fn, /, *args, **kwargs):
            f = cf.Future()
            try:
                fn, args, kwargs = pickle.loads(pickle.dumps((fn, args, kwargs)))
                result = fn(*args, **kwargs)
                f.set_result(pickle.loads(pickle.dumps(result)))
            except BaseException as e:  # noqa: BLE001
                try:
                    e = pickle.loads(pickle.dumps(e))
                except Exception:
                    pass
                f.set_exception(e)
            return f

        def shutdown(self, wait=True, *, cancel_futures=False):
            pass

    cf.ProcessPoolExecutor = _SyncExecutor


def _child(root, files, test_ids):
    """Runs in the forked child: load the patched tree and run the tests."""
    import builtins
    import importlib.abc
    import importlib.machinery
    import io
    import unittest

    # stdout belongs to the selector: tests must not write to it
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    sys.stdout = open(os.devnull, "w")
    sys.stderr = sys.stdout

    sys.path.insert(0, root)
    os.chdir(root)

    py = {rel: text for rel, text in files.items() if rel.endswith(".py")}

    class Overlay(importlib.abc.MetaPathFinder, importlib.abc.Loader):
        def find_spec(self, name, path, target=None):
            rel = name.replace(".", "/")
            for cand, pkg in ((rel + ".py", False), (rel + "/__init__.py", True)):
                if cand in py:
                    origin = os.path.join(root, cand)
                    spec = importlib.machinery.ModuleSpec(name, self, origin=origin, is_package=pkg)
                    if pkg:
                        spec.submodule_search_locations = [os.path.dirname(origin)]
                    spec.has_location = True
                    return spec
            return None

        def create_module(self, spec):
            return None

        def exec_module(self, module):
            origin = module.__spec__.origin
            rel = os.path.relpath(origin, root).replace(os.sep, "/")
            module.__file__ = origin
            code = compile(py[rel], origin, "exec", dont_inherit=True)
            exec(code, module.__dict__)

    sys.meta_path.insert(0, Overlay())
    _inline_process_pools()

    # every patched file (data and source read as text) is served from a copy in /tmp
    tmp = os.path.join("/tmp", f"blast_ov_{os.getpid()}")
    redirect = {}
    for rel, text in files.items():
        dst = os.path.join(tmp, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with io.open(dst, "w", encoding="utf-8") as fh:
            fh.write(text)
        redirect[os.path.normpath(os.path.join(root, rel))] = dst
    real_open = io.open

    def _open(file, *args, **kwargs):
        if isinstance(file, (str, os.PathLike)):
            p = os.path.normpath(os.path.join(os.getcwd(), os.fspath(file)))
            if p in redirect:
                file = redirect[p]
        return real_open(file, *args, **kwargs)

    builtins.open = _open
    io.open = _open

    by_module = {}
    for tid in test_ids:
        parts = tid.split("::")
        if len(parts) != 3:
            return set(test_ids)  # unexpected shape: say everything fails (caller keeps all)
        by_module.setdefault(parts[0], []).append((parts[1], parts[2], tid))

    failed = set()
    suite = unittest.TestSuite()
    owner = {}
    for path, items in by_module.items():
        modname = path[:-3].replace("/", ".")
        try:
            module = __import__(modname, fromlist=["_"])
        except BaseException:
            failed.update(t for _, _, t in items)
            continue
        for cls_name, meth, tid in items:
            try:
                case = getattr(module, cls_name)(meth)
            except BaseException:
                failed.add(tid)
                continue
            owner[case.id()] = tid
            suite.addTest(case)

    class_of = {}
    for tid in test_ids:
        f, c, _ = tid.split("::")
        class_of.setdefault(f[:-3].replace("/", ".") + "." + c, set()).add(tid)

    class Result(unittest.TestResult):
        def _mark(self, test):
            tid = owner.get(getattr(test, "id", lambda: "")())
            if tid is not None:
                failed.add(tid)
                return
            # set-up/tear-down of a class or module (an _ErrorHolder): fail all it covers
            desc = str(test)
            for key, tids in class_of.items():
                if key in desc or key.rsplit(".", 1)[0] in desc:
                    failed.update(tids)

        def addError(self, test, err):
            self._mark(test)

        def addFailure(self, test, err):
            self._mark(test)

        def addSubTest(self, test, subtest, err):
            if err is not None:
                self._mark(test)

        def addUnexpectedSuccess(self, test):
            self._mark(test)

    suite.run(Result())
    return failed


def _snapshot_child(root):
    """Runs in a forked child: fingerprints of the unpatched tree in every mode, hex encoded."""
    import importlib
    import pkgutil

    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    sys.stdout = open(os.devnull, "w")
    sys.stderr = sys.stdout
    # build-time only: shift allocation addresses so address-ordered values show up as unstable
    n = int(os.environ.get("IMSTATE_PERTURB", "0"))
    junk = [object() for _ in range(n)]
    _install_overlay(root, {})
    import sqlglot

    for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
        if info.name != "sqlglot.__main__":
            importlib.import_module(info.name)
    from . import imstate

    del junk
    return [{k: v.hex() for k, v in imstate.snapshot(mode=m).items()} for m in (0, 1, 2)]


def baseline_snapshot(root=SRC):
    """The pinned tree's fingerprints, taken exactly as import_diff takes the patched ones."""
    v = Verifier(root)
    if not v.start(heavy=False):
        return None
    return v._fork(lambda: _snapshot_child(root), 300)
