"""Turn a change to one Python file into a list of *impacts*.

An impact names a piece of the pinned program whose behaviour the change can alter:

  ("lines", file, lines, fn)       code that runs while tests run: tests that executed
                                   any of these lines are affected (fn: enclosing function)
  ("defbody", file, lines, name)   a def header/decorator/default changed: tests that ran
                                   the body; if the body ran at import time, its callers
  ("class", module, qualname, names)  class-body data changed (names assigned, or None)
  ("const", module, names)         module-level assignment changed
  ("module", module)               anything else at import time: every user of the module
  ("broken", module)               the new file does not compile: every importer fails

Runtime code here uses the builtin `_ast` (no `import ast`) and parses only the statements
a patch touches; whole-file work (tokenizing, per-line impacts, statement chains) happens
once, at build time, in `file_pages`.
"""

import _ast as A

PyCF_ONLY_AST = 1024
FUNC = (A.FunctionDef, A.AsyncFunctionDef)
SCOPE = FUNC + (A.ClassDef,)
_TRYSTAR = getattr(A, "TryStar", A.Try)


def parse(src):
    return compile(src, "<patch>", "exec", PyCF_ONLY_AST)


def _walk(node):
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        for f in n._fields:
            v = getattr(n, f, None)
            if isinstance(v, list):
                stack.extend(x for x in v if isinstance(x, A.AST))
            elif isinstance(v, A.AST):
                stack.append(v)


def module_name(path):
    p = path[:-3] if path.endswith(".py") else path
    if p.endswith("/__init__"):
        p = p[: -len("/__init__")]
    return p.replace("/", ".")


def _start(node):
    decos = getattr(node, "decorator_list", None)
    if decos:
        return min(d.lineno for d in decos)
    if isinstance(node, A.match_case):
        return node.pattern.lineno
    return node.lineno


def _end(node):
    if isinstance(node, A.match_case):
        return node.body[-1].end_lineno
    return node.end_lineno or node.lineno


def _child_blocks(node):
    for name in ("body", "orelse", "finalbody"):
        block = getattr(node, name, None)
        if isinstance(block, list) and block and isinstance(block[0], A.stmt):
            yield block
    for h in getattr(node, "handlers", []) or []:
        yield [h]
    for c in getattr(node, "cases", []) or []:
        yield [c]


def _body_of(node):
    if isinstance(node, (A.ExceptHandler, A.match_case)):
        return node.body
    return getattr(node, "body", None)


def _header_end(node):
    body = _body_of(node)
    if isinstance(body, list) and body and isinstance(body[0], A.stmt):
        return max(_start(node), _start(body[0]) - 1)
    return _end(node)


def locate(tree, line):
    """Chain of nodes (outermost first) whose span holds `line`; last is the innermost."""
    chain = [tree]
    block_lists = [tree.body]
    while True:
        found = None
        for block in block_lists:
            for st in block:
                if _start(st) <= line <= _end(st):
                    found = st
                    break
            if found:
                break
        if found is None:
            return chain
        chain.append(found)
        block_lists = list(_child_blocks(found))


def _strip_comment(line):
    """The line without its comment, whitespace runs collapsed (strings kept verbatim)."""
    out = []
    i = 0
    n = len(line)
    q = None
    while i < n:
        ch = line[i]
        if q is not None:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(line[i + 1])
                i += 2
                continue
            if line.startswith(q, i):
                out.append(q[1:])
                i += len(q)
                q = None
                continue
            i += 1
            continue
        if ch == "#":
            break
        if ch in "\"'":
            q = line[i:i + 3] if line[i:i + 3] in ('"""', "'''") else ch
            out.append(q)
            i += len(q)
            continue
        if ch in " \t":
            if out and out[-1] != " ":
                out.append(" ")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out).strip()


def _blank_or_comment(text):
    s = text.lstrip()
    return not s or s[0] == "#"


