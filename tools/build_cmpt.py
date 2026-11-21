"""Build maps/cmpt.bin: per comparison site, which tests can see each operator swap.

    python tools/build_cmpt.py CMP_DIR TESTS_JSON SQLGLOT_ROOT LINE_TRACE_DIR OUT_FILE [TRACE_LOG]

CMP_DIR holds tools/cmp_trace.py output: <number>.json per test (disagreements seen while
that test ran alone, collection included) and _import.json (disagreements while importing
sqlglot, which every test sees). A test with no profile, or whose profiled run did not pass
(listed in TRACE_LOG), is counted as seeing every swap on every line it executes
(LINE_TRACE_DIR/<number>.json from tools/trace_fork.py).

Output: {path: {line: ((col, op, {alt: test_bits}), ...)}} as zlib-compressed marshal.
"""

import ast
import glob
import json
import marshal
import os
import re
import sys
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tools import cmp_trace as ct  # noqa: E402


def sites_of(root):
    out = {}
    for p in sorted(glob.glob(os.path.join(root, "sqlglot", "**", "*.py"), recursive=True)):
        rel = os.path.relpath(p, root).replace(os.sep, "/")
        src = open(p, encoding="utf-8").read()
        sites = {}

        class Collect(ct._Rewrite):
            def visit_Compare(self, node):
                self.generic_visit(node)
                if len(node.ops) != 1 or type(node.ops[0]) not in ct.OPS:
                    return node
                left, right = node.left, node.comparators[0]
                if not (left.end_lineno == right.lineno == node.lineno):
                    return node
                op = ct.OPS[type(node.ops[0])]
                col = ct._op_col(self.lines[node.lineno - 1], left, right, op)
                if col is not None:
                    sites[(node.lineno, col)] = op
                return node

        Collect(0, src.split("\n")).visit(ast.parse(src))
        if sites:
            out[rel] = sites
    return out


def main():
    cmp_dir, tests_json, root, trace_dir, out = sys.argv[1:6]
    log = sys.argv[6] if len(sys.argv) > 6 else None
    tests = json.load(open(tests_json))["tests"]
    n_all = len(tests)
    every = (1 << n_all) - 1
    bad_ids = set()
    if log and os.path.exists(log):
        for line in open(log):
            m = re.match(r"(\S+::\S+): exit (-?\d+)", line.strip())
            if m and m.group(2) != "0":
                bad_ids.add(m.group(1))

    sites = sites_of(root)
    dis = {}  # (path, line, col, alt) -> bits

    def add(key, bit):
        dis[key] = dis.get(key, 0) | bit

    imp = json.load(open(os.path.join(cmp_dir, "_import.json")))
    for fid, line, col, alt in imp["import_dis"]:
        add((imp["files"][fid], line, col, alt), every)

    unprofiled = []
    for t in tests:
        n = t["number"]
        p = os.path.join(cmp_dir, f"{n}.json")
        if t["id"] in bad_ids or not os.path.exists(p):
            unprofiled.append(n)
            continue
        d = json.load(open(p))
        files = d["files"]
        for fid, line, col, alt in d["dis"]:
            add((files[fid], line, col, alt), 1 << n)

    for n in unprofiled:
        tr = os.path.join(trace_dir, f"{n}.json")
        lines = set()
        if os.path.exists(tr):
            d = json.load(open(tr))
            groups = [ph["lines"] for phases in d.get("tests", {}).values() for ph in phases.values()]
            groups.append(d.get("collect_lines", {}))
            for g in groups:
                for f, ls in g.items():
                    lines.update((f, l) for l in ls)
            touched = lambda f, l: (f, l) in lines  # noqa: E731
        else:
            touched = lambda f, l: True  # noqa: E731  no trace at all: assume everything
        for f, fs in sites.items():
            for (line, col), op in fs.items():
                if touched(f, line):
                    for alt in ct.ALTS[op]:
                        add((f, line, col, alt), 1 << n)

    # and/or swaps (tools/bool_trace.py): one entry per operator of each instrumented chain
    bool_sites = {}
    bool_dir = os.environ.get("BOOL_DIR")
    if bool_dir:
        bimp = json.load(open(os.path.join(bool_dir, "_import.json")))
        bsites = bimp["sites"]
        bdis = {}
        for sid, pos in bimp["dis"]:
            bdis[(sid, pos)] = every
        b_unprofiled = []
        for t in tests:
            n = t["number"]
            p = os.path.join(bool_dir, f"{n}.json")
            if not os.path.exists(p):
                b_unprofiled.append(n)
                continue
            bit = 1 << n
            for sid, pos in json.load(open(p))["dis"]:
                bdis[(sid, pos)] = bdis.get((sid, pos), 0) | bit
        touched_by = {}
        for n in b_unprofiled:
            tr = os.path.join(trace_dir, f"{n}.json")
            lines = None
            if os.path.exists(tr):
                d = json.load(open(tr))
                lines = set()
                groups = [ph["lines"] for phases in d.get("tests", {}).values() for ph in phases.values()]
                groups.append(d.get("collect_lines", {}))
                for g in groups:
                    for f, ls in g.items():
                        lines.update((f, l) for l in ls)
            touched_by[n] = lines
        for sid, (f, positions, is_and) in enumerate(bsites):
            op, alt = ("and", "or") if is_and else ("or", "and")
            for pos, (line, col) in enumerate(positions):
                bits = bdis.get((sid, pos), 0)
                for n, lines in touched_by.items():
                    # an unprofiled test sees the swap if it runs any line of the chain
                    span = range(min(l for l, _ in positions) - 1, max(l for l, _ in positions) + 2)
                    if lines is None or any((f, l) in lines for l in span):
                        bits |= 1 << n
                bool_sites.setdefault(f, {}).setdefault(line, []).append((col, op, {alt: bits}))

    table = {}
    nsites = nquiet = 0
    for f in sorted(set(sites) | set(bool_sites)):
        byline = {}
        for (line, col), op in sorted(sites.get(f, {}).items()):
            alts = {alt: dis.get((f, line, col, alt), 0) for alt in ct.ALTS[op]}
            nsites += 1
            nquiet += sum(1 for v in alts.values() if v == 0)
            byline.setdefault(line, []).append((col, op, alts))
        for line, entries in bool_sites.get(f, {}).items():
            for e in entries:
                nsites += 1
                nquiet += sum(1 for v in e[2].values() if v == 0)
                byline.setdefault(line, []).append(e)
        table[f] = {line: tuple(sorted(v, key=lambda e: e[0])) for line, v in byline.items()}
    with open(out, "wb") as fh:
        fh.write(zlib.compress(marshal.dumps(table), 9))
    print(f"{nsites} sites; {nquiet} (site, alternative) pairs no test can see; "
          f"{len(unprofiled)} unprofiled tests; {os.path.getsize(out)} bytes", file=sys.stderr)


if __name__ == "__main__":
    main()
