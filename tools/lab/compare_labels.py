"""Compare lab labels (full-suite runs) with answers.json. usage: compare_labels.py <sample_dir> <labels_dir>"""
import json, sys
from pathlib import Path
sample, labels = Path(sys.argv[1]), Path(sys.argv[2])
tests = json.loads((sample / "tests.json").read_text())["tests"]
num = {t["id"]: t["number"] for t in tests}
answers = json.loads((sample / "answers.json").read_text())["commits"]
base = json.loads((labels / "none.json").read_text()) if (labels / "none.json").exists() else None
base_failed = set(base["failed"]) if base else set()
print("baseline failures:", sorted(base_failed))
ok = bad = 0
for cid in sorted(answers):
    f = labels / f"{cid}.json"
    if not f.exists():
        continue
    d = json.loads(f.read_text())
    got = {num[n] for n in d["failed"] if n in num}
    if d["collect_errors"]:
        for ce in d["collect_errors"]:
            got |= {t["number"] for t in tests if t["file"] == ce or t["id"].startswith(ce + "::")}
    want = set(answers[cid]["failing"])
    status = "OK" if got == want else "DIFF"
    ok += got == want; bad += got != want
    extra = sorted(tests[i]["id"] for i in got - want)
    missing = sorted(tests[i]["id"] for i in want - got)
    print(f"{cid} {answers[cid]['kind']:8s} {status} want={len(want)} got={len(got)}"
          + (f" extra={extra}" if extra else "") + (f" missing={missing}" if missing else ""))
print(f"match {ok}  differ {bad}")
