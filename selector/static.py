"""Static candidate selection: patch -> tests that could fail.

Uses the dependency map built from the pinned sqlglot commit (maps/depmap.json.gz)
and a structural diff of each changed Python file. Every rule errs toward
including tests; anything not understood falls back to the whole suite.
"""

from __future__ import annotations

import ast
import gzip
import json
import os
import re
from dataclasses import dataclass, field

from patchlib import FileChange, PatchError, apply_change, parse_patch
from pydiff import Broad, ClassHeader, Definition, Runtime, diff_python


@dataclass
class Analysis:
    commit: str
    candidates: set[int] = field(default_factory=set)
    everything: bool = False  # could not bound the blast radius
    reasons: list[str] = field(default_factory=list)
    new_files: dict[str, bytes | None] = field(default_factory=dict)  # path -> patched content (None = deleted)
    python_changed: bool = False  # sqlglot/ or tests/ python changed (needs fresh import to verify)
    syntax_error: bool = False
    # Modules whose definition-time code changed: the caller must check they
    # still import (a removed name, a failing metaclass... breaks every importer).
    import_check: set[str] = field(default_factory=set)


class DepMap:
    def __init__(self, path: str, tests: list[dict]):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            raw = json.load(f)
        id2num = {t["id"]: t["number"] for t in tests}
        self.remap = [id2num.get(i) for i in raw["tests"]]
        self.unmapped = set(id2num.values()) - {n for n in self.remap if n is not None}
        self.sets = raw["sets"]
        self._set_cache: dict[int, frozenset[int]] = {}
        self.lines = raw["lines"]
        self.import_lines = {k: set(v) for k, v in raw["import_lines"].items()}
        self.executed_lines = raw["executed_lines"]
        self.reads = raw["reads"]
        self.classes = raw["classes"]
        self.subclasses = raw["subclasses"]
        self.idents = raw["idents"]
        self.ident_import = set(raw["ident_import"])
        self.class_refs = raw.get("class_refs", {})
        self.module_refs = raw.get("module_refs", {})

    def tests_of(self, sid: int | None) -> frozenset[int]:
        if sid is None:
            return frozenset()
        got = self._set_cache.get(sid)
        if got is None:
            got = frozenset(self.remap[i] for i in self.sets[sid] if self.remap[i] is not None)
            self._set_cache[sid] = got
        return got

    def line_tests(self, rel: str, lines) -> set[int]:
        m = self.lines.get(rel, {})
        out: set[int] = set()
        for ln in lines:
            out |= self.tests_of(m.get(str(ln)))
        return out

    def file_tests(self, rel: str) -> set[int]:
        out: set[int] = set()
        for sid in self.lines.get(rel, {}).values():
            out |= self.tests_of(sid)
        return out

    def readers(self, rel: str) -> set[int]:
        return set(self.tests_of(self.reads.get(rel)))

    def dir_readers(self, reldir: str) -> set[int]:
        return set(self.tests_of(self.reads.get("dir:" + reldir)))

    def class_users(self, key: str) -> set[int]:
        out = set(self.tests_of(self.classes.get(key)))
        for sub in self.subclasses.get(key, ()):
            out |= self.tests_of(self.classes.get(sub))
        return out

    def ident_readers(self, name: str) -> set[int]:
        return set(self.tests_of(self.idents.get(name)))


def module_name(rel: str) -> str:
    mod = rel[:-3].replace("/", ".")
    return mod[: -len(".__init__")] if mod.endswith(".__init__") else mod


