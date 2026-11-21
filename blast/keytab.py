"""Entry-level view of the dict and set tables sqlglot keeps in class bodies and modules.

A Generator's TRANSFORMS, a Parser's FUNCTIONS, a Tokenizer's KEYWORDS or NUMERIC_ESCAPES are
plain dicts assigned in a class body. maps/keys.bin (tools/key_trace.py, tools/build_keys.py)
records, for each such table, which tests read which key's value, which only asked whether a
key is there, which read its length or only its truth value, and which read it as a whole,
together with a fingerprint of every entry on the pinned commit.

When the import diff (blast/imstate.py) says such a table differs on the patched tree,
`entry_diff` names the entries that differ. A test that never read one of those entries (nor,
for an added or removed key, asked for its presence) and never read the table as a whole got
exactly what it got before from every read it made. Sets are the same with elements for
keys: an element has no value, only its presence.
"""

import sys

from .imstate import Walker


def keyrepr(k):
    """Process-independent name of a table key; None if it has none. (Same function as in
    tools/key_trace.py: the names must match what the tracer recorded.)"""
    t = type(k)
    # keys that compare equal must get the same name whatever their type: True and 1,
    # 1 and 1.0, a str-valued enum member and its string
    if isinstance(k, str):
        return "s:" + str.__str__(k)
    if isinstance(k, (bool, int)):
        return f"n:{int(k)}"
    if isinstance(k, float):
        return f"n:{int(k)}" if k.is_integer() else f"n:{float(k)!r}"
    if k is None or t is bytes:
        return f"p:{t.__name__}:{k!r}"
    if isinstance(k, type):
        return f"c:{k.__module__}:{k.__qualname__}"
    import enum

    if isinstance(k, enum.Enum):
        return f"e:{t.__module__}:{t.__qualname__}.{k.name}"
    if t in (tuple, frozenset):
        parts = [keyrepr(x) for x in k]
        if any(p is None for p in parts):
            return None
        return ("t:(" if t is tuple else "f:(") + ",".join(parts if t is tuple else sorted(parts)) + ")"
    return None


def resolve(name):
    """The object behind "module:Class.Inner.ATTR" (looked up in the class's own __dict__) or
    behind "module:NAME" (a module global)."""
    mod, _, rest = name.partition(":")
    path = rest.split(".")
    o = sys.modules.get(mod)
    if o is None:
        return None
    if len(path) == 1:
        return vars(o).get(path[0])
    for p in path[:-1]:
        o = vars(o).get(p)
        if o is None:
            return None
    if not isinstance(o, type):
        return None
    return vars(o).get(path[-1])


def entries(table, line_maps=None):
    """{key name: fingerprint} of a dict (or {element name: marker} of a set); None if it is
    neither or a key has no stable name. Each value is fingerprinted on its own, so the result does not depend on the
    order of the entries or on which other entries exist."""
    if type(table) in (set, frozenset):
        out = {}
        for k in table:
            r = keyrepr(k)
            if r is None or r in out:
                return None
            out[r] = b"\x01"   # an element has no value: only its presence can change
        return out
    if type(table) is not dict:
        return None
    out = {}
    for k, v in table.items():
        r = keyrepr(k)
        if r is None or r in out:
            return None
        out[r] = Walker(line_maps).fp(v)
    return out


def entry_diff(name, pinned, line_maps=None):
    """{"keys": [entries whose value changed], "gone": [entries added or removed], "len":
    whether the number of entries changed, "empty": whether it became empty or stopped being
    empty}, or None if the patched table cannot be compared entry by entry."""
    now = entries(resolve(name), line_maps)
    if now is None:
        return None
    keys = sorted(k for k in set(now) & set(pinned) if now[k] != pinned[k])
    gone = sorted(set(now) ^ set(pinned))
    return {"keys": keys, "gone": gone, "len": len(now) != len(pinned),
            "empty": (not now) != (not pinned)}
