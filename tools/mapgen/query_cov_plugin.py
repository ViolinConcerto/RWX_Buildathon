"""pytest plugin (map-building only): per-query coverage of the TPC-H/TPC-DS executor tests.

test_execute_tpch / test_execute_tpcds run one independent sub-case per fixture
query (parse -> pickle -> execute -> compare). This records, for each query, the
sqlglot lines executed while handling it, plus 'common' lines (class setup, loop
overhead), and each query's CPU time. All from the pinned commit only.

Env: QCOV_OUT=<dir>, SRC_ROOT=<sqlglot checkout>
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import pickle
import resource
import sys

import coverage

SRC_ROOT = os.path.realpath(os.environ.get("SRC_ROOT", os.getcwd()))
OUT = os.environ["QCOV_OUT"]
_cov = coverage.Coverage(data_file=os.path.join(OUT, "qcov.sqlite"),
                         source=[os.path.join(SRC_ROOT, "sqlglot")], branch=False)
_ctx = ["common"]
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
    if not os.environ.get("QCOV_NOCOV"):
        _cov.switch_context(name)


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
    if not os.environ.get("QCOV_NOCOV"):
        _cov.start()
    _switch("common")


def pytest_collection_finish(session):
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
        if hit is not None:
            _switch(f"{hit[0]}:{hit[1]}")
        return orig_parse(sql, *a, **kw)

    te.parse_one = parse_one
    orig_helper = cls.subtestHelper

    def subtestHelper(self, i, table, tpch=True):
        _switch(f"{'tpch' if tpch else 'tpcds'}:{i}")
        try:
            return orig_helper(self, i, table, tpch=tpch)
        finally:
            _switch("common")

    cls.subtestHelper = subtestHelper


def pytest_unconfigure(config):
    _switch("common")
    if not os.environ.get("QCOV_NOCOV"):
        _cov.stop()
        _cov.save()
    with open(os.path.join(OUT, "qcpu_nocov.json" if os.environ.get("QCOV_NOCOV") else "qcpu.json"), "w") as f:
        json.dump(_cpu, f)
