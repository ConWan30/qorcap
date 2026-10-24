"""Retired writer. Glyphs are cut only by tools/harvest_templates.py.

The held-out helpers below stay so the grammar test can still name a held-out
path and a held-out sha256. ``main`` writes nothing and returns 2.

Held out of training (the helpers still recognise these):

* 170123, 021003, 125430
* Con's labelled games in ``sidecar/eval/human_labels.jsonl``: 084523 and 193534

``held_out_sha256.json`` is the public clips and event frames of 170123, 021003, and 125430.
084523 and 193534 have no source video in either repo, so those matches are recognised by path.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

# Public clips and event frames of 170123, 021003, and 125430 at this qortime commit.
# 084523 and 193534 have no source video in either repo, so those stay path checks.
_HASH_PATH = Path(__file__).resolve().parents[1] / "sidecar" / "packs" / "held_out_sha256.json"
_SHA256: dict[str, str] | None = None

# Match ids. A path, file name, or manifest row that contains one of these is not training data.
# Match ids, plus the public highlight clip ids. A bare hl00/f175.png path does not contain
# "170123", and that frame is still held out.
HELD_OUT = (
    "170123", "021003", "125430", "084523", "193534",
    "hl00-q3-a3-h0", "hl01-q4-a9-h0", "hl02-q4-a10-h0",
    "hl00-q1-a0-h6", "hl01-q1-a0-h7", "hl02-q2-a6-h7",
    "hl03-q2-a7-h7", "hl04-q2-a7-h13", "hl05-q2-a7-h14",
)
CLOCK_DIGITS = tuple("0123456789")


def held_out_reason(text: str) -> str | None:
    """The held-out id inside ``text``, or None."""
    folded = str(text).replace("\\", "/")
    for item in HELD_OUT:
        if item in folded:
            return item
    return None


def held_out_table() -> tuple[str, dict[str, str]]:
    """``(qortime commit, sha256 -> source path)`` for the public held-out videos and frames."""
    global _SHA256
    data = json.loads(_HASH_PATH.read_text(encoding="utf-8"))
    if _SHA256 is None:
        _SHA256 = dict(data["sha256"])
    return str(data["qortime_commit"]), _SHA256


def clip_sha256(match: str, name: str) -> str:
    """sha256 of ``site/matches/<match>/clips/<name>.mp4`` in the pinned table."""
    _commit, table = held_out_table()
    suffix = f"site/matches/{match}/clips/{name}.mp4"
    hits = [digest for digest, path in table.items() if path == suffix]
    if len(hits) != 1:
        raise KeyError(suffix)
    return hits[0]


def held_out_content(path: Path) -> str | None:
    """Source path when ``path`` has the same bytes as a held-out video or frame."""
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    _commit, table = held_out_table()
    return table.get(digest)


def frame_index(path: Path) -> int | None:
    """``f175.png`` or ``frame_175.png`` -> 175. None when the name has no index."""
    stem = path.stem
    digits = ""
    if stem.startswith("f") and stem[1:].isdigit():
        digits = stem[1:]
    else:
        for token in stem.replace("-", "_").split("_"):
            if token.isdigit():
                digits = token
    return int(digits) if digits else None


def main(argv: list[str] | None = None) -> int:
    """Retired. Writes nothing. Glyphs are cut only by tools/harvest_templates.py."""
    del argv
    print(
        "refusing: this harvester is retired. use tools/harvest_templates.py. no templates written.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
