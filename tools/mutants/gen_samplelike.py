"""Patches that mirror the public sample's mutation style (for offline analysis only).

Operator families seen in starter/sample: comparison swaps, and<->or, dropped `not`,
bool flips, integer +-1, one-token fixture edits (optimizer fixtures), comment
insertions, and "multi" (comments + one real edit). Sites are uniform over all
candidate nodes of a family; families are equally likely. Never used as a lookup.

usage: python gen_samplelike.py <sqlglot_root> <out_dir> <count> [seed]
"""

import ast
import difflib
import random
import re
import sys
from pathlib import Path

ROOT, OUT, COUNT = Path(sys.argv[1]).resolve(), Path(sys.argv[2]), int(sys.argv[3])
SEED = int(sys.argv[4]) if len(sys.argv) > 4 else 21
rng = random.Random(SEED)
PY = sorted((ROOT / "sqlglot").rglob("*.py"))
FIX = sorted((ROOT / "tests" / "fixtures" / "optimizer").glob("*.sql"))
COMMENTS = ["# Cheap check first", "# Kept for backwards compatibility", "# Intentionally not cached",
            "# Mirrors the behavior of the reference implementation", "# Keep in sync with the generator"]
CMP = {ast.Eq: ("==", "!="), ast.NotEq: ("!=", "=="), ast.Lt: ("<", "<="), ast.LtE: ("<=", "<"),
       ast.Gt: (">", ">="), ast.GtE: (">=", ">"), ast.Is: ("is", "=="), ast.IsNot: ("is not", "!=")}

sites = {"cmp": [], "bool": [], "not": [], "flip": [], "int": []}
SRC = {}
for f in PY:
    rel = str(f.relative_to(ROOT))
    src = f.read_text()
    SRC[rel] = src
    lines = src.split("\n")

    def seg(a_line, a_col, b_col):
        return lines[a_line - 1].encode()[a_col:b_col].decode()

    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in CMP:
            l, r = node.left, node.comparators[0]
            if l.end_lineno == r.lineno:
                old, new = CMP[type(node.ops[0])]
                mid = seg(l.end_lineno, l.end_col_offset, r.col_offset)
                if mid.strip() == old:
                    sites["cmp"].append((rel, l.end_lineno, l.end_col_offset, r.col_offset, mid.replace(old, new, 1)))
        elif isinstance(node, ast.BoolOp):
            for a, b in zip(node.values, node.values[1:]):
                if a.end_lineno == b.lineno:
                    old, new = ("and", "or") if isinstance(node.op, ast.And) else ("or", "and")
                    mid = seg(a.end_lineno, a.end_col_offset, b.col_offset)
                    if mid.strip() == old:
                        sites["bool"].append((rel, a.end_lineno, a.end_col_offset, b.col_offset, mid.replace(old, new, 1)))
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not) and node.lineno == node.operand.lineno:
            if seg(node.lineno, node.col_offset, node.operand.col_offset).strip() == "not":
                sites["not"].append((rel, node.lineno, node.col_offset, node.operand.col_offset, ""))
        elif isinstance(node, ast.Constant) and node.lineno == node.end_lineno:
            text = seg(node.lineno, node.col_offset, node.end_col_offset)
            if node.value is True and text == "True":
                sites["flip"].append((rel, node.lineno, node.col_offset, node.end_col_offset, "False"))
            elif node.value is False and text == "False":
                sites["flip"].append((rel, node.lineno, node.col_offset, node.end_col_offset, "True"))
            elif type(node.value) is int and text.isdigit():
                sites["int"].append((rel, node.lineno, node.col_offset, node.end_col_offset, str(node.value + 1)))


def edit_line(src, ln, a, b, new):
    lines = src.split("\n")
    raw = lines[ln - 1].encode()
    lines[ln - 1] = (raw[:a] + new.encode() + raw[b:]).decode()
    return "\n".join(lines)


