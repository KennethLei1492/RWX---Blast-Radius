"""Generate patches that change one scalar setting the tokenizer forwards into its core.

    python tools/mutate_settings.py SQLGLOT_ROOT OUT_DIR COUNT SEED

For every class-body assignment of a setting that `Tokenizer._init_core` passes to
TokenizerCore (tools/attr_trace.py forwarded_settings) to a literal: a bool is flipped, a
string gets one character changed or appended, an int is moved by one. Only used to label
patches locally and check the forwarded-settings rule; nothing derived from them ships.
"""
import ast
import glob
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.attr_trace import forwarded_settings  # noqa: E402

root, out, count, seed = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
rng = random.Random(seed)
os.makedirs(os.path.join(out, "commits"), exist_ok=True)
names = {name for _, name in forwarded_settings(root).values()}

cands = []
for p in sorted(glob.glob(os.path.join(root, "sqlglot", "**", "*.py"), recursive=True)):
    src = open(p, encoding="utf-8").read()
    if not src.isascii():
        continue
    lines = src.split("\n")
    for cls in ast.walk(ast.parse(src)):
        if not isinstance(cls, ast.ClassDef):
            continue
        for st in cls.body:
            if isinstance(st, ast.Assign) and len(st.targets) == 1:
                t, v = st.targets[0], st.value
            elif isinstance(st, ast.AnnAssign) and st.value is not None:
                t, v = st.target, st.value
            else:
                continue
            if not (isinstance(t, ast.Name) and t.id in names and isinstance(v, ast.Constant)):
                continue
            if v.lineno != v.end_lineno:
                continue
            old = lines[v.lineno - 1][v.col_offset:v.end_col_offset]
            val = v.value
            if isinstance(val, bool):
                new = repr(not val)
            elif isinstance(val, int):
                new = repr(val + rng.choice((-1, 1)))
            elif isinstance(val, str):
                new = repr(val[:-1] + "#" if val else "#")
            elif val is None:
                new = '"#"'
            else:
                continue
            cands.append((os.path.relpath(p, root).replace(os.sep, "/"), v.lineno, v.col_offset, v.end_col_offset, old, new))

rng.shuffle(cands)
for i, (rel, ln, c0, c1, old, new) in enumerate(cands[:count]):
    src = open(os.path.join(root, rel), encoding="utf-8").read().split("\n")
    line = src[ln - 1]
    changed = line[:c0] + new + line[c1:]
    lo, hi = max(1, ln - 3), min(len(src), ln + 3)
    body = []
    for j in range(lo, hi + 1):
        if j == ln:
            body.append("-" + line)
            body.append("+" + changed)
        else:
            body.append(" " + src[j - 1])
    n = hi - lo + 1
    patch = (f"diff --git a/{rel} b/{rel}\nindex 0000000..1111111 100644\n--- a/{rel}\n+++ b/{rel}\n"
             f"@@ -{lo},{n} +{lo},{n} @@\n" + "\n".join(body) + "\n")
    with open(os.path.join(out, "commits", f"s{seed:02d}{i:03d}.patch"), "w", encoding="utf-8") as fh:
        fh.write(patch)
print(f"{len(cands)} candidate settings; wrote {min(count, len(cands))} patches")
