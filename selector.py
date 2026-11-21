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


PLAN_MIN_MS = 12000   # probe only when dropping test_execute_tpcds saves at least this much


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
        try:
            spans = sel.body_lines(patches[cid], sel.m["ids"].index(planfp.TEST_ID))
        except Exception:
            spans = None
        if spans is None:
            continue
        lines = blob["lines"]
        if any(any(a <= l <= b for l in lines.get(f, ())) for f, ss in spans.items() for a, b in ss):
            continue   # an edited function runs while plans are executed
        todo.append((saving, cid, sel.body_at_import))
    if not todo:
        return {}
    v = Verifier()
    if not v.start(heavy=len(todo) >= 2):
        return {}
    import resource

    start = time.monotonic()
    results = {}
    imstate = None
    for saving, cid, at_import in sorted(todo, reverse=True):
        if time.monotonic() - start > float(os.environ.get("BLAST_PLAN_BUDGET", "60")) or wall_left() < 45:
            print(f"{cid}: no time left for the planning check", file=sys.stderr)
            continue
        c0 = resource.getrusage(resource.RUSAGE_CHILDREN)
        if at_import:
            # an edited function also runs while modules are imported: the import must
            # leave exactly the pinned state (blast/imstate.py) for the argument to hold
            if imstate is None:
                try:
                    with open(os.path.join(HERE, "maps", "imstate.bin"), "rb") as fh:
                        imstate = marshal.loads(zlib.decompress(fh.read()))
                except OSError:
                    imstate = False
            diff = v.import_diff(patches[cid], imstate, timeout=20) if imstate else None
            if diff != []:
                print(f"{cid}: planning check not possible, the import "
                      f"{'is unknown' if diff is None else 'differs'}; kept", file=sys.stderr)
                continue
        same = v.plan_same(patches[cid], blob["base"], timeout=30)
        c1 = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu = c1.ru_utime + c1.ru_stime - c0.ru_utime - c0.ru_stime
        if same:
            print(f"{cid}: planning check {cpu:.3f}s cpu: plans unchanged, dropping test_execute_tpcds "
                  f"(saves {saving / 1000:.1f}s)", file=sys.stderr)
            results[cid] = static[cid] - {num}
        else:
            print(f"{cid}: planning check {cpu:.3f}s cpu: "
                  f"{'inconclusive' if same is None else 'differs at ' + str(v.plan_at)}; kept", file=sys.stderr)
    return results


def verify_cheap_commits(sel, tests, ms, to_num, patches, static):
    """Run the candidate tests of commits where that is expected to cost less than printing
    them, and keep only the tests that really fail (blast/verify.py, blast/policy.py)."""
    from blast import policy
    from blast.verify import Verifier

    start = time.monotonic()
    budget = float(os.environ.get("BLAST_VERIFY_BUDGET", min(110.0, 1.5 * max(len(patches), 1))))
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
    return results


ISD_MIN_MS = 3500   # only commits whose ordinary selection costs at least this much


def import_diff_commits(sel, tests, ms, to_num, patches, static, uncertain):
    """For commits whose selection fell back on "whatever the import may have changed": import
    the patched tree in a child, see which class attributes and module globals really differ
    from the pinned import (blast/imstate.py), and select the tests that run the changed lines
    plus the recorded readers of what differs. Kept only when it is cheaper."""
    import marshal
    import zlib

    from blast.verify import Verifier

    todo = [c for c in static if uncertain.get(c) and c in patches and cost_ms(static[c], ms) >= ISD_MIN_MS]
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
    results = {}
    file_of = {t["number"]: t["file"] for t in tests}
    for cid in sorted(todo, key=lambda c: -cost_ms(static[c], ms)):
        if time.monotonic() - start > float(os.environ.get("BLAST_ISD_BUDGET", "45")) or wall_left() < 70:
            break
        diff = v.import_diff(patches[cid], base, timeout=20, tables=tables)
        if diff is None:
            print(f"{cid}: import diff unavailable", file=sys.stderr)
            continue
        bits = sel.select_alt(patches[cid], diff, v.entry)
        if bits is None:
            print(f"{cid}: import diff has keys without readers ({len(diff)}); kept", file=sys.stderr)
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
            print(f"{cid}: import diff ({len(diff)} keys): {len(static[cid])} -> {len(alt)} tests", file=sys.stderr)
            results[cid] = alt
    return results


KNOWN: set = set()


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
            uncertain[cid] = sel.uncertain or sel.table_edit
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

    try:
        static.update(import_diff_commits(sel, tests, ms, to_num, patches, static, uncertain))
    except Exception as e:  # the ordinary selections stand
        print(f"import diff skipped: {e!r}", file=sys.stderr)

    try:
        static.update(plan_check_commits(sel, tests, ms, patches, static))
    except Exception as e:  # the selections stand
        print(f"planning check skipped: {e!r}", file=sys.stderr)

    final = dict(static)
    try:
        final.update(verify_cheap_commits(sel, tests, ms, to_num, patches, static))
    except Exception as e:  # the static selections stand
        print(f"verification skipped: {e!r}", file=sys.stderr)

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