def _assigned_names(st):
    names = []
    targets = []
    if isinstance(st, A.Assign):
        targets = st.targets
    elif isinstance(st, (A.AnnAssign, A.AugAssign)):
        targets = [st.target]
    elif isinstance(st, SCOPE):
        return [st.name]
    elif isinstance(st, (A.Import, A.ImportFrom)):
        return None
    else:
        # compound statement at class/module level: names assigned anywhere inside
        for n in _walk(st):
            if isinstance(n, (A.Assign, A.AnnAssign, A.AugAssign)):
                sub = _assigned_names(n)
                if sub is None:
                    return None
                names.extend(sub)
            elif isinstance(n, SCOPE):
                names.append(n.name)
        return names or None
    for t in targets:
        for n in _walk(t):
            if isinstance(n, A.Name):
                names.append(n.id)
            elif isinstance(n, (A.Attribute, A.Subscript, A.Starred)):
                if isinstance(n, A.Attribute) and not isinstance(n.value, A.Name):
                    return None
    return names or None


def _stores_into_object(st):
    """True for a statement that assigns to an attribute or an item (`Scope.x = ...`,
    `TABLE[k] = ...`): it changes an object that other modules may share, not a name here."""
    if isinstance(st, A.Assign):
        targets = st.targets
    elif isinstance(st, (A.AnnAssign, A.AugAssign)):
        targets = [st.target]
    else:
        return False
    for t in targets:
        for n in _walk(t):
            if isinstance(n, (A.Attribute, A.Subscript)):
                return True
    return False


def _lambdas_holding(st, line, col_span):
    """Innermost lambda (or nested function inside an expression) in `st` holding the change."""
    best = None
    for n in _walk(st):
        if isinstance(n, A.Lambda) and n.lineno <= line <= n.end_lineno:
            if col_span is not None and n.lineno == n.end_lineno == line:
                lo, hi = col_span
                if hi <= n.col_offset or lo >= n.end_col_offset:
                    continue
            elif col_span is not None and n.lineno == line:
                lo, hi = col_span
                if hi <= n.col_offset:
                    continue
            if best is None or (n.lineno, -n.end_lineno) > (best.lineno, -best.end_lineno):
                best = n
    return best


def _qualname(chain):
    return ".".join(n.name for n in chain if isinstance(n, SCOPE))


def _region(node):
    if isinstance(node, (A.stmt, A.ExceptHandler, A.match_case)) and _body_of(node) and \
            isinstance(_body_of(node), list) and isinstance(_body_of(node)[0], A.stmt):
        header = list(range(_start(node), _header_end(node) + 1))
        if isinstance(node, (A.Try, _TRYSTAR)):
            # `try:` may emit no line event of its own: whoever enters it runs its first statement
            first = node.body[0]
            header += list(range(_start(first), (first.end_lineno or first.lineno) + 1))
        return header
    return list(range(_start(node), _end(node) + 1))


def _body_lines(fn):
    if isinstance(fn, A.Lambda):
        return list(range(fn.body.lineno, fn.end_lineno + 1)) + [fn.lineno]
    return list(range(fn.body[0].lineno if fn.body else fn.lineno, fn.end_lineno + 1)) + [fn.lineno]


