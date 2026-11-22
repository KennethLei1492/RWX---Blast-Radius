"""Check the planning-only check (blast/planfp.py) against labeled patches.

  python -I -B tools/check_tpcds.py MAPS TPCDS.bin EXP_DIR PATCH_DIR [EXP_DIR PATCH_DIR ...]

EXP_DIR holds tools/oracle_exp.py results ({"ran": [...], "failing": [...]}). For every patch
whose selection holds test_execute_tpcds and whose real outcome for it is known, report
whether the check would drop it. Dropping it when it really fails is a miss.
"""
import glob
import json
import marshal
import os
import resource
import sys
import zlib

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.environ.setdefault("SKIP_INTEGRATION", "0")
os.environ.setdefault("OMP_NUM_THREADS", "1")
from blast import planfp, verify  # noqa: E402
from blast.select import Selector  # noqa: E402
from selector import queries_reaching, read_in_execution  # noqa: E402


def main():
    sel = Selector(sys.argv[1])
    blob = marshal.loads(zlib.decompress(open(sys.argv[2], "rb").read()))
    base, lines = blob["base"], blob["lines"]
    imstate = marshal.loads(zlib.decompress(open(os.path.join(sys.argv[1], "imstate.bin"), "rb").read()))
    bit = sel.m["ids"].index(planfp.TEST_ID)
    v = verify.Verifier()
    v.start(heavy=True)
    stats = {"dropped": 0, "kept, differs": 0, "kept, runs in execution": 0, "kept, not eligible": 0,
             "kept, unknown": 0, "kept, execution reads a changed key": 0}
    miss = 0
    cpu = 0.0
    saved = 0
    pairs = list(zip(sys.argv[3::2], sys.argv[4::2]))
    for exp, pdir in pairs:
        for p in sorted(glob.glob(os.path.join(exp, "*.json"))):
            d = json.load(open(p))
            if "failing" not in d:
                continue
            cid = os.path.basename(p)[:-5]
            try:
                patch = open(os.path.join(pdir, cid + ".patch"), encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            bits, notes = sel.select(patch)
            if not bits >> bit & 1:
                continue
            ran = set(d.get("ran", ())) or None
            if ran is not None and bit not in ran and bit not in d["failing"]:
                continue   # the oracle never ran it for this patch
            fails = bit in d["failing"]
            spans = sel.body_lines(patch, bit)
            at_import = sel.body_at_import
            only = queries_reaching(blob, sel.body_spans) if os.environ.get("NO_GATE") is None else None
            if spans is None and "xattrs" in blob and sel.import_only(patch, bit):
                r0 = resource.getrusage(resource.RUSAGE_CHILDREN)
                diff = v.import_diff(patch, imstate, 60)
                hit = "unknown" if diff is None else read_in_execution(blob, diff)
                same = None
                if hit is None:
                    saving_s = (sel.cost(sel.pack(bits)) - sel.cost(sel.pack(bits & ~(1 << bit)))) / 1000
                    run_limit = max(0.0, 0.4 * saving_s - 2.0)
                    same = v.plan_same(patch, base, 300, run_limit=run_limit, qshare=blob.get("qshare"),
                                       test_seconds=sel.ms[bit] / 1000)
                r1 = resource.getrusage(resource.RUSAGE_CHILDREN)
                c = r1.ru_utime + r1.ru_stime - r0.ru_utime - r0.ru_stime
                cpu += c
                if hit is not None:
                    what = "kept, execution reads a changed key"
                else:
                    what = "dropped" if same else ("kept, unknown" if same is None else "kept, differs")
                print(f"   {cid}: import-only probe {c:.1f}s cpu, {len(diff or [])} keys, read {hit}, "
                      f"first difference {getattr(v, 'plan_at', None)}", flush=True)
            elif spans is None:
                what = "kept, not eligible"
            elif any(any(a <= l <= b for l in lines.get(f, ())) for f, ss in spans.items() for a, b in ss):
                what = "kept, runs in execution"
            else:
                r0 = resource.getrusage(resource.RUSAGE_CHILDREN)
                same = None
                expected = set(sel.body_expected)
                idiff = v.import_diff(patch, imstate, 60) if at_import else []
                if idiff is not None:
                    idiff = [k for k in idiff if k not in expected]
                if not at_import or idiff == []:
                    # the selector's limits (selector.plan_check_commits), with a long timeout
                    # so that a loaded machine does not hide a wrong answer behind "unknown"
                    saving_s = (sel.cost(sel.pack(bits)) - sel.cost(sel.pack(bits & ~(1 << bit)))) / 1000
                    run_limit = max(0.0, 0.4 * saving_s - 2.0)
                    same = v.plan_same(patch, base, 300, run_limit=run_limit, qshare=blob.get("qshare"),
                                       test_seconds=sel.ms[bit] / 1000, only=only)
                else:
                    v.plan_at = "import"
                    same = False
                r1 = resource.getrusage(resource.RUSAGE_CHILDREN)
                c = r1.ru_utime + r1.ru_stime - r0.ru_utime - r0.ru_stime
                cpu += c
                what = "dropped" if same else ("kept, unknown" if same is None else "kept, differs")
                what_at = getattr(v, "plan_at", None)
                planned = "all" if only is None else bin(only).count("1")
                print(f"   {cid}: probe {c:.1f}s cpu, {planned} planned, first difference {what_at}, "
                      f"ran {getattr(v, 'plan_ran', None)}", flush=True)
            stats[what] += 1
            if what == "dropped" and fails:
                miss += 1
            print(f"{'MISS ' if what == 'dropped' and fails else ''}{cid} fails={fails} -> {what} ({notes[0][:60]})", flush=True)
    print(stats, "misses", miss, "probe cpu %.1fs" % cpu)


if __name__ == "__main__":
    main()
