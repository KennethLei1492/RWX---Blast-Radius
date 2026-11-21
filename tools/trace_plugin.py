"""pytest plugin that records, per test, what it touches in the pinned sqlglot checkout.

Run at map-build time only (never at selection time):

    SQLGLOT_TRACE_OUT=/out/x.json python -m pytest -p tools.trace_plugin tests/...

For every test (setup, call and teardown phases) it records
  * lines: every (file, line) under the checkout that executed (sys.monitoring LINE,
    disabled after the first hit and re-armed at every phase, so it is cheap);
  * types: every sqlglot class in the MRO of any object whose __init__ ran;
  * opens: every non-source file under the checkout that was opened or listed.
Lines that ran before the first test (imports, class bodies, metaclasses) are kept
separately as import-time lines.
"""

import importlib
import json
import os
import pkgutil
import sys

import pytest

ROOT = os.path.realpath(os.environ.get("SQLGLOT_ROOT", os.getcwd())) + os.sep
OUT = os.environ.get("SQLGLOT_TRACE_OUT")

mon = sys.monitoring
TOOL = mon.COVERAGE_ID
DISABLE = mon.DISABLE

_import_lines: set = set()
_import_opens: set = set()
_cur_lines = _import_lines
_cur_types: set = set()
_cur_opens = _import_opens
_records: dict = {}
_collect_lines: set = set()
_collect_opens: set = set()
_file_cache: dict = {}


def _rel(path):
    rel = _file_cache.get(path)
    if rel is None:
        rel = path[len(ROOT):] if path.startswith(ROOT) else ""
        _file_cache[path] = rel
    return rel


def _on_line(code, line):
    rel = _rel(code.co_filename)
    if rel:
        _cur_lines.add((rel, line))
    return DISABLE


def _audit(event, args):
    if event == "open":
        path = args[0]
        if isinstance(path, (str, bytes, os.PathLike)) and not isinstance(path, int):
            try:
                path = os.path.realpath(os.fsdecode(path))
            except Exception:
                return
            if path.startswith(ROOT) and not path.endswith((".pyc",)):
                _cur_opens.add(path[len(ROOT):])
    elif event in ("subprocess.Popen", "os.system", "os.posix_spawn", "os.exec"):
        _cur_opens.add("<spawn>")
    elif event in ("os.listdir", "os.scandir", "glob.glob"):
        path = args[0]
        try:
            path = os.path.realpath(os.fsdecode(path if path is not None else "."))
        except Exception:
            return
        if path.startswith(ROOT):
            _cur_opens.add(path[len(ROOT):] + "/")


def _wrap_inits():
    import functools
    import inspect

    seen = set()
    for name, module in list(sys.modules.items()):
        if not (name == "sqlglot" or name.startswith("sqlglot.")) or module is None:
            continue
        for obj in list(vars(module).values()):
            stack = [obj]
            while stack:
                cls = stack.pop()
                if not isinstance(cls, type) or cls in seen:
                    continue
                seen.add(cls)
                if not getattr(cls, "__module__", "").startswith("sqlglot"):
                    continue
                stack.extend(v for v in vars(cls).values() if isinstance(v, type))
                init = cls.__dict__.get("__init__")
                if init is None or not inspect.isfunction(init):
                    continue

                def make(init):
                    @functools.wraps(init)
                    def __init__(self, *args, **kwargs):
                        t = type(self)
                        if t not in _cur_types:
                            _cur_types.add(t)
                        return init(self, *args, **kwargs)

                    return __init__

                setattr(cls, "__init__", make(init))


def _import_everything():
    import sqlglot

    for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
        if info.name == "sqlglot.__main__":
            continue
        try:
            importlib.import_module(info.name)
        except Exception as e:  # pragma: no cover
            print(f"trace: cannot import {info.name}: {e}", file=sys.stderr)


def _inline_process_pools():
    """test_optimizer and test_executor fan work out to spawned processes, which this
    tracer cannot see. Run that work in-process instead, synchronously."""
    import concurrent.futures as cf

    class _SyncExecutor(cf.Executor):
        def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
            if initializer is not None:
                initializer(*initargs)

        def submit(self, fn, /, *args, **kwargs):
            # A process pool pickles the call and its result (sqlglot expressions pickle
            # through sqlglot.serde), so do the same round trips here.
            import pickle

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


_SETUP_DONE = False


def global_setup():
    """Expensive one-time work; a fork server calls this once before forking per test."""
    global _SETUP_DONE
    if _SETUP_DONE:
        return
    _inline_process_pools()
    mon.use_tool_id(TOOL, "blast-radius-trace")
    mon.register_callback(TOOL, mon.events.LINE, _on_line)
    mon.set_events(TOOL, mon.events.LINE)
    sys.addaudithook(_audit)
    _import_everything()
    _wrap_inits()
    _SETUP_DONE = True


def pytest_configure(config):
    global_setup()
    # From here until the first test: collection (importing the test module). With one
    # test per process these lines belong to that test.
    global _cur_lines, _cur_opens
    _cur_lines, _cur_opens = _collect_lines, _collect_opens
    mon.set_events(TOOL, mon.events.LINE)
    mon.restart_events()


def _start(item, phase):
    global _cur_lines, _cur_types, _cur_opens
    rec = _records.setdefault(item.nodeid, {})
    p = rec.setdefault(phase, {"lines": set(), "types": set(), "opens": set()})
    _cur_lines, _cur_types, _cur_opens = p["lines"], p["types"], p["opens"]
    mon.restart_events()


def _stop():
    global _cur_lines, _cur_types, _cur_opens
    _cur_lines, _cur_types, _cur_opens = set(), set(), set()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    _start(item, "setup")
    yield
    _stop()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    _start(item, "call")
    yield
    _stop()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item, nextitem):
    _start(item, "teardown")
    yield
    _stop()


_outcomes: dict = {}


def pytest_runtest_logreport(report):
    if report.failed or (report.when == "call" and report.outcome != "passed"):
        _outcomes.setdefault(report.nodeid, []).append(f"{report.when}:{report.outcome}")


def _types_to_names(types):
    names = set()
    for t in types:
        for k in getattr(t, "__mro__", ()):
            mod = getattr(k, "__module__", "") or ""
            if mod.startswith("sqlglot"):
                names.add(f"{mod}:{k.__qualname__}")
    return sorted(names)


def _lines_to_dict(lines):
    out: dict = {}
    for f, l in lines:
        out.setdefault(f, []).append(l)
    return {f: sorted(v) for f, v in sorted(out.items())}


def pytest_sessionfinish(session, exitstatus):
    mon.set_events(TOOL, 0)
    if not OUT:
        return
    tests = {}
    for nodeid, phases in _records.items():
        tests[nodeid] = {
            ph: {
                "lines": _lines_to_dict(p["lines"]),
                "types": _types_to_names(p["types"]),
                "opens": sorted(p["opens"]),
            }
            for ph, p in phases.items()
        }
    data = {
        "import_lines": _lines_to_dict(_import_lines),
        "import_opens": sorted(_import_opens),
        "collect_lines": _lines_to_dict(_collect_lines),
        "collect_opens": sorted(_collect_opens),
        "tests": tests,
        "outcomes": _outcomes,
    }
    with open(OUT, "w") as f:
        json.dump(data, f)