def impacts_for_line(path, tree, line, col_span=None):
    mod = module_name(path)
    chain = locate(tree, line)
    if len(chain) == 1:
        return []  # outside any statement (blank/comment between statements)
    st = chain[-1]
    out = []
    # Header of a def/lambda: affects every run of its body.
    in_header = isinstance(st, SCOPE) and line <= _header_end(st)
    enclosing = [n for n in chain[:-1] if isinstance(n, SCOPE)]
    ctx = enclosing[-1] if enclosing else None

    if isinstance(st, FUNC) and in_header:
        out.append(("defbody", path, _body_lines(st), _qualname(chain)))
        return out
    if isinstance(st, A.ClassDef) and in_header:
        out.append(("class", mod, _qualname(chain), None))
        return out

    parent = chain[-2]
    parent_body = _body_of(parent) if not isinstance(parent, A.Module) else parent.body
    if isinstance(st, A.Expr) and isinstance(st.value, A.Constant) and \
            isinstance(st.value.value, str) and isinstance(parent_body, list) and parent_body and \
            parent_body[0] is st and isinstance(parent, SCOPE + (A.Module,)):
        return [("none",)]  # a docstring: no scored test reads them (test_docs is not collected)

    if ctx is not None and isinstance(ctx, FUNC):
        lines = _region(st)
        if isinstance(st, (A.Pass, A.Global, A.Nonlocal)) or (
            isinstance(st, A.Expr) and isinstance(st.value, A.Constant)
        ):
            parent = chain[-2]
            lines = lines + _region(parent)
        out.append(("lines", path, lines, _qualname(chain)))
        return out

    # Import-time statement (class body or module level).
    lam = _lambdas_holding(st, line, col_span) if not isinstance(st, SCOPE) else None
    if lam is not None:
        # A lambda that is an element of a dict/list/tuple/set display (FUNCTIONS = {"X":
        # lambda args: ...}) is never called while the display is built: its body lines
        # also belong to the import-time statement, but only calls run them.
        in_display = any(
            (isinstance(n, A.Dict) and any(v is lam for v in n.values))
            or (isinstance(n, (A.List, A.Tuple, A.Set)) and any(v is lam for v in n.elts))
            for n in _walk(st)
        )
        out.append(("defbody", path, _body_lines(lam), "<display-lambda>" if in_display else None))
        return out
    if isinstance(st, A.Expr) and isinstance(st.value, A.Constant) and isinstance(st.value.value, str):
        return [("none",)]  # a string statement at import time (a docstring)
    names = _assigned_names(st)
    if ctx is not None:  # class body
        out.append(("class", mod, _qualname(chain[:-1]), names))
        return out
    if isinstance(st, (A.Import, A.ImportFrom)) or names is None:
        out.append(("module", mod))
        return out
    out.append(("const", mod, names))
    return out


def _freeze(imps):
    return tuple(tuple(tuple(x) if isinstance(x, list) else x for x in imp) for imp in imps)


# ---------------------------------------------------------------------------- build time


def _code_lines(src):
    """Lines that hold something other than comments and whitespace."""
    import io
    import tokenize

    lines = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT,
                            tokenize.DEDENT, tokenize.ENDMARKER):
                continue
            for l in range(tok.start[0], tok.end[0] + 1):
                lines.add(l)
    except (tokenize.TokenError, SyntaxError, IndentationError):
        return set(range(1, src.count("\n") + 2))
    return lines


def _continued_lines(src):
    """Lines L where a token (a multi-line string) continues onto line L+1."""
    import io
    import tokenize

    out = set()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.end[0] > tok.start[0] and tok.type not in (tokenize.NL, tokenize.NEWLINE):
                out.update(range(tok.start[0], tok.end[0]))
    except (tokenize.TokenError, SyntaxError, IndentationError):
        pass
    return out


def _stmt_nodes(tree, src_lines):
    """Statement chain per line. A node is (id, start, end, header_end, kind, name, parent id,
    block index); kind D def, C class, H not parseable on its own (except/case/elif), O other."""
    chains = {}
    counter = [0]

    def visit(node, parent_id, block_idx, chain):
        counter[0] += 1
        nid = counter[0]
        if isinstance(node, FUNC):
            kind, name = "D", node.name
        elif isinstance(node, A.ClassDef):
            kind, name = "C", node.name
        elif isinstance(node, (A.ExceptHandler, A.match_case)):
            kind, name = "H", ""
        elif isinstance(node, A.If) and src_lines[node.lineno - 1].lstrip().startswith("elif"):
            kind, name = "H", ""
        else:
            kind, name = "O", ""
        start, end = _start(node), _end(node)
        hdr = _header_end(node) if isinstance(_body_of(node), list) and _body_of(node) else end
        tup = (nid, start, end, hdr, kind, name, parent_id, block_idx)
        ch = chain + (tup,)
        for l in range(start, end + 1):
            chains[l] = ch  # children overwrite with deeper chains
        for bi, block in enumerate(_child_blocks(node)):
            for child in block:
                visit(child, nid, bi, ch)

    for st in tree.body:
        visit(st, 0, 0, ())
    return chains


