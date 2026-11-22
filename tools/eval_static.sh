#!/usr/bin/env bash
# Static-only evaluation of the working-tree selector on labeled sets (local python).
# usage: eval_static.sh <tag> <set_dir>...
tag=$1; shift
for d in "$@"; do
  name=$(basename "$d")
  BR_VERIFY=0 BR_DATA="$d" BR_SRC=work/sqlglot python3 selector/main.py > "work/eval_${tag}_${name}.jsonl" 2> "work/eval_${tag}_${name}.log"
  printf '%-10s %-8s ' "$tag" "$name"
  python3 tools/breakdown.py "$d" "work/eval_${tag}_${name}.jsonl" "work/eval_${tag}_${name}.log" --top 0 | head -1
done
