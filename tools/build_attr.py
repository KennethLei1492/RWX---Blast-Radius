"""Build maps/attr.bin from tools/attr_trace.py output.

    python tools/build_attr.py ATTR_DIR TESTS_JSON OUT_FILE

Output (zlib-compressed marshal):
  reads       {owner class "module:Qual": {attribute name: bits of tests that read it}}
              ("*" as owner: reads whose class could not be resolved; "*" as name: the
               class __dict__ was iterated)
  imp         {owner: (names read while importing sqlglot)}
  unprofiled  bits of tests with no profile (too slow, or failed under instrumentation)
  names       the instrumented attribute names
"""

import json
import marshal
import os
import sys
import zlib


def main():
    attr_dir, tests_json, out = sys.argv[1:4]
    tests = json.load(open(tests_json))["tests"]
    imp_data = json.load(open(os.path.join(attr_dir, "_import.json")))
    reads = {}
    unprofiled = 0
    for t in tests:
        n = t["number"]
        p = os.path.join(attr_dir, f"{n}.json")
        if not os.path.exists(p):
            unprofiled |= 1 << n
            continue
        bit = 1 << n
        for owner, name in json.load(open(p))["reads"]:
            d = reads.setdefault(owner, {})
            d[name] = d.get(name, 0) | bit
    imp = {}
    for owner, name in imp_data["reads"]:
        imp.setdefault(owner, set()).add(name)
    data = {"reads": reads, "imp": {k: tuple(sorted(v)) for k, v in imp.items()},
            "unprofiled": unprofiled, "names": frozenset(imp_data["names"])}
    with open(out, "wb") as fh:
        fh.write(zlib.compress(marshal.dumps(data), 9))
    pairs = sum(len(v) for v in reads.values())
    print(f"{pairs} (class, attribute) pairs read by tests; {sum(len(v) for v in imp.values())} read at import; "
          f"{bin(unprofiled).count('1')} unprofiled tests; {os.path.getsize(out)} bytes", file=sys.stderr)


if __name__ == "__main__":
    main()
