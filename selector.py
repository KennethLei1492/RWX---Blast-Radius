"""Blast Radius selector: for each patch, the sqlglot tests it can break, packed onto workers.

Reads /data/tests.json and /data/commits/*.patch, prints one JSON line per commit.
The analysis uses maps built from the pinned sqlglot commit (see tools/ and README.md). Where
it pays for its CPU, the patched tree is also imported, planned on, or has cheap tests run.
"""

import json
import os
import sys
import time

T0 = time.monotonic()
WALL = float(os.environ.get("BLAST_WALL", "150"))   # seconds this program allows itself

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from blast.select import Selector  # noqa: E402

DATA = os.environ.get("BLAST_DATA", "/data")


def pack(numbers, ms):
    """Longest first onto the least loaded worker, for the k in 1..8 with the lowest cost."""
    if not numbers:
        return []
    tests = sorted(numbers, key=lambda t: (-ms[t], t))
    best = None
    for k in range(1, min(8, len(tests)) + 1):
        loads = [0] * k
        ws = [[] for _ in range(k)]
        for t in tests:
            i = loads.index(min(loads))
            ws[i].append(t)
            loads[i] += ms[t]
        cost = max(loads) + 2000 * k
        if best is None or cost < best[0]:
            best = (cost, ws)
        if max(loads) == ms[tests[0]]:
            break  # the longest test alone bounds the cost; more workers only add
    return best[1]


def cost_ms(numbers, ms):
    ws = pack(numbers, ms)
    return (max(sum(ms[t] for t in w) for w in ws) + 2000 * len(ws)) if ws else 0


def wall_left():
    return WALL - (time.monotonic() - T0)


# Each stage below may run twice (see main): its time budget is shared by both passes.
_SPENT: dict = {}


def budget_left(key, default):
    return float(os.environ.get(f"BLAST_{key.upper()}_BUDGET", default)) - _SPENT.get(key, 0.0)


def spend(key, since):
    _SPENT[key] = _SPENT.get(key, 0.0) + (time.monotonic() - since)


PLAN_MIN_MS = 12000   # probe only when dropping test_execute_tpcds saves at least this much
# Queries whose plans differ are run for real, up to this share of the saving (estimated from
# each query's share of the test's execution time) and this many queries.
PLAN_RUN_FRAC = float(os.environ.get("BLAST_PLAN_RUN_FRAC", "0.4"))
PLAN_COLLECT = int(os.environ.get("BLAST_PLAN_COLLECT", "6"))


def plan_check_commits(sel, tests, ms, patches, static):
    """test_execute_tpcds (21 s) spends nearly all its time executing plans. For a commit
    that selects it but only edits functions that never run during that execution, plan its
    queries on the patched tree (a few seconds of CPU) and drop the test when everything
    handed to the executor is unchanged (blast/planfp.py)."""
    import marshal
    import zlib

    from blast import planfp
    from blast.verify import Verifier

    num = next((t["number"] for t in tests if t["id"] == planfp.TEST_ID), None)
    if num is None or num not in KNOWN:
        return {}
    todo = []
    blob = None
    for cid, S in static.items():
        if num not in S or cid not in patches:
            continue
        saving = cost_ms(S, ms) - cost_ms(S - {num}, ms)
        if saving < PLAN_MIN_MS:
            continue
        if blob is None:
            try:
                with open(os.path.join(HERE, "maps", "tpcds.bin"), "rb") as fh:
                    blob = marshal.loads(zlib.decompress(fh.read()))
            except OSError:
                return {}
        bit = sel.m["ids"].index(planfp.TEST_ID)
        try:
            spans = sel.body_lines(patches[cid], bit)
        except Exception:
            spans = None
        mixed = False
        if spans is None:
            # Only class-body or module-level statements changed: their whole effect is the
            # import-state difference, checked key by key against what the execution part reads.
            try:
                if "xattrs" in blob and sel.import_only(patches[cid], bit):
                    todo.append((saving, cid, ("keys", frozenset()), None, None))
                    continue
            except Exception:
                continue
            # Function bodies and class-body or module-level statements together: both checks.
            try:
                spans = sel.body_lines(patches[cid], bit, allow_import=True) if "xattrs" in blob else None
            except Exception:
                spans = None
            if spans is None or not sel.body_import:
                continue
            mixed = True
        lines = blob["lines"]
        force = None
        if any(any(a <= l <= b for l in lines.get(f, ())) for f, ss in spans.items() for a, b in ss):
            # an edited function runs while plans are executed: only the queries whose execution
            # ran an edited statement on the pinned commit need running, if they are few and cheap
            force = queries_executing(blob, sel.body_spans)
            if force is None:
                continue
            cost = sum((blob.get("qshare") or [1.0] * 99)[i] for i in range(force.bit_length()) if force >> i & 1)
            if bin(force).count("1") > PLAN_COLLECT or \
                    cost * ms[num] / 1000 > max(0.0, PLAN_RUN_FRAC * saving / 1000 - 2.0):
                continue
        if mixed:
            # the import-time edits may change what any query's planning sees: plan them all
            todo.append((saving, cid, ("keys", frozenset(sel.body_expected)), None, force))
            continue
        todo.append((saving, cid, (sel.body_at_import, frozenset(sel.body_expected)),
                     queries_reaching(blob, sel.body_spans), force))
    if not todo:
        return {}
    v = Verifier()
    if not v.start(heavy=False):   # the probe stands in for duckdb/numpy/pandas (planfp._lazy_heavy)
        return {}

    start = time.monotonic()
    allowed = budget_left("plan", 60)
    results = {}
    imstate = None
    try:
        return _plan_loop(todo, start, allowed, results, imstate, v, patches, blob, ms, num, static, sel)
    finally:
        spend("plan", start)


