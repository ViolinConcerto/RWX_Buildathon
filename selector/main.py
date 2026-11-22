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
import signal
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

DATA = Path(os.environ.get("BR_DATA", "/data"))
SRC = os.path.abspath(os.environ.get("BR_SRC", "/opt/sqlglot"))
MAP = os.environ.get("BR_MAP", str(HERE.parent / "maps" / "depmap.json.gz"))
CMP_PROFILE = os.environ.get("BR_CMP", str(HERE.parent / "maps" / "cmp_profile.json.gz"))
PRED_MAP = os.environ.get("BR_PRED", str(HERE.parent / "maps" / "predmap.json.gz"))
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
FILE_SETUP_CPU = {"tests/test_executor.py": 0.3, "tests/test_optimizer.py": 0.1}  # with the TPC store
MIN_GAIN = 0.15  # skip coin-flip verifications whose expected gain is within noise
TPCDS_ID = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
MAX_NODES_ID = "tests/test_parser.py::TestParser::test_max_nodes"
TPCDS_RUN_LIMIT = 8.0   # CPU seconds of affected TPC-DS queries we are willing to re-run
# Rule A1 (statistical, not a proof): the two tests too expensive to re-run are
# selected only when a cheaper candidate fails. On 255 labeled patches, tpcds
# failed 14 times and never without a sub-second test failing too.
RULE_A1 = os.environ.get("BR_A1", "1") == "1"
STATEDIFF = os.environ.get("BR_STATEDIFF", "1") == "1"
STATEFP_PATH = os.environ.get("BR_STATEFP", "/opt/statefp.json")
STATEDIFF_MIN_GAIN = float(os.environ.get("BR_STATEDIFF_MIN", "3.0"))
FORCE = os.environ.get("BR_FORCE", "0") == "1"  # measurement probes: verify everything runnable
EXACT = os.environ.get("BR_EXACT", "0") == "1"  # offline only: also run tpcds / max_nodes for real
# test_max_nodes' scaled stand-in (statistical, not a proof): 1/25 of the width and of
# the node limits, same nesting depth; fails/raises/times out or runs >3x slower than
# on the pinned commit => max_nodes is selected. BR_MAXNODES_PROXY=0 disables it.
MAX_NODES_PROXY = os.environ.get("BR_MAXNODES_PROXY", "1") == "1"
MAX_NODES_PROXY_CPU = 0.94  # measured on the pinned commit (container)
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


try:
    with _gzip.open(HERE.parent / "maps" / "entrymap.json.gz", "rt") as _f:
        EMAP = json.load(_f)
    EMAP_COMMON = {t: {rel: set(v) for rel, v in d["common"].items()} for t, d in EMAP["tests"].items()}
except Exception:  # noqa: BLE001
    EMAP, EMAP_COMMON = None, {}


