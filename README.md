# csv_to_dat

A robust, dependency-free Python 3 converter between **CSV** and **Concordance
DAT** (the de-facto document-level load file format used in legal eDiscovery).
Supports both directions and round-trip verification, with configurable
delimiters, quote characters, in-field newline handling, multi-value
separators, and text encoding.

See [`DAT_FORMAT.md`](DAT_FORMAT.md) for the authoritative format reference.

## Features

* **csv2dat** -- CSV to Concordance DAT, with an optional header-sidecar `.dct`
  field-name list (not a Concordance CPL dictionary).
* **dat2csv** -- DAT back to CSV, using a `.dct`, explicit field names, or
  inferred `FIELD1..FIELDn` headers.
* **roundtrip** -- verify CSV -> DAT -> CSV (or DAT -> CSV -> DAT) field-by-field
  and report any differences.
* **validate** -- semantic load checks: BEGDOC uniqueness, ENDDOC order,
  optional native-file existence.
* **opt** -- minimal Opticon `.opt` companion (one image line per document by
  default; paths derived from Bates + `--image-dir`).
* Classic Concordance defaults: delim `0x14`, quote `0xFE`, in-field newline
  `0xAE`, multi-value `;` (`0x3B`), encoding **`cp1252`** (single-byte thorn).
* Fixed field-count enforcement (pad short rows; reject or truncate overlong).
* Legacy `0x1E` multi-value separators inside values are rewritten to `;` on write.
* Every field wrapped in the quote char; empty fields become two consecutive
  quote chars; embedded quote chars are doubled; embedded newlines are encoded
  as `0xAE`.
* Streaming I/O -- rows are converted one at a time; multi-hundred-MB files do
  not need to be fully resident in memory.
* Standard library only -- no `pip install` required (Python 3.8+).
* Optional `pip install -e .` registers the `csv-to-dat` console script.

## Install

No dependencies. Just clone / copy the `csv_to_dat` package folder. Optional
test dependency: `pytest`.

```
# requirements.txt is empty of runtime deps; tests use pytest (optional)
```

## Usage (PowerShell)

**If your workspace root is this `csv_to_dat` folder** (typical in Cursor), use
`python .\\run.py` or `python .\\cli.py` — `python -m csv_to_dat` only works when
run from the **parent** directory:

```powershell
# From inside csv_to_dat\ (Cursor workspace root)
python .\run.py csv2dat `
  ".\tests\fixtures\VOL001_slice.csv" `
  ".\VOL001.dat" `
  --field-names BEGDOC,ENDDOC,CODED,FILEPATH

# From the parent of csv_to_dat\ (package on sys.path)
python -m csv_to_dat csv2dat `
  ".\csv_to_dat\tests\fixtures\VOL001_slice.csv" `
  ".\csv_to_dat\VOL001.dat"
```

```powershell
# CSV -> DAT (classic Concordance defaults: cp1252 + 0x14/0xFE/0xAE)
python .\run.py csv2dat `
  "G:\My Drive\GLG - Google Drive\Casedoxx\Code\csv_to_dat\samples\VOL001.csv" `
  ".\VOL001.dat"

# Override field names to standard Concordance names
python .\run.py csv2dat `
  ".\VOL001.csv" `
  ".\VOL001.dat" `
  --field-names BEGDOC,ENDDOC,CODED,FILEPATH

# Field names from a file (one per line; use when a name contains a comma)
python .\run.py csv2dat ".\VOL001.csv" ".\VOL001.dat" `
  --field-names-file ".\fields.txt"

# DAT -> CSV (headers auto-read from the companion .dct)
python .\run.py dat2csv ".\VOL001.dat" ".\VOL001_roundtrip.csv"

# Modern UTF-8 DAT (only when the receiving platform expects UTF-8)
python .\run.py csv2dat ".\VOL001.csv" ".\VOL001_utf8.dat" `
  --encoding utf-8

# Accept data loss for characters that cannot encode into cp1252
python .\run.py csv2dat ".\VOL001.csv" ".\VOL001.dat" `
  --encoding-errors replace

# Verify CSV -> DAT -> CSV (prints PASS/FAIL + diff counts)
python .\run.py roundtrip --direction csv2dat ".\VOL001.csv"

# Verify DAT -> CSV -> DAT
python .\run.py roundtrip --direction dat2csv ".\VOL001.dat"

# Validate Bates uniqueness / ENDDOC order
python .\run.py validate ".\VOL001.dat" --field-names BEGDOC,ENDDOC,CODED,FILEPATH

# Optional: require natives to exist on disk
python .\run.py validate ".\VOL001.csv" --field-names BEGDOC,ENDDOC,CODED,FILEPATH `
  --check-natives --natives-root ".\NATIVES"

# Write a minimal Opticon .opt companion (1 page/doc placeholders)
python .\run.py opt ".\VOL001.dat" ".\VOL001.opt" `
  --volume VOL001 --image-dir "IMAGES\001"
