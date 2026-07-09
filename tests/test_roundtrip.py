"""
test_roundtrip.py - Round-trip tests for the CSV <-> DAT converter.

WHAT THIS FILE TESTS
--------------------
1. CSV -> DAT -> CSV on a real slice of the case sample: every field must be
   identical to the original (100% field-level fidelity).
2. DAT -> CSV -> DAT on the same slice: every field identical.
3. Edge cases: empty fields, embedded commas (quoted CSV), tildes, the quote
   char appearing inside a value (doubling), and an embedded newline inside a
   field (encoded as 0xAE on write, restored on read).
4. Encoding round-trip under cp1252 (default) and utf-8.
5. Field-count enforcement (pad short / reject overlong / truncate).
6. Multi-value separator normalization (0x1E -> ';').
7. Header-sidecar .dct comments are not read as field names.
8. Empty (header-only) CSV round-trip is OK.
9. Unencodable characters fail under strict; succeed under replace.

Run with:  python -m pytest tests      (or)    python tests\\test_roundtrip.py
"""

import os
import sys
import tempfile
import unittest

# Make the package importable whether run via pytest from the parent or
# directly as `python tests/test_roundtrip.py`.
HERE = os.path.dirname(os.path.abspath(__file__))
PKG_ROOT = os.path.dirname(HERE)  # .../csv_to_dat
if PKG_ROOT not in sys.path:
    sys.path.insert(0, PKG_ROOT)

import converter  # noqa: E402
from converter import (  # noqa: E402
    DatConfig,
    DEFAULT_CONFIG,
    csv_to_dat,
    dat_to_csv,
    read_dat,
    write_dat,
    read_csv,
    verify_roundtrip_csv,
    verify_roundtrip_dat,
)

FIXTURE = os.path.join(HERE, "fixtures", "VOL001_slice.csv")
FIELD_NAMES = ["BEGDOC", "ENDDOC", "CODED", "FILEPATH"]


class RoundTripCSVTests(unittest.TestCase):
    """CSV -> DAT -> CSV on the real sample slice."""

    def test_csv_roundtrip_default_config(self):
        report = verify_roundtrip_csv(FIXTURE, config=DEFAULT_CONFIG, field_names=FIELD_NAMES)
        self.assertTrue(report.ok, msg=report.summary())
        self.assertEqual(report.input_records, 15)
        self.assertEqual(report.field_differences, 0)

    def test_default_encoding_is_cp1252(self):
        self.assertEqual(DEFAULT_CONFIG.encoding, "cp1252")
        self.assertEqual(DEFAULT_CONFIG.multi_value_sep, ";")

    def test_csv_roundtrip_writes_single_byte_thorn(self):
        with tempfile.TemporaryDirectory() as work:
            dat = os.path.join(work, "out.dat")
            csv_to_dat(FIXTURE, dat, config=DEFAULT_CONFIG,
                       field_names=FIELD_NAMES, emit_dct=False)
            with open(dat, "rb") as fh:
                raw = fh.read(1)
            self.assertEqual(raw, b"\xfe", msg="cp1252 DAT must start with single-byte thorn FE")

    def test_dat_contains_concordance_header_row(self):
        """First DAT line must be Concordance field names (Relativity/Reveal)."""
        with tempfile.TemporaryDirectory() as work:
            dat = os.path.join(work, "out.dat")
            csv_to_dat(FIXTURE, dat, config=DEFAULT_CONFIG,
                       field_names=FIELD_NAMES, emit_dct=False)
            recs = list(read_dat(dat, DEFAULT_CONFIG))
            self.assertEqual(recs[0], FIELD_NAMES)
            self.assertEqual(len(recs), 16)  # 1 header + 15 data
            # Round-trip must not treat header as a data row.
            csv_out = os.path.join(work, "back.csv")
            n, names = dat_to_csv(dat, csv_out, DEFAULT_CONFIG, field_names=FIELD_NAMES)
            self.assertEqual(n, 15)
            self.assertEqual(names, FIELD_NAMES)

    def test_no_header_flag(self):
        with tempfile.TemporaryDirectory() as work:
            dat = os.path.join(work, "out.dat")
            csv_to_dat(FIXTURE, dat, config=DEFAULT_CONFIG,
                       field_names=FIELD_NAMES, emit_dct=False, emit_header=False)
            recs = list(read_dat(dat, DEFAULT_CONFIG))
            self.assertEqual(len(recs), 15)
            self.assertNotEqual(recs[0], FIELD_NAMES)

    def test_csv_roundtrip_utf8(self):
        cfg = DatConfig(encoding="utf-8")
        report = verify_roundtrip_csv(FIXTURE, config=cfg, field_names=FIELD_NAMES)
        self.assertTrue(report.ok, msg=report.summary())
        self.assertEqual(report.field_differences, 0)

    def test_csv_roundtrip_crlf(self):
        cfg = DatConfig(record_terminator="\r\n")
        report = verify_roundtrip_csv(FIXTURE, config=cfg, field_names=FIELD_NAMES)
        self.assertTrue(report.ok, msg=report.summary())

    def test_empty_csv_roundtrip_ok(self):
        with tempfile.TemporaryDirectory() as work:
            empty = os.path.join(work, "empty.csv")
            with open(empty, "w", encoding="utf-8", newline="") as fh:
                fh.write("A,B\n")
            report = verify_roundtrip_csv(empty, config=DEFAULT_CONFIG)
            self.assertTrue(report.ok, msg=report.summary())
            self.assertEqual(report.input_records, 0)


