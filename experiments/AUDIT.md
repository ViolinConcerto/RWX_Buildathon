# Ablation audit (2026-11-22, after r28 = 175.1 s on practice)

Same 7 sets for every variant: public sample (30 real commits) + 6 labeled mutant sets.
Totals are sim + full container CPU over all sets; "excl. o1" drops o1, the set the r27/r28
cost constants were tuned on. Local noise: identical builds differ by 1.3-3.6 s on totals.

| variant | total | excl. o1 | sample | misses |
| --- | --- | --- | --- | --- |
| r22 b411eeb | 1859.1 | 1317.5 | 113.8 | 0 |
| r25 b685952 | 1774.4 | 1282.7 | 108.1 | 0 |
| r28 3d4fa64 (2 runs) | 1760.6 | 1286.8 | 109.9 | 0 |
| r28 BR_TWO_STAGE=0 | 1776.4 / 1772.8 | 1284.4 / 1280.9 | 108.4 / 108.0 | 0 |
| r28 BR_STATEDIFF=0 | 1844.8 | 1321.1 | 115.7 | 0 |
| r28 both off | 1858.5 | 1317.0 | 113.9 | 0 |
| r28 BR_LAZY=0 | 1773.5 | 1298.3 | 110.4 | 0 |
| r28 two-stage off + verify every plan | 1846.0 | 1336.8 | 121.2 | 0 |
| **r29 f3b856e** (r25 + fixture-filter fix) | 1774.2 | 1282.7 | 108.0 | 0 |

Mechanism net gains (positive = helps):

| mechanism | all sets | excl. o1 | sample | practice | decision |
| --- | --- | --- | --- | --- | --- |
| import-state diff (r24/r25) | +82.1 | +32.6 | +5.5 | sim -2.3, total ~0 | keep |
| two-stage tpcds (r26-r28) | +15.8 (all from o1) | -2.4 | -1.5 | 0 (sim 163.708 unchanged) | roll back |
| lazy executor sub-cases | +8.4 with two-stage, ~0 without | | | | roll back with it |
| fixture-filter leak fix | 0 | 0 | 0 | – | keep (correctness) |
| verifying every plan (planner override) | -73.2 | -55.9 | -13.2 | – | planner rejections are right |

tpcds conditional check (r28, all sets): ran 7 times; excluded tpcds 3 times, all in o1
(-61.5 s sim, +21.7 s CPU); no change 4 times (o013, o019, 2jrq, m01059: +16.7 s CPU);
skipped once (nothing else failed). Practice: never changed an answer.

Verification overall (r28): 110 verified commits, sim removed 344.1 s, child CPU 153.5 s,
net +190.6 s; 76 of them individually negative (-43.0 s), the price of checking commits that
really break. Not verified: 59 no candidates, 80 expected gain below threshold, 10 everything,
2 timeouts (m01056, since r22: safe static fallback, ~16 s CPU each).

Wasted cost (final - ideal, ideal = truly failing tests only), r28 without two-stage:
- sample (real commits): 102.5 vs ideal 94.4 -> 8.1 s, almost all 4d63 (definition read of
  Snowflake.Tokenizer.NUMERIC_ESCAPES at tokenizer-core build -> 135 readers, 0 fail)
- mutant sets excl. o1: 390.6 s: everything 147.8 (7 commits), heavy unverified 123.5 (10),
  verified ride-along 59.8 (74), blind_code 35.9 (16), blind_data 23.6 (34)
- by rule (all sets, unverified answers): runtime-coverage width 199.0, everything fallback
  186.3, definition readers 59.3, definition -> everything 21.9
