# Concordance DAT Load File -- Format Reference

A **Concordance DAT file** is the de-facto document-level metadata load file
format used in legal eDiscovery. Each row represents one document (one BEGDOC
record) and carries the fielded metadata (Bates range, native file path,
custodian, dates, doc type, etc.) that review platforms such as Concordance,
Relativity, Reveal, and Summation import to build a review database.

DAT has **no encoding declaration**. Modern Concordance / Relativity / Reveal
productions put **field names on the first line** of the DAT (same thorn +
delimiter encoding as data rows). Relativity does not strictly require a header
but **strongly recommends** it; Concordance can load field names from the DAT
header; Reveal expects one. A companion **.dct** in this tool is only a
convenience name list (not Concordance's binary search dictionary). The format
relies on three rare single-byte control characters (chosen because they almost
never appear in real document text) plus a multi-value separator.

**Scope of this tool:** metadata DAT (+ a header-sidecar name list) only.
Image productions also need OPT or LFP load files; those are out of scope.

## Control characters (classic Concordance / Relativity defaults)

| Role | Glyph | ASCII | Hex | Notes |
|---|---|---|---|---|
| Field delimiter (column) | pilcrow | 020 | 0x14 | Separates fields; appears after every field, including the last. |
| Quote / text qualifier | thorn | 254 | 0xFE | Wraps every field value, even empty ones (empty = two thorns). |
| In-field newline | registered sign | 174 | 0xAE | Represents a newline inside a field value, keeping each record on one physical line. |
| Multi-value separator | semicolon | 059 | 0x3B | Relativity documented default. Older exports sometimes use ASCII 030 (0x1E); this tool rewrites 0x1E → `;` on write by default. |
| Nested-value separator | backslash | 092 | 0x5C | Denotes hierarchy within a multi-choice field (Relativity). |

## Record layout

**Header row (first line — recommended / expected by Concordance & Reveal):**

```
[thorn]BEGDOC[thorn][delim][thorn]ENDDOC[thorn][delim][thorn]CODED[thorn][delim][thorn]FILEPATH[thorn][delim]<newline>
```

**Data rows (one document per line):**

```
[thorn]DAVILLA_...000001[thorn][delim][thorn]DAVILLA_...000001[thorn][delim][thorn][thorn][delim][thorn]file.docx[thorn][delim]<newline>
```

* Each field is [thorn] + value + [thorn].
* Fields are joined by the delimiter [delim] (0x14).
* The record ends with a **trailing delimiter** followed by a real newline
  (\n, or \r\n on legacy Windows exports). The parser in this tool also
  tolerates the variant that omits the trailing delimiter.
* Every record in a load file must have the **same field count**. This tool
  pads short rows and rejects (or optionally truncates) overlong rows.
* Sources: Relativity load-file specs (header strongly recommended; field
  names on first line for processed data); Concordance "Load Field Names From
  DAT File"; Reveal AI DAT import (first line = header).

## Escaping

* A literal [thorn] (the quote char) **inside** a value is escaped by
  **doubling** it (two thorns represent one thorn), exactly like CSV doubles
  its quote char.
* A real newline (\n / \r\n / \r) inside a value is rewritten to the in-field
  newline char [registered] (0xAE) on write, and restored to \n on read.
* **Known format ambiguity:** a *literal* registered sign (®) in source data is
  indistinguishable from an encoded newline on the return trip and reads back
  as \n. This is inherent to the Concordance format — Concordance's own docs
  advise changing the newline delimiter (`--newline`) when case data contains ®.

## Encoding -- the thorn trap

DAT files carry no encoding marker. The thorn quote char (U+00FE) encodes
differently per code page:

| Encoding | thorn bytes | delim (0x14) | newline (0xAE) |
|---|---|---|---|
| CP1252 / Latin-1 | FE | 14 | AE |
| UTF-8 | C3 BE | 14 | C2 AE |

Relativity Desktop Client auto-detects encoding **from a byte order mark**, so
BOM'd UTF-8/UTF-16 DATs are common in the wild. This tool strips a leading BOM
on read (`utf-8` reads as `utf-8-sig`; `utf-16` consumes its own BOM) and, when
writing UTF-16, emits exactly one BOM at the start of the file. Classic cp1252
output is never BOM'd — the first byte is the thorn (`FE`).

A parser that assumes the wrong encoding will split fields in the wrong place
or see the whole file as one giant line. **This tool defaults to cp1252**
(classic Concordance single-byte thorn). Pass `--encoding utf-8` only when the
receiving platform is known to expect UTF-8. Always use the same encoding for
read and write to round-trip.

Characters that cannot encode into the chosen code page fail under the default
`--encoding-errors strict`. Use `replace` / `ignore` only when you accept data loss.

## Standard field names (commonly seen)

BEGDOC, ENDDOC, CUSTODIAN, DATES, DOCTYPE, FILEPATH / NATIVE / NATIVEFILE,
TEXT, FROM, TO, SUBJECT, BEGATTACH, ENDATTACH, PAGECOUNT, MD5HASH, etc.
These are not fixed by the format -- the import map / .dct defines them.
A volume-index CSV with only Bates + filename is a valid but thin load file;
production loads usually also carry custodian, dates, and a relative native path.

This tool ships `STANDARD_NATIVE_FIELDS` (17 common native-load columns) and
`--merge-schema` so a thin CSV can be projected onto that fuller schema with
blank values for every missing field (empty = `þþ`, never omitted).

## External sample alignment

Public samples used for structure checks (under `tests/fixtures/`):

| Sample | Source | Delimiters | Notes |
|---|---|---|---|
| `external/starter_sample.dat` | pmdenlinger/ediscovery-starter | `"…"` + `\|` | Teaching CSV-style DAT; not classic Concordance bytes. |
| `classic/starter_sample_classic.dat` | converted here | `þ` + `0x14` | Same 17 fields; blanks preserved as `þþ`. |
| `external/load_file_01.dat` | relativitydev/relativity-import-samples | UTF-8 BOM + `\|` | Relativity pipe load file (23 fields, many blanks). |
| `classic/relativity_load_file_01_classic.dat` | converted here | cp1252 `þ` + `0x14` | Classic Concordance encoding of the same rows. |
| `external/load_file_03.dat` | Relativity | mixed / incomplete quoting | Not a clean classic DAT; do not treat as a golden sample. |
| `external/Opticon_01.opt` | Relativity | CSV Opticon | Matches this tool's OPT column layout. |

VOL001 (Davila) lines up with classic Concordance: header row, trailing
delimiter, empty CODED as `þþ`, cp1252 single-byte thorn.

## This converter's .dct companion

A full Concordance CPL data dictionary is complex (field types, widths, etc.).
This tool writes a **header sidecar**: comment lines plus one field name per
line. That is enough to rebuild a CSV header on the return trip. It is **not**
a Concordance CPL dictionary. Relativity and Concordance import UIs map fields
by position/name and do not require this file.

## Opticon (.opt) companion

Image productions use a page-level Opticon load file alongside the DAT:

```
PAGEID,VOLUME,PATH,Y,,,PAGECOUNT
```

`Y` marks the first page of a document. This tool's `opt` subcommand writes a
**minimal** companion: one line per DAT record (document), path =
`{image_dir}/{BEGDOC}{ext}`. It does not discover real multi-page TIFF sets.

## Sources

* Concordance -- Managing Data Files (answercenter.ediscovery.co)
* Relativity -- Import/Export Load file specifications (help.relativity.com)
* Reveal AI -- DAT Import Instructions
* Hintyr -- What Is a Load File? DAT, OPT, and Modern Production Formats
