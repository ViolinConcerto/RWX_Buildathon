"""Dynamic verification: run candidate tests against the patched tree, in a fork.

The parent preloads pytest and the heavy third-party modules (never sqlglot),
then forks one child per commit. The child:
  * serves patched modules from memory via a meta-path finder (original
    __file__ kept, so fixture paths resolve as in the real tree),
  * redirects opens of patched data files to patched copies in /tmp,
  * replaces process pools with an in-process executor that still pickles,
  * runs pytest on the chosen node ids with stdout redirected to stderr,
  * reports failed node ids over a pipe.

Anything unexpected (crash, timeout, missing report) makes the caller treat
every candidate as failing, i.e. fall back to running them.
"""

from __future__ import annotations

import concurrent.futures
import importlib.abc
import importlib.machinery
import importlib.util
import io
import json
import os
import pickle
import select
import signal
import sys
import time


def preload(heavy: bool = True) -> None:
    """Import what every child needs but that does not depend on the patch."""
    import builtins  # noqa: F401
    import unittest  # noqa: F401
    import unittest.mock  # noqa: F401

    mods = ["gzip", "csv", "tracemalloc", "multiprocessing", "concurrent.futures.process", "decimal", "datetime"]
    if os.environ.get("BR_RUNNER") == "pytest":
        mods += ["pytest", "_pytest.unittest", "_pytest.python", "_pytest.terminal", "pytest_timeout"]
    if heavy:
        mods += ["duckdb", "numpy", "pandas"]
    for mod in mods:
        try:
            importlib.import_module(mod)
        except Exception:  # noqa: BLE001
            pass


