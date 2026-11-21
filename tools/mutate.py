"""Generate synthetic patches against the pinned checkout, for validating the selector.

    python tools/mutate.py SQLGLOT_ROOT OUT_DIR COUNT SEED

Writes OUT_DIR/commits/<id>.patch and OUT_DIR/kinds.json. Kinds mirror the sample set
(code, comment, fixture, multi) plus kinds the sample never had (test-file edits, new
methods, deleted statements, module/class constants, docstrings, syntax errors).
These are only used to measure misses locally; nothing derived from them ships.
"""

import ast
import difflib
import glob
import json
import os
import random
import re
import sys

root, out, count, seed = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
rng = random.Random(seed)

SRC = sorted(os.path.relpath(p, root).replace(os.sep, "/")
             for p in glob.glob(os.path.join(root, "sqlglot", "**", "*.py"), recursive=True))
TESTS = sorted(os.path.relpath(p, root).replace(os.sep, "/")
               for p in glob.glob(os.path.join(root, "tests", "**", "test_*.py"), recursive=True))
FIX = sorted(os.path.relpath(p, root).replace(os.sep, "/")
             for p in glob.glob(os.path.join(root, "tests", "fixtures", "**", "*"), recursive=True)
             if os.path.isfile(p))


def read(p):
    return open(os.path.join(root, p), encoding="utf-8").read()


def diff(path, old, new):
    a = old.splitlines(keepends=True)
    b = new.splitlines(keepends=True)
    body = "".join(difflib.unified_diff(a, b, f"a/{path}", f"b/{path}", n=3))
    return f"diff --git a/{path} b/{path}\nindex 0000000..1111111 100644\n" + body


OPS = [
    (r" and ", " or "), (r" or ", " and "), (r" == ", " != "), (r" != ", " == "),
    (r" is not ", " is "), (r" is None", " == None"), (r" < ", " <= "), (r" > ", " >= "),
    (r" <= ", " < "), (r" >= ", " > "), (r"if not ", "if "), (r"\bTrue\b", "False"),
    (r"\bFalse\b", "True"), (r"\[0\]", "[1]"), (r"\[-1\]", "[0]"), (r"copy=False", "copy=True"),
    (r" \+ 1\b", " - 1"), (r"\breturn None\b", "return 0"), (r" in ", " not in "),
]


def code_mutation(path, src):
    lines = src.split("\n")
    cands = []
    for i, l in enumerate(lines):
        s = l.strip()
        if not s or s.startswith(("#", '"""', "'")):
            continue
        for pat, rep in OPS:
            for m in re.finditer(pat, l):
                cands.append((i, m.start(), m.end(), rep))
        for m in re.finditer(r"(?<![\w.])(\d+)(?![\w.])", l):
            if "#" in l[: m.start()]:
                continue
            cands.append((i, m.start(), m.end(), str(int(m.group(1)) + 1)))
    rng.shuffle(cands)
    old_dump = ast.dump(ast.parse(src))
    for i, a, b, rep in cands[:50]:
        new_line = lines[i][:a] + rep + lines[i][b:]
        new = "\n".join(lines[:i] + [new_line] + lines[i + 1:])
        try:
            if ast.dump(ast.parse(new)) != old_dump:
                return new
        except SyntaxError:
            continue
    return None


COMMENTS = ["# Cheap check first", "# Kept for backwards compatibility", "# Keep in sync with the generator",
            "# Intentionally not cached", "# Mirrors the behavior of the reference implementation", "# TODO: revisit"]


def comment_mutation(path, src):
    lines = src.split("\n")
    idx = [i for i, l in enumerate(lines) if l.strip() and not l.strip().startswith("#")]
    for _ in range(20):
        i = rng.choice(idx)
        indent = re.match(r"\s*", lines[i]).group(0)
        new = "\n".join(lines[:i] + [indent + rng.choice(COMMENTS)] + lines[i:])
        try:
            ast.parse(new)
            return new
        except SyntaxError:
            continue
    return None


def fixture_mutation(path, src):
    lines = src.split("\n")
    idx = [i for i, l in enumerate(lines) if l.strip() and not l.startswith(("#", "--"))]
    if not idx:
        return None
    i = rng.choice(idx)
    words = lines[i].split(" ")
    j = rng.randrange(len(words))
    op = rng.random()
    if op < 0.5:
        words.insert(j, rng.choice(["42", "0", "x", "NULL", "1"]))
    elif op < 0.8 and len(words) > 1:
        del words[j]
    else:
        words[j] = words[j] + rng.choice(["42", "a", "_"])
    new_line = " ".join(words)
    if new_line == lines[i]:
        return None
    return "\n".join(lines[:i] + [new_line] + lines[i + 1:])


def docstring_mutation(path, src):
    lines = src.split("\n")
    idx = [i for i, l in enumerate(lines) if l.strip().startswith('"""') and len(l.strip()) > 6]
    if not idx:
        return None
    i = rng.choice(idx)
    return "\n".join(lines[:i] + [lines[i].replace('"""', '"""Note: ', 1)] + lines[i + 1:])