BY_FILE = {}
for _fam, _lst in sites.items():
    for _site in _lst:
        BY_FILE.setdefault(_site[0], {}).setdefault(_fam, []).append(_site)
FILES = sorted(BY_FILE)
FILE_UNIFORM = len(sys.argv) > 5 and sys.argv[5] == "file-uniform"


def code():
    for _ in range(50):
        if FILE_UNIFORM:  # the public sample looks file-uniform: mostly small dialect files
            rel = rng.choice(FILES)
            fam = rng.choice(sorted(BY_FILE[rel]))
            rel, ln, a, b, new = rng.choice(BY_FILE[rel][fam])
        else:
            fam = rng.choice(list(sites))
            rel, ln, a, b, new = rng.choice(sites[fam])
        out = edit_line(SRC[rel], ln, a, b, new)
        try:
            ast.parse(out)
        except SyntaxError:
            continue
        return rel, SRC[rel], out, "code-" + fam


def fixture():
    for _ in range(50):
        f = rng.choice(FIX)
        src = f.read_text()
        lines = src.split("\n")
        cand = [i for i, l in enumerate(lines) if l.strip() and not l.startswith(("--", "#"))]
        if not cand:
            continue
        i = rng.choice(cand)
        toks = lines[i].split(" ")
        j = rng.randrange(len(toks))
        r = rng.random()
        if r < 0.4:
            toks.insert(j, rng.choice(["42", "NULL", "1"]))
        elif r < 0.7 and len(toks) > 1:
            del toks[j]
        else:
            if not toks[j]:
                continue
            k = rng.randrange(len(toks[j]))
            toks[j] = toks[j][:k] + toks[j][k + 1:] if rng.random() < 0.5 else toks[j][:k] + "a" + toks[j][k:]
        new_line = " ".join(toks)
        if new_line == lines[i]:
            continue
        return str(f.relative_to(ROOT)), src, "\n".join(lines[:i] + [new_line] + lines[i + 1:]), "fixture"


def comment(exclude=()):
    for _ in range(50):
        f = rng.choice(PY)
        rel = str(f.relative_to(ROOT))
        if rel in exclude:
            continue
        lines = SRC[rel].split("\n")
        i = rng.randrange(1, len(lines))
        if not lines[i].strip() or lines[i - 1].rstrip().endswith("\\"):
            continue
        indent = re.match(r"\s*", lines[i]).group(0)
        out = "\n".join(lines[:i] + [indent + rng.choice(COMMENTS)] + lines[i:])
        try:
            ast.parse(out)
        except SyntaxError:
            continue
        return rel, SRC[rel], out, "comment"


def unified(rel, old, new):
    body = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{rel}", f"b/{rel}", n=3))
    return f"diff --git a/{rel} b/{rel}\nindex 1234567..89abcde 100644\n" + body


OUT.mkdir(parents=True, exist_ok=True)
kinds = []
for n in range(COUNT):
    r = rng.random()
    if r < 0.53:
        parts = [code()]
    elif r < 0.80:
        parts = [fixture()]
    elif r < 0.90:
        parts = [comment()]
    else:
        real = code() if rng.random() < 0.4 else fixture()
        parts = [comment(exclude=(real[0],)) for _ in range(rng.choice([1, 2]))] + [real]
        if len({p[0] for p in parts}) != len(parts):
            parts = parts[-2:]
    parts.sort(key=lambda p: p[0])
    cid = f"{OUT.name}{n:03d}"
    (OUT / f"{cid}.patch").write_text("".join(unified(rel, old, new) for rel, old, new, _ in parts))
    kind = "multi:" + "+".join(p[3] for p in parts if p[3] != "comment") if len(parts) > 1 else parts[0][3]
    kinds.append(f"{cid} {kind}")
(OUT.parent / f"{OUT.name}.kinds.txt").write_text("\n".join(kinds) + "\n")
print({k: len(v) for k, v in sites.items()}, "wrote", COUNT)