def read_in_execution(blob, keys, sel=None):
    """The first import-state key that the TPC-DS test's class set-up or execution part may
    read (maps/tpcds.bin: the class attributes recorded there, and every name loaded on the
    lines they run), or None. A key that cannot be judged counts as read. A symbol the patch
    adds whose name occurs nowhere in the pinned tree cannot be read by the pinned execution."""
    xa, xn = blob.get("xattrs"), blob.get("xnames")
    if xa is None or xn is None:
        return keys[0] if keys else None
    for k in keys:
        if k == "<modules>" or k.endswith(".<bases>"):
            return k
        if k.startswith("+"):
            sym = k[1:].partition(":")[2]
            nm = sym.rsplit(".", 1)[-1] if sym else ""
            if nm and sel is not None and sel._name_absent_from_pinned(nm):
                continue
            return k
        mod, _, rest = k.partition(":")
        if "." in rest:
            cls, _, attr = rest.rpartition(".")
            if f"{mod}:{cls}\x1f{attr}" in xa or f"*\x1f{attr}" in xa or attr in xn:
                return k
        elif rest in xn:
            return k
    return None


def queries_executing(blob, bodies):
    """Bitmask of the TPC-DS queries whose execution ran an edited statement on the pinned
    commit (maps/tpcds.bin "xlines", all queries in order in one process); None when an edited
    statement runs in the class set-up or on an execution line no single query is credited
    with, or that is not known."""
    xl, xs = blob.get("xlines"), blob.get("xsetup")
    if xl is None or xs is None or bodies is None:
        return None
    lines = blob["lines"]
    mask = 0
    for f, ss in bodies.items():
        inside = lambda ln: any(a <= ln <= b for a, b in ss)  # noqa: E731
        if any(inside(ln) for ln in xs.get(f, ())):
            return None
        d = xl.get(f, {})
        if any(inside(ln) and ln not in d for ln in lines.get(f, ())):
            return None
        for ln, m in d.items():
            if inside(ln):
                mask |= m
    return mask


def queries_reaching(blob, bodies):
    """Bitmask of the TPC-DS queries whose planning ran an edited statement on the pinned
    commit (maps/tpcds.bin "qlines": each query alone, and all in order in one process); None
    when that is not known, or when an edited statement is one some query runs only after
    others ran before it ("qwarm"), so which queries reach it depends on the order."""
    ql = blob.get("qlines")
    warm = blob.get("qwarm")
    if ql is None or warm is None or bodies is None:
        return None
    mask = 0
    for f, ss in bodies.items():
        w = warm.get(f, ())
        if any(a <= ln <= b for ln in w for a, b in ss):
            return None
        d = ql.get(f)
        if not d:
            continue
        for ln, m in d.items():
            if any(a <= ln <= b for a, b in ss):
                mask |= m
    return mask


