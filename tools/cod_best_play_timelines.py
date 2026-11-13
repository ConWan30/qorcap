"""Build the committed CoD end-of-match replay timelines (no images) from local practice tails.

Box-only helper. For each sampled reader time it takes the nearest 60 fps video frame and stores
what the shipped readers return on it: the end word (read_end), Qortrola30's row (read_owner_row:
read or not, and whether the board row was found at all), and the BEST PLAY title score
(read_best_play). The output has no pixels and no gamertags.

  python tools/cod_best_play_timelines.py SPEC.json OUT.json

SPEC is a list of {"match", "clip", "clip_start_pts_s", "t_minus_pts_ms", "signals" (optional
cod_signals.jsonl, gives the reader's real sample times), "reader_ms" (cadence used past the last
signals row, or for the whole clip when there is no signals file), "confirm" (optional, from
cod_end_stop.json), "split" ("tuning" or "held_out")}.
"""
import json
import sys
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sidecar"))
from qorcap_cod_bestplay import read_best_play  # noqa: E402
from qorcap_cod_endstop import end_word  # noqa: E402
from qorcap_cod_hud import read_end, read_owner_row  # noqa: E402
from qorcap_cod import same_owner_tag  # noqa: E402


def _frames(clip):
    cap = cv2.VideoCapture(str(clip))
    out = []
    while True:
        ok, img = cap.read()
        if not ok:
            break
        out.append(img)
    return out


def build(spec):
    frames = _frames(spec["clip"])
    fps = 60.0
    t0 = spec["clip_start_pts_s"]
    off = spec["t_minus_pts_ms"]
    clip_lo = int(round(t0 * 1000 + off))
    clip_hi = int(round((t0 + len(frames) / fps) * 1000 + off))
    times = []
    if spec.get("signals"):
        for line in Path(spec["signals"]).read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            if r.get("kind") == "hud" and isinstance(r.get("t_ms"), int):
                times.append(r["t_ms"])
    last = max(times) if times else clip_lo
    t = last + spec["reader_ms"]
    while t < clip_hi:
        times.append(t)
        t += spec["reader_ms"]
    rows = []
    for t in sorted(set(times)):
        if t < clip_lo:
            rows.append({"t_ms": t})  # before the tail: gameplay, no end word (from cod_signals)
            continue
        i = int(round(((t - off) / 1000.0 - t0) * fps))
        if not 0 <= i < len(frames):
            continue
        img = frames[i]
        w = end_word(read_end(img))
        o = read_owner_row(img)
        ok = bool(getattr(o, "ok", False) and getattr(o, "calibrated", False)
                  and same_owner_tag(getattr(o, "player_tag", None) or ""))
        stage = getattr(o, "stage", None)
        bp = read_best_play(img)
        row = {"t_ms": t, "frame": i}
        if w:
            row["word"] = w
        if ok:
            row["owner"] = True
        if ok or (stage not in (None, "tag_not_matched", "board_scale", "pack_missing")):
            row["board"] = True
        if bp.score:
            row["bp"] = bp.score
        if t > last:
            row["extrapolated"] = True
        rows.append(row)
    return {"match": spec["match"], "split": spec["split"], "confirm": spec.get("confirm"),
            "clip_t_ms": [clip_lo, clip_hi], "last_signals_t_ms": last if spec.get("signals") else None,
            "rows": rows}


def main(argv):
    specs = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    out = {"schema": "qorcap-cod-end-timelines/v0",
           "note": "Reader outputs on local practice tails. No pixels. Built by tools/cod_best_play_timelines.py.",
           "matches": [build(s) for s in specs]}
    Path(argv[2]).write_text(json.dumps(out, separators=(",", ":")) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv)
