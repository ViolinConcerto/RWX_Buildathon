#!/usr/bin/env bash
# usage: run_sets.sh <tag> [extra docker run flags...] -- <set_dir>...
# Runs the built image on each labeled set with the scorer's flags; prints sim / CPU / misses.
tag=$1; shift; extra=()
while [ "$1" != "--" ]; do extra+=("$1"); shift; done; shift
export PATH="$(cd "$(dirname "$0")" && pwd)/bin:$PATH"
tsim=0; tcpu=0; tmiss=0
for set in "$@"; do
  name=$(basename "$set"); out=work/${tag}_$name.jsonl
  start=$(date +%s.%N)
  docker run --rm --platform linux/amd64 --network none --cpus 2 --memory 4096m --memory-swap 4096m --pids-limit 256 --read-only --tmpfs /tmp:rw,size=64m,mode=1777 --cap-drop ALL --security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp "${extra[@]}" --volume "$PWD/$set/commits:/data/commits:ro" --volume "$PWD/$set/tests.json:/data/tests.json:ro" blast-radius-entry sh -c 'python /app/selector/main.py; rc=$?; grep usage_usec /sys/fs/cgroup/cpu.stat >&2; exit $rc' > "$out" 2> "${out%.jsonl}.log"
  wall=$(echo "$(date +%s.%N) - $start" | bc)
  cpu=$(awk '/usage_usec/ {print $2/1000000}' "${out%.jsonl}.log")
  res=$(python3 starter/score.py "$set" "$set/answers.json" "$out")
  sim=$(echo "$res" | awk '/Simulated/ {print $2}'); miss=$(echo "$res" | awk '/Misses/ {print $2}')
  printf '%-4s %-7s sim=%9s cpu=%7.2f total=%9.2f misses=%s wall=%.1f %s\n' "$tag" "$name" "$sim" "$cpu" "$(echo "$sim + $cpu" | bc)" "$miss" "$wall" "$(echo "$res" | grep missed | tr '\n' ' ')"
done