def _plan_loop(todo, start, allowed, results, imstate, v, patches, blob, ms, num, static, sel):
    import marshal
    import resource
    import zlib

    for saving, cid, at_import, only, force in sorted(todo, key=lambda t: t[:2], reverse=True):
        if time.monotonic() - start > allowed or wall_left() < 45:
            print(f"{cid}: no time left for the planning check", file=sys.stderr)
            continue
        c0 = resource.getrusage(resource.RUSAGE_CHILDREN)
        at_import, expected = at_import
        if at_import == "keys":
            # class-body or module-level edits: the import diff lists everything they change;
            # none of it may be read while tables load or plans execute
            if imstate is None:
                try:
                    with open(os.path.join(HERE, "maps", "imstate.bin"), "rb") as fh:
                        imstate = marshal.loads(zlib.decompress(fh.read()))
                except OSError:
                    imstate = False
            diff = v.import_diff(patches[cid], imstate, timeout=20) if imstate else None
            hit = "unknown" if diff is None else read_in_execution(blob, [k for k in diff if k not in expected], sel)
            if hit is not None:
                print(f"{cid}: planning check not possible, the execution part may read {hit}; kept",
                      file=sys.stderr)
                continue
        elif at_import:
            # an edited function also runs while modules are imported: the import must
            # leave exactly the pinned state (blast/imstate.py) for the argument to hold,
            # apart from the functions whose parameter defaults the patch changes (the probe
            # runs those patched functions anyway)
            if imstate is None:
                try:
                    with open(os.path.join(HERE, "maps", "imstate.bin"), "rb") as fh:
                        imstate = marshal.loads(zlib.decompress(fh.read()))
                except OSError:
                    imstate = False
            diff = v.import_diff(patches[cid], imstate, timeout=20) if imstate else None
            if diff is not None:
                diff = [k for k in diff if k not in expected]
            if diff != []:
                print(f"{cid}: planning check not possible, the import "
                      f"{'is unknown' if diff is None else 'differs'}; kept", file=sys.stderr)
                continue
        # The probe may spend at most about what dropping the test saves: a wall-clock limit of
        # the saving for planning, plus up to 0.4 x the saving (less the cost of loading the
        # tables) for running the few queries whose plans changed.
        saving_s = saving / 1000
        run_limit = max(0.0, PLAN_RUN_FRAC * saving_s - 2.0)
        n_forced = bin(force).count("1") if force else 0
        if force:   # the forced queries' share of the budget is spent already
            qs = blob.get("qshare") or [1.0] * 99
            run_limit = max(0.0, run_limit - ms[num] / 1000 * sum(qs[i] for i in range(force.bit_length()) if force >> i & 1))
        same = v.plan_same(patches[cid], blob["base"], timeout=min(60.0, saving_s + 2 * run_limit),
                           run_limit=run_limit, qshare=blob.get("qshare"), test_seconds=ms[num] / 1000,
                           only=only, collect=max(0, PLAN_COLLECT - n_forced), force=force)
        c1 = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu = c1.ru_utime + c1.ru_stime - c0.ru_utime - c0.ru_stime
        planned = "all" if only is None else bin(only).count("1")
        if same:
            how = f"queries {v.plan_ran} pass when run" if v.plan_ran else f"plans unchanged ({planned} planned)"
            print(f"{cid}: planning check {cpu:.3f}s cpu: {how}, dropping test_execute_tpcds "
                  f"(saves {saving / 1000:.1f}s)", file=sys.stderr)
            results[cid] = static[cid] - {num}
        else:
            print(f"{cid}: planning check {cpu:.3f}s cpu ({planned} planned): "
                  f"{'inconclusive' if same is None else 'differs at ' + str(v.plan_at)}; kept", file=sys.stderr)
    return results


