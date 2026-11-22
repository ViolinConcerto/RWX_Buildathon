"""Measure our verification runner's CPU per test file vs recorded seconds.
Runs inside the submission image. usage: python calibrate_cpu.py /data/tests.json > factors.json"""
import collections, json, os, sys
sys.path.insert(0, "/app/selector")
import verify  # noqa: E402

tests = json.load(open(sys.argv[1]))["tests"]
by_file = collections.defaultdict(list)
for t in tests:
    by_file[t["file"]].append(t)
SKIP = {"tests/test_executor.py"}
out = {}
verify.preload(heavy=True)
for f, ts in sorted(by_file.items()):
    if f in SKIP:
        continue
    ids = [t["id"] for t in ts if t["seconds"] < 5]
    if not ids:
        continue
    rec = sum(t["seconds"] for t in ts if t["seconds"] < 5)
    rfd, wfd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(rfd)
        try:
            verify._child("/opt/sqlglot", "/tmp/cal", {}, ids, 120, wfd)
        finally:
            os._exit(0)
    os.close(wfd)
    data = b""
    while chunk := os.read(rfd, 65536):
        data += chunk
    _, _, ru = os.wait4(pid, 0)
    cpu = ru.ru_utime + ru.ru_stime
    rep = json.loads(data or b"{}")
    out[f] = {"tests": len(ids), "recorded": round(rec, 3), "cpu": round(cpu, 3),
              "failed": len(rep.get("failed", [])), "seen": len(rep.get("seen", []))}
    print(f, out[f], file=sys.stderr, flush=True)
json.dump(out, sys.stdout, indent=0)
