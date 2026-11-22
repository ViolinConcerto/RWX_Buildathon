"""Adversarial validation set for selector/equiv.py: identity <-> equality swaps.

Picks single-operator `is`/`is not`/`==`/`!=` comparisons that the base test run
executes and swaps the operator (is<->==, is not<->!=). About 70% are drawn from
sites the profile-based proof accepts, 30% from sites it rejects, so both the
proof's claims and its refusals get checked against full-suite labels.

usage: python gen_swaps.py <sqlglot_root> <cmp_profile.json.gz> <out_dir> <count> [seed]
"""

import ast
import difflib
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "selector"))
from equiv import CmpProfile  # noqa: E402

ROOT, PROF, OUT, COUNT = Path(sys.argv[1]).resolve(), sys.argv[2], Path(sys.argv[3]), int(sys.argv[4])
SEED = int(sys.argv[5]) if len(sys.argv) > 5 else 7
rng = random.Random(SEED)
prof = CmpProfile(PROF)
SWAP = {ast.Is: "==", ast.Eq: "is", ast.IsNot: "!=", ast.NotEq: "is not"}

provable, unprovable = [], []
for f in sorted((ROOT / "sqlglot").rglob("*.py")):
    rel = str(f.relative_to(ROOT))
    src = f.read_text()
    lines = src.split("\n")
    for node in ast.walk(ast.parse(src)):
        if not (isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in SWAP):
            continue
        left, right = node.left, node.comparators[0]
        if left.end_lineno != right.lineno:
            continue
        key = f"{rel}:{node.lineno}:{node.col_offset}"
        pairs = prof.sites.get(key)
        if not pairs:
            continue
        line = lines[left.end_lineno - 1]
        # col offsets are UTF-8 byte offsets
        b = line.encode()
        mid = b[left.end_col_offset:right.col_offset].decode()
        new_mid = mid.replace({ast.Is: "is", ast.Eq: "==", ast.IsNot: "is not", ast.NotEq: "!="}[type(node.ops[0])],
                              SWAP[type(node.ops[0])], 1)
        if new_mid == mid:
            continue
        new_line = (b[:left.end_col_offset].decode() + new_mid + b[right.col_offset:].decode())
        ok = all(prof.pair_equivalent(*p) for p in pairs)
        (provable if ok else unprovable).append((rel, left.end_lineno, line, new_line, src))

rng.shuffle(provable)
rng.shuffle(unprovable)
n_prov = round(COUNT * 0.7)
chosen = [("prov", x) for x in provable[:n_prov]] + [("unprov", x) for x in unprovable[: COUNT - n_prov]]
OUT.mkdir(parents=True, exist_ok=True)
kinds = []
for i, (tag, (rel, ln, line, new_line, src)) in enumerate(chosen):
    lines = src.split("\n")
    lines[ln - 1] = new_line
    new = "\n".join(lines)
    a = src.splitlines(keepends=True)
    bb = new.splitlines(keepends=True)
    body = "".join(difflib.unified_diff(a, bb, f"a/{rel}", f"b/{rel}", n=3))
    cid = f"s{SEED:02d}{i:03d}"
    (OUT / f"{cid}.patch").write_text(f"diff --git a/{rel} b/{rel}\nindex 1234567..89abcde 100644\n" + body)
    kinds.append(f"{cid} swap-{tag}")
(OUT.parent / f"{OUT.name}.kinds.txt").write_text("\n".join(kinds) + "\n")
print(f"provable sites {len(provable)}, unprovable {len(unprovable)}; wrote {len(chosen)} patches")