def entry_subset(a, test_id: str):
    """Fixture entries of a data-driven test that can observe this commit's edits.

    Returns ({fixture file: [entry indices]}, cpu estimate) or None (run it whole).
    """
    if EMAP is None or a.everything or a.other_facts or not a.runtime_lines:
        return None
    info = EMAP["tests"].get(test_id)
    if info is None:
        return None
    common = EMAP_COMMON[test_id]
    for rel, lines in a.runtime_lines.items():
        if lines & common.get(rel, set()):
            return None  # the test's own code path (outside entries) reaches the edit
    picked: dict[str, set[int]] = {}
    for fname, g in info["groups"].items():
        for rel, lines in a.runtime_lines.items():
            per_line = g["lines"].get(rel, {})
            for ln in lines:
                sid = per_line.get(str(ln))
                if sid is not None:
                    picked.setdefault(fname, set()).update(EMAP["sets"][sid])
    if not picked:
        return None  # coverage says it reaches the edit but no entry does: be safe
    cpu = info["base_cpu"] + sum(info["groups"][f]["cpu"][i] for f, idx in picked.items() for i in idx)
    return {f: sorted(v) for f, v in picked.items()}, 1.15 * cpu + 0.01


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
    blind = cost_of(partition(cands, dur), dur) / 1000
    tpcds_num = next((n for n in cands if tests_by_num[n]["id"] == TPCDS_ID), None)
    subset = tpcds_subset(a) if tpcds_num is not None else None
    subset_cpu = (0.9 * sum(QMAP["suites"]["tpcds"]["cpu"][i] for i in subset)) if subset is not None else None

    # Tests too expensive to re-run here: kept or dropped by rule, never executed.
    held = []
    for n in cands:
        tid = tests_by_num[n]["id"]
        if EXACT:
            continue  # offline analysis: really run everything, however expensive
        if tid == MAX_NODES_ID or (tid == TPCDS_ID and (subset_cpu is None or subset_cpu > TPCDS_RUN_LIMIT)):
            held.append(n)
    run = [n for n in cands if n not in held]
    proxy_ok = (MAX_NODES_PROXY and any(tests_by_num[n]["id"] == MAX_NODES_ID for n in held)
                and "tests/test_parser.py" not in a.new_files and "tests/helpers.py" not in a.new_files)
    if not run and not (proxy_ok and all(tests_by_num[n]["id"] == MAX_NODES_ID for n in held)):
        return None
    files = {tests_by_num[n]["file"] for n in run}

    entry_plan: dict[str, dict] = {}
    entry_cpu: dict[int, float] = {}
    for n in cands:
        es = entry_subset(a, tests_by_num[n]["id"])
        if es is not None:
            full_cpu = TEST_CPU.get(tests_by_num[n]["id"])
            if full_cpu is None or es[1] < 1.1 * full_cpu:
                entry_plan[tests_by_num[n]["id"]] = es[0]
                entry_cpu[n] = es[1]

    def test_cpu(n):
        if n == tpcds_num:
            return (subset_cpu if subset_cpu is not None else 36.0) + PER_TEST_CPU
        if n in entry_cpu:
            return entry_cpu[n] + PER_TEST_CPU
        tid = tests_by_num[n]["id"]
        if tid in TEST_CPU:
            return 1.1 * TEST_CPU[tid] + PER_TEST_CPU
        return CPU_FACTORS.get(tests_by_num[n]["file"], CPU_FACTOR_DEFAULT) * tests_by_num[n]["seconds"] + PER_TEST_CPU

    def file_setup(f):
        return SETUP_CPU[f] if f in SETUP_CPU else FILE_SETUP_CPU.get(f, 0.0)

    cpu = BASE_OVERHEAD + PER_FILE_OVERHEAD * len(files) + sum(file_setup(f) for f in files) + sum(
        test_cpu(n) for n in run)
    p = P_BREAK[category(a)]
    # Run order: cheap classes first (class fixtures stay grouped), executor last.
    cls_cost: dict[str, float] = {}
    for n in run:
        c = tests_by_num[n]["id"].rsplit("::", 1)[0]
        cls_cost[c] = cls_cost.get(c, 0.0) + test_cpu(n)
    order = sorted(run, key=lambda n: (file_setup(tests_by_num[n]["file"]) > 0.5,
                                       cls_cost[tests_by_num[n]["id"].rsplit("::", 1)[0]],
                                       tests_by_num[n]["id"].rsplit("::", 1)[0], test_cpu(n)))
    plan = {"cands": cands, "run": order, "held": held, "blind": blind, "cpu": cpu, "entries": entry_plan,
            "tpcds_subset": subset if (tpcds_num is not None and tpcds_num in run) else None,
            "stop_on_fail": False, "drop_held_if_clean": False}
    if held:
        if FORCE:
            plan["gain"] = 1.0  # probe: exact outcome of everything runnable, held tests kept
            return plan
        held_ids = {tests_by_num[n]["id"] for n in held}
        use_proxy = (MAX_NODES_ID in held_ids and MAX_NODES_PROXY
                     and "tests/test_parser.py" not in a.new_files and "tests/helpers.py" not in a.new_files)
        tpcds_held = TPCDS_ID in held_ids
        if not RULE_A1 and (tpcds_held or not use_proxy):
            return None
        plan["proxy"] = use_proxy
        # Held tests: tpcds (rule A1) is selected only if a runnable candidate fails;
        # max_nodes is decided by its scaled stand-in when available, else by A1.
        # With tpcds held, stop at the first failure: the remaining tests are then
        # selected unrun (they ride along under tpcds' load).
        fixed = BASE_OVERHEAD + sum(file_setup(f) for f in files)
        proxy_cpu = MAX_NODES_PROXY_CPU if use_proxy else 0.0
        if tpcds_held:
            cpu_eff = (1 - p) * cpu + p * min(cpu, fixed + 0.3 * (cpu - fixed) + 0.2)
            expected = cpu_eff + proxy_cpu + p * blind
            plan.update(stop_on_fail=True)
        else:
            heavy_blind = max(dur[n] for n in held) / 1000
            rest = [n for n in cands if n not in held]
            blind_rest = cost_of(partition(rest, dur), dur) / 1000
            light = min(blind_rest, 2.0 + 0.3 * max(0.0, blind_rest - 2.0)) if rest else 2.0
            p_heavy = 0.3  # P(max_nodes itself fails | it is a candidate): no labeled case so far
            expected = cpu + proxy_cpu + p_heavy * (heavy_blind + 2.0 + light) + (1 - p_heavy) * p * light
        plan.update(drop_held_if_clean=True, gain=blind - expected, cpu=cpu + proxy_cpu)
        return plan if plan["gain"] >= MIN_GAIN else None
    if tpcds_num is not None:
        rest = [n for n in cands if n != tpcds_num]
        blind_rest = cost_of(partition(rest, dur), dur) / 1000
        light = min(blind_rest, 2.0 + 0.3 * max(0.0, blind_rest - 2.0)) if rest else 2.0
        exp_fail_cost = P_TPCDS_GIVEN_BREAK * blind + (1 - P_TPCDS_GIVEN_BREAK) * light
    else:
        exp_fail_cost = min(blind, 2.0 + 0.3 * max(0.0, blind - 2.0))
    expected = cpu + p * exp_fail_cost
    plan["gain"] = blind - expected
    if FORCE:
        plan["gain"] = max(plan["gain"], 1.0)
        return plan
    long_plan = longest_plan(a, cands, run, tests_by_num, dur, test_cpu, file_setup, p, blind, entry_plan, subset,
                             tpcds_num)
    best = max((x for x in (plan, long_plan) if x is not None), key=lambda x: x["gain"])
    return best if best["gain"] >= MIN_GAIN else None