def delete_stmt_mutation(path, src):
    tree = ast.parse(src)
    stmts = [n for n in ast.walk(tree) if isinstance(n, (ast.Assign, ast.Expr, ast.Return, ast.AugAssign))
             and n.lineno == n.end_lineno and not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
    if not stmts:
        return None
    n = rng.choice(stmts)
    lines = src.split("\n")
    indent = re.match(r"\s*", lines[n.lineno - 1]).group(0)
    new = "\n".join(lines[: n.lineno - 1] + [indent + "pass"] + lines[n.lineno:])
    try:
        ast.parse(new)
        return new
    except SyntaxError:
        return None


def class_attr_mutation(path, src):
    """Change a class-level constant (dict entry, flag, number)."""
    tree = ast.parse(src)
    cands = []
    for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
        for st in cls.body:
            if isinstance(st, ast.Assign):
                for c in ast.walk(st.value):
                    if isinstance(c, ast.Constant) and isinstance(c.value, (bool, int, str)) and c.lineno == c.end_lineno:
                        cands.append(c)
    if not cands:
        return None
    c = rng.choice(cands)
    lines = src.split("\n")
    l = lines[c.lineno - 1]
    seg = l[c.col_offset:c.end_col_offset]
    if isinstance(c.value, bool):
        rep = "False" if c.value else "True"
    elif isinstance(c.value, int):
        rep = str(c.value + 1)
    else:
        if len(seg) < 3:
            return None
        rep = seg[0] + seg[1:-1] + "X" + seg[-1]
    new = "\n".join(lines[: c.lineno - 1] + [l[: c.col_offset] + rep + l[c.end_col_offset:]] + lines[c.lineno:])
    return new


def new_method_mutation(path, src):
    """Add an override to a Generator/Parser subclass method that the base defines."""
    tree = ast.parse(src)
    classes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.body]
    if not classes:
        return None
    cls = rng.choice(classes)
    meths = [m for m in cls.body if isinstance(m, ast.FunctionDef) and m.args.args and m.args.args[0].arg == "self"]
    if not meths:
        return None
    m = rng.choice(meths)
    lines = src.split("\n")
    indent = re.match(r"\s*", lines[m.lineno - 1]).group(0)
    # duplicate the method right after itself with a broken body: the later one wins
    body = [f"{indent}def {m.name}(self, *args, **kwargs):", f"{indent}    raise NotImplementedError"]
    new = "\n".join(lines[: m.end_lineno] + [""] + body + lines[m.end_lineno:])
    try:
        ast.parse(new)
        return new
    except SyntaxError:
        return None


def syntax_error_mutation(path, src):
    lines = src.split("\n")
    idx = [i for i, l in enumerate(lines) if l.rstrip().endswith(":") and l.strip().startswith(("if ", "for ", "def "))]
    if not idx:
        return None
    i = rng.choice(idx)
    return "\n".join(lines[:i] + [lines[i].rstrip()[:-1]] + lines[i + 1:])


def test_file_mutation(path, src):
    lines = src.split("\n")
    idx = [i for i, l in enumerate(lines) if "validate_identity(" in l or "assertEqual(" in l]
    if not idx:
        return None
    i = rng.choice(idx)
    l = lines[i]
    m = list(re.finditer(r'"([^"]{3,})"', l))
    if not m:
        return None
    mm = rng.choice(m)
    new_l = l[: mm.end() - 1] + " " + l[mm.end() - 1:]
    return "\n".join(lines[:i] + [new_l] + lines[i + 1:])


PY_KINDS = [
    ("code", code_mutation, 50), ("comment", comment_mutation, 12), ("docstring", docstring_mutation, 3),
    ("delete", delete_stmt_mutation, 6), ("classattr", class_attr_mutation, 10),
    ("newmethod", new_method_mutation, 4), ("syntax", syntax_error_mutation, 2),
]


ONLY = set(filter(None, os.environ.get("ONLY_KINDS", "").split(",")))
if ONLY:
    PY_KINDS = [k for k in PY_KINDS if k[0] in ONLY]


def one(kind_pool):
    while True:
        r = 1.0 if ONLY else rng.random()
        if r < 0.18:
            p = rng.choice(FIX)
            try:
                src = read(p)
            except UnicodeDecodeError:
                continue
            new = fixture_mutation(p, src)
            if new:
                return "fixture", diff(p, src, new)
        elif r < 0.23:
            p = rng.choice(TESTS)
            src = read(p)
            new = test_file_mutation(p, src)
            if new:
                return "testfile", diff(p, src, new)
        else:
            kinds, funcs, w = zip(*kind_pool)
            k = rng.choices(range(len(kinds)), weights=w)[0]
            p = rng.choice(SRC)
            src = read(p)
            if len(src) < 200:
                continue
            new = funcs[k](p, src)
            if new and new != src:
                return kinds[k], diff(p, src, new)


os.makedirs(os.path.join(out, "commits"), exist_ok=True)
kinds = {}
for i in range(count):
    cid = f"m{seed % 100:02d}{i:03d}"
    if not ONLY and rng.random() < 0.15:
        parts = [one(PY_KINDS) for _ in range(rng.randint(2, 3))]
        paths = set()
        texts = []
        for k, t in parts:
            p = t.split("\n", 1)[0].split(" b/")[-1]
            if p in paths:
                continue
            paths.add(p)
            texts.append(t)
        kind, text = "multi:" + "+".join(k for k, _ in parts), "".join(texts)
    else:
        kind, text = one(PY_KINDS)
    open(os.path.join(out, "commits", cid + ".patch"), "w", encoding="utf-8").write(text)
    kinds[cid] = kind
json.dump(kinds, open(os.path.join(out, "kinds.json"), "w"), indent=1)
print(f"wrote {count} patches", file=sys.stderr)
