"""gstack-qa pass 2 — post Concordance header-row fix."""
from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

PKG = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PKG)

from converter import (  # noqa: E402
    DEFAULT_CONFIG,
    DatConfig,
    csv_to_dat,
    dat_to_csv,
    dat_to_opt,
    read_dat,
    validate_csv_file,
    validate_dat_file,
    verify_roundtrip_csv,
    verify_roundtrip_dat,
    write_dat,
    write_dct,
    _looks_like_header_row,
)
import cli  # noqa: E402

FIXTURE = os.path.join(PKG, "tests", "fixtures", "VOL001_slice.csv")
SAMPLE = (
    r"G:\My Drive\GLG - Google Drive\Cases\Open\Davila, Niger"
    r"\DISCOVERY\2026-06-23\KVH_DAVILLA_Niger\KVH_DAVILLA_Niger\VOL001.csv"
)
NAMES = ["BEGDOC", "ENDDOC", "CODED", "FILEPATH"]

findings: list = []
results: list = []


def find(sev: str, title: str, detail: str) -> None:
    findings.append((sev, title, detail))
    print(f"FINDING [{sev}] {title}: {detail[:220]}")


def case(name: str, fn) -> None:
    try:
        fn()
        results.append((name, True, ""))
        print(f"PASS  {name}")
    except AssertionError as e:
        results.append((name, False, str(e)))
        print(f"FAIL  {name}: {e}")
        find("high", f"QA case failed: {name}", str(e))
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        results.append((name, False, msg))
        print(f"ERROR {name}: {msg}")
        find("high", f"QA case error: {name}", msg)


def t_unit() -> None:
    suite = unittest.TestLoader().discover(os.path.join(PKG, "tests"), pattern="test_*.py")
    r = unittest.TextTestRunner(verbosity=0, stream=open(os.devnull, "w")).run(suite)
    assert r.wasSuccessful(), f"failures={len(r.failures)} errors={len(r.errors)}"
    assert r.testsRun >= 30, r.testsRun


def t_header_written() -> None:
    with tempfile.TemporaryDirectory() as w:
        dat = os.path.join(w, "o.dat")
        assert cli.main(["csv2dat", FIXTURE, dat, "--field-names", ",".join(NAMES)]) == 0
        recs = list(read_dat(dat, DEFAULT_CONFIG))
        assert recs[0] == NAMES, recs[0]
        assert len(recs) == 16
        assert open(dat, "rb").read(1) == b"\xfe"


def t_dat2csv_skips_header() -> None:
    with tempfile.TemporaryDirectory() as w:
        dat = os.path.join(w, "o.dat")
        cli.main(["csv2dat", FIXTURE, dat, "--field-names", ",".join(NAMES)])
        out = os.path.join(w, "o.csv")
        n, names = dat_to_csv(dat, out, DEFAULT_CONFIG, field_names=NAMES)
        assert n == 15 and names == NAMES
        lines = open(out, encoding="cp1252").read().splitlines()
        assert lines[0] == "BEGDOC,ENDDOC,CODED,FILEPATH"
        assert len(lines) == 16


def t_infer_from_header() -> None:
    with tempfile.TemporaryDirectory() as w:
        dat = os.path.join(w, "o.dat")
        cli.main(["csv2dat", FIXTURE, dat, "--field-names", ",".join(NAMES)])
        out = os.path.join(w, "o.csv")
        n, names = dat_to_csv(dat, out, DEFAULT_CONFIG, field_names=None, infer_if_missing=True)
        assert names == NAMES and n == 15


def t_no_header() -> None:
    with tempfile.TemporaryDirectory() as w:
        dat = os.path.join(w, "o.dat")
        cli.main(["csv2dat", FIXTURE, dat, "--field-names", ",".join(NAMES), "--no-header"])
        recs = list(read_dat(dat, DEFAULT_CONFIG))
        assert len(recs) == 15 and recs[0] != NAMES


