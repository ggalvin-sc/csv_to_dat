# QA Report — Pass 4 (close-out to 100) — 2026-07-08

Target: csv_to_dat Concordance DAT ↔ CSV converter. This pass fixed every
remaining Medium/Low finding from countervoice pass 2 and QA pass 3.

## Fixed this pass

| ID | Severity | Finding | Fix |
|---|---|---|---|
| M1 | Medium | Config allowed newline/multivalue char to equal quote/delim (corruption path) | `DatConfig.__post_init__` now requires all four control chars pairwise distinct and rejects real newlines |
| M2 | Medium | `--merge-schema` bypassed `--field-count-mode` | Merge rows are coerced against the source header first: `reject`/`pad_reject` now raise on malformed rows; `pad_truncate` still trims |
| M3 | Medium | `validate` loaded entire CSV into memory | `validate_csv_file` streams rows through a generator |
| M4 | Medium | Round-trip verify hardcoded UTF-8 for source CSVs | `verify_roundtrip_csv(csv_encoding=...)` + `roundtrip --csv-encoding`; cp1252 accented-character round-trip tested |
| M5 | Medium | No Bates overlap / family checks; ENDDOC compare lexicographic only | Numeric-aware Bates compare (PREFIX_9 < PREFIX_10), overlapping-range detection, and BEGATTACH→BEGDOC family integrity errors |
| M6 | Medium | All-empty DAT record dropped on round-trip | Blank-line skip now only skips `[]` / whitespace-only single cells; explicit `,,,` rows are preserved |
| H2 (pass 2) | High | Header detection false-negative on foreign DATs with mostly custom names | Accept ≥2 well-known names when nothing looks data-like; uniqueness check rejects BEGDOC==ENDDOC one-pager rows |
| L2 | Low | OPT had no comma guard (silent column shift) | Commas rejected in volume/image_dir/image_ext/page ID |
| L3 | Low | `write_dat` empty-schema left partial artifacts before raising | Rows are peeked before the output file or `.dct` is touched |
| L4 | Low | Field names containing DAT control chars written silently | `write_dat` rejects names containing delim/quote/newline chars |

## Not a code item

- M8 (restricted Bates identifiers stored under a Google-Drive-synced folder)
  is a data-handling/workflow decision for the user, not a converter defect.
  Flagged in pass 2; no code change applicable.

## Verification

- Unit suite: **68/68 PASS** (18 new tests this pass: config validation ×3,
  merge field-count ×3, validation semantics ×5, hardening/header ×7).
- CI smoke script: PASS (csv2dat → dat2csv → validate → opt → roundtrip).
- Case data `VOL001.dat`: validate PASS under the new stricter checks
  (342 records, 0 errors, 0 warnings); round-trip PASS (0 field diffs).
- Classic fixtures (starter 17f, relativity 23f, full-schema 17f): all PASS.

## Health score: **100/100**

Every finding from countervoice pass 2 (C1–C2, H1–H3, M1–M7, L1–L6) and QA
pass 3 (BOM read, UTF-16 BOM-per-record, duplicate header names) is now fixed,
tested, or documented as an inherent format limitation (® newline ambiguity —
documented in DAT_FORMAT.md with the `--newline` workaround; L1 CSV-encoding
output choice is operator-selectable via `dat2csv` config).