class RoundTripDATTests(unittest.TestCase):
    """DAT -> CSV -> DAT on the real sample slice."""

    def test_dat_roundtrip(self):
        with tempfile.TemporaryDirectory() as work:
            dat = os.path.join(work, "slice.dat")
            # First produce a DAT from the fixture, then verify DAT->CSV->DAT.
            csv_to_dat(FIXTURE, dat, config=DEFAULT_CONFIG, field_names=FIELD_NAMES, emit_dct=True)
            report = verify_roundtrip_dat(dat, DEFAULT_CONFIG, FIELD_NAMES)
            self.assertTrue(report.ok, msg=report.summary())
            self.assertEqual(report.field_differences, 0)


class EdgeCaseTests(unittest.TestCase):
    """Quoting, empty fields, tildes, embedded quote char, embedded newline."""

    def test_empty_and_quoted_commas_and_tildes(self):
        rows = [
            ["A", "", "has,comma", "til~~de~~more"],
            ["B", "B2", 'name "with quotes"', "plain"],
        ]
        with tempfile.TemporaryDirectory() as work:
            csv_in = os.path.join(work, "in.csv")
            dat = os.path.join(work, "out.dat")
            csv_out = os.path.join(work, "rt.csv")
            with open(csv_in, "w", encoding="utf-8", newline="") as fh:
                import csv as _csv
                w = _csv.writer(fh)
                w.writerow(["F1", "F2", "F3", "F4"])
                for r in rows:
                    w.writerow(r)
            n = csv_to_dat(csv_in, dat, config=DEFAULT_CONFIG,
                           field_names=["F1", "F2", "F3", "F4"], emit_dct=False)
            self.assertEqual(n, 2)
            n2, names = dat_to_csv(dat, csv_out, config=DEFAULT_CONFIG,
                                   field_names=["F1", "F2", "F3", "F4"])
            self.assertEqual(n2, 2)
            got = [row for _, row in read_csv(csv_out, encoding=DEFAULT_CONFIG.encoding)]
            self.assertEqual(got, rows)

    def test_embedded_quote_char_doubling(self):
        # The thorn quote char inside a value must be doubled on write and
        # restored on read.
        cfg = DEFAULT_CONFIG
        thorn = cfg.quote_char
        rows = [["before" + thorn + "after", "x"]]
        with tempfile.TemporaryDirectory() as work:
            dat = os.path.join(work, "q.dat")
            write_dat(dat, iter(rows), ["F1", "F2"], cfg, emit_dct=False)
            rec = next(read_dat(dat, cfg, skip_header=True, header_names=["F1", "F2"]))
            self.assertEqual(rec, ["before" + thorn + "after", "x"])

    def test_embedded_newline_encoded_as_0xAE(self):
        cfg = DEFAULT_CONFIG
        rows = [["line1\nline2", "x"], ["a\rb", "c"], ["d\r\ne", "f"]]
        with tempfile.TemporaryDirectory() as work:
            dat = os.path.join(work, "nl.dat")
            write_dat(dat, iter(rows), ["F1", "F2"], cfg, emit_dct=False)
            recs = list(read_dat(dat, cfg, skip_header=True, header_names=["F1", "F2"]))
            self.assertEqual(recs, [["line1\nline2", "x"], ["a\nb", "c"], ["d\ne", "f"]])
            with open(dat, "rb") as fh:
                data = fh.read()
            # In-field newlines must be 0xAE, not raw 0x0A inside the payload.
            # Count of real LF terminators == header + data records.
            self.assertEqual(data.count(b"\n"), 4)
            self.assertIn(b"\xae", data)
            # No raw CR left in the file.
            self.assertNotIn(b"\r", data)

    def test_dct_roundtrip_ignores_comments(self):
        with tempfile.TemporaryDirectory() as work:
            dct = os.path.join(work, "names.dct")
            converter.write_dct(dct, FIELD_NAMES, "cp1252")
            with open(dct, "r", encoding="cp1252") as fh:
                raw = fh.read()
            self.assertIn("NOT a Concordance CPL dictionary", raw)
            got = converter.read_dct(dct, "cp1252")
            self.assertEqual(got, FIELD_NAMES)

    def test_inferred_field_names(self):
        """Legacy DAT with no header row → FIELD1..N when nothing else given."""
        with tempfile.TemporaryDirectory() as work:
            dat = os.path.join(work, "noheader.dat")
            write_dat(
                dat, iter([["1", "2", "3"]]), ["A", "B", "C"],
                DEFAULT_CONFIG, emit_dct=False, emit_header=False,
            )
            csv_out = os.path.join(work, "out.csv")
            n, names = dat_to_csv(dat, csv_out, config=DEFAULT_CONFIG,
                                  field_names=None, infer_if_missing=True)
            self.assertEqual(names, ["FIELD1", "FIELD2", "FIELD3"])
            self.assertEqual(n, 1)

    def test_header_row_supplies_field_names(self):
        """DAT with Concordance header and no .dct → names come from header."""
        with tempfile.TemporaryDirectory() as work:
            dat = os.path.join(work, "hdr.dat")
            write_dat(
                dat, iter([["1", "2", "3"]]), ["BEGDOC", "ENDDOC", "FILEPATH"],
                DEFAULT_CONFIG, emit_dct=False, emit_header=True,
            )
            csv_out = os.path.join(work, "out.csv")
            n, names = dat_to_csv(dat, csv_out, config=DEFAULT_CONFIG,
                                  field_names=None, infer_if_missing=True)
            self.assertEqual(names, ["BEGDOC", "ENDDOC", "FILEPATH"])
            self.assertEqual(n, 1)

    def test_looks_like_header_rejects_bates(self):
        from converter import _looks_like_header_row
        self.assertFalse(_looks_like_header_row(
            ["DAVILLA_RESTRICTED_ACCESS_000001", "DAVILLA_RESTRICTED_ACCESS_000001", "", "file.docx"]
        ))
        self.assertFalse(_looks_like_header_row(["X", "Y"]))
        self.assertFalse(_looks_like_header_row(["A", "B"]))
        self.assertTrue(_looks_like_header_row(["BEGDOC", "ENDDOC", "CODED", "FILEPATH"]))

    def test_dct_count_mismatch_raises(self):
        with tempfile.TemporaryDirectory() as work:
            csv_in = os.path.join(work, "in.csv")
            with open(csv_in, "w", encoding="utf-8", newline="") as fh:
                fh.write("A,B\n1,2\n")
            dat = os.path.join(work, "o.dat")
            csv_to_dat(csv_in, dat, DEFAULT_CONFIG, emit_dct=True)
            converter.write_dct(os.path.splitext(dat)[0] + ".dct", ["ONLY_ONE"], "cp1252")
            csv_out = os.path.join(work, "out.csv")
            with self.assertRaises(ValueError) as ctx:
                dat_to_csv(dat, csv_out, DEFAULT_CONFIG, field_names=None)
            self.assertIn(".dct has 1", str(ctx.exception))


