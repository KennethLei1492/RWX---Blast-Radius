"""Generate patches that flip one arg_types boolean (validation set for maps/args.bin).

    python tools/mutate_args.py SQLGLOT_ROOT OUT_DIR COUNT SEED
"""

import ast
import difflib
import glob
import json
import os
import random
import sys

root, out, count, seed = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
rng = random.Random(seed)
sites = []
for p in sorted(glob.glob(os.path.join(root, "sqlglot", "**", "*.py"), recursive=True)):
    rel = os.path.relpath(p, root).replace(os.sep, "/")
    src = open(p, encoding="utf-8").read()
    for cls in ast.walk(ast.parse(src)):
        if not isinstance(cls, ast.ClassDef):
            continue
        for st in cls.body:
            value = None
            if isinstance(st, ast.Assign) and isinstance(st.targets[0], ast.Name) and st.targets[0].id == "arg_types":
                value = st.value
            elif isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name) and st.target.id == "arg_types":
                value = st.value
            if isinstance(value, ast.Dict):
                for v in value.values:
                    if isinstance(v, ast.Constant) and isinstance(v.value, bool):
                        sites.append((rel, v.lineno, v.col_offset, v.value))
rng.shuffle(sites)
os.makedirs(os.path.join(out, "commits"), exist_ok=True)
kinds = {}
for i, (rel, line, col, val) in enumerate(sites[:count]):
    src = open(os.path.join(root, rel), encoding="utf-8").read()
    lines = src.split("\n")
    old = "True" if val else "False"
    new = "False" if val else "True"
    l = lines[line - 1]
    assert l.startswith(old, col)
    lines[line - 1] = l[:col] + new + l[col + len(old):]
    body = "".join(difflib.unified_diff(src.splitlines(True), "\n".join(lines).splitlines(True),
                                        f"a/{rel}", f"b/{rel}", n=3))
    cid = f"a{seed % 100:02d}{i:03d}"
    open(os.path.join(out, "commits", cid + ".patch"), "w", encoding="utf-8").write(
        f"diff --git a/{rel} b/{rel}\nindex 0000000..1111111 100644\n" + body)
    kinds[cid] = "argflip:" + ("req->opt" if val else "opt->req")
json.dump(kinds, open(os.path.join(out, "kinds.json"), "w"), indent=1)
print(f"{len(sites)} arg_types booleans; wrote {min(count, len(sites))} patches", file=sys.stderr)
