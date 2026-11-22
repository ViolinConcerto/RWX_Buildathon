"""Audit report over the ablation matrix (work/audit_matrix.sh).

usage: python3 tools/audit_report.py
Reads work/<tag>_<set>.jsonl / .log for each variant tag, the labeled answers, and the
AUDIT lines of the instrumented build (tag Aaud and its ablations).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict

ROOT = "/home/jack/projects/rwx-blast-radius"
sys.path.insert(0, f"{ROOT}/selector")
os.environ.setdefault("BR_SRC", f"{ROOT}/work/sqlglot")
import main as M  # noqa: E402  (partition / cost_of only)

SETS = {"sample": "starter/sample", "m1": "work/sets/m1", "s1": "work/sets/s1", "o1": "work/sets/o1",
        "o2": "work/sets/o2", "tp": "work/sets/tp", "adv": "work/sets/adv"}
TUNED = {"o1"}  # used to calibrate r27/r28 cost constants: not independent for r28
VARIANTS = [("A22", "r22 b411eeb"), ("A25", "r25 b685952"), ("A28a", "r28 3d4fa64 (run 1)"),
            ("A28b", "r28 3d4fa64 (run 2)"), ("Aaud", "r28 + logging (defaults)"),
            ("Ants", "r28  BR_TWO_STAGE=0"), ("Ansd", "r28  BR_STATEDIFF=0"),
            ("Anbo", "r28  both off"), ("Anlz", "r28  BR_LAZY=0")]


def load_set(name):
    d = f"{ROOT}/{SETS[name]}"
    tests = json.load(open(f"{d}/tests.json"))["tests"]
    dur = {t["number"]: M.duration_ms(t["seconds"]) for t in tests}
    ans = {c: set(v["failing"]) for c, v in json.load(open(f"{d}/answers.json"))["commits"].items()}
    return tests, dur, ans


def score(tag, name):
    d = f"{ROOT}/{SETS[name]}"
    out = f"{ROOT}/work/{tag}_{name}.jsonl"
    if not os.path.exists(out):
        return None
    res = subprocess.run([sys.executable, f"{ROOT}/starter/score.py", d, f"{d}/answers.json", out],
                         capture_output=True, text=True).stdout
    ms, mm = re.search(r"Simulated\S*\s+([\d.]+)", res), re.search(r"Misses\S*\s+(\d+)", res)
    if not ms or not mm:
        return None  # still running / unreadable
    sim, miss = float(ms.group(1)), int(mm.group(1))
    cpu = 0.0
    for line in open(f"{ROOT}/work/{tag}_{name}.log"):
        if line.startswith("usage_usec"):
            cpu = int(line.split()[1]) / 1e6
    return sim, cpu, miss


def summary():
    print("## 1. Ablation matrix (sim / container CPU / misses per set; totals)\n")
    hdr = "| variant | " + " | ".join(SETS) + " | total | total excl. o1 |"
    print(hdr)
    print("|" + " --- |" * (len(SETS) + 3))
    rows = {}
    for tag, label in VARIANTS:
        cells, tot, tot_ind, miss = [], 0.0, 0.0, 0
        ok = True
        for name in SETS:
            r = score(tag, name)
            if r is None:
                ok = False
                cells.append("–")
                continue
            sim, cpu, m = r
            cells.append(f"{sim:.1f}+{cpu:.1f}" + (f" **{m}M**" if m else ""))
            tot += sim + cpu + 0 * m
            miss += m
            if name not in TUNED:
                tot_ind += sim + cpu
        rows[tag] = (tot, tot_ind, miss, ok)
        print(f"| {label} | " + " | ".join(cells) + f" | {tot:.1f} ({miss} missed) | {tot_ind:.1f} |")
    return rows


def audit_lines(tag, name):
    p = f"{ROOT}/work/{tag}_{name}.log"
    out = {}
    if not os.path.exists(p):
        return out
    for line in open(p):
        if line.startswith("AUDIT "):
            r = json.loads(line[6:])
            out[r["cid"]] = r
    return out


def verification_records(tag="Aaud"):
    print(f"\n## 2. Dynamic verification records ({tag})\n")
    tot = Counter()
    notes = Counter()
    stage = Counter()
    rows = []
    for name in SETS:
        tests, dur, ans = load_set(name)
        for cid, r in audit_lines(tag, name).items():
            if r["cls"] == "verified":
                gain = r["static"] - r["final"]
                tot["n"] += 1
                tot["child_cpu"] += r.get("child_cpu", 0.0)
                tot["planned_cpu"] += r.get("planned_cpu", 0.0)
                tot["gain"] += gain
                tot["excluded"] += r["n_cands"] - len(r["sel"])
                rows.append((gain - r.get("child_cpu", 0.0), name, cid, gain, r.get("child_cpu", 0.0),
                             r["n_cands"], len(r["sel"]), r.get("kind")))
                st = r.get("stage") or {}
                if "conditional" in st:
                    key = st["conditional"].split(" ")[0]
                    if key == "ran":
                        key += "/" + st.get("tpcds", "?")
                    stage[key] += 1
                    if key == "ran/pass":
                        stage["cond_cpu_pass"] += st.get("cpu", 0.0)
                    if key == "ran/fail":
                        stage["cond_cpu_fail"] += st.get("cpu", 0.0)
                elif st:
                    stage["unconditional"] += 1
            elif r.get("note"):
                notes[re.sub(r"[\d.]+", "#", r["note"])[:70]] += 1
    print(f"verified commits: {tot['n']}; child CPU {tot['child_cpu']:.1f} s (planned {tot['planned_cpu']:.1f}); "
          f"sim cost removed {tot['gain']:.1f} s; tests excluded {tot['excluded']}; "
          f"net (removed - child CPU) {tot['gain'] - tot['child_cpu']:+.1f} s\n")
    rows.sort()
    print("Worst 10 verifications by (sim removed - child CPU):\n")
    print("| set | commit | sim removed | child CPU | candidates -> selected | plan |")
    print("| --- | --- | --- | --- | --- | --- |")
    for net, name, cid, gain, cpu, nc, ns, kind in rows[:10]:
        print(f"| {name} | {cid} | {gain:.2f} | {cpu:.2f} | {nc} -> {ns} | {kind} |")
    neg = [x for x in rows if x[0] < 0]
    print(f"\nverifications with negative net: {len(neg)} of {len(rows)}, total {sum(x[0] for x in neg):.1f} s")
    print("\nNot verified, by reason:\n")
    for k, v in notes.most_common():
        print(f"- {v:3d}  {k}")
    print("\ntpcds checks:", dict(stage))


def waste_breakdown(tag="Aaud"):
    """Selected-but-passing cost, by how the commit's answer was obtained and by rule."""
    print(f"\n## 3. Where mis-selected cost comes from ({tag})\n")
    by_cls = defaultdict(lambda: [0, 0.0, 0.0, 0])
    detail = []
    for name in SETS:
        tests, dur, ans = load_set(name)
        for cid, r in audit_lines(tag, name).items():
            fail = ans.get(cid, set())
            ideal = M.cost_of(M.partition(sorted(fail), dur), dur) / 1000 if fail else 0.0
            waste = r["final"] - ideal
            c = by_cls[r["cls"]]
            c[0] += 1
            c[1] += r["final"]
            c[2] += waste
            c[3] += int(bool(fail))
            detail.append((waste, name, cid, r))
    print("| class | commits | final cost | wasted (final - ideal) | breaking |")
    print("| --- | --- | --- | --- | --- |")
    for k, (n, f, w, b) in sorted(by_cls.items(), key=lambda kv: -kv[1][2]):
        print(f"| {k} | {n} | {f:.1f} | {w:.1f} | {b} |")
    detail.sort(key=lambda x: -x[0])
    print("\nTop 15 commits by wasted cost:\n")
    print("| set | commit | class | final | wasted | cands (base/def) | def_everything | note / reason |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for w, name, cid, r in detail[:15]:
        why = r.get("note") or (r.get("reasons") or [""])[0]
        print(f"| {name} | {cid} | {r['cls']} | {r['final']:.1f} | {w:.1f} | {r.get('n_cands')} "
              f"({r.get('n_base')}/{r.get('n_def')}) | {r.get('def_everything')} | {str(why)[:90]} |")
    return detail


def rule_attribution(tag="Aaud"):
    """Unverified answers: how much of the static cost each rule family adds."""
    print(f"\n## 4. Rule attribution for answers that were not verified ({tag})\n")
    agg = defaultdict(lambda: [0, 0.0, 0.0, 0.0, 0])
    ev = Counter()
    for name in SETS:
        tests, dur, ans = load_set(name)
        for cid, r in audit_lines(tag, name).items():
            if r["cls"] in ("verified", "empty") or "static" not in r:
                continue
            fail = ans.get(cid, set())
            ideal = M.cost_of(M.partition(sorted(fail), dur), dur) / 1000 if fail else 0.0
            if r["cls"] == "everything":
                key = "everything"
                ev[re.sub(r"[\d]+", "#", (r.get("reasons") or ["?"])[0])[:80]] += 1
            elif r.get("def_everything"):
                key = "definition -> everything"
            elif r.get("n_def"):
                key = "runtime + definition readers"
            else:
                key = "runtime coverage only"
            a = agg[key]
            a[0] += 1
            a[1] += r["final"]
            a[2] += max(0.0, r["final"] - r.get("base", 0.0)) if key != "everything" else 0.0
            a[3] += max(0.0, min(r["final"], r.get("base", 0.0)) - ideal) if key != "everything" else r["final"] - ideal
            a[4] += int(bool(fail))
    print("| answer built from | commits | final cost | added by definition rules | runtime/fallback width over ideal | breaking |")
    print("| --- | --- | --- | --- | --- | --- |")
    for k, (n, f, d, w, b) in sorted(agg.items(), key=lambda kv: -kv[1][1]):
        print(f"| {k} | {n} | {f:.1f} | {d:.1f} | {w:.1f} | {b} |")
    print("\n'everything' reasons:")
    for k, v in ev.most_common(8):
        print(f"- {v:3d}  {k}")


if __name__ == "__main__":
    summary()
    verification_records()
    waste_breakdown()
    rule_attribution()
