"""pytest plugin (map-building only): per-test relational profile of predicates.

Every single-operator comparison and every `and`/`or` in sqlglot is rewritten
(AST, line numbers kept) so the base test run records, per test, which
*relations* each site observed:

  Compare sites:  L (left < right), E (equal), G (greater) for builtin orderable
                  operands; e/n for == true/false; i/j for `is` true/false;
                  m/o for `in` true/false. Only relations that are free of side
                  effects are evaluated (builtin types, or the operator the
                  program itself evaluates).
  BoolOp sites:   the truthiness of each evaluated operand, in order, per
                  evaluation (e.g. "TF" = first truthy, second falsy, stopped).

This is relational/branch coverage of the pinned commit (like line coverage, a
property of the base run); it holds no patch information. Results are returned
unchanged and evaluation order/short-circuiting are preserved.

Env: PRED_OUT=<dir>, SRC_ROOT=<sqlglot checkout>
"""

from __future__ import annotations

import ast
import builtins
import concurrent.futures
import importlib.abc
import importlib.machinery
import importlib.util
import json
import os
import pickle
import sys

SRC_ROOT = os.path.realpath(os.environ.get("SRC_ROOT", os.getcwd()))
OUT = os.environ["PRED_OUT"]
ORDERABLE = (int, float, str, bytes, tuple)
SIMPLE = (int, float, str, bytes, tuple, frozenset, bool, type(None))

_sites: list[str] = []
_ctx = ["init"]
_obs: dict[str, dict[int, set]] = {}  # context -> site -> set of signatures
_stack: list[list] = []


def _bucket(site: int) -> set:
    d = _obs.get(_ctx[0])
    if d is None:
        d = _obs[_ctx[0]] = {}
    s = d.get(site)
    if s is None:
        s = d[site] = set()
    return s


def _cmp(site, left, right, op):
    # op: 0 <, 1 <=, 2 >, 3 >=, 4 ==, 5 !=, 6 is, 7 is not, 8 in, 9 not in
    sig = []
    lt, rt = type(left), type(right)
    builtin_pair = lt in SIMPLE and rt in SIMPLE
    if op <= 3 or (builtin_pair and lt is rt and lt in ORDERABLE):
        try:
            if isinstance(left, ORDERABLE) and lt is rt and lt in ORDERABLE:
                sig.append("L" if left < right else ("E" if left == right else "G"))
        except Exception:  # noqa: BLE001
            pass
    if op in (4, 5) or builtin_pair:
        try:
            sig.append("e" if left == right else "n")
        except Exception:  # noqa: BLE001
            pass
    sig.append("i" if left is right else "j")
    if op in (8, 9):
        try:
            sig.append("m" if left in right else "o")
        except Exception:  # noqa: BLE001
            pass
    _bucket(site).add("".join(sig))
    if op == 0:
        return left < right
    if op == 1:
        return left <= right
    if op == 2:
        return left > right
    if op == 3:
        return left >= right
    if op == 4:
        return left == right
    if op == 5:
        return left != right
    if op == 6:
        return left is right
    if op == 7:
        return left is not right
    if op == 8:
        return left in right
    return left not in right


def _bo_begin(site):
    _stack.append([site])
    return len(_stack) - 1


def _bo_val(token, value):
    if token < len(_stack):
        _stack[token].append("T" if value else "F")
    return value


def _bo_end(token, value):
    if token < len(_stack):
        # Records above ours belong to evaluations that raised: keep them, marked
        # incomplete ("!"), so no test looks like it never evaluated the site.
        for stale in _stack[token + 1:]:
            _bucket(stale[0]).add("".join(stale[1:]) + "!")
        rec = _stack[token]
        del _stack[token:]
        _bucket(rec[0]).add("".join(rec[1:]))
    return value


def _flush(new_ctx: str) -> None:
    for stale in _stack:
        _bucket(stale[0]).add("".join(stale[1:]) + "!")
    _stack.clear()
    _ctx[0] = new_ctx


builtins.__br_cmp__ = _cmp
builtins.__br_bob__ = _bo_begin
builtins.__br_bov__ = _bo_val
builtins.__br_boe__ = _bo_end
OPS = {ast.Lt: 0, ast.LtE: 1, ast.Gt: 2, ast.GtE: 3, ast.Eq: 4, ast.NotEq: 5, ast.Is: 6, ast.IsNot: 7,
       ast.In: 8, ast.NotIn: 9}
