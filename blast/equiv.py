"""Edits that do not change what a function compiles to select no tests.

If every changed line lies inside functions, and each of those functions compiles to the
same code before and after (same bytecode, constants, names, parameters, closure variables
and exception table), the patch cannot change behaviour: only line numbers, formatting,
names of plain local variables, docstrings and (under `from __future__ import annotations`,
where they are never evaluated) signature annotations may differ. This is "trivial compiler
equivalence" from the mutation-testing literature, applied per function so it costs a
millisecond or two.

Anything outside a function, a changed parameter name, a default, a decorator, a renamed
closure variable, or a function that uses locals()/vars()/eval/exec is not covered here and
goes through the normal analysis.
"""

import _ast as A

_INTROSPECT = {"locals", "vars", "eval", "exec", "dir", "globals"}
_FUNC = (A.FunctionDef, A.AsyncFunctionDef)


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


def _compile(lines, strip_annotations):
    """Code object of a chunk of source holding one def (possibly indented)."""
    first = next((l for l in lines if l.strip()), "")
    indented = first[:1] in (" ", "\t")
    src = ("if 1:\n" if indented else "") + "\n".join(lines) + "\n"
    tree = compile(src, "<chunk>", "exec", A.PyCF_ONLY_AST, dont_inherit=True)
    for n in _walk(tree):
        if isinstance(n, _FUNC):
            if strip_annotations:
                n.returns = None
                args = n.args
                for a in args.posonlyargs + args.args + args.kwonlyargs:
                    a.annotation = None
                if args.vararg is not None:
                    args.vararg.annotation = None
                if args.kwarg is not None:
                    args.kwarg.annotation = None
            body = n.body
            if body and isinstance(body[0], A.Expr) and isinstance(body[0].value, A.Constant) \
                    and isinstance(body[0].value.value, str):
                # docstrings do not matter to any scored test
                if len(body) > 1:
                    del body[0]
                else:
                    body[0] = A.Pass(lineno=body[0].lineno, col_offset=body[0].col_offset,
                                     end_lineno=body[0].end_lineno, end_col_offset=body[0].end_col_offset)
    return compile(tree, "<chunk>", "exec", dont_inherit=True)


def _same_code(a, b):
    if a.co_code != b.co_code or a.co_names != b.co_names or a.co_flags != b.co_flags:
        return False
    if (a.co_argcount, a.co_posonlyargcount, a.co_kwonlyargcount, a.co_name, a.co_qualname) != \
            (b.co_argcount, b.co_posonlyargcount, b.co_kwonlyargcount, b.co_name, b.co_qualname):
        return False
    if a.co_freevars != b.co_freevars or a.co_cellvars != b.co_cellvars:
        return False
    if a.co_exceptiontable != b.co_exceptiontable:
        return False
    nparams = a.co_argcount + a.co_kwonlyargcount + bool(a.co_flags & 0x04) + bool(a.co_flags & 0x08)
    if len(a.co_varnames) != len(b.co_varnames) or a.co_varnames[:nparams] != b.co_varnames[:nparams]:
        return False
    if a.co_varnames != b.co_varnames and (_INTROSPECT & set(a.co_names)):
        return False  # local names may be observable
    if len(a.co_consts) != len(b.co_consts):
        return False
    for x, y in zip(a.co_consts, b.co_consts):
        cx, cy = hasattr(x, "co_code"), hasattr(y, "co_code")
        if cx != cy:
            return False
        if cx:
            if not _same_code(x, y):
                return False
        elif type(x) is not type(y) or repr(x) != repr(y):
            return False
    return True


def _outer_def(view, line):
    """(start, end) of the outermost def whose span holds old line `line`, else None."""
    chain = view.chain(line)
    if not chain:
        return None
    for node in chain:
        if node[4] == "D":
            return node[1], node[2]
        if node[4] == "C":
            continue
    return None


def equivalent(view, old_lines, new_lines, applied):
    """True if the change provably leaves every touched function's compiled code unchanged."""
    if not applied.removed and not applied.inserted:
        return False
    spans = set()
    try:
        for l in applied.removed:
            sp = _outer_def(view, l)
            if sp is None:
                return False
            spans.add(sp)
        removed = set(applied.removed)
        for anchor, newno, text in applied.inserted:
            if (anchor - 1) in removed or anchor in removed:
                continue  # replaces removed lines: inside the span those lines gave
            before = _outer_def(view, anchor - 1) if anchor > 1 else None
            at = _outer_def(view, anchor) if anchor <= len(old_lines) else None
            if before is None:
                return False
            if before != at:
                # appended after the function's last line: it belongs to the function only
                # if it is indented deeper than the def (or is blank or a comment)
                s = before[0]
                def_line = next((l for l in old_lines[s - 1:before[1]] if l.lstrip().startswith(("def ", "async def "))), "")
                def_indent = len(def_line) - len(def_line.lstrip())
                body = text.strip()
                if anchor - 1 != before[1] or (body and not body.startswith("#") and
                                               len(text) - len(text.lstrip()) <= def_indent):
                    return False
            spans.add(before)
        future = any(l.startswith("from __future__ import") and "annotations" in l for l in old_lines[:60]) and \
            any(l.startswith("from __future__ import") and "annotations" in l for l in new_lines[:60])
        for s, e in spans:
            ns, ne = applied.new_range(s, e)
            old_code = _compile(old_lines[s - 1:e], future)
            new_code = _compile(new_lines[ns - 1:ne], future)
            if not _same_code(old_code, new_code):
                return False
        return True
    except (SyntaxError, ValueError, IndexError, TypeError, RecursionError):
        return False


def only_defaults_differ(old_def_lines, new_def_lines):
    """True if two versions of one def differ in nothing but the values of parameter
    defaults, each a constant, a name or a dotted name (possibly negated)."""
    import ast

    def simple(n):
        if isinstance(n, ast.UnaryOp):
            n = n.operand
        while isinstance(n, ast.Attribute):
            n = n.value
        return isinstance(n, (ast.Constant, ast.Name))

    def load(lines):
        first = next((l for l in lines if l.strip()), "")
        indented = first[:1] in (" ", "\t")
        return ast.parse(("if 1:\n" if indented else "") + "\n".join(lines) + "\n")

    def blank(tree):
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                a = n.args
                a.defaults = [ast.Constant(0) if simple(d) else d for d in a.defaults]
                a.kw_defaults = [ast.Constant(0) if d is not None and simple(d) else d for d in a.kw_defaults]
        return ast.dump(tree)

    try:
        old, new = load(old_def_lines), load(new_def_lines)
        if ast.dump(old) == ast.dump(new):
            return False
        return blank(old) == blank(new)
    except (SyntaxError, ValueError, RecursionError):
        return False
