#!/usr/bin/env bash
# Operand-type profile of comparison sites, sharded by test file (LPT), 4 at a time.
# usage: cmp_collect_all.sh <tests.json> <out_dir>
set -eu
TJ=$1; OUT=$(realpath -m "$2"); N=4
HERE=$(cd "$(dirname "$0")" && pwd); export PATH="$HERE/../bin:$PATH"
mkdir -p "$OUT"
python3 - "$TJ" "$N" > "$OUT/shards.txt" <<'PY'
import json, sys, collections
tests = json.load(open(sys.argv[1]))["tests"]; n = int(sys.argv[2])
sec = collections.Counter()
for t in tests: sec[t["file"]] += t["seconds"]
loads = [[0.0, []] for _ in range(n)]
for f, s in sorted(sec.items(), key=lambda kv: -kv[1]):
    b = min(loads, key=lambda l: l[0]); b[0] += s; b[1].append(f)
for l in loads: print(" ".join(l[1]))
PY
i=0
while read -r files; do
  c=$((i*4)); mkdir -p "$OUT/shard$i"
  (docker run --rm --cpuset-cpus $c-$((c+3)) --memory 2500m -v "$HERE":/mapgen:ro -v "$OUT/shard$i":/out br-lab \
     sh -c "cd /src/sqlglot && PYTHONPATH=/src/sqlglot:/mapgen SRC_ROOT=/src/sqlglot CMP_OUT=/out python -m pytest $files -q -p no:cacheprovider -p cmp_profile_plugin --timeout=1800 > /out/pytest.log 2>&1; tail -1 /out/pytest.log" > "$OUT/shard$i.status" 2>&1; echo "shard$i done" >> "$OUT/done.txt") &
  i=$((i+1))
done < "$OUT/shards.txt"
wait
cat "$OUT"/shard*.status
