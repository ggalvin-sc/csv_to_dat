"""
converter.py - Core library for the Concordance DAT <-> CSV converter.

MODULE PURPOSE
--------------
This module implements the reading and writing of Concordance DAT load files
(the de-facto eDiscovery document-level metadata exchange format) and converts
between DAT and CSV in both directions with round-trip fidelity.

It is deliberately dependency-free (Python 3.8+ standard library only) so it
can run on any Windows/PowerShell machine without a virtualenv.

PUBLIC API
----------
- DatConfig           : dataclass holding the configurable delimiter/quote/
                        newline/multivalue/encoding settings.
- read_csv(path, ...) : generator yielding (field_names, row) tuples from a CSV.
- write_dat(path, rows, field_names, config, ...): stream rows out as a DAT.
- csv_to_dat(in_csv, out_dat, config, field_names=None, emit_dct=True)
- read_dat(path, ...) : generator yielding record (list[str]) from a DAT.
- write_csv(path, records, field_names, ...): stream records out as a CSV.
- dat_to_csv(in_dat, out_csv, config, field_names=None, dct_path=None)
- verify_roundtrip_csv(in_csv, config, field_names=None) -> VerifyReport
- verify_roundtrip_dat(in_dat, config, field_names, ...)  -> VerifyReport
- read_dct(path) / write_dct(path, field_names)
- validate_load_records(field_names, rows, ...) -> ValidationReport
- write_opt(path, records, ...) / dat_to_opt(...)  Opticon image companion
- project_row(...) / analyze_schema_merge(...) / MergeReport / STANDARD_NATIVE_FIELDS
  schema-merge helpers (conservative aliases; collisions & dropped columns reported)

DESIGN NOTES
------------
* Every DAT field value is wrapped in the quote char, even when empty (so an
  empty field is two consecutive quote chars).
* The quote char is escaped inside a value by *doubling* it (the same
  convention CSV uses for its quote char).
* Embedded newlines (\\n / \\r\\n / \\r) inside a field value are rewritten to
  the DAT in-field newline character (default ASCII 174 / 0xAE) on write, and
  restored to \\n on read. This keeps each DAT record on a single physical
  line, which is the canonical Concordance layout.
* Legacy multi-value separators (ASCII 030 / 0x1E) inside field values are
  rewritten on write to the configured multi_value_sep (default ';', Relativity)
  unless normalize_multivalue is False.
* A record is: QUOTE val QUOTE DELIM QUOTE val QUOTE DELIM ... QUOTE val QUOTE
  DELIM NEWLINE  -- i.e. every field (including the last) is followed by the
  field delimiter, and the record ends with a newline. The parser is tolerant
  of the variant that omits the trailing delimiter before the newline.
* DAT files carry no encoding declaration and the thorn quote char encodes as
  a single byte (0xFE) under cp1252 but two bytes (C3 BE) under UTF-8. The
  encoding is therefore an explicit, required decision; callers must pass the
  same encoding for read and write to round-trip. Classic Concordance defaults
  to cp1252 (single-byte thorn).
* Row field counts are coerced to the schema length (pad short / reject or
  truncate overlong) so every DAT record has a fixed width.
* By default the first physical line of the DAT is a Concordance/Relativity
  header row: field names encoded with the same quote/delimiter as data.
  (Relativity strongly recommends this; Concordance can load field names
  from the DAT header; Reveal expects it.)
"""

from __future__ import annotations

import codecs
import csv
import io
import os
import re
import sys
from dataclasses import dataclass, field as dc_field
from typing import Generator, Iterator, List, Optional, Sequence, Tuple, Union


__all__ = [
    "DatConfig",
    "DatParseError",
    "VerifyReport",
    "ValidationReport",
    "read_csv",
    "write_dat",
    "csv_to_dat",
    "read_dat",
    "write_csv",
    "dat_to_csv",
    "verify_roundtrip_csv",
    "verify_roundtrip_dat",
    "read_dct",
    "write_dct",
    "validate_load_records",
    "write_opt",
    "dat_to_opt",
    "project_row",
    "analyze_schema_merge",
    "MergeReport",
    "STANDARD_NATIVE_FIELDS",
    "DEFAULT_CONFIG",
]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# How write paths reconcile a row's length with the declared field count.
# pad_reject: pad short rows with ""; raise on overlong rows (safe default).
# pad_truncate: pad short rows; drop extra trailing cells on overlong rows.
# reject: raise on any length mismatch.


@dataclass
class DatConfig:
    """Configurable delimiter set and encoding for DAT I/O.

    All control characters are stored as single-character Python strings so
    they can be concatenated into field text directly. Defaults match the
    classic Concordance / Relativity load-file specification:

        * field delimiter  = ASCII 020 (0x14)  "pilcrow"
        * quote / qualifier = ASCII 254 (0xFE)  "thorn"
        * in-field newline  = ASCII 174 (0xAE)  "registered sign"
        * multi-value sep   = ASCII 059 (0x3B)  semicolon (Relativity default)
        * record terminator = '\\n'  (a real newline; '\\r\\n' is accepted on
          read and can be selected on write via `record_terminator`).
        * text encoding     = 'cp1252'  (classic single-byte Concordance DAT;
          use 'utf-8' only when the receiving platform is known to expect it).
        * encoding_errors   = 'strict'  (fail on unencodable chars; set
          'replace' / 'ignore' only when you accept data loss).
        * field_count_mode  = 'pad_reject'  (pad short rows; reject overlong).
        * normalize_multivalue = True  (rewrite ASCII 0x1E to multi_value_sep
          on write so legacy RS-separated values become Relativity-safe).
    """

    field_delimiter: str = chr(0x14)
    quote_char: str = chr(0xFE)
    newline_char: str = chr(0xAE)
    multi_value_sep: str = ";"
    encoding: str = "cp1252"
    encoding_errors: str = "strict"
    record_terminator: str = "\n"
    field_count_mode: str = "pad_reject"
    normalize_multivalue: bool = True

    def __post_init__(self) -> None:
        # Guard against callers passing ints (convenient from the CLI) or
        # empty strings, which would make parsing ambiguous.
        for name in ("field_delimiter", "quote_char", "newline_char", "multi_value_sep"):
            v = getattr(self, name)
            if isinstance(v, int):
                setattr(self, name, chr(v))
            elif not isinstance(v, str) or len(v) != 1:
                raise ValueError(f"{name} must be a single character or int code, got {v!r}")
        # All four control characters must be pairwise distinct. A newline or
        # multivalue char equal to the quote/delimiter corrupts records: the
        # newline rewrite runs after quote-doubling, so an injected quote char
        # would land un-doubled and the record would not parse.
        chars = {
            "field_delimiter": self.field_delimiter,
            "quote_char": self.quote_char,
            "newline_char": self.newline_char,
            "multi_value_sep": self.multi_value_sep,
        }
        names_list = list(chars)
        for i, a in enumerate(names_list):
            for b in names_list[i + 1:]:
                if chars[a] == chars[b]:
                    raise ValueError(f"{a} and {b} must differ (both 0x{ord(chars[a]):02X})")
        for name, ch in chars.items():
            if ch in ("\n", "\r"):
                raise ValueError(f"{name} must not be a real newline character")
        if self.record_terminator not in ("\n", "\r\n"):
            raise ValueError("record_terminator must be '\\n' or '\\r\\n'")
        if self.encoding_errors not in ("strict", "replace", "ignore", "xmlcharrefreplace"):
            raise ValueError(
                "encoding_errors must be one of: strict, replace, ignore, xmlcharrefreplace"
            )
        if self.field_count_mode not in ("pad_reject", "pad_truncate", "reject"):
            raise ValueError(
                "field_count_mode must be one of: pad_reject, pad_truncate, reject"
            )

    # Convenience for the CLI / logging.
    def describe(self) -> str:
        def code(c: str) -> str:
            return f"0x{ord(c):02X}"
        return (
            f"delim={code(self.field_delimiter)} "
            f"quote={code(self.quote_char)} "
            f"newline={code(self.newline_char)} "
            f"multival={code(self.multi_value_sep)} "
            f"enc={self.encoding}/{self.encoding_errors} "
            f"fields={self.field_count_mode} "
            f"term={'CRLF' if self.record_terminator == chr(13)+chr(10) else 'LF'}"
        )


