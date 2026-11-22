#!/usr/bin/env bash
# usage: label_all.sh <commits_dir> <out_dir> [parallel=6]
# Runs the full sqlglot suite once per patch (plus "none") in the lab image,
# each container pinned to its own 4-CPU set. Writes <out_dir>/<id>.json.
set -u
CDIR=$(realpath "$1"); OUT=$(realpath "$2"); PAR=${3:-6}; CPU0=${4:-0}
LAB=$(cd "$(dirname "$0")" && pwd)
export PATH="$LAB/../bin:$PATH"
mkdir -p "$OUT"
ids=$( (echo none; ls "$CDIR" | sed 's/\.patch$//') )
i=0
for id in $ids; do
  [ -s "$OUT/$id.json" ] && continue
  slot=$(( i % PAR )); i=$((i+1))
  echo "$slot $id"
done | xargs -P "$PAR" -L 1 sh -c '
  slot=$0; id=$1; c0=$(('"$CPU0"'+slot*4)); c1=$((c0+3))
  if [ "$id" = none ]; then p=none; else p=/commits/$id.patch; fi
  start=$(date +%s)
  docker run --rm --cpuset-cpus $c0-$c1 --memory 2400m \
    -v '"$LAB"':/lab:ro -v '"$OUT"':/out -v '"$CDIR"':/commits:ro br-lab \
    /lab/label.sh $p /out/$id.json -n 2 >/dev/null 2>"'"$OUT"'/$id.err"
  echo "$id done in $(( $(date +%s)-start ))s"
'