```

### Subcommand reference

| Subcommand | Args | Notes |
|---|---|---|
| `csv2dat` | `input` `output` | Writes `output` and a header-sidecar `.dct` (unless `--no-dct`). |
| `dat2csv` | `input` `output` | Headers from `--field-names` / `--field-names-file`, `--dct`, the companion `.dct`, or inferred. |
| `roundtrip` | `input` `--direction {csv2dat,dat2csv}` | Returns exit code 0 on PASS, 1 on FAIL. |
| `validate` | `input` | Bates uniqueness, ENDDOC order, optional native existence. Exit 0/1. |
| `opt` | `input.dat` `output.opt` | Minimal Opticon companion from BEGDOC values. |

### Common options (all subcommands)

| Option | Default | Meaning |
|---|---|---|
| `--delim` | `0x14` | Field delimiter (char or ASCII/Unicode code, e.g. `20`, `0x14`, `,`). |
| `--quote` | `0xFE` | Quote / text qualifier. |
| `--newline` | `0xAE` | In-field newline representation. |
| `--multival` | `0x3B` (`;`) | Multi-value separator (Relativity default). Legacy `0x1E` inside values is rewritten to this on write. |
| `--encoding` | `cp1252` | Text encoding for the DAT. Use `utf-8` only when the receiver expects it. |
| `--encoding-errors` | `strict` | `strict` / `replace` / `ignore` / `xmlcharrefreplace`. |
| `--field-count-mode` | `pad_reject` | `pad_reject` (pad short, reject overlong), `pad_truncate`, or `reject`. |
| `--no-normalize-multivalue` | off | Keep legacy `0x1E` separators inside values as-is. |
| `--crlf` | off | Use CRLF as the record terminator. |

`csv2dat` also accepts `--field-names`, `--field-names-file`, `--no-dct`,
`--csv-encoding` (default utf-8 for the source CSV), `--csv-delim`.
`dat2csv` also accepts `--field-names`, `--field-names-file`, and `--dct PATH`.

## Field-name mapping for the sample volume index

The sample `VOL001.csv` header is:

```
Bates/Control #,End Bates/Control #,Coded,Filename
```

A sensible mapping to standard Concordance names (pass via `--field-names`):

```
Bates/Control #      -> BEGDOC
End Bates/Control #  -> ENDDOC
Coded                -> CODED
Filename             -> FILEPATH
```

The converter stays generic, though: any CSV header works, and the original
header names are written to the `.dct` when `--field-names` is omitted.
`--field-names` is a **positional rename** when the name count matches the CSV
column count. To project a thin CSV onto a fuller production schema (keeping
every target field, blank when missing), use `--merge-schema`:

```powershell
python .\run.py csv2dat .\VOL001.csv .\VOL001_full.dat `
  --field-names-file .\schemas\standard_native.txt `
  --merge-schema
```

Missing columns (CUSTODIAN, BEGATTACH, MD5, …) are written as empty Concordance
fields (`þþ`). Unmapped CSV columns and alias collisions are **warned on stderr**;
add `--strict-merge` to fail instead of dropping metadata silently.

## Inspecting a DAT (see the control characters)

Because the control characters are non-printing, open the file in a hex viewer,
or use PowerShell to render an escaped view of the first record:

```powershell
$bytes = [System.IO.File]::ReadAllBytes(".\VOL001.dat")
$first = $bytes[0..200]
($first | ForEach-Object {
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

A classic cp1252 DAT should start with byte `FE` (not UTF-8 `C3 BE`).

## Tests

```powershell
python -m pytest tests
# or without pytest:
python tests\test_roundtrip.py
# local CI smoke (unit + CLI validate/opt/roundtrip):
powershell -File .\scripts\ci_test.ps1
```

Optional install (registers `csv-to-dat` on PATH):

```powershell
pip install -e .
csv-to-dat --help
```

The test suite copies a small slice of the real sample into
`tests/fixtures/VOL001_slice.csv` so the tests are self-contained and do not
depend on the live case path.

## Project layout

```
csv_to_dat/
  __init__.py        # package exports
  __main__.py        # `python -m csv_to_dat` entry point
  cli.py             # argparse CLI: csv2dat / dat2csv / roundtrip
  converter.py       # core read/write/round-trip library (stdlib only)
  DAT_FORMAT.md      # Concordance DAT format reference
  README.md          # this file
  requirements.txt   # runtime: none (stdlib only); tests: pytest
  tests/
    test_roundtrip.py
    fixtures/
      VOL001_slice.csv
```

## Caveats / assumptions

* **Encoding defaults to cp1252.** Classic Concordance / many Relativity loads
  expect a single-byte thorn (`FE`). UTF-8 writes `C3 BE` and will break those
  loaders. Pass `--encoding utf-8` only when you know the receiver wants it.
* **DAT header row is on by default.** The first line of the `.dat` is
  Concordance/Relativity field names (thorn-wrapped). Use `--no-header` only
  for legacy no-header DATs.
* **`.dct` is a header sidecar**, not a full Concordance CPL data dictionary
  (no field types/widths). Relativity maps fields in the import UI.
* **Fixed field count.** Short rows are padded; overlong rows are rejected by
  default (`--field-count-mode pad_truncate` to drop extras).
* **Multi-value:** Relativity default is `;`. Legacy `0x1E` inside values is
  rewritten to `;` on write unless `--no-normalize-multivalue`.
* The sample's `Coded` column is empty; it is preserved as an empty field
  (two consecutive quote chars in the DAT).
* Embedded newlines inside a field are encoded as `0xAE` on write and restored
  to `\n` on read.
* Records end with the **trailing delimiter + newline** convention. The parser
  also tolerates the no-trailing-delimiter variant.
* This tool can emit a **minimal Opticon `.opt`** (one line per document by
  default). It does **not** invent multi-page TIFF breakouts from natives, and
  it does not write LFP. Use `--pages-per-doc` only for intentional placeholders.
* **`validate`** also checks numeric ENDDOC ordering (PREFIX_9 < PREFIX_10),
  overlapping Bates ranges between documents, and BEGATTACH family integrity
  (every BEGATTACH must match a BEGDOC in the same load).
* `--field-names` is positional; run **`validate`** to check Bates uniqueness
  and ENDDOC order after mapping.
