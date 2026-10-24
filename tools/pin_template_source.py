"""Write the lowercase sha256 of a match video into the template allowlist.

Refuses a match id that is not in ``templates/template_sources.json``. Does not
harvest and does not copy the video.

    python tools/pin_template_source.py --match-id 102332 --video PATH\\video.mp4
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


HELD_IDS = ("170123", "021003", "125430", "084523", "193534")


def _held_in_path(video: Path) -> str | None:
    text = str(video)
    for item in HELD_IDS:
        if item in text:
            return item
    return None


def _realpath(path: Path) -> Path:
    """Follow symlinks. A link whose target names a held-out id is still that video."""
    return Path(os.path.realpath(path))


def _blocklist(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"refusing: held-out sha256 table unreadable ({exc}). allowlist not written.") from exc
    rows = data.get("sha256") if isinstance(data, dict) else None
    if not isinstance(rows, dict):
        raise SystemExit("refusing: held-out sha256 table has no sha256 object. allowlist not written.")
    return {str(key): str(value) for key, value in rows.items()}


def pin(match_id: str, video: Path, sources_path: Path, hash_path: Path | None = None) -> str:
    if not video.is_file():
        raise SystemExit(f"refusing: {video} is not a file. allowlist not written.")
    root = Path(__file__).resolve().parents[1]
    hash_path = hash_path or (root / "sidecar" / "packs" / "held_out_sha256.json")
    try:
        data = json.loads(sources_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"refusing: allowlist unreadable ({exc}). allowlist not written.") from exc
    rows = data.get("sources") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise SystemExit("refusing: allowlist has no sources list. allowlist not written.")
    match_id = str(match_id)
    row = next((item for item in rows if isinstance(item, dict) and str(item.get("match_id")) == match_id), None)
    if row is None:
        raise SystemExit(
            f"refusing: match {match_id} is not allowlisted in {sources_path.name}. allowlist not written."
        )
    folder = str(row.get("folder") or "")
    if not folder.endswith(match_id):
        raise SystemExit(
            f"refusing: allowlist folder {folder!r} is not the folder for {match_id}. allowlist not written."
        )
    real = _realpath(video)
    held = _held_in_path(video) or _held_in_path(real)
    if held:
        raise SystemExit(
            f"refusing: {video} contains held-out id {held}. allowlist not written."
        )
    if video.parent.name != folder or real.parent.name != folder:
        raise SystemExit(
            f"refusing: {real.parent.name} is not the allowlisted folder {folder} for {match_id}. "
            "allowlist not written."
        )
    digest = sha256_file(video)
    blocked = _blocklist(hash_path)
    if digest in blocked:
        raise SystemExit(
            f"refusing: video sha256 matches held-out {blocked[digest]}. allowlist not written."
        )
    row["video_sha256"] = digest
    # LF even on Windows. write_text would translate newlines to CRLF.
    sources_path.write_bytes((json.dumps(data, indent=2) + "\n").encode("utf-8"))
    return digest


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parents[1]
    ap = argparse.ArgumentParser(description="Pin a template-source video sha256")
    ap.add_argument("--match-id", required=True)
    ap.add_argument("--video", type=Path, required=True)
    ap.add_argument("--sources", type=Path, default=root / "templates" / "template_sources.json")
    ap.add_argument("--heldout-sha256", type=Path, default=root / "sidecar" / "packs" / "held_out_sha256.json")
    args = ap.parse_args(argv)
    try:
        digest = pin(args.match_id, args.video, args.sources, args.heldout_sha256)
    except SystemExit as exc:
        if exc.code not in (None, 0):
            print(exc.code if isinstance(exc.code, str) else exc, file=sys.stderr)
            return 2
        raise
    print(f"pinned {args.match_id} video_sha256={digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
