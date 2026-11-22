"""Structural (AST) diff of two versions of a Python module.

Turns a source change into facts the selector can map to tests:

  Runtime(lines)                 old-file lines whose executors are affected
  Definition(scope, names)       a module/class-level binding changed (scope = class path)
  ClassHeader(scope)             class bases / decorators / keywords changed
  Broad(reason)                  cannot localise (syntax error, ...)

Formatting and comments do not change the AST, so a comment-only edit yields
no facts here (file-as-data readers are handled by the caller).
"""

from __future__ import annotations

import ast
import difflib
from dataclasses import dataclass, field


@dataclass
class Runtime:
    lines: set[int]
    note: str = ""
    old: object = None  # original statement (for equivalence proofs), when localised to one
    new: object = None


@dataclass
class Definition:
    scope: tuple[str, ...]  # enclosing class path; () = module level
    names: set[str]
    note: str = ""


@dataclass
class ClassHeader:
    scope: tuple[str, ...]  # the class path itself


@dataclass
class Broad:
    reason: str


@dataclass
class DiffResult:
    facts: list = field(default_factory=list)
    syntax_error: bool = False


def _dump(node) -> str:
    return ast.dump(node, annotate_fields=True, include_attributes=False)


def _span(node) -> set[int]:
    return set(range(node.lineno, (node.end_lineno or node.lineno) + 1))


BODY_FIELDS = ("body", "orelse", "finalbody")


def _header_span(node) -> set[int]:
    """Lines of a compound statement before its first body statement."""
    first = None
    for f in BODY_FIELDS + ("handlers", "cases"):
        v = getattr(node, f, None)
        if v:
            cand = v[0].lineno
            first = cand if first is None else min(first, cand)
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        start = min([d.lineno for d in node.decorator_list] + [node.lineno])
    else:
        start = node.lineno
    end = (first - 1) if first else (node.end_lineno or node.lineno)
    return set(range(start, max(start, end) + 1))


def _without_bodies(node) -> str:
    """Dump of a compound statement with its statement lists blanked out."""
    fields = []
    for name, value in ast.iter_fields(node):
        if name in BODY_FIELDS or name == "cases":
            continue
        if name == "handlers":
            value = [(_dump(h.type) if h.type else None, h.name) for h in value]
            fields.append((name, repr(value)))
            continue
        if isinstance(value, list):
            fields.append((name, "[" + ",".join(_dump(v) if isinstance(v, ast.AST) else repr(v) for v in value) + "]"))
        elif isinstance(value, ast.AST):
            fields.append((name, _dump(value)))
        else:
            fields.append((name, repr(value)))
    return type(node).__name__ + repr(fields)


def _targets(node) -> set[str]:
    names: set[str] = set()

    def add_target(t):
        if isinstance(t, ast.Name):
            names.add(t.id)
        elif isinstance(t, (ast.Tuple, ast.List)):
            for e in t.elts:
                add_target(e)
        elif isinstance(t, ast.Starred):
            add_target(t.value)
        elif isinstance(t, ast.Attribute):
            names.add(t.attr)
        elif isinstance(t, ast.Subscript):
            add_target(t.value)

    if isinstance(node, ast.Assign):
        for t in node.targets:
            add_target(t)
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        add_target(node.target)
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        names.add(node.name)
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
        for a in node.names:
            names.add((a.asname or a.name).split(".")[0])
    elif isinstance(node, ast.Expr):
        # e.g. a bare call at class/module level: names it mentions
        for n in ast.walk(node):
            if isinstance(n, ast.Name):
                names.add(n.id)
            elif isinstance(n, ast.Attribute):
                names.add(n.attr)
    else:
        for n in ast.walk(node):
            if isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.FunctionDef, ast.ClassDef)):
                names |= _targets(n)
    return names


def _is_docstring(node) -> bool:
    return isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)


def _def_names(node) -> set[str] | None:
    """Names a module/class-level statement binds; None for a no-op (pass / ...)."""
    if isinstance(node, ast.Pass) or (
        isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and node.value.value is Ellipsis
    ):
        return None
    if _is_docstring(node):
        return {"__doc__"}
    return _targets(node)


