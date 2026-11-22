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


def preload() -> None:
    """Import what every child needs but that does not depend on the patch."""
    import builtins  # noqa: F401
    import unittest  # noqa: F401
    import unittest.mock  # noqa: F401

    import pytest  # noqa: F401
    import _pytest.unittest  # noqa: F401
    import _pytest.python  # noqa: F401
    import _pytest.terminal  # noqa: F401

    for mod in ("pytest_timeout", "duckdb", "numpy", "pandas", "gzip", "csv", "tracemalloc",
                "multiprocessing", "concurrent.futures.process", "decimal", "datetime"):
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


def _child(src_root: str, tmp_dir: str, new_files: dict[str, bytes | None], nodeids: list[str],
           timeout_s: float, wfd: int) -> None:
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

    import pytest

    plugin = _OutcomePlugin()
    args = ["-q", "-p", "no:cacheprovider", "-p", "no:xdist", "-p", "no:randomly", "--assert=plain",
            "-o", "addopts=", f"--timeout={max(5, int(timeout_s))}", "--rootdir", src_root, *nodeids]
    try:
        pytest.main(args, plugins=[plugin])
        report = {"ok": True, "failed": sorted(plugin.failed), "seen": sorted(plugin.seen),
                  "collect_errors": plugin.collect_errors}
    except BaseException as e:  # noqa: BLE001
        report = {"ok": False, "error": repr(e)}
    data = json.dumps(report).encode()
    view = memoryview(data)
    while view:
        n = os.write(wfd, view)
        view = view[n:]
    os.close(wfd)


class Job:
    def __init__(self, key, pid, rfd, deadline, nodeids):
        self.key, self.pid, self.rfd, self.deadline, self.nodeids = key, pid, rfd, deadline, nodeids
        self.buf = bytearray()
        self.started = time.monotonic()


def start(key, src_root: str, tmp_root: str, new_files, nodeids: list[str], timeout_s: float) -> Job:
    rfd, wfd = os.pipe()
    tmp_dir = os.path.join(tmp_root, str(key))
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()
    if pid == 0:
        code = 0
        try:
            os.close(rfd)
            _child(src_root, tmp_dir, new_files, nodeids, timeout_s, wfd)
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