DEFAULT_CONFIG = DatConfig()


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class DatParseError(ValueError):
    """Raised when a DAT stream cannot be parsed under the given config."""

    def __init__(self, message: str, offset: Optional[int] = None) -> None:
        super().__init__(message)
        self.offset = offset


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _normalize_internal_newlines(value: str, newline_char: str) -> str:
    """Replace real newlines inside a field value with the DAT newline char.

    Used when writing. \\r\\n, \\r and \\n are all collapsed to `newline_char`
    so the resulting DAT record stays on one physical line.
    """
    if "\r\n" in value:
        value = value.replace("\r\n", newline_char)
    if "\r" in value:
        value = value.replace("\r", newline_char)
    if "\n" in value:
        value = value.replace("\n", newline_char)
    return value


def _restore_internal_newlines(value: str, newline_char: str) -> str:
    """Inverse of _normalize_internal_newlines; used when reading."""
    if newline_char in value:
        value = value.replace(newline_char, "\n")
    return value


# ASCII Record Separator sometimes used as a multi-value separator in older
# exports. When normalize_multivalue is on, rewrite it to multi_value_sep.
_LEGACY_MULTIVALUE_SEP = chr(0x1E)


def _normalize_multivalue(value: str, config: DatConfig) -> str:
    """Rewrite legacy RS (0x1E) multi-value separators to config.multi_value_sep.

    Relativity's documented default is ';'. Values that already use the
    configured separator are left alone. No-op when normalize_multivalue is
    False or when the configured separator is itself 0x1E.
    """
    if not config.normalize_multivalue:
        return value
    if config.multi_value_sep == _LEGACY_MULTIVALUE_SEP:
        return value
    if _LEGACY_MULTIVALUE_SEP not in value:
        return value
    return value.replace(_LEGACY_MULTIVALUE_SEP, config.multi_value_sep)


def _escape_field(value: str, config: DatConfig) -> str:
    """Double the quote char, normalize multivalue, rewrite newlines."""
    if value == "":
        return ""
    value = _normalize_multivalue(value, config)
    if config.quote_char in value:
        value = value.replace(config.quote_char, config.quote_char + config.quote_char)
    value = _normalize_internal_newlines(value, config.newline_char)
    return value


def _format_field(value: str, config: DatConfig) -> str:
    """Wrap one escaped value in the quote char."""
    return config.quote_char + _escape_field(value, config) + config.quote_char


def _coerce_row(
    values: Sequence[Optional[str]],
    expected: int,
    mode: str,
    record_index: int,
) -> List[str]:
    """Pad/truncate/reject a row so it has exactly `expected` fields.

    `record_index` is 1-based for error messages (0 means unknown / not CSV).
    """
    cells = ["" if v is None else str(v) for v in values]
    n = len(cells)
    if n == expected:
        return cells
    label = f"record {record_index}" if record_index > 0 else "record"
    if n < expected:
        if mode == "reject":
            raise ValueError(
                f"{label} has {n} fields but schema expects {expected}"
            )
        return cells + [""] * (expected - n)
    # n > expected
    if mode == "pad_truncate":
        return cells[:expected]
    raise ValueError(
        f"{label} has {n} fields but schema expects {expected} "
        f"(use field_count_mode=pad_truncate to drop extras)"
    )


# Alias groups used when projecting a thin CSV onto a fuller DAT schema.
# Names in the same frozenset are treated as the same logical field.
#
# Intentionally CONSERVATIVE: do not fold fields that are routinely distinct
# in productions (DOCID≠BEGDOC, Author≠From, generic Hash≠MD5, Text≠TextPath).
# Relativity "Control Number" still aliases to BEGDOC (common load-file ID).
_FIELD_ALIAS_GROUPS: Tuple[frozenset, ...] = (
    frozenset({
        "begdoc", "begdoc#", "beg bates", "begin bates", "bates/control #",
        "bates", "control number", "control#",
    }),
    frozenset({
        "enddoc", "enddoc#", "end bates", "end bates/control #", "endbates",
    }),
    frozenset({"docid", "doc id", "document id", "documentid"}),
    frozenset({
        "begattach", "beg attach", "begin attach", "group identifier",
        "family range begin", "attach begin",
    }),
    frozenset({
        "endattach", "end attach", "family range end", "attach end",
    }),
    frozenset({
        "filepath", "file path", "file_path", "native", "nativefile",
    }),
    frozenset({"filename", "file name", "file_name"}),
    frozenset({
        "nativepath", "native path", "native_path", "native file path",
        "nativefilepath",
    }),
    frozenset({"coded"}),
    frozenset({"custodian", "custodian name"}),
    frozenset({"from", "email from"}),
    frozenset({"author"}),
    frozenset({"to", "email to"}),
    frozenset({"recipient"}),
    frozenset({"cc", "email cc"}),
    frozenset({"bcc", "email bcc"}),
    frozenset({"subject", "email subject"}),
    frozenset({"md5", "md5 hash", "md5hash"}),
    frozenset({"datesent", "date sent", "sent date"}),
    frozenset({"datereceived", "date received", "received date"}),
    frozenset({"fileext", "file ext", "extension", "document extension"}),
    frozenset({"textpath", "text path", "ocr path"}),
)


@dataclass
class MergeReport:
    """Result of analyzing a CSV header against a target DAT schema.

    Attributes:
        mapped: source header name -> target field name (1:1 after aliases).
        collisions: target field -> list of source names that all map to it.
        dropped: source header names that map to no target field.
        unfilled: target field names with no source mapping (will be blank).
    """

    mapped: dict  # str -> str
    collisions: dict  # str -> List[str]
    dropped: List[str]
    unfilled: List[str]

    @property
    def ok(self) -> bool:
        """True when there are no collisions and no dropped source columns."""
        return not self.collisions and not self.dropped

    def summary(self) -> str:
        lines = [
            f"merge: mapped={len(self.mapped)} "
            f"collisions={len(self.collisions)} "
            f"dropped={len(self.dropped)} "
            f"unfilled={len(self.unfilled)}",
        ]
        if self.collisions:
            for tgt, srcs in sorted(self.collisions.items()):
                lines.append(f"  COLLISION -> {tgt}: {', '.join(srcs)}")
        if self.dropped:
            lines.append(f"  DROPPED: {', '.join(self.dropped)}")
        if self.unfilled:
            lines.append(f"  BLANK (no source): {', '.join(self.unfilled)}")
        return "\n".join(lines)


def _canonical_field_key(name: str) -> str:
    """Normalize a field name for schema merge (case-insensitive + aliases)."""
    key = str(name).strip().lower()
    for group in _FIELD_ALIAS_GROUPS:
        if key in group:
            # Stable representative so aliases collide on the same key.
            return min(group)
    return key