def verified_cheaply(sel, tests, ms, to_num, patch, S):
    """Will the verify stage run all of S, for less CPU than an import diff costs? Then the
    import diff can only add CPU: running the tests gives the exact failing ones."""
    from blast import policy

    file_of = {t["number"]: t["file"] for t in tests}
    spawn_bits = sel.m.get("spawners", 0)
    spawners = {to_num[i] for i in range(len(to_num)) if spawn_bits >> i & 1 and to_num[i] is not None}
    U = {t for t in S if t in spawners or file_of[t] in policy.UNVERIFIABLE_FILES}
    if U or not S:
        return False
    go, est = policy.decide(patch, S, S, U, ms, lambda s: cost_ms(s, ms), file_of,
                            policy.costs(os.path.join(HERE, "maps")))
    return go and est < ISD_CPU_MS


def verify_cheap_commits(sel, tests, ms, to_num, patches, static):
    """Run the candidate tests of commits where that is expected to cost less than printing
    them, and keep only the tests that really fail (blast/verify.py, blast/policy.py)."""
    from blast import policy
    from blast.verify import Verifier

    start = time.monotonic()
    budget = budget_left("verify", min(110.0, 1.5 * max(TOTAL_COMMITS[0], 1)))
    budget = min(budget, wall_left() - 8)
    v = Verifier()
    file_of = {t["number"]: t["file"] for t in tests}
    id_of = {t["number"]: t["id"] for t in tests}
    spawn_bits = sel.m.get("spawners", 0)
    spawners = {to_num[i] for i in range(len(to_num)) if spawn_bits >> i & 1 and to_num[i] is not None}

    table = policy.costs(os.path.join(HERE, "maps"))
    plans = []
    for cid, S in static.items():
        if not S or cid not in patches:
            continue
        U = {t for t in S if t in spawners or file_of[t] in policy.UNVERIFIABLE_FILES}
        V = S - U
        go, est = policy.decide(patches[cid], S, V, U, ms, lambda s: cost_ms(s, ms), file_of, table)
        if go:
            gain = cost_ms(S, ms) - cost_ms(U, ms) - est
            plans.append((gain, cid, V, U, est))
    plans.sort(key=lambda p: -p[0])
    if not plans:
        return {}
    heavy_files = {"tests/test_optimizer.py", "tests/test_executor.py"}
    heavy_runs = sum(1 for p in plans if any(file_of[t] in heavy_files for t in p[2]))
    if not v.start(heavy=heavy_runs >= 2):
        print("verification unavailable (no sqlglot tree)", file=sys.stderr)
        return {}
    print(f"verifying {len(plans)} commits ({heavy_runs} with optimizer/executor tests)", file=sys.stderr)

    results = {}
    try:
        _verify_loop(plans, budget, start, v, patches, id_of, results)
    finally:
        spend("verify", start)
    return results


def _verify_loop(plans, budget, start, v, patches, id_of, results):
    for gain, cid, V, U, est in plans:
        left = budget - (time.monotonic() - start)
        if left < 2:
            print(f"{cid}: verification budget spent; static selection", file=sys.stderr)
            continue
        timeout = min(left, 10 + 4 * est / 1000)
        import resource

        c0 = resource.getrusage(resource.RUSAGE_CHILDREN)
        failed = v.run(patches[cid], [id_of[t] for t in sorted(V)], timeout)
        c1 = resource.getrusage(resource.RUSAGE_CHILDREN)
        print(f"{cid}: verify cpu {c1.ru_utime + c1.ru_stime - c0.ru_utime - c0.ru_stime:.3f}s "
              f"(estimated {est / 1000:.3f}s)", file=sys.stderr)
        if failed is None:
            print(f"{cid}: verification inconclusive; static selection", file=sys.stderr)
            continue
        num_of = {id_of[t]: t for t in V}
        kept = {num_of[i] for i in failed if i in num_of} | U
        print(f"{cid}: ran {len(V)} tests, {len(failed)} failed; printing {len(kept)}", file=sys.stderr)
        results[cid] = kept


ISD_MIN_MS = 2300   # only commits whose ordinary selection costs at least this much
ISD_TABLE_MIN_MS = 2000   # a class or module table edit: its entries usually have few readers
# An import diff costs about this much CPU (scorer, ms); a selection the verify stage will run
# for less anyway gets its exact failing tests there, so the import diff is skipped for it.
ISD_CPU_MS = 1200


