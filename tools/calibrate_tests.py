"""Per-test CPU of the verification runner on the pinned commit (passing runs).
Runs inside the submission image. usage: python calibrate_tests.py /data/tests.json > per_test.json"""
import collections, json, os, sys
sys.path.insert(0, "/app/selector")
import verify  # noqa: E402

tests = json.load(open(sys.argv[1]))["tests"]
by_file = collections.defaultdict(list)
for t in tests:
    by_file[t["file"]].append(t)
verify.preload(heavy=True)
out, setup = {}, {}
for f, ts in sorted(by_file.items()):
    ids = [t["id"] for t in ts if t["seconds"] < 5]
    if not ids:
        continue
    rfd, wfd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(rfd)
        try:
            verify._child("/opt/sqlglot", "/tmp/cal", {}, ids, 600, wfd)
        finally:
            os._exit(0)
    os.close(wfd)
    data = b""
    while chunk := os.read(rfd, 65536):
        data += chunk
    _, _, ru = os.wait4(pid, 0)
    rep = json.loads(data or b"{}")
    out.update(rep.get("times", {}))
    setup[f] = round(max(0.0, rep.get("suite_cpu", 0.0) - sum(rep.get("times", {}).values())), 3)
    print(f, len(ids), "suite", rep.get("suite_cpu"), "setup", setup[f], "failed", len(rep.get("failed", [])), file=sys.stderr, flush=True)
json.dump({"_doc": "verification-runner CPU per test (s) and per-file class/module setup (s), pinned commit",
           "test": out, "setup": setup}, sys.stdout)
