"""pytest plugin (map-building only): per-fixture-entry coverage of data-driven tests.

Many tests loop over the entries of a fixture file (tests.helpers.load_sql_fixtures /
load_sql_fixture_pairs; TestOptimizer.check_file). This records, for every
(test, fixture file, entry index), the sqlglot lines executed while that entry was
processed, plus the test's remaining ("common") lines, and per-entry CPU. Entries
are processed (a) while the loader generator is suspended at that entry and
(b) inside the subTest whose title check_file derives from the entry.

Env: ENTRY_OUT=<dir>, SRC_ROOT=<sqlglot checkout>, ENTRY_NOCOV=1 for timing only.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import json
import os
import pickle
import resource
import sys
import unittest

import coverage

SRC_ROOT = os.path.realpath(os.environ.get("SRC_ROOT", os.getcwd()))
OUT = os.environ["ENTRY_OUT"]
NOCOV = os.environ.get("ENTRY_NOCOV") == "1"
_cov = coverage.Coverage(data_file=os.path.join(OUT, "entry.sqlite"), source=[os.path.join(SRC_ROOT, "sqlglot")])
_ctx = ["init"]
_test = [None]          # current test node id
_base = ["init"]        # context to return to after an entry
_titles: dict[str, tuple[str, int]] = {}   # check_file subTest title -> (file, index), current test
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


def _entry_ctx(fname: str, i: int) -> str:
    return f"{_test[0]}#{fname}#{i}"


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


def _install_import_context():
    import importlib._bootstrap_external as be

    stack = []

    def wrap(cls):
        original = cls.exec_module

        def exec_module(self, module, __original=original):
            name = getattr(module, "__name__", "")
            if not (name == "sqlglot" or name.startswith("sqlglot.")):
                return __original(self, module)
            stack.append(_ctx[0])
            _switch("import")
            try:
                return __original(self, module)
            finally:
                _switch(stack.pop())

        cls.exec_module = exec_module

    wrap(be.SourceFileLoader)
    wrap(be.SourcelessFileLoader)


def _wrap_loader(orig, pairs: bool):
    def loader(filename, *a, **kw):
        if _test[0] is None:
            yield from orig(filename, *a, **kw)
            return
        for i, item in enumerate(orig(filename, *a, **kw)):
            if pairs:
                meta, sql, _ = item
                _titles[meta.get("title") or f"{i + 1}, {sql}"] = (filename, i)
            _switch(_entry_ctx(filename, i))
            try:
                yield item
            finally:
                _switch(_base[0])

    loader.__wrapped__ = orig
    return loader


def pytest_configure(config):
    _install_import_context()
    import concurrent.futures.process as cfp

    concurrent.futures.ProcessPoolExecutor = InlinePickleExecutor
    cfp.ProcessPoolExecutor = InlinePickleExecutor
    if not NOCOV:
        _cov.start()
    _switch("init")
    original = unittest.TestCase.subTest

    @contextlib.contextmanager
    def subTest(self, msg=unittest.case._subtest_msg_sentinel, **params):
        key = msg if isinstance(msg, str) else None
        if key is not None and key.startswith("(execute) "):
            key = key[len("(execute) "):]
        hit = _titles.get(key) if key is not None else None
        if hit is None or _ctx[0] != _base[0]:
            with original(self, msg, **params):
                yield
            return
        _switch(_entry_ctx(*hit))
        try:
            with original(self, msg, **params):
                yield
        finally:
            _switch(_base[0])

    unittest.TestCase.subTest = subTest


def pytest_collection_finish(session):
    import tests.helpers as h

    pairs, plain = h.load_sql_fixture_pairs, h.load_sql_fixtures
    wp, wl = _wrap_loader(pairs, True), _wrap_loader(plain, False)
    h.load_sql_fixture_pairs, h.load_sql_fixtures = wp, wl
    for name, mod in list(sys.modules.items()):
        if name.startswith("tests.") and mod is not None:
            if getattr(mod, "load_sql_fixture_pairs", None) is pairs:
                mod.load_sql_fixture_pairs = wp
            if getattr(mod, "load_sql_fixtures", None) is plain:
                mod.load_sql_fixtures = wl
    _switch("between")


import pytest  # noqa: E402


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    _test[0] = item.nodeid
    _titles.clear()
    _base[0] = item.nodeid + "|setup"
    _switch(_base[0])
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    _base[0] = item.nodeid + "|call"
    _switch(_base[0])
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item):
    _base[0] = item.nodeid + "|teardown"
    _switch(_base[0])
    yield
    _test[0] = None
    _base[0] = "between"
    _switch("between")


_failed: list[str] = []


def pytest_runtest_logreport(report):
    if report.failed:
        _failed.append(report.nodeid)


def pytest_unconfigure(config):
    _switch("between")
    if not NOCOV:
        _cov.stop()
        _cov.save()
    with open(os.path.join(OUT, "entry_cpu_nocov.json" if NOCOV else "entry_cpu.json"), "w") as f:
        json.dump({"cpu": _cpu, "failed": _failed}, f)
