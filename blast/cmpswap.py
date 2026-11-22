"""Recognise a patch that only swaps comparison operators the test suite cannot tell apart.

maps/cmp.bin (built by tools/build_cmp.py from the pinned commit) lists, per comparison
site (line, column of the operator), the original operator and the alternatives that gave
the same result as the original at every evaluation in the whole test suite. If every
changed line of a file differs from the pinned line only by such a swap, the change cannot
alter any test's execution.

Plain string checks only (no tokenize): the site columns come from the pinned AST.
"""

_OPS = ("is not", "==", "!=", "<=", ">=", "and", "is", "or", "<", ">")  # longest first
_safe = None


def _load(maps_dir):
    global _safe
    if _safe is None:
        import marshal
        import os
        import zlib

        try:
            with open(os.path.join(maps_dir, "cmp.bin"), "rb") as fh:
                _safe = marshal.loads(zlib.decompress(fh.read()))
        except OSError:
            _safe = {}
    return _safe


def _ident(c):
    return c.isalnum() or c == "_"


def _op_at(s, col):
    for op in _OPS:
        if s.startswith(op, col):
            end = col + len(op)
            if op[0].isalpha() and ((end < len(s) and _ident(s[end])) or (col > 0 and _ident(s[col - 1]))):
                continue  # `is` in `isinstance`, `or` in `order`
            return op
    return None


def _one_swap(sites, a, b):
    """True if b is a with exactly one known comparison operator swapped to a safe one."""
    for col, (op, ok) in sites:
        if not a.startswith(op, col) or b[:col] != a[:col]:
            continue
        new = _op_at(b, col)
        if new is None or new == op or new not in ok:
            continue
        if b[col + len(new):] == a[col + len(op):]:
            return True
    return False


# ---------------------------------------------------------------------------- per test
# maps/cmpt.bin (tools/build_cmpt.py): per comparison site (line, operator column), the
# original operator and, for each alternative operator, the bitset of tests in which that
# alternative gave a different result at some evaluation (or which could not be profiled).
# A test outside that set evaluates the swapped comparison identically every time, so the
# swap cannot change its run.
_per_test = None


def _load_per_test(maps_dir):
    global _per_test
    if _per_test is None:
        import marshal
        import os
        import zlib

        try:
            with open(os.path.join(maps_dir, "cmpt.bin"), "rb") as fh:
                _per_test = marshal.loads(zlib.decompress(fh.read()))
        except OSError:
            _per_test = {}
    return _per_test


def _swap_bits(sites, a, b):
    """Bits of the tests that can see b's operator in place of a's (None if not one swap)."""
    for col, op, alts in sites:
        if not a.startswith(op, col) or b[:col] != a[:col]:
            continue
        new = _op_at(b, col)
        if new is None or new == op or new not in alts:
            continue
        if b[col + len(new):] == a[col + len(op):]:
            return alts[new]
    return None


def swap_users(path, old_lines, new_lines, applied, maps_dir):
    """If the change to `path` only swaps comparison operators at profiled sites, the tests
    that can observe any of the swaps; otherwise None."""
    sites_by_line = _load_per_test(maps_dir).get(path)
    if not sites_by_line:
        return None
    removed = sorted(applied.removed)
    added = applied.new_changed
    if not removed or len(removed) != len(added):
        return None
    bits = 0
    for o, n in zip(removed, added):
        sites = sites_by_line.get(o)
        if not sites:
            return None
        got = _swap_bits(sites, old_lines[o - 1], new_lines[n - 1])
        if got is None:
            return None
        bits |= got
    return bits


def swap_only(path, old_lines, new_lines, applied, maps_dir) -> bool:
    """True if the change to `path` only swaps comparison operators no test can tell apart."""
    safe = _load(maps_dir).get(path)
    if not safe:
        return False
    removed = sorted(applied.removed)
    added = applied.new_changed
    if not removed or len(removed) != len(added):
        return False
    for o, n in zip(removed, added):
        sites = safe.get(o)
        if not sites or not _one_swap(sites, old_lines[o - 1], new_lines[n - 1]):
            return False
    return True


