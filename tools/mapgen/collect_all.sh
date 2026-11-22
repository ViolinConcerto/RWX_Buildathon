#!/usr/bin/env bash
# Shard the suite by test file (LPT on recorded seconds) and collect maps in parallel.
# usage: collect_all.sh <tests.json> <out_dir> <shards> <first_cpu>
set -eu
TJ=$1; OUT=$(realpath -m "$2"); N=$3; C0=${4:-0}
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
  c=$((C0 + i*4)); (docker run --rm --cpuset-cpus $c-$((c+3)) --memory 3g -v "$HERE":/mapgen:ro -v "$OUT":/out br-lab /mapgen/collect.sh /out/shard$i $files > "$OUT/shard$i.status" 2>&1; echo "shard$i done" >> "$OUT/done.txt") &
  i=$((i+1))
done < "$OUT/shards.txt"
wait
