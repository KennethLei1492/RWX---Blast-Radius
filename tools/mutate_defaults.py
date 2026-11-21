"""Generate patches that change one parameter default (validation set for maps/dflt.bin).

    python tools/mutate_defaults.py SQLGLOT_ROOT OUT_DIR COUNT SEED
"""

import ast
import difflib
import glob
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from defaults_trace import default_params  # noqa: E402

root, out, count, seed = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
rng = random.Random(seed)
sites = []
for p in sorted(glob.glob(os.path.join(root, "sqlglot", "**", "*.py"), recursive=True)):
    rel = os.path.relpath(p, root).replace(os.sep, "/")
    src = open(p, encoding="utf-8").read()
    lines = src.split("\n")
    for fn in ast.walk(ast.parse(src)):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for name, d in default_params(fn):
                if d.lineno != d.end_lineno or not lines[d.lineno - 1].isascii():
                    continue
                text = lines[d.lineno - 1][d.col_offset:d.end_col_offset]
                if text == "True":
                    new = "False"
                elif text == "False":
                    new = "True"
                elif text.isdigit():
                    new = str(int(text) + 1)
                elif text == "None":
                    new = rng.choice(["True", "0"])
                else:
                    continue
                sites.append((rel, d.lineno, d.col_offset, d.end_col_offset, new))
rng.shuffle(sites)
os.makedirs(os.path.join(out, "commits"), exist_ok=True)
kinds = {}
for i, (rel, line, c, ec, new) in enumerate(sites[:count]):
    src = open(os.path.join(root, rel), encoding="utf-8").read()
    lines = src.split("\n")
    l = lines[line - 1]
    kinds[f"d{seed % 100:02d}{i:03d}"] = f"default:{l[c:ec]}->{new}"
    lines[line - 1] = l[:c] + new + l[ec:]
    body = "".join(difflib.unified_diff(src.splitlines(True), "\n".join(lines).splitlines(True),
                                        f"a/{rel}", f"b/{rel}", n=3))
    open(os.path.join(out, "commits", f"d{seed % 100:02d}{i:03d}.patch"), "w", encoding="utf-8").write(
        f"diff --git a/{rel} b/{rel}\nindex 0000000..1111111 100644\n" + body)
json.dump(kinds, open(os.path.join(out, "kinds.json"), "w"), indent=1)
print(f"{len(sites)} simple defaults; wrote {min(count, len(sites))} patches", file=sys.stderr)
