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
called = set()  # (rel, co_qualname, co_firstlineno) of sqlglot code objects run during import
PREFIX = os.path.join(SRC, "sqlglot") + os.sep


def _profile(frame, event, arg):
    if event == "call":
        co = frame.f_code
        if co.co_filename.startswith(PREFIX) and co.co_name != "<module>":
            called.add((os.path.relpath(co.co_filename, SRC), co.co_qualname, co.co_firstlineno))


cov.start()
sys.setprofile(_profile)
import sqlglot  # noqa: E402

failed = []
for mod in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
    if mod.name.endswith("__main__"):
        continue
    try:
        importlib.import_module(mod.name)
    except Exception as e:  # noqa: BLE001
        failed.append(f"{mod.name}: {e!r}")
sys.setprofile(None)
cov.stop()
data = cov.get_data()
out = {}
for f in data.measured_files():
    out[os.path.relpath(f, SRC)] = sorted(data.lines(f) or [])
with open(os.path.join(OUT, "import_closure.json"), "w") as fh:
    json.dump(out, fh)
with open(os.path.join(OUT, "import_called.json"), "w") as fh:
    json.dump(sorted(called), fh)
print(f"{len(out)} files, {sum(map(len, out.values()))} lines; import failures: {failed}", file=sys.stderr)
