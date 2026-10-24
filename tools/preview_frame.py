"""Print fps and frame count, and write one PNG in the harvester's decode order.

``--frame`` is the sequential index ``harvest_templates.py`` cuts. ``--t-ms``
walks that same order and keeps the first frame whose container timestamp is
at least T. The script does not seek.

    python tools/preview_frame.py --video PATH\\video.mp4 --frame 1200 --out preview.png
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harvest_templates as H  # noqa: E402


def preview(video: Path, out: Path, *, frame: int | None, t_ms: int | None) -> tuple[int, int, float, int]:
    if not video.is_file():
        raise H.Refuse(f"refusing: {video} is not a file. no preview written.")
    if (frame is None) == (t_ms is None):
        raise H.Refuse("refusing: pass exactly one of --frame or --t-ms. no preview written.")
    if frame is not None and frame < 0:
        raise H.Refuse("refusing: --frame must be >= 0. no preview written.")
    if t_ms is not None and t_ms < 0:
        raise H.Refuse("refusing: --t-ms must be >= 0. no preview written.")
    chosen = None
    fps = 0.0
    count = 0
    for index, image, measured, got_fps, got_count in H.iter_decoded(video):
        fps, count = got_fps, got_count
        if frame is not None and index == frame:
            chosen = (index, image, measured)
            break
        if t_ms is not None and measured >= t_ms:
            chosen = (index, image, measured)
            break
    if chosen is None:
        which = f"frame {frame}" if frame is not None else f"t_ms {t_ms}"
        raise H.Refuse(f"refusing: video has no {which}. no preview written.")
    import cv2

    out.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(out), chosen[1]):
        raise H.Refuse("refusing: png encode failed. no preview written.")
    return chosen[0], chosen[2], fps, count


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Preview one frame in harvest decode order")
    ap.add_argument("--video", type=Path, required=True)
    ap.add_argument("--frame", type=int, default=None)
    ap.add_argument("--t-ms", type=int, default=None)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    try:
        index, measured, fps, count = preview(args.video, args.out, frame=args.frame, t_ms=args.t_ms)
    except H.Refuse as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"fps={fps:.3f} frame_count={count} frame_index={index} t_ms={measured} wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
