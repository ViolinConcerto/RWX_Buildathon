"""Stable, unique keys for classes (shared by the collector and the map builder).

`module:qualname` when that path resolves back to the class itself; otherwise the
class was created dynamically (e.g. sqlglot's _Dialect metaclass builds a
`Tokenizer` subclass for every dialect without one, all named
`sqlglot.dialects.dialect:Tokenizer`), so it is keyed by its bases instead:
`dyn:<qualname><<base keys>`. Without this, those classes share one key and
their users are merged.
"""

import sys


def class_key(c, _depth=0) -> str:
    mod, q = c.__module__, c.__qualname__
    obj = sys.modules.get(mod)
    for part in q.split("."):
        obj = getattr(obj, part, None) if obj is not None else None
    if obj is c or _depth > 8:
        return f"{mod}:{q}"
    return f"dyn:{q}<" + ",".join(class_key(b, _depth + 1) for b in c.__bases__) + ">"
