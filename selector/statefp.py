"""Fingerprint of sqlglot's import-time state: what did a definition-time edit really change?

After importing every sqlglot module, each module global and each class attribute
gets a digest of its value (containers recursively; classes and plain functions by
reference; lambdas/closures by bytecode + captured values, without line numbers).
Comparing a patched tree's fingerprint with the pinned commit's (built into the
image) yields the exact set of (class, attribute) / (module, global) bindings whose
value differs -- including state derived by metaclasses and class-creation helpers.

usage (build): PYTHONPATH=<sqlglot> python statefp.py <sqlglot root> <out.json>
"""

from __future__ import annotations

import enum
import functools
import hashlib
import importlib
import json
import logging
import os
import pkgutil
import re
import sys
import threading
import types

IGNORED = {"__module__", "__dict__", "__weakref__", "__doc__", "__annotations__", "__firstlineno__",
           "__static_attributes__", "__qualname__", "__orig_bases__", "__parameters__", "__slots__",
           "__abstractmethods__", "_abc_impl", "__match_args__", "__type_params__", "__annotate__",
           "__annotate_func__", "__annotations_cache__", "__classcell__"}
SKIP_ATTRS = {"_hash"}  # per-process cached hash() values


def class_key(c, _depth=0) -> str:
    """Same naming as tools/mapgen/classkey.py (kept in sync)."""
    mod, q = c.__module__, c.__qualname__
    obj = sys.modules.get(mod)
    for part in q.split("."):
        obj = getattr(obj, part, None) if obj is not None else None
    if obj is c or _depth > 8:
        return f"{mod}:{q}"
    return f"dyn:{q}<" + ",".join(class_key(b, _depth + 1) for b in c.__bases__) + ">"


