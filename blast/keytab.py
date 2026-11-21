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


def pathname(path):
    """Name of a key path: the key's own name for a top-level key; for a key inside a nested
    dict, "P:" and the names of the keys along the path joined by \x1f, so that the name of a
    path's parent node is the name with its last part cut off (see parent_name). None if a key
    has no stable name. (Same function in tools/key_trace.py and blast/keytab.py.)"""
    parts = [keyrepr(x) for x in path]
    if any(p is None for p in parts):
        return None
    if len(parts) == 1:
        return parts[0]   # a top-level name is never split, whatever it holds
    if any("\x1f" in p for p in parts):
        return None
    return "P:" + "\x1f".join(parts)


def parent_name(name):
    """Name of the node that holds the entry `name`: "" for the table itself."""
    if not name.startswith("P:"):
        return ""
    head = name[2:].rsplit("\x1f", 1)[0]
    return head if "\x1f" not in head else "P:" + head


class _Unnamed(Exception):
    pass


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


def entries(table, line_maps=None, nodes=None):
    """{key name: fingerprint} of a dict ({element name: marker} of a set, {index: fingerprint}
    of a list); None if it is none of these or a key has no stable name. Each value is fingerprinted on its own, so the result
    does not depend on the order of the entries or on which other entries exist. A nested dict
    at a key path named in `nodes` (the ones the tracer recorded reads inside) is entered: it
    counts as an entry of its own (a marker) and so does each of its keys, by path."""
    if type(table) in (set, frozenset):
        out = {}
        for k in table:
            r = keyrepr(k)
            if r is None or r in out:
                return None
            out[r] = b"\x01"   # an element has no value: only its presence can change
        return out
    if type(table) is list:
        # an element by its index (what an indexed read records); the length is compared too
        return {keyrepr(i): Walker(line_maps).fp(v) for i, v in enumerate(table)}
    if type(table) is not dict:
        return None
    out = {}

    def walk(d, path):
        for k, v in d.items():
            p = path + (k,)
            r = pathname(p)
            if r is None or r in out:
                raise _Unnamed()
            if nodes and type(v) is dict and r in nodes:
                out[r] = b"\x02"
                walk(v, p)
            else:
                out[r] = Walker(line_maps).fp(v)

    try:
        walk(table, ())
    except (_Unnamed, RecursionError):
        return None
    return out


def entry_diff(name, pinned, line_maps=None, nodes=None):
    """{"keys": [entries whose value changed], "gone": [entries added or removed], "len":
    whether the number of top-level entries changed, "empty": whether the table became empty or
    stopped being empty, "parents": nested nodes that gained or lost an entry}, or None if the
    patched table cannot be compared entry by entry."""
    now = entries(resolve(name), line_maps, nodes)
    if now is None:
        return None
    keys = sorted(k for k in set(now) & set(pinned) if now[k] != pinned[k])
    gone = sorted(set(now) ^ set(pinned))
    top_now = sum(1 for k in now if not k.startswith("P:"))
    top_pinned = sum(1 for k in pinned if not k.startswith("P:"))
    return {"keys": keys, "gone": gone, "len": top_now != top_pinned,
            "empty": (top_now == 0) != (top_pinned == 0),
            # nested nodes that gained or lost an entry: their length and truth readers count
            "parents": sorted({parent_name(k) for k in gone} - {""})}
