"""Blast Radius selector entry point.

Reads /data/commits/*.patch and /data/tests.json, prints one JSON line per commit
to stdout. Logs go to stderr.

1. Static analysis bounds each commit's candidate tests (static.py).
2. Where it pays off, the candidates are run against the patched tree in a
   forked child (verify.py) and only the tests that actually fail are selected.
Any failure along the way falls back to selecting the static candidates (or the
whole suite), so a bug here costs seconds, never a miss.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

DATA = Path(os.environ.get("BR_DATA", "/data"))
SRC = os.path.abspath(os.environ.get("BR_SRC", "/opt/sqlglot"))
MAP = os.environ.get("BR_MAP", str(HERE.parent / "maps" / "depmap.json.gz"))
CMP_PROFILE = os.environ.get("BR_CMP", str(HERE.parent / "maps" / "cmp_profile.json.gz"))
TMP = os.environ.get("BR_TMP", "/tmp/br")
VERIFY = os.environ.get("BR_VERIFY", "1") == "1"
PARALLEL = int(os.environ.get("BR_PARALLEL", "2"))
WORKER_COST_MS = 2000
MAX_WORKERS = 8

# Prior probability that a commit of this shape breaks at least one candidate.
# Priors from the public sample's small-candidate commits (runtime 5/8, data 10/10,
# definition 1/3), shrunk toward 0.5; see experiments/LOG.md (E6).
P_BREAK = {"runtime": 0.65, "definition": 0.4, "data": 0.9, "test": 0.9, "readers-only": 0.1}
# Runner CPU per recorded second, per test file, measured on the pinned commit.
try:
    CPU_FACTORS = json.loads((HERE.parent / "maps" / "cpu_factors.json").read_text())["factor"]
except Exception:  # noqa: BLE001
    CPU_FACTORS = {}
# Measured per-test CPU / per-file setup of the verification runner (tools/calibrate_tests.py).
try:
    _tc = json.loads((HERE.parent / "maps" / "test_cpu.json").read_text())
    TEST_CPU, SETUP_CPU = _tc["test"], _tc["setup"]
except Exception:  # noqa: BLE001
    TEST_CPU, SETUP_CPU = {}, {}
CPU_FACTOR_DEFAULT = 1.6
PER_TEST_CPU = 0.002    # unittest bookkeeping per test
BASE_OVERHEAD = 0.13    # fork + fresh patched sqlglot import (measured 0.113 s)
PER_FILE_OVERHEAD = 0.03
# Class-fixture CPU paid once per verification that runs any test of the file
# (measured: an executor test costs 2.0 s because setUpClass loads TPC-H/TPC-DS).
FILE_SETUP_CPU = {"tests/test_executor.py": 1.9, "tests/test_optimizer.py": 0.1}
MIN_GAIN = 0.15  # skip coin-flip verifications whose expected gain is within noise
TPCDS_ID = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
# P(test_execute_tpcds itself fails | the commit breaks something), for commits
# that reach it through few queries (labeled sets: 1 of 4); kept pessimistic.
P_TPCDS_GIVEN_BREAK = 0.6
try:
    import gzip as _gzip

    with _gzip.open(HERE.parent / "maps" / "querymap.json.gz", "rt") as _f:
        QMAP = json.load(_f)
    QMAP_COMMON = {rel: set(v) for rel, v in QMAP["common"].items()}
except Exception:  # noqa: BLE001
    QMAP, QMAP_COMMON = None, {}


def tpcds_subset(a) -> list[int] | None:
    """Queries of test_execute_tpcds that can observe this commit's edits, or None."""
    if QMAP is None or a.everything or a.other_facts or not a.runtime_lines:
        return None
    suite = QMAP["suites"]["tpcds"]
    out: set[int] = set()
    for rel, lines in a.runtime_lines.items():
        if lines & QMAP_COMMON.get(rel, set()):
            return None  # class setup / shared code: every query is affected
        per_line = suite["lines"].get(rel, {})
        for ln in lines:
            out.update(per_line.get(str(ln), ()))
    return sorted(out)


