"""Self-test of the table recorders in tools/key_trace.py.

    python tools/key_selftest.py

Every way sqlglot or a test can read a tracked dict or set must leave a record: a keyed read,
a presence read, a length or truth read, or a whole read. Each case below performs one
operation, through source rewritten exactly as the tracer's import hook rewrites it, and
states the weakest record that must exist afterwards. A case that leaves no sufficient record
is a way for a test to depend on a table without the map knowing.
"""
import ast
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("key_trace", os.path.join(HERE, "key_trace.py"))
kt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kt)

CASES = [
    # (source, what must be recorded: "whole", "keyed:<key>", "has:<key>", "len", "bool")
    ("d['a']", "keyed:a"), ("d.get('a')", "keyed:a"), ("d.get('zz', 1)", "keyed:zz"),
    ("'a' in d", "has:a"), ("'zz' not in d", "has:zz"),
    ("len(d)", "len"), ("bool(d)", "bool"), ("1 if d else 2", "bool"), ("d or {}", "bool"),
    ("not d", "bool"),
    ("list(d)", "whole"), ("sorted(d)", "whole"), ("[k for k in d]", "whole"), ("d.keys()", "whole"),
    ("d.values()", "whole"), ("list(d.items())", "whole"), ("dict(**d)", "whole"), ("{**d}", "whole"),
    ("{**d, 'x': 1}", "whole"), ("{}.update(d)", "whole"), ("{} | d", "whole"), ("d | {}", "whole"),
    ("d == {'a': 1, 'b': 2}", "whole"), ("{'a': 1, 'b': 2} == d", "whole"), ("d != {}", "whole"),
    ("repr(d)", "whole"), ("str(d)", "whole"), ("tuple(d)", "whole"), ("set(d)", "whole"),
    ("max(d)", "whole"), ("any(d)", "whole"), ("'{a}'.format_map(d)", "keyed:a"),
    ("'%(a)s' % d", "keyed:a"), ("(lambda **k: k)(**d)", "whole"), ("[*d]", "whole"),
    ("__import__('json').dumps(d)", "whole"), ("__import__('copy').copy(d)", "whole"),
    ("__import__('copy').deepcopy(d)", "whole"), ("__import__('pickle').dumps(d)", "whole"),
    ("list(reversed(d))", "whole"), ("__import__('operator').getitem(d, 'a')", "keyed:a"),
    ("__import__('operator').contains(d, 'a')", "has:a"), ("next(iter(d))", "whole"),
    ("dict.fromkeys(d)", "whole"), ("list(map(d.get, ['a']))", "keyed:a"),
    ("__import__('collections').ChainMap(d)['a']", "keyed:a"),
    ("__import__('types').MappingProxyType(d)['a']", "keyed:a"),
    ("'a' in __import__('types').MappingProxyType(d)", "has:a"),
    ("list(__import__('types').MappingProxyType(d))", "whole"),
    ("__import__('types').MappingProxyType(d).get('a')", "keyed:a"),
    ("dict(d, x=1)", "whole"), ("dict(d.items())", "whole"), ("d.items() <= {'a': 1}.items()", "whole"),
    ("{'a': 1}.keys() & d", "whole"), ("{'a'} & d.keys()", "whole"), ("sorted(d, key=d.get)", "whole"),
    ("d.__getitem__('a')", "keyed:a"), ("d.__contains__('a')", "has:a"), ("getattr(d, 'get')('a')", "keyed:a"),
    ("__import__('string').Template('$a').substitute(d)", "keyed:a"),
    ("[d[k] for k in ('a', 'b')]", "keyed:a"), ("{k: v for k, v in d.items()}", "whole"),
    ("{'a': 1, 'b': 2}.items() == d.items()", "whole"), ("len(d) == 2", "len"),
    # a linked copy keeps recording against the table
    ("dict(d)['a']", "keyed:a"), ("dict(d).get('b')", "keyed:b"), ("'a' in dict(d)", "has:a"),
    ("d.copy()['a']", "keyed:a"), ("list(dict(d))", "whole"), ("dict(d) == {}", "whole"),
    ("dict(dict(d))['a']", "keyed:a"),
    # sets
    ("1 in s", "has:1"), ("9 not in s", "has:9"), ("len(s)", "len"), ("bool(s)", "bool"),
    ("s or set()", "bool"), ("list(s)", "whole"), ("sorted(s)", "whole"), ("set(s)", "whole"),
    ("frozenset(s)", "whole"), ("{*s}", "whole"), ("{*s, 5}", "whole"), ("[*s]", "whole"),
    ("s | {5}", "whole"), ("{5} | s", "whole"), ("s & {1}", "whole"), ("{1} & s", "whole"),
    ("s - {1}", "whole"), ("{1, 7} - s", "whole"), ("s ^ {1}", "whole"), ("{1} ^ s", "whole"),
    ("s == {1, 2}", "whole"), ("{1, 2} == s", "whole"), ("s <= {1, 2, 3}", "whole"),
    ("{1} <= s", "whole"), ("{1} < s", "whole"), ("s >= {1}", "whole"), ("{1, 2, 3} > s", "whole"),
    ("s.union({5})", "whole"), ("{5}.union(s)", "whole"), ("{5}.union({6}, s)", "whole"),
    ("{1}.issubset(s)", "whole"), ("{1}.issuperset(s)", "whole"), ("{1}.isdisjoint(s)", "whole"),
    ("{1}.intersection(s)", "whole"), ("{1}.difference(s)", "whole"),
    ("{1}.symmetric_difference(s)", "whole"), ("set().union(*[s])", "whole"),
    ("tuple(s)", "whole"), ("max(s)", "whole"), ("repr(s)", "whole"), ("any(x > 1 for x in s)", "whole"),
    ("__import__('copy').copy(s)", "whole"), ("__import__('pickle').dumps(s)", "whole"),
    ("(lambda *a: a)(*s)", "whole"), ("s.copy()", "whole"), ("s.isdisjoint({9})", "whole"),
    ("1 in f", "has:1"), ("hash(f)", "whole"),
    ("{f: 1}", "whole"), ("frozenset(f)", "whole"), ("{1}.issubset(f)", "whole"), ("sorted(f)", "whole"),
    ("f | {5}", "whole"),
    # the other operand: a set against a frozenset, or two tables
    ("{5} & f", "whole:f"), ("{5} - f", "whole:f"), ("{5} ^ f", "whole:f"), ("{1, 2} == f", "whole:f"),
    ("{1} <= f", "whole:f"), ("{1} != f", "whole:f"), ("frozenset({5}) | s", "whole:s"),
    ("frozenset({1, 2, 3}) == s", "whole:s"), ("frozenset({1}) < s", "whole:s"),
    ("s | f", "whole:f"), ("f | s", "whole:s"), ("s - f", "whole:f"), ("s == f", "whole:f"),
    ("f <= s", "whole:s"), ("s.union(f)", "whole:f"), ("f.issubset(s)", "whole:s"),
    ("s.isdisjoint(f)", "whole:f"), ("__import__('collections').OrderedDict(a=1, b=2) == d", "whole:d"),
    ("{5} | f", "whole:f"),
]
STATEMENTS = [
    ("x = {5}; x |= s", "whole"), ("x = {5}; x.update(s)", "whole"), ("x = {5}; x &= s", "whole"),
    ("x = {5}; x -= s", "whole"), ("x = {5}; x ^= s", "whole"), ("x = {5}; x.intersection_update(s)", "whole"),
    ("x = {}; x |= d", "whole"), ("x = {}; x.update(d)", "whole"),
    ("for k in d: pass", "whole"), ("for k in s: pass", "whole"), ("a, b = d", "whole"), ("a, *b = s", "whole"),
    ("x = dict(d); x['n'] = 1; x['a']", "keyed:a"),
]


