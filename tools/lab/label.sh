#!/bin/sh
# usage: label.sh <patch-file-or-"none"> <out.json> [pytest args...]
# Copies sqlglot to /tmp/w, applies the patch, runs the full suite, writes outcomes.
set -e
P="$1"; OUT="$2"; shift 2
rm -rf /tmp/w && cp -r /src/sqlglot /tmp/w && cd /tmp/w
if [ "$P" != none ]; then patch -p1 -s < "$P"; fi
export PYTHONPATH=/tmp/w:/lab OUTCOMES="$OUT"
python -m pytest tests -q -p no:cacheprovider -p outcomes_plugin --timeout=600 "$@" > /tmp/pytest.log 2>&1 || true
tail -3 /tmp/pytest.log >&2
