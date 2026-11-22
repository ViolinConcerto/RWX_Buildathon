"""Validation set for predicate-sensitivity reasoning: operator swaps.

Swaps < <-> <=, > <-> >=, and <-> or at sites the base line coverage executes
(chosen uniformly among executed lines). Labeled by full-suite runs; used only to
measure misses, never as a lookup table.
usage: python gen_opswaps.py <sqlglot_root> <depmap.json.gz> <out_dir> <count> [seed]
"""
import ast, difflib, gzip, json, random, sys
from pathlib import Path

ROOT, MAP, OUT, COUNT = Path(sys.argv[1]).resolve(), sys.argv[2], Path(sys.argv[3]), int(sys.argv[4])
rng = random.Random(int(sys.argv[5]) if len(sys.argv) > 5 else 11)
dm = json.load(gzip.open(MAP, "rt"))
SW = {ast.Lt: "<=", ast.LtE: "<", ast.Gt: ">=", ast.GtE: ">"}
TXT = {ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">="}
cands = []
for f in sorted((ROOT / "sqlglot").rglob("*.py")):
    rel = str(f.relative_to(ROOT)); src = f.read_text(); lines = src.split("\n")
    covered = dm["lines"].get(rel, {})
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in SW:
            l, r = node.left, node.comparators[0]
            if l.end_lineno == r.lineno and str(node.lineno) in covered:
                b = lines[l.end_lineno - 1].encode(); mid = b[l.end_col_offset:r.col_offset].decode()
                new_mid = mid.replace(TXT[type(node.ops[0])], SW[type(node.ops[0])], 1)
                if new_mid != mid:
                    cands.append(("cmp", rel, l.end_lineno, b[:l.end_col_offset].decode() + new_mid + b[r.col_offset:].decode()))
        elif isinstance(node, ast.BoolOp) and len(node.values) == 2:
            a, c = node.values
            if a.end_lineno == c.lineno and str(node.lineno) in covered:
                b = lines[a.end_lineno - 1].encode(); mid = b[a.end_col_offset:c.col_offset].decode()
                old, new = ("and", "or") if isinstance(node.op, ast.And) else ("or", "and")
                if mid.strip() == old:
                    new_line = b[:a.end_col_offset].decode() + mid.replace(old, new, 1) + b[c.col_offset:].decode()
                    cands.append(("bool", rel, a.end_lineno, new_line))
ONLY = sys.argv[6] if len(sys.argv) > 6 else None
if ONLY:
    cands = [c for c in cands if c[0] == ONLY]
rng.shuffle(cands)
OUT.mkdir(parents=True, exist_ok=True)
kinds, made, used = [], 0, set()
for kind, rel, ln, new_line in cands:
    if made >= COUNT or (rel, ln) in used:
        continue
    src = (ROOT / rel).read_text(); lines = src.split("\n"); lines[ln - 1] = new_line; new = "\n".join(lines)
    try:
        ast.parse(new)
    except SyntaxError:
        continue
    body = "".join(difflib.unified_diff(src.splitlines(True), new.splitlines(True), f"a/{rel}", f"b/{rel}", n=3))
    cid = f"{OUT.name}_{made:03d}"
    (OUT / f"{cid}.patch").write_text(f"diff --git a/{rel} b/{rel}\nindex 1234567..89abcde 100644\n" + body)
    kinds.append(f"{cid} op-{kind}"); used.add((rel, ln)); made += 1
(OUT.parent / f"{OUT.name}.kinds.txt").write_text("\n".join(kinds) + "\n")
print(f"candidates {len(cands)}; wrote {made}: " + str(sum(1 for k in kinds if 'cmp' in k)) + " cmp")