def import_diff_commits(sel, tests, ms, to_num, patches, static, uncertain):
    """For commits whose selection fell back on "whatever the import may have changed": import
    the patched tree in a child, see which class attributes and module globals really differ
    from the pinned import (blast/imstate.py), and select the tests that run the changed lines
    plus the recorded readers of what differs. Kept only when it is cheaper."""
    import marshal
    import zlib

    from blast.verify import Verifier

    todo = [c for c in static if uncertain.get(c) and c in patches
            and cost_ms(static[c], ms) >= (ISD_TABLE_MIN_MS if uncertain[c] == "table" else ISD_MIN_MS)]
    todo = [c for c in todo if not verified_cheaply(sel, tests, ms, to_num, patches[c], static[c])]
    if not todo:
        return {}
    try:
        with open(os.path.join(HERE, "maps", "imstate.bin"), "rb") as fh:
            base = marshal.loads(zlib.decompress(fh.read()))
    except OSError:
        return {}
    v = Verifier()
    if not v.start(heavy=False):
        return {}
    km = sel.key_map()
    tables = km["tables"] if km else None   # entry-level view of class-level dict tables
    start = time.monotonic()
    allowed = budget_left("isd", 45)
    results = {}
    try:
        _isd_loop(todo, start, allowed, v, base, tables, sel, patches, static, ms, to_num, results)
    finally:
        spend("isd", start)
    return results


def _isd_loop(todo, start, allowed, v, base, tables, sel, patches, static, ms, to_num, results):
    for cid in sorted(todo, key=lambda c: -cost_ms(static[c], ms)):
        if time.monotonic() - start > allowed or wall_left() < 70:
            break
        c0 = _children_cpu()
        diff = v.import_diff(patches[cid], base, timeout=20, tables=tables)
        cpu = _children_cpu() - c0
        if diff is None:
            print(f"{cid}: import diff unavailable ({cpu:.3f}s cpu)", file=sys.stderr)
            continue
        bits = sel.select_alt(patches[cid], diff, v.entry)
        if bits is None:
            print(f"{cid}: import diff has keys without readers ({len(diff)}, {cpu:.3f}s cpu); kept", file=sys.stderr)
            continue
        alt = set()
        while bits:
            low = bits & -bits
            n = to_num[low.bit_length() - 1]
            if n is not None:
                alt.add(n)
            bits ^= low
        # tests this build knows nothing about stay in, as they were
        alt |= {t for t in static[cid] if t not in KNOWN}
        if cost_ms(alt, ms) < cost_ms(static[cid], ms):
            print(f"{cid}: import diff ({len(diff)} keys, {cpu:.3f}s cpu): {len(static[cid])} -> {len(alt)} tests",
                  file=sys.stderr)
            results[cid] = alt
        else:
            print(f"{cid}: import diff ({len(diff)} keys, {cpu:.3f}s cpu): no cheaper selection", file=sys.stderr)


def _children_cpu():
    import resource

    r = resource.getrusage(resource.RUSAGE_CHILDREN)
    return r.ru_utime + r.ru_stime


KNOWN: set = set()
TOTAL_COMMITS = [0]


def touched_files(patch):
    import re

    return set(re.findall(r"^\+\+\+ b/(\S+)", patch, re.M)) | set(re.findall(r"^--- a/(\S+)", patch, re.M))


def core_files(sel):
    """Source files that `import sqlglot` and `import tests.helpers` load (from the import map)."""
    out = {"tests/__init__.py", "tests/helpers.py", "sqlglot/__init__.py"}
    for mod, users in sel.m.get("rdeps", {}).items():
        if mod == "sqlglot" or "sqlglot" in users:
            base = mod.replace(".", "/")
            out |= {base + ".py", base + "/__init__.py"}
    return out