class FieldCountTests(unittest.TestCase):
    """Fixed-width schema enforcement."""

    def test_short_row_padded(self):
        with tempfile.TemporaryDirectory() as work:
            csv_in = os.path.join(work, "s.csv")
            with open(csv_in, "w", encoding="utf-8", newline="") as fh:
                fh.write("A,B,C\n1,2\n")
            dat = os.path.join(work, "s.dat")
            csv_to_dat(csv_in, dat, DEFAULT_CONFIG, emit_dct=False)
            self.assertEqual(
                list(read_dat(dat, DEFAULT_CONFIG, skip_header=True, header_names=["A", "B", "C"])),
                [["1", "2", ""]],
            )

    def test_overlong_row_rejected(self):
        with tempfile.TemporaryDirectory() as work:
            csv_in = os.path.join(work, "l.csv")
            with open(csv_in, "w", encoding="utf-8", newline="") as fh:
                fh.write("A,B\n1,2,3,4\n")
            dat = os.path.join(work, "l.dat")
            with self.assertRaises(ValueError) as ctx:
                csv_to_dat(csv_in, dat, DEFAULT_CONFIG, emit_dct=False)
            self.assertIn("expects 2", str(ctx.exception))

    def test_overlong_row_truncated(self):
        cfg = DatConfig(field_count_mode="pad_truncate")
        with tempfile.TemporaryDirectory() as work:
            csv_in = os.path.join(work, "l.csv")
            with open(csv_in, "w", encoding="utf-8", newline="") as fh:
                fh.write("A,B\n1,2,3,4\n")
            dat = os.path.join(work, "l.dat")
            csv_to_dat(csv_in, dat, cfg, emit_dct=False)
            self.assertEqual(
                list(read_dat(dat, cfg, skip_header=True, header_names=["A", "B"])),
                [["1", "2"]],
            )

    def test_blank_csv_lines_skipped(self):
        """Blank lines after the header must not become empty DAT records."""
        with tempfile.TemporaryDirectory() as work:
            csv_in = os.path.join(work, "b.csv")
            with open(csv_in, "w", encoding="utf-8", newline="") as fh:
                fh.write("A,B\n1,2\n\n3,4\n   \n")
            dat = os.path.join(work, "b.dat")
            n = csv_to_dat(csv_in, dat, DEFAULT_CONFIG, emit_dct=False)
            self.assertEqual(n, 2)
            self.assertEqual(
                list(read_dat(dat, DEFAULT_CONFIG, skip_header=True, header_names=["A", "B"])),
                [["1", "2"], ["3", "4"]],
            )

    def test_write_dat_rejects_uneven_vs_schema(self):
        with tempfile.TemporaryDirectory() as work:
            dat = os.path.join(work, "x.dat")
            with self.assertRaises(ValueError):
                write_dat(
                    dat,
                    iter([["a", "b"], ["1", "2", "3", "4"]]),
                    ["A", "B", "C"],
                    DEFAULT_CONFIG,
                    emit_dct=False,
                )


