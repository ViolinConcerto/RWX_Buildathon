"""Two-stage check of tests/test_executor.py::TestExecutor::test_execute_tpcds.

A TPC-DS sub-case = parse (in the test) -> pickle round trip -> execute(): optimize +
Plan (the "front"), then PythonExecutor over the data, then a comparison with duckdb's
answer for transpile(sql, write="duckdb"). The data phase costs ~90% of the test.

Stage 1 recomputes, under the patch, only the front of each affected query and the duckdb
text, and compares them with the pinned commit's (stored at image build). A query whose
plan and duckdb text are unchanged, and whose data/compare phases never executed an edited
line on the pinned commit, gets exactly the same input into an unchanged data phase and an
unchanged expected answer -- so its outcome cannot change. Everything else goes to stage 2
(the real sub-case). The plan fingerprint is structural (class names, arguments, step
fields); it does not use sqlglot's generator or serde, which a patch may change.

usage (build): python tpcplan.py <sqlglot root> <out.json>
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import pickle
import sys


def _h(*parts: str) -> str:
    return hashlib.blake2b("\x1f".join(parts).encode("utf-8", "surrogatepass"), digest_size=10).hexdigest()


def value_fp(v, memo=None) -> str:
    """Structural digest of plans, steps, expressions and plain values."""
    from sqlglot.expressions.core import Expr
    from sqlglot.planner import Step

    if memo is None:
        memo = {}
    if v is None or isinstance(v, (bool, int, str, bytes)):
        return _h(type(v).__name__, repr(v))
    if isinstance(v, float):
        return _h("float", "inf" if math.isinf(v) else repr(v))
    i = id(v)
    if i in memo:
        return memo[i]
    memo[i] = "cycle"
    if isinstance(v, Expr):
        parts = [type(v).__module__ + "." + type(v).__qualname__]
        for k in sorted(v.args):
            parts += [k, value_fp(v.args[k], memo)]
        t = getattr(v, "_type", None)
        if t is not None:
            parts += ["<type>", value_fp(t, memo)]
        out = _h(*parts)
    elif isinstance(v, Step):
        parts = [type(v).__qualname__]
        for k in sorted(vars(v)):
            if k in ("dependents",):
                continue
            if k == "dependencies":
                parts += [k, _h(*sorted(value_fp(d, memo) for d in v.dependencies))]
            else:
                parts += [k, value_fp(getattr(v, k), memo)]
        out = _h(*parts)
    elif isinstance(v, (list, tuple)):
        out = _h(type(v).__name__, *[value_fp(x, memo) for x in v])
    elif isinstance(v, (set, frozenset)):
        out = _h("set", *sorted(value_fp(x, memo) for x in v))
    elif isinstance(v, dict):
        out = _h("dict", *[p for k, x in v.items() for p in (value_fp(k, memo), value_fp(x, memo))])
    elif isinstance(v, type):
        out = _h("type", v.__module__ + "." + v.__qualname__)
    else:
        d = getattr(v, "__dict__", None)
        if isinstance(d, dict):
            out = _h("obj", type(v).__qualname__, *[p for k in sorted(d) for p in (k, value_fp(d[k], memo))])
        else:
            out = _h("opaque", type(v).__qualname__, repr(v)[:200])
    memo[i] = out
    return out


def front(sql: str, meta: dict, schema_dict: dict) -> dict:
    """Stage 1 for one fixture query, mirroring the test's and execute()'s front steps."""
    from sqlglot import find_tables, parse_one, transpile
    from sqlglot.executor import Plan, ensure_schema, optimize

    out: dict = {}
    expression = pickle.loads(pickle.dumps(parse_one(sql)))
    if not meta.get("execute"):
        out["fp"] = "no-exec"
        return out
    # mp_execute: the worker gets the tables find_tables() names
    out["tables"] = sorted({t.name for t in find_tables(expression)})
    # execute(): the same calls in the same order (the debug log renders the SQL eagerly)
    schema = ensure_schema(schema_dict, dialect=None)
    optimized = optimize(expression, schema, leave_tables_isolated=True, dialect=None)
    optimized.sql(pretty=True)
    out["fp"] = value_fp(Plan(optimized))
    out["duck"] = transpile(sql, write="duckdb")[0]
    return out


def stage1(indices, base: dict) -> list[int]:
    """Indices (of `indices`) whose front or duckdb text differs from the pinned commit's."""
    import tests.test_executor as te
    from tests.helpers import load_sql_fixture_pairs

    sqls = list(load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))
    changed = []
    for i in indices:
        b = base.get(str(i))
        if b is None or b.get("unstable") or i >= len(sqls):
            changed.append(i)
            continue
        meta, sql, _ = sqls[i]
        try:
            got = front(sql, meta, te.TPCDS_SCHEMA)
        except Exception:  # noqa: BLE001  (the real sub-case will fail the same way)
            changed.append(i)
            continue
        if got.get("fp") != b.get("fp") or got.get("duck") != b.get("duck") or got.get("tables") != b.get("tables"):
            changed.append(i)
    return changed


def _one(root: str) -> None:
    sys.path.insert(0, root)
    os.chdir(root)
    os.environ["SKIP_INTEGRATION"] = "0"
    import tests.test_executor as te
    from tests.helpers import load_sql_fixture_pairs

    out = {}
    for i, (meta, sql, _) in enumerate(load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql")):
        try:
            out[str(i)] = front(sql, meta, te.TPCDS_SCHEMA)
        except Exception as e:  # noqa: BLE001
            out[str(i)] = {"error": repr(e)[:200]}
    json.dump(out, sys.stdout)


def main() -> None:
    import subprocess

    root, out_path = sys.argv[1], sys.argv[2]
    runs = []
    for seed in ("3", "41", "977"):
        env = dict(os.environ, PYTHONHASHSEED=seed, SKIP_INTEGRATION="0", PYTHONPATH=root)
        res = subprocess.run([sys.executable, os.path.abspath(__file__), "--one", root], env=env,
                             capture_output=True, text=True, check=True)
        runs.append(json.loads(res.stdout))
    base = {}
    for i in runs[0]:
        vals = [r.get(i) for r in runs]
        if any("error" in v for v in vals) or any(v != vals[0] for v in vals):
            base[i] = {"unstable": True}
        else:
            base[i] = vals[0]
    with open(out_path, "w") as f:
        json.dump(base, f)
    unstable = sorted(int(i) for i, v in base.items() if v.get("unstable"))
    print(f"tpcds plans: {len(base)} queries, unstable across hash seeds: {unstable}", file=sys.stderr)


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--one":
        _one(sys.argv[2])
    else:
        main()
