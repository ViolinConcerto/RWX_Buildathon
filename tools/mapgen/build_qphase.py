"""Per-query, per-phase line map for test_execute_tpcds (from qphase_plugin output).
usage (lab image): build_qphase.py <qphase_dir> <out.json.gz>
{"n": queries, "cpu": {"F": [..], "D": [..], "C": [..]}, "common": {rel: [lines]},
 "lines": {phase: {rel: {line: [queries]}}}}"""
import gzip, json, os, sys
from collections import defaultdict
from coverage import CoverageData
qdir, out = sys.argv[1:]
data = CoverageData(os.path.join(qdir, "qphase.sqlite")); data.read()
cpu = json.load(open(os.path.join(qdir, "qphase_cpu_nocov.json")))
lines = {p: defaultdict(lambda: defaultdict(set)) for p in "FTDC"}
common = defaultdict(set)
n = 0
for path in data.measured_files():
    rel = os.path.relpath(path, "/src/sqlglot")
    if not rel.startswith("sqlglot/"):
        continue
    for ln, ctxs in data.contexts_by_lineno(path).items():
        for c in ctxs:
            parts = c.split(":")
            if len(parts) == 3 and parts[0] == "tpcds":
                i = int(parts[1]); n = max(n, i + 1)
                lines[parts[2]][rel][ln].add(i)
            elif parts[0] != "tpch":
                common[rel].add(ln)
n = max(n, 1 + max(int(k.split(":")[1]) for k in cpu if k.startswith("tpcds:")))
res = {"n": n,
       "cpu": {p: [round(cpu.get(f"tpcds:{i}:{p}", 0.0), 4) for i in range(n)] for p in "FTDC"},
       "common": {rel: sorted(v) for rel, v in common.items()},
       "lines": {p: {rel: {str(l): sorted(q) for l, q in m.items()} for rel, m in lines[p].items()} for p in "FTDC"}}
with gzip.open(out, "wt") as f:
    json.dump(res, f, separators=(",", ":"))
print("queries", n, {p: (len(lines[p]), round(sum(res["cpu"][p]), 2)) for p in "FTDC"}, "common files", len(common),
      "size", os.path.getsize(out))
