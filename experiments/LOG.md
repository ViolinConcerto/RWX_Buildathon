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

- Sample (30 commits) oracle (exact failing set, best LPT split over k≤8): **94.35 s**. 12/30 commits break nothing (oracle 0). (Earlier draft said 15 — wrong; corrected from answers.json.)
- All tests every commit: k=3 → 1210 s, k=4 → 1104 s (best), k=8 → 1345 s.
- Practice leaderboard top at time of reading: 121.8 s on ~40 commits ⇒ leaders are near-oracle; one miss (300 s) is decisive.

## E1 — can we reproduce the labels by running the suite? (H1)

- Lab image = official base digest + requirements.lock + sqlglot tarball @ eb11aec, SKIP_INTEGRATION=0.
- First attempt (xdist -n 4, no CPU pinning): 13 min, 4 spurious failures unpatched. Cause **[verified]**: tests create spawn ProcessPoolExecutors sized by os.cpu_count() = host's 24 even under --cpus; pools of 24 × xdist workers thrash.
- Fix: pin with --cpuset-cpus (process_cpu_count → 4). Unpatched run: 0 failures.
- Result **[verified]**: all 30 sample commits reproduce answers.json exactly (failing sets equal) when the suite runs pinned to 4 CPUs.

## Experiment table (official-flag runs via `make sample`-equivalent; CPU = container cgroup)

| Version | Change | Sample sim | CPU | Misses (sample/m1/s1) | Net vs prev | Risk |
| --- | --- | --- | --- | --- | --- | --- |
| v1 7e8d47e | static coverage selection | 170.684 | 1.10 | 0/–/– | — | **Practice: 216.5 s, 0 missed** |
| v2 152a33f | fork+pytest verification | 163.956 | 7.18 | 0/–/– | +0.7 | pytest session ~0.45 s/child |
| r2 71d42b7 | import closure, no-op defs, patched-import check | 170.684 | – | 0/0/– | mutants −107.7 | caught a NameError miss (m01112) |
| r9 4f8321f | is↔== proofs, import-called code objects, unittest runner, calibrated planner | 111.179 | 5.68 | 0/0/0 | **−54.9** | 28/40 swap proofs, 0 false |
| r12 | AST text keys, tree reuse, import check inside verification child | 111.179 | 5.01 | 0/0/0 | −0.7 | outputs byte-identical to r9 static |

Mutant set m1 (120, own generator, full-suite labels): v1 logic 1056.8 s → r12 888.3 s simulated, 0 misses.
Swap set s1 (40 is/== swaps, 70% at provable sites): r12 105.8 s simulated, 0 misses; 28 proofs, none on a failing commit.

## E6 — where the remaining sample gap is (r12, oracle 94.351)

- 4d63 (Snowflake tokenizer NUMERIC_ESCAPES class attr, 816 class users): 14.3 s. Value-level dataflow needed; parked.
- 5y72: candidate set includes an executor test (setUpClass loads TPC data: 2.0 s CPU measured) -> verification not worth it.
- Remaining small exactness gaps (535z, bj7g, verification CPU): ~3 s.

## E7 — verification economics (H1) [verified]

- Cold fork child: import sqlglot 0.113 s; pytest session for 1 test 0.459 s vs unittest 0.147 s; executor test 2.0 s (class setup).
- Runner CPU / recorded seconds per file: 0.8–1.6 (optimizer 1.53); old CPU_FACTOR=1.6 overestimated small tests.
- Sample A/B (r4): verify off 122.00, on 118.63 -> +3.4 s net. Upper bound for small no-fail commits ~9.5 s.
- Priors (small candidate sets): sample runtime 5/8, data 10/10, definition 1/3; mutants are harsher (runtime 31/33) so priors follow the sample.

## E8 — per-query TPC-DS map (H3, partial execution) [measured, not shipped]

- Inline tpcds: 37.9 s CPU + 3.25 s class setup; per query ≤ 1.65 s.
- Changed-line query subsets are bimodal: ~57 queries (hot executor/optimizer paths) or 1–6.
- On m1, 3 commits would save ~12–18 s each, 1 would lose 8 s; on the sample, 0 (both tpcds commits fail with |S| ≥ 57).

## Round after practice r12 (201.4 s, 0 missed)

Practice fact: r12 saved 16.6 s simulated vs v1 on practice, vs 59.5 s on the sample (50.2 s of it from
two sample commits). Sample-based extrapolation is not trusted any more.

| Version | Change | Sample total | m1 total | s1 total | Misses | Practice |
| --- | --- | --- | --- | --- | --- | --- |
| r12 19c3581 | (scored) | 116.19 | 899.72 | 110.37 | 0/0/0 | **201.4 s, 0 missed** |
| probe 80690ac | r12 minus tpcds/max_nodes/generator_memory (measurement only) | 683.5 (2 misses) | – | – | – | pending |
| r13 344fcf6 | import-time execution not attributed to the lazily-importing test; unique keys for metaclass-built classes | 116.2 (static 120.443) | 882.1 sim | 110.3 sim | 0/0/0 | – |
| r14 | tpcds query-subset verification; per-test CPU calibration; cheap-failure runner; wall budget 2.5 s/commit | 115.85 | 864.36 | 110.27 | 0/0/0 | pending |

