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
SRC = os.environ.get("BR_SRC", "/opt/sqlglot")
MAP = os.environ.get("BR_MAP", str(HERE.parent / "maps" / "depmap.json.gz"))
TMP = os.environ.get("BR_TMP", "/tmp/br")
VERIFY = os.environ.get("BR_VERIFY", "1") == "1"
PARALLEL = int(os.environ.get("BR_PARALLEL", "2"))
WORKER_COST_MS = 2000
MAX_WORKERS = 8

# Prior probability that a commit of this shape breaks at least one candidate.
P_BREAK = {"runtime": 0.55, "definition": 0.4, "data": 0.9, "test": 0.9, "readers-only": 0.1}
CPU_FACTOR = 1.6        # our in-process run CPU vs recorded seconds
PER_TEST_CPU = 0.004    # pytest bookkeeping per test
BASE_OVERHEAD = 0.18    # fork + fresh sqlglot import + pytest session
PER_FILE_OVERHEAD = 0.06
HEAVY_FILES = {"tests/test_executor.py"}  # spawn-pool tests; never re-run here


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
    if files & HEAVY_FILES:
        return None
    blind = cost_of(partition(cands, dur), dur) / 1000
    cpu = BASE_OVERHEAD + PER_FILE_OVERHEAD * len(files) + sum(
        CPU_FACTOR * tests_by_num[n]["seconds"] + PER_TEST_CPU for n in cands
    )
    p = P_BREAK[category(a)]
    exp_fail_cost = min(blind, 2.0 + 0.3 * max(0.0, blind - 2.0))
    expected = cpu + p * exp_fail_cost
    if expected >= blind:
        return None
    return {"cands": cands, "blind": blind, "cpu": cpu, "gain": blind - expected}


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
        selector = StaticSelector(depmap, SRC, tests)
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

    # Write the static answers first? No: stdout must hold exactly one line per
    # commit, so answers are printed once, at the end. Verification only refines.
    if VERIFY and analyses:
        try:
            verify_all(analyses, results, tests_by_num, dur, n, t0)
        except Exception as e:  # noqa: BLE001
            log(f"verification aborted: {type(e).__name__}: {e}")

    out = sys.stdout
    for p in patches:
        out.write(json.dumps({"commit": p.stem, "workers": results.get(p.stem, full)}) + "\n")
    out.flush()
    total = sum(cost_of(results.get(p.stem, full), dur) for p in patches) / 1000
    log(f"done {n} commits in {time.monotonic() - t0:.2f}s; selected cost {total:.3f}s")
    return 0


def verify_all(analyses, results, tests_by_num, dur, n, t0) -> None:
    import verify

    plans = []
    for cid, a in analyses.items():
        plan = plan_verification(a, tests_by_num, dur)
        if plan:
            plans.append((plan["gain"], cid, plan))
    if not plans:
        return
    plans.sort(key=lambda x: -x[0])
    # Wall budget: the run limit is 3 min for the practice batch (~40 commits) and
    # 4.5 min for the final (~60). Stay well inside: ~1.6 s of wall per commit.
    budget_end = t0 + min(150.0, max(20.0, 1.6 * n))
    tv = time.monotonic()
    verify.preload()
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
            job = verify.start(cid, SRC, TMP, a.new_files, nodeids, timeout)
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
            failed_ids = set(report["failed"])
            seen = set(report["seen"])
            id2num = {tests_by_num[x]["id"]: x for x in plan["cands"]}
            failing = {id2num[i] for i in failed_ids if i in id2num}
            unseen = {num for i, num in id2num.items() if i not in seen}
            for ce in report["collect_errors"]:
                failing |= {num for i, num in id2num.items() if i == ce or i.startswith(ce + "::")}
            chosen = failing | unseen
            results[cid] = partition(chosen, dur)
            log(f"{cid}: verified {len(plan['cands'])} in {wall:.2f}s -> {len(failing)} failing, "
                f"{len(unseen)} unseen; cost {plan['blind']:.3f}s -> {cost_of(results[cid], dur) / 1000:.3f}s")


if __name__ == "__main__":
    sys.exit(main())