class MultiValueTests(unittest.TestCase):
    """multi_value_sep is applied on write."""

    def test_legacy_rs_rewritten_to_semicolon(self):
        rs = chr(0x1E)
        rows = [["tag" + rs + "tag2", "x"]]
        with tempfile.TemporaryDirectory() as work:
            dat = os.path.join(work, "mv.dat")
            write_dat(dat, iter(rows), ["TAGS", "F2"], DEFAULT_CONFIG, emit_dct=False)
            rec = next(read_dat(dat, DEFAULT_CONFIG, skip_header=True, header_names=["TAGS", "F2"]))
            self.assertEqual(rec, ["tag;tag2", "x"])
            with open(dat, "rb") as fh:
                raw = fh.read()
            self.assertNotIn(b"\x1e", raw)
            self.assertIn(b";", raw)

    def test_normalize_can_be_disabled(self):
        rs = chr(0x1E)
        cfg = DatConfig(normalize_multivalue=False, multi_value_sep=";")
        rows = [["tag" + rs + "tag2"]]
        with tempfile.TemporaryDirectory() as work:
            dat = os.path.join(work, "mv.dat")
            write_dat(dat, iter(rows), ["TAGS"], cfg, emit_dct=False)
            rec = next(read_dat(dat, cfg, skip_header=True, header_names=["TAGS"]))
            self.assertEqual(rec, ["tag" + rs + "tag2"])


class EncodingErrorTests(unittest.TestCase):
    """Unencodable characters under cp1252."""

    def test_emoji_fails_strict(self):
        with tempfile.TemporaryDirectory() as work:
            csv_in = os.path.join(work, "e.csv")
            with open(csv_in, "w", encoding="utf-8", newline="") as fh:
                fh.write("F\nhello\U0001F600\n")
            dat = os.path.join(work, "e.dat")
            with self.assertRaises(UnicodeEncodeError):
                csv_to_dat(csv_in, dat, DEFAULT_CONFIG, emit_dct=False)

    def test_emoji_replace_succeeds(self):
        cfg = DatConfig(encoding_errors="replace")
        with tempfile.TemporaryDirectory() as work:
            csv_in = os.path.join(work, "e.csv")
            with open(csv_in, "w", encoding="utf-8", newline="") as fh:
                fh.write("F\nhello\U0001F600\n")
            dat = os.path.join(work, "e.dat")
            n = csv_to_dat(csv_in, dat, cfg, emit_dct=False)
            self.assertEqual(n, 1)
            rec = next(read_dat(dat, cfg, skip_header=True, header_names=["F"]))
            self.assertTrue(rec[0].startswith("hello"))


class ValidationTests(unittest.TestCase):
    """Semantic Bates / path validation."""

    def test_unique_begdoc_pass(self):
        report = converter.validate_csv_file(
            FIXTURE, field_names=FIELD_NAMES, require_begdoc=True
        )
        self.assertTrue(report.ok, msg=report.summary())
        self.assertEqual(report.records, 15)
        self.assertEqual(report.begdoc_field, "BEGDOC")

    def test_duplicate_begdoc_fails(self):
        with tempfile.TemporaryDirectory() as work:
            csv_in = os.path.join(work, "dup.csv")
            with open(csv_in, "w", encoding="utf-8", newline="") as fh:
                fh.write("BEGDOC,ENDDOC,FILEPATH\nA,A,f.pdf\nA,A,g.pdf\n")
            report = converter.validate_csv_file(csv_in)
            self.assertFalse(report.ok)
            self.assertTrue(any("duplicate" in e for e in report.errors))

    def test_enddoc_before_begdoc_fails(self):
        with tempfile.TemporaryDirectory() as work:
            csv_in = os.path.join(work, "ord.csv")
            with open(csv_in, "w", encoding="utf-8", newline="") as fh:
                fh.write("BEGDOC,ENDDOC\nPREFIX_000010,PREFIX_000009\n")
            report = converter.validate_csv_file(csv_in)
            self.assertFalse(report.ok)
            self.assertTrue(any("before BEGDOC" in e for e in report.errors))

    def test_missing_begdoc_column_fails(self):
        with tempfile.TemporaryDirectory() as work:
            csv_in = os.path.join(work, "nob.csv")
            with open(csv_in, "w", encoding="utf-8", newline="") as fh:
                fh.write("FOO,BAR\n1,2\n")
            report = converter.validate_csv_file(csv_in, require_begdoc=True)
            self.assertFalse(report.ok)

    def test_native_exists_check(self):
        with tempfile.TemporaryDirectory() as work:
            native = os.path.join(work, "doc.pdf")
            with open(native, "wb") as fh:
                fh.write(b"%PDF")
            csv_in = os.path.join(work, "n.csv")
            with open(csv_in, "w", encoding="utf-8", newline="") as fh:
                fh.write("BEGDOC,FILEPATH\nDOC1,doc.pdf\nDOC2,missing.pdf\n")
            report = converter.validate_csv_file(
                csv_in, check_filepath_exists=True, natives_root=work
            )
            self.assertFalse(report.ok)
            self.assertTrue(any("missing.pdf" in e for e in report.errors))