def t_validate_skips() -> None:
    with tempfile.TemporaryDirectory() as w:
        dat = os.path.join(w, "o.dat")
        cli.main(["csv2dat", FIXTURE, dat, "--field-names", ",".join(NAMES)])
        rep = validate_dat_file(dat, NAMES, DEFAULT_CONFIG)
        assert rep.ok and rep.records == 15, rep.summary()


def t_opt_skips() -> None:
    with tempfile.TemporaryDirectory() as w:
        dat = os.path.join(w, "o.dat")
        cli.main(["csv2dat", FIXTURE, dat, "--field-names", ",".join(NAMES)])
        opt = os.path.join(w, "o.opt")
        n = dat_to_opt(dat, opt, DEFAULT_CONFIG, field_names=NAMES, volume="V")
        assert n == 15


def t_roundtrip_csv() -> None:
    rep = verify_roundtrip_csv(FIXTURE, DEFAULT_CONFIG, field_names=NAMES)
    assert rep.ok and rep.input_records == 15, rep.summary()


def t_roundtrip_dat() -> None:
    with tempfile.TemporaryDirectory() as w:
        dat = os.path.join(w, "o.dat")
        cli.main(["csv2dat", FIXTURE, dat, "--field-names", ",".join(NAMES)])
        rep = verify_roundtrip_dat(dat, DEFAULT_CONFIG, NAMES)
        assert rep.ok and rep.input_records == 15, rep.summary()


def t_sample() -> None:
    if not os.path.exists(SAMPLE):
        print("  (sample missing — skip)")
        return
    with tempfile.TemporaryDirectory() as w:
        dat = os.path.join(w, "V.dat")
        assert cli.main(["csv2dat", SAMPLE, dat, "--field-names", ",".join(NAMES)]) == 0
        recs = list(read_dat(dat, DEFAULT_CONFIG))
        assert recs[0] == NAMES and len(recs) == 343
        rep = validate_dat_file(dat, NAMES, DEFAULT_CONFIG)
        assert rep.ok and rep.records == 342
        assert cli.main(
            ["roundtrip", "--direction", "csv2dat", SAMPLE, "--field-names", ",".join(NAMES)]
        ) == 0


def t_case_folder_dat() -> None:
    if not os.path.exists(SAMPLE):
        print("  (skip)")
        return
    dat = os.path.join(os.path.dirname(SAMPLE), "VOL001.dat")
    assert os.path.exists(dat), "case VOL001.dat missing"
    recs = list(read_dat(dat, DEFAULT_CONFIG))
    assert recs[0] == NAMES, f"case DAT first row={recs[0]!r}"
    assert len(recs) == 343
    assert open(dat, "rb").read(1) == b"\xfe"
    rep = validate_dat_file(dat, NAMES, DEFAULT_CONFIG)
    assert rep.ok and rep.records == 342, rep.summary()


def t_legacy_no_header() -> None:
    with tempfile.TemporaryDirectory() as w:
        dat = os.path.join(w, "l.dat")
        write_dat(
            dat, iter([["X", "Y"], ["1", "2"]]), ["A", "B"],
            DEFAULT_CONFIG, emit_dct=False, emit_header=False,
        )
        out = os.path.join(w, "o.csv")
        n, names = dat_to_csv(dat, out, DEFAULT_CONFIG, field_names=None, infer_if_missing=True)
        assert n == 2 and names == ["FIELD1", "FIELD2"]


def t_heuristic() -> None:
    assert not _looks_like_header_row(
        ["DAVILLA_RESTRICTED_ACCESS_000001", "DAVILLA_RESTRICTED_ACCESS_000001", "", "file.docx"]
    )
    assert _looks_like_header_row(NAMES)


def t_empty() -> None:
    with tempfile.TemporaryDirectory() as w:
        c = os.path.join(w, "e.csv")
        open(c, "w", encoding="utf-8").write("A,B\n")
        dat = os.path.join(w, "o.dat")
        assert cli.main(["csv2dat", c, dat]) == 0
        assert cli.main(["roundtrip", "--direction", "csv2dat", c]) == 0


