"""Retired. 084523 is held out. This script writes nothing.

Glyph templates are cut only by ``tools/harvest_templates.py``, which refuses
this match.
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
