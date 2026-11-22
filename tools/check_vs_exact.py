"""Does a run still select every test the exact (run-everything) run found failing?
usage: check_vs_exact.py <tests.json> <run.jsonl> <exact.jsonl> <exact.log>"""
import json, re, sys
tj, rj, ej, el = sys.argv[1:]
tests = json.load(open(tj))["tests"]; dur = {t["number"]: int(t["seconds"] * 1000 + 0.5) for t in tests}
def load(p): return {json.loads(l)["commit"]: {x for w in json.loads(l)["workers"] for x in w} for l in open(p)}
def cost(p):
    out = {}
    for l in open(p):
        d = json.loads(l); ws = [w for w in d["workers"] if w]
        out[d["commit"]] = 0 if not ws else (max(sum(dur[n] for n in w) for w in ws) + 2000 * len(ws)) / 1000
    return out
R, E = load(rj), load(ej)
known = set()
for l in open(el):
    m = re.match(r"^(\w+): verified (\d+)/(\d+) \(held 0", l)
    if m: known.add(m.group(1))
    m = re.match(r"^(\w+): static 0 tests", l)
    if m: known.add(m.group(1))
miss = [(c, len(E[c] - R[c])) for c in sorted(known) if E[c] - R[c]]
cr, ce = cost(rj), cost(ej)
print(f"commits {len(R)}; exact known for {len(known)}; run cost {sum(cr.values()):.1f}s vs exact {sum(ce.values()):.1f}s; "
      f"POTENTIAL MISSES: {len(miss)} {miss[:10]}")