class StaticSelector:
    def __init__(self, depmap: DepMap, src_root: str, tests: list[dict]):
        self.map = depmap
        self.src = src_root
        self.tests = tests
        self.all = {t["number"] for t in tests}
        self.by_file: dict[str, set[int]] = {}
        for t in tests:
            self.by_file.setdefault(t["file"], set()).add(t["number"])
        self.by_class: dict[tuple[str, str], set[int]] = {}
        for t in tests:
            parts = t["id"].split("::")
            if len(parts) >= 3:
                self.by_class.setdefault((t["file"], parts[1]), set()).add(t["number"])
        self.by_method: dict[tuple[str, str, str], int] = {}
        for t in tests:
            parts = t["id"].split("::")
            if len(parts) >= 3:
                self.by_method[(t["file"], parts[1], parts[2].split("[")[0])] = t["number"]

    # ------------------------------------------------------------------
    def analyze(self, commit: str, patch_text: str) -> Analysis:
        a = Analysis(commit)
        try:
            changes = parse_patch(patch_text)
        except Exception as e:  # noqa: BLE001
            a.everything = True
            a.reasons.append(f"unparsable patch: {e}")
            return a
        if not changes and patch_text.strip():
            a.everything = True
            a.reasons.append("no file changes parsed")
        for ch in changes:
            try:
                self._file_change(ch, a)
            except Exception as e:  # noqa: BLE001
                a.everything = True
                a.reasons.append(f"{ch.path}: {type(e).__name__}: {e}")
        if a.everything:
            a.candidates = set(self.all)
        else:
            a.candidates |= self.map.unmapped & a.candidates  # no-op, keeps type
        return a

    def _read_old(self, rel: str) -> bytes | None:
        try:
            with open(os.path.join(self.src, rel), "rb") as f:
                return f.read()
        except FileNotFoundError:
            return None

    def _file_change(self, ch: FileChange, a: Analysis) -> None:
        old_rel, new_rel = ch.old_path, ch.new_path
        old = self._read_old(old_rel) if old_rel else None
        if old_rel and old is None:
            raise PatchError(f"old file missing: {old_rel}")
        if ch.binary:
            new = None
            a.reasons.append(f"binary {ch.path}")
            a.everything = True
            return
        new_text = apply_change(ch, old.decode("utf-8") if old is not None else None)
        new = new_text.encode("utf-8") if new_text is not None else None
        if old_rel and new_rel and old_rel != new_rel:
            a.new_files[old_rel] = None
            a.new_files[new_rel] = new
        else:
            a.new_files[new_rel or old_rel] = new
        mode_change = ch.old_mode != ch.new_mode and ch.old_mode and ch.new_mode

        for rel in ch.touched_paths():
            # file contents read as data by tests (fixtures, sources parsed by test_typing)
            a.candidates |= self.map.readers(rel)

        if (old_rel is None or new_rel is None or old_rel != new_rel) and not self._is_inert_path(ch.path):
            # Files appearing/disappearing: directory listings + importers.
            for rel in ch.touched_paths():
                a.candidates |= self.map.dir_readers(os.path.dirname(rel))
            if ch.path.endswith(".py") and (ch.path.startswith("sqlglot/") or ch.path.startswith("tests/")):
                if old_rel is not None:
                    # deleted / renamed module: everything that imports it may break
                    a.reasons.append(f"module removed/renamed {old_rel}")
                    a.everything = True
                    return
                # new module: only matters if something imports it (another change) or discovery
                a.reasons.append(f"new module {new_rel}")
                a.python_changed = True
            return

        rel = new_rel
        if rel.endswith(".py") and rel.startswith("sqlglot/"):
            a.python_changed = True
            self._sqlglot_py(rel, old, new, a)
        elif rel.endswith(".py") and rel.startswith("tests/"):
            a.python_changed = True
            self._test_py(rel, old, new, a)
        elif rel.endswith(".py"):
            a.reasons.append(f"other python {rel}")  # setup.py, benchmarks, ... readers only
        else:
            a.reasons.append(f"data {rel}")
        if mode_change:
            a.reasons.append(f"mode change {rel}")

    @staticmethod
    def _is_inert_path(path: str) -> bool:
        return not (path.startswith("sqlglot/") or path.startswith("tests/"))

    # ------------------------------------------------------------------
    def _sqlglot_py(self, rel: str, old: bytes, new: bytes, a: Analysis) -> None:
        res = diff_python(old, new)
        if res.syntax_error:
            a.syntax_error = True
            a.reasons.append(f"syntax error in {rel}")
            a.everything = True
            return
        mod = module_name(rel)
        imp = self.map.import_lines.get(rel, set())
        if not imp and rel not in self.map.executed_lines:
            # Never imported anywhere in the suite (e.g. __main__): only
            # file-as-data readers (already added) can observe it.
            a.reasons.append(f"{rel}: module never imported by the suite")
            return
        self._old_src = old
        for fact in res.facts:
            if isinstance(fact, Broad):
                a.everything = True
                a.reasons.append(f"{rel}: {fact.reason}")
                return
            if isinstance(fact, Runtime):
                tests = self.map.line_tests(rel, fact.lines)
                a.candidates |= tests
                if fact.lines & imp:
                    # Code that runs at import time (class creation helpers, metaclasses).
                    names = self._enclosing_functions(old, fact.lines)
                    a.reasons.append(f"{rel}:{min(fact.lines)} import-time code in {sorted(names)}")
                    a.import_check.add(mod)
                    self._definition(mod, rel, (), names, a, via_import=True)
                else:
                    a.reasons.append(f"{rel}:{min(fact.lines)} runtime[{fact.note}] -> {len(tests)}")
            elif isinstance(fact, ClassHeader):
                a.import_check.add(mod)
                key = f"{mod}:{'.'.join(fact.scope)}"
                users = self.map.class_users(key)
                a.candidates |= users
                a.reasons.append(f"{rel} class header {key} -> {len(users)}")
                self._definition(mod, rel, fact.scope[:-1], {fact.scope[-1]}, a)
            elif isinstance(fact, Definition):
                self._definition(mod, rel, fact.scope, fact.names, a)

    def _enclosing_functions(self, src: bytes, lines: set[int]) -> set[str]:
        tree = ast.parse(src)
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if any(node.lineno <= ln <= (node.end_lineno or node.lineno) for ln in lines):
                    names.add(node.name)
        return names

    def _definition(self, mod: str, rel: str, scope, names: set[str], a: Analysis, via_import=False, depth=0) -> None:
        """A binding changed at definition time. Find who can observe it."""
        a.import_check.add(mod)
        names = {n for n in names if n}
        if not names:
            a.everything = True
            a.reasons.append(f"{rel}: definition change with no names")
            return
        cand: set[int] = set()
        classes_hit: set[str] = set()
        if scope:
            classes_hit.add(f"{mod}:{'.'.join(scope)}")
        # Classes whose bodies (definition-time code) mention a changed name
        # derive state from it at import: their users can observe the change.
        for n in names:
            for key in self.map.class_refs.get(n, ()):
                if not scope or key != f"{mod}:{'.'.join(scope)}":
                    if not scope or self._related(key, f"{mod}:{'.'.join(scope)}"):
                        classes_hit.add(key)
        users: set[int] = set()
        for key in classes_hit:
            users |= self.map.class_users(key)
        readers: set[int] = set()
        for n in names:
            readers |= self.map.ident_readers(n)
        import_read = any(n in self.map.ident_import for n in names)

        if scope:
            key = f"{mod}:{'.'.join(scope)}"
            own_users = self.map.class_users(key)
            if not own_users and key not in self.map.classes and key not in self.map.subclasses:
                # Never instantiated (enum, mixin, namespace...): fall back to readers / module users.
                cand = readers | self.map.file_tests(rel)
                if import_read or not readers:
                    cand |= users
                    if not cand:
                        a.everything = True
                        a.reasons.append(f"{key}.{sorted(names)}: unused class, no readers")
                        return
                a.reasons.append(f"{key}.{sorted(names)} (no instances) -> {len(cand)}")
            elif import_read or not readers or via_import:
                cand = users
                a.reasons.append(f"{key}.{sorted(names)} users(import_read={import_read}) -> {len(cand)}")
            else:
                cand = users & readers
                a.reasons.append(f"{key}.{sorted(names)} users&readers -> {len(cand)}")
        else:
            # Module-level binding: functions/classes defined here, constants, imports.
            cand = readers | users
            derived: set[str] = set()
            for n in names:
                for ref in self.map.module_refs.get(n, ()):
                    m2, b = ref.split(":", 1)
                    if m2 == mod:
                        derived.add(b)
            derived -= names
            if derived and depth < 3:
                self._definition(mod, rel, (), derived, a, via_import, depth + 1)
            # Module-level functions/classes whose definition-time code (defaults,
            # decorators, bases) mentions a changed name: their executors observe it.
            spans = self._def_spans(rel)
            for b in derived | names:
                for lo, hi in spans.get(b, ()):
                    cand |= self.map.line_tests(rel, range(lo, hi + 1))
            if import_read and not users:
                a.everything = True
                a.reasons.append(f"{mod}.{sorted(names)}: read at import, no class users")
                return
            a.reasons.append(f"{mod}.{sorted(names)} module-level -> {len(cand)} (derived {sorted(derived)[:5]})")
        a.candidates |= cand

    def _def_spans(self, rel: str) -> dict[str, list[tuple[int, int]]]:
        """Module-level def/class name -> body line spans in the pinned source."""
        cache = getattr(self, "_span_cache", None)
        if cache is None:
            cache = self._span_cache = {}
        if rel not in cache:
            out: dict[str, list[tuple[int, int]]] = {}
            src = self._read_old(rel)
            try:
                tree = ast.parse(src) if src is not None else None
            except SyntaxError:
                tree = None
            for node in (tree.body if tree else []):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    out.setdefault(node.name, []).append((node.body[0].lineno, node.end_lineno or node.lineno))
            cache[rel] = out
        return cache[rel]

    def _related(self, key: str, defining: str) -> bool:
        """Does class `key`'s body mention the defining class by name?"""
        cls_name = defining.rsplit(":", 1)[1].split(".")[-1]
        return key in self.map.class_refs.get(cls_name, ())

    # ------------------------------------------------------------------
    def _test_py(self, rel: str, old: bytes, new: bytes, a: Analysis) -> None:
        try:
            old_tree, new_tree = ast.parse(old), ast.parse(new)
            compile(new, rel, "exec", dont_inherit=True)
        except SyntaxError:
            # The test module (and its importers) cannot be imported.
            a.candidates |= self.by_file.get(rel, set()) | self._test_importers(rel)
            a.reasons.append(f"syntax error in test file {rel}")
            return
        res = diff_python(old, new)
        old_classes = {n.name: n for n in old_tree.body if isinstance(n, ast.ClassDef)}
        for fact in res.facts:
            if isinstance(fact, Runtime) and fact.lines:
                # Which test method / helper contains these lines?
                hit = False
                for cname, cnode in old_classes.items():
                    for m in cnode.body:
                        if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.lineno <= min(fact.lines) <= (m.end_lineno or m.lineno):
                            hit = True
                            num = self.by_method.get((rel, cname, m.name))
                            if num is not None and m.name.startswith("test"):
                                a.candidates.add(num)
                            else:
                                # helper / setUp: whole class and subclasses, plus callers
                                a.candidates |= self._class_and_subclasses(rel, cname)
                            a.candidates |= self.map.line_tests(rel, fact.lines)
                if not hit:
                    a.candidates |= self.map.line_tests(rel, fact.lines) | self.by_file.get(rel, set())
                    a.candidates |= self._test_importers(rel)
            else:
                # module / class level change in a test file
                scope = getattr(fact, "scope", ())
                if scope and scope[0] in old_classes:
                    a.candidates |= self._class_and_subclasses(rel, scope[0])
                else:
                    a.candidates |= self.by_file.get(rel, set()) | self._test_importers(rel)
        a.reasons.append(f"test file {rel}")

    def _test_importers(self, rel: str) -> set[int]:
        """Tests in files that import the given test-support module."""
        stem = os.path.splitext(os.path.basename(rel))[0]
        out: set[int] = set()
        pat = re.compile(r"\b" + re.escape(stem) + r"\b")
        for f, nums in self.by_file.items():
            if f == rel:
                continue
            src = self._read_old(f)
            if src is not None and pat.search(src.decode("utf-8", "replace")):
                out |= nums
        return out

    def _class_and_subclasses(self, rel: str, cname: str) -> set[int]:
        out = set(self.by_class.get((rel, cname), set()))
        pat = re.compile(r"class\s+\w+\s*\([^)]*\b" + re.escape(cname) + r"\b")
        for f, nums in self.by_file.items():
            src = self._read_old(f)
            if src is not None and pat.search(src.decode("utf-8", "replace")):
                for (ff, c), cn in self.by_class.items():
                    if ff == f:
                        out |= cn
        return out