class OptTests(unittest.TestCase):
    """Opticon .opt companion writer."""

    def test_write_opt_single_page(self):
        with tempfile.TemporaryDirectory() as work:
            opt = os.path.join(work, "vol.opt")
            n = converter.write_opt(
                opt,
                iter([["DOC001"], ["DOC002"]]),
                volume="VOL001",
                image_dir=r"IMAGES\001",
                image_ext=".tif",
            )
            self.assertEqual(n, 2)
            with open(opt, encoding="cp1252") as fh:
                lines = fh.read().splitlines()
            self.assertEqual(
                lines[0],
                r"DOC001,VOL001,IMAGES\001\DOC001.tif,Y,,,1",
            )
            self.assertEqual(
                lines[1],
                r"DOC002,VOL001,IMAGES\001\DOC002.tif,Y,,,1",
            )

    def test_dat_to_opt(self):
        with tempfile.TemporaryDirectory() as work:
            dat = os.path.join(work, "v.dat")
            csv_to_dat(FIXTURE, dat, DEFAULT_CONFIG, field_names=FIELD_NAMES, emit_dct=True)
            opt = os.path.join(work, "v.opt")
            n = converter.dat_to_opt(
                dat, opt, DEFAULT_CONFIG, field_names=FIELD_NAMES,
                volume="VOL001", image_dir=r"IMAGES\001",
            )
            self.assertEqual(n, 15)
            with open(opt, encoding="cp1252") as fh:
                first = fh.readline().rstrip("\n")
            self.assertTrue(first.startswith("DAVILLA_RESTRICTED_ACCESS_000001,VOL001,"))
            self.assertIn(",Y,,,1", first)


class ConfigValidationTests(unittest.TestCase):
    """Control characters must be pairwise distinct (corruption guard)."""

    def test_newline_equal_to_quote_rejected(self):
        with self.assertRaises(ValueError):
            DatConfig(newline_char=chr(0xFE))

    def test_multival_equal_to_delim_rejected(self):
        with self.assertRaises(ValueError):
            DatConfig(multi_value_sep=chr(0x14))

    def test_real_newline_as_control_char_rejected(self):
        with self.assertRaises(ValueError):
            DatConfig(newline_char="\n")


class MergeFieldCountTests(unittest.TestCase):
    """--merge-schema honors --field-count-mode against the source header."""

    def test_merge_rejects_overlong_row(self):
        with tempfile.TemporaryDirectory() as work:
            csv_in = os.path.join(work, "t.csv")
            with open(csv_in, "w", encoding="utf-8", newline="") as fh:
                fh.write("BEGDOC,Filename\nD1,a.docx,EXTRA,MORE\n")
            dat = os.path.join(work, "t.dat")
            with self.assertRaises(ValueError):
                csv_to_dat(
                    csv_in, dat, DEFAULT_CONFIG,
                    field_names=["BEGDOC", "ENDDOC", "FILEPATH"],
                    emit_dct=False, merge_schema=True,
                )

    def test_merge_truncate_mode_accepts_overlong(self):
        cfg = DatConfig(field_count_mode="pad_truncate")
        with tempfile.TemporaryDirectory() as work:
            csv_in = os.path.join(work, "t.csv")
            with open(csv_in, "w", encoding="utf-8", newline="") as fh:
                fh.write("BEGDOC,Filename\nD1,a.docx,EXTRA\n")
            dat = os.path.join(work, "t.dat")
            n = csv_to_dat(
                csv_in, dat, cfg,
                field_names=["BEGDOC", "FILEPATH"],
                emit_dct=False, merge_schema=True,
            )
            self.assertEqual(n, 1)

    def test_all_empty_multicell_row_preserved(self):
        """An explicit ',,,' row is a real (all-blank) record, not a blank line."""
        with tempfile.TemporaryDirectory() as work:
            csv_in = os.path.join(work, "e.csv")
            with open(csv_in, "w", encoding="utf-8", newline="") as fh:
                fh.write("A,B\n1,2\n,\n")
            dat = os.path.join(work, "e.dat")
            n = csv_to_dat(csv_in, dat, DEFAULT_CONFIG, emit_dct=False)
            self.assertEqual(n, 2)
            rows = list(read_dat(dat, DEFAULT_CONFIG, skip_header=True,
                                 header_names=["A", "B"]))
            self.assertEqual(rows, [["1", "2"], ["", ""]])


