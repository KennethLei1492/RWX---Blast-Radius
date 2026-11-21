"""Unified diff parsing and application (git style, as produced by `git diff`).

Kept free of heavy imports (no dataclasses, no re): the selector's CPU time is scored.
"""


class Hunk:
    __slots__ = ("old_start", "old_len", "new_start", "new_len", "lines")

    def __init__(self, old_start, old_len, new_start, new_len):
        self.old_start = old_start
        self.old_len = old_len
        self.new_start = new_start
        self.new_len = new_len
        self.lines = []  # (tag, text) tag in ' ', '-', '+'


class FilePatch:
    __slots__ = ("old_path", "new_path", "hunks", "binary")

    def __init__(self, old_path, new_path):
        self.old_path = old_path
        self.new_path = new_path
        self.hunks = []
        self.binary = False

    @property
    def path(self):
        return self.old_path or self.new_path or ""

    @property
    def is_new(self):
        return self.old_path is None

    @property
    def is_deleted(self):
        return self.new_path is None


def _strip(p):
    p = p.strip()
    if "\t" in p:
        p = p.split("\t", 1)[0]
    if p == "/dev/null":
        return None
    if p.startswith('"') and p.endswith('"'):
        p = p[1:-1]
    if p.startswith(("a/", "b/")):
        p = p[2:]
    return p


def _hunk_header(line):
    """'@@ -a,b +c,d @@ ...' -> (a, b, c, d) or None."""
    if not line.startswith("@@ -"):
        return None
    end = line.find(" @@", 4)
    if end < 0:
        return None
    parts = line[3:end].split()
    if len(parts) != 2 or not parts[0].startswith("-") or not parts[1].startswith("+"):
        return None
    try:
        old = parts[0][1:].split(",")
        new = parts[1][1:].split(",")
        return (int(old[0]), int(old[1]) if len(old) > 1 else 1,
                int(new[0]), int(new[1]) if len(new) > 1 else 1)
    except ValueError:
        return None


def parse(text):
    files = []
    cur = None
    hunk = None
    need_old = need_new = 0
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    for line in lines:
        if hunk is not None and (need_old > 0 or need_new > 0) and line[:1] in (" ", "-", "+", "") \
                and not line.startswith("diff --git "):
            tag = line[:1] or " "
            hunk.lines.append((tag, line[1:]))
            if tag != "+":
                need_old -= 1
            if tag != "-":
                need_new -= 1
            continue
        if line.startswith("\\"):
            continue
        hunk = None
        if line.startswith("diff --git "):
            parts = line[len("diff --git "):].split(" ")
            a = _strip(parts[0]) if parts else None
            b = _strip(parts[-1]) if len(parts) > 1 else None
            cur = FilePatch(a, b)
            files.append(cur)
        elif line.startswith("--- "):
            if cur is None or cur.hunks:
                cur = FilePatch(None, None)
                files.append(cur)
            cur.old_path = _strip(line[4:])
        elif line.startswith("+++ "):
            if cur is None:
                cur = FilePatch(None, None)
                files.append(cur)
            cur.new_path = _strip(line[4:])
        elif line.startswith("new file mode") and cur is not None:
            cur.old_path = None
        elif line.startswith("deleted file mode") and cur is not None:
            cur.new_path = None
        elif line.startswith("rename from ") and cur is not None:
            cur.old_path = line[len("rename from "):].strip()
        elif line.startswith("rename to ") and cur is not None:
            cur.new_path = line[len("rename to "):].strip()
        elif (line.startswith("Binary files") or line.startswith("GIT binary patch")) and cur is not None:
            cur.binary = True
        else:
            h = _hunk_header(line)
            if h is not None and cur is not None:
                hunk = Hunk(*h)
                need_old, need_new = h[1], h[3]
                cur.hunks.append(hunk)
    return files


class Applied:
    __slots__ = ("new_lines", "removed", "inserted", "new_changed", "ok", "segs", "nold", "old_lines")

    def __init__(self, new_lines, removed, inserted, new_changed, ok, segs, nold, old_lines=None):
        self.new_lines = new_lines      # new file lines (without newline chars)
        self.removed = removed          # old line numbers (1-based) that were removed/replaced
        self.inserted = inserted        # (old_anchor, new_line_no, text): inserted before old line
        self.new_changed = new_changed  # new line numbers (1-based) that are added lines
        self.ok = ok
        self.segs = segs                # (old first line, new first line, tags) per applied hunk
        self.nold = nold
        self.old_lines = old_lines      # old file lines (without the empty piece after a final newline)

    def old_to_new(self, line):
        """New line number of a kept old line, None if the line was removed."""
        delta = 0
        for ostart, nstart, tags in self.segs:
            if line < ostart:
                return line + delta
            o, n = ostart, nstart
            for tag in tags:
                if tag == " ":
                    if o == line:
                        return n
                    o += 1
                    n += 1
                elif tag == "-":
                    if o == line:
                        return None
                    o += 1
                else:
                    n += 1
            delta = n - o
        return line + delta

    def new_range(self, s, e):
        """New lines that replaced old lines s..e (with insertions at either edge)."""
        a = s - 1
        while a >= 1 and self.old_to_new(a) is None:
            a -= 1
        ns = self.old_to_new(a) + 1 if a >= 1 else 1
        b = e + 1
        while b <= self.nold and self.old_to_new(b) is None:
            b += 1
        if b <= self.nold:
            ne = self.old_to_new(b) - 1
        else:
            ne = len(self.new_lines)
            while ne >= 1 and self.new_lines[ne - 1] == "" and ne > ns:
                ne -= 1
        return ns, ne


def apply(old_text, fp):
    old_lines = [] if old_text is None else old_text.split("\n")
    trailing = bool(old_lines) and old_lines[-1] == ""
    if trailing:
        old_lines = old_lines[:-1]
    out = []
    removed = []
    inserted = []
    new_changed = []
    segs = []
    pos = 0  # index into old_lines (0-based) already copied
    ok = True
    for h in fp.hunks:
        start = max(h.old_start - 1, 0) if h.old_len > 0 else h.old_start
        old_seq = [t for tag, t in h.lines if tag != "+"]
        # Locate the hunk (exact first, then search nearby).
        cand = start
        if old_lines[cand:cand + len(old_seq)] != old_seq:
            found = None
            for delta in range(1, 2000):
                for c in (start - delta, start + delta):
                    if pos <= c <= len(old_lines) and old_lines[c:c + len(old_seq)] == old_seq:
                        found = c
                        break
                if found is not None:
                    break
            if found is None:
                ok = False
            else:
                cand = found
        out.extend(old_lines[pos:cand])
        segs.append((cand + 1, len(out) + 1, [tag for tag, _ in h.lines]))
        o = cand
        for tag, text in h.lines:
            if tag == " ":
                out.append(old_lines[o] if o < len(old_lines) else text)
                o += 1
            elif tag == "-":
                removed.append(o + 1)
                o += 1
            else:
                out.append(text)
                new_changed.append(len(out))
                inserted.append((o + 1, len(out), text))
        pos = o
    out.extend(old_lines[pos:])
    if trailing or (old_text is None):
        out.append("")
    return Applied(out, removed, inserted, new_changed, ok, segs, len(old_lines), old_lines)
