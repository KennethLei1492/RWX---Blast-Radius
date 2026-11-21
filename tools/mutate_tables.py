"""Generate patches that change one element of a class-level set or dict table.

    python tools/mutate_tables.py SQLGLOT_ROOT OUT_DIR COUNT SEED [sets|dicts|both|module]

`module` takes the tables assigned at module level instead of those in class bodies.

For a set (or frozenset(...) / set display) assigned in a class body, one element is replaced
by a copy of another element of the same table (so the element disappears and the file still
parses). For a dict, one value is replaced by another entry's value, or one key by another
key. Only used to label patches locally and check the entry-level rule; nothing derived from
them ships.
"""
import ast
import glob
import json
import os
import random
import sys

root, out, count, seed = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
which = sys.argv[5] if len(sys.argv) > 5 else "both"
rng = random.Random(seed)
os.makedirs(os.path.join(out, "commits"), exist_ok=True)


def seg(lines, node):
    if node.lineno != node.end_lineno:
        return None
    return lines[node.lineno - 1][node.col_offset:node.end_col_offset]


def candidates(path):
    src = open(path, encoding="utf-8").read()
    if not src.isascii():
        return []
    lines = src.split("\n")
    out = []
    tree = ast.parse(src)
    scopes = [tree] if which.startswith("module") else [c for c in ast.walk(tree) if isinstance(c, ast.ClassDef)]
    for cls in scopes:
        for st in cls.body:
            value = getattr(st, "value", None)
            if not isinstance(st, (ast.Assign, ast.AnnAssign)) or value is None:
                continue
            sets = [n for n in ast.walk(value) if isinstance(n, ast.Set)] if which in ("sets", "both", "module") else []
            for s in sets:
                elts = [e for e in s.elts if not isinstance(e, ast.Starred) and seg(lines, e)]
                if len(elts) >= 2:
                    out.append(("set", elts, None))
            if which in ("dicts", "both", "module") and isinstance(value, ast.Dict):
                pairs = [(k, v) for k, v in zip(value.keys, value.values)
                         if k is not None and seg(lines, k) and seg(lines, v)]
                if len(pairs) >= 2:
                    out.append(("dict", [p[0] for p in pairs], [p[1] for p in pairs]))
    return [(lines, c) for c in out]


files = sorted(glob.glob(os.path.join(root, "sqlglot", "**", "*.py"), recursive=True))
pool = []
for f in files:
    for lines, c in candidates(f):
        pool.append((f, lines, c))
kinds = {}
made = 0
tries = 0
seen = set()
while made < count and tries < count * 50:
    tries += 1
    f, lines, (kind, a, b) = rng.choice(pool)
    if kind == "set":
        x, y = rng.sample(a, 2)
        target, donor = x, y
        label = "set-elt"
    elif rng.random() < 0.5:
        i, j = rng.sample(range(len(a)), 2)
        target, donor = b[i], b[j]
        label = "dict-value"
    else:
        i, j = rng.sample(range(len(a)), 2)
        target, donor = a[i], a[j]
        label = "dict-key"
    new_text = seg(lines, donor)
    old_text = seg(lines, target)
    if new_text == old_text:
        continue
    rel = os.path.relpath(f, root).replace(os.sep, "/")
    key = (rel, target.lineno, target.col_offset, new_text)
    if key in seen:
        continue
    seen.add(key)
    line = lines[target.lineno - 1]
    new_line = line[:target.col_offset] + new_text + line[target.end_col_offset:]
    n = target.lineno
    lo, hi = max(1, n - 3), min(len(lines), n + 3)
    hunk = [f"@@ -{lo},{hi - lo + 1} +{lo},{hi - lo + 1} @@"]
    for i in range(lo, hi + 1):
        if i == n:
            hunk.append("-" + line)
            hunk.append("+" + new_line)
        else:
            hunk.append(" " + lines[i - 1])
    cid = f"t{seed:02d}{made:03d}"
    with open(os.path.join(out, "commits", cid + ".patch"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(f"diff --git a/{rel} b/{rel}\nindex 0000000..1111111 100644\n--- a/{rel}\n+++ b/{rel}\n"
                 + "\n".join(hunk) + "\n")
    kinds[cid] = label
    made += 1
json.dump(kinds, open(os.path.join(out, "kinds.json"), "w"), indent=1)
print(made, "patches;", {k: list(kinds.values()).count(k) for k in set(kinds.values())})
