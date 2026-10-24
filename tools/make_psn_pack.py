"""Retired. PSN glyphs are not cut by this script.

``tools/harvest_templates.py`` is the only harvester, and it has no PSN region
yet. This script writes nothing.
"""
from __future__ import annotations

import sys


def main(argv=None) -> int:
    del argv
    print(
        "refusing: this harvester is retired. use tools/harvest_templates.py. no templates written.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