# ---------------------------------------------------------------------------- arg_types
# maps/args.bin (tools/build_args.py): the True/False values of `arg_types` class-body
# literals. Their only reader is validation (a required argument that is missing is an
# error), so flipping the value for key k of class C can only change tests that validated an
# instance of C with k missing.
_args = None


def _load_args(maps_dir):
    global _args
    if _args is None:
        import marshal
        import os
        import zlib

        try:
            with open(os.path.join(maps_dir, "args.bin"), "rb") as fh:
                _args = marshal.loads(zlib.decompress(fh.read()))
        except OSError:
            _args = {}
    return _args


def argflip_users(path, old_lines, new_lines, applied, maps_dir):
    """If the change to `path` only flips arg_types booleans, the tests that validated the
    class with that argument missing; otherwise None."""
    t = _load_args(maps_dir)
    sites_by_line = t.get("sites", {}).get(path) if t else None
    if not sites_by_line:
        return None
    removed = sorted(applied.removed)
    added = applied.new_changed
    if not removed or len(removed) != len(added):
        return None
    bits = 0
    for o, n in zip(removed, added):
        a, b = old_lines[o - 1], new_lines[n - 1]
        hit = None
        for col, text, owner, key in sites_by_line.get(o, ()):
            flipped = "False" if text == "True" else "True"
            if a.startswith(text, col) and b == a[:col] + flipped + a[col + len(text):]:
                hit = (owner, key)
                break
        if hit is None:
            return None
        owner, key = hit
        if key in t["imp"].get(owner, ()):
            return None  # seen missing while importing: the flip changes import itself
        bits |= t["missing"].get(owner, {}).get(key, 0)
    return bits | t["unprofiled"]


# ---------------------------------------------------------------------------- defaults
# maps/dflt.bin (tools/build_dflt.py): per parameter default in a def header, the tests that
# entered the function with the parameter holding its default. A changed default cannot
# matter to a call that supplied the parameter.
_dflt = None


def _load_dflt(maps_dir):
    global _dflt
    if _dflt is None:
        import marshal
        import os
        import zlib

        try:
            with open(os.path.join(maps_dir, "dflt.bin"), "rb") as fh:
                _dflt = marshal.loads(zlib.decompress(fh.read()))
        except OSError:
            _dflt = {}
    return _dflt


def _simple_token(s):
    return bool(s) and all(c.isalnum() or c in "_.-+'\"" for c in s)


def default_users(path, old_lines, new_lines, applied, maps_dir, after_import=False):
    """If the change to `path` only replaces parameter defaults by another simple token, the
    tests that relied on those defaults; otherwise None. Defaults also used while importing
    count only `after_import`: when an import diff has shown that the import leaves the pinned
    state, so that only the uses after it can tell the defaults apart."""
    t = _load_dflt(maps_dir)
    sites_by_line = t.get("sites", {}).get(path) if t else None
    if after_import and t:
        extra = t.get("imp_sites", {}).get(path)
        if extra:
            merged = dict(sites_by_line or {})
            for ln, v in extra.items():
                merged[ln] = tuple(merged.get(ln, ())) + tuple(v)
            sites_by_line = merged
    if not sites_by_line:
        return None
    removed = sorted(applied.removed)
    added = applied.new_changed
    if not removed or len(removed) != len(added):
        return None
    bits = 0
    for o, n in zip(removed, added):
        a, b = old_lines[o - 1], new_lines[n - 1]
        hit = None
        for col, end, text, tb in sites_by_line.get(o, ()):
            if a[col:end] != text or a[:col] != b[:col]:
                continue
            suffix = a[end:]
            if len(b) < col + len(suffix) or not b.endswith(suffix):
                continue
            new = b[col:len(b) - len(suffix)]
            if new != text and _simple_token(new) and _simple_token(text):
                hit = tb
                break
        if hit is None:
            return None
        bits |= hit
    return bits