Mechanism evidence (pinned commit, not distribution): test_execute_tpcds executes 17.5% of covered sqlglot
runtime lines, test_generator_memory 15% (98% of which were lazy-import artifacts, fixed in r13).
tpcds per-line query subsets: 39% of lines reach <=5 queries, 46% reach >=36 (no partial gain there).
| probe2 73b9fdd | r14 minus heavy three (measurement only) | – | – | – | – | not submitted |
| r15 | generator_memory verifiable (measured 2.0 s, was estimated 10.7 s); pytest log/warning capture emulated in runner (r14 runner would have *missed* memory-test failures caused by logging/warnings: adversarial set); gc.freeze() of inherited selector heap in children (generator_memory 12 s -> 0.95 s) | 116.26 | 851.47 | 110.61 | 0/0/0 (+adv 0/2) | pending |

Practice: probe (r12 minus heavy) = 421.627 sim, 1 missed => heavy three cost 76.9 s of r12's 198.5 s; 1 commit needs one.
Practice: r14 = 178.7 s (172.387 sim + 6.3 CPU), 0 missed (r12 -22.7 s).

Practice: r15 = 178.0 s (172.387 sim — identical to r14 — + 5.6 CPU), 0 missed: generator_memory
was not a deciding factor on practice after r13.
Practice: probe3 (r15 minus heavy) = 415.574 sim, 1 missed => heavy three still cost 56.8 s of r15's
172.4 s; non-heavy remainder 115.6 s alone exceeds 100 s, so both buckets must shrink.

| r16 | predicate-sensitivity narrowing for operator edits (maps/predmap.json.gz, selector/predsens.py) | 116.83* | 861.42* | 49.30* | 0/0/0, o2 0, adv 0 | pending |

\* CPU inflated by concurrent labeling containers; simulated equals r15 on sample/m1/tp.
o2 (24 comparison swaps, full-suite labels, 13 breaking): rule applied to 20, narrowed 14
(e.g. 37 -> 2, 29 -> 1 candidates), 0 misses. o1 (39 and/or swaps, 37 breaking): applied 34,
narrowed 8 (231 -> 2, 104 -> 29), 0 misses. Official-flag runs: o1 538.1, o2 193.0, sample 115.79; 0 misses.

Decisions: optimizer per-fixture-entry partial verification parked (sample upside ~2.5 s once
4d63 is excluded; m1 bulk is executor hot paths). Fixture-comment equivalence parked (some
tests read raw text). Entry-level dict dependencies parked (metaclasses derive structures).

## r17 – r26 (practice board, official scorer)

| version | change | practice |
| --- | --- | --- |
| r16 | predicate-sensitivity narrowing | 176.3 s, 0 missed |
| r22 | supervisor always prints an answer for every commit (+ r17 A1, r19 TPC store, r20 max_nodes stand-in, r21 longest-candidates plan) | 174.4 s, 0 missed |
| r24 | import-state diff + per-test attribute reads for definition edits | 175.1 s, 0 missed (state diff on every definition commit: +CPU) |
| r25 | state diff only when definition rules add >= 3 s | 174.5 s (163.708 sim + 10.8 CPU), 0 missed |
| r26 3f9163b | conditional two-stage tpcds check (only when A1 would keep tpcds blindly); lazy executor sub-cases; fixture-filter leak fix | pending |

Class probes on r25 (measurement only, never final; class cost = r25 sim + 300 x misses - probe sim):

| probe | emptied / dropped | probe sim | misses | class cost | breaking |
| --- | --- | --- | --- | --- | --- |
| P2 | heavy + everything classes emptied | 988.607 | 3 | 75.1 s | 3 |
| P3 | blind_code class emptied | 1033.81 | 3 | 29.9 s | 3 |
| P5 576799a | tpcds dropped from heavy-class answers | 406.895 (CPU 11.6) | 1 | 56.8 s | 1 |

P5 => ~3 non-everything commits carry tpcds, 1 needs it: ~35-40 s recoverable. r25's A1 already drops
held tpcds when every cheaper candidate passes, so the waste is where a cheaper candidate fails.
Labeled sets: 19 such commits, tpcds truly fails in 11.
Blanket verification of blind non-heavy commits (g1/g2 analysis): net negative (CPU > saved sim).

r26 development notes: first draft missed o019 on o1 — the fixture-entry filter of the previous test
leaked into TestExecutor.setUpClass and restricted tpcds_sqls (latent in r25 too, never triggered
because tpcds was held there). Fixed in stopTest. Forced-check measurement (BR_COND_FORCE=1):
o017 25.45 -> 4.90 s for 1.7 s CPU (55 plans re-made, 0 changed); o026 25.45 -> 7.34 s for 16.8 s CPU;
o013 / o019 tpcds really fail (0.45 s / 5.9 s). Stage-1 cost model recalibrated in r27.