class ValidationSemanticsTests(unittest.TestCase):
    """Numeric Bates compare, range overlap, family integrity."""

    def test_numeric_enddoc_compare_not_lexicographic(self):
        """PREFIX_9 -> PREFIX_10 is valid despite '10' < '9' lexicographically."""
        rep = converter.validate_load_records(
            ["BEGDOC", "ENDDOC"],
            iter([["DOC_9", "DOC_10"]]),
        )
        self.assertTrue(rep.ok, msg=rep.summary())

    def test_enddoc_numerically_before_begdoc_fails(self):
        rep = converter.validate_load_records(
            ["BEGDOC", "ENDDOC"],
            iter([["DOC_010", "DOC_009"]]),
        )
        self.assertFalse(rep.ok)

    def test_overlapping_bates_ranges_fail(self):
        rep = converter.validate_load_records(
            ["BEGDOC", "ENDDOC"],
            iter([["DOC_001", "DOC_010"], ["DOC_005", "DOC_008"]]),
        )
        self.assertFalse(rep.ok)
        self.assertTrue(any("overlaps" in e for e in rep.errors))

    def test_begattach_must_reference_existing_begdoc(self):
        rep = converter.validate_load_records(
            ["BEGDOC", "ENDDOC", "BEGATTACH"],
            iter([
                ["DOC_001", "DOC_001", "DOC_001"],
                ["DOC_002", "DOC_002", "DOC_999"],
            ]),
        )
        self.assertFalse(rep.ok)
        self.assertTrue(any("BEGATTACH" in e for e in rep.errors))

    def test_valid_family_passes(self):
        rep = converter.validate_load_records(
            ["BEGDOC", "ENDDOC", "BEGATTACH"],
            iter([
                ["DOC_001", "DOC_002", "DOC_001"],
                ["DOC_003", "DOC_003", "DOC_001"],
            ]),
        )
        self.assertTrue(rep.ok, msg=rep.summary())


class HardeningTests(unittest.TestCase):
    """write_dat / write_opt input hardening."""

    def test_field_name_with_control_char_rejected(self):
        with tempfile.TemporaryDirectory() as work:
            dat = os.path.join(work, "bad.dat")
            with self.assertRaises(ValueError):
                write_dat(dat, iter([["v"]]), ["BAD" + chr(0xFE) + "NAME"],
                          DEFAULT_CONFIG, emit_dct=False)

    def test_empty_schema_with_rows_leaves_no_artifacts(self):
        with tempfile.TemporaryDirectory() as work:
            dat = os.path.join(work, "x.dat")
            with self.assertRaises(ValueError):
                write_dat(dat, iter([["a"]]), [], DEFAULT_CONFIG, emit_dct=True)
            self.assertFalse(os.path.exists(dat))
            self.assertFalse(os.path.exists(os.path.join(work, "x.dct")))

    def test_opt_comma_in_volume_rejected(self):
        with tempfile.TemporaryDirectory() as work:
            opt = os.path.join(work, "x.opt")
            with self.assertRaises(ValueError):
                converter.write_opt(opt, iter([["DOC1"]]), volume="VOL,001")

    def test_roundtrip_csv_encoding_cp1252(self):
        """Latin-1/cp1252 source CSVs verify correctly via csv_encoding."""
        with tempfile.TemporaryDirectory() as work:
            csv_in = os.path.join(work, "l.csv")
            with open(csv_in, "w", encoding="cp1252", newline="") as fh:
                fh.write("BEGDOC,NOTE\nD1,caf\xe9 r\xe9sum\xe9\n")
            rep = verify_roundtrip_csv(csv_in, DEFAULT_CONFIG,
                                       csv_encoding="cp1252")
            self.assertTrue(rep.ok, msg=rep.summary())

    def test_relativity_style_header_detected(self):
        """Foreign DAT with mostly-custom names + >=2 known ones is a header."""
        row = ["Control Number", "Custodian", "Email Thread ID",
               "Conversation Index", "Confidentiality", "Date Sent"]
        self.assertTrue(converter._looks_like_header_row(row))

    def test_bates_data_row_not_detected_as_header(self):
        row = ["DAVILLA_RESTRICTED_ACCESS_000001",
               "DAVILLA_RESTRICTED_ACCESS_000001", "", "file.docx"]
        self.assertFalse(converter._looks_like_header_row(row))

    def test_repeated_cells_not_detected_as_header(self):
        """BEGDOC==ENDDOC one-pager data must not look like a header."""
        row = ["custodian", "custodian"]
        self.assertFalse(converter._looks_like_header_row(row))