def log(*args) -> None:
    print(*args, file=sys.stderr, flush=True)


def duration_ms(seconds: float) -> int:
    return int(seconds * 1000 + 0.5)


def partition(numbers, dur: dict[int, int]) -> list[list[int]]:
    """Split tests over k<=8 workers minimising max load + 2 s per worker (LPT per k)."""
    nums = sorted(set(numbers), key=lambda n: (-dur[n], n))
    if not nums:
        return []
    best = None
    for k in range(1, min(MAX_WORKERS, len(nums)) + 1):
        loads = [0] * k
        bins: list[list[int]] = [[] for _ in range(k)]
        for n in nums:
            j = loads.index(min(loads))
            loads[j] += dur[n]
            bins[j].append(n)
        cost = max(loads) + WORKER_COST_MS * k
        if best is None or cost < best[0]:
            best = (cost, bins)
    return best[1]


def cost_of(workers, dur) -> int:
    ws = [w for w in workers if w]
    if not ws:
        return 0
    return max(sum(dur[n] for n in w) for w in ws) + WORKER_COST_MS * len(ws)


def category(a) -> str:
    text = " ".join(a.reasons)
    if "test file" in text:
        return "test"
    if "data " in text:
        return "data"
    if "runtime[" in text or "import-time" in text:
        return "runtime"
    if a.python_changed and a.candidates:
        return "definition"
    return "readers-only"


def plan_verification(a, tests_by_num, dur) -> dict | None:
    """Expected-cost comparison: run candidates vs select them blindly."""
    if a.everything or not a.candidates:
        return None
    if any(p is None for p in a.new_files.values()) or any(
        rel not in () and content is not None and not os.path.exists(os.path.join(SRC, rel))
        for rel, content in a.new_files.items()
    ):
        return None  # added/removed files: listings would differ; keep static answer
    cands = sorted(a.candidates)
    files = {tests_by_num[n]["file"] for n in cands}
    blind = cost_of(partition(cands, dur), dur) / 1000
    tpcds_num = next((n for n in cands if tests_by_num[n]["id"] == TPCDS_ID), None)
    subset = tpcds_subset(a) if tpcds_num is not None else None

    def test_cpu(n):
        if n == tpcds_num and subset is not None:
            return 1.15 * sum(QMAP["suites"]["tpcds"]["cpu"][i] for i in subset) + PER_TEST_CPU
        tid = tests_by_num[n]["id"]
        if tid in TEST_CPU:
            return 1.1 * TEST_CPU[tid] + PER_TEST_CPU
        return CPU_FACTORS.get(tests_by_num[n]["file"], CPU_FACTOR_DEFAULT) * tests_by_num[n]["seconds"] + PER_TEST_CPU

    def file_setup(f):
        return SETUP_CPU[f] if f in SETUP_CPU else FILE_SETUP_CPU.get(f, 0.0)

    cpu = BASE_OVERHEAD + PER_FILE_OVERHEAD * len(files) + sum(file_setup(f) for f in files) + sum(
        test_cpu(n) for n in cands)
    p = P_BREAK[category(a)]
    if tpcds_num is not None:
        rest = [n for n in cands if n != tpcds_num]
        blind_rest = cost_of(partition(rest, dur), dur) / 1000
        light = min(blind_rest, 2.0 + 0.3 * max(0.0, blind_rest - 2.0)) if rest else 2.0
        exp_fail_cost = P_TPCDS_GIVEN_BREAK * blind + (1 - P_TPCDS_GIVEN_BREAK) * light
    else:
        exp_fail_cost = min(blind, 2.0 + 0.3 * max(0.0, blind - 2.0))
    expected = cpu + p * exp_fail_cost
    if blind - expected < MIN_GAIN:
        return None
    return {"cands": cands, "blind": blind, "cpu": cpu, "gain": blind - expected,
            "tpcds_subset": subset if tpcds_num is not None else None}