def run_stages(sel, tests, ms, to_num, patches, static, uncertain, final):
    """The stages that fork children of this process for some commits: the import diff, the
    planning check and running cheap selections. `patches` holds the commits to handle."""
    if not patches:
        return
    try:
        static.update(import_diff_commits(sel, tests, ms, to_num, patches, static, uncertain))
    except Exception as e:  # the ordinary selections stand
        print(f"import diff skipped: {e!r}", file=sys.stderr)
    try:
        static.update(plan_check_commits(sel, tests, ms, patches, static))
    except Exception as e:  # the selections stand
        print(f"planning check skipped: {e!r}", file=sys.stderr)
    final.update({c: static[c] for c in patches})
    try:
        final.update(verify_cheap_commits(sel, tests, ms, to_num, patches, static))
    except Exception as e:  # the static selections stand
        print(f"verification skipped: {e!r}", file=sys.stderr)


def main():
    tests = json.load(open(os.path.join(DATA, "tests.json"), encoding="utf-8"))["tests"]
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    by_id = {t["id"]: t["number"] for t in tests}
    every = sorted(ms)

    sel = Selector(os.path.join(HERE, "maps"))
    # map index -> runtime test number (by id, in case numbering ever differs)
    to_num = [by_id.get(i) for i in sel.m["ids"]]
    known = {n for n in to_num if n is not None}
    KNOWN.update(known)
    unknown = [n for n in every if n not in known]
    unknown_files = {}
    for t in tests:
        if t["number"] in unknown:
            unknown_files.setdefault(t["file"], []).append(t["number"])

    commits = sorted(f[:-6] for f in os.listdir(os.path.join(DATA, "commits")) if f.endswith(".patch"))
    out = []
    patches = {}
    static = {}
    uncertain = {}
    for cid in commits:
        try:
            with open(os.path.join(DATA, "commits", cid + ".patch"), encoding="utf-8", errors="replace") as fh:
                patches[cid] = fh.read()
            bits, notes = sel.select(patches[cid])
            uncertain[cid] = "table" if sel.table_edit else sel.uncertain
            chosen = set()
            while bits:
                low = bits & -bits
                n = to_num[low.bit_length() - 1]
                if n is not None:
                    chosen.add(n)
                bits ^= low
            if unknown_files and chosen:
                chosen_files = {t["file"] for t in tests if t["number"] in chosen}
                for f in chosen_files & set(unknown_files):
                    chosen.update(unknown_files[f])
            print(f"{cid}: {len(chosen)} tests ({' '.join(notes)})", file=sys.stderr)
        except Exception as e:  # never miss because of a bug: run everything
            print(f"{cid}: error {e!r}; selecting all tests", file=sys.stderr)
            chosen = set(every)
        static[cid] = chosen

    # Every child that imports the patched tree starts with `import sqlglot`. Commits that
    # change none of the files that import loads are handled after this process has done that
    # import once (from the pinned tree, which for them is the patched one): their children
    # start where a fresh child would be after its first import, without paying for it again.
    # Commits that do change those files are handled first, while this process has imported
    # nothing of sqlglot.
    TOTAL_COMMITS[0] = len(commits)
    final = dict(static)
    core = core_files(sel)
    first = {c: p for c, p in patches.items() if touched_files(p) & core}
    later = {c: p for c, p in patches.items() if c not in first}
    run_stages(sel, tests, ms, to_num, first, static, uncertain, final)
    if later:
        from blast import verify

        loaded = verify.preload_core()
        if loaded is None:
            later = {}   # no sqlglot tree: nothing to fork for
        else:
            surprise = loaded - core
            if surprise:
                print(f"import sqlglot loaded files the map does not list: {sorted(surprise)[:5]}", file=sys.stderr)
            later = {c: p for c, p in later.items() if not touched_files(p) & loaded}
    run_stages(sel, tests, ms, to_num, later, static, uncertain, final)

    for cid in commits:
        workers = pack(final[cid], ms)
        out.append('{"commit":%s,"workers":%s}' % (json.dumps(cid), str(workers).replace(" ", "")))
    sys.stdout.write("\n".join(out) + ("\n" if out else ""))
    sys.stdout.flush()
    try:
        import resource

        r = resource.getrusage(resource.RUSAGE_SELF)
        c = resource.getrusage(resource.RUSAGE_CHILDREN)
        print(f"cpu: user {r.ru_utime:.3f}s sys {r.ru_stime:.3f}s, children {c.ru_utime + c.ru_stime:.3f}s, "
              f"{len(commits)} commits", file=sys.stderr)
    except Exception:
        pass


if __name__ == "__main__":
    main()
