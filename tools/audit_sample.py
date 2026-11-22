#!/usr/bin/env python3
"""Read an RWX Blast Radius sample JSONL output and decompose official score.

Only works with PUBLIC sample labels. This is an offline diagnostic tool;
it does not create or look up patch-specific answers inside a submission.

Usage:
    python rwx_sample_score_audit.py --repo . --output work/output.jsonl \
      --csv analysis/sample_breakdown.csv
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import math
import sys
from pathlib import Path


def load_official_scorer(repo: Path):
    path = repo / "starter" / "score.py"
    spec = importlib.util.spec_from_file_location("rwx_official_score", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def partition_minimum_lpt(selected: set[int], durations: dict[int, int]):
    """Best of 1..8 LPT schedules, same heuristic as candidate project."""
    if not selected:
        return 0
    items = sorted(selected, key=lambda n: (-durations[n], n))
    best = math.inf
    for k in range(1, min(8, len(items)) + 1):
        loads = [0] * k
        for n in items:
            j = loads.index(min(loads))
            loads[j] += durations[n]
        best = min(best, max(loads) + 2000 * k)
    return best


def breakdown(repo: Path, output_path: Path, csv_path: Path):
    scorer = load_official_scorer(repo)
    sample = repo / "starter" / "sample"
    tests = json.loads((sample / "tests.json").read_text())['tests']
    answers = json.loads((sample / "answers.json").read_text())['commits']
    stdout = output_path.read_bytes()
    scored = scorer.evaluate_run(stdout, tests, answers)
    if not scored.get("ok"):
        raise ValueError(f"Official sample scorer rejected output: {scored}")

    duration = {t['number']: scorer._duration_milliseconds(t['seconds']) for t in tests}
    test_ids = {t['number']: t['id'] for t in tests}
    test_files = {t['number']: t['file'] for t in tests}
    records = []
    for line in stdout.decode().splitlines():
        item = json.loads(line)
        commit = item['commit']
        workers = [w for w in item['workers'] if w]
        selected = set(n for w in workers for n in w)
        fails = set(answers[commit]['failing'])
        loads = [sum(duration[n] for n in w) for w in workers]
        busiest = max(loads, default=0)
        overhead = len(workers) * 2000
        miss = int(not fails.issubset(selected))
        oracle_ms = partition_minimum_lpt(fails, duration)
        cost_ms = busiest + overhead + miss * 300000
        records.append({
            'commit': commit,
            'kind_public_debug_only': answers[commit].get('kind', ''),
            'selected_tests': len(selected),
            'selected_test_files': len(set(test_files[n] for n in selected)),
            'failing_tests': len(fails),
            'extra_tests_vs_public_oracle': len(selected - fails),
            'misses': miss,
            'worker_count': len(workers),
            'busiest_ms': busiest,
            'worker_overhead_ms': overhead,
            'score_ms': cost_ms,
            'oracle_lpt_ms': oracle_ms,
            'gap_vs_oracle_ms': cost_ms - oracle_ms,
        })
    records.sort(key=lambda r: -r['gap_vs_oracle_ms'])
    if sum(r['score_ms'] for r in records) != scored['simulated_milliseconds']:
        raise AssertionError('Decomposition mismatch against official scorer')
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=records[0].keys())
        w.writeheader(); w.writerows(records)
    cols = ['busiest_ms', 'worker_overhead_ms', 'score_ms', 'oracle_lpt_ms']
    print('OFFICIAL SAMPLE:', scored)
    print('DECOMPOSITION (seconds):', {c: round(sum(r[c] for r in records)/1000,3) for c in cols})
    print('Top avoidable sample costs (only a diagnostic upper bound):')
    for r in records[:12]:
        print(f"  {r['commit']}: cost={r['score_ms']/1000:.3f}s  "
              f"oracle={r['oracle_lpt_ms']/1000:.3f}s  "
              f"gap={r['gap_vs_oracle_ms']/1000:.3f}s  "
              f"selected={r['selected_tests']} failing={r['failing_tests']} "
              f"workers={r['worker_count']} miss={r['misses']}")
    print('CSV:', csv_path.resolve())
    print('Caution: sample oracle/answers may ONLY be used for offline evaluation;')
    print('never ship any patch-specific answer, mapping, or detector in Docker.')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--repo', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--csv', type=Path, default=Path('analysis/sample_breakdown.csv'))
    a = ap.parse_args()
    breakdown(a.repo, a.output, a.csv)


if __name__ == '__main__':
    main()
