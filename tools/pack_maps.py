"""Convert maps written by build_maps.py into the packed format the selector loads fastest.

    python tools/pack_maps.py MAPS_IN MAPS_OUT

Packed format (marshal + zlib; marshal needs no import and loads faster than pickle):
  core.bin            core tables, plus the page index
  f/<id>.src          the pinned source of file <id> (not committed: the selector reads the
                      pinned tree in the image instead, which is the same text)
  f/<id>_<page>.bin   per-line facts for lines [page*P, (page+1)*P): test bits, import-time
                      lines, code lines, statement chains, impacts, lambda alternatives
  r/<bucket>.bin      name-reference index, split by crc32(name) % NBUCKETS
Run with the scorer image's Python (3.13) so marshal's format matches.
"""

import marshal
import os
import pickle
import shutil
import sys
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from blast.pyimpact import file_pages  # noqa: E402

PAGE = 128
NBUCKETS = 64


def load(path):
    with open(path, "rb") as fh:
        return pickle.loads(zlib.decompress(fh.read()))


def dump(obj, path):
    with open(path, "wb") as fh:
        fh.write(zlib.compress(marshal.dumps(obj, 4), 9))


def main():
    src_dir, out = sys.argv[1], sys.argv[2]
    core = load(os.path.join(src_dir, "core.pkl"))
    refs = load(os.path.join(src_dir, "refs.pkl"))
    if os.path.exists(out):
        shutil.rmtree(out)
    os.makedirs(os.path.join(out, "f"))
    os.makedirs(os.path.join(out, "r"))

    file_index = {}
    pages_present = {}
    srcs = set()
    for fid, (path, name) in enumerate(sorted(core["file_index"].items())):
        blob = load(os.path.join(src_dir, "files", name))
        file_index[path] = fid
        pages = {}

        def page(l):
            p = l // PAGE
            return pages.setdefault(p, {})

        for l, bits in blob["lines"].items():
            page(l).setdefault("bits", {})[l] = bits
        for l in blob["import_lines"]:
            page(l).setdefault("il", set()).add(l)
        src = blob.get("src")
        if src is not None:
            dump(src, os.path.join(out, "f", f"{fid}.src"))
            srcs.add(fid)
            try:
                facts = file_pages(path, src, PAGE)
            except SyntaxError:
                facts = {}
            for p, pg in facts.items():
                d = pages.setdefault(p, {})
                d["code"] = pg["code"]
                d["cont"] = pg["cont"]
                if pg["imp"]:
                    d["imp"] = pg["imp"]
                if pg["lam"]:
                    d["lam"] = pg["lam"]
                if pg["chain"]:
                    d["chain"] = pg["chain"]
        for p, d in pages.items():
            if "il" in d:
                d["il"] = frozenset(d["il"])
            dump(d, os.path.join(out, "f", f"{fid}_{p}.bin"))
        pages_present[fid] = frozenset(pages)

    buckets = [{"rt": {}, "it": {}} for _ in range(NBUCKETS)]
    for name, bits in refs["refs_rt"].items():
        buckets[zlib.crc32(name.encode()) % NBUCKETS]["rt"][name] = bits
    for name, entries in refs["refs_it"].items():
        buckets[zlib.crc32(name.encode()) % NBUCKETS]["it"][name] = tuple(
            tuple(tuple(x) if isinstance(x, list) else x for x in e) for e in entries)
    for i, bk in enumerate(buckets):
        dump(bk, os.path.join(out, "r", f"{i}.bin"))

    core = dict(core)
    core["file_index"] = file_index
    core["pages"] = pages_present
    core["srcs"] = frozenset(srcs)
    core["page_size"] = PAGE
    core["nbuckets"] = NBUCKETS
    core["refs_imp"] = frozenset(refs.get("refs_imp", ()))
    core["rdeps"] = {k: tuple(v) for k, v in core["rdeps"].items()}
    dump(core, os.path.join(out, "core.bin"))
    total = sum(os.path.getsize(os.path.join(dp, x)) for dp, _, fs in os.walk(out) for x in fs)
    nfiles = sum(len(fs) for _, _, fs in os.walk(out))
    print(f"packed {len(file_index)} source files into {nfiles} files, {total} bytes", file=sys.stderr)


if __name__ == "__main__":
    main()
