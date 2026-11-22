"""Where does the selector overspend? Compare a normal run with an exact (run-everything) run.
usage: gap_report.py <tests.json> <kinds.txt> <normal.jsonl> <normal.log> <exact.jsonl> <exact.log>"""
import collections, json, re, sys
tj, kinds_f, nj, nl, ej, el = sys.argv[1:]
tests = json.load(open(tj))["tests"]; dur = {t["number"]: int(t["seconds"] * 1000 + 0.5) for t in tests}
name = {t["number"]: t["id"].split("::")[-1] for t in tests}
def cost(ws):
    ws = [w for w in ws if w]
    return 0 if not ws else (max(sum(dur[n] for n in w) for w in ws) + 2000 * len(ws)) / 1000
def load(p): return {json.loads(l)["commit"]: json.loads(l)["workers"] for l in open(p)}
N, E = load(nj), load(ej)
kinds = dict(l.split(" ", 1) for l in open(kinds_f).read().split("\n") if l)
static, verified, everything, exact_ver = {}, {}, set(), set()
for l in open(nl):
    m = re.match(r"^(\w+): static (\d+) tests cost=([\d.]+)s (EVERYTHING )?\| (.*)$", l)
    if m:
        static[m.group(1)] = (int(m.group(2)), float(m.group(3)), m.group(5))
        if m.group(4): everything.add(m.group(1))
    m = re.match(r"^(\w+): verified (\d+)/(\d+) \(held (\d+), (\w+)\)", l)
    if m: verified[m.group(1)] = (int(m.group(2)), int(m.group(3)), int(m.group(4)))
    m = re.match(r"^(\w+): patched import FAILED", l)
    if m: everything.add(m.group(1))
for l in open(el):
    m = re.match(r"^(\w+): verified (\d+)/(\d+) \(held (\d+)", l)
    if m: exact_ver.add(m.group(1))
def rule(reason):
    if "users(" in reason or "users&readers" in reason or "module-level" in reason or "no instances" in reason or "class header" in reason: return "definition"
    if "import-time" in reason: return "import-time code"
    if "runtime[" in reason or "predicate-sensitive" in reason: return "runtime"
    if "data " in reason or "test file" in reason: return "data/test"
    return "none"
rows = []
for c in N:
    n_cost, e_cost = cost(N[c]), cost(E[c])
    st = static.get(c, (0, 0.0, ""))
    path = "everything" if c in everything else ("verified" if c in verified and verified[c][2] == 0 else ("verified+held" if c in verified else ("empty" if st[0] == 0 else "blind")))
    heavy = max((dur[x] for w in N[c] for x in w), default=0)
    rows.append(dict(c=c, kind=kinds.get(c, "?").split("-")[0].split(":")[0], rule=rule(st[2]), path=path, n=n_cost, e=e_cost,
                     gap=n_cost - e_cost, heavy=heavy, cands=st[0], exact_known=c in exact_ver or st[0] == 0, reason=st[2][:100]))
tot_n = sum(r["n"] for r in rows); tot_e = sum(r["e"] for r in rows)
print(f"commits={len(rows)} normal={tot_n:.1f}s ({tot_n/len(rows):.2f}/commit) exact={tot_e:.1f}s ({tot_e/len(rows):.2f}/commit) gap={tot_n-tot_e:.1f}s; "
      f"exact unknown (fallback, not verified): {sum(1 for r in rows if not r['exact_known'])} commits costing {sum(r['n'] for r in rows if not r['exact_known']):.1f}s")
def table(key, title):
    g = collections.defaultdict(lambda: [0, 0.0, 0.0])
    for r in rows:
        k = key(r); g[k][0] += 1; g[k][1] += r["n"]; g[k][2] += r["gap"]
    print(f"-- by {title}")
    for k, (cnt, n, gap) in sorted(g.items(), key=lambda kv: -kv[1][2]):
        print(f"   {str(k):34s} commits={cnt:3d} cost={n:7.1f}s gap={gap:7.1f}s ({100*gap/max(1e-9,tot_n-tot_e):3.0f}% of gap)")
table(lambda r: r["kind"], "patch kind")
table(lambda r: (r["path"], r["rule"]), "decision path / rule")
table(lambda r: "heavy>=20s" if r["heavy"] >= 20000 else ("heavy 5-20s" if r["heavy"] >= 5000 else ("heavy 1-5s" if r["heavy"] >= 1000 else "light")), "heaviest selected test")
print("-- top gaps")
for r in sorted(rows, key=lambda r: -r["gap"])[:14]:
    print(f"   {r['c']} gap={r['gap']:6.2f} n={r['n']:6.2f} e={r['e']:6.2f} {r['path']:13s} cands={r['cands']:4d} | {r['reason']}")
json.dump(rows, open(nj.replace(".jsonl", "_gap.json"), "w"))
