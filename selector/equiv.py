"""Proof of behavioural equivalence for identity <-> equality operator swaps.

Applies only when an edited statement differs from the original in nothing but
one or more single-operator comparisons switching between `is` and `==` (or
`is not` and `!=`), with identical operands.

The proof uses the operand-type profile of the pinned commit's test run
(maps/cmp_profile.json.gz): every pair of operand types that reached the site.
For each pair the two operators must agree on *all* values of those types:

  * both None                                   -> identical
  * one side None, other's __eq__/__ne__ are object's or a builtin's that returns
    NotImplemented for foreign types (int, str, tuple, ...)  -> both mean `is None`
  * same type, bool                             -> singletons
  * same type, __eq__/__ne__ inherited from object -> == is identity
  * same Enum class (not Flag), values unique, __eq__/__ne__ from object/str/int
                                                -> equal iff same member
Anything else (custom __eq__ such as sqlglot's Expr or Dialect, mocks, plain
ints/strs compared to each other, different enum classes) is not proven.

Soundness argument: a test that does not otherwise reach changed code runs
exactly as on the base revision until it evaluates the swapped comparison; its
operands then have types seen in the profile, for which both operators return
the same value, so execution continues identically. By induction the test's
behaviour is unchanged.
"""

from __future__ import annotations

import ast
import gzip
import json

ALLOWED = {(ast.Is, ast.Eq), (ast.Eq, ast.Is), (ast.IsNot, ast.NotEq), (ast.NotEq, ast.IsNot)}
BUILTIN_NOTIMPL_EQ = {"object", "int", "float", "complex", "str", "bytes", "bytearray", "tuple", "list",
                      "dict", "set", "frozenset", "bool", "range", "type"}
BODY_FIELDS = {"body", "orelse", "finalbody", "handlers", "cases"}


def _dump(node) -> str:
    return ast.dump(node, include_attributes=False)


class CmpProfile:
    def __init__(self, path: str):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            raw = json.load(f)
        self.sites: dict[str, list[list[str]]] = raw["sites"]
        self.types: dict[str, dict] = raw["types"]

    def pair_equivalent(self, left: str, right: str) -> bool:
        dl, dr = self.types.get(left), self.types.get(right)
        if dl is None or dr is None:
            return False
        if dl.get("is_none") and dr.get("is_none"):
            return True
        if dl.get("is_none") or dr.get("is_none"):
            other = dr if dl.get("is_none") else dl
            if "enum" in other:
                return _enum_ok(other, none_side=True)
            return other.get("eq_owner") in BUILTIN_NOTIMPL_EQ and other.get("ne_owner") in BUILTIN_NOTIMPL_EQ
        if left != right:
            return False
        if dl.get("is_bool"):
            return True
        if dl.get("eq_identity"):
            return True
        return _enum_ok(dl, none_side=False)


def _enum_ok(d: dict, none_side: bool) -> bool:
    e = d.get("enum")
    if not e:
        return False
    owners_ok = e.get("eq_owner") in ("object", "str", "int") and e.get("ne_owner") in ("object", "str", "int")
    if none_side:
        return owners_ok
    return owners_ok and bool(e.get("unique_values"))


def find_swaps(old, new) -> list | None:
    """Comparisons whose operator changed; None if anything else differs."""
    if _dump(old) == _dump(new):
        return []
    if type(old) is not type(new):
        return None
    if isinstance(old, ast.Compare):
        if (len(old.ops) == 1 and len(new.ops) == 1 and type(old.ops[0]) is not type(new.ops[0])
                and _dump(old.left) == _dump(new.left)
                and _dump(old.comparators[0]) == _dump(new.comparators[0])):
            return [(old, type(old.ops[0]), type(new.ops[0]))]
    out = []
    for name, ov in ast.iter_fields(old):
        if name in BODY_FIELDS:
            continue  # statement lists are diffed separately
        nv = getattr(new, name, None)
        if isinstance(ov, list):
            if not isinstance(nv, list) or len(ov) != len(nv):
                return None
            for a, b in zip(ov, nv):
                if isinstance(a, ast.AST) and isinstance(b, ast.AST):
                    sub = find_swaps(a, b)
                    if sub is None:
                        return None
                    out += sub
                elif a != b:
                    return None
        elif isinstance(ov, ast.AST) and isinstance(nv, ast.AST):
            sub = find_swaps(ov, nv)
            if sub is None:
                return None
            out += sub
        elif ov != nv:
            return None
    return out


def proven_equivalent(profile: CmpProfile | None, rel: str, old_stmt, new_stmt) -> tuple[bool, str]:
    if profile is None or old_stmt is None or new_stmt is None:
        return False, "no profile"
    swaps = find_swaps(old_stmt, new_stmt)
    if not swaps:
        return False, "not an operator swap"
    for node, a, b in swaps:
        if (a, b) not in ALLOWED:
            return False, f"{a.__name__}->{b.__name__} not identity/equality"
        key = f"{rel}:{node.lineno}:{node.col_offset}"
        pairs = profile.sites.get(key)
        if not pairs:
            return False, f"site {key} not profiled"
        bad = [p for p in pairs if not profile.pair_equivalent(p[0], p[1])]
        if bad:
            return False, f"site {key}: {bad[0][0]} vs {bad[0][1]} not provable"
    return True, f"{len(swaps)} swap(s) proven over {sum(len(profile.sites[f'{rel}:{n.lineno}:{n.col_offset}']) for n, _, _ in swaps)} type pairs"