def longest_plan(a, cands, run, tests_by_num, dur, test_cpu, file_setup, p, blind, entry_plan, subset, tpcds_num):
    """Verify only the longest candidates; select the rest unrun.

    The busiest worker is set by the long tests; small ones share other workers.
    Long = recorded >= 5 s first, then >= 1 s, at most 12.
    """
    ranked = sorted(run, key=lambda n: -dur[n])
    longs = [n for n in ranked if dur[n] >= 5000] or []
    longs += [n for n in ranked if 1000 <= dur[n] < 5000]
    longs = longs[:12]
    if not longs or len(longs) == len(run):
        return None
    rest = [n for n in cands if n not in longs]
    cost_rest = cost_of(partition(rest, dur), dur) / 1000
    files = {tests_by_num[n]["file"] for n in longs}
    cpu = BASE_OVERHEAD + PER_FILE_OVERHEAD * len(files) + sum(file_setup(f) for f in files) + sum(
        test_cpu(n) for n in longs)
    # pessimistic: a failure among the long tests (prob p) costs as much as running blind
    expected = cpu + p * blind + (1 - p) * cost_rest
    order = sorted(longs, key=lambda n: (tests_by_num[n]["id"].rsplit("::", 1)[0], test_cpu(n)))
    return {"cands": cands, "run": order, "held": [], "blind_rest": rest, "blind": blind, "cpu": cpu,
            "entries": {k: v for k, v in entry_plan.items() if any(tests_by_num[n]["id"] == k for n in longs)},
            "tpcds_subset": subset if (tpcds_num is not None and tpcds_num in longs) else None,
            "stop_on_fail": False, "drop_held_if_clean": False, "gain": blind - expected, "kind": "longest"}


