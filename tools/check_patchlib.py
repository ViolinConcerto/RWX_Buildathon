"""Check selector/patchlib.py against `git apply` on every patch in a dir."""
import subprocess, sys, tempfile, shutil
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "selector"))
from patchlib import parse_patch, apply_change
repo = Path(sys.argv[1]).resolve(); pdir = Path(sys.argv[2])
bad = 0
for p in sorted(pdir.glob("*.patch")):
    text = p.read_text()
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "--", "."], check=True)
    r = subprocess.run(["git", "-C", str(repo), "apply", str(p.resolve())], capture_output=True, text=True)
    if r.returncode:
        print(p.stem, "git apply failed", r.stderr[:200]); bad += 1; continue
    for ch in parse_patch(text):
        subprocess.run(["git", "-C", str(repo), "show", f"HEAD:{ch.old_path}"], capture_output=True)
        old = subprocess.run(["git", "-C", str(repo), "show", f"HEAD:{ch.old_path}"], capture_output=True, text=True).stdout if ch.old_path else None
        mine = apply_change(ch, old)
        theirs = (repo / ch.new_path).read_text() if ch.new_path else None
        if mine != theirs:
            print(p.stem, ch.path, "MISMATCH"); bad += 1
subprocess.run(["git", "-C", str(repo), "checkout", "-q", "--", "."], check=True)
print("mismatches:", bad)