_tok = [0]


class _Rewriter(ast.NodeTransformer):
    def __init__(self, rel):
        self.rel = rel

    def _site(self, node, kind):
        _sites.append(f"{self.rel}:{node.lineno}:{node.col_offset}:{kind}")
        return len(_sites) - 1

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) != 1 or type(node.ops[0]) not in OPS:
            return node
        site = self._site(node, "c")
        call = ast.Call(func=ast.Name("__br_cmp__", ast.Load()),
                        args=[ast.Constant(site), node.left, node.comparators[0], ast.Constant(OPS[type(node.ops[0])])],
                        keywords=[])
        return ast.copy_location(call, node)

    def visit_BoolOp(self, node):
        self.generic_visit(node)
        site = self._site(node, "b")
        _tok[0] += 1
        tname = f"__br_t{_tok[0]}"
        # (__br_t := begin(site)) is avoided: walrus would leak into class scopes.
        # Instead begin() is evaluated as the first argument of end(); operands
        # read the token through a module-unique global slot set by begin.
        values = [
            ast.copy_location(ast.Call(func=ast.Name("__br_bov__", ast.Load()),
                                       args=[ast.Call(func=ast.Name("__br_tok__", ast.Load()), args=[], keywords=[]), v],
                                       keywords=[]), v)
            for v in node.values
        ]
        inner = ast.copy_location(ast.BoolOp(op=node.op, values=values), node)
        call = ast.Call(func=ast.Name("__br_boe__", ast.Load()),
                        args=[ast.Call(func=ast.Name("__br_bob__", ast.Load()), args=[ast.Constant(site)], keywords=[]),
                              inner],
                        keywords=[])
        del tname
        return ast.copy_location(call, node)


# Operands need the token of the innermost BoolOp being evaluated: that is the
# top of the stack when they run (nested BoolOps push/pop around themselves).
builtins.__br_tok__ = lambda: len(_stack) - 1


class _Loader(importlib.machinery.SourceFileLoader):
    def path_stats(self, path):
        raise OSError("no bytecode for instrumented modules")

    def source_to_code(self, data, path, *, _optimize=-1):
        rel = os.path.relpath(os.path.realpath(path), SRC_ROOT)
        tree = ast.parse(data, filename=path)
        tree = ast.fix_missing_locations(_Rewriter(rel).visit(tree))
        return compile(tree, path, "exec", dont_inherit=True)


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname != "sqlglot" and not fullname.startswith("sqlglot."):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path or [SRC_ROOT])
        if spec is None or not spec.origin or not spec.origin.endswith(".py"):
            return spec
        if not os.path.realpath(spec.origin).startswith(os.path.join(SRC_ROOT, "sqlglot")):
            return spec
        loader = _Loader(fullname, spec.origin)
        return importlib.util.spec_from_file_location(
            fullname, spec.origin, loader=loader, submodule_search_locations=spec.submodule_search_locations)


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
            _ctx[0] = "import"
            try:
                return __original(self, module)
            finally:
                _ctx[0] = stack.pop()

        cls.exec_module = exec_module

    wrap(be.SourceFileLoader)
    wrap(be.SourcelessFileLoader)
    wrap(_Loader)


def pytest_configure(config):
    sys.dont_write_bytecode = True
    import concurrent.futures.process as cfp

    concurrent.futures.ProcessPoolExecutor = InlinePickleExecutor
    cfp.ProcessPoolExecutor = InlinePickleExecutor
    sys.meta_path.insert(0, _Finder())
    _install_import_context()


def pytest_collectstart(collector):
    if collector.__class__.__name__ == "Module":
        _flush("collect|" + collector.nodeid)


def pytest_collection_finish(session):
    _flush("between")


import pytest  # noqa: E402


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    _flush(item.nodeid + "|setup")
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    _flush(item.nodeid + "|call")
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item):
    _flush(item.nodeid + "|teardown")
    yield
    _flush("between")


_outcomes: dict[str, str] = {}


def pytest_runtest_logreport(report):
    if report.failed:
        _outcomes[report.nodeid] = "failed"


def pytest_unconfigure(config):
    _flush("between")
    out = {ctx: {_sites[s]: sorted(sigs) for s, sigs in d.items()} for ctx, d in _obs.items()}
    with open(os.path.join(OUT, "pred.json"), "w") as f:
        json.dump({"obs": out, "failed": sorted(_outcomes)}, f)