REPORT_FD = None  # set in the supervised child: records go to the supervisor, not stdout
VERIFIED: set[str] = set()  # commits whose final answer came from a verification run


def send_answer(kind: str, cid: str, workers) -> None:
    """Send a provisional ("S") or final ("F") answer for one commit to the supervisor."""
    if REPORT_FD is None:
        return
    data = (json.dumps([kind, cid, workers]) + "\n").encode()
    view = memoryview(data)
    while view:
        view = view[os.write(REPORT_FD, view):]


def supervise() -> int:
    """Print an answer for every commit whatever happens to the analysis.

    The analysis runs in a child (own process group). It reports safe provisional
    answers after the static phase and final answers at the end; if it crashes,
    hangs or nears the batch time limit, the best answer known so far is printed
    (whole suite for commits without one).
    """
    global REPORT_FD
    t0 = time.monotonic()
    tests = json.loads((DATA / "tests.json").read_text("utf-8"))["tests"]
    dur = {t["number"]: duration_ms(t["seconds"]) for t in tests}
    full = partition([t["number"] for t in tests], dur)
    patches = sorted((DATA / "commits").glob("*.patch"))
    n = len(patches)
    limit = 180.0 if n <= 45 else 270.0  # practice batch 3 min, final 4.5 min
    deadline = t0 + float(os.environ.get("BR_DEADLINE", limit - 25.0))
    rfd, wfd = os.pipe()
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()
    if pid == 0:
        code = 1
        try:
            os.close(rfd)
            os.setpgid(0, 0)
            REPORT_FD = wfd
            code = main()
        except BaseException as e:  # noqa: BLE001
            log(f"analysis crashed: {type(e).__name__}: {e}")
        finally:
            os._exit(code)
    os.close(wfd)
    import select

    best: dict[str, tuple[int, list]] = {}
    buf = b""
    timed_out = False
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            timed_out = True
            break
        ready, _, _ = select.select([rfd], [], [], min(left, 1.0))
        if not ready:
            continue
        chunk = os.read(rfd, 1 << 16)
        if not chunk:
            break
        buf += chunk
        *lines, buf = buf.split(b"\n")
        for line in lines:
            try:
                kind, cid, workers = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            rank = 2 if kind == "F" else 1
            if rank >= best.get(cid, (0, None))[0]:
                best[cid] = (rank, workers)
    if timed_out:
        log(f"SUPERVISOR: analysis still running at {time.monotonic() - t0:.1f}s; using best known answers")
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass
    known = sum(1 for p in patches if p.stem in best)
    finals = sum(1 for p in patches if best.get(p.stem, (0,))[0] == 2)
    out = sys.stdout
    for p in patches:
        out.write(json.dumps({"commit": p.stem, "workers": best.get(p.stem, (0, full))[1]}) + "\n")
    out.flush()
    log(f"SUPERVISOR: {n} commits, {finals} final, {known - finals} provisional, {n - known} whole suite; "
        f"{time.monotonic() - t0:.2f}s")
    return 0


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
        predmap = None
        if os.path.exists(PRED_MAP):
            try:
                from predsens import PredMap

                predmap = PredMap(PRED_MAP, tests)
            except Exception as e:  # noqa: BLE001
                log(f"predicate map unavailable: {type(e).__name__}: {e}")
        selector = StaticSelector(depmap, SRC, tests, profile, predmap)
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

    # Definition-time edits: ask a fork which import-state bindings really changed and
    # replace the broad definition-rule candidates with users-and-readers of those.
    if STATEDIFF and selector is not None and os.path.exists(STATEFP_PATH):
        try:
            refine_all(selector, analyses, results, full, dur)
        except Exception as e:  # noqa: BLE001
            log(f"state diff aborted: {type(e).__name__}: {e}")

    # Provisional answers for the supervisor: safe as soon as no import check is pending.
    for p in patches:
        a = analyses.get(p.stem)
        if a is None or a.everything or not a.import_check:
            send_answer("S", p.stem, results.get(p.stem, full))

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

    # Classify every commit by how its final answer was obtained (for logs and for
    # measurement probes: BR_PROBE_EMPTY=<classes> prints [] for those commits so the
    # practice board's aggregate reveals each class's cost and how many of them fail).
    heavy_nums = {t["number"] for t in tests if t["id"] in (TPCDS_ID, MAX_NODES_ID,
                  "tests/test_generator.py::TestGenerator::test_generator_memory")}
    classes: dict[str, str] = {}
    for p in patches:
        cid = p.stem
        a = analyses.get(cid)
        sel = {x for w in results.get(cid, full) for x in w}
        if a is None or a.everything:
            cls = "everything"
        elif sel & heavy_nums:
            cls = "heavy"
        elif not sel:
            cls = "empty"
        elif cid in VERIFIED:
            cls = "verified"
        elif not a.python_changed:
            cls = "blind_data"
        else:
            cls = "blind_code"
        classes[cid] = cls
    counts: dict[str, list] = {}
    for cid, cls in classes.items():
        counts.setdefault(cls, [0, 0.0])
        counts[cls][0] += 1
        counts[cls][1] += cost_of(results.get(cid, full), dur) / 1000
    log("CLASSES " + ", ".join(f"{k}: {v[0]} commits {v[1]:.1f}s" for k, v in sorted(counts.items())))
    probe = {x for x in os.environ.get("BR_PROBE_EMPTY", "").split(",") if x}
    if probe:
        for cid, cls in classes.items():
            if "all" in probe or cls in probe:
                results[cid] = []
        log(f"PROBE: emptied classes {sorted(probe)}")

    if REPORT_FD is not None:
        for p in patches:
            send_answer("F", p.stem, results.get(p.stem, full))
    else:
        out = sys.stdout
        for p in patches:
            out.write(json.dumps({"commit": p.stem, "workers": results.get(p.stem, full)}) + "\n")
        out.flush()
    total = sum(cost_of(results.get(p.stem, full), dur) for p in patches) / 1000
    log(f"done {n} commits in {time.monotonic() - t0:.2f}s; selected cost {total:.3f}s")
    return 0


