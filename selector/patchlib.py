"""Minimal unified-diff parser/applier (git-style diffs), no external tools.

parse_patch(text) -> list[FileChange]
apply_change(change, old_text) -> new_text   (raises PatchError on mismatch)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class PatchError(Exception):
    pass


@dataclass
class Hunk:
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    lines: list[str] = field(default_factory=list)  # each starts with ' ', '-', '+', or '\\'

    def removed_old_lines(self) -> list[int]:
        """Old-file line numbers of removed lines."""
        out, n = [], self.old_start
        for line in self.lines:
            tag = line[:1]
            if tag == " ":
                n += 1
            elif tag == "-":
                out.append(n)
                n += 1
        return out

    def insertion_points(self) -> list[int]:
        """Old-file line numbers *before which* added lines were inserted (no removal there)."""
        out, n, prev = [], self.old_start, None
        for line in self.lines:
            tag = line[:1]
            if tag == " ":
                n += 1
            elif tag == "-":
                n += 1
            elif tag == "+":
                if prev != "-" and prev != "+":
                    out.append(n)
            if tag in " -+":
                prev = tag
        return out


@dataclass
class FileChange:
    old_path: str | None  # None when the file is new
    new_path: str | None  # None when the file is deleted
    hunks: list[Hunk] = field(default_factory=list)
    binary: bool = False
    new_mode: str | None = None
    old_mode: str | None = None

    @property
    def path(self) -> str:
        return self.old_path or self.new_path or ""

    def touched_paths(self) -> set[str]:
        return {p for p in (self.old_path, self.new_path) if p}


def _strip_prefix(path: str) -> str | None:
    path = path.strip()
    if "\t" in path:
        path = path.split("\t", 1)[0]
    if path.startswith('"') and path.endswith('"'):
        path = path[1:-1].encode("utf-8").decode("unicode_escape").encode("latin-1").decode("utf-8")
    if path == "/dev/null":
        return None
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def parse_patch(text: str) -> list[FileChange]:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    changes: list[FileChange] = []
    cur: FileChange | None = None
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            m = re.match(r'^diff --git (?:"?a/)(.+?)"? (?:"?b/)(.+?)"?$', line)
            cur = FileChange(old_path=m.group(1) if m else None, new_path=m.group(2) if m else None)
            changes.append(cur)
            i += 1
            continue
        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            old = _strip_prefix(line[4:])
            new = _strip_prefix(lines[i + 1][4:])
            if cur is None or cur.hunks:
                cur = FileChange(old_path=old, new_path=new)
                changes.append(cur)
            else:
                cur.old_path, cur.new_path = old, new
            i += 2
            continue
        if cur is not None:
            if line.startswith("new file mode"):
                cur.old_path = None
                cur.new_mode = line.split()[-1]
            elif line.startswith("deleted file mode"):
                cur.new_path = None
                cur.old_mode = line.split()[-1]
            elif line.startswith("rename from "):
                cur.old_path = line[len("rename from "):]
            elif line.startswith("rename to "):
                cur.new_path = line[len("rename to "):]
            elif line.startswith("old mode "):
                cur.old_mode = line.split()[-1]
            elif line.startswith("new mode "):
                cur.new_mode = line.split()[-1]
            elif line.startswith("Binary files") or line.startswith("GIT binary patch"):
                cur.binary = True
            elif line.startswith("@@"):
                m = HUNK_RE.match(line)
                if not m:
                    raise PatchError(f"bad hunk header: {line!r}")
                hunk = Hunk(
                    int(m.group(1)),
                    int(m.group(2)) if m.group(2) is not None else 1,
                    int(m.group(3)),
                    int(m.group(4)) if m.group(4) is not None else 1,
                )
                cur.hunks.append(hunk)
                i += 1
                old_left, new_left = hunk.old_len, hunk.new_len
                while i < len(lines) and (old_left > 0 or new_left > 0 or lines[i].startswith("\\")):
                    body = lines[i]
                    tag = body[:1]
                    if tag == "\\":
                        hunk.lines.append(body)
                    elif tag == " " or body == "":
                        hunk.lines.append(" " + body[1:] if body else " ")
                        old_left -= 1
                        new_left -= 1
                    elif tag == "-":
                        hunk.lines.append(body)
                        old_left -= 1
                    elif tag == "+":
                        hunk.lines.append(body)
                        new_left -= 1
                    else:
                        break
                    i += 1
                continue
        i += 1
    return changes


def apply_change(change: FileChange, old_text: str | None) -> str | None:
    """Return the new file text (None if the file is deleted)."""
    if change.new_path is None:
        return None
    if change.binary:
        raise PatchError("binary patch")
    old_lines = (old_text or "").split("\n")
    # Track whether the old file ended with a newline.
    old_trailing_nl = old_text is not None and old_text.endswith("\n")
    if old_trailing_nl:
        old_lines.pop()
    elif old_text == "":
        old_lines = []
    out: list[str] = []
    pos = 0  # index into old_lines
    new_trailing_nl = old_trailing_nl if old_text else True
    for hunk in change.hunks:
        start = hunk.old_start - 1 if hunk.old_len > 0 else hunk.old_start
        if start < pos:
            raise PatchError("overlapping hunks")
        out.extend(old_lines[pos:start])
        pos = start
        last_tag = None
        for body in hunk.lines:
            tag, content = body[:1], body[1:]
            if tag == " ":
                if pos >= len(old_lines) or old_lines[pos] != content:
                    raise PatchError(f"context mismatch at old line {pos + 1}")
                out.append(content)
                pos += 1
            elif tag == "-":
                if pos >= len(old_lines) or old_lines[pos] != content:
                    raise PatchError(f"removal mismatch at old line {pos + 1}")
                pos += 1
            elif tag == "+":
                out.append(content)
            elif tag == "\\":
                # "\ No newline at end of file" refers to the previous line.
                if last_tag in ("+",):
                    new_trailing_nl = False
                elif last_tag == " ":
                    new_trailing_nl = False
            last_tag = tag if tag != "\\" else last_tag
        if hunk.lines and hunk.lines[-1][:1] == "\\":
            pass
    out.extend(old_lines[pos:])
    # A trailing '+' line after a "\ No newline" on the '-' side restores the newline.
    text = "\n".join(out)
    if out and new_trailing_nl:
        text += "\n"
    return text
