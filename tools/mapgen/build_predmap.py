"""Merge predicate-profile shards into maps/predmap.json.gz.

usage: python build_predmap.py <tests.json> <out.json.gz> <shard_dir>...

Per site ("rel:line:col:c|b"): signature -> tests that observed it. Context
attribution mirrors build_map.py (setup/teardown -> whole class, module
collection -> whole file). Sites evaluated while modules were imported or
outside tests are flagged in "import_sites" (reasoning must not narrow those).
"""

import gzip
import json
import os
import sys
from collections import defaultdict


def main():
    tests_json, out_path, *shards = sys.argv[1:]
    tests = json.load(open(tests_json))["tests"]
    number = {t["id"]: t["number"] for t in tests}
    cls_of = {t["number"]: t["id"].rsplit("::", 1)[0] for t in tests}
    by_class, by_file = defaultdict(list), defaultdict(list)
    for t in tests:
        by_class[cls_of[t["number"]]].append(t["number"])
        by_file[t["file"]].append(t["number"])

    def expand(ctx):
        if ctx.startswith("collect|"):
            return by_file.get(ctx[len("collect|"):], []), False
        if "|" in ctx:
            nodeid, phase = ctx.rsplit("|", 1)
            n = number.get(nodeid)
            if n is None:
                return [], False
            return (by_class[cls_of[n]] if phase in ("setup", "teardown") else [n]), False
        return [], True  # import / init / between

    obs = defaultdict(lambda: defaultdict(set))
    import_sites = set()
    failed = []
    for shard in shards:
        raw = json.load(open(os.path.join(shard, "pred.json")))
        failed += raw["failed"]
        for ctx, sites in raw["obs"].items():
            nums, is_global = expand(ctx)
            for site, sigs in sites.items():
                if is_global:
                    import_sites.add(site)
                    continue
                for sig in sigs:
                    obs[site][sig].update(nums)
    sets, set_list = {}, []

    def sid(s):
        k = frozenset(s)
        if k not in sets:
            sets[k] = len(set_list)
            set_list.append(sorted(k))
        return sets[k]

    out = {
        "tests": [t["id"] for t in tests],
        "sites": {site: {sig: sid(nums) for sig, nums in sorted(d.items())} for site, d in sorted(obs.items())},
        "import_sites": sorted(import_sites),
        "sets": set_list,
    }
    with gzip.open(out_path, "wt") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"sites={len(obs)} import_sites={len(import_sites)} sets={len(set_list)} failed={failed} "
          f"size={os.path.getsize(out_path)}", file=sys.stderr)


if __name__ == "__main__":
    main()
