## 1. Ablation matrix (sim / container CPU / misses per set; totals)

| variant | sample | m1 | s1 | o1 | o2 | tp | adv | total | total excl. o1 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| r22 b411eeb | 109.0+4.8 | 762.7+64.8 | 12.1+14.6 | 519.0+22.6 | 158.5+9.7 | 91.5+33.3 | 55.2+1.3 | 1859.1 (0 missed) | 1317.5 |
| r25 b685952 | 102.5+5.6 | 726.3+80.7 | 12.1+14.4 | 466.6+25.1 | 149.1+11.1 | 91.5+32.9 | 55.2+1.3 | 1774.4 (0 missed) | 1282.7 |
| r28 3d4fa64 (run 1) | 102.5+7.4 | 726.3+85.9 | 12.1+12.3 | 405.1+68.8 | 149.1+11.5 | 91.5+32.2 | 55.2+1.4 | 1761.2 (0 missed) | 1287.3 |
| r28 3d4fa64 (run 2) | 102.5+7.5 | 726.3+85.5 | 12.1+12.3 | 405.1+68.6 | 149.1+11.2 | 91.5+31.6 | 55.2+1.4 | 1759.9 (0 missed) | 1286.3 |
| r28 + logging (defaults) | 102.5+7.4 | 726.3+86.0 | 12.1+12.7 | 405.1+71.6 | 149.1+11.6 | 91.5+32.5 | 55.2+1.5 | 1765.1 (0 missed) | 1288.4 |
| r28  BR_TWO_STAGE=0 | 102.5+5.9 | 726.3+81.3 | 12.1+14.7 | 466.6+25.4 | 149.1+11.4 | 91.5+33.1 | 55.2+1.3 | 1776.4 (0 missed) | 1284.4 |
| r28  BR_STATEDIFF=0 | 109.0+6.8 | 762.7+69.9 | 12.1+12.4 | 457.5+66.2 | 158.5+10.1 | 91.5+31.5 | 55.2+1.3 | 1844.8 (0 missed) | 1321.1 |
| r28  both off | 109.0+4.9 | 762.7+64.5 | 12.1+14.7 | 519.0+22.5 | 158.5+10.0 | 91.5+32.5 | 55.2+1.4 | 1858.5 (0 missed) | 1317.0 |
| r28  BR_LAZY=0 | 102.5+7.9 | 726.3+96.9 | 12.1+12.5 | 405.1+70.1 | 149.1+11.3 | 91.5+31.5 | 55.2+1.3 | 1773.5 (0 missed) | 1298.3 |

## 2. Dynamic verification records (Aaud)

verified commits: 110; child CPU 153.5 s (planned 215.0); sim cost removed 344.1 s; tests excluded 608; net (removed - child CPU) +190.6 s

Worst 10 verifications by (sim removed - child CPU):

| set | commit | sim removed | child CPU | candidates -> selected | plan |
| --- | --- | --- | --- | --- | --- |
| o1 | o019 | 0.00 | 8.15 | 7 -> 7 | all |
| m1 | m01059 | 0.00 | 7.74 | 29 -> 11 | all |
| sample | 2jrq | 0.00 | 2.68 | 29 -> 26 | all |
| s1 | s07039 | 0.00 | 1.54 | 3 -> 3 | all |
| o1 | o003 | 0.00 | 1.02 | 1 -> 1 | all |
| o1 | o013 | 0.00 | 0.99 | 43 -> 43 | all |
| m1 | m01048 | 0.00 | 0.90 | 9 -> 9 | all |
| o1 | o021 | 0.00 | 0.90 | 25 -> 25 | all |
| m1 | m01065 | 0.00 | 0.76 | 61 -> 61 | all |
| m1 | m01078 | 0.00 | 0.74 | 61 -> 61 | all |

verifications with negative net: 76 of 110, total -43.0 s

Not verified, by reason:

-  59  no candidates
-  56  expected gain -# < # (cpu #, blind #)
-  12  expected gain # < # (cpu #, blind #)
-  12  expected gain -# < # (held plan, cpu #)
-  10  everything
-   2  verify failed: timeout/crash

tpcds checks: {'ran/fail': 4, 'cond_cpu_fail': 16.712, 'unconditional': 5, 'ran/pass': 3, 'cond_cpu_pass': 21.680999999999997, 'skipped': 1}