def analyze_schema_merge(
    source_names: Sequence[str],
    target_names: Sequence[str],
) -> MergeReport:
    """Analyze how ``source_names`` project onto ``target_names``.

    Detects alias collisions (two CSV columns → one DAT field) and dropped
    source columns (CSV fields with no target). Does not read row values.
    """
    target_keys = [_canonical_field_key(t) for t in target_names]
    target_key_to_name = {}
    for t, k in zip(target_names, target_keys):
        # First target name wins as the display label for this key.
        target_key_to_name.setdefault(k, str(t))

    # key -> list of source header names that canonicalize to it
    sources_by_key: dict = {}
    for name in source_names:
        key = _canonical_field_key(name)
        sources_by_key.setdefault(key, []).append(str(name))

    mapped: dict = {}
    collisions: dict = {}
    dropped: List[str] = []
    for key, srcs in sources_by_key.items():
        if key in target_key_to_name:
            tgt = target_key_to_name[key]
            if len(srcs) > 1:
                collisions[tgt] = list(srcs)
            # Record first source as the mapped name (even under collision).
            mapped[srcs[0]] = tgt
        else:
            # Filename may still feed FILEPATH via thin-CSV fallback — not dropped.
            filepath_key = _canonical_field_key("FILEPATH")
            filename_key = _canonical_field_key("FILENAME")
            if key == filename_key and filepath_key in target_key_to_name:
                mapped[srcs[0]] = target_key_to_name[filepath_key]
                if len(srcs) > 1:
                    collisions[target_key_to_name[filepath_key]] = list(srcs)
            else:
                dropped.extend(srcs)

    filled_keys = set(sources_by_key) & set(target_key_to_name)
    # Filename always copies into empty FILEPATH in project_row, so treat
    # FILEPATH as filled whenever a Filename-like source column exists.
    filename_key = _canonical_field_key("FILENAME")
    filepath_key = _canonical_field_key("FILEPATH")
    if filename_key in sources_by_key and filepath_key in target_key_to_name:
        filled_keys.add(filepath_key)

    unfilled = [
        target_key_to_name[k]
        for k in target_keys
        if k not in filled_keys
    ]
    # Dedupe unfilled while preserving order (duplicate target keys).
    seen = set()
    unfilled_unique: List[str] = []
    for name in unfilled:
        if name not in seen:
            seen.add(name)
            unfilled_unique.append(name)

    return MergeReport(
        mapped=mapped,
        collisions=collisions,
        dropped=dropped,
        unfilled=unfilled_unique,
    )


def project_row(
    source_names: Sequence[str],
    source_values: Sequence[Optional[str]],
    target_names: Sequence[str],
) -> List[str]:
    """Map a source row onto ``target_names``, filling missing fields with \"\".

    Matching is case-insensitive and uses conservative Concordance/Relativity
    aliases (e.g. Bates/Control # -> BEGDOC, Control Number -> BEGDOC).
    Every target field is always present — blank when the source has no match.

    Thin volume-index CSVs often have a single ``Filename`` column. When
    FILEPATH is empty after mapping, ``Filename`` is copied into FILEPATH so
    native-path loaders still see a value (FILENAME is also filled when present
    in the target schema).

    Call ``analyze_schema_merge`` before projecting to detect collisions and
    dropped columns; this function does not warn on its own.
    """
    # key -> (value, source_index) ; first occurrence wins for the value.
    src_map: dict = {}
    for i, name in enumerate(source_names):
        key = _canonical_field_key(name)
        if key not in src_map:
            val = source_values[i] if i < len(source_values) else ""
            src_map[key] = "" if val is None else str(val)

    target_keys = [_canonical_field_key(t) for t in target_names]
    filepath_key = _canonical_field_key("FILEPATH")
    filename_key = _canonical_field_key("FILENAME")

    out: List[str] = []
    for tkey in target_keys:
        val = src_map.get(tkey, "")
        out.append(val)

    # Thin-CSV fallback: copy Filename into empty FILEPATH so natives resolve.
    if filepath_key in target_keys and filename_key in src_map:
        fp_i = target_keys.index(filepath_key)
        if not out[fp_i]:
            out[fp_i] = src_map[filename_key]
    return out


# Common production-style native load-file schema (Lexbe / Relativity-ish).
# Used as a documented reference and optional merge target; not required.
# Includes CODED so thin volume-index CSVs (Bates + Coded + Filename) retain
# the coding column when projected onto this schema.
STANDARD_NATIVE_FIELDS: Tuple[str, ...] = (
    "BEGDOC",
    "ENDDOC",
    "BEGATTACH",
    "ENDATTACH",
    "CUSTODIAN",
    "CODED",
    "FILEPATH",
    "FILENAME",
    "FILEEXT",
    "MD5",
    "DATESENT",
    "FROM",
    "TO",
    "CC",
    "BCC",
    "SUBJECT",
    "TEXTPATH",
    "NATIVEPATH",
)


def _encode_record(values: Sequence[str], config: DatConfig) -> str:
    """Build a full DAT record string (without the trailing terminator)."""
    parts = [_format_field(v, config) for v in values]
    return config.field_delimiter.join(parts) + config.field_delimiter


# ---------------------------------------------------------------------------
# CSV reading / writing
# ---------------------------------------------------------------------------

def _open_text_read(path: str, encoding: str) -> io.TextIOBase:
    # newline='' keeps raw line endings intact so csv.reader sees them as-is.
    # utf-8-sig transparently strips a BOM if present; otherwise honor encoding.
    enc = "utf-8-sig" if encoding.lower() in ("utf-8", "utf8") else encoding
    return open(path, "r", encoding=enc, newline="", errors="strict")


def _open_text_write(path: str, encoding: str, terminator: str) -> io.TextIOBase:
    # newline='' so we control the exact terminator written by csv.writer.
    return open(path, "w", encoding=encoding, newline="", errors="strict")


def read_csv(
    path: str,
    encoding: str = "utf-8",
    delimiter: str = ",",
    quotechar: str = '"',
) -> Generator[Tuple[List[str], List[str]], None, None]:
    """Yield (field_names, row) for each data row of a CSV file.

    The header is read once and yielded with every row so callers don't have
    to track state. Rows are streamed (no full load into memory).
    """
    with _open_text_read(path, encoding) as fh:
        reader = csv.reader(fh, delimiter=delimiter, quotechar=quotechar)
        try:
            header = next(reader)
        except StopIteration:
            return
        for row in reader:
            # csv.reader already yields lists; pad/truncate defensively so the
            # caller always sees a list the same length as the header.
            if len(row) < len(header):
                row = row + [""] * (len(header) - len(row))
            yield header, row


def write_csv(
    path: str,
    records: Iterator[Sequence[str]],
    field_names: Sequence[str],
    encoding: str = "utf-8",
    delimiter: str = ",",
    quotechar: str = '"',
    terminator: str = "\n",
) -> int:
    """Write records to a CSV with a header row. Returns the row count."""
    count = 0
    with _open_text_write(path, encoding, terminator) as fh:
        writer = csv.writer(
            fh,
            delimiter=delimiter,
            quotechar=quotechar,
            lineterminator=terminator,
            quoting=csv.QUOTE_MINIMAL,
        )
        writer.writerow(list(field_names))
        for rec in records:
            writer.writerow(["" if v is None else str(v) for v in rec])
            count += 1
    return count


# ---------------------------------------------------------------------------
# DAT writing (streaming)
# ---------------------------------------------------------------------------

