"""pytest plugin (map-building only): per-query, per-phase coverage of test_execute_tpcds.

Phases of one TPC-DS sub-case:
  F (front)   parse_one(sql) in the test, ensure_schema / optimize / Plan inside execute()
  D (data)    everything else while the query runs in the pool worker: ensure_tables,
              PythonExecutor.execute(plan), the body of execute() and mp_execute, pickling
              (conservative: anything not explicitly front is data)
              except: the debug-log expression.sql(pretty=True) inside execute() -> F
  T (transpile) transpile(sql, write="duckdb") in cached_execute (stage 1 compares its text)
  C (compare) the rest of subtestHelper: duckdb answer and the DataFrame comparison
Records lines per context "tpcds:<i>:<F|D|C>" plus CPU per context (GM-style timing run
with QP_NOCOV=1). Pinned commit only.

Env: QP_OUT=<dir>, SRC_ROOT=<sqlglot checkout>, QP_NOCOV=1
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import pickle
import resource

import coverage

SRC_ROOT = os.path.realpath(os.environ.get("SRC_ROOT", os.getcwd()))
OUT = os.environ["QP_OUT"]
NOCOV = os.environ.get("QP_NOCOV") == "1"
_cov = coverage.Coverage(data_file=os.path.join(OUT, "qphase.sqlite"), source=[os.path.join(SRC_ROOT, "sqlglot")])
_ctx = ["common"]
_cur = [None]  # (suite, index) of the query being processed
_cpu: dict[str, float] = {}
_last = [None, 0.0]


def _now():
    r = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime + r.ru_stime


def _switch(name: str) -> None:
    t = _now()
    if _last[0] is not None:
        _cpu[_last[0]] = _cpu.get(_last[0], 0.0) + (t - _last[1])
    _last[0], _last[1] = name, t
    _ctx[0] = name
    if not NOCOV:
        _cov.switch_context(name)


def _phase(p: str) -> str:
    return f"{_cur[0][0]}:{_cur[0][1]}:{p}" if _cur[0] else "common"


class InlinePickleExecutor(concurrent.futures.Executor):
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **_):
        self._init = (initializer, initargs)
        self._done = initializer is None

    def submit(self, fn, /, *args, **kwargs):
        future = concurrent.futures.Future()
        try:
            if not self._done:
                self._done = True
                init, iargs = pickle.loads(pickle.dumps(self._init))
                init(*iargs)
            # pickling of the arguments / result belongs to the data phase
            if _cur[0]:
                _switch(_phase("D"))
            fn2, a2, k2 = pickle.loads(pickle.dumps((fn, args, kwargs)))
            future.set_result(pickle.loads(pickle.dumps(fn2(*a2, **k2))))
        except BaseException as error:  # noqa: BLE001
            future.set_exception(error)
        return future

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass


def pytest_configure(config):
    import concurrent.futures.process as cfp

    concurrent.futures.ProcessPoolExecutor = InlinePickleExecutor
    cfp.ProcessPoolExecutor = InlinePickleExecutor
    if not NOCOV:
        _cov.start()
    _switch("common")


def _front(fn):
    def wrapper(*a, **kw):
        prev = _ctx[0]
        if _cur[0]:
            _switch(_phase("F"))
        try:
            return fn(*a, **kw)
        finally:
            _switch(prev)

    return wrapper


def pytest_collection_finish(session):
    import sqlglot.executor as ex
    import tests.test_executor as te

    cls = te.TestExecutor
    index: dict[str, tuple[str, int]] = {}
    orig_parse = te.parse_one

    def parse_one(sql, *a, **kw):
        if not index:
            for suite, sqls in (("tpch", cls.tpch_sqls), ("tpcds", cls.tpcds_sqls)):
                for i, (_, s, _) in enumerate(sqls):
                    index.setdefault(s, (suite, i))
        hit = index.get(sql)
        _cur[0] = hit
        if hit is not None:
            _switch(_phase("F"))
        return orig_parse(sql, *a, **kw)

    te.parse_one = parse_one
    for name in ("optimize", "Plan", "ensure_schema"):
        setattr(ex, name, _front(getattr(ex, name)))
    import sys

    from sqlglot.expressions.core import Expr

    execute_code = ex.execute.__code__
    orig_sql = Expr.sql

    def sql(self, *a, **kw):
        if _cur[0] and sys._getframe(1).f_code is execute_code:
            prev = _ctx[0]
            _switch(_phase("F"))
            try:
                return orig_sql(self, *a, **kw)
            finally:
                _switch(prev)
        return orig_sql(self, *a, **kw)

    Expr.sql = sql
    orig_transpile = te.transpile

    def transpile(*a, **kw):
        if _cur[0] and _ctx[0].endswith(":C"):
            _switch(_phase("T"))
            try:
                return orig_transpile(*a, **kw)
            finally:
                _switch(_phase("C"))
        return orig_transpile(*a, **kw)

    te.transpile = transpile
    orig_helper = cls.subtestHelper

    def subtestHelper(self, i, table, tpch=True):
        _cur[0] = ("tpch" if tpch else "tpcds", i)
        _switch(_phase("C"))
        try:
            return orig_helper(self, i, table, tpch=tpch)
        finally:
            _cur[0] = None
            _switch("common")

    cls.subtestHelper = subtestHelper
    orig_mp = cls._mp_execute

    def _mp_execute(self, *a, **kw):
        try:
            return orig_mp(self, *a, **kw)
        finally:
            _cur[0] = None
            _switch("common")

    cls._mp_execute = _mp_execute


def pytest_unconfigure(config):
    _switch("common")
    if not NOCOV:
        _cov.stop()
        _cov.save()
    with open(os.path.join(OUT, "qphase_cpu_nocov.json" if NOCOV else "qphase_cpu.json"), "w") as f:
        json.dump(_cpu, f)