def t_overlong() -> None:
    with tempfile.TemporaryDirectory() as w:
        c = os.path.join(w, "l.csv")
        open(c, "w", encoding="utf-8", newline="").write("A,B\n1,2,3\n")
        assert cli.main(["csv2dat", c, os.path.join(w, "o.dat")]) == 2


def t_emoji() -> None:
    with tempfile.TemporaryDirectory() as w:
        c = os.path.join(w, "e.csv")
        open(c, "w", encoding="utf-8", newline="").write("F\nhello\U0001F600\n")
        dat = os.path.join(w, "o.dat")
        assert cli.main(["csv2dat", c, dat]) == 2
        assert cli.main(["csv2dat", c, dat, "--encoding-errors", "replace"]) == 0


def t_names_file() -> None:
    with tempfile.TemporaryDirectory() as w:
        c = os.path.join(w, "in.csv")
        open(c, "w", encoding="utf-8", newline="").write("A,B\n1,2\n")
        nf = os.path.join(w, "f.txt")
        open(nf, "w", encoding="utf-8").write("Foo, Bar\nBaz\n")
        dat = os.path.join(w, "o.dat")
        assert cli.main(["csv2dat", c, dat, "--field-names-file", nf]) == 0
        assert list(read_dat(dat, DEFAULT_CONFIG))[0] == ["Foo, Bar", "Baz"]


def t_crlf() -> None:
    with tempfile.TemporaryDirectory() as w:
        dat = os.path.join(w, "o.dat")
        cli.main(["csv2dat", FIXTURE, dat, "--field-names", ",".join(NAMES), "--crlf"])
        assert b"\r\n" in open(dat, "rb").read()
        recs = list(read_dat(dat, DatConfig(record_terminator="\r\n")))
        assert recs[0] == NAMES and len(recs) == 16