## 3. Where mis-selected cost comes from (Aaud)

| class | commits | final cost | wasted (final - ideal) | breaking |
| --- | --- | --- | --- | --- |
| heavy_unverified | 14 | 366.7 | 189.3 | 14 |
| everything | 10 | 368.2 | 186.3 | 8 |
| verified | 110 | 588.1 | 82.3 | 91 |
| blind_code | 26 | 123.1 | 56.0 | 22 |
| blind_data | 42 | 95.8 | 23.6 | 32 |
| empty | 59 | 0.0 | 0.0 | 0 |

Top 15 commits by wasted cost:

| set | commit | class | final | wasted | cands (base/def) | def_everything | note / reason |
| --- | --- | --- | --- | --- | --- | --- | --- |
| m1 | m01033 | everything | 36.8 | 36.8 | 1620 (0/151) | True | everything |
| o2 | o2_019 | everything | 36.8 | 36.8 | 1620 (842/0) | True | everything |
| o2 | o2_010 | everything | 36.8 | 34.5 | 1620 (1517/0) | True | everything |
| o1 | o011 | everything | 36.8 | 30.7 | 1620 (1512/0) | True | everything |
| m1 | m01028 | heavy_unverified | 27.5 | 23.5 | 736 (0/3) | False | expected gain -11.33 < 0.15 (held plan, cpu 38.3) |
| o1 | o031 | heavy_unverified | 25.5 | 23.2 | 114 (114/0) | False | expected gain -3.27 < 0.15 (held plan, cpu 25.5) |
| o1 | o028 | heavy_unverified | 25.5 | 23.2 | 231 (231/0) | False | expected gain -6.26 < 0.15 (held plan, cpu 29.7) |
| m1 | m01114 | everything | 36.8 | 23.0 | 1620 (1/0) | False | everything |
| m1 | m01006 | heavy_unverified | 25.5 | 22.9 | 104 (104/0) | False | expected gain -0.06 < 0.15 (held plan, cpu 21.8) |
| tp | m01006 | heavy_unverified | 25.5 | 22.9 | 104 (104/0) | False | expected gain -0.06 < 0.15 (held plan, cpu 21.8) |
| o1 | o036 | verified | 24.8 | 22.4 | 53 (53/0) | False | sqlglot/executor/context.py:43 equivalence not proven: not an operator swap |
| m1 | m01086 | verified | 24.0 | 21.9 | 53 (0/53) | False | sqlglot.executor.env.['ENV'] module-level -> 53 (derived []) |
| m1 | m01065 | verified | 25.5 | 19.6 | 61 (61/0) | False | data tests/fixtures/optimizer/tpc-h/tpc-h.sql |
| o1 | o035 | heavy_unverified | 25.5 | 19.4 | 271 (271/0) | False | expected gain -6.46 < 0.15 (held plan, cpu 29.9) |
| adv | advwarn | heavy_unverified | 27.6 | 18.9 | 1347 (1347/0) | False | expected gain -18.38 < 0.15 (held plan, cpu 50.8) |

## 4. Rule attribution for answers that were not verified (Aaud)

| answer built from | commits | final cost | added by definition rules | runtime/fallback width over ideal | breaking |
| --- | --- | --- | --- | --- | --- |
| runtime coverage only | 73 | 485.1 | 0.0 | 199.0 | 60 |
| everything | 10 | 368.2 | 0.0 | 186.3 | 8 |
| runtime + definition readers | 7 | 65.7 | 59.3 | 0.0 | 6 |
| definition -> everything | 2 | 34.7 | 0.0 | 21.9 | 2 |

'everything' reasons:
-   4  sqlglot/parser.py:# equivalence not proven: not an operator swap
-   1  sqlglot.errors.['highlight_sql']: read at import, no class users
-   1  sqlglot.dialects.hive:Hive.['COERCES_TO'] users&readers -> #
-   1  syntax error in sqlglot/typing/spark#.py
-   1  sqlglot/tokenizer_core.py:# equivalence not proven: not an operator swap
-   1  sqlglot/parser.py:# import-time code in ['_advance']
-   1  sqlglot/tokenizer_core.py:# import-time code in ['_scan_number']
