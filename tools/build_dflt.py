"""Build maps/dflt.bin from tools/defaults_trace.py output.

    python tools/build_dflt.py DFLT_DIR TESTS_JSON SQLGLOT_ROOT OUT_FILE

{file: {line: ((col, end_col, default source text, bits of tests that used it), ...)}} for
every single-line default expression on an ASCII line. Defaults used while importing sqlglot
are left out of "sites" (the general rule handles them) and kept in "imp_sites", with the
tests that used them after the import: those apply only once an import diff has shown that
the import leaves the pinned state; tests without a profile count for every default.
"""

import json
import marshal
import os
import sys
import zlib


def main():
    dflt_dir, tests_json, root, out = sys.argv[1:5]
    tests = json.load(open(tests_json))["tests"]
    every = (1 << len(tests)) - 1
    imp = json.load(open(os.path.join(dflt_dir, "_import.json")))
    used = {}
    at_import = {(site, i) for site, i in imp["used"]}
    unprofiled = 0
    for t in tests:
        n = t["number"]
        p = os.path.join(dflt_dir, f"{n}.json")
        if not os.path.exists(p):
            unprofiled |= 1 << n
            continue
        bit = 1 << n
        for site, i in json.load(open(p))["used"]:
            used[(site, i)] = used.get((site, i), 0) | bit
    table = {}
    imp_table = {}
    n_sites = n_imp = 0
    src_cache = {}
    for site, (rel, def_line, spans) in enumerate(imp["sites"]):
        if rel not in src_cache:
            src_cache[rel] = open(os.path.join(root, rel), encoding="utf-8").read().split("\n")
        lines = src_cache[rel]
        for i, (name, l, c, el, ec) in enumerate(spans):
            if l != el or not lines[l - 1].isascii():
                continue
            text = lines[l - 1][c:ec]
            if not text:
                continue
            bits = used.get((site, i), 0) | unprofiled
            if (site, i) in at_import:
                # used while importing: the general rule, unless an import diff shows the
                # import unaffected; then these are the tests that used it afterwards
                imp_table.setdefault(rel, {}).setdefault(l, []).append((c, ec, text, bits))
                n_imp += 1
                continue
            table.setdefault(rel, {}).setdefault(l, []).append((c, ec, text, bits))
            n_sites += 1
    table = {f: {l: tuple(v) for l, v in d.items()} for f, d in table.items()}
    imp_table = {f: {l: tuple(v) for l, v in d.items()} for f, d in imp_table.items()}
    with open(out, "wb") as fh:
        fh.write(zlib.compress(marshal.dumps({"sites": table, "imp_sites": imp_table}), 9))
    print(f"{n_sites} defaults in {len(table)} files, {n_imp} more used while importing; "
          f"{bin(unprofiled).count('1')} unprofiled tests; {os.path.getsize(out)} bytes", file=sys.stderr)


if __name__ == "__main__":
    main()
