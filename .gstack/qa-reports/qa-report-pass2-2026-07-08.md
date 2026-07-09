# gstack-qa Report Pass 2: csv_to_dat

**Date:** 2026-07-08  
**Target:** CLI converter after Concordance DAT header-row fix  
**Tier:** Standard  
**Mode:** CLI-adapted gstack-qa  
**gstack:** v0.6.0.1

## Health score

| | Pass 1 (pre-header) | Pass 2 (post-header + fixes) |
|---|---|---|
| **Score** | 100 (then header gap found by user) | **100 / 100** |
| Unit tests | 31 | **33/33 PASS** |
| QA matrix | — | **28/28 PASS** |
| Findings | — | **0** |
| Case VOL001.dat | values only | header + 342 data, thorn `FE` |

## What Pass 2 found and fixed

| ID | Sev | Issue | Fix |
|---|---|---|---|
| QA2-001 | high | Header heuristic treated Bates IDs / `X,Y` as field names | Require ≥1 known Concordance name (BEGDOC/FILEPATH/…); reject Bates/path tokens |
| QA2-002 | high | Corrupt `.dct` field count silently ignored when DAT had its own header | Raise `ValueError` on `.dct` vs DAT field-count mismatch |
| QA2-003 | high | Empty (header-only) CSV round-trip FAIL — sole DAT header line read as data | `verify_roundtrip_csv` passes CSV header names through so header-only DAT is skipped correctly |

## Evidence

```
Ran 33 tests — OK
QA PASS 2: 28/28 passed, 0 findings
Case folder VOL001.dat: first row BEGDOC|ENDDOC|CODED|FILEPATH, 343 lines, FE thorn
Full sample: 342 records validate PASS, round-trip PASS
```

## Ship call

**READY.** Concordance header row is default; readers skip it; legacy `--no-header` still works; heuristic no longer eats Bates rows.
