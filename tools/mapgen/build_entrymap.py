"""Build maps/entrymap.json.gz from entry_cov_plugin output (coverage + no-cov timing runs).

usage (lab image): python build_entrymap.py <tests.json> <out.json.gz> <cov_dir>:<nocov_dir> ...

Per test with fixture entries: for each fixture file, the entry count, per-entry CPU,
and line -> entries covering it; plus "common" lines (the test's own setup/call/
teardown outside entries, and class fixtures of its class) and their CPU.
"""

import gzip
import json
import os
import sys
from collections import defaultdict

from coverage import CoverageData


def main():
    tests_json, out_path, *pairs = sys.argv[1:]
    tests = json.load(open(tests_json))["tests"]
    cls_of = {t["id"]: t["id"].rsplit("::", 1)[0] for t in tests}
    by_class = defaultdict(list)
    for t in tests:
        by_class[cls_of[t["id"]]].append(t["id"])
    entry_lines = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: defaultdict(set))))
    base_lines = defaultdict(lambda: defaultdict(set))   # nodeid -> rel -> lines (own phases)
    setup_lines = defaultdict(lambda: defaultdict(set))  # class -> rel -> lines (setup/teardown)
    counts = defaultdict(lambda: defaultdict(int))
    cpu_entry = defaultdict(lambda: defaultdict(dict))
    cpu_base = defaultdict(float)
    for pair in pairs:
        cov_dir, nocov_dir = pair.split(":")
        data = CoverageData(os.path.join(cov_dir, "entry.sqlite"))
        data.read()
        for path in data.measured_files():
            rel = os.path.relpath(path, "/src/sqlglot")
            for ln, ctxs in data.contexts_by_lineno(path).items():
                for c in ctxs:
                    if "#" in c:
                        nodeid, fname, i = c.rsplit("#", 2)
                        entry_lines[nodeid][fname][rel][ln].add(int(i))
                        counts[nodeid][fname] = max(counts[nodeid][fname], int(i) + 1)
                    elif "|" in c:
                        nodeid, phase = c.rsplit("|", 1)
                        if nodeid not in cls_of:
                            continue
                        if phase in ("setup", "teardown"):
                            setup_lines[cls_of[nodeid]][rel].add(ln)
                        else:
                            base_lines[nodeid][rel].add(ln)
        timing = json.load(open(os.path.join(nocov_dir, "entry_cpu_nocov.json")))
        for c, v in timing["cpu"].items():
            if "#" in c:
                nodeid, fname, i = c.rsplit("#", 2)
                cpu_entry[nodeid][fname][int(i)] = cpu_entry[nodeid][fname].get(int(i), 0.0) + v
                counts[nodeid][fname] = max(counts[nodeid][fname], int(i) + 1)
            elif "|" in c and c.rsplit("|", 1)[0] in cls_of:
                cpu_base[c.rsplit("|", 1)[0]] += v

    sets, set_list = {}, []

    def sid(s):
        k = tuple(sorted(s))
        if k not in sets:
            sets[k] = len(set_list)
            set_list.append(list(k))
        return sets[k]

    out_tests = {}
    for nodeid, groups in entry_lines.items():
        common = defaultdict(set)
        for rel, ls in base_lines[nodeid].items():
            common[rel] |= ls
        for rel, ls in setup_lines[cls_of.get(nodeid, "")].items():
            common[rel] |= ls
        g_out = {}
        for fname, rels in groups.items():
            n = counts[nodeid][fname]
            g_out[fname] = {
                "n": n,
                "cpu": [round(cpu_entry[nodeid][fname].get(i, 0.0), 4) for i in range(n)],
                "lines": {rel: {str(ln): sid(idx) for ln, idx in sorted(m.items())} for rel, m in rels.items()
                          if rel.startswith("sqlglot/")},
            }
        out_tests[nodeid] = {
            "groups": g_out,
            "common": {rel: sorted(ls) for rel, ls in common.items() if rel.startswith("sqlglot/")},
            "base_cpu": round(cpu_base[nodeid], 3),
        }
    with gzip.open(out_path, "wt") as f:
        json.dump({"tests": out_tests, "sets": set_list}, f, separators=(",", ":"))
    ent = sum(g["n"] for t in out_tests.values() for g in t["groups"].values())
    print(f"tests={len(out_tests)} entries={ent} sets={len(set_list)} size={os.path.getsize(out_path)}", file=sys.stderr)


if __name__ == "__main__":
    main()
