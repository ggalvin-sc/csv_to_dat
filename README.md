# csv_to_dat

A robust, dependency-free Python 3 converter between **CSV** and **Concordance
DAT** — the de-facto document-level load file format used in legal eDiscovery
(Concordance, Relativity, Logikcull, Nuix, and friends).

Converts in both directions, verifies round-trips field-by-field, validates
Bates/family semantics, merges thin CSVs onto full production schemas, and can
emit a minimal Opticon `.opt` companion. Standard library only — no
`pip install` required (Python 3.8+).

See [`DAT_FORMAT.md`](DAT_FORMAT.md) for the full format reference, including
control-character encoding, BOM handling, and known format ambiguities.

## Why this exists

Concordance DAT files are CSV-like but use non-printing control characters
instead of commas and double quotes:

| Role | Byte | Character |
|---|---|---|
| Field delimiter | `0x14` | DC4 (looks like `¶` in some viewers) |
| Text qualifier ("quote") | `0xFE` | `þ` thorn |
| In-field newline | `0xAE` | `®` registered sign |
| Multi-value separator | `0x3B` | `;` (Relativity default) |

Generic CSV tooling mangles these files. This tool reads and writes them
natively, with the classic **cp1252** encoding by default (single-byte thorn —
UTF-8 would write `C3 BE` and break older loaders).

## Features

- **`csv2dat`** — CSV to Concordance DAT, with an optional header-sidecar
  `.dct` field-name list.
- **`dat2csv`** — DAT back to CSV, using a `.dct`, explicit field names, or
  inferred `FIELD1..FIELDn` headers.
- **`roundtrip`** — verify CSV → DAT → CSV (or DAT → CSV → DAT) field-by-field
  and report differences. Exit code 0 on PASS, 1 on FAIL.
- **`validate`** — semantic load checks: BEGDOC uniqueness, numeric-aware
  ENDDOC ordering (`PREFIX_9 < PREFIX_10`), overlapping Bates-range detection,
  BEGATTACH family integrity, optional native-file existence.
- **`opt`** — minimal Opticon `.opt` companion (one image line per document by
  default; paths derived from Bates + `--image-dir`).
- **Schema merge** (`--merge-schema`) — project a thin CSV onto a fuller
  production schema by field name, keeping every target field (blank when
  missing). Conservative alias handling, dropped-column and collision warnings,
  and a `--strict-merge` fail-fast mode.
- **Correct BOM handling** — reads UTF-8-BOM DATs transparently; UTF-16 writes
  emit exactly one BOM at the start of the file; cp1252 output is BOM-free.
- **Streaming I/O** — rows are converted one at a time; multi-hundred-MB files
  never need to be fully resident in memory.
- Fixed field-count enforcement: pad short rows; reject or truncate overlong
  rows (`--field-count-mode`).
- Legacy `0x1E` multi-value separators inside values are rewritten to `;` on
  write (disable with `--no-normalize-multivalue`).
- Every field is wrapped in the quote char; empty fields become `þþ`; embedded
  quote chars are doubled; embedded newlines are encoded as `0xAE` and restored
  to `\n` on read.

## Install

```powershell
git clone https://github.com/ggalvin-sc/csv_to_dat.git
cd csv_to_dat

# Nothing to install — run directly:
python .\run.py --help

# Optional: register the `csv-to-dat` console script
pip install -e .
csv-to-dat --help
```

Runtime dependencies: **none** (standard library only). Tests optionally use
`pytest`.

## Quick start (PowerShell)

```powershell
# CSV -> DAT (classic Concordance defaults: cp1252 + 0x14/0xFE/0xAE)
python .\run.py csv2dat .\load.csv .\VOL001.dat

# Rename columns to standard Concordance names (positional)
python .\run.py csv2dat .\load.csv .\VOL001.dat `
  --field-names BEGDOC,ENDDOC,CODED,FILEPATH

# Project a thin CSV onto a full production schema (name-based merge)
python .\run.py csv2dat .\load.csv .\VOL001_full.dat `
  --field-names-file .\schemas\standard_native.txt `
  --merge-schema

# DAT -> CSV (headers auto-read from the companion .dct)
python .\run.py dat2csv .\VOL001.dat .\VOL001.csv

# Verify a lossless round-trip (exit 0 = PASS)
python .\run.py roundtrip --direction csv2dat .\load.csv
python .\run.py roundtrip --direction dat2csv .\VOL001.dat

# Validate Bates uniqueness, ENDDOC order, ranges, and families
python .\run.py validate .\VOL001.dat --field-names BEGDOC,ENDDOC,CODED,FILEPATH

# Optionally require natives to exist on disk
python .\run.py validate .\load.csv --field-names BEGDOC,ENDDOC,CODED,FILEPATH `
  --check-natives --natives-root .\NATIVES

# Write a minimal Opticon .opt companion
python .\run.py opt .\VOL001.dat .\VOL001.opt --volume VOL001 --image-dir "IMAGES\001"
```