def write_dat(
    path: str,
    rows: Iterator[Sequence[str]],
    field_names: Sequence[str],
    config: DatConfig,
    emit_dct: bool = True,
    dct_path: Optional[str] = None,
    emit_header: bool = True,
) -> int:
    """Stream `rows` out to a Concordance DAT file at `path`.

    Every row is coerced to exactly ``len(field_names)`` fields using
    ``config.field_count_mode`` (default: pad short rows, reject overlong).

    When ``emit_header`` is True (default), the first physical line is a
    Concordance/Relativity header row: field names wrapped in the quote char
    and separated by the field delimiter — the same encoding as data rows.
    Relativity strongly recommends this; Concordance can load field names
    from the DAT header; Reveal expects a header line.

    `field_names` are also written to a companion header-sidecar `.dct` when
    `emit_dct` is True (convenience for this tool; not a Concordance CPL dict).

    Returns the number of **data** records written (header not counted).
    """
    expected = len(field_names)
    if expected == 0:
        # Schema-less write is only legal when there are no data rows. Peek
        # BEFORE touching the output so a bad call leaves no partial artifacts.
        first_row = next(iter(rows), None)
        if first_row is not None:
            raise ValueError("cannot write data rows when field_names is empty")
        open(path, "wb").close()
        if emit_dct:
            dpath = dct_path or os.path.splitext(path)[0] + ".dct"
            write_dct(dpath, field_names, config.encoding)
        return 0

    # Field names carrying DAT control characters would produce a header other
    # platforms cannot map (even though this tool's own parser tolerates it).
    bad_names = [
        str(n) for n in field_names
        if any(c in str(n) for c in (
            config.field_delimiter, config.quote_char,
            config.newline_char, "\n", "\r",
        ))
    ]
    if bad_names:
        raise ValueError(
            "field names contain DAT control characters: "
            + ", ".join(repr(n) for n in bad_names)
        )

    count = 0
    term = config.record_terminator
    # Open binary and encode manually so we have full control over the exact
    # bytes. A stateful incremental encoder is required so BOM-emitting
    # encodings (utf-16/utf-32) write exactly one BOM at file start instead of
    # one per record (per-record .encode() would inject U+FEFF mid-file).
    encoder = codecs.getincrementalencoder(config.encoding)(config.encoding_errors)
    with open(path, "wb") as fh:
        if emit_header:
            header_rec = _encode_record([str(n) for n in field_names], config) + term
            fh.write(encoder.encode(header_rec))
        for row in rows:
            count += 1
            coerced = _coerce_row(row, expected, config.field_count_mode, count)
            rec = _encode_record(coerced, config) + term
            fh.write(encoder.encode(rec))
        fh.write(encoder.encode("", final=True))
    if emit_dct:
        dpath = dct_path or os.path.splitext(path)[0] + ".dct"
        write_dct(dpath, field_names, config.encoding)
    return count


def csv_to_dat(
    in_csv: str,
    out_dat: str,
    config: DatConfig = DEFAULT_CONFIG,
    field_names: Optional[Sequence[str]] = None,
    emit_dct: bool = True,
    emit_header: bool = True,
    csv_encoding: Optional[str] = None,
    csv_delimiter: str = ",",
    merge_schema: bool = False,
    strict_merge: bool = False,
) -> int:
    """Convert a CSV file to a DAT file. Returns the data-record count.

    `field_names` overrides the names written to the DAT header / .dct.
    When ``merge_schema`` is False (default), ``field_names`` must match the
    CSV column count (positional rename). When ``merge_schema`` is True,
    ``field_names`` is the full target schema: each CSV column is mapped by
    name/alias onto that schema and every target field is emitted even when
    blank or missing from the CSV.

    Unmapped source columns and alias collisions are reported on stderr.
    When ``strict_merge`` is True, those conditions raise ``ValueError``
    instead of warning (prevents silent metadata loss).

    When ``field_names`` is None, the CSV header is used as-is.
    `emit_header` (default True) writes Concordance-style field names as the
    first DAT line. `csv_encoding` defaults to UTF-8 for the source CSV.
    """
    # Prefer an explicit CSV encoding; otherwise default CSV to utf-8 (common
    # for Excel exports) while the DAT uses config.encoding (default cp1252).
    csv_enc = csv_encoding if csv_encoding is not None else "utf-8"

    # Open the CSV once and drive csv.reader directly so we can capture the
    # header row before deciding field names, while still streaming the body.
    enc = "utf-8-sig" if csv_enc.lower() in ("utf-8", "utf8") else csv_enc
    fh = open(in_csv, "r", encoding=enc, newline="", errors="strict")
    rows_iter: Optional[Iterator[List[str]]] = None
    try:
        reader = csv.reader(fh, delimiter=csv_delimiter, quotechar='"')
        try:
            header = next(reader)
        except StopIteration:
            header = None

        if header is None:
            # empty CSV -> emit an empty DAT and (optionally) a .dct
            names: List[str] = list(field_names) if field_names else []
            open(out_dat, "wb").close()
            if emit_dct:
                write_dct(os.path.splitext(out_dat)[0] + ".dct", names, config.encoding)
            return 0

        source_header = list(header)
        if field_names is not None:
            names = list(field_names)
            if not merge_schema and len(names) != len(source_header):
                raise ValueError(
                    f"--field-names has {len(names)} names but CSV has "
                    f"{len(source_header)} columns "
                    f"(pass --merge-schema to project onto a fuller schema)"
                )
        else:
            names = list(source_header)
            merge_schema = False

        # Unique header names are required by several platforms (Logikcull
        # rejects duplicates; Relativity mapping becomes ambiguous).
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            print(
                "WARNING: duplicate field names in DAT header: "
                + ", ".join(dupes)
                + " (Logikcull/Relativity require unique header names)",
                file=sys.stderr,
            )

        if merge_schema:
            report = analyze_schema_merge(source_header, names)
            if report.collisions or report.dropped:
                msg = report.summary()
                if strict_merge:
                    raise ValueError(
                        "strict schema merge refused (collisions and/or dropped "
                        f"columns):\n{msg}"
                    )
                print(
                    "WARNING: schema merge has collisions and/or dropped columns "
                    "(pass --strict-merge to fail instead):\n" + msg,
                    file=sys.stderr,
                )
            elif report.unfilled:
                print(
                    "schema merge: " + report.summary(),
                    file=sys.stderr,
                )

        expected = len(names)
        src_expected = len(source_header)

        def _rows() -> Iterator[List[str]]:
            for idx, row in enumerate(reader, start=1):
                # Skip blank physical CSV lines ([]) and whitespace-only single
                # cells — they create phantom documents. An all-empty multi-cell
                # row (",,,") is a real record and is preserved.
                if not row:
                    continue
                if len(row) == 1 and not (row[0] or "").strip():
                    continue
                if merge_schema:
                    # Honor field_count_mode against the SOURCE header so merge
                    # does not silently accept malformed CSV rows.
                    row = _coerce_row(row, src_expected, config.field_count_mode, idx)
                    yield project_row(source_header, row, names)
                else:
                    yield _coerce_row(row, expected, config.field_count_mode, idx)

        rows_iter = _rows()
        return write_dat(
            out_dat,
            rows_iter,
            names,
            config,
            emit_dct=emit_dct,
            emit_header=emit_header,
        )
    finally:
        if rows_iter is not None and hasattr(rows_iter, "close"):
            rows_iter.close()
        fh.close()


# ---------------------------------------------------------------------------
# DAT reading (streaming state machine)
# ---------------------------------------------------------------------------