def run(src, statement):
    for store in (kt.KEYED, kt.HAS, kt.WHOLE, kt.LEN, kt.BOOLS):
        store.clear()
    d = kt._table({"a": 1, "b": 2}, "D")
    s = kt._table({1, 2, 3}, "S")
    f = kt._table(frozenset({1, 2}), "F")
    tree = ast.parse(src, mode="exec" if statement else "eval")
    rw = kt._Rewrite()
    rw.tables = False
    tree = ast.fix_missing_locations(rw.visit(tree))
    env = {"d": d, "s": s, "f": f}
    if statement:
        exec(compile(tree, "<case>", "exec"), env)
    else:
        eval(compile(tree, "<case>", "eval"), env)
    return d, s, f


def satisfied(want, d, s, f):
    if want.startswith("whole:"):   # that particular table must be recorded as read whole
        return {"d": d, "s": s, "f": f}[want[6:]]._i in kt.WHOLE
    ids = {d._i, s._i, f._i}
    if ids & kt.WHOLE:
        return True   # a whole read covers everything
    if want == "whole":
        return False
    lens = {i for i, _ in kt.LEN}
    bools = {i for i, _ in kt.BOOLS}
    if want == "len":
        return bool(ids & lens)
    if want == "bool":
        return bool(ids & (bools | lens))
    kind, _, key = want.partition(":")
    for k in (key, int(key) if key.isdigit() else key):
        for i in ids:
            if (i, (k,)) in kt.KEYED or (kind == "has" and (i, (k,)) in kt.HAS):
                return True
    return False