def main() -> int:
    t0 = time.monotonic()
    tests = json.loads((DATA / "tests.json").read_text("utf-8"))["tests"]
    tests_by_num = {t["number"]: t for t in tests}
    dur = {t["number"]: duration_ms(t["seconds"]) for t in tests}
    full = partition([t["number"] for t in tests], dur)
    patches = sorted((DATA / "commits").glob("*.patch"))
    n = len(patches)
    results: dict[str, list[list[int]]] = {}
    analyses = {}

    selector = None
    try:
        from static import DepMap, StaticSelector

        depmap = DepMap(MAP, tests)
        profile = None
        if os.path.exists(CMP_PROFILE):
            try:
                from equiv import CmpProfile

                profile = CmpProfile(CMP_PROFILE)
            except Exception as e:  # noqa: BLE001
                log(f"comparison profile unavailable: {type(e).__name__}: {e}")
        selector = StaticSelector(depmap, SRC, tests, profile)
        log(f"map loaded in {time.monotonic() - t0:.2f}s; {len(depmap.unmapped)} tests not in map")
    except Exception as e:  # noqa: BLE001
        log(f"FATAL map load: {type(e).__name__}: {e}; running everything")

    for p in patches:
        cid = p.stem
        try:
            if selector is None:
                raise RuntimeError("no selector")
            a = selector.analyze(cid, p.read_text("utf-8", errors="replace"))
            analyses[cid] = a
            workers = full if a.everything else partition(a.candidates, dur)
            results[cid] = workers
            log(f"{cid}: static {len(a.candidates)} tests cost={cost_of(workers, dur) / 1000:.3f}s "
                f"{'EVERYTHING ' if a.everything else ''}| " + " ; ".join(a.reasons)[:300])
        except Exception as e:  # noqa: BLE001
            results[cid] = full
            log(f"{cid}: ERROR {type(e).__name__}: {e} -> everything")

    # Definition-time edits must leave their modules importable; otherwise every
    # importer fails, which the static rules cannot bound: select everything.
    # Commits that will be verified get their import check inside the
    # verification child; everything else is checked on its own first.
    planned = {}
    if VERIFY and analyses:
        try:
            planned = plan_all(analyses, tests_by_num, dur)
        except Exception as e:  # noqa: BLE001
            log(f"planning aborted: {type(e).__name__}: {e}")
            planned = {}
    # stdout must hold exactly one line per commit, so answers are printed once,
    # at the end. Verification only refines them.
    if planned:
        try:
            verify_all(analyses, results, tests_by_num, dur, n, t0, planned, full)
        except Exception as e:  # noqa: BLE001
            log(f"verification aborted: {type(e).__name__}: {e}")
    try:
        check_imports(analyses, results, full, t0)
    except Exception as e:  # noqa: BLE001
        log(f"import checks aborted: {type(e).__name__}: {e}; marking unchecked commits as everything")
        for cid, a in analyses.items():
            if a.import_check and not getattr(a, "import_ok", False):
                a.everything = True
                results[cid] = full

    out = sys.stdout
    for p in patches:
        out.write(json.dumps({"commit": p.stem, "workers": results.get(p.stem, full)}) + "\n")
    out.flush()
    total = sum(cost_of(results.get(p.stem, full), dur) for p in patches) / 1000
    log(f"done {n} commits in {time.monotonic() - t0:.2f}s; selected cost {total:.3f}s")
    return 0


def check_imports(analyses, results, full, t0) -> None:
    import verify

    for cid, a in analyses.items():
        if a.everything or not a.import_check or getattr(a, "import_ok", False):
            continue
        tc = time.monotonic()
        report = verify.import_check(SRC, a.new_files, a.import_check)
        if report is None or report.get("errors"):
            a.everything = True
            results[cid] = full
            log(f"{cid}: patched import FAILED {report and report.get('errors')} -> everything")
        else:
            a.import_ok = True
            log(f"{cid}: patched modules import ok ({time.monotonic() - tc:.2f}s): {sorted(a.import_check)}")