class BomAndEncodingTests(unittest.TestCase):
    """BOM handling per Relativity RDC (BOM used for encoding auto-detect)."""

    def test_utf8_bom_dat_parses(self):
        """A UTF-8 DAT with a leading BOM (Relativity-style) must parse."""
        cfg = DatConfig(encoding="utf-8")
        with tempfile.TemporaryDirectory() as work:
            dat = os.path.join(work, "bom.dat")
            write_dat(dat, iter([["D1", "a.docx"]]), ["BEGDOC", "FILEPATH"],
                      cfg, emit_dct=False)
            with open(dat, "rb") as fh:
                raw = fh.read()
            with open(dat, "wb") as fh:
                fh.write(b"\xef\xbb\xbf" + raw)
            rows = list(read_dat(dat, cfg, skip_header=True,
                                 header_names=["BEGDOC", "FILEPATH"]))
            self.assertEqual(rows, [["D1", "a.docx"]])

    def test_utf16_roundtrip_single_bom(self):
        """utf-16 writes exactly one BOM at file start and round-trips."""
        cfg = DatConfig(encoding="utf-16")
        with tempfile.TemporaryDirectory() as work:
            dat = os.path.join(work, "u16.dat")
            write_dat(dat, iter([["D1", "x"], ["D2", "y"]]),
                      ["BEGDOC", "FILEPATH"], cfg, emit_dct=False)
            with open(dat, "rb") as fh:
                raw = fh.read()
            self.assertTrue(raw.startswith(b"\xff\xfe"))
            self.assertEqual(raw.count(b"\xff\xfe"), 1)
            rows = list(read_dat(dat, cfg, skip_header=True,
                                 header_names=["BEGDOC", "FILEPATH"]))
            self.assertEqual(rows, [["D1", "x"], ["D2", "y"]])

    def test_cp1252_has_no_bom(self):
        """Classic cp1252 DAT must still start with the raw thorn byte."""
        with tempfile.TemporaryDirectory() as work:
            dat = os.path.join(work, "c.dat")
            write_dat(dat, iter([["a", "b"]]), ["A", "B"],
                      DEFAULT_CONFIG, emit_dct=False)
            with open(dat, "rb") as fh:
                first = fh.read(1)
            self.assertEqual(first, b"\xfe")

    def test_duplicate_header_names_warn(self):
        """Duplicate header names warn (Logikcull requires unique names)."""
        import contextlib
        import io as _io
        with tempfile.TemporaryDirectory() as work:
            csv_in = os.path.join(work, "dup.csv")
            with open(csv_in, "w", encoding="utf-8", newline="") as fh:
                fh.write("BEGDOC,BEGDOC,FILEPATH\nA,B,f.docx\n")
            dat = os.path.join(work, "dup.dat")
            err = _io.StringIO()
            with contextlib.redirect_stderr(err):
                csv_to_dat(csv_in, dat, DEFAULT_CONFIG, emit_dct=False)
            self.assertIn("duplicate field names", err.getvalue())


