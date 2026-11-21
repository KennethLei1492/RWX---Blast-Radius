"""Check the import-diff selection against patches whose failing tests are known (in the image).

    python tools/recheck_isd.py MAPS_DIR EXP_DIR PATCH_DIR [EXP_DIR PATCH_DIR ...]

For every patch where the ordinary selection used an import-time fallback, take the import
diff (Verifier.import_diff) and the selection it gives (Selector.select_alt). Any known-failing
test left out is a miss. EXP_DIR holds tools/oracle_exp.py results, or is a labeled set
directory with answers.json (then PATCH_DIR is its commits directory).
"""

import glob
import json
import marshal
import os
import resource
import sys
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from blast.select import Selector  # noqa: E402
from blast.verify import Verifier  # noqa: E402


def cpu():
    r = resource.getrusage(resource.RUSAGE_CHILDREN)
    return r.ru_utime + r.ru_stime


def load(exp):
    ans = os.path.join(exp, "answers.json")
    if os.path.exists(ans):
        return {c: v["failing"] for c, v in json.load(open(ans))["commits"].items()}
    out = {}
    for p in glob.glob(os.path.join(exp, "*.json")):
        d = json.load(open(p))
        if "failing" in d and "ideal_ms" in d:
            out[os.path.basename(p)[:-5]] = d["failing"]
    return out


def main():
    maps = sys.argv[1]
    sel = Selector(maps)
    base = marshal.loads(zlib.decompress(open(os.path.join(maps, "imstate.bin"), "rb").read()))
    v = Verifier()
    km = sel.key_map()
    tables = km["tables"] if km else None
    pairs = sys.argv[2:]
    show = int(os.environ.get("SHOW", "25"))
    for exp, pdir in zip(pairs[::2], pairs[1::2]):
        truth = load(exp)
        n = unc = used = none = miss = worse = 0
        st = al = best = 0
        c0 = cpu()
        rows = []
        for cid in sorted(truth):
            patch = open(os.path.join(pdir, cid + ".patch"), encoding="utf-8").read()
            bits, notes = sel.select(patch)
            n += 1
            cost = sel.cost(sel.pack(bits))
            st += cost
            if not (sel.uncertain or sel.table_edit):
                al += cost
                best += cost
                continue
            unc += 1
            d = v.import_diff(patch, base, timeout=60, tables=tables)
            alt = sel.select_alt(patch, d, v.entry) if d is not None else None
            if alt is None:
                none += 1
                al += cost
                best += cost
                continue
            used += 1
            acost = sel.cost(sel.pack(alt))
            al += acost
            best += min(cost, acost)
            worse += acost > cost
            lost = [f for f in truth[cid] if not alt >> f & 1]
            if lost:
                miss += 1
                print("MISS", cid, lost[:6], notes, d[:6])
            rows.append((cost - acost, cid, bits.bit_count(), alt.bit_count(), len(truth[cid]), len(d), " ".join(notes)[:60]))
        for r in sorted(rows, reverse=True)[:show]:
            print("  saved %7.3fs %s sel %d -> %d fail=%d keys=%d %s" % ((r[0] / 1000,) + r[1:]))
        print(f"{exp}: {n} patches, {unc} uncertain, import diff usable for {used} (unusable {none}), "
              f"worse in {worse}; static={st / 1000:.1f}s alt={al / 1000:.1f}s best={best / 1000:.1f}s "
              f"misses={miss}; cpu {cpu() - c0:.1f}s", flush=True)


if __name__ == "__main__":
    main()
