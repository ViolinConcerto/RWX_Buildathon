#!/bin/sh
# Runs inside the lab image: collect coverage/reads/classes for a shard of test files.
# usage: collect.sh <out_dir> <test files...>
set -e
OUT="$1"; shift
mkdir -p "$OUT"
cd /src/sqlglot
export PYTHONPATH=/src/sqlglot:/mapgen SRC_ROOT=/src/sqlglot MAP_OUT="$OUT" COVERAGE_CORE=ctrace
python -m pytest "$@" -q -p no:cacheprovider -p collect_plugin --timeout=1800 > "$OUT/pytest.log" 2>&1 || true
tail -2 "$OUT/pytest.log"
