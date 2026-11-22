"""Per-query map for the TPC executor tests (from query_cov_plugin output).
Runs in the lab image (needs coverage). usage: build_qmap.py <qcov_dir> <out.json.gz>"""
import gzip, json, os, sys
from collections import defaultdict
from coverage import CoverageData

qdir, out = sys.argv[1:]
data = CoverageData(os.path.join(qdir, "qcov.sqlite")); data.read()
cpu = json.load(open(os.path.join(qdir, "qcpu_nocov.json")))
suites = {"tpch": "tests/test_executor.py::TestExecutor::test_execute_tpch",
          "tpcds": "tests/test_executor.py::TestExecutor::test_execute_tpcds"}
lines = {s: defaultdict(dict) for s in suites}
common = defaultdict(set)
for path in data.measured_files():
    rel = os.path.relpath(path, "/src/sqlglot")
    for ln, ctxs in data.contexts_by_lineno(path).items():
        for c in ctxs:
            if ":" in c and c.split(":")[0] in suites:
                s, i = c.split(":"); lines[s][rel].setdefault(ln, []).append(int(i))
            else:
                common[rel].add(ln)
res = {"setup_cpu": round(cpu.get("common", 0.0), 3), "common": {r: sorted(v) for r, v in common.items()}, "suites": {}}
for s, nodeid in suites.items():
    n = 1 + max([int(k.split(":")[1]) for k in cpu if k.startswith(s + ":")] or [-1])
    res["suites"][s] = {"test": nodeid, "n": n,
                        "cpu": [round(cpu.get(f"{s}:{i}", 0.0), 3) for i in range(n)],
                        "lines": {r: {str(l): sorted(set(q)) for l, q in m.items()} for r, m in lines[s].items()}}
with gzip.open(out, "wt") as f:
    json.dump(res, f, separators=(",", ":"))
print({s: (v["n"], round(sum(v["cpu"]), 2), len(v["lines"])) for s, v in res["suites"].items()}, "common files", len(common))
