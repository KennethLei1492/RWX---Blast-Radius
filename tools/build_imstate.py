"""Write maps/imstate.bin: the import-state fingerprint of the pinned sqlglot (run in the image).

    IMSTATE_PERTURB=<n> python tools/build_imstate.py OUT.<i>      one perturbed run
    python tools/build_imstate.py merge OUT_FILE RUN...            combine the runs

Each run imports every sqlglot module exactly as blast/verify.py's children do and stores the
fingerprints (blast/imstate.py) in three modes. A key keeps the strictest mode in which every
run agrees (0 exact, 1 ignoring dict order, 2 ignoring list/tuple order too); keys that never
agree are listed as unstable and are not compared at selection time.
"""

import marshal
import os
import sys
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def main():
    if sys.argv[1] == "merge":
        out, parts = sys.argv[2], sys.argv[3:]
        runs = [marshal.loads(zlib.decompress(open(p, "rb").read())) for p in parts]
        fp, unstable = {}, []
        counts = [0, 0, 0]
        for k in runs[0][0]:
            for mode in (0, 1, 2):
                vals = {r[mode].get(k) for r in runs}
                if len(vals) == 1 and None not in vals:
                    fp[k] = (mode, bytes.fromhex(vals.pop()))
                    counts[mode] += 1
                    break
            else:
                unstable.append(k)
        same_keys = len({frozenset(r[0]) for r in runs}) == 1
        with open(out, "wb") as fh:
            fh.write(zlib.compress(marshal.dumps({"fp": fp, "unstable": tuple(sorted(unstable))}), 9))
        print(f"{counts[0]} exact keys, {counts[1]} ignoring dict order, {counts[2]} ignoring all order, "
              f"{len(unstable)} unstable {unstable[:6]}; same key set in every run: {same_keys}; "
              f"{os.path.getsize(out)} bytes", file=sys.stderr)
        return
    from blast import verify

    snap = verify.baseline_snapshot()
    with open(sys.argv[1], "wb") as fh:
        fh.write(zlib.compress(marshal.dumps(snap), 9))
    print(f"{len(snap[0])} keys", file=sys.stderr)


if __name__ == "__main__":
    main()