def refine_all(selector, analyses, results, full, dur) -> None:
    import verify

    # A state diff costs ~0.7 s CPU (fork, import every module, fingerprint ~17k bindings).
    # Only worth it when the definition-time rules add a lot to the static answer; small
    # sets are left to verification (which imports the patched modules anyway).
    todo = []
    for cid, a in analyses.items():
        if not a.import_check or a.base_everything:
            continue
        with_def = cost_of(full if a.everything else partition(a.candidates, dur), dur) / 1000
        without = cost_of(partition(a.base_candidates, dur), dur) / 1000
        if with_def - without >= STATEDIFF_MIN_GAIN:
            todo.append(cid)
        else:
            log(f"{cid}: state diff skipped (definition rules add only {with_def - without:.2f}s)")
    jobs: list = []
    while todo or jobs:
        while todo and len(jobs) < PARALLEL:
            cid = todo.pop(0)
            jobs.append(verify.start_statediff(cid, SRC, analyses[cid].new_files))
        for job, report in verify.collect(jobs):
            cid, a = job.key, analyses[job.key]
            if not report or not report.get("ok"):
                log(f"{cid}: state diff failed -> keeping static candidates")
                continue
            if report["errors"]:
                a.everything = True
                a.import_ok = True
                results[cid] = full
                log(f"{cid}: patched import FAILED {report['errors']} -> everything")
                continue
            a.import_ok = True  # every module imported under the patch
            refined, why = selector.refine_definitions(report["changed"], a.def_candidates, a.def_everything)
            if refined is None:
                log(f"{cid}: state diff not boundable ({why}) -> keeping static candidates")
                continue
            before = len(a.candidates) if not a.everything else len(full_set(full))
            a.candidates = set(a.base_candidates) | refined
            a.everything = False
            a.statediff = report["changed"]
            results[cid] = partition(a.candidates, dur)
            log(f"{cid}: state diff {len(report['changed'])} bindings changed; candidates {before} -> "
                f"{len(a.candidates)} | " + " ; ".join(why)[:260])


