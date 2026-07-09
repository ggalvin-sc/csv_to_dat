"""
cli.py - Command-line interface for the Concordance DAT <-> CSV converter.

MODULE PURPOSE
--------------
Exposes three subcommands over argparse:

  csv2dat    Convert a CSV file to a Concordance DAT load file (+ optional .dct).
  dat2csv    Convert a Concordance DAT file back to a CSV file.
  roundtrip  Verify round-trip fidelity in either direction and print a report.

All control characters (delimiter/quote/newline/multivalue) and the text
encoding are configurable so the same tool handles classic Concordance
(ASCII 020/254/174, cp1252) and modern variants (comma/pipe/double-quote, utf-8).

EXAMPLES (PowerShell)
---------------------
  # CSV -> DAT with classic Concordance delimiters (the defaults: cp1252)
  python -m csv_to_dat csv2dat .\\VOL001.csv .\\VOL001.dat

  # DAT -> CSV using the companion .dct for headers
  python -m csv_to_dat dat2csv .\\VOL001.dat .\\VOL001_roundtrip.csv

  # Verify CSV -> DAT -> CSV
  python -m csv_to_dat roundtrip --direction csv2dat .\\VOL001.csv

  # Override field names (e.g. map to standard Concordance names)
  python -m csv_to_dat csv2dat .\\VOL001.csv .\\VOL001.dat `
    --field-names BEGDOC,ENDDOC,CODED,FILEPATH

  # Field names from a file (one per line; use when a name contains a comma)
  python -m csv_to_dat csv2dat .\\VOL001.csv .\\VOL001.dat `
    --field-names-file .\\fields.txt
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional, Sequence

try:  # package context (python -m csv_to_dat from the parent dir)
    from .converter import (
        DEFAULT_CONFIG,
        DatConfig,
        DatParseError,
        dat_to_csv,
        dat_to_opt,
        csv_to_dat,
        verify_roundtrip_csv,
        verify_roundtrip_dat,
        read_dct,
        validate_csv_file,
        validate_dat_file,
    )
except ImportError:  # direct script context (python cli.py from inside the dir)
    from converter import (
        DEFAULT_CONFIG,
        DatConfig,
        DatParseError,
        dat_to_csv,
        dat_to_opt,
        csv_to_dat,
        verify_roundtrip_csv,
        verify_roundtrip_dat,
        read_dct,
        validate_csv_file,
        validate_dat_file,
    )


# ---------------------------------------------------------------------------
# Argument helpers
# ---------------------------------------------------------------------------

def _parse_char_arg(value: str, name: str) -> str:
    """Accept either a single character or a decimal/hex integer code.

    Examples accepted: "20", "0x14", "þ", ",".
    """
    if value is None or value == "":
        raise argparse.ArgumentTypeError(f"{name} must not be empty")
    s = value.strip()
    # Integer codes (decimal or 0x.. hex)
    if s.lower().startswith("0x"):
        try:
            code = int(s, 16)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{name}: bad hex {value!r}")
        if not 0 <= code <= 0x10FFFF:
            raise argparse.ArgumentTypeError(f"{name}: code out of range {value!r}")
        return chr(code)
    if s.isdigit():
        code = int(s)
        if not 0 <= code <= 0x10FFFF:
            raise argparse.ArgumentTypeError(f"{name}: code out of range {value!r}")
        return chr(code)
    if len(s) != 1:
        raise argparse.ArgumentTypeError(
            f"{name}: pass one character or an int code (got {value!r})"
        )
    return s


def _build_config(args: argparse.Namespace) -> DatConfig:
    return DatConfig(
        field_delimiter=_parse_char_arg(args.delim, "--delim"),
        quote_char=_parse_char_arg(args.quote, "--quote"),
        newline_char=_parse_char_arg(args.newline, "--newline"),
        multi_value_sep=_parse_char_arg(args.multival, "--multival"),
        encoding=args.encoding,
        encoding_errors=args.encoding_errors,
        record_terminator="\r\n" if args.crlf else "\n",
        field_count_mode=args.field_count_mode,
        normalize_multivalue=not args.no_normalize_multivalue,
    )


def _split_fields(value: Optional[str]) -> Optional[List[str]]:
    """Split a comma-separated field-name list.

    Prefer --field-names-file when a field name itself contains a comma.
    """
    if value is None:
        return None
    return [p.strip() for p in value.split(",")]


def _read_field_names_file(path: str, encoding: str = "utf-8") -> List[str]:
    """Read one field name per line (blank/# comment lines ignored)."""
    names: List[str] = []
    with open(path, "r", encoding=encoding, newline="") as fh:
        for line in fh:
            text = line.rstrip("\r\n")
            stripped = text.strip()
            if not stripped or stripped.startswith("#"):
                continue
            names.append(text)
    if not names:
        raise ValueError(f"--field-names-file {path!r} contained no field names")
    return names


def _resolve_field_names(args: argparse.Namespace) -> Optional[List[str]]:
    """Resolve --field-names / --field-names-file (file wins if both set)."""
    file_path = getattr(args, "field_names_file", None)
    inline = getattr(args, "field_names", None)
    if file_path:
        if inline:
            print(
                "WARNING: both --field-names and --field-names-file given; "
                "using --field-names-file",
                file=sys.stderr,
            )
        return _read_field_names_file(file_path)
    return _split_fields(inline)


# ---------------------------------------------------------------------------
# Subcommand implementations
# ---------------------------------------------------------------------------

def cmd_csv2dat(args: argparse.Namespace) -> int:
    config = _build_config(args)
    names = _resolve_field_names(args)
    try:
        merge = bool(getattr(args, "merge_schema", False))
        if merge and not names:
            print(
                "ERROR: --merge-schema requires --field-names or --field-names-file "
                "(the full target schema, including blank fields)",
                file=sys.stderr,
            )
            return 2
        n = csv_to_dat(
            args.input,
            args.output,
            config=config,
            field_names=names,
            emit_dct=not args.no_dct,
            emit_header=not args.no_header,
            csv_encoding=args.csv_encoding,
            csv_delimiter=_parse_char_arg(args.csv_delim, "--csv-delim"),
            merge_schema=merge,
            strict_merge=bool(getattr(args, "strict_merge", False)),
        )
    except (DatParseError, ValueError, OSError, UnicodeEncodeError, UnicodeDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"csv2dat: wrote {n} records to {args.output}")
    print(f"  config: {config.describe()}")
    if names:
        # Use ' | ' so names that contain commas are not ambiguous in the log.
        print(f"  fields ({len(names)}): {' | '.join(names)}")
    if getattr(args, "merge_schema", False):
        mode = "strict" if getattr(args, "strict_merge", False) else "warn"
        print(
            f"  schema merge ({mode}): CSV columns mapped by name/alias; "
            "missing fields blank; collisions/dropped columns reported"
        )
    if not args.no_header:
        print("  Concordance header row written as first line of the .dat")
    else:
        print("  (no DAT header row — --no-header)")
    if not args.no_dct:
        print(
            "  .dct header sidecar written beside the .dat "
            "(NOT a Concordance CPL dictionary)"
        )
    return 0


def cmd_dat2csv(args: argparse.Namespace) -> int:
    config = _build_config(args)
    names = _resolve_field_names(args)
    try:
        n, used = dat_to_csv(
            args.input,
            args.output,
            config=config,
            field_names=names,
            dct_path=args.dct,
            infer_if_missing=True,
        )
    except (DatParseError, ValueError, OSError, UnicodeEncodeError, UnicodeDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"dat2csv: wrote {n} records to {args.output}")
    print(f"  config: {config.describe()}")
    print(f"  fields ({len(used)}): {' | '.join(used)}")
    return 0


def cmd_roundtrip(args: argparse.Namespace) -> int:
    config = _build_config(args)
    names = _resolve_field_names(args)
    if args.direction == "csv2dat":
        report = verify_roundtrip_csv(
            args.input,
            config=config,
            field_names=names,
            csv_encoding=args.csv_encoding or "utf-8",
        )
    else:
        if not names:
            # Try the companion .dct so DAT->CSV->DAT has proper headers.
            cand = os.path.splitext(args.input)[0] + ".dct"
            if os.path.exists(cand):
                names = read_dct(cand, config.encoding)
        if not names:
            print(
                "ERROR: roundtrip dat2csv requires --field-names, "
                "--field-names-file, or a .dct",
                file=sys.stderr,
            )
            return 2
        report = verify_roundtrip_dat(args.input, config, names)
    print(report.summary())
    return 0 if report.ok else 1


def cmd_validate(args: argparse.Namespace) -> int:
    """Validate Bates / path semantics on a CSV or DAT load file."""
    names = _resolve_field_names(args)
    path = args.input
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".dat":
            config = _build_config(args)
            if not names:
                cand = args.dct or (os.path.splitext(path)[0] + ".dct")
                if os.path.exists(cand):
                    names = read_dct(cand, config.encoding)
            if not names:
                print(
                    "ERROR: validate on a .dat requires --field-names, "
                    "--field-names-file, or a .dct",
                    file=sys.stderr,
                )
                return 2
            report = validate_dat_file(
                path,
                names,
                config,
                require_begdoc=not args.allow_missing_begdoc,
                check_filepath_exists=args.check_natives,
                natives_root=args.natives_root,
            )
        else:
            report = validate_csv_file(
                path,
                field_names=names,
                csv_encoding=args.csv_encoding or "utf-8",
                require_begdoc=not args.allow_missing_begdoc,
                check_filepath_exists=args.check_natives,
                natives_root=args.natives_root,
            )
    except (DatParseError, ValueError, OSError, UnicodeDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(report.summary())
    return 0 if report.ok else 1


def cmd_opt(args: argparse.Namespace) -> int:
    """Write an Opticon .opt companion from a DAT (or CSV via intermediate)."""
    config = _build_config(args)
    names = _resolve_field_names(args)
    try:
        n = dat_to_opt(
            args.input,
            args.output,
            config=config,
            field_names=names,
            dct_path=args.dct,
            volume=args.volume or "",
            image_ext=args.image_ext,
            pages_per_doc=args.pages_per_doc,
            image_dir=args.image_dir or "",
        )
    except (DatParseError, ValueError, OSError, UnicodeEncodeError, UnicodeDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"opt: wrote {n} OPT lines to {args.output}")
    print(f"  config: {config.describe()}")
    print(f"  volume={args.volume or '(none)'} ext={args.image_ext} "
          f"pages/doc={args.pages_per_doc} image_dir={args.image_dir or '(relative)'}")
    return 0


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------

def _add_common_delim_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--delim",
        default="0x14",
        help="field delimiter (char or code; default 0x14 / ASCII 20)",
    )
    p.add_argument(
        "--quote",
        default="0xFE",
        help="quote/qualifier char (default 0xFE / ASCII 254)",
    )
    p.add_argument(
        "--newline",
        default="0xAE",
        help="in-field newline char (default 0xAE / ASCII 174)",
    )
    p.add_argument(
        "--multival",
        default="0x3B",
        help="multi-value separator (default 0x3B / ';'; Relativity default). "
             "On write, legacy 0x1E separators inside values are rewritten to this "
             "char unless --no-normalize-multivalue is set.",
    )
    p.add_argument(
        "--encoding",
        default="cp1252",
        help="DAT text encoding (default cp1252 for classic Concordance; "
             "use utf-8 only when the receiving platform expects it)",
    )
    p.add_argument(
        "--encoding-errors",
        default="strict",
        choices=["strict", "replace", "ignore", "xmlcharrefreplace"],
        help="how to handle characters that cannot encode into --encoding "
             "(default strict = fail; replace/ignore accept data loss)",
    )
    p.add_argument(
        "--field-count-mode",
        default="pad_reject",
        choices=["pad_reject", "pad_truncate", "reject"],
        help="how to handle rows whose field count differs from the schema "
             "(default pad_reject: pad short rows, reject overlong)",
    )
    p.add_argument(
        "--no-normalize-multivalue",
        action="store_true",
        help="do not rewrite legacy 0x1E multi-value separators to --multival",
    )
    p.add_argument("--crlf", action="store_true", help="use CRLF as the record terminator")


def _add_field_name_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--field-names",
        default=None,
        help="comma-separated field names (avoid if a name itself contains a comma; "
             "use --field-names-file instead)",
    )
    p.add_argument(
        "--field-names-file",
        default=None,
        help="path to a text file with one field name per line "
             "(# comments and blank lines ignored)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="csv_to_dat",
        description=(
            "Convert between CSV and Concordance DAT eDiscovery load files. "
            "By default the first DAT line is a Concordance/Relativity header "
            "row (field names). Also validates Bates/path semantics and can "
            "emit a minimal Opticon .opt companion."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_csv2dat = sub.add_parser("csv2dat", help="convert CSV -> DAT")
    p_csv2dat.add_argument("input", help="input CSV path")
    p_csv2dat.add_argument("output", help="output DAT path")
    _add_field_name_args(p_csv2dat)
    p_csv2dat.add_argument(
        "--no-dct",
        action="store_true",
        help="do not write a companion header-sidecar .dct",
    )
    p_csv2dat.add_argument(
        "--no-header",
        action="store_true",
        help="do not write Concordance field-name header as the first DAT line "
             "(default is to write it — Relativity/Concordance/Reveal recommended)",
    )
    p_csv2dat.add_argument(
        "--csv-encoding",
        default=None,
        help="encoding of the input CSV (default utf-8, independent of DAT --encoding)",
    )
    p_csv2dat.add_argument("--csv-delim", default=",", help="CSV delimiter (default ',')")
    p_csv2dat.add_argument(
        "--merge-schema",
        action="store_true",
        help="project CSV columns onto --field-names by name/alias; emit every "
             "target field even when blank or missing from the CSV "
             "(required when --field-names is longer than the CSV header)",
    )
    p_csv2dat.add_argument(
        "--strict-merge",
        action="store_true",
        help="with --merge-schema: fail if any CSV column is dropped or two "
             "CSV columns collide on the same target field (default: warn)",
    )
    _add_common_delim_args(p_csv2dat)
    p_csv2dat.set_defaults(func=cmd_csv2dat)

    p_dat2csv = sub.add_parser("dat2csv", help="convert DAT -> CSV")
    p_dat2csv.add_argument("input", help="input DAT path")
    p_dat2csv.add_argument("output", help="output CSV path")
    _add_field_name_args(p_dat2csv)
    p_dat2csv.add_argument(
        "--dct",
        default=None,
        help="path to a .dct field-name list (default: beside the DAT)",
    )
    _add_common_delim_args(p_dat2csv)
    p_dat2csv.set_defaults(func=cmd_dat2csv)

    p_rt = sub.add_parser("roundtrip", help="verify CSV->DAT->CSV or DAT->CSV->DAT")
    p_rt.add_argument(
        "input",
        help="input file (CSV for csv2dat direction, DAT for dat2csv)",
    )
    p_rt.add_argument(
        "--direction",
        choices=["csv2dat", "dat2csv"],
        default="csv2dat",
        help="round-trip direction (default csv2dat)",
    )
    _add_field_name_args(p_rt)
    p_rt.add_argument(
        "--csv-encoding",
        default=None,
        help="encoding of the source CSV for the csv2dat direction "
             "(default utf-8, independent of DAT --encoding)",
    )
    _add_common_delim_args(p_rt)
    p_rt.set_defaults(func=cmd_roundtrip)

    p_val = sub.add_parser(
        "validate",
        help="validate Bates uniqueness / ENDDOC order / native paths",
    )
    p_val.add_argument("input", help="input CSV or DAT path")
    _add_field_name_args(p_val)
    p_val.add_argument(
        "--dct",
        default=None,
        help="path to a .dct (DAT only; default: beside the DAT)",
    )
    p_val.add_argument(
        "--csv-encoding",
        default="utf-8",
        help="CSV encoding when input is a CSV (default utf-8)",
    )
    p_val.add_argument(
        "--allow-missing-begdoc",
        action="store_true",
        help="do not require a BEGDOC-like column",
    )
    p_val.add_argument(
        "--check-natives",
        action="store_true",
        help="require FILEPATH/NATIVE files to exist on disk",
    )
    p_val.add_argument(
        "--natives-root",
        default=None,
        help="directory to resolve relative native paths against",
    )
    _add_common_delim_args(p_val)
    p_val.set_defaults(func=cmd_validate)

    p_opt = sub.add_parser(
        "opt",
        help="write a minimal Opticon .opt companion from a DAT",
    )
    p_opt.add_argument("input", help="input DAT path")
    p_opt.add_argument("output", help="output .opt path")
    _add_field_name_args(p_opt)
    p_opt.add_argument(
        "--dct",
        default=None,
        help="path to a .dct field-name list (default: beside the DAT)",
    )
    p_opt.add_argument("--volume", default="", help="OPT volume identifier (field 2)")
    p_opt.add_argument(
        "--image-ext",
        default=".tif",
        help="image extension appended to Bates IDs (default .tif)",
    )
    p_opt.add_argument(
        "--pages-per-doc",
        type=int,
        default=1,
        help="placeholder pages per document (default 1 = one OPT line per doc)",
    )
    p_opt.add_argument(
        "--image-dir",
        default="",
        help="directory prefix for image paths in the OPT (e.g. IMAGES\\\\001)",
    )
    _add_common_delim_args(p_opt)
    p_opt.set_defaults(func=cmd_opt)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
