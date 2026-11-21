"""Build maps/cmp.bin from the comparison profiles written by tools/cmp_plugin.py.

    python tools/build_cmp.py PROFILE_DIR[,PROFILE_DIR...] SQLGLOT_ROOT OUT_FILE [UNPROFILED_TRACE...]

UNPROFILED_TRACE: per-test line traces (tools/trace_plugin.py output) of tests that were left
out of the profile (too slow to instrument); every site on a line they executed is unsafe.

For every single-line comparison site in sqlglot it stores (operator, alternatives that
never gave a different result anywhere in the suite, at import time or while testing).
Swapping a comparison to one of those alternatives cannot change any test's run.
"""

import ast
import glob
import json
import os
import marshal
import sys
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tools import cmp_trace as ct  # noqa: E402


def main():
    prof_dirs, root, out = sys.argv[1:4]
    unprofiled = set()
    for tr in sys.argv[4:]:
        d = json.load(open(tr))
        groups = [ph["lines"] for phases in d["tests"].values() for ph in phases.values()]
        groups.append(d.get("collect_lines", {}))
        for g in groups:
            for f, ls in g.items():
                unprofiled.update((f, l) for l in ls)
    dis = set()
    bad = []
    paths = [p for d in prof_dirs.split(",") for p in sorted(glob.glob(os.path.join(d, "*.json")))]
    for path in paths:
        log = open(path + ".log").read() if os.path.exists(path + ".log") else ""
        last = (log.strip().splitlines() or [""])[-1]
        if " failed" in last or " error" in last or "passed" not in last:
            bad.append((path, last))
        d = json.load(open(path))
        files = d["files"]
        for fid, line, col, alt in d["dis"] + d["import_dis"]:
            dis.add((files[fid], line, col, alt))
    if bad:
        for b in bad:
            print("profile run not clean:", b, file=sys.stderr)
        sys.exit(1)

    safe = {}
    nsites = nsafe = 0
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
        fsafe = {}
        for (line, col), op in sorted(sites.items()):
            if (rel, line) in unprofiled:
                nsites += 1
                continue
            ok = frozenset(a for a in ct.ALTS[op] if (rel, line, col, a) not in dis)
            nsites += 1
            if ok:
                nsafe += 1
                fsafe.setdefault(line, []).append((col, (op, ok)))
        if fsafe:
            safe[rel] = {line: tuple(v) for line, v in fsafe.items()}
    with open(out, "wb") as fh:
        fh.write(zlib.compress(marshal.dumps(safe), 9))
    print(f"{nsites} comparison sites, {nsafe} with a swap no test can see; {os.path.getsize(out)} bytes",
          file=sys.stderr)


if __name__ == "__main__":
    main()