# ------------------------------------------------------------ lists
LIST_CASES = [
    ("x[0]", "keyed:0"), ("x[1]", "keyed:1"), ("x[-1]", "len"), ("len(x)", "len"), ("bool(x)", "bool"),
    ("1 if x else 2", "bool"), ("x or []", "bool"), ("list(x)", "whole"), ("tuple(x)", "whole"),
    ("sorted(x)", "whole"), ("[*x]", "whole"), ("(*x,)", "whole"), ("set(x)", "whole"),
    ("'a' in x", "whole"), ("x[0:1]", "whole"), ("x.index('a')", "whole"), ("x.count('a')", "whole"),
    ("x + ['z']", "whole"), ("['z'] + x", "whole"), ("x * 2", "whole"), ("x == ['a', 'b']", "whole"),
    ("['a', 'b'] == x", "whole"), ("x != []", "whole"), ("[] < x", "whole"), ("max(x)", "whole"),
    ("','.join(x)", "whole"), ("repr(x)", "whole"), ("str(x)", "whole"), ("list(reversed(x))", "whole"),
    ("__import__('json').dumps(x)", "whole"), ("__import__('copy').copy(x)", "whole"),
    ("__import__('pickle').dumps(x)", "whole"), ("dict.fromkeys(x)", "whole"), ("{*x}", "whole"),
    ("(lambda *a: a)(*x)", "whole"), ("x.copy()", "whole"), ("any(x)", "whole"), ("enumerate(x).__next__()", "whole"),
    ("next(iter(x))", "whole"), ("__import__('itertools').chain(x).__next__()", "whole"),
    ("{'a': 1}.keys() & x", "whole"), ("{e: 1 for e in x}", "whole"),
    ("__import__('collections').Counter(x)", "whole"), ("[].__add__(x)", "whole"),
]
LIST_STATEMENTS = [("y = []; y += x", "whole"), ("y = []; y.extend(x)", "whole"), ("a, b = x", "whole"),
                   ("a, *b = x", "whole"), ("for e in x: pass", "whole"), ("y = list(); y[:] = x", "whole")]


def list_check():
    bad = 0
    for src, want, stmt in [(a, b, False) for a, b in LIST_CASES] + [(a, b, True) for a, b in LIST_STATEMENTS]:
        for store in (kt.KEYED, kt.HAS, kt.WHOLE, kt.LEN, kt.BOOLS):
            store.clear()
        x = kt._table(["a", "b"], "X")
        if type(x) is not kt.TList:
            print("NOT WRAPPED: a fresh list")
            return bad + 1
        tree = ast.parse(src, mode="exec" if stmt else "eval")
        rw = kt._Rewrite()
        rw.tables = False
        tree = ast.fix_missing_locations(rw.visit(tree))
        try:
            if stmt:
                exec(compile(tree, "<case>", "exec"), {"x": x})
            else:
                eval(compile(tree, "<case>", "eval"), {"x": x})
        except Exception as e:  # noqa: BLE001
            print("ERROR", src, repr(e))
            bad += 1
            continue
        i = x._i
        if i in kt.WHOLE:
            ok = True
        elif want == "whole":
            ok = False
        elif want == "len":
            ok = any(j == i for j, _ in kt.LEN)
        elif want == "bool":
            ok = any(j == i for j, _ in kt.BOOLS | kt.LEN)
        else:
            ok = (i, (int(want.split(":")[1]),)) in kt.KEYED
        if not ok:
            bad += 1
            print("NOT RECORDED (list):", src, "needs", want)
    return bad


