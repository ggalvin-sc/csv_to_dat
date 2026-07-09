"""
__main__.py - Package entry point so `python -m csv_to_dat ...` works.

Delegates to cli.main().
"""

import sys

try:  # package context
    from .cli import main
except ImportError:  # direct script context
    from cli import main

if __name__ == "__main__":
    sys.exit(main())