class InlinePickleExecutor(concurrent.futures.Executor):
    """Process-pool stand-in: runs work immediately, round-tripping through pickle."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **_):
        self._init = (initializer, initargs)
        self._initialized = initializer is None

    def submit(self, fn, /, *args, **kwargs):
        future = concurrent.futures.Future()
        try:
            if not self._initialized:
                self._initialized = True
                init, iargs = pickle.loads(pickle.dumps(self._init))
                init(*iargs)
            fn2, a2, k2 = pickle.loads(pickle.dumps((fn, args, kwargs)))
            future.set_result(pickle.loads(pickle.dumps(fn2(*a2, **k2))))
        except BaseException as error:  # noqa: BLE001
            future.set_exception(error)
        return future

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass


class _LazyFuture(concurrent.futures.Future):
    """Runs its work when the result is first asked for (pickling like a pool would)."""

    def __init__(self, ex, fn, args, kwargs):
        super().__init__()
        self._call = (ex, fn, args, kwargs)

    def result(self, timeout=None):
        if self._call is not None:
            ex, fn, args, kwargs = self._call
            self._call = None
            try:
                ex._ensure_init()
                fn2, a2, k2 = pickle.loads(pickle.dumps((fn, args, kwargs)))
                self.set_result(pickle.loads(pickle.dumps(fn2(*a2, **k2))))
            except BaseException as error:  # noqa: BLE001
                self.set_exception(error)
        return super().result(timeout)


class LazyPickleExecutor(InlinePickleExecutor):
    """For the executor tests only (they read futures one by one with .result()): a
    sub-case's query runs when the test reaches it, so a test that stops at its first
    failing sub-case does not execute the remaining queries."""

    def _ensure_init(self):
        if not self._initialized:
            self._initialized = True
            init, iargs = pickle.loads(pickle.dumps(self._init))
            init(*iargs)

    def submit(self, fn, /, *args, **kwargs):
        return _LazyFuture(self, fn, args, kwargs)


class _PatchedLoader(importlib.machinery.SourceFileLoader):
    def __init__(self, fullname, path, source: bytes):
        super().__init__(fullname, path)
        self._source = source

    def get_data(self, path):
        if path == self.path:
            return self._source
        return super().get_data(path)

    def path_stats(self, path):
        raise OSError("patched source: no bytecode cache")


class _PatchedFinder(importlib.abc.MetaPathFinder):
    def __init__(self, modules: dict[str, tuple[str, bytes]]):
        self.modules = modules

    def find_spec(self, fullname, path=None, target=None):
        hit = self.modules.get(fullname)
        if hit is None:
            return None
        filename, source = hit
        loader = _PatchedLoader(fullname, filename, source)
        if filename.endswith("__init__.py"):
            return importlib.util.spec_from_file_location(
                fullname, filename, loader=loader, submodule_search_locations=[os.path.dirname(filename)]
            )
        return importlib.util.spec_from_file_location(fullname, filename, loader=loader)


def _module_name(rel: str) -> str | None:
    if not rel.endswith(".py"):
        return None
    mod = rel[:-3].replace("/", ".")
    if mod.endswith(".__init__"):
        mod = mod[: -len(".__init__")]
    return mod


class _OutcomePlugin:
    def __init__(self):
        self.failed: set[str] = set()
        self.seen: set[str] = set()
        self.collect_errors: list[str] = []

    def pytest_runtest_logreport(self, report):
        self.seen.add(report.nodeid)
        if report.failed:
            self.failed.add(report.nodeid)

    def pytest_collectreport(self, report):
        if report.failed:
            self.collect_errors.append(report.nodeid)


TPC_STORE = os.environ.get("BR_TPC_STORE", "/opt/tpcstore")


def tpc_store_usable(new_files) -> bool:
    """The build-time store reflects the pinned TPC data and test harness only."""
    if not os.path.exists(os.path.join(TPC_STORE, "rows.pkl")):
        return False
    for rel in new_files:
        if rel in ("tests/test_executor.py", "tests/helpers.py", "tests/__init__.py") or \
                rel.startswith("tests/fixtures/optimizer/tpc-"):
            return False
    return True


class _StoredResult:
    def __init__(self, blob):
        self._blob = blob

    def fetchdf(self):
        return pickle.loads(self._blob)


class _LazyTPCConn:
    """duckdb connection that answers known queries from the build-time store and
    only creates the CSV views (like TestExecutor.setUpClass) on a miss."""

    def __init__(self, kind, directory, schema, answers):
        self.kind, self.directory, self.schema, self.answers = kind, directory, schema, answers
        self._conn = None

    def _real(self):
        if self._conn is None:
            import duckdb

            conn = duckdb.connect()
            for table, columns in self.schema.items():
                file_name = f"{self.directory}{table}.csv.gz"
                conn.execute(
                    f"""
                CREATE VIEW {table} AS
                SELECT *
                FROM READ_CSV('{file_name}', delim='|', header=True, columns={columns})
                """
                )
            self._conn = conn
        return self._conn

    def execute(self, query, *args, **kwargs):
        if not args and not kwargs:
            blob = self.answers().get((self.kind, query))
            if blob is not None:
                return _StoredResult(blob)
        return self._real().execute(query, *args, **kwargs)

    def close(self):
        if self._conn is not None:
            self._conn.close()

    def __getattr__(self, name):
        return getattr(self._real(), name)


def _install_tpc_store() -> None:
    te = importlib.import_module("tests.test_executor")
    cls = te.TestExecutor
    loaded: dict = {}

    def answers():
        if "answers" not in loaded:
            with open(os.path.join(TPC_STORE, "answers.pkl"), "rb") as f:
                loaded["answers"] = pickle.load(f)
        return loaded["answers"]

    def setUpClass(klass):
        with open(os.path.join(TPC_STORE, "rows.pkl"), "rb") as f:
            rows = pickle.load(f)
        klass.tpch_conn = _LazyTPCConn("tpch", te.DIR_TPCH, te.TPCH_SCHEMA, answers)
        klass.tpcds_conn = _LazyTPCConn("tpcds", te.DIR_TPCDS, te.TPCDS_SCHEMA, answers)
        klass.tpch_tables = {t: te.Table(columns=c, rows=rows["tpch"][t]) for t, c in te.TPCH_SCHEMA.items()}
        klass.tpcds_tables = {t: te.Table(columns=c, rows=rows["tpcds"][t]) for t, c in te.TPCDS_SCHEMA.items()}
        klass.cache = {}
        klass.tpch_sqls = list(te.load_sql_fixture_pairs("optimizer/tpc-h/tpc-h.sql"))
        klass.tpcds_sqls = list(te.load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))

    cls.setUpClass = classmethod(setUpClass)


def _install_tpcds_subset(indices: list[int]) -> None:
    """Restrict test_execute_tpcds to the given fixture queries.

    The test runs one independent sub-case per TPC-DS query; queries whose base
    execution never reached the changed lines cannot change outcome, so only
    the others are re-run (maps/querymap.json.gz). tpcds_sqls is used by no
    other test.
    """
    te = importlib.import_module("tests.test_executor")
    cls = te.TestExecutor
    original = cls.__dict__.get("_br_setup") or cls.setUpClass.__func__  # re-installable
    cls._br_setup = original
    keep = list(dict.fromkeys(indices))  # caller's order (cheapest first)

    def setUpClass(klass):
        original(klass)
        klass.tpcds_sqls = [klass.tpcds_sqls[i] for i in keep if i < len(klass.tpcds_sqls)]

    cls.setUpClass = classmethod(setUpClass)


MAX_NODES_PROXY_BASE_CPU = 0.94  # stand-in CPU on the pinned commit (container measurement)


def _max_nodes_proxy(limit_s: float = 6.0) -> tuple[str, float]:
    """Scaled-down stand-in for tests/test_parser.py::TestParser::test_max_nodes.

    Same assertions with 1/25 of the width and of the node limits (nesting depth
    unchanged). Returns ("pass"|"fail"|"timeout"|"slow", cpu seconds).
    """
    import resource

    from sqlglot import exp, parse_one
    from sqlglot.errors import ParseError

    def cpu():
        r = resource.getrusage(resource.RUSAGE_SELF)
        return r.ru_utime + r.ru_stime

    class _Timeout(BaseException):
        pass

    def on_alarm(signum, frame):
        raise _Timeout()

    k = 25
    t0 = cpu()
    old = signal.signal(signal.SIGALRM, on_alarm)
    signal.setitimer(signal.ITIMER_REAL, limit_s)
    try:
        if not isinstance(parse_one("SELECT 1", max_nodes=1_000_000 // k), exp.Select):
            return "fail", cpu() - t0
        cols = [f"col{i}" for i in range(500)]
        if not isinstance(parse_one(f"SELECT {','.join(cols)} FROM t", max_nodes=1_000_000 // k), exp.Select):
            return "fail", cpu() - t0
        sql = "SELECT " + ",".join(["x" + "=x" * 100 for _ in range(10000 // k)])
        for limit, needle in ((1_000_000 // k, "Maximum number of AST nodes"), (50_000 // k, f"({50_000 // k})")):
            try:
                parse_one(sql, max_nodes=limit)
                return "fail", cpu() - t0
            except ParseError as e:
                if needle not in str(e):
                    return "fail", cpu() - t0
        try:
            parse_one("SELECT " + ",".join(f"x={i}" for i in range(100)), max_nodes=5)
            return "fail", cpu() - t0
        except ParseError:
            pass
        if not isinstance(parse_one("SELECT 1, 2, 3"), exp.Select):
            return "fail", cpu() - t0
    except _Timeout:
        return "timeout", cpu() - t0
    except Exception:  # noqa: BLE001
        return "fail", cpu() - t0
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)
    used = cpu() - t0
    return ("slow" if used > 3 * MAX_NODES_PROXY_BASE_CPU else "pass"), used


def _child(src_root: str, tmp_dir: str, new_files: dict[str, bytes | None], nodeids: list[str],
           timeout_s: float, wfd: int, import_modules=(), tpcds_indices=None, stop_on_fail=False,
           proxy=False, two_stage=None, entry_filter=None, conditional=False) -> None:
    # The selector's own heap (dependency map, cached ASTs) is inherited by the
    # fork; freeze it so tests that call gc.collect() do not traverse it.
    import gc

    gc.freeze()
    # Answers go to the real stdout of the parent only: silence ours.
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    sys.dont_write_bytecode = True
    os.environ["SKIP_INTEGRATION"] = "0"
    os.environ.setdefault("HOME", "/tmp")

    modules: dict[str, tuple[str, bytes]] = {}
    redirect: dict[str, str] = {}
    for rel, content in new_files.items():
        full = os.path.join(src_root, rel)
        if content is None:
            continue
        mod = _module_name(rel)
        if mod and (rel.startswith("sqlglot/") or rel.startswith("tests/")):
            modules[mod] = (full, content)
        dst = os.path.join(tmp_dir, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "wb") as f:
            f.write(content)
        redirect[os.path.realpath(full)] = dst

    if modules:
        sys.meta_path.insert(0, _PatchedFinder(modules))

    if redirect:
        import builtins

        real_open = io.open

        def patched_open(file, *args, **kwargs):
            if isinstance(file, (str, bytes, os.PathLike)):
                try:
                    key = os.path.realpath(os.fsdecode(file))
                except Exception:  # noqa: BLE001
                    key = None
                if key in redirect:
                    file = redirect[key]
            return real_open(file, *args, **kwargs)

        builtins.open = patched_open
        io.open = patched_open

    import concurrent.futures.process as cfp

    concurrent.futures.ProcessPoolExecutor = InlinePickleExecutor
    cfp.ProcessPoolExecutor = InlinePickleExecutor

    sys.path.insert(0, src_root)
    os.chdir(src_root)
    signal.signal(signal.SIGALRM, signal.SIG_DFL)

    import_errors = {}
    for mod in sorted(import_modules):
        try:
            importlib.import_module(mod)
        except BaseException as e:  # noqa: BLE001
            import_errors[mod] = f"{type(e).__name__}: {e}"[:300]
    if not import_errors and any(n.startswith("tests/test_executor.py::") for n in nodeids) \
            and tpc_store_usable(new_files):
        try:
            _install_tpc_store()
        except BaseException as e:  # noqa: BLE001
            import_errors["tests.test_executor"] = f"{type(e).__name__}: {e}"[:300]
    stage_report, proven_pass = None, []
    tpcds_id = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
    if not import_errors and two_stage is not None and tpcds_id in nodeids and not conditional:
        try:
            import tpcplan

            with open(os.path.join(TPC_STORE, "plans.json")) as f:
                base = json.load(f)
            changed = tpcplan.stage1(two_stage["front"], base)
            cost = two_stage.get("cost", {})
            stage2 = sorted(set(two_stage["data"]) | set(changed), key=lambda i: (cost.get(str(i), 0.0), i))
            stage_report = {"front": len(two_stage["front"]), "changed": len(changed), "stage2": len(stage2)}
            if not stage2:
                nodeids = [n for n in nodeids if n != tpcds_id]
                proven_pass = [tpcds_id]
            else:
                tpcds_indices = stage2
        except BaseException as e:  # noqa: BLE001
            stage_report = {"error": repr(e)[:200]}  # fall back to the subset below / whole test
            if tpcds_indices is None:
                tpcds_indices = None
    if not import_errors and any(n.startswith("tests/test_executor.py::") for n in nodeids):
        try:
            importlib.import_module("tests.test_executor").ProcessPoolExecutor = LazyPickleExecutor
        except BaseException as e:  # noqa: BLE001
            import_errors["tests.test_executor"] = f"{type(e).__name__}: {e}"[:300]
    if not import_errors and tpcds_indices is not None and tpcds_id in nodeids:
        try:
            _install_tpcds_subset(tpcds_indices)
        except BaseException as e:  # noqa: BLE001
            import_errors["tests.test_executor"] = f"{type(e).__name__}: {e}"[:300]
    if import_errors:
        report = {"ok": True, "import_errors": import_errors, "failed": [], "seen": [], "collect_errors": []}
        data = json.dumps(report).encode()
        view = memoryview(data)
        while view:
            view = view[os.write(wfd, view):]
        os.close(wfd)
        return
    proxy_report = {}
    if proxy:
        verdict, used = _max_nodes_proxy()
        proxy_report = {"max_nodes": verdict, "max_nodes_cpu": round(used, 3)}
    try:
        if os.environ.get("BR_RUNNER") == "pytest":
            import pytest

            plugin = _OutcomePlugin()
            args = ["-q", "-p", "no:cacheprovider", "-p", "no:xdist", "-p", "no:randomly", "--assert=plain",
                    "-o", "addopts=", f"--timeout={max(5, int(timeout_s))}", "--rootdir", src_root, *nodeids]
            pytest.main(args, plugins=[plugin])
            report = {"ok": True, "failed": sorted(plugin.failed), "seen": sorted(plugin.seen),
                      "collect_errors": plugin.collect_errors}
        elif conditional and two_stage is not None and tpcds_id in nodeids:
            report = _run_conditional(nodeids, entry_filter or {}, two_stage, tpcds_id)
            proven_pass, stage_report = report.pop("proven_pass"), report.pop("tpcds_stage")
        else:
            report = run_unittest(nodeids, stop_on_fail, entry_filter or {})
    except BaseException as e:  # noqa: BLE001
        report = {"ok": False, "error": repr(e)}
    report["proxy"] = proxy_report
    report["proven_pass"] = proven_pass
    report["tpcds_stage"] = stage_report
    data = json.dumps(report).encode()
    view = memoryview(data)
    while view:
        n = os.write(wfd, view)
        view = view[n:]
    os.close(wfd)


FIRST_ROUND = 8


def _isolated_run(nodeids, stop_on_fail, entry_filter) -> dict:
    """run_unittest, restoring the unittest.TestCase methods it replaces (several runs
    in one child)."""
    import unittest

    names = ("assertMultiLineEqual", "assertDictEqual", "assertListEqual", "assertTupleEqual",
             "assertSequenceEqual", "assertSetEqual", "subTest")
    saved = {k: unittest.TestCase.__dict__[k] for k in names if k in unittest.TestCase.__dict__}
    try:
        if not nodeids:
            return {"ok": True, "failed": [], "seen": [], "collect_errors": [], "times": {}, "suite_cpu": 0.0}
        return run_unittest(nodeids, stop_on_fail, entry_filter)
    finally:
        for k, v in saved.items():
            setattr(unittest.TestCase, k, v)


def _merge(rep: dict, more: dict) -> None:
    rep["failed"] = sorted(set(rep["failed"]) | set(more["failed"]))
    rep["seen"] = sorted(set(rep["seen"]) | set(more["seen"]))
    rep["collect_errors"] = list(rep["collect_errors"]) + list(more["collect_errors"])
    rep["times"] = {**rep.get("times", {}), **more.get("times", {})}
    rep["suite_cpu"] = round(rep.get("suite_cpu", 0.0) + more.get("suite_cpu", 0.0), 3)


def _run_conditional(nodeids, entry_filter, two_stage, tpcds_id) -> dict:
    """tpcds held under rule A1 (selected only if a cheaper candidate fails), checked
    by the two-stage method only when that happens:
      1. the other candidates, stopping at the first failure
      2. if one failed: test_execute_tpcds, two-stage
      3. if tpcds passed: the candidates step 1 did not reach
    tpcds not run (nothing else failed) is reported in "conditional_skipped"."""
    others = [n for n in nodeids if n != tpcds_id]
    rep = _isolated_run(others, True, entry_filter)
    rep["proven_pass"], rep["conditional_skipped"] = [], [tpcds_id]
    rep["tpcds_stage"] = {"conditional": "skipped (no other candidate failed)"}
    if not (rep["failed"] or rep["collect_errors"]):
        return rep
    rep["conditional_skipped"] = []
    import resource

    def cpu():
        r = resource.getrusage(resource.RUSAGE_SELF)
        return r.ru_utime + r.ru_stime

    c0 = cpu()
    try:
        import tpcplan

        with open(os.path.join(TPC_STORE, "plans.json")) as f:
            base = json.load(f)
        cost = two_stage.get("cost", {})
        key = lambda i: (cost.get(str(i), 0.0), i)  # noqa: E731
        front = sorted(two_stage["front"], key=key)
        # Rounds: the forced queries plus the first few re-planned ones, then the rest. A
        # failing tpcds usually shows in the first round, before most of stage 1 is paid.
        rounds = [(sorted(two_stage["data"], key=key), front[:FIRST_ROUND]), ([], front[FIRST_ROUND:])]
        n_changed = n_run = 0
        tp_ok = True
        for forced, to_plan in rounds:
            changed = tpcplan.stage1(to_plan, base) if to_plan else []
            n_changed += len(changed)
            stage2 = sorted(set(forced) | set(changed), key=key)
            if not stage2:
                continue
            n_run += len(stage2)
            _install_tpcds_subset(stage2)
            more = _isolated_run([tpcds_id], False, {})
            _merge(rep, more)
            if tpcds_id not in more["seen"] or tpcds_id in more["failed"] or more["collect_errors"]:
                tp_ok = False
                break
        rep["tpcds_stage"] = {"conditional": "ran", "front": len(front), "changed": n_changed, "stage2": n_run,
                              "cpu": round(cpu() - c0, 3), "tpcds": "pass" if tp_ok else "fail"}
        if tp_ok and not n_run:
            rep["proven_pass"] = [tpcds_id]
    except BaseException as e:  # noqa: BLE001  (tpcds stays unseen -> selected)
        rep["tpcds_stage"] = {"conditional": "error", "error": repr(e)[:200]}
        return rep
    if tp_ok:
        done = set(rep["seen"]) | set(rep["failed"])
        rest = [n for n in others if n not in done]
        if rest:
            _merge(rep, _isolated_run(rest, False, entry_filter))
    return rep


def _install_entry_filter(entry_filter: dict, current: list) -> None:
    """Data-driven tests re-run only the fixture entries that can observe the edit.

    entry_filter: {test node id: {fixture file: [entry indices]}}; the filter is
    active only while that test runs (other tests read the same files unfiltered).
    """
    import tests.helpers as h

    def wrap(orig):
        def loader(filename, *a, **kw):
            keep = entry_filter.get(current[0], {}).get(filename) if current[0] else None
            if keep is None:
                yield from orig(filename, *a, **kw)
                return
            keep = set(keep)
            for i, item in enumerate(orig(filename, *a, **kw)):
                if i in keep:
                    yield item
        return loader

    pairs, plain = h.load_sql_fixture_pairs, h.load_sql_fixtures
    wp, wl = wrap(pairs), wrap(plain)
    h.load_sql_fixture_pairs, h.load_sql_fixtures = wp, wl
    for name, mod in list(sys.modules.items()):
        if name.startswith("tests.") and mod is not None:
            if getattr(mod, "load_sql_fixture_pairs", None) is pairs:
                mod.load_sql_fixture_pairs = wp
            if getattr(mod, "load_sql_fixtures", None) is plain:
                mod.load_sql_fixtures = wl


def run_unittest(nodeids: list[str], stop_on_fail: bool = False, entry_filter=None) -> dict:
    """Run pytest-style node ids (file::Class::method) with plain unittest.

    Outcome rules match how the labels were produced with pytest: a test fails
    if it errors, fails, has a failing subtest or unexpectedly succeeds, or if
    its class/module fixture fails, or its module cannot be imported. Skips and
    expected failures pass.
    """
    import unittest

    failed: set[str] = set()
    seen: set[str] = set()
    collect_errors: list[str] = []
    by_uid: dict[str, str] = {}
    by_class: dict[str, list[str]] = {}
    by_module: dict[str, list[str]] = {}
    suite = unittest.TestSuite()
    modules: dict[str, object] = {}
    for nid in nodeids:
        parts = nid.split("::")
        if len(parts) != 3:
            failed.add(nid)
            continue
        path, cname, meth = parts
        modname = path[:-3].replace("/", ".")
        if modname not in modules:
            try:
                modules[modname] = importlib.import_module(modname)
            except BaseException:  # noqa: BLE001
                modules[modname] = None
                collect_errors.append(path)
        mod = modules[modname]
        if mod is None:
            failed.add(nid)
            continue
        cls = getattr(mod, cname, None)
        if cls is None or not hasattr(cls, meth):
            failed.add(nid)
            continue
        test = cls(meth)
        by_uid[test.id()] = nid
        by_class.setdefault(f"{cls.__module__}.{cls.__qualname__}", []).append(nid)
        by_module.setdefault(cls.__module__, []).append(nid)
        suite.addTest(test)

    holder: list = []
    current_test: list = [None]
    if entry_filter:
        _install_entry_filter(entry_filter, current_test)

    def mark(test) -> None:
        if stop_on_fail and holder:
            holder[0].shouldStop = True  # tests not run are reported unseen -> selected
        nid = by_uid.get(test.id()) if hasattr(test, "id") else None
        if nid is not None:
            failed.add(nid)
            return
        # _ErrorHolder for setUpClass / tearDownClass / setUpModule failures
        desc = str(getattr(test, "description", test))
        hit = False
        for key, nids in list(by_class.items()) + list(by_module.items()):
            if f"({key})" in desc or desc.endswith(key):
                failed.update(nids)
                hit = True
        if not hit:
            failed.update(nodeids)  # unattributable fixture error: be conservative

    # Only pass/fail matters here, so make failing cheap: (1) equality assertions
    # fail without computing diffs, (2) a test stops at its first failing subtest.
    class _Stop(Exception):
        pass

    # Same pass/fail decisions as unittest's versions (type checks kept), no diffs.
    def _typed(kind):
        def check(self, first, second, msg=None):
            if not isinstance(first, kind) or not isinstance(second, kind):
                raise self.failureException("wrong type")
            if not first == second:
                raise self.failureException("values differ")
        return check

    def _seq(self, seq1, seq2, msg=None, seq_type=None):
        if seq_type is not None and (not isinstance(seq1, seq_type) or not isinstance(seq2, seq_type)):
            raise self.failureException("wrong type")
        if not seq1 == seq2:
            raise self.failureException("values differ")

    def _set(self, set1, set2, msg=None):
        try:
            set1.difference(set2)
            set2.difference(set1)
        except (TypeError, AttributeError):
            raise self.failureException("invalid set") from None
        if not set1 == set2:
            raise self.failureException("values differ")

    unittest.TestCase.assertMultiLineEqual = _typed(str)
    unittest.TestCase.assertDictEqual = _typed(dict)
    unittest.TestCase.assertListEqual = _typed(list)
    unittest.TestCase.assertTupleEqual = _typed(tuple)
    unittest.TestCase.assertSequenceEqual = _seq
    unittest.TestCase.assertSetEqual = _set

    failed_uids: set[str] = set()
    original_subtest = unittest.TestCase.subTest

    import contextlib

    @contextlib.contextmanager
    def subTest(self, msg=unittest.case._subtest_msg_sentinel, **params):
        with original_subtest(self, msg, **params):
            yield
        if self.id() in failed_uids:
            raise _Stop("stopping after first failing subtest")

    unittest.TestCase.subTest = subTest

    import resource

    def _cpu():
        r = resource.getrusage(resource.RUSAGE_SELF)
        return r.ru_utime + r.ru_stime

    times: dict[str, float] = {}
    last = [_cpu()]

    # Reproduce what pytest does around every test, because some tests measure
    # memory: log records are kept by a capture handler and warnings are recorded
    # (DeprecationWarning/PendingDeprecationWarning always), both for the test's span.
    import logging
    import warnings

    class _Capture(logging.Handler):
        def __init__(self):
            super().__init__(0)
            self.records = []

        def emit(self, record):
            self.records.append(record)

    capture = {}

    class Result(unittest.TestResult):
        def startTest(self, test):
            super().startTest(test)
            nid = by_uid.get(test.id())
            if nid:
                seen.add(nid)
            current_test[0] = nid
            handler = _Capture()
            logging.getLogger().addHandler(handler)
            cw = warnings.catch_warnings(record=True)
            cw.__enter__()
            warnings.filterwarnings("always", category=DeprecationWarning)
            warnings.filterwarnings("always", category=PendingDeprecationWarning)
            capture[test.id()] = (handler, cw)
            last[0] = _cpu()

        def stopTest(self, test):
            # Class/module fixtures (setUpClass of the next class) run between tests:
            # they must never see the previous test's fixture-entry filter.
            current_test[0] = None
            handler, cw = capture.pop(test.id(), (None, None))
            if cw is not None:
                cw.__exit__(None, None, None)
            if handler is not None:
                logging.getLogger().removeHandler(handler)
            super().stopTest(test)
            nid = by_uid.get(test.id())
            if nid:
                times[nid] = round(_cpu() - last[0], 3)
            last[0] = _cpu()

        def addError(self, test, err):
            super().addError(test, err)
            mark(test)

        def addFailure(self, test, err):
            super().addFailure(test, err)
            mark(test)

        def addSubTest(self, test, subtest, err):
            super().addSubTest(test, subtest, err)
            if err is not None:
                failed_uids.add(test.id())
                mark(test)

        def addUnexpectedSuccess(self, test):
            super().addUnexpectedSuccess(test)
            mark(test)

    t0 = _cpu()
    holder.append(Result())
    suite.run(holder[0])
    return {"ok": True, "failed": sorted(failed), "seen": sorted(seen), "collect_errors": collect_errors,
            "times": times, "suite_cpu": round(_cpu() - t0, 3)}


class Job:
    def __init__(self, key, pid, rfd, deadline, nodeids):
        self.key, self.pid, self.rfd, self.deadline, self.nodeids = key, pid, rfd, deadline, nodeids
        self.buf = bytearray()
        self.started = time.monotonic()


def start(key, src_root: str, tmp_root: str, new_files, nodeids: list[str], timeout_s: float,
          import_modules=(), tpcds_indices=None, stop_on_fail=False, proxy=False, two_stage=None,
          entry_filter=None, conditional=False) -> Job:
    rfd, wfd = os.pipe()
    tmp_dir = os.path.join(tmp_root, str(key))
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()
    if pid == 0:
        code = 0
        try:
            os.close(rfd)
            _child(src_root, tmp_dir, new_files, nodeids, timeout_s, wfd, import_modules, tpcds_indices, stop_on_fail,
                   proxy, two_stage, entry_filter, conditional)
        except BaseException:  # noqa: BLE001
            code = 1
        finally:
            os._exit(code)
    os.close(wfd)
    return Job(key, pid, rfd, time.monotonic() + timeout_s, nodeids)


def collect(jobs: list[Job], poll: float = 0.05) -> list[tuple[Job, dict | None]]:
    """Wait until at least one job finishes or times out; return finished ones."""
    done: list[tuple[Job, dict | None]] = []
    while not done:
        now = time.monotonic()
        rfds = [j.rfd for j in jobs]
        timeout = max(0.0, min(j.deadline for j in jobs) - now) if jobs else 0
        ready, _, _ = select.select(rfds, [], [], min(timeout, 1.0) if timeout else poll)
        for j in jobs:
            if j.rfd in ready:
                chunk = os.read(j.rfd, 1 << 16)
                if chunk:
                    j.buf.extend(chunk)
                    continue
                os.close(j.rfd)
                os.waitpid(j.pid, 0)
                try:
                    done.append((j, json.loads(bytes(j.buf).decode())))
                except Exception:  # noqa: BLE001
                    done.append((j, None))
        now = time.monotonic()
        for j in jobs:
            if j not in [d[0] for d in done] and now > j.deadline:
                try:
                    os.kill(j.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                os.close(j.rfd)
                os.waitpid(j.pid, 0)
                done.append((j, None))
    for j, _ in done:
        jobs.remove(j)
    return done


def _import_child(src_root: str, new_files, modules: list[str], wfd: int) -> None:
    import gc

    gc.freeze()
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    sys.dont_write_bytecode = True
    os.environ["SKIP_INTEGRATION"] = "0"
    patched: dict[str, tuple[str, bytes]] = {}
    for rel, content in new_files.items():
        mod = _module_name(rel)
        if content is not None and mod and rel.startswith("sqlglot/"):
            patched[mod] = (os.path.join(src_root, rel), content)
    if patched:
        sys.meta_path.insert(0, _PatchedFinder(patched))
    sys.path.insert(0, src_root)
    os.chdir(src_root)
    errors = {}
    for mod in modules:
        try:
            importlib.import_module(mod)
        except BaseException as e:  # noqa: BLE001
            errors[mod] = f"{type(e).__name__}: {e}"[:300]
    data = json.dumps({"ok": True, "errors": errors}).encode()
    view = memoryview(data)
    while view:
        view = view[os.write(wfd, view):]
    os.close(wfd)


def import_check(src_root: str, new_files, modules, timeout_s: float = 20.0) -> dict | None:
    """Import the patched modules in a fork. Returns {"errors": {mod: msg}} or None on crash/timeout."""
    rfd, wfd = os.pipe()
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()
    if pid == 0:
        code = 0
        try:
            os.close(rfd)
            _import_child(src_root, new_files, sorted(modules), wfd)
        except BaseException:  # noqa: BLE001
            code = 1
        finally:
            os._exit(code)
    os.close(wfd)
    job = Job("import", pid, rfd, time.monotonic() + timeout_s, [])
    (_, report), = collect([job])
    return report


STATEFP = os.environ.get("BR_STATEFP", "/opt/statefp.json")


def _statediff_child(src_root: str, new_files, wfd: int) -> None:
    import gc

    gc.freeze()
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    sys.dont_write_bytecode = True
    os.environ["SKIP_INTEGRATION"] = "0"
    patched: dict[str, tuple[str, bytes]] = {}
    for rel, content in new_files.items():
        mod = _module_name(rel)
        if content is not None and mod and rel.startswith("sqlglot/"):
            patched[mod] = (os.path.join(src_root, rel), content)
    if patched:
        sys.meta_path.insert(0, _PatchedFinder(patched))
    sys.path.insert(0, src_root)
    os.chdir(src_root)
    import statefp

    try:
        errors = statefp.import_all(src_root)
    except BaseException as e:  # noqa: BLE001  (sqlglot itself failed to import)
        errors = {"sqlglot": f"{type(e).__name__}: {e}"[:200]}
    report = {"ok": True, "errors": errors}
    if not errors:
        with open(STATEFP) as f:
            base = json.load(f)
        report["changed"] = statefp.diff(base)
    data = json.dumps(report).encode()
    view = memoryview(data)
    while view:
        view = view[os.write(wfd, view):]
    os.close(wfd)


def start_statediff(key, src_root: str, new_files, timeout_s: float = 25.0) -> Job:
    """Import the whole patched sqlglot in a fork and report which import-state bindings changed."""
    rfd, wfd = os.pipe()
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()
    if pid == 0:
        code = 0
        try:
            os.close(rfd)
            _statediff_child(src_root, new_files, wfd)
        except BaseException:  # noqa: BLE001
            code = 1
        finally:
            os._exit(code)
    os.close(wfd)
    return Job(key, pid, rfd, time.monotonic() + timeout_s, [])
