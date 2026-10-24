"""Retired. 170123 is held out of training.

The clock means and the FIELD GOAL / TOUCHDOWN fragments this script used to write were cut from
170123 highlight frames (the clean f+12k grid, plus hl00 f175 and hl01 f174). Those packs are
not in the reader. Use ``tools/harvest_standard_clock_banner.py``, which refuses this match.
"""
from __future__ import annotations

import sys


def main() -> int:
    print(
        "refusing: 170123 is held out. no clock or banner templates written. "
        "see tools/harvest_standard_clock_banner.py and sidecar/packs/template_manifest.json",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