class _Walker:
    def __init__(self, ordered: bool = True):
        self.ordered = ordered  # False ("bag"): dict/list/tuple digests ignore order
        self.memo: dict[int, str] = {}
        self.keep: list = []          # keep visited objects alive so ids stay unique
        self.busy: set[int] = set()
        self.ckey: dict[int, str] = {}

    def h(self, *parts) -> str:
        return hashlib.blake2b("\x1f".join(parts).encode("utf-8", "surrogatepass"), digest_size=8).hexdigest()

    def cls(self, c) -> str:
        k = self.ckey.get(id(c))
        if k is None:
            try:
                k = class_key(c)
            except Exception:  # noqa: BLE001
                k = f"{getattr(c, '__module__', '?')}:{getattr(c, '__qualname__', '?')}"
            self.ckey[id(c)] = k
        return k

    def code(self, co) -> str:
        consts = []
        for c in co.co_consts:
            consts.append(self.code(c) if isinstance(c, types.CodeType) else self.d(c))
        return self.h("code", co.co_code.hex(), ",".join(consts), ",".join(co.co_names), ",".join(co.co_varnames),
                      ",".join(co.co_freevars), ",".join(co.co_cellvars), str(co.co_argcount),
                      str(co.co_kwonlyargcount), str(co.co_flags & 0x0F))

    def func(self, f) -> str:
        mod = getattr(f, "__module__", None) or ""
        q = getattr(f, "__qualname__", "")
        defaults = self.d(f.__defaults__) + self.d(f.__kwdefaults__)
        if mod.startswith("sqlglot") and "<lambda>" not in q and "<locals>" not in q:
            return self.h("F", mod, q, defaults)  # plain def: by reference (its body is runtime code)
        cells = []
        for cell in f.__closure__ or ():
            try:
                cells.append(self.d(cell.cell_contents))
            except ValueError:
                cells.append("empty")
        return self.h("f", mod, q, self.code(f.__code__), ",".join(cells), defaults)

    def d(self, v) -> str:
        t = type(v)
        if v is None or t in (bool, int, float, complex, str, bytes):
            return self.h(t.__name__, repr(v))
        i = id(v)
        got = self.memo.get(i)
        if got is not None:
            return got
        if i in self.busy:
            return "cycle"
        self.busy.add(i)
        try:
            out = self._d(v, t)
        except Exception as e:  # noqa: BLE001
            out = self.h("err", t.__name__, type(e).__name__)
        self.busy.discard(i)
        self.memo[i] = out
        self.keep.append(v)
        return out

    def _d(self, v, t) -> str:
        if isinstance(v, type):
            return self.h("C", self.cls(v))
        if isinstance(v, enum.Enum):
            return self.h("E", self.cls(t), v.name, self.d(v.value))
        if t in (tuple, list):
            parts = [self.d(x) for x in v]
            if not self.ordered:
                parts.sort()
            return self.h(t.__name__, *parts)
        if t in (set, frozenset):
            return self.h(t.__name__, *sorted(self.d(x) for x in v))
        if isinstance(v, (dict, types.MappingProxyType)):
            pairs = [(self.d(k), self.d(x)) for k, x in v.items()]
            if not self.ordered:
                pairs.sort()
            return self.h("dict", *[p for pair in pairs for p in pair])
        if t is types.FunctionType:
            return self.func(v)
        if t is types.MethodType:
            return self.h("meth", self.d(v.__func__), self.d(v.__self__))
        if t in (classmethod, staticmethod):
            return self.h(t.__name__, self.d(v.__func__))
        if t is property:
            return self.h("prop", self.d(v.fget), self.d(v.fset), self.d(v.fdel))
        if t is functools.partial:
            return self.h("partial", self.d(v.func), self.d(v.args), self.d(v.keywords))
        if t is types.ModuleType:
            return self.h("M", v.__name__)
        if isinstance(v, (logging.Logger, logging.Handler, logging.Manager, threading.Thread)) or \
                t.__module__ in ("_thread", "threading", "_io", "weakref", "_weakref", "_weakrefset"):
            # process-level plumbing: its state reflects the process, not the code
            return self.h("P", self.cls(t), getattr(v, "name", "") if isinstance(v, logging.Logger) else "")
        if t is re.Pattern:
            return self.h("re", v.pattern if isinstance(v, str) else repr(v.pattern), str(v.flags))
        if t in (types.BuiltinFunctionType, types.BuiltinMethodType, types.WrapperDescriptorType,
                 types.MethodDescriptorType, types.MethodWrapperType, types.ClassMethodDescriptorType,
                 types.GetSetDescriptorType, types.MemberDescriptorType):
            return self.h("B", getattr(v, "__module__", None) or "", getattr(v, "__qualname__", repr(t)))
        if isinstance(v, (tuple, list)):  # namedtuples, list subclasses
            return self.h("seq", self.cls(t), *[self.d(x) for x in v])
        if isinstance(v, (set, frozenset)):
            return self.h("set", self.cls(t), *sorted(self.d(x) for x in v))
        attrs = []
        d = getattr(v, "__dict__", None)
        if isinstance(d, dict):
            for k in sorted(d):
                if k not in SKIP_ATTRS:
                    attrs.append(k)
                    attrs.append(self.d(d[k]))
        for klass in t.__mro__:
            for s in getattr(klass, "__slots__", ()) or ():
                if isinstance(s, str) and s not in SKIP_ATTRS and not s.startswith("__"):
                    try:
                        attrs.append(s)
                        attrs.append(self.d(getattr(v, s)))
                    except AttributeError:
                        attrs.append("unset")
        if attrs:
            return self.h("O", self.cls(t), *attrs)
        return self.h("X", self.cls(t))  # opaque


def import_all(root: str) -> dict[str, str]:
    """Import every sqlglot module; returns {module: error} for failures."""
    import sqlglot

    errors = {}
    for mod in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        if mod.name.endswith("__main__"):
            continue
        try:
            importlib.import_module(mod.name)
        except BaseException as e:  # noqa: BLE001
            errors[mod.name] = f"{type(e).__name__}: {e}"[:200]
    return errors


def bindings():
    """Yield (path, value) for every module global and class attribute of sqlglot; then owners."""
    return _collect()


def fingerprint(ordered: bool = True, only=None) -> tuple[dict[str, str], dict[str, list[str]]]:
    """({binding path: digest}, {component class key: [owning dialect class keys]})."""
    w = _Walker(ordered)
    out: dict[str, str] = {}
    items, owners = _collect()
    for path, val in items:
        if only is None or path in only:
            out[path] = w.d(val)
    return out, owners


