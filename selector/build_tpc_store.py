"""Build-time store for the TPC-H / TPC-DS executor tests (runs in the Dockerfile).

usage: SKIP_INTEGRATION=0 PYTHONPATH=<sqlglot> python build_tpc_store.py <sqlglot root> <out dir>

Writes:
  rows.pkl     {"tpch"|"tpcds": {table: rows}} -- the CSV rows exactly as
               TestExecutor.setUpClass parses them (depends on the CSV files and
               ast.literal_eval only, not on sqlglot).
  answers.pkl  {("tpch"|"tpcds", duckdb sql text): pickled DataFrame} for every
               fixture query (and its optimized form) that duckdb can run, keyed
               by the *text* sqlglot transpiles it to at the pinned commit.
Verification children look answers up by the text the patched sqlglot produces,
so an edit that changes the transpiled SQL falls back to running duckdb.
"""

import os
import pickle
import sys


def main() -> None:
    root, out = sys.argv[1], sys.argv[2]
    os.makedirs(out, exist_ok=True)
    sys.path.insert(0, root)
    os.chdir(root)
    os.environ["SKIP_INTEGRATION"] = "0"
    import tests.test_executor as te
    from sqlglot import transpile

    cls = te.TestExecutor
    cls.setUpClass()
    rows = {"tpch": {t: list(v.rows) for t, v in cls.tpch_tables.items()},
            "tpcds": {t: list(v.rows) for t, v in cls.tpcds_tables.items()}}
    with open(os.path.join(out, "rows.pkl"), "wb") as f:
        pickle.dump(rows, f, protocol=pickle.HIGHEST_PROTOCOL)
    answers = {}
    for kind, sqls, conn in (("tpch", cls.tpch_sqls, cls.tpch_conn), ("tpcds", cls.tpcds_sqls, cls.tpcds_conn)):
        for _, sql, optimized in sqls:
            for q in (sql, optimized):
                try:
                    text = transpile(q, write="duckdb")[0]
                except Exception:  # noqa: BLE001
                    continue
                if (kind, text) in answers:
                    continue
                try:
                    answers[(kind, text)] = pickle.dumps(conn.execute(text).fetchdf(), protocol=pickle.HIGHEST_PROTOCOL)
                except Exception:  # noqa: BLE001
                    pass
    with open(os.path.join(out, "answers.pkl"), "wb") as f:
        pickle.dump(answers, f, protocol=pickle.HIGHEST_PROTOCOL)
    cls.tearDownClass()
    print(f"tpc store: {sum(len(r) for d in rows.values() for r in d.values())} rows, {len(answers)} answers",
          file=sys.stderr)


if __name__ == "__main__":
    main()
