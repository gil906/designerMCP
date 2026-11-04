#!/usr/bin/env python3
"""Validate the whole combined offline library using fixed default minima."""

from build_offline_library import main

if __name__ == "__main__":
    import sys
    raise SystemExit(main(["--check", *sys.argv[1:]]))
