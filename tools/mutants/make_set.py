"""Turn lab labels into a scorer-compatible set: <out>/{commits/,tests.json,answers.json}.
usage: make_set.py <patch_dir> <labels_dir> <tests.json> <kinds.txt> <out_dir>"""
import json, shutil, sys
from pathlib import Path
pdir, ldir, tj, kinds, out = map(Path, sys.argv[1:])
tests = json.loads(tj.read_text())["tests"]
num = {t["id"]: t["number"] for t in tests}
kind = dict(l.split(" ", 1) for l in kinds.read_text().split("\n") if l)
base = json.loads((ldir / "none.json").read_text())
assert not base["failed"], f"baseline failures: {base['failed']}"
(out / "commits").mkdir(parents=True, exist_ok=True)
shutil.copy(tj, out / "tests.json")
answers, skipped = {}, []
for p in sorted(pdir.glob("*.patch")):
    f = ldir / f"{p.stem}.json"
    if not f.exists():
        skipped.append(p.stem); continue
    d = json.loads(f.read_text())
    failing = {num[n] for n in d["failed"] if n in num}
    for ce in d["collect_errors"]:
        failing |= {t["number"] for t in tests if t["id"].startswith(ce + "::") or t["file"] == ce}
    answers[p.stem] = {"failing": sorted(failing), "kind": kind.get(p.stem, "?").strip()}
    shutil.copy(p, out / "commits" / p.name)
(out / "answers.json").write_text(json.dumps({"commits": answers}))
broke = sum(1 for a in answers.values() if a["failing"])
print(f"{len(answers)} commits ({broke} break something), skipped {skipped}")
