"""Lines executed while importing every sqlglot module (definition time).

Run under the lab image. Writes <out>/import_closure.json: {rel_path: [lines]}.
Lazily imported modules (dialects) are included, so class-creation code is
known as import-time regardless of which test first triggered the import.
"""
import importlib
import json
import os
import pkgutil
import sys

import coverage

SRC = os.path.realpath(sys.argv[1])
OUT = sys.argv[2]
cov = coverage.Coverage(data_file=None, source=[os.path.join(SRC, "sqlglot")], branch=False)
cov.start()
import sqlglot  # noqa: E402

failed = []
for mod in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
    if mod.name.endswith("__main__"):
        continue
    try:
        importlib.import_module(mod.name)
    except Exception as e:  # noqa: BLE001
        failed.append(f"{mod.name}: {e!r}")
cov.stop()
data = cov.get_data()
out = {}
for f in data.measured_files():
    out[os.path.relpath(f, SRC)] = sorted(data.lines(f) or [])
with open(os.path.join(OUT, "import_closure.json"), "w") as fh:
    json.dump(out, fh)
print(f"{len(out)} files, {sum(map(len, out.values()))} lines; import failures: {failed}", file=sys.stderr)