def file_pages(path, src, page_size):
    """Per-line facts about a pinned file, grouped in pages of `page_size` lines."""
    tree = parse(src)
    src_lines = src.split("\n")
    code = _code_lines(src)
    cont = _continued_lines(src)
    chains = _stmt_nodes(tree, src_lines)
    lambdas_at = {}
    for n in _walk(tree):
        if isinstance(n, A.Lambda):
            lambdas_at.setdefault(n.lineno, []).append(n)
    pages = {}

    def page(l):
        p = l // page_size
        if p not in pages:
            pages[p] = {"code": set(), "cont": set(), "imp": {}, "lam": {}, "chain": {}}
        return pages[p]

    for l in cont:
        page(l)["cont"].add(l)
    for l in sorted(code):
        pg = page(l)
        pg["code"].add(l)
        imps = impacts_for_line(path, tree, l, None)
        if imps:
            pg["imp"][l] = _freeze(imps)
            if imps[0][0] == "defbody" and imps[0][3] is None:
                # a lambda on this line: what applies if the edit is outside it instead
                ranges = [(n.col_offset, n.end_col_offset if n.end_lineno == l else -1)
                          for n in lambdas_at.get(l, ())]
                alt = _freeze(impacts_for_line(path, tree, l, (0, 0)))
                if ranges and alt != pg["imp"][l]:
                    pg["lam"][l] = (tuple(ranges), alt)
        if l in chains:
            pg["chain"][l] = chains[l]
    for pg in pages.values():
        pg["code"] = frozenset(pg["code"])
        pg["cont"] = frozenset(pg["cont"])
    return pages


def file_info(path, src):
    """Whole-file form of the same facts (used by tools that load everything at once)."""
    pages = file_pages(path, src, 1 << 30)
    pg = pages.get(0, {"code": frozenset(), "cont": frozenset(), "imp": {}, "lam": {}, "chain": {}})
    return {"code": pg["code"], "instr": pg["cont"], "impacts": pg["imp"], "lam": pg["lam"],
            "chain": pg["chain"], "nlines": src.count("\n") + 1}


# ---------------------------------------------------------------------------- run time


def _target_of(line):
    """Text before the first assignment sign of a line ('' when it has none): the
    assignment target when the line starts a statement, so equal targets mean the same name."""
    s = line.strip()
    depth = 0
    for i, c in enumerate(s):
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c in "\"'#":
            break
        elif c == "=" and depth == 0 and s[i + 1:i + 2] != "=" and s[i - 1:i] not in ("=", "!", "<", ">"):
            return s[:i].strip()
    return ""


def _pair_spans(old_lines, new_lines, removed, applied):
    """Map removed old line -> changed column span, when a 1:1 replacement is evident."""
    spans = {}
    rem = sorted(removed)
    added = applied.new_changed
    if len(rem) == len(added):
        for o, n in zip(rem, added):
            a = old_lines[o - 1]
            b = new_lines[n - 1]
            i = 0
            m = min(len(a), len(b))
            while i < m and a[i] == b[i]:
                i += 1
            j = 0
            while j < m - i and a[len(a) - 1 - j] == b[len(b) - 1 - j]:
                j += 1
            spans[o] = (i, len(a) - j)
    return spans


def _chunk_for(chain_lo, chain_hi):
    """Smallest standalone source range holding both lines: (start, end, outer nodes) or None
    for the whole file. Ranges are one statement or consecutive siblings of one block."""
    k = 0
    while k < len(chain_lo) and k < len(chain_hi) and chain_lo[k][0] == chain_hi[k][0]:
        k += 1
    if k < len(chain_lo) and k < len(chain_hi):
        a, b = chain_lo[k], chain_hi[k]
        if a[6] == b[6] and a[7] == b[7] and a[4] != "H" and b[4] != "H":
            return a[1], b[2], chain_lo[:k]
    # the deepest common statement itself, or the nearest ancestor that stands alone
    j = k - 1
    while j >= 0 and chain_lo[j][4] == "H":
        j -= 1
    if j < 0:
        return None
    n = chain_lo[j]
    return n[1], n[2], chain_lo[:j]


