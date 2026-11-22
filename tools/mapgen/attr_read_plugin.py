"""pytest plugin (map-building only): which class attributes does each test read?

After every sqlglot module is imported, each data attribute in a sqlglot class's
__dict__ is replaced by a recording (non-data) descriptor that returns the same
object. A read is attributed to the binding that defines the value (the class in
the MRO where the name is found), which is what an edit to that binding changes.
Methods, properties and other descriptors are left alone (line coverage covers them).

Env: ATTR_OUT=<dir>, SRC_ROOT=<sqlglot checkout>
Output: attr_reads.json = {"reads": {context: [binding]}, "tracked": [binding], "failed": [...]}
"""

from __future__ import annotations

import concurrent.futures
import enum
import importlib
import json
import os
import pickle
import pkgutil
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from classkey import class_key  # noqa: E402

OUT = os.environ["ATTR_OUT"]
IGNORED = {"__module__", "__dict__", "__weakref__", "__doc__", "__annotations__", "__firstlineno__",
           "__static_attributes__", "__qualname__", "__orig_bases__", "__parameters__", "__slots__",
           "__abstractmethods__", "_abc_impl", "__match_args__", "__type_params__", "__classcell__"}
_ctx = ["init"]
_reads: dict[str, set] = {}
_tracked: list[str] = []


class Rec:
    __slots__ = ("v", "k")

    def __init__(self, v, k):
        self.v = v
        self.k = k

    def __get__(self, obj, owner=None):
        if _capture[0] is not None:
            _capture[0].append(self.k)  # read while building a TokenizerCore: attributed on field use
            return self.v
        s = _reads.get(_ctx[0])
        if s is None:
            s = _reads[_ctx[0]] = set()
        s.add(self.k)
        return self.v


_capture: list = [None]
_core_fields: dict[int, dict[str, list[str]]] = {}   # id(core) -> field -> class bindings it was built from


def _note(keys) -> None:
    s = _reads.get(_ctx[0])
    if s is None:
        s = _reads[_ctx[0]] = set()
    s.update(keys)


class RecSlot:
    """Wraps a TokenizerCore slot: reading the field counts as reading the class
    attributes its value was taken from when the core was built."""

    __slots__ = ("orig", "name")

    def __init__(self, orig, name):
        self.orig = orig
        self.name = name

    def __get__(self, obj, owner=None):
        if obj is None:
            return self
        keys = _core_fields.get(id(obj), {}).get(self.name)
        if keys:
            _note(keys)
        return self.orig.__get__(obj, type(obj))

    def __set__(self, obj, value):
        self.orig.__set__(obj, value)

    def __delete__(self, obj):
        self.orig.__delete__(obj)


