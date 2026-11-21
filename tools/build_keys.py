"""Build maps/keys.bin from tools/key_trace.py output (run inside the scorer image).

    python -I -B tools/build_keys.py entries KEY_DIR OUT.json      pinned entry fingerprints
    python -I -B tools/build_keys.py merge KEY_DIR TESTS_JSON OUT.bin ENTRIES.json [ENTRIES.json ...]

`entries` imports the pinned tree in a clean child, exactly as the import diff does, and
fingerprints every entry of every traced table; it is run under several hash seeds and `merge`
drops any table whose fingerprints differ between runs.

keys.bin: {"tables": {"module:Class.ATTR": {"entries": {key: fingerprint}, "keyed": {key: bits
of tests that read the key's value}, "has": {key: bits of tests that only asked whether the key
is there}, "whole": bits, "len": bits, "bool": bits (of the table itself), "lenp" / "boolp":
{nested node name: bits}, "nodes": names of nested dicts entered by path}}, "unprofiled": bits}.
"""
import json
import marshal
import os
import sys
import zlib

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
ROOT = os.environ.get("BLAST_SQLGLOT", "/opt/sqlglot")


def _entries_child(names, nodes_of):
    import importlib
    import pkgutil

    from blast import keytab, verify

    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    verify._install_overlay(ROOT, {})
    import sqlglot

    for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
        if info.name != "sqlglot.__main__":
            importlib.import_module(info.name)
    out = {}
    for n in names:
        e = keytab.entries(keytab.resolve(n), None, nodes_of.get(n))
        out[n] = None if e is None else {k: v.hex() for k, v in e.items()}
    return out


def run_entries(key_dir, out):
    from blast import verify

    imp = json.load(open(os.path.join(key_dir, "_import.json")))
    names = sorted({n for v in imp["tables"].values() for n in v})
    nodes_of = {n: set(imp.get("nodes", {}).get(i, ())) for i, v in imp["tables"].items() for n in v}
    v = verify.Verifier(ROOT)
    v.start(heavy=False)
    got = v._fork(lambda: _entries_child(names, nodes_of), 600)
    json.dump(got, open(out, "w"))
    print(len(names), "tables,", sum(1 for x in got.values() if x is None), "not comparable,",
          sum(len(x) for x in got.values() if x), "entries")


def merge(key_dir, tests_json, out, entry_files):
    tests = json.load(open(tests_json))["tests"]
    imp = json.load(open(os.path.join(key_dir, "_import.json")))
    names_of = {int(i): v for i, v in imp["tables"].items()}
    runs = [json.load(open(p)) for p in entry_files]
    stable = {}
    dropped = 0
    for n, e in runs[0].items():
        if e is None or any(r.get(n) != e for r in runs[1:]):
            dropped += 1
            continue
        stable[n] = {k: bytes.fromhex(v) for k, v in e.items()}
    keyed = {}
    has = {}
    whole = {}
    lens = {}
    bools = {}
    unprofiled = 0
    for t in tests:
        n = t["number"]
        p = os.path.join(key_dir, f"{n}.json")
        if not os.path.exists(p):
            unprofiled |= 1 << n
            continue
        d = json.load(open(p))
        bit = 1 << n
        for i, k in d["keyed"]:
            keyed.setdefault(i, {})
            keyed[i][k] = keyed[i].get(k, 0) | bit
        for i, k in d["has"]:
            has.setdefault(i, {})
            has[i][k] = has[i].get(k, 0) | bit
        for i in d["whole"]:
            whole[i] = whole.get(i, 0) | bit
        for i, node in d["len"]:
            lens.setdefault(i, {})
            lens[i][node] = lens[i].get(node, 0) | bit
        for i, node in d.get("bool", ()):
            bools.setdefault(i, {})
            bools[i][node] = bools[i].get(node, 0) | bit
    tables = {}
    for i, names in names_of.items():
        for name in names:
            if name not in stable:
                continue
            ln = dict(lens.get(i, {}))
            bl = dict(bools.get(i, {}))
            tables[name] = {"entries": stable[name], "keyed": keyed.get(i, {}), "has": has.get(i, {}),
                            "whole": whole.get(i, 0), "len": ln.pop("", 0), "bool": bl.pop("", 0),
                            "lenp": ln, "boolp": bl}
            nd = imp.get("nodes", {}).get(str(i))
            if nd:
                tables[name]["nodes"] = frozenset(nd)
    blob = {"tables": tables, "unprofiled": unprofiled}
    with open(out, "wb") as fh:
        fh.write(zlib.compress(marshal.dumps(blob), 9))
    print(f"{len(tables)} tables ({dropped} dropped), {sum(len(t['keyed']) for t in tables.values())} "
          f"keys with readers, {bin(unprofiled).count('1')} unprofiled tests, {os.path.getsize(out)} bytes")


if __name__ == "__main__":
    if sys.argv[1] == "entries":
        run_entries(sys.argv[2], sys.argv[3])
    else:
        merge(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5:])
