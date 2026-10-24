"""Write the lowercase sha256 of a match video into the template allowlist.

Refuses a match id that is not in ``templates/template_sources.json``. Does not
harvest and does not copy the video.

    python tools/pin_template_source.py --match-id 102332 --video PATH\\video.mp4
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def pin(match_id: str, video: Path, sources_path: Path) -> str:
    if not video.is_file():
        raise SystemExit(f"refusing: {video} is not a file. allowlist not written.")
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
    digest = sha256_file(video)
    row["video_sha256"] = digest
    sources_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return digest


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parents[1]
    ap = argparse.ArgumentParser(description="Pin a template-source video sha256")
    ap.add_argument("--match-id", required=True)
    ap.add_argument("--video", type=Path, required=True)
    ap.add_argument("--sources", type=Path, default=root / "templates" / "template_sources.json")
    args = ap.parse_args(argv)
    try:
        digest = pin(args.match_id, args.video, args.sources)
    except SystemExit as exc:
        if exc.code not in (None, 0):
            print(exc.code if isinstance(exc.code, str) else exc, file=sys.stderr)
            return 2
        raise
    print(f"pinned {args.match_id} video_sha256={digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