def _collect():
    w = _Walker()
    out: list = []
    for name, m in sorted(sys.modules.items()):
        if m is None or not (name == "sqlglot" or name.startswith("sqlglot.")):
            continue
        for attr, val in vars(m).items():
            if (attr.startswith("__") and attr.endswith("__")) or isinstance(val, types.ModuleType):
                continue
            out.append((f"{name}::{attr}", val))
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
    owners: dict[str, list[str]] = {}
    for c in classes:
        key = w.cls(c)
        out.append((f"{key}.<bases>", c.__bases__))
        for attr, val in list(vars(c).items()):
            if attr in IGNORED:
                continue
            out.append((f"{key}.{attr}", val))
            if attr in ("tokenizer_class", "parser_class", "generator_class", "jsonpath_tokenizer_class") \
                    and isinstance(val, type):
                owners.setdefault(w.cls(val), []).append(key)
    return out, {k: sorted(set(v)) for k, v in owners.items()}


def diff(base: dict) -> list[str]:
    """Binding paths whose value differs from the pinned commit's (call after import_all).

    Bindings whose strict digest is not reproducible across processes (containers
    built by iterating sets of classes, whose order follows memory addresses) are
    compared by their order-insensitive digest; non-reproducible ones are ignored.
    """
    fp, _ = fingerprint(True)
    bag_paths = set(base["bag"])
    bag, _ = fingerprint(False, only=bag_paths) if bag_paths else ({}, None)
    unstable = set(base["unstable"])
    changed = []
    for path in set(fp) | set(base["fp"]) | bag_paths:
        if path in unstable:
            continue
        if path in bag_paths:
            if bag.get(path) != base["bag"][path]:
                changed.append(path)
        elif fp.get(path) != base["fp"].get(path):
            changed.append(path)
    values = dict(_collect()[0])
    out = []
    for path in sorted(changed):
        if path not in values:
            kind = "removed"
        else:
            v = values[path]
            kind = "code" if isinstance(v, (types.FunctionType, classmethod, staticmethod, property, type)) \
                or callable(v) else "data"
        out.append([path, kind])
    return out


def _one(root: str) -> None:
    # Perturb the memory layout so address-ordered containers come out differently.
    k = int(os.environ.get("BR_FP_PERTURB", "0"))
    ballast = [object() for _ in range(k * 7919)] + [bytearray(k * 1013 + 1) for _ in range(k * 31)]
    sys.path.insert(0, root)
    os.chdir(root)
    errors = import_all(root)
    fp, owners = fingerprint(True)
    bag, _ = fingerprint(False)
    json.dump({"fp": fp, "bag": bag, "owners": owners, "import_errors": errors, "ballast": len(ballast)}, sys.stdout)


def main() -> None:
    """Build the base fingerprint from several processes (different hash seeds and
    memory layouts): reproducible digests are compared strictly, order-only
    variation falls back to the order-insensitive digest, the rest is ignored."""
    import subprocess

    root, out_path = sys.argv[1], sys.argv[2]
    runs = []
    for k in range(6):
        env = dict(os.environ, PYTHONHASHSEED=str(11 * k + 1), BR_FP_PERTURB=str(k))
        res = subprocess.run([sys.executable, os.path.abspath(__file__), "--one", root], env=env,
                             capture_output=True, text=True, check=True)
        runs.append(json.loads(res.stdout))
    fp, bag, unstable = {}, {}, []
    for path in set().union(*[set(r["fp"]) for r in runs]):
        strict = {r["fp"].get(path) for r in runs}
        loose = {r["bag"].get(path) for r in runs}
        if len(strict) == 1:
            fp[path] = runs[0]["fp"][path]
        elif len(loose) == 1:
            bag[path] = runs[0]["bag"][path]
        else:
            unstable.append(path)
    with open(out_path, "w") as f:
        json.dump({"fp": fp, "bag": bag, "unstable": sorted(unstable), "owners": runs[0]["owners"],
                   "import_errors": runs[0]["import_errors"]}, f, separators=(",", ":"))
    print(f"state fingerprint: {len(fp)} strict, {len(bag)} order-insensitive {sorted(bag)[:6]}, "
          f"unstable {sorted(unstable)}; import errors {runs[0]['import_errors']}", file=sys.stderr)


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--one":
        _one(sys.argv[2])
    else:
        main()
