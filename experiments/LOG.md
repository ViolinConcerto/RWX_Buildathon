# Experiment log

Each entry: hypothesis → change → result (numbers) → decision / next.
Facts are marked **[verified]**, guesses **[guess]**, open questions **[open]**.

## Constraints (from official page + starter kit, 2026-11-21)

- Score = Σ commits (busiest worker recorded secs + 2 s × workers) + 300 s × missed commits + container CPU secs; middle of 3 runs. Lower wins.
- Practice set ≈ 40 commits **[verified: baseline 1614.1 / 40.349 s per all-tests commit]**; final ≈ 60, includes unseen kinds of change.
- Run: no network, 2 CPUs, 4 GB, 3 min whole batch (4.5 min final), read-only FS, 64 MB /tmp, user nobody, ≤256 pids, stdout ≤2 MB.
- Build: network, 4 cores, 8 GB, 10 min. Downloads only PyPI / Docker Hub / GitHub releases at fixed tag / sqlglot @ eb11aec.
- Repo ≤ 20 MB, public at submission, MIT, Dockerfile at top. 1 submission/hour. Cutoff Sun 2026-11-22 11:00 ET.
- Forbidden: patch→tests lookup tables. Allowed: maps from the pinned commit (coverage, imports, fixtures) and running tests at run time (CPU charged).

## E0 — floor and baselines [verified]

- Sample (30 commits) oracle (exact failing set, best LPT split over k≤8): **94.35 s**. 15/30 commits break nothing (oracle 0).
- All tests every commit: k=3 → 1210 s, k=4 → 1104 s (best), k=8 → 1345 s.
- Practice leaderboard top at time of reading: 121.8 s on ~40 commits ⇒ leaders are near-oracle; one miss (300 s) is decisive.

## E1 — can we reproduce the labels by running the suite? (H1)

- Lab image = official base digest + requirements.lock + sqlglot tarball @ eb11aec, SKIP_INTEGRATION=0.
- First attempt (xdist -n 4, no CPU pinning): 13 min, 4 spurious failures unpatched. Cause **[verified]**: tests create spawn ProcessPoolExecutors sized by os.cpu_count() = host's 24 even under --cpus; pools of 24 × xdist workers thrash.
- Fix: pin with --cpuset-cpus (process_cpu_count → 4). Unpatched run: 0 failures.