`python -m csv_to_dat …` also works when the package folder's **parent** is on
`sys.path` (i.e., run it from the directory that contains `csv_to_dat\`).

## Subcommand reference

| Subcommand | Args | Notes |
|---|---|---|
| `csv2dat` | `input` `output` | Writes `output` plus a header-sidecar `.dct` (unless `--no-dct`). |
| `dat2csv` | `input` `output` | Headers from `--field-names` / `--field-names-file`, `--dct PATH`, the companion `.dct`, or inferred. |
| `roundtrip` | `input` `--direction {csv2dat,dat2csv}` | Field-by-field verification; exit 0/1. Accepts `--csv-encoding` for non-UTF-8 sources. |
| `validate` | `input` | Bates uniqueness, ENDDOC order, range overlap, family integrity, optional native existence. Exit 0/1. |
| `opt` | `input.dat` `output.opt` | Minimal Opticon companion from BEGDOC values. |

### Common options (all subcommands)

| Option | Default | Meaning |
|---|---|---|
| `--delim` | `0x14` | Field delimiter (char or code: `20`, `0x14`, `,`). |
| `--quote` | `0xFE` | Quote / text qualifier. |
| `--newline` | `0xAE` | In-field newline representation. |
| `--multival` | `0x3B` (`;`) | Multi-value separator (Relativity default). |
| `--encoding` | `cp1252` | DAT text encoding. Use `utf-8` only when the receiver expects it. |
| `--encoding-errors` | `strict` | `strict` / `replace` / `ignore` / `xmlcharrefreplace`. |
| `--field-count-mode` | `pad_reject` | `pad_reject` (pad short, reject overlong), `pad_truncate`, or `reject`. |
| `--no-normalize-multivalue` | off | Keep legacy `0x1E` separators inside values as-is. |
| `--crlf` | off | Use CRLF as the record terminator. |

`csv2dat` also accepts `--field-names`, `--field-names-file`, `--merge-schema`,
`--strict-merge`, `--no-dct`, `--csv-encoding` (source CSV, default utf-8), and
`--csv-delim`. All four control characters are validated to be pairwise
distinct before any file is written.

## Schema merge

`--field-names` is a **positional rename** when the name count matches the CSV
column count. To project a thin CSV onto a fuller production schema by *name*
(keeping every target field, blank when missing), add `--merge-schema`:

```powershell
python .\run.py csv2dat .\thin.csv .\full.dat `
  --field-names-file .\schemas\standard_native.txt `
  --merge-schema
```

- Source columns are matched to target fields case-insensitively, with a
  conservative alias map (e.g. `Bates/Control #` → `BEGDOC`, `Custodian Name`
  → `CUSTODIAN`). Distinct concepts such as `DOCID` vs `BEGDOC` or `Author`
  vs `FROM` are deliberately **not** merged.
- Missing target fields (CUSTODIAN, BEGATTACH, MD5, …) are written as empty
  Concordance fields (`þþ`).
- Unmapped source columns and alias collisions are **warned on stderr**. Add
  `--strict-merge` to fail instead of dropping metadata silently.
- `--field-count-mode` is honored on the source rows before projection.

A ready-to-use full schema ships in
[`schemas/standard_native.txt`](schemas/standard_native.txt).

## Inspecting a DAT

The control characters are non-printing, so use a hex viewer — or render an
escaped view of the first record in PowerShell:

```powershell
$bytes = [System.IO.File]::ReadAllBytes(".\VOL001.dat")
($bytes[0..200] | ForEach-Object {
  switch ($_) {
    0x14 { '[DELIM]' }
    0xFE { '[THORN]' }
    0xAE { '[NL]' }
    0x0A { '\n' }
    0x0D { '\r' }
    default { [char]$_ }
  }
}) -join ''
```

A classic cp1252 DAT starts with byte `FE` (not UTF-8 `C3 BE`).

## Tests

```powershell
python -m pytest tests
# or without pytest:
python tests\test_roundtrip.py
# local CI smoke (unit + CLI validate/opt/roundtrip):
powershell -File .\scripts\ci_test.ps1
```

The suite covers round-trip fidelity, encodings and BOM behavior, schema-merge
aliasing/collision/strict modes, field-count enforcement, header-detection
heuristics, Bates/family validation, and hardening edge cases. Fixtures under
`tests/fixtures/external/` exercise real-world Relativity-style DAT and
Opticon samples.

## Project layout

```
csv_to_dat/
  __init__.py           # package exports
  __main__.py           # `python -m csv_to_dat` entry point
  run.py                # direct entry point when the repo is your cwd
  cli.py                # argparse CLI: csv2dat / dat2csv / roundtrip / validate / opt
  converter.py          # core read/write/validate/merge library (stdlib only)
  DAT_FORMAT.md         # Concordance DAT format reference
  pyproject.toml        # optional editable install (`csv-to-dat` script)
  schemas/
    standard_native.txt # full native-production schema for --merge-schema
  scripts/
    ci_test.ps1         # local CI smoke test (PowerShell)
  tests/
    test_roundtrip.py
    fixtures/           # CSV slice, classic DAT fixtures, external samples
  .github/workflows/
    ci.yml              # GitHub Actions: unit tests + CLI smoke
```

## Caveats / assumptions

- **Encoding defaults to cp1252.** Classic Concordance and many Relativity
  loads expect a single-byte thorn (`FE`). Pass `--encoding utf-8` only when
  you know the receiver wants it; UTF-8-BOM DATs are read transparently.
- **DAT header row is on by default.** The first line is a
  Concordance/Relativity field-name row (thorn-wrapped). Use `--no-header`
  only for legacy no-header DATs. Header auto-detection on foreign DATs uses a
  heuristic — spot-check the first record of unfamiliar files.
- **`.dct` is a header sidecar**, not a Concordance CPL data dictionary (no
  field types/widths). Relativity maps fields in its import UI regardless.
- **Fixed field count.** Short rows are padded; overlong rows are rejected by
  default (`--field-count-mode pad_truncate` to drop extras instead).
- The Opticon writer is **minimal**: one line per document by default. It does
  not invent multi-page TIFF breakouts from natives and does not write LFP.
  Use `--pages-per-doc` only for intentional placeholders.
- The `®` (`0xAE`) byte is overloaded by the format itself: it means "newline"
  inside a field, so a literal registered-trademark character in data is
  indistinguishable from a line break. See `DAT_FORMAT.md` for details.

## License

No license file yet — all rights reserved unless one is added.
