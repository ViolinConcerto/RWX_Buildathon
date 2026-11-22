"""Merge attr_read_plugin shards into maps/attrreads.json.gz.
usage: python build_attrmap.py <tests.json> <out.json.gz> <shard_dir>...
binding ("<class key>.<attr>") -> tests that read it; "tracked" lists every recorded binding.
Attribution as in build_map.py: setup/teardown reads count for the whole class, reads made
while a test module was collected count for every test of that file."""
import gzip, json, os, sys
from collections import defaultdict

tests_json, out_path, *shards = sys.argv[1:]
tests = json.load(open(tests_json))["tests"]
number = {t["id"]: t["number"] for t in tests}
cls_of = {t["number"]: t["id"].rsplit("::", 1)[0] for t in tests}
by_class, by_file = defaultdict(list), defaultdict(list)
for t in tests:
    by_class[cls_of[t["number"]]].append(t["number"]); by_file[t["file"]].append(t["number"])
reads, tracked, global_reads = defaultdict(set), set(), set()
for shard in shards:
    d = json.load(open(os.path.join(shard, "attr_reads.json")))
    assert not d["failed"], d["failed"]
    tracked |= set(d["tracked"])
    for ctx, keys in d["reads"].items():
        if ctx.startswith("collect|"):
            nums = by_file.get(ctx[len("collect|"):], [])
        elif "|" in ctx:
            nodeid, phase = ctx.rsplit("|", 1)
            n = number.get(nodeid)
            nums = [] if n is None else (by_class[cls_of[n]] if phase in ("setup", "teardown") else [n])
        else:
            nums = None  # init / between / wrap: not attributable to a test
        for k in keys:
            if nums is None:
                global_reads.add(k)
            else:
                reads[k].update(nums)
sets, set_list = {}, []
def sid(s):
    k = tuple(sorted(s))
    if k not in sets:
        sets[k] = len(set_list); set_list.append(list(k))
    return sets[k]
out = {"tests": [t["id"] for t in tests], "tracked": sorted(tracked), "global_reads": sorted(global_reads),
       "reads": {k: sid(v) for k, v in sorted(reads.items())}, "sets": set_list}
with gzip.open(out_path, "wt") as f:
    json.dump(out, f, separators=(",", ":"))
sizes = sorted(len(v) for v in reads.values())
print(f"tracked={len(tracked)} read-by-tests={len(reads)} global-only={len(global_reads - set(reads))} sets={len(set_list)} "
      f"median readers={sizes[len(sizes)//2]} size={os.path.getsize(out_path)}")
