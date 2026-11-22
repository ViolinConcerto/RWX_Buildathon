"""Blast Radius selector entry point.

Reads /data/commits/*.patch and /data/tests.json, prints one JSON line per commit
to stdout. Logs go to stderr. Any failure for a commit falls back to running
the whole suite for it, so a bug here costs seconds, never a miss.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

DATA = Path(os.environ.get("BR_DATA", "/data"))
SRC = os.environ.get("BR_SRC", "/opt/sqlglot")
MAP = os.environ.get("BR_MAP", str(HERE.parent / "maps" / "depmap.json.gz"))
WORKER_COST_MS = 2000
MAX_WORKERS = 8


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
    for k in range(1, MAX_WORKERS + 1):
        if k > len(nums):
            break
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


def main() -> int:
    t0 = time.monotonic()
    tests = json.loads((DATA / "tests.json").read_text("utf-8"))["tests"]
    dur = {t["number"]: duration_ms(t["seconds"]) for t in tests}
    all_tests = [t["number"] for t in tests]
    full = partition(all_tests, dur)
    patches = sorted((DATA / "commits").glob("*.patch"))
    results: dict[str, list[list[int]]] = {}

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
            cands = set(a.candidates) | (selector.map.unmapped if a.candidates else set())
            workers = full if a.everything else partition(cands, dur)
            results[cid] = workers
            log(f"{cid}: {len(cands)} tests cost={cost_of(workers, dur) / 1000:.3f}s "
                f"{'EVERYTHING ' if a.everything else ''}| " + " ; ".join(a.reasons)[:400])
        except Exception as e:  # noqa: BLE001
            results[cid] = full
            log(f"{cid}: ERROR {type(e).__name__}: {e} -> everything")

    out = sys.stdout
    for p in patches:
        out.write(json.dumps({"commit": p.stem, "workers": results.get(p.stem, full)}) + "\n")
    out.flush()
    log(f"done {len(patches)} commits in {time.monotonic() - t0:.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
