#!/usr/bin/env bash
# usage: run_sample.sh <image> <output.jsonl> <sample_dir> <docker run flags...>
# Runs the image once like the scorer, records wall time and the container's
# CPU (cgroup cpu.stat usage_usec, all processes, at exit), then scores.
set -u
IMAGE=$1; OUT=$2; SAMPLE=$3; shift 3
LOG="${OUT%.jsonl}.log"
start=$(date +%s.%N)
docker run --rm "$@" "$IMAGE" sh -c 'python /app/selector/main.py; rc=$?; grep usage_usec /sys/fs/cgroup/cpu.stat >&2; exit $rc' > "$OUT" 2> "$LOG"
rc=$?
end=$(date +%s.%N)
cpu=$(awk '/usage_usec/ {print $2/1000000}' "$LOG")
printf 'exit: %s  wall: %.2f s  cpu: %s s\n' "$rc" "$(echo "$end - $start" | bc)" "${cpu:-?}"
python3 "$(dirname "$0")/../starter/score.py" "$SAMPLE" "$SAMPLE/answers.json" "$OUT" ${cpu:+--cpu-seconds $cpu}