def _wrap_tokenizer_core() -> None:
    """Tokenizer._init_core copies ~35 class attributes into a TokenizerCore; the
    values only matter where the core reads the corresponding field."""
    import ast
    import inspect
    import textwrap

    from sqlglot import tokenizer_core, tokens

    core_cls = tokenizer_core.TokenizerCore
    call = next(n for n in ast.walk(ast.parse(textwrap.dedent(inspect.getsource(tokens.Tokenizer._init_core))))
                if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "TokenizerCore")
    kw_attrs = {kw.arg: {n.attr for n in ast.walk(kw.value) if isinstance(n, ast.Attribute)} - {"dialect"}
                for kw in call.keywords}
    init = ast.parse(textwrap.dedent(inspect.getsource(core_cls.__init__))).body[0]
    uses: dict[str, int] = {}
    for n in ast.walk(init):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
            uses[n.id] = uses.get(n.id, 0) + 1
    simple: dict[str, str] = {}   # constructor parameter -> slot it is stored in, when that is all it does
    for stmt in init.body:
        if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Attribute)
                and isinstance(stmt.targets[0].value, ast.Name) and stmt.targets[0].value.id == "self"
                and isinstance(stmt.value, ast.Name) and uses.get(stmt.value.id) == 1
                and stmt.value.id in kw_attrs and stmt.targets[0].attr in core_cls.__dict__):
            simple[stmt.value.id] = stmt.targets[0].attr
    original = tokens.Tokenizer._init_core

    def _init_core(self):
        prev = _capture[0]
        _capture[0] = cap = []
        try:
            core = original(self)
        finally:
            _capture[0] = prev
        by_attr: dict[str, list[str]] = {}
        for key in cap:
            by_attr.setdefault(key.rsplit(".", 1)[1], []).append(key)
        fields: dict[str, list[str]] = {}
        deferred: set[str] = set()
        for param, attrs in kw_attrs.items():
            if param in simple:
                keys = [k for a in attrs for k in by_attr.get(a, ())]
                fields[simple[param]] = keys
                deferred.update(keys)
        _note([k for k in cap if k not in deferred])  # anything not provably a plain copy: read now
        _core_fields[id(core)] = fields
        return core

    tokens.Tokenizer._init_core = _init_core
    for slot in set(simple.values()):
        type.__setattr__(core_cls, slot, RecSlot(core_cls.__dict__[slot], slot))
    print(f"attr_read_plugin: TokenizerCore pass-through fields: {len(simple)} of {len(kw_attrs)}", file=sys.stderr)


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


def _wrap_all() -> None:
    import sqlglot

    for mod in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        if not mod.name.endswith("__main__"):
            importlib.import_module(mod.name)
    seen, stack, classes = set(), [object], []
    while stack:
        c = stack.pop()
        try:
            subs = type.__subclasses__(c)
        except TypeError:
            continue
        for s in subs:
            if id(s) not in seen:
                seen.add(id(s))
                stack.append(s)
                if (getattr(s, "__module__", "") or "").startswith("sqlglot"):
                    classes.append(s)
    for c in classes:
        if isinstance(c, enum.EnumMeta) or issubclass(c, (dict, tuple, BaseException)) and c.__module__ == "sqlglot._typing":
            continue
        if isinstance(c, enum.EnumMeta):
            continue
        key = class_key(c)
        for name, val in list(vars(c).items()):
            if name in IGNORED or (name.startswith("__") and name.endswith("__")):
                continue
            if isinstance(val, (types.FunctionType, classmethod, staticmethod, property, type)) or callable(val):
                continue
            if hasattr(type(val), "__get__") or hasattr(type(val), "__set__"):
                continue
            try:
                type.__setattr__(c, name, Rec(val, f"{key}.{name}"))
            except (TypeError, AttributeError):
                continue
            _tracked.append(f"{key}.{name}")


def pytest_configure(config):
    import concurrent.futures.process as cfp

    concurrent.futures.ProcessPoolExecutor = InlinePickleExecutor
    cfp.ProcessPoolExecutor = InlinePickleExecutor
    # Wrap before collection so that reads made while test modules are imported count too.
    _ctx[0] = "wrap"
    _wrap_all()
    _wrap_tokenizer_core()
    _ctx[0] = "init"


def pytest_collectstart(collector):
    if collector.__class__.__name__ == "Module":
        _ctx[0] = "collect|" + collector.nodeid


def pytest_collection_finish(session):
    _ctx[0] = "between"


import pytest  # noqa: E402


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    _ctx[0] = item.nodeid + "|setup"
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    _ctx[0] = item.nodeid + "|call"
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item):
    _ctx[0] = item.nodeid + "|teardown"
    yield
    _ctx[0] = "between"


_failed: list[str] = []


def pytest_runtest_logreport(report):
    if report.failed:
        _failed.append(report.nodeid)


def pytest_unconfigure(config):
    with open(os.path.join(OUT, "attr_reads.json"), "w") as f:
        json.dump({"reads": {k: sorted(v) for k, v in _reads.items()}, "tracked": sorted(_tracked),
                   "failed": sorted(set(_failed))}, f)