def _indent(text):
    return text[: len(text) - len(text.lstrip(" \t"))]


def analyze(path, view, old_src, new_src, applied):
    """Impacts of changing `path` from old_src to new_src. `view` answers per-line questions
    about the pinned file: is_code(l), cont(l), impacts(l), lam(l), chain(l)."""
    mod = module_name(path)
    if old_src is None:
        return []  # a new module: nothing in the pinned tree imports it
    if new_src is None:
        return [("broken", mod)]

    # reuse the lines diffparse.apply already split (splitting big files again costs)
    old_lines = applied.old_lines if applied.old_lines is not None else old_src.split("\n")
    new_lines = applied.new_lines
    nold = applied.nold
    is_code = view.is_code
    cache = {}

    def old_tree():
        if "t" not in cache:
            cache["t"] = parse(old_src)
        return cache["t"]

    def line_imps(l, span=None):
        if span is not None:
            lam = view.lam(l)
            if lam is not None:
                lo, hi = span
                ranges, alt = lam
                hit = any(not (hi <= c0 or (c1 >= 0 and lo >= c1)) for c0, c1 in ranges)
                return list(view.impacts(l) if hit else alt)
            if not view.has_lam_info:
                return impacts_for_line(path, old_tree(), l, span)
        return list(view.impacts(l))

    def prev_code(l):
        while l >= 1 and not is_code(l):
            l -= 1
        return l

    def next_code(l):
        while l <= nold and not is_code(l):
            l += 1
        return l

    out = []
    code_change = False
    spans = _pair_spans(old_lines, new_lines, applied.removed, applied)
    rank = {l: i for i, l in enumerate(sorted(applied.removed))}
    for l in applied.removed:
        if not is_code(l):
            continue
        if spans and not view.cont(l - 1) and _strip_comment(old_lines[l - 1]) == _strip_comment(
                new_lines[applied.new_changed[rank[l]] - 1]):
            continue
        code_change = True
        sp = spans.get(l)
        # the column span only matters for a lambda on that line
        imps = line_imps(l, sp if (sp is not None and "lambda" in old_lines[l - 1]) else None)
        for imp in imps:
            if imp[0] == "class" and imp[3]:
                # The exact reader set of a class attribute holds only while the statement
                # still assigns the same name. If the target on this line changed (a rename
                # may shadow a base attribute), or the pairing is unknown, mark it "new".
                new_text = new_lines[applied.new_changed[rank[l]] - 1] if spans else None
                if new_text is None or _target_of(old_lines[l - 1]) != _target_of(new_text):
                    imp = tuple(imp) + ("new",)
            out.append(imp)

    # --- does the new file still parse, and what are the inserted statements? Parse only
    # the smallest standalone pieces of the new file around each edit.
    points = []
    for l in applied.removed:
        if is_code(l):
            points.append(l)
        else:
            points.append(prev_code(l))
            points.append(next_code(l))
    for anchor, newno, text in applied.inserted:
        points.append(prev_code(anchor - 1))
        points.append(next_code(anchor))
    points = sorted({p for p in points if 1 <= p <= nold})

    full = None  # whole new tree, only when pieces are not enough
    pieces = []  # (new start, new end, wrapper lines, tree, outer nodes)
    if points and view.has_chains:
        groups = []
        for p in points:
            if groups and p - groups[-1][1] <= 3:
                groups[-1][1] = p
            else:
                groups.append([p, p])
        ranges = []
        for lo, hi in groups:
            clo, chi = view.chain(lo), view.chain(hi)
            r = _chunk_for(clo, chi) if clo and chi else None
            if r is None:
                ranges = None
                break
            ranges.append(r)
        if ranges is not None:
            ranges.sort(key=lambda r: (r[0], -r[1]))
            merged = []
            for r in ranges:
                if merged and r[0] <= merged[-1][1]:
                    m = merged[-1]
                    if r[1] > m[1]:
                        # partial overlap: siblings of the same block (union stays valid)
                        merged[-1] = (m[0], r[1], m[2] if len(m[2]) <= len(r[2]) else r[2])
                else:
                    merged.append(r)
            for s, e, outer in merged:
                ns, ne = applied.new_range(s, e)
                ind = _indent(old_lines[s - 1])
                if ns > ne or _indent(new_lines[ns - 1]) != ind:
                    pieces = None
                    break
                text = "\n".join(new_lines[ns - 1:ne])
                wrap = 1 if ind else 0
                try:
                    tree = parse(("if 1:\n" + text) if wrap else text)
                except (SyntaxError, ValueError):
                    pieces = None
                    break
                if wrap and len(tree.body) != 1:
                    # a new line dedents out of the piece (a statement appended after the
                    # last block of the file, say): its real nesting is not in the piece
                    pieces = None
                    break
                pieces.append((ns, ne, wrap, tree, outer))
        else:
            pieces = None
        if pieces is not None:
            # every added line must sit in a parsed piece, or the pieces prove nothing
            for n in applied.new_changed:
                if not any(p[0] <= n <= p[1] for p in pieces):
                    pieces = None
                    break
    elif points or applied.new_changed:
        pieces = None
    if pieces is None:
        try:
            full = parse(new_src)
        except (SyntaxError, ValueError):
            return [("broken", mod)]

    def new_statements(newno):
        """The statements of the new file that hold line `newno`, outermost first, each as
        (statement node, its span in new lines, enclosing scope kinds, qualname)."""
        found = []
        if full is not None:
            chain = locate(full, newno)
            for i in range(1, len(chain)):
                st = chain[i]
                scopes = ["D" if isinstance(n, FUNC) else "C" for n in chain[:i] if isinstance(n, SCOPE)]
                found.append((st, (_start(st), _end(st)), scopes, _qualname(chain[:i])))
            return found
        for ns, ne, wrap, tree, outer in pieces:
            if ns <= newno <= ne:
                rel = newno - ns + 1 + wrap
                chain = locate(tree, rel)[1 + wrap:]
                off = ns - 1 - wrap
                for i in range(len(chain)):
                    st = chain[i]
                    scopes = [n[4] for n in outer if n[4] in ("C", "D")] + [
                        "D" if isinstance(n, FUNC) else "C" for n in chain[:i] if isinstance(n, SCOPE)]
                    names = [n[5] for n in outer if n[4] in ("C", "D")] + [
                        n.name for n in chain[:i] if isinstance(n, SCOPE)]
                    found.append((st, (_start(st) + off, _end(st) + off), scopes, ".".join(names)))
                break
        return found

    paired = set(applied.new_changed) if spans else set()
    added_set = set(applied.new_changed)
    for anchor, newno, text in applied.inserted:
        if newno in paired:
            continue
        if not (view.cont(anchor - 1) or not _blank_or_comment(text)):
            continue
        code_change = True
        # A whole new statement at class or module level (a new method, an override, a
        # new attribute) changes the class or the module namespace.
        # The outermost wholly new statement holding the line decides: the body of a new
        # function or method runs only through the name it adds, not when the old lines
        # beside it run.
        placed = False
        for st_new, (s0, s1), scopes, qual in new_statements(newno):
            if not all(l in added_set or _blank_or_comment(new_lines[l - 1]) for l in range(s0, s1 + 1)):
                continue
            if not (scopes and scopes[-1] == "D"):
                names = None if _stores_into_object(st_new) else _assigned_names(st_new)
                if scopes:
                    out.append(("class", mod, qual, names, "new"))
                elif names is None:
                    out.append(("module", mod))
                else:
                    out.append(("const", mod, names))
                placed = True
            break
        if placed:
            continue
        # A new line of code between old lines anchor-1 and anchor: it runs when its
        # neighbours run.
        for l in (prev_code(anchor - 1), next_code(anchor)):
            if 1 <= l <= nold:
                out.extend(line_imps(l))
    if not out and code_change:
        # The code changed but no line could be attributed: be safe.
        out.append(("module", mod))
    return out
