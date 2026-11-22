"""pytest plugin used at map-building time (never at scoring time).

For every test it records, per phase (setup / call / teardown, plus collection
of each test module):
  * the sqlglot + tests lines executed (coverage.py dynamic contexts), and
  * the files opened / directories listed (audit hook), so fixture reads and
    source files read as data are known.

Process pools are replaced by an in-process executor that still round-trips
arguments and results through pickle, so code that only runs in pool workers
(and the serde code pickling triggers) is attributed to the test that caused it.

Env:  MAP_OUT=<dir>  SRC_ROOT=<sqlglot checkout root>
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import pickle
import sys

import coverage

SRC_ROOT = os.path.realpath(os.environ.get("SRC_ROOT", os.getcwd()))
OUT = os.environ["MAP_OUT"]

_cov = coverage.Coverage(
    data_file=os.path.join(OUT, "coverage.sqlite"),
    source=[os.path.join(SRC_ROOT, "sqlglot"), os.path.join(SRC_ROOT, "tests")],
    branch=False,
)
_context = ["init"]
_reads: dict[str, set[str]] = {}
_classes: dict[str, set[type]] = {}


def _install_instance_hooks() -> None:
    """Record, per context, the concrete classes of sqlglot objects created."""
    from sqlglot.dialects.dialect import Dialect
    from sqlglot.expressions.core import Expr
    from sqlglot.generator import Generator
    from sqlglot.parser import Parser
    from sqlglot.tokens import Tokenizer

    for base in (Expr, Generator, Parser, Tokenizer, Dialect):
        original = base.__init__

        def wrapper(self, *args, __original=original, **kwargs):
            bucket = _classes.get(_context[0])
            if bucket is None:
                bucket = _classes[_context[0]] = set()
            bucket.add(type(self))
            return __original(self, *args, **kwargs)

        wrapper.__wrapped__ = original
        base.__init__ = wrapper


def _set_context(name: str) -> None:
    _context[0] = name
    _cov.switch_context(name)


def _audit(event, args):
    if event == "open":
        path = args[0]
        if not isinstance(path, (str, bytes, os.PathLike)) or isinstance(path, int):
            return
        mode = args[1] if len(args) > 1 else "r"
        if mode is not None and any(c in str(mode) for c in "wax+"):
            return
    elif event in ("os.listdir", "os.scandir"):
        path = args[0] if args else "."
        if isinstance(path, int):
            return
    else:
        return
    try:
        path = os.fsdecode(path)
        full = os.path.realpath(path)
    except Exception:
        return
    if not full.startswith(SRC_ROOT + os.sep) or "__pycache__" in full:
        return
    rel = os.path.relpath(full, SRC_ROOT)
    if event != "open" or rel.endswith(".py"):
        # Ignore the import system reading module source / scanning sys.path.
        frame = sys._getframe(1)
        for _ in range(6):
            if frame is None:
                break
            if "importlib" in frame.f_code.co_filename:
                return
            frame = frame.f_back
    tag = "dir:" if event != "open" else ""
    _reads.setdefault(_context[0], set()).add(tag + rel)


class InlinePickleExecutor(concurrent.futures.Executor):
    """Runs submitted work immediately in-process, pickling like a real pool."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **_):
        self._initialized = False
        self._initializer = initializer
        self._initargs = initargs

    def submit(self, fn, /, *args, **kwargs):
        future = concurrent.futures.Future()
        try:
            if not self._initialized:
                self._initialized = True
                if self._initializer is not None:
                    init, iargs = pickle.loads(pickle.dumps((self._initializer, self._initargs)))
                    init(*iargs)
            fn2, args2, kwargs2 = pickle.loads(pickle.dumps((fn, args, kwargs)))
            result = fn2(*args2, **kwargs2)
            future.set_result(pickle.loads(pickle.dumps(result)))
        except BaseException as error:  # noqa: BLE001
            future.set_exception(error)
        return future

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass


def pytest_configure(config):
    if hasattr(config, "workerinput"):
        raise RuntimeError("collect_plugin must run without xdist")
    concurrent.futures.ProcessPoolExecutor = InlinePickleExecutor
    import concurrent.futures.process as cfp

    cfp.ProcessPoolExecutor = InlinePickleExecutor
    sys.addaudithook(_audit)
    _cov.start()
    _set_context("init")
    _install_instance_hooks()


def pytest_collectstart(collector):
    if collector.__class__.__name__ == "Module":
        _set_context("collect|" + collector.nodeid)


def pytest_collection_finish(session):
    _set_context("between")


import pytest  # noqa: E402


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    _set_context(item.nodeid + "|setup")
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    _set_context(item.nodeid + "|call")
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item):
    _set_context(item.nodeid + "|teardown")
    yield
    _set_context("between")


_outcomes: dict[str, str] = {}


def pytest_runtest_logreport(report):
    if report.failed:
        _outcomes[report.nodeid] = "failed"
    else:
        _outcomes.setdefault(report.nodeid, "passed")


def pytest_unconfigure(config):
    _cov.stop()
    _cov.save()
    with open(os.path.join(OUT, "reads.json"), "w") as f:
        json.dump({k: sorted(v) for k, v in _reads.items()}, f)
    with open(os.path.join(OUT, "outcomes.json"), "w") as f:
        json.dump(_outcomes, f)
    with open(os.path.join(OUT, "classes.json"), "w") as f:
        json.dump({k: sorted(f"{c.__module__}:{c.__qualname__}" for c in v) for k, v in _classes.items()}, f)