def _parse_one(buffer: str, start: int, config: DatConfig, final: bool) -> Optional[Tuple[List[str], int]]:
    """Try to parse one record from `buffer` starting at `start`.

    Returns (fields, next_offset) on success, or None if the buffer does not
    yet contain a complete record (caller should fetch more data). If `final`
    is True the buffer will never grow, so a trailing record without a
    terminator is accepted.
    """
    n = len(buffer)
    delim = config.field_delimiter
    quote = config.quote_char
    i = start

    # Skip inter-record blank lines.
    while i < n and buffer[i] in ("\n", "\r"):
        i += 1
    if i >= n:
        return [], i  # nothing but blanks; signal "consumed blanks, no record"

    if buffer[i] != quote:
        raise DatParseError(
            f"expected quote char 0x{ord(quote):02X} at start of field, "
            f"found 0x{ord(buffer[i]):02X}",
            offset=i,
        )

    fields: List[str] = []
    while True:
        # Consume opening quote.
        i += 1
        content: List[str] = []
        closed = False
        while i < n:
            ch = buffer[i]
            if ch == quote:
                if i + 1 < n and buffer[i + 1] == quote:
                    content.append(quote)
                    i += 2
                else:
                    i += 1
                    closed = True
                    break
            else:
                content.append(ch)
                i += 1
        if not closed:
            if final:
                # Tolerant: EOF inside a field; keep what we have.
                fields.append(_restore_internal_newlines("".join(content), config.newline_char))
                return fields, i
            return None  # need more data

        fields.append(_restore_internal_newlines("".join(content), config.newline_char))

        if i >= n:
            if final:
                return fields, i
            return None

        ch = buffer[i]
        if ch == delim:
            i += 1
            if i >= n:
                if final:
                    return fields, i
                return None
            nxt = buffer[i]
            if nxt == "\n":
                return fields, i + 1
            if nxt == "\r" and i + 1 < n and buffer[i + 1] == "\n":
                return fields, i + 2
            if nxt == "\r" and final:
                return fields, i + 1
            if nxt == quote:
                continue  # next field
            raise DatParseError(
                f"expected quote, newline or EOF after delimiter, found 0x{ord(nxt):02X}",
                offset=i,
            )
        elif ch == "\n":
            return fields, i + 1
        elif ch == "\r":
            if i + 1 < n and buffer[i + 1] == "\n":
                return fields, i + 2
            if final:
                return fields, i + 1
            return None
        else:
            raise DatParseError(
                f"expected delimiter or newline after closing quote, found 0x{ord(ch):02X}",
                offset=i,
            )


# ---------------------------------------------------------------------------
# Field-name aliases (shared by header detection + semantic validation)
# ---------------------------------------------------------------------------

_BEGDOC_ALIASES = {
    "begdoc", "begdoc#", "beg bates", "begin bates", "bates/control #",
    "bates", "control number", "control#",
}
_ENDDOC_ALIASES = {
    "enddoc", "enddoc#", "end bates", "end bates/control #", "endbates",
}
_BEGATTACH_ALIASES = {
    "begattach", "beg attach", "begin attach", "group identifier",
    "family range begin", "attach begin",
}
# Path-like columns for semantic validation (natives exist?). Filename alone
# is included so thin volume indexes still get a path check when that is the
# only file column. DOCID is intentionally NOT a BEGDOC alias here either.
_FILEPATH_ALIASES = {
    "filepath", "file path", "file_path", "native", "nativefile",
    "nativepath", "native path", "native_path", "native file path",
    "filename", "file name", "file_name",
}


def _field_index(field_names: Sequence[str], aliases: set) -> Optional[int]:
    for i, name in enumerate(field_names):
        key = str(name).strip().lower()
        if key in aliases:
            return i
    return None


_BATES_NUM_RE = re.compile(r"^(.*?)(\d+)\s*$")


def _bates_key(value: str) -> Optional[Tuple[str, int]]:
    """Split a Bates ID into (prefix, numeric suffix) for numeric compare.

    Returns None when the value has no trailing digits (compare falls back
    to lexicographic, which is only safe for equal-length zero-padded IDs).
    """
    m = _BATES_NUM_RE.match(value)
    if not m:
        return None
    return (m.group(1), int(m.group(2)))


def read_dat(
    path: str,
    config: DatConfig = DEFAULT_CONFIG,
    chunk_size: int = 65536,
    skip_header: bool = False,
    header_names: Optional[Sequence[str]] = None,
) -> Generator[List[str], None, None]:
    """Yield records (each a list[str] of field values) from a DAT file.

    Streams the file in chunks through a text wrapper so multi-hundred-MB
    files do not need to be fully resident in memory. Each record is normally
    a single physical line; the state machine additionally tolerates the rare
    case of a literal newline embedded inside a quoted field.

    When ``skip_header`` is True, the first record is treated as a Concordance
    header row and omitted from the yield stream. If ``header_names`` is given,
    the first record is skipped only when it matches those names
    (case-insensitive); otherwise it is kept as data (legacy DAT without header).

    A leading byte order mark is stripped: Relativity Desktop Client uses the
    BOM to auto-detect encoding, so BOM'd UTF-8/UTF-16 DATs are common.
    """
    # utf-8-sig strips a UTF-8 BOM if present (and reads plain UTF-8 fine);
    # the utf-16 codec consumes its own BOM. A stray U+FEFF that survives
    # decoding is stripped below so the parser never sees it as field text.
    enc = config.encoding
    if enc.lower().replace("_", "-") in ("utf-8", "utf8"):
        enc = "utf-8-sig"
    buffer = ""
    first = True
    first_chunk = True
    with open(path, "r", encoding=enc, newline="", errors="strict") as fh:
        exhausted = False
        while not exhausted:
            chunk = fh.read(chunk_size)
            if not chunk:
                exhausted = True
            if first_chunk and chunk:
                chunk = chunk.lstrip("\ufeff")
                first_chunk = False
            buffer += chunk
            pos = 0
            while True:
                result = _parse_one(buffer, pos, config, final=exhausted)
                if result is None:
                    break
                fields, new_pos = result
                if new_pos == pos and not fields:
                    # no progress (only blanks at end of non-final buffer)
                    break
                if fields or new_pos > pos:
                    if fields:
                        if first and skip_header:
                            first = False
                            if header_names is None or _row_matches_names(fields, header_names):
                                pos = new_pos
                                continue
                            # First row did not match expected header — keep as data.
                        first = False
                        yield fields
                    pos = new_pos
                else:
                    break
            # keep the unparsed tail
            buffer = buffer[pos:]


def _row_matches_names(row: Sequence[str], names: Sequence[str]) -> bool:
    """True if row equals names (case-insensitive, stripped)."""
    if len(row) != len(names):
        return False
    return all(str(a).strip().lower() == str(b).strip().lower() for a, b in zip(row, names))