def _expr_diffs(old, new, in_lambda: bool, out: list) -> None:
    """Collect (old_node, inside_lambda_body) for minimal differing subtrees."""
    if type(old) is not type(new):
        out.append((old, in_lambda))
        return
    if isinstance(old, ast.AST):
        if _dump(old) == _dump(new):
            return
        for name, ov in ast.iter_fields(old):
            nv = getattr(new, name, None)
            child_lambda = in_lambda or (isinstance(old, ast.Lambda) and name == "body")
            if isinstance(ov, list):
                if not isinstance(nv, list) or len(ov) != len(nv):
                    out.append((old, in_lambda))
                    return
                for a, b in zip(ov, nv):
                    _expr_diffs(a, b, child_lambda, out)
            elif isinstance(ov, ast.AST) or isinstance(nv, ast.AST):
                if ov is None or nv is None:
                    out.append((old, in_lambda))
                    return
                _expr_diffs(ov, nv, child_lambda, out)
            elif ov != nv:
                out.append((old, in_lambda))
                return
    elif old != new:
        out.append((None, in_lambda))


class _Differ:
    def __init__(self) -> None:
        self.facts: list = []

    # -- whole-statement changes ------------------------------------------------
    def changed_whole(self, node, scope, in_func, note="") -> None:
        if in_func:
            self.facts.append(Runtime(_span(node), note or "stmt"))
            return
        if isinstance(node, ast.ClassDef):
            self.facts.append(ClassHeader(scope + (node.name,)))
            self.facts.append(Definition(scope, {node.name}, "class"))
            return
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            self.facts.append(Runtime(set(range(body[0].lineno, (node.end_lineno or body[0].lineno) + 1)), "callers"))
            self.facts.append(Definition(scope, {node.name}, "def"))
            return
        # Compound statements at module/class level (if/try/for at import time):
        names = _def_names(node)
        if names is None:
            return
        self.facts.append(Definition(scope, names, note or type(node).__name__))
        for n in ast.walk(node):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n is not node:
                self.facts.append(Runtime(set(range(n.body[0].lineno, (n.end_lineno or n.lineno) + 1)), "callers"))

    def inserted(self, new_node, scope, in_func, anchors) -> None:
        if in_func:
            lines: set[int] = set()
            for a in anchors:
                if a is not None:
                    lines |= _header_span(a) if _is_compound(a) else _span(a)
            self.facts.append(Runtime(lines, "insert"))
            return
        names = _def_names(new_node)
        if names is not None:
            self.facts.append(Definition(scope, names, "insert"))

    # -- recursive diff --------------------------------------------------------
    def block(self, old_list, new_list, scope, in_func, parent=None) -> None:
        ok = [_dump(s) for s in old_list]
        nk = [_dump(s) for s in new_list]
        sm = difflib.SequenceMatcher(a=ok, b=nk, autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                continue
            olds, news = old_list[i1:i2], new_list[j1:j2]
            if tag == "replace" and len(olds) == len(news):
                for o, n in zip(olds, news):
                    self.stmt(o, n, scope, in_func)
                continue
            prev = old_list[i1 - 1] if i1 > 0 else parent
            nxt = old_list[i2] if i2 < len(old_list) else None
            # Pair same-typed statements greedily; the rest are whole changes.
            paired = 0
            for o, n in zip(olds, news):
                if type(o) is type(n):
                    self.stmt(o, n, scope, in_func)
                    paired += 1
                else:
                    break
            for o in olds[paired:]:
                self.changed_whole(o, scope, in_func)
            for n in news[paired:]:
                self.inserted(n, scope, in_func, [prev, nxt, parent])
            if olds[paired:] and in_func:
                # neighbours of a deletion: control may now flow differently
                for a in (prev, nxt):
                    if a is not None:
                        self.facts.append(Runtime(_header_span(a) if _is_compound(a) else _span(a), "del-neighbour"))

    def stmt(self, o, n, scope, in_func) -> None:
        if _dump(o) == _dump(n):
            return
        if type(o) is not type(n):
            self.changed_whole(o, scope, in_func)
            if not in_func:
                names = _def_names(n)
                if names:
                    self.facts.append(Definition(scope, names, "retype"))
            return
        if isinstance(o, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _without_bodies(o) != _without_bodies(n):
                if in_func:
                    self.facts.append(Runtime(_header_span(o), "nested-def-header"))
                else:
                    self.facts.append(Runtime(set(range(o.body[0].lineno, (o.end_lineno or o.lineno) + 1)), "callers"))
                    self.facts.append(Definition(scope, {o.name}, "def-header"))
            self.block(o.body, n.body, scope + (o.name,), True, parent=o)
            return
        if isinstance(o, ast.ClassDef):
            if _without_bodies(o) != _without_bodies(n):
                if in_func:
                    self.facts.append(Runtime(_header_span(o), "nested-class-header"))
                else:
                    self.facts.append(ClassHeader(scope + (o.name,)))
            self.block(o.body, n.body, scope + (o.name,), in_func, parent=o)
            return
        if _is_compound(o):
            if _without_bodies(o) != _without_bodies(n):
                if in_func:
                    self.facts.append(Runtime(_header_span(o), "header", o, n))
                else:
                    self.changed_whole(o, scope, in_func, "compound-header")
                    return
            for f in BODY_FIELDS:
                ov, nv = getattr(o, f, None) or [], getattr(n, f, None) or []
                if not ov and not nv:
                    continue
                if not ov or not nv:
                    # else-branch added/removed
                    if in_func:
                        self.facts.append(Runtime(_span(o), f"{f}-added/removed"))
                    else:
                        self.changed_whole(o, scope, in_func)
                    continue
                self.block(ov, nv, scope, in_func, parent=o)
            oh, nh = getattr(o, "handlers", None), getattr(n, "handlers", None)
            if oh is not None:
                if len(oh) != len(nh):
                    self.changed_whole(o, scope, in_func)
                else:
                    for a, b in zip(oh, nh):
                        self.block(a.body, b.body, scope, in_func, parent=a)
            oc, nc = getattr(o, "cases", None), getattr(n, "cases", None)
            if oc is not None:
                if len(oc) != len(nc):
                    self.changed_whole(o, scope, in_func)
                else:
                    for a, b in zip(oc, nc):
                        if _dump(a.pattern) != _dump(b.pattern) or _dump(a.guard) != _dump(b.guard):
                            self.facts.append(Runtime(_span(o), "case"))
                        self.block(a.body, b.body, scope, in_func, parent=o)
            return
        # Simple statement.
        if in_func:
            self.facts.append(Runtime(_span(o), "stmt", o, n))
            return
        diffs: list = []
        _expr_diffs(o, n, False, diffs)
        if diffs and all(lam and node is not None and hasattr(node, "lineno") for node, lam in diffs):
            lines: set[int] = set()
            for node, _ in diffs:
                lines |= _span(node)
            self.facts.append(Runtime(lines, "lambda-body"))
            return
        if _is_docstring(o) and _is_docstring(n):
            self.facts.append(Definition(scope, {"__doc__"}, "docstring"))
            return
        self.facts.append(Definition(scope, _targets(o) | _targets(n), _keys_note(diffs)))


def _keys_note(diffs) -> str:
    return "assign"


def _is_compound(node) -> bool:
    return any(hasattr(node, f) for f in ("body", "handlers", "cases"))


def diff_python(old_src: bytes, new_src: bytes) -> DiffResult:
    res = DiffResult()
    try:
        old_tree = ast.parse(old_src)
    except SyntaxError:
        res.facts.append(Broad("old-syntax"))
        return res
    try:
        new_tree = ast.parse(new_src)
        compile(new_src, "<patched>", "exec", dont_inherit=True)
    except (SyntaxError, ValueError):
        res.syntax_error = True
        res.facts.append(Broad("syntax-error"))
        return res
    d = _Differ()
    d.block(old_tree.body, new_tree.body, (), False)
    res.facts = d.facts
    return res