def t_runpy() -> None:
    r = subprocess.run([sys.executable, "run.py", "--help"], cwd=PKG, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[:200]


def t_mod_parent() -> None:
    parent = os.path.dirname(PKG)
    r = subprocess.run(
        [sys.executable, "-m", "csv_to_dat", "--help"],
        cwd=parent, capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr[:200]


def t_dct_mismatch() -> None:
    with tempfile.TemporaryDirectory() as w:
        c = os.path.join(w, "in.csv")
        open(c, "w", encoding="utf-8", newline="").write("A,B\n1,2\n")
        dat = os.path.join(w, "o.dat")
        csv_to_dat(c, dat, DEFAULT_CONFIG, emit_dct=True)
        write_dct(os.path.splitext(dat)[0] + ".dct", ["ONLY_ONE"], "cp1252")
        assert cli.main(["dat2csv", dat, os.path.join(w, "o.csv")]) == 2


def t_bom() -> None:
    with tempfile.TemporaryDirectory() as w:
        c = os.path.join(w, "b.csv")
        open(c, "wb").write(b"\xef\xbb\xbfA,B\r\n1,2\r\n")
        dat = os.path.join(w, "o.dat")
        assert cli.main(["csv2dat", c, dat]) == 0
        assert list(read_dat(dat, DEFAULT_CONFIG))[0] == ["A", "B"]


def t_mv() -> None:
    with tempfile.TemporaryDirectory() as w:
        c = os.path.join(w, "m.csv")
        rs = chr(0x1E)
        open(c, "w", encoding="utf-8", newline="").write(f"TAGS\ntag{rs}tag2\n")
        dat = os.path.join(w, "o.dat")
        cli.main(["csv2dat", c, dat])
        rec = next(read_dat(dat, DEFAULT_CONFIG, skip_header=True, header_names=["TAGS"]))
        assert rec == ["tag;tag2"]


def t_winpath() -> None:
    with tempfile.TemporaryDirectory() as w:
        c = os.path.join(w, "p.csv")
        open(c, "w", encoding="utf-8", newline="").write("F\nC:\\Natives\\doc.pdf\n")
        dat = os.path.join(w, "o.dat")
        cli.main(["csv2dat", c, dat])
        rec = next(read_dat(dat, DEFAULT_CONFIG, skip_header=True, header_names=["F"]))
        assert rec == ["C:\\Natives\\doc.pdf"]


def t_blank() -> None:
    with tempfile.TemporaryDirectory() as w:
        c = os.path.join(w, "b.csv")
        open(c, "w", encoding="utf-8", newline="").write("A,B\n1,2\n\n3,4\n")
        dat = os.path.join(w, "o.dat")
        n = csv_to_dat(c, dat, DEFAULT_CONFIG, emit_dct=False)
        assert n == 2
        recs = list(read_dat(dat, DEFAULT_CONFIG, skip_header=True, header_names=["A", "B"]))
        assert recs == [["1", "2"], ["3", "4"]]


def t_dup() -> None:
    with tempfile.TemporaryDirectory() as w:
        c = os.path.join(w, "d.csv")
        open(c, "w", encoding="utf-8", newline="").write(
            "BEGDOC,ENDDOC,FILEPATH\nA,A,f.pdf\nA,A,g.pdf\n"
        )
        rep = validate_csv_file(c)
        assert not rep.ok and any("duplicate" in e for e in rep.errors)


def t_opt_content() -> None:
    with tempfile.TemporaryDirectory() as w:
        dat = os.path.join(w, "o.dat")
        cli.main(["csv2dat", FIXTURE, dat, "--field-names", ",".join(NAMES)])
        opt = os.path.join(w, "o.opt")
        cli.main(["opt", dat, opt, "--volume", "VOL001", "--image-dir", "IMAGES\\001"])
        first = open(opt, encoding="cp1252").readline().rstrip("\n")
        assert first.startswith("DAVILLA_RESTRICTED_ACCESS_000001,VOL001,IMAGES\\001\\")
        assert ",Y,,,1" in first


def t_help() -> None:
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            cli.main(["--help"])
    except SystemExit as e:
        assert e.code == 0
    text = buf.getvalue()
    assert "Concordance" in text or "DAT" in text
    assert "header" in text.lower()


print("=== gstack-qa PASS 2 (csv_to_dat) ===\n")
for name, fn in [
    ("unit_suite", t_unit),
    ("header_written", t_header_written),
    ("dat2csv_skips_header", t_dat2csv_skips_header),
    ("infer_from_header", t_infer_from_header),
    ("no_header_flag", t_no_header),
    ("validate_skips_header", t_validate_skips),
    ("opt_skips_header", t_opt_skips),
    ("roundtrip_csv", t_roundtrip_csv),
    ("roundtrip_dat", t_roundtrip_dat),
    ("full_sample", t_sample),
    ("case_folder_dat", t_case_folder_dat),
    ("legacy_no_header", t_legacy_no_header),
    ("heuristic", t_heuristic),
    ("empty_csv", t_empty),
    ("overlong_rejected", t_overlong),
    ("emoji_strict", t_emoji),
    ("names_file_comma", t_names_file),
    ("crlf", t_crlf),
    ("runpy", t_runpy),
    ("mod_parent", t_mod_parent),
    ("dct_mismatch", t_dct_mismatch),
    ("bom", t_bom),
    ("multivalue", t_mv),
    ("winpath", t_winpath),
    ("blank_skipped", t_blank),
    ("dup_begdoc", t_dup),
    ("opt_content", t_opt_content),
    ("help_mentions_header", t_help),
]:
    case(name, fn)

passed = sum(1 for _, ok, _ in results if ok)
print(f"\n=== SUMMARY: {passed}/{len(results)} passed ===")
print(f"=== FINDINGS: {len(findings)} ===")
for sev, title, detail in findings:
    print(f"[{sev}] {title}")

out = os.path.join(os.path.dirname(__file__), "qa-pass2-results.txt")
with open(out, "w", encoding="utf-8") as fh:
    fh.write(f"passed={passed} total={len(results)} findings={len(findings)}\n")
    for n, ok, d in results:
        fh.write(f"{'PASS' if ok else 'FAIL'}\t{n}\t{d}\n")
print(f"Wrote {out}")
sys.exit(0 if not findings else 1)
