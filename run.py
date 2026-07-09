"""
run.py - In-folder entry point for the csv_to_dat converter.

WHAT THIS FILE DOES
-------------------
Lets you run the CLI when the Cursor/workspace root IS this package folder
(where `python -m csv_to_dat` fails because Python looks for a nested
csv_to_dat package). Prefer this (or `python cli.py`) from inside the folder;
from the parent directory, `python -m csv_to_dat` still works.

EXAMPLES (PowerShell)
---------------------
  python .\\run.py csv2dat .\\tests\\fixtures\\VOL001_slice.csv .\\out.dat
  python .\\cli.py --help
"""

from __future__ import annotations

import sys

from cli import main

if __name__ == "__main__":
    sys.exit(main())
