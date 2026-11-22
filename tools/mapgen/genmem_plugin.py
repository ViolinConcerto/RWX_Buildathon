"""pytest plugin (map-building only): per-dialect profile of test_generator_memory.

The test runs one independent sub-case per dialect (warm up, then measure the
memory retained by 1000 generator() calls). For each dialect sub-case this
records the sqlglot lines executed (outside module imports), the sqlglot classes
instantiated, and the CPU used. Lines/classes seen outside any sub-case are
'common'. Pinned commit only.

Env: GM_OUT=<dir>, SRC_ROOT=<sqlglot checkout>, GM_NOCOV=1 for a timing-only run.
"""

from __future__ import annotations

import contextlib
import json
import os
import resource
import sys
import unittest

import coverage

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from classkey import class_key  # noqa: E402

SRC_ROOT = os.path.realpath(os.environ.get("SRC_ROOT", os.getcwd()))
OUT = os.environ["GM_OUT"]
NOCOV = os.environ.get("GM_NOCOV") == "1"
_cov = coverage.Coverage(data_file=os.path.join(OUT, "gm.sqlite"), source=[os.path.join(SRC_ROOT, "sqlglot")])
_ctx = ["common"]
_cpu: dict[str, float] = {}
_last = [None, 0.0]
_classes: dict[str, set] = {}


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


def _install_instance_hooks():
    from sqlglot.dialects.dialect import Dialect
    from sqlglot.expressions.core import Expr
    from sqlglot.generator import Generator
    from sqlglot.parser import Parser
    from sqlglot.tokens import Tokenizer

    for base in (Expr, Generator, Parser, Tokenizer, Dialect):
        original = base.__init__

        def wrapper(self, *args, __original=original, **kwargs):
            _classes.setdefault(_ctx[0], set()).add(type(self))
            return __original(self, *args, **kwargs)

        base.__init__ = wrapper


def pytest_configure(config):
    _install_import_context()
    if not NOCOV:
        _cov.start()
    _switch("common")
    _install_instance_hooks()
    original = unittest.TestCase.subTest

    @contextlib.contextmanager
    def subTest(self, msg=unittest.case._subtest_msg_sentinel, **params):
        if self._testMethodName == "test_generator_memory" and "dialect" in params:
            d = params["dialect"]
            _switch(f"gm:{getattr(d, 'value', d)}")
            try:
                with original(self, msg, **params):
                    yield
            finally:
                _switch("common")
        else:
            with original(self, msg, **params):
                yield

    unittest.TestCase.subTest = subTest


def pytest_unconfigure(config):
    _switch("common")
    if not NOCOV:
        _cov.stop()
        _cov.save()
    with open(os.path.join(OUT, "gm_cpu_nocov.json" if NOCOV else "gm_cpu.json"), "w") as f:
        json.dump(_cpu, f)
    if not NOCOV:
        with open(os.path.join(OUT, "gm_classes.json"), "w") as f:
            json.dump({k: sorted({class_key(c) for c in v}) for k, v in _classes.items()}, f)
