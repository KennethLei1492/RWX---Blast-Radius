"""Build maps/args.bin from tools/args_trace.py output.

    python tools/build_args.py ARGS_DIR TESTS_JSON SQLGLOT_ROOT OUT_FILE

sites    {file: {line: ((col, "True"|"False", owner "module:Qual", key), ...)}}
         one entry per boolean value of a plain `arg_types = {"k": True, ...}` class-body
         literal. Classes whose dict is spread into another (`**Cast.arg_types`) or read at
         class/module level are left out, as are dicts that are not plain literals.
missing  {owner: {key: bits of tests that validated an instance with `key` missing}}
imp      {owner: (keys seen missing while importing sqlglot)}
unprofiled  bits of tests without a profile
"""

import ast
import glob
import json
import marshal
import os
import sys
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from blast.pyimpact import module_name  # noqa: E402


def main():
    args_dir, tests_json, root, out = sys.argv[1:5]
    tests = json.load(open(tests_json))["tests"]
    files = sorted(glob.glob(os.path.join(root, "sqlglot", "**", "*.py"), recursive=True))

    # classes whose arg_types is used outside a function body (copied or derived at import)
    copied = set()
    trees = {}
    for p in files:
        rel = os.path.relpath(p, root).replace(os.sep, "/")
        tree = ast.parse(open(p, encoding="utf-8").read())
        trees[rel] = tree

        def walk(node, in_func):
            for child in ast.iter_child_nodes(node):
                f = in_func or isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
                if not in_func and isinstance(child, ast.Attribute) and child.attr == "arg_types":
                    v = child.value
                    if isinstance(v, ast.Attribute):
                        copied.add(v.attr)
                    elif isinstance(v, ast.Name):
                        copied.add(v.id)
                walk(child, f)

        walk(tree, False)

    sites = {}
    nsites = 0
    for rel, tree in trees.items():
        mod = module_name(rel)

        def visit(body, qual):
            nonlocal nsites
            for st in body:
                if isinstance(st, ast.ClassDef):
                    q = qual + [st.name]
                    for s2 in st.body:
                        value = None
                        if isinstance(s2, ast.Assign) and len(s2.targets) == 1 and \
                                isinstance(s2.targets[0], ast.Name) and s2.targets[0].id == "arg_types":
                            value = s2.value
                        elif isinstance(s2, ast.AnnAssign) and isinstance(s2.target, ast.Name) and \
                                s2.target.id == "arg_types":
                            value = s2.value
                        if value is None or st.name in copied or not isinstance(value, ast.Dict):
                            continue
                        if not all(isinstance(k, ast.Constant) and isinstance(k.value, str) for k in value.keys):
                            continue
                        if not all(isinstance(v, ast.Constant) and isinstance(v.value, bool) for v in value.values):
                            continue
                        owner = f"{mod}:{'.'.join(q)}"
                        for k, v in zip(value.keys, value.values):
                            sites.setdefault(rel, {}).setdefault(v.lineno, []).append(
                                (v.col_offset, "True" if v.value else "False", owner, k.value))
                            nsites += 1
                    visit(st.body, q)

        visit(tree.body, [])
    sites = {f: {l: tuple(v) for l, v in d.items()} for f, d in sites.items()}

    missing = {}
    unprofiled = 0
    for t in tests:
        n = t["number"]
        p = os.path.join(args_dir, f"{n}.json")
        if not os.path.exists(p):
            unprofiled |= 1 << n
            continue
        bit = 1 << n
        for owner, k in json.load(open(p))["missing"]:
            d = missing.setdefault(owner, {})
            d[k] = d.get(k, 0) | bit
    imp = {}
    for owner, k in json.load(open(os.path.join(args_dir, "_import.json")))["missing"]:
        imp.setdefault(owner, set()).add(k)
    data = {"sites": sites, "missing": missing, "imp": {k: tuple(sorted(v)) for k, v in imp.items()},
            "unprofiled": unprofiled}
    with open(out, "wb") as fh:
        fh.write(zlib.compress(marshal.dumps(data), 9))
    print(f"{nsites} arg_types values in {len(sites)} files; copied classes left out: {sorted(copied)}; "
          f"{bin(unprofiled).count('1')} unprofiled tests; {os.path.getsize(out)} bytes", file=sys.stderr)


if __name__ == "__main__":
    main()