def plan_all(analyses, tests_by_num, dur) -> dict:
    out = {}
    for cid, a in analyses.items():
        plan = plan_verification(a, tests_by_num, dur)
        if plan:
            out[cid] = plan
    return out


def verify_all(analyses, results, tests_by_num, dur, n, t0, planned, full) -> None:
    import verify

    plans = sorted(((plan["gain"], cid, plan) for cid, plan in planned.items()), key=lambda x: -x[0])
    if not plans:
        return
    # Wall budget: the run limit is 3 min for the practice batch (~40 commits) and
    # 4.5 min for the final (~60). Stay well inside: ~1.6 s of wall per commit.
    # Hard limits: 180 s (practice, ~40 commits) / 270 s (final, ~60) for the batch;
    # static analysis takes a few seconds, two children run in parallel.
    budget_end = t0 + min(140.0, max(30.0, 2.5 * n))
    tv = time.monotonic()
    heavy_users = sum(1 for _, _, plan in plans
                      if any(tests_by_num[x]["file"] == "tests/test_optimizer.py" for x in plan["cands"]))
    verify.preload(heavy=heavy_users >= 2)
    log(f"preload {time.monotonic() - tv:.2f}s; {len(plans)} commits to verify")
    os.makedirs(TMP, exist_ok=True)
    pending = list(plans)
    jobs: list = []
    meta: dict = {}
    while pending or jobs:
        while pending and len(jobs) < PARALLEL:
            gain, cid, plan = pending.pop(0)
            remaining = budget_end - time.monotonic()
            est_wall = plan["cpu"] * 1.5 + 1.0
            if est_wall > remaining:
                log(f"{cid}: skip verify (budget)")
                continue
            a = analyses[cid]
            nodeids = [tests_by_num[x]["id"] for x in plan["cands"]]
            timeout = min(remaining, max(8.0, 4 * plan["cpu"] + 5))
            job = verify.start(cid, SRC, TMP, a.new_files, nodeids, timeout, sorted(a.import_check),
                               plan.get("tpcds_subset"))
            meta[cid] = plan
            jobs.append(job)
        if not jobs:
            break
        for job, report in verify.collect(jobs):
            cid = job.key
            plan = meta[cid]
            wall = time.monotonic() - job.started
            shutil.rmtree(os.path.join(TMP, cid), ignore_errors=True)
            if not report or not report.get("ok"):
                log(f"{cid}: verify FAILED ({report.get('error') if report else 'timeout/crash'}) "
                    f"after {wall:.2f}s -> static")
                continue
            if a_import_errors := report.get("import_errors"):
                analyses[cid].everything = True
                results[cid] = full
                log(f"{cid}: patched import FAILED in verification {a_import_errors} -> everything")
                continue
            analyses[cid].import_ok = True
            failed_ids = set(report["failed"])
            seen = set(report["seen"])
            id2num = {tests_by_num[x]["id"]: x for x in plan["cands"]}
            failing = {id2num[i] for i in failed_ids if i in id2num}
            unseen = {num for i, num in id2num.items() if i not in seen}
            for ce in report["collect_errors"]:
                failing |= {num for i, num in id2num.items() if i == ce or i.startswith(ce + "::")}
            chosen = failing | unseen
            results[cid] = partition(chosen, dur)
            if plan.get("tpcds_subset") is not None:
                log(f"{cid}: tpcds restricted to {len(plan['tpcds_subset'])} queries")
            top = sorted(report.get("times", {}).items(), key=lambda kv: -kv[1])[:4]
            log(f"{cid}: suite cpu {report.get('suite_cpu')}s (planned {plan['cpu']:.2f}s); top "
                + ", ".join(f"{k.split('::')[-1]}={v}" for k, v in top))
            log(f"{cid}: verified {len(plan['cands'])} in {wall:.2f}s -> {len(failing)} failing, "
                f"{len(unseen)} unseen; cost {plan['blind']:.3f}s -> {cost_of(results[cid], dur) / 1000:.3f}s")


if __name__ == "__main__":
    sys.exit(main())
