"""Merge collector shards into the runtime dependency map (maps/depmap.json.gz).

Runs inside the lab image (needs sqlglot importable for the class hierarchy).
usage: python build_map.py <tests.json> <src_root> <out.json.gz> <shard_dir>...

Everything here is derived from the pinned sqlglot commit (coverage, file reads,
object classes, identifiers). Nothing is derived from any patch.

Per test T, a fact counts for T when it was observed in T's own setup/call/
teardown, in the setup/teardown of any test of T's class (setUpClass runs inside
the first test's setup), or while T's module was being collected.
"""

from __future__ import annotations

import ast
import gzip
import json
import os
import sys
from collections import defaultdict

from coverage import CoverageData


def classify_lines(source: str) -> tuple[set[int], dict[int, set[str]]]:
    """Return (runtime_lines, identifiers_by_line).

    runtime_lines: lines inside a function or lambda body (run when called).
    identifiers_by_line: Name ids / Attribute attrs / identifier-like string
    constants appearing on each line (for name-based reader lookups).
    """
    tree = ast.parse(source)
    runtime: set[int] = set()
    idents: dict[int, set[str]] = defaultdict(set)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0]
            # Skip the docstring statement's lines? They are inert; keep simple.
            runtime.update(range(first.lineno, (node.end_lineno or first.lineno) + 1))
        elif isinstance(node, ast.Lambda):
            b = node.body
            runtime.update(range(b.lineno, (b.end_lineno or b.lineno) + 1))
        if isinstance(node, ast.Name):
            idents[node.lineno].add(node.id)
        elif isinstance(node, ast.Attribute):
            for ln in range(node.lineno, (node.end_lineno or node.lineno) + 1):
                idents[ln].add(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            v = node.value
            if v.isidentifier() and len(v) <= 64:
                idents[node.lineno].add(v)
    return runtime, idents


def statement_bindings(stmt) -> set[str]:
    """Module-level names a statement binds (not comprehension/lambda locals)."""
    out: set[str] = set()

    def target(t):
        if isinstance(t, ast.Name):
            out.add(t.id)
        elif isinstance(t, (ast.Tuple, ast.List)):
            for e in t.elts:
                target(e)
        elif isinstance(t, ast.Starred):
            target(t.value)

    if isinstance(stmt, ast.Assign):
        for t in stmt.targets:
            target(t)
    elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
        target(stmt.target)
    elif isinstance(stmt, (ast.For, ast.AsyncFor)):
        target(stmt.target)
        for b in stmt.body + stmt.orelse:
            out |= statement_bindings(b)
    elif isinstance(stmt, (ast.With, ast.AsyncWith)):
        for it in stmt.items:
            if it.optional_vars is not None:
                target(it.optional_vars)
        for b in stmt.body:
            out |= statement_bindings(b)
    elif isinstance(stmt, (ast.If, ast.While)):
        for b in stmt.body + stmt.orelse:
            out |= statement_bindings(b)
    elif isinstance(stmt, ast.Try):
        for b in stmt.body + stmt.orelse + stmt.finalbody + [x for h in stmt.handlers for x in h.body]:
            out |= statement_bindings(b)
    elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        out.add(stmt.name)
    elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
        out.update((a.asname or a.name).split(".")[0] for a in stmt.names)
    return out


def deftime_refs(source: str, module: str):
    """Identifiers mentioned by definition-time code (class bodies / module level,
    outside function bodies, lambda bodies excluded).

    Returns (class_refs, module_refs): ident -> set of class keys whose body
    mentions it; ident -> set of "module:name" bound by module-level statements
    mentioning it.
    """
    tree = ast.parse(source)
    class_refs: dict[str, set[str]] = defaultdict(set)
    module_refs: dict[str, set[str]] = defaultdict(set)

    def idents_of(node):
        out = set()
        stack = [node]
        while stack:
            n = stack.pop()
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # decorators, defaults and annotations run at definition time
                stack.extend(n.decorator_list)
                stack.extend(n.args.defaults)
                stack.extend(d for d in n.args.kw_defaults if d is not None)
                continue
            if isinstance(n, ast.Lambda):
                stack.extend(n.args.defaults)
                continue
            if isinstance(n, ast.ClassDef):
                continue  # handled by its own scope
            if isinstance(n, ast.Name):
                out.add(n.id)
            elif isinstance(n, ast.Attribute):
                out.add(n.attr)
            elif isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.isidentifier():
                out.add(n.value)
            stack.extend(ast.iter_child_nodes(n))
        return out

    def visit_body(body, scope):
        for stmt in body:
            if isinstance(stmt, ast.ClassDef):
                key = f"{module}:{'.'.join(scope + [stmt.name])}"
                header = stmt.bases + stmt.keywords + stmt.decorator_list
                for h in header:
                    for i in idents_of(h):
                        class_refs[i].add(key)
                visit_body(stmt.body, scope + [stmt.name])
                continue
            ids = idents_of(stmt)
            if scope:
                key = f"{module}:{'.'.join(scope)}"
                for i in ids:
                    class_refs[i].add(key)
            else:
                bound = statement_bindings(stmt)
                for i in ids:
                    for b in bound:
                        if b != i:
                            module_refs[i].add(f"{module}:{b}")

    visit_body(tree.body, [])
    return class_refs, module_refs


def main() -> None:
    tests_json, src_root, out_path, *shards = sys.argv[1:]
    src_root = os.path.realpath(src_root)
    tests = json.load(open(tests_json))["tests"]
    number = {t["id"]: t["number"] for t in tests}
    cls_of = {t["number"]: t["id"].rsplit("::", 1)[0] for t in tests}
    by_class: dict[str, list[int]] = defaultdict(list)
    by_file: dict[str, list[int]] = defaultdict(list)
    for t in tests:
        by_class[cls_of[t["number"]]].append(t["number"])
        by_file[t["file"]].append(t["number"])

    def expand(context: str) -> tuple[str, list[int]]:
        """Map a context to (kind, test numbers it counts for)."""
        if context.startswith("collect|"):
            return "collect", by_file.get(context[len("collect|"):], [])
        if "|" in context:
            nodeid, phase = context.rsplit("|", 1)
            n = number.get(nodeid)
            if n is None:
                return "unknown", []
            if phase in ("setup", "teardown"):
                return phase, by_class[cls_of[n]]
            return phase, [n]
        return "global", []  # init / between: import time, not test-specific

    sets: dict[frozenset, int] = {}
    set_list: list[list[int]] = []

    def set_id(s) -> int:
        key = frozenset(s)
        if key not in sets:
            sets[key] = len(set_list)
            set_list.append(sorted(key))
        return sets[key]

    line_tests: dict[str, dict[int, set[int]]] = defaultdict(lambda: defaultdict(set))
    import_lines: dict[str, set[int]] = defaultdict(set)  # lines run at import / outside tests
    any_lines: dict[str, set[int]] = defaultdict(set)
    reads: dict[str, set[int]] = defaultdict(set)
    classes: dict[str, set[int]] = defaultdict(set)
    runtime_cache: dict[str, tuple[set[int], dict]] = {}

    def runtime_info(rel: str):
        if rel not in runtime_cache:
            try:
                src = open(os.path.join(src_root, rel), encoding="utf-8").read()
                runtime_cache[rel] = classify_lines(src)
            except (OSError, SyntaxError):
                runtime_cache[rel] = (set(), {})
        return runtime_cache[rel]

    for shard in shards:
        data = CoverageData(os.path.join(shard, "coverage.sqlite"))
        data.read()
        for path in data.measured_files():
            rel = os.path.relpath(path, "/src/sqlglot")
            runtime, _ = runtime_info(rel)
            for line, contexts in data.contexts_by_lineno(path).items():
                any_lines[rel].add(line)
                for ctx in contexts:
                    kind, nums = expand(ctx)
                    if kind == "global":
                        import_lines[rel].add(line)
                    if kind == "collect" and line not in runtime:
                        continue  # import-time execution of module/class bodies
                    if nums:
                        line_tests[rel][line].update(nums)
        r = json.load(open(os.path.join(shard, "reads.json")))
        for ctx, paths in r.items():
            kind, nums = expand(ctx)
            for p in paths:
                reads[p].update(nums)
                if kind == "global":
                    reads.setdefault(p, set())
        c = json.load(open(os.path.join(shard, "classes.json")))
        for ctx, names in c.items():
            _, nums = expand(ctx)
            for name in names:
                classes[name].update(nums)

    # Lines executed while importing every sqlglot module, independent of
    # which test happened to trigger a lazy import (tools/mapgen/import_closure.py).
    closure_path = os.environ.get("IMPORT_CLOSURE")
    if closure_path:
        for rel, lines in json.load(open(closure_path)).items():
            import_lines[rel].update(lines)

    # Identifier -> tests that ran a runtime line mentioning it; and whether it
    # is mentioned on a function-body line run at import time (metaclasses,
    # class-creation helpers), i.e. read while classes are being built.
    ident_tests: dict[str, set[int]] = defaultdict(set)
    ident_import: set[str] = set()
    for rel, lines in import_lines.items():
        if rel.startswith("sqlglot/"):
            runtime, idents = runtime_info(rel)
            for line in lines:
                if line in runtime:
                    ident_import.update(idents.get(line, ()))
    for rel, lines in any_lines.items():
        if not rel.startswith("sqlglot/"):
            continue
        runtime, idents = runtime_info(rel)
        for line in lines:
            names = idents.get(line, ())
            tl = line_tests[rel].get(line)
            if tl and line in runtime:
                for name in names:
                    ident_tests[name].update(tl)

    # Class hierarchy: every sqlglot class -> all transitive subclasses.
    sys.path.insert(0, src_root)
    import importlib
    import pkgutil

    import sqlglot

    for mod in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        try:
            importlib.import_module(mod.name)
        except Exception:  # noqa: BLE001
            pass
    subclasses: dict[str, list[str]] = {}

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from classkey import class_key as qual

    seen = set()
    stack = [object]
    allc = []
    while stack:
        c = stack.pop()
        try:
            subs_of_c = type.__subclasses__(c) if c is type else c.__subclasses__()
        except TypeError:
            continue
        for s in subs_of_c:
            if s not in seen:
                seen.add(s)
                stack.append(s)
                if s.__module__.startswith("sqlglot"):
                    allc.append(s)
    for c in allc:
        subs, st = set(), [c]
        while st:
            x = st.pop()
            try:
                xs = x.__subclasses__()
            except TypeError:
                continue
            for s in xs:
                if s not in subs:
                    subs.add(s)
                    st.append(s)
        subclasses[qual(c)] = sorted(qual(s) for s in subs if s.__module__.startswith("sqlglot"))

    class_refs_all: dict[str, set[str]] = defaultdict(set)
    module_refs_all: dict[str, set[str]] = defaultdict(set)
    for dirpath, _, files in os.walk(os.path.join(src_root, "sqlglot")):
        for fn in files:
            if not fn.endswith(".py"):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, src_root)
            module = rel[:-3].replace(os.sep, ".")
            if module.endswith(".__init__"):
                module = module[: -len(".__init__")]
            try:
                cr, mr = deftime_refs(open(full, encoding="utf-8").read(), module)
            except SyntaxError:
                continue
            for k, v in cr.items():
                class_refs_all[k] |= v
            for k, v in mr.items():
                module_refs_all[k] |= v

    out = {
        "version": 1,
        "class_refs": {k: sorted(v) for k, v in sorted(class_refs_all.items())},
        "module_refs": {k: sorted(v) for k, v in sorted(module_refs_all.items())},
        "tests": [t["id"] for t in tests],
        "lines": {
            rel: {str(line): set_id(nums) for line, nums in sorted(m.items())}
            for rel, m in sorted(line_tests.items())
        },
        "import_lines": {rel: sorted(v) for rel, v in sorted(import_lines.items())},
        "executed_lines": {rel: sorted(v) for rel, v in sorted(any_lines.items())},
        "reads": {p: set_id(v) for p, v in sorted(reads.items())},
        "classes": {k: set_id(v) for k, v in sorted(classes.items())},
        "subclasses": {k: v for k, v in sorted(subclasses.items()) if v},
        "idents": {k: set_id(v) for k, v in sorted(ident_tests.items())},
        "ident_import": sorted(ident_import),
        "sets": set_list,
    }
    with gzip.open(out_path, "wt", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"))
    print(
        f"files={len(out['lines'])} sets={len(set_list)} reads={len(reads)} "
        f"classes={len(classes)} idents={len(ident_tests)} size={os.path.getsize(out_path)}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
