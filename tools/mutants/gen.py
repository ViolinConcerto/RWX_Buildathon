"""Generate synthetic patches against the pinned sqlglot tree for *validation*.

The patches are labeled by running the full suite (tools/lab/label_all.sh) and
used only to measure the selector's misses and cost. They are never used to
build any patch->tests table.

usage: python gen.py <sqlglot_root> <out_dir> <count> [seed]
"""

from __future__ import annotations

import ast
import difflib
import os
import random
import re
import sys
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2])
COUNT = int(sys.argv[3])
SEED = int(sys.argv[4]) if len(sys.argv) > 4 else 1
rng = random.Random(SEED)

PY_FILES = sorted(p for p in (ROOT / "sqlglot").rglob("*.py"))
FIXTURES = sorted(p for p in (ROOT / "tests" / "fixtures").rglob("*") if p.is_file() and p.suffix in (".sql", ".json", ".csv"))
TEST_FILES = sorted((ROOT / "tests").rglob("test_*.py"))

COMMENTS = ["# Cheap check first", "# Kept for backwards compatibility", "# TODO: revisit",
            "# Mirrors the behavior of the reference implementation", "# noqa: E501", "# type: ignore"]

SWAPS = [
    (r" == ", " != "), (r" != ", " == "), (r" is not ", " is "), (r" is None", " is not None"),
    (r" < ", " <= "), (r" <= ", " < "), (r" > ", " >= "), (r" >= ", " > "),
    (r" and ", " or "), (r" or ", " and "), (r"\bnot ", ""), (r"\bTrue\b", "False"), (r"\bFalse\b", "True"),
    (r" in ", " not in "), (r"\bif ", "if not "), (r"\+ 1\b", "- 1"), (r"- 1\b", "+ 1"),
    (r"seq_get\(args, (\d)\)", None), (r"\b(\d+)\b", None), (r'"([A-Z_]{3,})"', None),
    (r"\bcopy=False\b", "copy=True"), (r"\bcopy=True\b", "copy=False"), (r"\bbreak\b", "continue"),
    (r"\breturn (\w+)", "return None"), (r"\.lower\(\)", ".upper()"), (r"\.upper\(\)", ".lower()"),
]


def unified(rel: str, old: str, new: str) -> str:
    a = old.splitlines(keepends=True)
    b = new.splitlines(keepends=True)
    body = "".join(difflib.unified_diff(a, b, f"a/{rel}", f"b/{rel}", n=3))
    if not body:
        return ""
    return f"diff --git a/{rel} b/{rel}\nindex 1234567..89abcde 100644\n" + body


def line_kinds(src: str):
    """line -> 'func' | 'def' for lines inside functions vs module/class level."""
    tree = ast.parse(src)
    func: set[int] = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func.update(range(n.body[0].lineno, (n.end_lineno or n.lineno) + 1))
    return func


def mutate_code(prefer_def: bool = False):
    for _ in range(200):
        path = rng.choice(PY_FILES)
        src = path.read_text()
        lines = src.split("\n")
        try:
            func = line_kinds(src)
        except SyntaxError:
            continue
        idx = rng.randrange(len(lines))
        ln = idx + 1
        line = lines[idx]
        s = line.strip()
        if not s or s.startswith("#") or s.startswith(('"""', "'''")) or s.startswith(("import ", "from ")):
            continue
        in_func = ln in func
        if prefer_def == in_func:
            continue
        pat, rep = rng.choice(SWAPS)
        m = re.search(pat, line)
        if not m:
            continue
        if rep is None:
            if pat.startswith("seq_get"):
                new_line = line[: m.start(1)] + str((int(m.group(1)) + 1) % 4) + line[m.end(1):]
            elif pat == r"\b(\d+)\b":
                v = int(m.group(1))
                new_line = line[: m.start(1)] + str(v + rng.choice([1, -1]) if v else 1) + line[m.end(1):]
            else:
                w = m.group(1)
                new_line = line[: m.start(1)] + w[:-1] + rng.choice("XYZ") + line[m.end(1):]
        else:
            new_line = line[: m.start()] + re.sub(pat, rep, m.group(0), count=1) + line[m.end():]
        if new_line == line:
            continue
        new = "\n".join(lines[:idx] + [new_line] + lines[idx + 1:])
        try:
            ast.parse(new)
        except SyntaxError:
            continue
        rel = str(path.relative_to(ROOT))
        return rel, src, new, "code-def" if not in_func else "code"
    return None


def delete_stmt():
    for _ in range(200):
        path = rng.choice(PY_FILES)
        src = path.read_text()
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        stmts = [n for n in ast.walk(tree) if isinstance(n, (ast.Expr, ast.Assign, ast.AugAssign, ast.Return))
                 and n.lineno == n.end_lineno]
        if not stmts:
            continue
        n = rng.choice(stmts)
        lines = src.split("\n")
        indent = re.match(r"\s*", lines[n.lineno - 1]).group(0)
        new_lines = lines[: n.lineno - 1] + [indent + "pass"] + lines[n.lineno:]
        new = "\n".join(new_lines)
        try:
            ast.parse(new)
        except SyntaxError:
            continue
        return str(path.relative_to(ROOT)), src, new, "delete-stmt"
    return None


