"""
csv_to_dat - a Concordance DAT <-> CSV converter for legal eDiscovery.

Public modules:
  converter.py - core read/write library (no third-party deps).
  cli.py       - argparse CLI: csv2dat / dat2csv / roundtrip / validate / opt.
"""

try:  # package context
    from .converter import (  # noqa: F401  (re-exported for convenience)
        DatConfig,
        DEFAULT_CONFIG,
        DatParseError,
        VerifyReport,
        ValidationReport,
        read_csv,
        write_dat,
        csv_to_dat,
        read_dat,
        write_csv,
        dat_to_csv,
        verify_roundtrip_csv,
        verify_roundtrip_dat,
        read_dct,
        write_dct,
        validate_load_records,
        validate_dat_file,
        validate_csv_file,
        write_opt,
        dat_to_opt,
    )
except ImportError:  # direct script context (package dir on sys.path)
    from converter import (  # noqa: F401
        DatConfig,
        DEFAULT_CONFIG,
        DatParseError,
        VerifyReport,
        ValidationReport,
        read_csv,
        write_dat,
        csv_to_dat,
        read_dat,
        write_csv,
        dat_to_csv,
        verify_roundtrip_csv,
        verify_roundtrip_dat,
        read_dct,
        write_dct,
        validate_load_records,
        validate_dat_file,
        validate_csv_file,
        write_opt,
        dat_to_opt,
    )

__all__ = [
    "DatConfig",
    "DEFAULT_CONFIG",
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
    "validate_dat_file",
    "validate_csv_file",
    "write_opt",
    "dat_to_opt",
]
