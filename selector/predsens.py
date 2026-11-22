"""Predicate-sensitivity narrowing for operator edits (relational coverage).

When an edited statement differs from the original only in operators of
comparisons (same operands) or of `and`/`or` (same operands), a test can only be
affected if, at some evaluation in the base run, the new operator would have
produced a different outcome (or evaluated different operands). The per-test
relations observed at each site come from maps/predmap.json.gz.

Soundness argument (per test): until the test first evaluates an edited site
with a relation for which old and new operators disagree, its execution is the
base execution; if no such evaluation exists, the whole run is unchanged.
Anything not decidable from the recorded relations counts as "may differ":
missing relation letters, incomplete evaluations ("!"), operands the new
operator would evaluate that the base run never evaluated, skipping operands
that contain calls, value (non-boolean) contexts where the returned operand
changes, and sites evaluated at import time.
"""

from __future__ import annotations

import ast
import gzip
import json

CMP_OPS = (ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq, ast.Is, ast.IsNot, ast.In, ast.NotIn)


def _dump(node) -> str:
    return ast.dump(node, include_attributes=False)


class PredMap:
    def __init__(self, path: str, tests: list[dict]):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            raw = json.load(f)
        id2num = {t["id"]: t["number"] for t in tests}
        self.remap = [id2num.get(i) for i in raw["tests"]]
        self.sites = raw["sites"]
        self.import_sites = set(raw["import_sites"])
        self.sets = raw["sets"]

    def tests_of(self, sid: int) -> set[int]:
        return {self.remap[i] for i in self.sets[sid] if self.remap[i] is not None}


def _cmp_outcome(op, sig: str):
    """Truth of `left OP right` given the recorded relations, or None if unknown."""
    order = next((c for c in sig if c in "LEG"), None)
    eq = "e" in sig or (order == "E")
    neq = "n" in sig or (order in ("L", "G"))
    if op is ast.Lt:
        return None if order is None else order == "L"
    if op is ast.LtE:
        return None if order is None else order in "LE"
    if op is ast.Gt:
        return None if order is None else order == "G"
    if op is ast.GtE:
        return None if order is None else order in "GE"
    if op is ast.Eq:
        return True if eq else (False if neq else None)
    if op is ast.NotEq:
        return False if eq else (True if neq else None)
    if op is ast.Is:
        return True if "i" in sig else (False if "j" in sig else None)
    if op is ast.IsNot:
        return False if "i" in sig else (True if "j" in sig else None)
    if op is ast.In:
        return True if "m" in sig else (False if "o" in sig else None)
    if op is ast.NotIn:
        return False if "m" in sig else (True if "o" in sig else None)
    return None


def _pure(node) -> bool:
    for n in ast.walk(node):
        if isinstance(n, (ast.Call, ast.Await, ast.Yield, ast.YieldFrom, ast.NamedExpr, ast.Lambda)):
            return False
    return True


def _bool_eval(is_and: bool, truths: str, n: int):
    """Simulate a BoolOp over recorded truths; returns (outcome, evaluated count) or None."""
    for i in range(n):
        if i >= len(truths):
            return None  # needs an operand the base run never evaluated here
        t = truths[i] == "T"
        if is_and and not t:
            return False, i + 1
        if not is_and and t:
            return True, i + 1
    return (truths[n - 1] == "T"), n


def _boolop_may_differ(old_and: bool, new_and: bool, sig: str, values, boolean_ctx: bool) -> bool:
    if "!" in sig or not sig:
        return True
    n = len(values)
    old = _bool_eval(old_and, sig, n)
    new = _bool_eval(new_and, sig, n)
    if old is None or new is None:
        return True
    (o_out, o_k), (n_out, n_k) = old, new
    if o_out != n_out:
        return True
    if not boolean_ctx and o_k != n_k:
        return True  # a different operand's value is returned
    if n_k < o_k and not all(_pure(v) for v in values[n_k:o_k]):
        return True  # skipped operands could have had side effects
    return False


def _boolean_contexts(tree) -> set[int]:
    """ids of BoolOp nodes whose value is only used for its truth."""
    out: set[int] = set()
    for node in ast.walk(tree):
        tests = []
        if isinstance(node, (ast.If, ast.While, ast.IfExp, ast.Assert)):
            tests.append(node.test)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            tests.append(node.operand)
        elif isinstance(node, ast.comprehension):
            tests.extend(node.ifs)
        stack = list(tests)
        while stack:
            t = stack.pop()
            if isinstance(t, ast.BoolOp):
                out.add(id(t))
                stack.extend(t.values)  # operands of a boolean-context BoolOp are boolean too
    return out


def find_op_changes(old, new):
    """[(old_node, kind)] if old/new differ only in comparison / and-or operators; else None."""
    if _dump(old) == _dump(new):
        return []
    if type(old) is not type(new):
        return None
    if isinstance(old, ast.Compare):
        if (len(old.ops) == 1 == len(new.ops) and type(old.ops[0]) is not type(new.ops[0])
                and type(new.ops[0]) in CMP_OPS
                and _dump(old.left) == _dump(new.left) and _dump(old.comparators[0]) == _dump(new.comparators[0])):
            return [(old, new, "c")]
    if isinstance(old, ast.BoolOp):
        if type(old.op) is not type(new.op) and len(old.values) == len(new.values) and all(
                _dump(a) == _dump(b) for a, b in zip(old.values, new.values)):
            return [(old, new, "b")]
    out = []
    for name, ov in ast.iter_fields(old):
        if name in ("body", "orelse", "finalbody", "handlers", "cases"):
            continue
        nv = getattr(new, name, None)
        if isinstance(ov, list):
            if not isinstance(nv, list) or len(ov) != len(nv):
                return None
            pairs = zip(ov, nv)
        else:
            pairs = [(ov, nv)]
        for a, b in pairs:
            if isinstance(a, ast.AST) and isinstance(b, ast.AST):
                sub = find_op_changes(a, b)
                if sub is None:
                    return None
                out += sub
            elif a != b:
                return None
    return out


def affected_tests(pm: PredMap | None, rel: str, old_stmt, new_stmt, old_tree) -> tuple[set[int] | None, str]:
    """Tests that may observe the edit, or None when the rule does not apply."""
    if pm is None or old_stmt is None or new_stmt is None:
        return None, "no predmap"
    changes = find_op_changes(old_stmt, new_stmt)
    if not changes:
        return None, "not an operator edit"
    bctx = _boolean_contexts(old_tree)
    out: set[int] = set()
    for o, n, kind in changes:
        key = f"{rel}:{o.lineno}:{o.col_offset}:{kind}"
        if key in pm.import_sites:
            return None, f"{key} evaluated at import"
        sigs = pm.sites.get(key)
        if sigs is None:
            continue  # never evaluated by any test: no test can observe it
        for sig, sid in sigs.items():
            if kind == "c":
                a = _cmp_outcome(type(o.ops[0]), sig)
                b = _cmp_outcome(type(n.ops[0]), sig)
                differ = a is None or b is None or a != b
            else:
                differ = _boolop_may_differ(isinstance(o.op, ast.And), isinstance(n.op, ast.And), sig,
                                            o.values, id(o) in bctx)
            if differ:
                out |= pm.tests_of(sid)
    return out, f"{len(changes)} operator edit(s)"
