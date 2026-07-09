# QA Report — Pass 3 (countervoice research + gstack-qa) — 2026-07-08

Target: csv_to_dat Concordance DAT ↔ CSV converter (CLI tool, no URL — gstack-qa
adapted: test → find → fix → verify). Tier: Standard.

## Research sources checked (countervoice)

| Source | What it specifies | Our compliance |
|---|---|---|
| Relativity Server 2024/2025 + RelativityOne load-file specs | Column 020, Quote 254, Newline 174, Multi-value 059, Nested 092; **RDC detects encoding via BOM**; header names in first line | PASS (BOM read fixed this pass) |
| Logikcull / Reveal transfer docs | **Header row required, unique field names**; single relative native path field | PASS (dup-name warning added this pass) |
| Lexbe Native Load File Spec | First line = headers; VOL1.DAT in LOADFILES | PASS |
| Nuix Appendix A (load file formats) | Concordance defaults 020/254/174; ASCII default, UTF-8 supported | PASS |
| Concordance Managing Data Files | Final carriage return required; ® newline customizable when data contains ® | PASS (docs updated for ® ambiguity) |
| Hintyr load-file guide | Delimiter-conflict warning; typical production header field list | PASS (quoted values round-trip embedded 0x14) |

## Bugs found and FIXED this pass

1. **HIGH — UTF-8 BOM DAT failed to parse** (`read_dat`): Relativity RDC keys
   encoding detection off the BOM, so BOM'd DATs are common. Parser saw U+FEFF
   where thorn expected → `DatParseError`. Fixed: `utf-8` reads as `utf-8-sig`
   plus first-chunk BOM strip; verified against a BOM'd sample.
2. **MED — UTF-16 write emitted a BOM per record** (`write_dat` used
   per-record `str.encode`). Fixed with a stateful incremental encoder: exactly
   one BOM at file start; utf-16 round-trip now passes.
3. **MED — duplicate header names written silently** (Logikcull requires
   unique names). Fixed: stderr warning listing duplicates.

## Probes that PASSED (no code change needed)

- Embedded 0x14 delimiter inside quoted value round-trips.
- Embedded thorn (doubling) round-trips under cp1252.
- Relativity nested multi-choice string `Hot\Really Hot\Super Hot; Look at Later`
  passes through byte-identical.
- CRLF terminator + final newline present (Concordance final-CR rule).
- cp1252 output starts with raw `FE` (never BOM'd).
- Case file `VOL001.dat`: validate PASS (0 errors / 0 warnings), round-trip
  PASS (342 records, 0 field diffs).
- Classic fixtures: starter (17f/2r), relativity_01 (23f/4r), full-schema
  slice (17f/15r) — all parse + round-trip with 0 diffs.

## Known limitation (documented, not fixable in-format)

- A literal ® (0xAE) in source data reads back as \n — inherent Concordance
  ambiguity; Concordance docs advise changing the newline delimiter. Now noted
  in DAT_FORMAT.md with the `--newline` workaround.

## Verification

- Unit suite: **50/50 PASS** (4 new: utf-8 BOM, utf-16 single-BOM, cp1252
  no-BOM, duplicate-header warning).
- CI smoke script: PASS (csv2dat → dat2csv → validate → opt → roundtrip).

## Health score

Before this pass: 92/100 (BOM ingestion + dup headers unhandled).
After: **98/100**. Remaining deductions: Medium findings from countervoice
pass 2 not yet fixed (config control-char collision validation, merge bypasses
field-count-mode, in-memory CSV validate, roundtrip csv-encoding).
