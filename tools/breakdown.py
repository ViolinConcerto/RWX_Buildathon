"""Per-commit cost attribution for a labeled set (offline diagnostics only).

usage: breakdown.py <set_dir> <output.jsonl> [selector.log] [--csv out.csv] [--top N]

Reconciles with the official scorer (starter/score.py evaluate_run) and splits
each commit's cost into busiest-worker load, worker overhead and miss penalty,
next to the label oracle (exact failing set, best LPT). Labels are only read
here, never by the selector.
"""

import argparse
import csv
import importlib.util
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def scorer():
    spec = importlib.util.spec_from_file_location("score", ROOT / "starter" / "score.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def lpt_cost(sel, dur):
    if not sel:
        return 0
    items = sorted(sel, key=lambda n: (-dur[n], n))
    best = None
    for k in range(1, min(8, len(items)) + 1):
        loads = [0] * k
        for n in items:
            j = loads.index(min(loads))
            loads[j] += dur[n]
        c = max(loads) + 2000 * k
        best = c if best is None or c < best else best
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("set_dir", type=Path)
    ap.add_argument("output", type=Path)
    ap.add_argument("log", type=Path, nargs="?")
    ap.add_argument("--csv", type=Path)
    ap.add_argument("--top", type=int, default=12)
    a = ap.parse_args()
    sc = scorer()
    tests = json.loads((a.set_dir / "tests.json").read_text())["tests"]
    answers = json.loads((a.set_dir / "answers.json").read_text())["commits"]
    out = a.output.read_bytes()
    ev = sc.evaluate_run(out, tests, answers)
    assert ev["ok"], ev
    dur = {t["number"]: sc._duration_milliseconds(t["seconds"]) for t in tests}
    reasons = {}
    verified = {}
    if a.log and a.log.exists():
        for line in a.log.read_text().splitlines():
            m = re.match(r"^(\w+): static (\d+) tests cost=([\d.]+)s (.*)$", line)
            if m:
                reasons[m.group(1)] = m.group(4)[:160]
            m = re.match(r"^(\w+): verified .*", line)
            if m:
                verified[m.group(1)] = line[len(m.group(1)) + 2:][:120]
    rows = []
    for line in out.decode().splitlines():
        item = json.loads(line)
        cid = item["commit"]
        ws = [w for w in item["workers"] if w]
        sel = {n for w in ws for n in w}
        fails = set(answers[cid]["failing"])
        busiest = max((sum(dur[n] for n in w) for w in ws), default=0)
        overhead = 2000 * len(ws)
        miss = int(not fails <= sel)
        cost = busiest + overhead + 300000 * miss
        oracle = lpt_cost(fails, dur)
        heaviest = max(sel, key=lambda n: dur[n]) if sel else None
        rows.append({
            "commit": cid, "kind_debug": answers[cid].get("kind", ""), "selected": len(sel),
            "failing": len(fails), "extra": len(sel - fails), "miss": miss, "workers": len(ws),
            "busiest_ms": busiest, "overhead_ms": overhead, "cost_ms": cost, "oracle_ms": oracle,
            "gap_ms": cost - oracle,
            "heaviest": (tests[heaviest]["id"].split("::")[-1] + f"({dur[heaviest]}ms)") if heaviest is not None else "",
            "heaviest_fails": int(heaviest in fails) if heaviest is not None else 0,
            "verified": verified.get(cid, ""), "reason": reasons.get(cid, ""),
        })
    assert sum(r["cost_ms"] for r in rows) == ev["simulated_milliseconds"], "reconciliation failed"
    rows.sort(key=lambda r: -r["gap_ms"])
    if a.csv:
        a.csv.parent.mkdir(parents=True, exist_ok=True)
        with a.csv.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    tot = lambda k: sum(r[k] for r in rows) / 1000  # noqa: E731
    print(f"commits={len(rows)} simulated={ev['simulated_milliseconds'] / 1000:.3f}s "
          f"misses={len(ev['missed_commits'])} | busiest={tot('busiest_ms'):.3f} overhead={tot('overhead_ms'):.3f} "
          f"oracle={tot('oracle_ms'):.3f} gap={tot('gap_ms'):.3f}")
    zero = [r for r in rows if not r["failing"]]
    print(f"zero-failure commits: {len(zero)}, their cost {sum(r['cost_ms'] for r in zero) / 1000:.3f}s; "
          f"commits whose heaviest selected test does not fail: "
          f"{sum(1 for r in rows if r['selected'] and not r['heaviest_fails'])}")
    for r in rows[: a.top]:
        print(f"  {r['commit']} gap={r['gap_ms'] / 1000:7.3f} cost={r['cost_ms'] / 1000:7.3f} "
              f"oracle={r['oracle_ms'] / 1000:6.3f} sel={r['selected']:4d} fail={r['failing']:3d} "
              f"w={r['workers']} heavy={r['heaviest']} | {r['reason'][:90]}")


if __name__ == "__main__":
    main()
