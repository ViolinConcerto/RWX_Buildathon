"""pytest plugin (map-building only): operand-type profile of identity/equality sites.

Every single-operator comparison using `is`, `is not`, `==` or `!=` in sqlglot is
rewritten (AST, line numbers kept) into a probe that records the pair of operand
types and returns the original operator's result. The profile is a property of
the pinned commit's test run, like coverage; it holds no patch information.

Env: CMP_OUT=<dir>, SRC_ROOT=<sqlglot checkout>
Output: <dir>/cmp_profile.json = {"sites": {"rel:line:col": [[ltype, rtype], ...]},
                                   "types": {type_key: descriptor}}
"""

from __future__ import annotations

import ast
import builtins
import concurrent.futures
import enum
import importlib.abc
import importlib.machinery
import importlib.util
import json
import os
import pickle
import sys

SRC_ROOT = os.path.realpath(os.environ.get("SRC_ROOT", os.getcwd()))
OUT = os.environ["CMP_OUT"]
OPS = {ast.Is: 0, ast.IsNot: 1, ast.Eq: 2, ast.NotEq: 3}

_sites: list[str] = []
_seen: list[set] = []


def _probe(site, left, right, op):
    _seen[site].add((type(left), type(right)))
    if op == 0:
        return left is right
    if op == 1:
        return left is not right
    if op == 2:
        return left == right
    return left != right


builtins.__br_cmp_probe__ = _probe


class _Rewriter(ast.NodeTransformer):
    def __init__(self, rel):
        self.rel = rel

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) != 1 or type(node.ops[0]) not in OPS:
            return node
        site = len(_sites)
        _sites.append(f"{self.rel}:{node.lineno}:{node.col_offset}")
        _seen.append(set())
        call = ast.Call(
            func=ast.Name(id="__br_cmp_probe__", ctx=ast.Load()),
            args=[ast.Constant(site), node.left, node.comparators[0], ast.Constant(OPS[type(node.ops[0])])],
            keywords=[],
        )
        return ast.copy_location(call, node)


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
            fullname, spec.origin, loader=loader, submodule_search_locations=spec.submodule_search_locations
        )


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
    sys.dont_write_bytecode = True
    sys.meta_path.insert(0, _Finder())
    import concurrent.futures.process as cfp

    concurrent.futures.ProcessPoolExecutor = InlinePickleExecutor
    cfp.ProcessPoolExecutor = InlinePickleExecutor


def _key(t: type) -> str:
    return f"{t.__module__}:{t.__qualname__}"


def describe(t: type) -> dict:
    """Equality semantics of a type, as needed to prove `is` <-> `==` equivalence."""
    def owner(name):
        b = next((b for b in t.__mro__ if name in b.__dict__), object)
        return b.__name__ if b.__module__ == "builtins" else _key(b)

    d = {
        "eq_identity": t.__eq__ is object.__eq__ and t.__ne__ is object.__ne__,
        "is_none": t is type(None),
        "is_bool": t is bool,
        "eq_owner": owner("__eq__"),
        "ne_owner": owner("__ne__"),
    }
    if isinstance(t, enum.EnumMeta) and issubclass(t, enum.Enum) and not issubclass(t, enum.Flag):
        members = list(t.__members__.values())
        mixin = None
        for base in t.__mro__[1:]:
            if base in (str, int):
                mixin = base.__name__
                break
        eq_owner = next((b for b in t.__mro__ if "__eq__" in b.__dict__), object)
        ne_owner = next((b for b in t.__mro__ if "__ne__" in b.__dict__), object)
        d["enum"] = {
            "mixin": mixin,
            "eq_owner": eq_owner.__name__ if eq_owner.__module__ == "builtins" else _key(eq_owner),
            "ne_owner": ne_owner.__name__ if ne_owner.__module__ == "builtins" else _key(ne_owner),
            "unique_values": len({repr(m.value) for m in members}) == len({id(m) for m in members}),
        }
    return d


def pytest_unconfigure(config):
    sites = {}
    types: dict[str, dict] = {}
    for i, seen in enumerate(_seen):
        if not seen:
            continue
        pairs = []
        for lt, rt in seen:
            for t in (lt, rt):
                k = _key(t)
                if k not in types:
                    try:
                        types[k] = describe(t)
                    except Exception:  # noqa: BLE001
                        types[k] = {"eq_identity": False, "is_none": False, "is_bool": False}
            pairs.append([_key(lt), _key(rt)])
        merged = {tuple(x) for x in sites.get(_sites[i], [])} | {tuple(x) for x in pairs}
        sites[_sites[i]] = sorted(list(x) for x in merged)
    with open(os.path.join(OUT, "cmp_profile.json"), "w") as f:
        json.dump({"sites": sites, "types": types, "instrumented": len(_sites)}, f)