def _looks_like_header_row(row: Sequence[str]) -> bool:
    """Heuristic: first DAT row is a Concordance header of field names.

    Used only when no .dct / --field-names are supplied. Requires at least one
    well-known Concordance/Relativity field name (BEGDOC, FILEPATH, etc.) and
    rejects Bates-like IDs / paths so legacy no-header DATs are not mis-read.
    Custom field names rely on the companion .dct (written by default).
    """
    if not row:
        return False
    known = _BEGDOC_ALIASES | _ENDDOC_ALIASES | _FILEPATH_ALIASES | {
        "coded", "custodian", "dates", "doctype", "subject", "from", "to",
        "cc", "bcc", "author", "title", "md5hash", "md5", "sha1", "pagecount",
        "begattach", "endattach", "text", "extracted text", "nativefile",
        "datecreated", "datemodified", "timesent", "datesent", "filetype",
        "pgcount", "page count", "prodvol", "volume",
    }
    nonempty = [str(c).strip() for c in row if str(c).strip()]
    if not nonempty:
        return False

    known_hits = 0
    for cell in nonempty:
        key = cell.lower()
        if key in known:
            known_hits += 1
            continue
        # Reject path-like or Bates-like tokens as "field names".
        if any(ch in key for ch in ("\\", "/", ".")):
            return False
        digits = sum(ch.isdigit() for ch in key)
        if digits >= 4 or (len(key) > 20 and digits >= 3):
            return False

    # Header field names must be unique (Logikcull/Relativity requirement);
    # repeated cells indicate a data row (e.g. BEGDOC == ENDDOC on 1-pagers).
    lowered = [c.lower() for c in nonempty]
    if len(set(lowered)) != len(lowered):
        return False

    # Production DATs often carry many custom names alongside a few standard
    # ones (Email Thread ID, Confidentiality, ...). Accept when at least two
    # cells are well-known Concordance/Relativity names and nothing above
    # looked like data. Tiny rows still need a majority of known names so
    # short data values are not mistaken for headers.
    if known_hits >= 2:
        return True
    return known_hits >= 1 and known_hits >= max(1, (len(nonempty) + 1) // 2)


# ---------------------------------------------------------------------------
# DAT -> CSV
# ---------------------------------------------------------------------------

def dat_to_csv(
    in_dat: str,
    out_csv: str,
    config: DatConfig = DEFAULT_CONFIG,
    field_names: Optional[Sequence[str]] = None,
    dct_path: Optional[str] = None,
    infer_if_missing: bool = True,
) -> Tuple[int, List[str]]:
    """Convert a DAT file to a CSV file.

    Field names are resolved in this order:
      1. explicit ``field_names``
      2. Concordance header row inside the DAT (first line)
      3. companion ``.dct``
      4. inferred FIELD1..FIELDn (when ``infer_if_missing``)

    A Concordance header row in the DAT is skipped so it is not written as a
    data row in the CSV. Returns (data_record_count, field_names_used).
    """
    names: Optional[List[str]] = list(field_names) if field_names is not None else None
    dct_names: Optional[List[str]] = None
    if names is None:
        candidate = dct_path
        if candidate is None:
            candidate = os.path.splitext(in_dat)[0] + ".dct"
        if os.path.exists(candidate):
            dct_names = read_dct(candidate, config.encoding)

    record_iter = read_dat(in_dat, config)

    try:
        first = next(record_iter)
    except StopIteration:
        resolved = names if names is not None else (dct_names or [])
        write_csv(out_csv, iter(()), resolved, encoding=config.encoding, terminator=config.record_terminator)
        return 0, resolved

    # Detect Concordance header row on the first line.
    header_from_dat = False
    if names is not None and _row_matches_names(first, names):
        header_from_dat = True
    elif names is None and dct_names is not None:
        if _row_matches_names(first, dct_names):
            names = list(dct_names)
            header_from_dat = True
        elif _looks_like_header_row(first):
            # DAT carries its own header; .dct must agree on field count.
            if len(dct_names) != len(first):
                raise ValueError(
                    f".dct has {len(dct_names)} names but DAT header has {len(first)} fields"
                )
            names = [str(c) for c in first]
            header_from_dat = True
        else:
            # Legacy DAT with no header — use .dct against the first data row.
            if len(dct_names) != len(first):
                raise ValueError(
                    f".dct has {len(dct_names)} names but DAT has {len(first)} fields"
                )
            names = list(dct_names)
    elif names is None and _looks_like_header_row(first):
        names = [str(c) for c in first]
        header_from_dat = True

    if names is None:
        if infer_if_missing:
            names = [f"FIELD{i + 1}" for i in range(len(first))]
            import sys
            print(
                f"WARNING: no field names provided; inferred {len(first)} names "
                f"({', '.join(names)}). Supply --field-names or a .dct for correct headers.",
                file=sys.stderr,
            )
        else:
            raise ValueError("No field names available; provide --field-names or a .dct")

    if len(names) != len(first) and not header_from_dat:
        raise ValueError(
            f"field-name count ({len(names)}) does not match DAT field count ({len(first)})"
        )
    if header_from_dat and len(names) != len(first):
        raise ValueError(
            f"header row field count ({len(first)}) does not match names ({len(names)})"
        )

    def _data_rows() -> Iterator[List[str]]:
        if not header_from_dat:
            yield first
        yield from record_iter

    count = write_csv(
        out_csv,
        _data_rows(),
        names,
        encoding=config.encoding,
        terminator=config.record_terminator,
    )
    return count, names


# ---------------------------------------------------------------------------
# .dct companion (field-name list)
# ---------------------------------------------------------------------------

def write_dct(path: str, field_names: Sequence[str], encoding: str) -> None:
    """Write a header-sidecar field-name list (one name per line).

    This is NOT a full Concordance CPL data dictionary (no field types/widths).
    It exists so this tool can rebuild a CSV header on the return trip.
    Relativity and Concordance import UIs map fields by position/name and do
    not require this file. A leading comment documents that limitation.
    """
    with open(path, "w", encoding=encoding, newline="") as fh:
        fh.write(
            "# csv_to_dat header sidecar (NOT a Concordance CPL dictionary)\n"
            "# One field name per line; blank lines and # comments are ignored on read.\n"
        )
        for name in field_names:
            fh.write(str(name).replace("\n", " ").replace("\r", " ") + "\n")


def read_dct(path: str, encoding: str) -> List[str]:
    """Read a field-name list written by write_dct (one name per line).

    Blank lines and lines whose first non-whitespace char is '#' are ignored
    so the sidecar header comments do not become field names.
    """
    names: List[str] = []
    with open(path, "r", encoding=encoding, newline="") as fh:
        for line in fh:
            text = line.rstrip("\r\n")
            stripped = text.strip()
            if not stripped or stripped.startswith("#"):
                continue
            names.append(text)
    return names


# ---------------------------------------------------------------------------
# Semantic load validation (Bates / path / uniqueness)
# ---------------------------------------------------------------------------


@dataclass
class ValidationReport:
    """Result of semantic load-file validation."""

    records: int = 0
    errors: List[str] = dc_field(default_factory=list)
    warnings: List[str] = dc_field(default_factory=list)
    begdoc_field: Optional[str] = None
    enddoc_field: Optional[str] = None
    filepath_field: Optional[str] = None

    @property
    def ok(self) -> bool:
        return len(self.errors) == 0

    def summary(self) -> str:
        lines = [
            "Load validation",
            f"  records : {self.records}",
            f"  errors  : {len(self.errors)}",
            f"  warnings: {len(self.warnings)}",
            f"  result  : {'PASS' if self.ok else 'FAIL'}",
        ]
        if self.begdoc_field:
            lines.append(f"  BEGDOC  : {self.begdoc_field}")
        if self.enddoc_field:
            lines.append(f"  ENDDOC  : {self.enddoc_field}")
        if self.filepath_field:
            lines.append(f"  FILEPATH: {self.filepath_field}")
        for e in self.errors[:20]:
            lines.append(f"  ERROR: {e}")
        if len(self.errors) > 20:
            lines.append(f"  ... and {len(self.errors) - 20} more errors")
        for w in self.warnings[:10]:
            lines.append(f"  WARN: {w}")
        return "\n".join(lines)


def validate_load_records(
    field_names: Sequence[str],
    rows: Iterator[Sequence[str]],
    *,
    require_begdoc: bool = True,
    check_filepath_exists: bool = False,
    natives_root: Optional[str] = None,
) -> ValidationReport:
    """Validate document-level load semantics against common Concordance fields.

    Checks (when the matching columns are present / required):
      * BEGDOC non-empty and unique
      * ENDDOC present when column exists; empty ENDDOC warned; numeric-aware
        compare (PREFIX + digits) with lexicographic fallback
      * Bates range overlap between documents (same prefix, numeric suffix)
      * BEGATTACH family integrity: every non-empty BEGATTACH must reference
        an existing BEGDOC in the same load
      * FILEPATH/NATIVE non-empty when column exists
      * optional: native file exists under natives_root (or as absolute path)

    Returns a ValidationReport. Does not mutate rows; consumes the iterator.
    """
    report = ValidationReport()
    beg_i = _field_index(field_names, _BEGDOC_ALIASES)
    end_i = _field_index(field_names, _ENDDOC_ALIASES)
    path_i = _field_index(field_names, _FILEPATH_ALIASES)
    att_i = _field_index(field_names, _BEGATTACH_ALIASES)

    if beg_i is not None:
        report.begdoc_field = field_names[beg_i]
    if end_i is not None:
        report.enddoc_field = field_names[end_i]
    if path_i is not None:
        report.filepath_field = field_names[path_i]

    if require_begdoc and beg_i is None:
        report.errors.append(
            "no BEGDOC-like column found "
            "(expected one of: BEGDOC, Bates/Control #, ...)"
        )

    seen: dict = {}
    ranges: List[Tuple[str, int, int, int]] = []  # (prefix, start, end, record)
    begattach_refs: List[Tuple[int, str]] = []  # (record, value)
    for idx, row in enumerate(rows, start=1):
        report.records += 1
        cells = ["" if v is None else str(v) for v in row]

        if beg_i is not None:
            beg = cells[beg_i].strip() if beg_i < len(cells) else ""
            if not beg:
                report.errors.append(f"record {idx}: BEGDOC is empty")
            elif beg in seen:
                report.errors.append(
                    f"record {idx}: duplicate BEGDOC {beg!r} "
                    f"(first seen at record {seen[beg]})"
                )
            else:
                seen[beg] = idx

            if end_i is not None:
                end = cells[end_i].strip() if end_i < len(cells) else ""
                if not end:
                    report.warnings.append(f"record {idx}: ENDDOC is empty")
                elif beg:
                    bk, ek = _bates_key(beg), _bates_key(end)
                    if bk and ek and bk[0] == ek[0]:
                        # Numeric-aware: PREFIX_9 < PREFIX_10 compares correctly.
                        if ek[1] < bk[1]:
                            report.errors.append(
                                f"record {idx}: ENDDOC {end!r} is before BEGDOC {beg!r}"
                            )
                        else:
                            ranges.append((bk[0], bk[1], ek[1], idx))
                    elif end < beg:
                        # Lexicographic fallback (zero-padded shared-prefix IDs).
                        report.errors.append(
                            f"record {idx}: ENDDOC {end!r} sorts before BEGDOC {beg!r}"
                        )

        if att_i is not None:
            att = cells[att_i].strip() if att_i < len(cells) else ""
            if att:
                begattach_refs.append((idx, att))

        if path_i is not None:
            path = cells[path_i].strip() if path_i < len(cells) else ""
            if not path:
                report.warnings.append(f"record {idx}: FILEPATH/NATIVE is empty")
            elif check_filepath_exists:
                candidates = [path]
                if natives_root and not os.path.isabs(path):
                    candidates.append(os.path.join(natives_root, path))
                if not any(os.path.isfile(c) for c in candidates):
                    report.errors.append(
                        f"record {idx}: native file not found for {path!r}"
                    )

    # Bates range overlap: doc A 1-10 and doc B 5-8 both claim pages 5-8.
    ranges.sort()
    for (pa, sa, ea, ra), (pb, sb, eb, rb) in zip(ranges, ranges[1:]):
        if pa == pb and sb <= ea:
            report.errors.append(
                f"record {rb}: Bates range {pb}{sb}-{pb}{eb} overlaps "
                f"record {ra} ({pa}{sa}-{pa}{ea})"
            )

    # Family integrity: BEGATTACH must point at a BEGDOC present in this load.
    for idx, att in begattach_refs:
        if att not in seen:
            report.errors.append(
                f"record {idx}: BEGATTACH {att!r} does not match any BEGDOC in this load"
            )

    return report


def validate_dat_file(
    dat_path: str,
    field_names: Sequence[str],
    config: DatConfig = DEFAULT_CONFIG,
    **kwargs,
) -> ValidationReport:
    """Validate an existing DAT file's records against semantic rules.

    Skips a Concordance header row when the first record matches ``field_names``.
    """
    return validate_load_records(
        field_names,
        read_dat(dat_path, config, skip_header=True, header_names=field_names),
        **kwargs,
    )


def validate_csv_file(
    csv_path: str,
    field_names: Optional[Sequence[str]] = None,
    csv_encoding: str = "utf-8",
    **kwargs,
) -> ValidationReport:
    """Validate a CSV (using its header or an override name list).

    Rows are streamed to the validator — large CSVs are not loaded into memory.
    """
    enc = "utf-8-sig" if csv_encoding.lower() in ("utf-8", "utf8") else csv_encoding
    with open(csv_path, "r", encoding=enc, newline="", errors="strict") as fh:
        reader = csv.reader(fh)
        try:
            header = next(reader)
        except StopIteration:
            header = []
        names = list(field_names) if field_names is not None else list(header)

        def _stream() -> Iterator[List[str]]:
            for row in reader:
                if not row or not any((c or "").strip() for c in row):
                    continue
                if len(row) < len(names):
                    row = row + [""] * (len(names) - len(row))
                yield row

        return validate_load_records(names, _stream(), **kwargs)


# ---------------------------------------------------------------------------
# Opticon (.opt) companion — page-level image load file
# ---------------------------------------------------------------------------

def write_opt(
    path: str,
    records: Iterator[Sequence[str]],
    *,
    volume: str = "",
    image_ext: str = ".tif",
    pages_per_doc: int = 1,
    image_dir: str = "",
    encoding: str = "cp1252",
) -> int:
    """Write a minimal Opticon/Concordance .opt image load file.

    Each input record is treated as one document. With the default
    ``pages_per_doc=1``, one OPT line is emitted per document (Y break,
    page count 1). Paths are ``{image_dir}/{begdoc}{image_ext}`` (or just
    ``{begdoc}{image_ext}`` when image_dir is empty).

    OPT layout (Relativity / Concordance):
      pageID,volume,path,Y|blank,folder,box,pageCount

    This does **not** invent multi-page TIFF breakouts; pass pages_per_doc > 1
    only when you intentionally want placeholder continuation pages.
    Returns the number of OPT lines written.
    """
    if pages_per_doc < 1:
        raise ValueError("pages_per_doc must be >= 1")
    # OPT is a bare comma-separated format with no quoting mechanism; a comma
    # in any component silently shifts every downstream column on import.
    for label, value in (("volume", volume), ("image_dir", image_dir), ("image_ext", image_ext)):
        if "," in value:
            raise ValueError(f"OPT {label} must not contain a comma: {value!r}")
    ext = image_ext if image_ext.startswith(".") else f".{image_ext}"
    lines_written = 0
    with open(path, "w", encoding=encoding, newline="") as fh:
        for rec in records:
            if not rec:
                continue
            beg = str(rec[0]).strip()
            if not beg:
                raise ValueError("OPT write requires a non-empty first-field Bates/page ID")
            if "," in beg:
                raise ValueError(f"OPT page ID must not contain a comma: {beg!r}")
            for page_i in range(pages_per_doc):
                if pages_per_doc == 1:
                    page_id = beg
                else:
                    # Zero-padded suffix for placeholder multi-page keys.
                    page_id = f"{beg}_{page_i + 1:04d}" if page_i else beg
                rel = f"{page_id}{ext}"
                img_path = os.path.join(image_dir, rel) if image_dir else rel
                # OPT uses forward or backslashes as provided; keep OS join then
                # normalize to backslash for classic Windows productions.
                img_path = img_path.replace("/", "\\")
                marker = "Y" if page_i == 0 else ""
                page_count = str(pages_per_doc) if page_i == 0 else ""
                fh.write(
                    f"{page_id},{volume},{img_path},{marker},,,{page_count}\n"
                )
                lines_written += 1
    return lines_written


def dat_to_opt(
    in_dat: str,
    out_opt: str,
    config: DatConfig = DEFAULT_CONFIG,
    field_names: Optional[Sequence[str]] = None,
    dct_path: Optional[str] = None,
    volume: str = "",
    image_ext: str = ".tif",
    pages_per_doc: int = 1,
    image_dir: str = "",
    begdoc_index: Optional[int] = None,
) -> int:
    """Build an .opt companion from a DAT (uses BEGDOC / first field as page ID)."""
    names: Optional[List[str]] = list(field_names) if field_names else None
    if names is None:
        candidate = dct_path or (os.path.splitext(in_dat)[0] + ".dct")
        if os.path.exists(candidate):
            names = read_dct(candidate, config.encoding)

    beg_i = begdoc_index
    if beg_i is None and names is not None:
        found = _field_index(names, _BEGDOC_ALIASES)
        beg_i = found if found is not None else 0
    if beg_i is None:
        beg_i = 0

    def _begdocs() -> Iterator[List[str]]:
        for rec in read_dat(
            in_dat, config, skip_header=True,
            header_names=names if names is not None else None,
        ):
            if beg_i >= len(rec):
                raise ValueError(f"record missing BEGDOC at index {beg_i}")
            yield [rec[beg_i]]

    return write_opt(
        out_opt,
        _begdocs(),
        volume=volume,
        image_ext=image_ext,
        pages_per_doc=pages_per_doc,
        image_dir=image_dir,
        encoding=config.encoding,
    )


# ---------------------------------------------------------------------------
# Round-trip verification
# ---------------------------------------------------------------------------

@dataclass
class VerifyReport:
    """Result of a round-trip verification."""

    direction: str  # "csv->dat->csv" or "dat->csv->dat"
    input_records: int = 0
    output_records: int = 0
    field_counts_match: bool = False
    field_differences: int = 0
    max_field_count: int = 0
    first_diff: Union[Tuple[int, int, str, str], None] = None
    ok: bool = False

    def summary(self) -> str:
        lines = [
            f"Round-trip verify ({self.direction})",
            f"  input records : {self.input_records}",
            f"  output records: {self.output_records}",
            f"  fields/record : {self.max_field_count}",
            f"  field diffs   : {self.field_differences}",
            f"  result        : {'PASS' if self.ok else 'FAIL'}",
        ]
        if self.first_diff is not None:
            r, c, a, b = self.first_diff
            lines.append(f"  first diff    : record {r + 1} field {c + 1}: {a!r} != {b!r}")
        return "\n".join(lines)


def verify_roundtrip_csv(
    in_csv: str,
    config: DatConfig = DEFAULT_CONFIG,
    field_names: Optional[Sequence[str]] = None,
    work_dir: Optional[str] = None,
    csv_encoding: str = "utf-8",
) -> VerifyReport:
    """Run CSV -> DAT -> CSV and compare field-by-field. Returns a report.

    ``csv_encoding`` is the encoding of the *source* CSV (default utf-8);
    pass cp1252/latin-1 etc. for non-UTF-8 exports so verification does not
    misread accented characters.
    """
    import tempfile

    work = work_dir or tempfile.mkdtemp(prefix="dat_rt_")
    dat_path = os.path.join(work, "roundtrip.dat")
    csv_path = os.path.join(work, "roundtrip.csv")

    # Resolve field names from the CSV header when not supplied so a
    # header-only DAT (0 data rows) is not mis-read as one data record.
    names_in: Optional[List[str]] = list(field_names) if field_names is not None else None
    if names_in is None:
        try:
            with _open_text_read(in_csv, csv_encoding) as fh:
                reader = csv.reader(fh)
                names_in = list(next(reader))
        except StopIteration:
            names_in = []

    n1 = csv_to_dat(
        in_csv, dat_path, config, field_names=names_in, emit_dct=False,
        csv_encoding=csv_encoding,
    )
    _count, names = dat_to_csv(
        dat_path, csv_path, config, field_names=names_in, infer_if_missing=True
    )

    report = VerifyReport(direction="csv->dat->csv")
    report.input_records = n1
    report.max_field_count = len(names) if names else 0

    # Source CSV is read with csv_encoding; the round-tripped CSV was written
    # with config.encoding (typically cp1252).
    gen_b = read_csv(csv_path, encoding=config.encoding)
    try:
        hdr_b, first_b = next(gen_b, (None, None))
    except StopIteration:
        hdr_b, first_b = None, None

    gen_a = read_csv(in_csv, encoding=csv_encoding)
    try:
        hdr_a, first_a = next(gen_a, (None, None))
    except StopIteration:
        hdr_a, first_a = None, None

    # Re-create iterators that include the first row we just peeked.
    def _it(peek, gen):
        if peek is None:
            return
        yield peek
        yield from (row for _, row in gen)

    it_a = _it(first_a, gen_a)
    it_b = _it(first_b, gen_b)

    out_n = 0
    idx = 0
    diffs = 0
    first_diff = None
    while True:
        a = next(it_a, None)
        b = next(it_b, None)
        if a is None and b is None:
            break
        if a is None or b is None:
            diffs += 1
            if first_diff is None:
                first_diff = (idx, -1, str(a), str(b))
            break
        out_n += 1
        maxlen = max(len(a), len(b))
        for c in range(maxlen):
            va = a[c] if c < len(a) else ""
            vb = b[c] if c < len(b) else ""
            if va != vb:
                diffs += 1
                if first_diff is None:
                    first_diff = (idx, c, va, vb)
        idx += 1

    report.output_records = out_n
    report.field_differences = diffs
    report.first_diff = first_diff
    report.field_counts_match = (report.max_field_count > 0) or (n1 == 0 and out_n == 0)
    # Empty files round-trip cleanly (0 diffs, matching counts). Non-empty
    # must have zero field diffs and matching record counts.
    report.ok = (n1 == out_n and diffs == 0)
    return report


def verify_roundtrip_dat(
    in_dat: str,
    config: DatConfig,
    field_names: Sequence[str],
    work_dir: Optional[str] = None,
) -> VerifyReport:
    """Run DAT -> CSV -> DAT and compare field-by-field. Returns a report."""
    import tempfile

    work = work_dir or tempfile.mkdtemp(prefix="dat_rt_")
    csv_path = os.path.join(work, "rt.csv")
    dat2_path = os.path.join(work, "rt.dat")

    n1, names = dat_to_csv(in_dat, csv_path, config, field_names=field_names)
    n2 = csv_to_dat(csv_path, dat2_path, config, field_names=names, emit_dct=False)

    report = VerifyReport(direction="dat->csv->dat")
    report.input_records = n1
    report.output_records = n2
    report.max_field_count = len(names) if names else 0

    it_a = read_dat(in_dat, config, skip_header=True, header_names=field_names)
    it_b = read_dat(dat2_path, config, skip_header=True, header_names=names)
    diffs = 0
    first_diff = None
    idx = 0
    while True:
        a = next(it_a, None)
        b = next(it_b, None)
        if a is None and b is None:
            break
        if a is None or b is None:
            diffs += 1
            if first_diff is None:
                first_diff = (idx, -1, str(a), str(b))
            break
        maxlen = max(len(a), len(b))
        for c in range(maxlen):
            va = a[c] if c < len(a) else ""
            vb = b[c] if c < len(b) else ""
            if va != vb:
                diffs += 1
                if first_diff is None:
                    first_diff = (idx, c, va, vb)
        idx += 1

    report.field_differences = diffs
    report.first_diff = first_diff
    report.ok = (n1 == n2 and diffs == 0)
    return report
