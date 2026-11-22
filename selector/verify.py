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


def _install_tpcds_subset(indices: list[int]) -> None:
    """Restrict test_execute_tpcds to the given fixture queries.

    The test runs one independent sub-case per TPC-DS query; queries whose base
    execution never reached the changed lines cannot change outcome, so only
    the others are re-run (maps/querymap.json.gz). tpcds_sqls is used by no
    other test.
    """
    te = importlib.import_module("tests.test_executor")
    cls = te.TestExecutor
    original = cls.setUpClass.__func__
    keep = sorted(set(indices))

    def setUpClass(klass):
        original(klass)
        klass.tpcds_sqls = [klass.tpcds_sqls[i] for i in keep if i < len(klass.tpcds_sqls)]

    cls.setUpClass = classmethod(setUpClass)


def _child(src_root: str, tmp_dir: str, new_files: dict[str, bytes | None], nodeids: list[str],
           timeout_s: float, wfd: int, import_modules=(), tpcds_indices=None) -> None:
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
    if not import_errors and tpcds_indices is not None:
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
    try:
        if os.environ.get("BR_RUNNER") == "pytest":
            import pytest

            plugin = _OutcomePlugin()
            args = ["-q", "-p", "no:cacheprovider", "-p", "no:xdist", "-p", "no:randomly", "--assert=plain",
                    "-o", "addopts=", f"--timeout={max(5, int(timeout_s))}", "--rootdir", src_root, *nodeids]
            pytest.main(args, plugins=[plugin])
            report = {"ok": True, "failed": sorted(plugin.failed), "seen": sorted(plugin.seen),
                      "collect_errors": plugin.collect_errors}
        else:
            report = run_unittest(nodeids)
    except BaseException as e:  # noqa: BLE001
        report = {"ok": False, "error": repr(e)}
    data = json.dumps(report).encode()
    view = memoryview(data)
    while view:
        n = os.write(wfd, view)
        view = view[n:]
    os.close(wfd)


def run_unittest(nodeids: list[str]) -> dict:
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

    def mark(test) -> None:
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
            handler = _Capture()
            logging.getLogger().addHandler(handler)
            cw = warnings.catch_warnings(record=True)
            cw.__enter__()
            warnings.filterwarnings("always", category=DeprecationWarning)
            warnings.filterwarnings("always", category=PendingDeprecationWarning)
            capture[test.id()] = (handler, cw)
            last[0] = _cpu()

        def stopTest(self, test):
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
    suite.run(Result())
    return {"ok": True, "failed": sorted(failed), "seen": sorted(seen), "collect_errors": collect_errors,
            "times": times, "suite_cpu": round(_cpu() - t0, 3)}


class Job:
    def __init__(self, key, pid, rfd, deadline, nodeids):
        self.key, self.pid, self.rfd, self.deadline, self.nodeids = key, pid, rfd, deadline, nodeids
        self.buf = bytearray()
        self.started = time.monotonic()


def start(key, src_root: str, tmp_root: str, new_files, nodeids: list[str], timeout_s: float,
          import_modules=(), tpcds_indices=None) -> Job:
    rfd, wfd = os.pipe()
    tmp_dir = os.path.join(tmp_root, str(key))
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()
    if pid == 0:
        code = 0
        try:
            os.close(rfd)
            _child(src_root, tmp_dir, new_files, nodeids, timeout_s, wfd, import_modules, tpcds_indices)
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