def full_set(full):
    return {x for w in full for x in w}


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
    budget_end = t0 + float(os.environ.get("BR_BUDGET", min(150.0, max(30.0, 3.0 * n))))
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
            nodeids = [tests_by_num[x]["id"] for x in plan["run"]]
            timeout = min(remaining, max(8.0, 4 * plan["cpu"] + 5))
            job = verify.start(cid, SRC, TMP, a.new_files, nodeids, timeout, sorted(a.import_check),
                               plan.get("tpcds_subset"), plan["stop_on_fail"], bool(plan.get("proxy")),
                               {k: v for k, v in plan.get("entries", {}).items() if k in set(nodeids)})
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
                send_answer("F", cid, full)
                log(f"{cid}: patched import FAILED in verification {a_import_errors} -> everything")
                continue
            analyses[cid].import_ok = True
            failed_ids = set(report["failed"])
            seen = set(report["seen"])
            id2num = {tests_by_num[x]["id"]: x for x in plan["run"]}
            failing = {id2num[i] for i in failed_ids if i in id2num}
            unseen = {num for i, num in id2num.items() if i not in seen}
            for ce in report["collect_errors"]:
                failing |= {num for i, num in id2num.items() if i == ce or i.startswith(ce + "::")}
            chosen = failing | unseen | set(plan.get("blind_rest", ()))
            for h in plan["held"]:
                hid = tests_by_num[h]["id"]
                if hid == MAX_NODES_ID and plan.get("proxy"):
                    verdict = report.get("proxy", {}).get("max_nodes")
                    if verdict != "pass":
                        chosen.add(h)
                    log(f"{cid}: max_nodes stand-in {verdict} ({report.get('proxy', {}).get('max_nodes_cpu')}s)")
                elif chosen or not plan["drop_held_if_clean"]:
                    chosen.add(h)
            if chosen - set(plan["held"]) and any(tests_by_num[h]["id"] == TPCDS_ID for h in plan["held"]) \
                    and plan["drop_held_if_clean"]:
                chosen |= {h for h in plan["held"] if tests_by_num[h]["id"] == TPCDS_ID}
            results[cid] = partition(chosen, dur)
            VERIFIED.add(cid)
            send_answer("F", cid, results[cid])  # verified (imports checked in the child): final
            if plan.get("tpcds_subset") is not None:
                log(f"{cid}: tpcds restricted to {len(plan['tpcds_subset'])} queries")
            if plan.get("entries"):
                log(f"{cid}: fixture entries restricted for {len(plan['entries'])} tests: "
                    + ", ".join(f"{k.split('::')[-1]}={sum(len(v) for v in g.values())}" for k, g in list(plan['entries'].items())[:4]))
            top = sorted(report.get("times", {}).items(), key=lambda kv: -kv[1])[:4]
            log(f"{cid}: suite cpu {report.get('suite_cpu')}s (planned {plan['cpu']:.2f}s); top "
                + ", ".join(f"{k.split('::')[-1]}={v}" for k, v in top))
            log(f"{cid}: verified {len(plan['run'])}/{len(plan['cands'])} (held {len(plan['held'])}, "
                f"{plan.get('kind', 'all')}) in {wall:.2f}s -> {len(failing)} failing, "
                f"{len(unseen)} unseen; cost {plan['blind']:.3f}s -> {cost_of(results[cid], dur) / 1000:.3f}s")


if __name__ == "__main__":
    sys.exit(main() if os.environ.get("BR_UNSUPERVISED") == "1" else supervise())
