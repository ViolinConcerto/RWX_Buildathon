"""Merge comparison-profile shards. usage: merge_cmp.py <out.json.gz> <shard_dir>..."""
import gzip, json, sys
out, *shards = sys.argv[1:]
sites, types, inst = {}, {}, 0
for d in shards:
    raw = json.load(open(f"{d}/cmp_profile.json"))
    inst = max(inst, raw["instrumented"])
    for k, pairs in raw["sites"].items():
        sites.setdefault(k, set()).update(tuple(p) for p in pairs)
    types.update(raw["types"])
data = {"sites": {k: sorted(map(list, v)) for k, v in sorted(sites.items())}, "types": types}
with gzip.open(out, "wt", encoding="utf-8") as f:
    json.dump(data, f, separators=(",", ":"))
print(f"sites={len(sites)} types={len(types)} instrumented(max shard)={inst}")
