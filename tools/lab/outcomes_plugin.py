"""pytest plugin: write per-test outcome (any failed phase/subtest => failed) to $OUTCOMES."""
import json, os

_failed, _seen, _collect_errors = set(), set(), []


def pytest_runtest_logreport(report):
    _seen.add(report.nodeid)
    if report.failed:
        _failed.add(report.nodeid)


def pytest_collectreport(report):
    if report.failed:
        _collect_errors.append(report.nodeid)


def pytest_sessionfinish(session, exitstatus):
    out = os.environ.get("OUTCOMES")
    if out and not hasattr(session.config, "workerinput"):
        with open(out, "w") as f:
            json.dump({"failed": sorted(_failed), "seen": sorted(_seen),
                       "collect_errors": _collect_errors, "exitstatus": int(exitstatus)}, f)