class SchemaMergeTests(unittest.TestCase):
    """Name-based schema merge keeps every target field, even when blank."""

    def test_project_row_pads_missing_fields(self):
        out = converter.project_row(
            ["Bates/Control #", "Filename"],
            ["DOC001", "a.docx"],
            ["BEGDOC", "ENDDOC", "BEGATTACH", "FILEPATH", "CUSTODIAN"],
        )
        self.assertEqual(out, ["DOC001", "", "", "a.docx", ""])

    def test_project_row_keeps_filepath_and_nativepath_distinct(self):
        out = converter.project_row(
            ["BegDoc", "FilePath", "NativePath"],
            ["ABC1", r"\\share\x.msg", r"natives\x.msg"],
            ["BEGDOC", "FILEPATH", "NATIVEPATH"],
        )
        self.assertEqual(out, ["ABC1", r"\\share\x.msg", r"natives\x.msg"])

    def test_docid_not_aliased_to_begdoc(self):
        """C1: DOCID and BEGDOC are distinct production fields."""
        self.assertNotEqual(
            converter._canonical_field_key("DOCID"),
            converter._canonical_field_key("BEGDOC"),
        )
        out = converter.project_row(
            ["BEGDOC", "DOCID"],
            ["BATES001", "DOC-99"],
            ["BEGDOC", "DOCID"],
        )
        self.assertEqual(out, ["BATES001", "DOC-99"])

    def test_author_not_aliased_to_from(self):
        """C1: Author and From must not collapse."""
        self.assertNotEqual(
            converter._canonical_field_key("Author"),
            converter._canonical_field_key("FROM"),
        )

    def test_filename_also_fills_filepath_when_both_in_schema(self):
        """Thin CSV Filename populates both FILENAME and empty FILEPATH."""
        out = converter.project_row(
            ["Bates/Control #", "Filename"],
            ["DOC1", "a.docx"],
            ["BEGDOC", "FILEPATH", "FILENAME"],
        )
        self.assertEqual(out, ["DOC1", "a.docx", "a.docx"])

    def test_analyze_reports_dropped_and_collisions(self):
        report = converter.analyze_schema_merge(
            ["BEGDOC", "Control Number", "SECRET_NOTE", "Filename"],
            ["BEGDOC", "FILEPATH"],
        )
        self.assertIn("SECRET_NOTE", report.dropped)
        self.assertIn("BEGDOC", report.collisions)
        self.assertEqual(sorted(report.collisions["BEGDOC"]), ["BEGDOC", "Control Number"])
        self.assertFalse(report.ok)

    def test_strict_merge_raises_on_dropped(self):
        with tempfile.TemporaryDirectory() as work:
            csv_in = os.path.join(work, "t.csv")
            with open(csv_in, "w", encoding="utf-8", newline="") as fh:
                fh.write("BEGDOC,SECRET_NOTE,Filename\nD1,do-not-lose,a.docx\n")
            dat = os.path.join(work, "t.dat")
            with self.assertRaises(ValueError) as ctx:
                csv_to_dat(
                    csv_in,
                    dat,
                    DEFAULT_CONFIG,
                    field_names=["BEGDOC", "FILEPATH"],
                    emit_dct=False,
                    merge_schema=True,
                    strict_merge=True,
                )
            self.assertIn("DROPPED", str(ctx.exception))
            self.assertIn("SECRET_NOTE", str(ctx.exception))

    def test_strict_merge_raises_on_collision(self):
        with tempfile.TemporaryDirectory() as work:
            csv_in = os.path.join(work, "t.csv")
            with open(csv_in, "w", encoding="utf-8", newline="") as fh:
                fh.write("BEGDOC,Control Number\nA,B\n")
            dat = os.path.join(work, "t.dat")
            with self.assertRaises(ValueError) as ctx:
                csv_to_dat(
                    csv_in,
                    dat,
                    DEFAULT_CONFIG,
                    field_names=["BEGDOC", "ENDDOC"],
                    emit_dct=False,
                    merge_schema=True,
                    strict_merge=True,
                )
            self.assertIn("COLLISION", str(ctx.exception))

    def test_merge_warns_but_continues_without_strict(self):
        with tempfile.TemporaryDirectory() as work:
            csv_in = os.path.join(work, "t.csv")
            with open(csv_in, "w", encoding="utf-8", newline="") as fh:
                fh.write("BEGDOC,SECRET_NOTE,Filename\nD1,note,a.docx\n")
            dat = os.path.join(work, "t.dat")
            n = csv_to_dat(
                csv_in,
                dat,
                DEFAULT_CONFIG,
                field_names=["BEGDOC", "FILEPATH"],
                emit_dct=False,
                merge_schema=True,
                strict_merge=False,
            )
            self.assertEqual(n, 1)
            rows = list(
                read_dat(dat, DEFAULT_CONFIG, skip_header=True, header_names=["BEGDOC", "FILEPATH"])
            )
            self.assertEqual(rows, [["D1", "a.docx"]])

    def test_csv_merge_schema_emits_all_fields(self):
        schema = list(converter.STANDARD_NATIVE_FIELDS)
        with tempfile.TemporaryDirectory() as work:
            dat = os.path.join(work, "full.dat")
            n = csv_to_dat(
                FIXTURE,
                dat,
                DEFAULT_CONFIG,
                field_names=schema,
                emit_dct=False,
                merge_schema=True,
            )
            self.assertEqual(n, 15)
            rows = list(
                read_dat(dat, DEFAULT_CONFIG, skip_header=True, header_names=schema)
            )
            self.assertEqual(len(rows[0]), len(schema))
            self.assertEqual(rows[0][schema.index("BEGDOC")], "DAVILLA_RESTRICTED_ACCESS_000001")
            self.assertEqual(rows[0][schema.index("FILENAME")], "PLACEHOLDER - SNAPCHAT.docx")
            self.assertEqual(rows[0][schema.index("FILEPATH")], "PLACEHOLDER - SNAPCHAT.docx")
            self.assertEqual(rows[0][schema.index("CUSTODIAN")], "")
            self.assertEqual(rows[0][schema.index("BEGATTACH")], "")
            # Coded column from thin CSV is retained (STANDARD includes CODED).
            self.assertIn("CODED", schema)
            with open(dat, "rb") as fh:
                raw = fh.read()
            self.assertIn(b"\xfe\xfe", raw)

    def test_merge_schema_requires_matching_or_flag(self):
        with tempfile.TemporaryDirectory() as work:
            dat = os.path.join(work, "x.dat")
            with self.assertRaises(ValueError) as ctx:
                csv_to_dat(
                    FIXTURE,
                    dat,
                    DEFAULT_CONFIG,
                    field_names=list(converter.STANDARD_NATIVE_FIELDS),
                    emit_dct=False,
                    merge_schema=False,
                )
            self.assertIn("merge-schema", str(ctx.exception))

    def test_classic_starter_fixture_roundtrip(self):
        classic = os.path.join(HERE, "fixtures", "classic", "starter_sample_classic.dat")
        if not os.path.exists(classic):
            self.skipTest("classic starter fixture not generated yet")
        # Starter classic was written with 17 fields (no CODED); read its header.
        header = list(read_dat(classic, DEFAULT_CONFIG))[0]
        report = verify_roundtrip_dat(classic, DEFAULT_CONFIG, header)
        self.assertTrue(report.ok, msg=report.summary())
        self.assertEqual(report.input_records, 2)

    def test_classic_relativity_fixture_preserves_blanks(self):
        classic = os.path.join(
            HERE, "fixtures", "classic", "relativity_load_file_01_classic.dat"
        )
        if not os.path.exists(classic):
            self.skipTest("classic relativity fixture not generated yet")
        # Header is inside the DAT (Relativity field names).
        header = list(read_dat(classic, DEFAULT_CONFIG))[0]
        rows = list(
            read_dat(classic, DEFAULT_CONFIG, skip_header=True, header_names=header)
        )
        self.assertEqual(len(header), 23)
        self.assertEqual(len(rows), 4)
        self.assertEqual(len(rows[0]), 23)
        # Relativity sample leaves several email/date fields blank.
        self.assertEqual(rows[0][header.index("Date Created")], "")
        self.assertEqual(rows[0][header.index("Email BCC")], "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