def add_comment(in_string_risk=False):
    path = rng.choice(PY_FILES)
    src = path.read_text()
    lines = src.split("\n")
    for _ in range(100):
        idx = rng.randrange(1, len(lines))
        if lines[idx].strip() and not lines[idx].rstrip().endswith("\\"):
            indent = re.match(r"\s*", lines[idx]).group(0)
            new = "\n".join(lines[:idx] + [indent + rng.choice(COMMENTS)] + lines[idx:])
            try:
                ast.parse(new)
            except SyntaxError:
                continue
            return str(path.relative_to(ROOT)), src, new, "comment"
    return None


def mutate_fixture():
    for _ in range(100):
        path = rng.choice(FIXTURES)
        src = path.read_text()
        lines = src.split("\n")
        cand = [i for i, l in enumerate(lines) if l.strip() and not l.startswith("--")]
        if not cand:
            continue
        i = rng.choice(cand)
        l = lines[i]
        op = rng.random()
        if op < 0.35:
            toks = l.split(" ")
            j = rng.randrange(len(toks))
            toks.insert(j, rng.choice(["42", "NULL", "x", ","]))
            nl = " ".join(toks)
        elif op < 0.6:
            nl = re.sub(r"\b([a-z])\b", lambda m: m.group(1) * 2, l, count=1)
        elif op < 0.75:
            nl = l.replace(" AS ", " ", 1)
        elif op < 0.85:
            nl = "-- " + l if not l.startswith("#") else l
            if nl == l:
                continue
        else:
            nl = l.rstrip(";") + " " if l.endswith(";") else l + " "
        if nl == l:
            continue
        new = "\n".join(lines[:i] + [nl] + lines[i + 1:])
        return str(path.relative_to(ROOT)), src, new, "fixture"
    return None


def mutate_test_file():
    for _ in range(100):
        path = rng.choice(TEST_FILES)
        src = path.read_text()
        lines = src.split("\n")
        cand = [i for i, l in enumerate(lines) if re.search(r'"[^"]*[A-Za-z][^"]*"', l) and "def " not in l]
        if not cand:
            continue
        i = rng.choice(cand)
        l = lines[i]
        m = re.search(r'"([^"]*[A-Za-z][^"]*)"', l)
        nl = l[: m.start(1)] + m.group(1).replace(" ", "  ", 1) if " " in m.group(1) else l[: m.start(1)] + m.group(1) + "x"
        nl = nl + l[m.end(1):] if " " in m.group(1) else nl + l[m.end(1):]
        if nl == l:
            continue
        new = "\n".join(lines[:i] + [nl] + lines[i + 1:])
        try:
            ast.parse(new)
        except SyntaxError:
            continue
        return str(path.relative_to(ROOT)), src, new, "test-file"
    return None


def syntax_error():
    path = rng.choice(PY_FILES)
    src = path.read_text()
    lines = src.split("\n")
    idx = rng.randrange(len(lines))
    new = "\n".join(lines[:idx] + [lines[idx] + " )"] + lines[idx + 1:]) if lines[idx].strip() else None
    if not new:
        return None
    return str(path.relative_to(ROOT)), src, new, "syntax"


GENERATORS = [
    (mutate_code, 0.36, {}), (lambda: mutate_code(True), 0.14, {}), (delete_stmt, 0.08, {}),
    (add_comment, 0.10, {}), (mutate_fixture, 0.18, {}), (mutate_test_file, 0.06, {}), (syntax_error, 0.02, {}),
]


def pick():
    r = rng.random()
    acc = 0.0
    for g, w, _ in GENERATORS:
        acc += w
        if r < acc:
            return g()
    return GENERATORS[0][0]()


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    made = 0
    kinds = {}
    while made < COUNT:
        multi = rng.random() < 0.12
        parts = []
        for _ in range(2 if multi else 1):
            m = pick()
            if m and all(m[0] != p[0] for p in parts):
                parts.append(m)
        if not parts:
            continue
        text = "".join(unified(rel, old, new) for rel, old, new, _ in parts)
        if not text:
            continue
        kind = "+".join(k for *_, k in parts)
        cid = f"m{SEED:02d}{made:03d}"
        (OUT / f"{cid}.patch").write_text(text)
        kinds[cid] = kind
        made += 1
    (OUT.parent / f"{OUT.name}.kinds.txt").write_text("".join(f"{k} {v}\n" for k, v in kinds.items()))
    print(f"wrote {made} patches to {OUT}")


main()