# ------------------------------------------------------------ nested dicts (a keyword trie)
TRIE_CASES = [
    # (source, key paths that must be recorded for table t, as keyed (k) or presence (h) reads)
    ("t['S']['E']", [("k", ("S",)), ("k", ("S", "E"))]),
    ("t.get('S').get('X')", [("k", ("S",)), ("k", ("S", "X"))]),
    ("0 in t['S']['E']", [("k", ("S", "E")), ("h", ("S", "E", 0))]),
    ("dict(t['S'])['E']", [("k", ("S", "E"))]),
    ("t['S'].copy().get('Q')", [("k", ("S", "Q"))]),
]


def trie_check():
    bad = 0
    for src, want in TRIE_CASES:
        for store in (kt.KEYED, kt.HAS, kt.WHOLE, kt.LEN, kt.BOOLS):
            store.clear()
        t = kt._table({"S": {"E": {0: True, "L": {0: True}}}, "F": {0: True}}, "T")
        eval(compile(ast.parse(src, mode="eval"), "<case>", "eval"), {"t": t})
        for kind, path in want:
            if (t._i, path) not in (kt.KEYED if kind == "k" else kt.KEYED | kt.HAS) and t._i not in kt.WHOLE:
                bad += 1
                print("NOT RECORDED (trie):", src, kind, path)
    # truth and length of a nested dict are recorded for that node
    for store in (kt.KEYED, kt.HAS, kt.WHOLE, kt.LEN, kt.BOOLS):
        store.clear()
    t = kt._table({"S": {"E": {0: True}}}, "T")
    bool(t["S"])
    len(t["S"]["E"])
    if (t._i, ("S",)) not in kt.BOOLS or (t._i, ("S", "E")) not in kt.LEN:
        bad += 1
        print("NOT RECORDED (trie): truth or length of a nested node")
    if kt.parent_name(kt.pathname(("S", "E", 0))) != kt.pathname(("S", "E")) or \
            kt.parent_name(kt.pathname(("S", "E"))) != kt.pathname(("S",)) or kt.parent_name(kt.pathname(("S",))) != "":
        bad += 1
        print("WRONG parent names")
    # iterating a nested dict reads the whole table
    for store in (kt.KEYED, kt.HAS, kt.WHOLE):
        store.clear()
    t = kt._table({"S": {"E": {0: True}}}, "T")
    list(t["S"])
    if t._i not in kt.WHOLE:
        bad += 1
        print("NOT RECORDED (trie): iteration of a nested dict")
    # a nested dict that something else also holds is not wrapped (its reads would escape)
    shared = {"x": 1}
    keep = [shared]
    t = kt._table({"A": shared}, "T")
    if type(dict.__getitem__(t, "A")) is not dict:
        bad += 1
        print("WRAPPED a shared nested dict")
    # a table value that something else holds is not wrapped either
    other = {"y": 2}
    if kt._table(other, "U") is not other:
        bad += 1
        print("WRAPPED a shared table")
    # a class attribute assigned inside a function is tracked; an instance attribute is not
    class K:
        pass
    k = K()
    if type(kt._attr_table(K, "X", {"a": 1})) is not kt.TDict:
        bad += 1
        print("NOT WRAPPED: class attribute assigned in a function")
    if type(kt._attr_table(k, "X", {"a": 1})) is not dict:
        bad += 1
        print("WRAPPED: instance attribute")
    del keep
    return bad


bad = 0
for src, want in CASES:
    try:
        ok = satisfied(want, *run(src, False))
    except Exception as e:  # noqa: BLE001
        ok = False
        print("ERROR", src, repr(e))
    if not ok:
        bad += 1
        print("NOT RECORDED:", src, "needs", want)
for src, want in STATEMENTS:
    try:
        ok = satisfied(want, *run(src, True))
    except Exception as e:  # noqa: BLE001
        ok = False
        print("ERROR", src, repr(e))
    if not ok:
        bad += 1
        print("NOT RECORDED:", src, "needs", want)
bad += trie_check()
bad += list_check()
print(f"{len(CASES) + len(STATEMENTS) + len(TRIE_CASES) + 5 + len(LIST_CASES) + len(LIST_STATEMENTS)} cases, "
      f"{bad} not recorded")
sys.exit(1 if bad else 0)
