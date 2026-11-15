#!/usr/bin/env python3
"""Offline encoder pace report from a QorCap log (qorcap.log or a console copy of it).

Read-only. Stdlib only. Prints JSON on stdout (``--table`` prints a short text table instead).
Nothing is written next to the log, and no receipt, checkpoint or recording is touched.

What it reads:
- ``encoder progress: frame=N ...`` lines (QorCap #149, every ~5 s). Per window between two such
  lines: wall ms from the log timestamps, frame delta, ``fps`` = frames / wall seconds, and the
  running ``deficit_s`` = wall seconds since the first line minus frames since then / nominal fps
  (how far the encoder output is behind real time, relative to the first line).
- ffmpeg lines logged as ``ffmpeg: ... real-time buffer ... frame dropped!`` and the
  ``Last message repeated N times`` lines right after them. Same run rule as src/capstop.rs:
  another ``ffmpeg:`` line ends the run; QorCap's own lines do not.

Wording: drop counts are warning MESSAGES, not frames. Dropped frames stay UNMEASURED and repeat
sums are a lower bound (ffmpeg prints a pending repeat count only when its next different message
arrives). Wall time is when QorCap logged the line, which is when it read ffmpeg's stderr.
No cause is inferred. See docs/ENCODER_PACE.md.

    python tools/encoder_pace.py path/to/qorcap.log [--fps 60] [--table]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

SLOW_RATIO = 59.0 / 60.0
OK_RATIO = 59.5 / 60.0
WARN_MIN_WINDOWS = 2
WARN_AFTER_MS = 9_500
RECOVER_MS = 30_000
DROP_WINDOW_MS = 30_000
UNMEASURED = "UNMEASURED"

_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?Z\s+\[(\w+)\]\s?(.*)$")
_FRAME = re.compile(r"\bframe=(\d+)\b")
_ARGV_R = re.compile(r"capture ffmpeg argv:.*?\s-r\s+(\d+(?:\.\d+)?)\s")
_REPEAT = re.compile(r"Last message repeated\s+(\S+)\s+times?")


def parse_ts(date: str, frac: str | None) -> int | None:
    """Milliseconds since the epoch (UTC) from ``2026-11-15T05:02:57`` + ``2582934``."""
    try:
        base = datetime.strptime(date, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    ms = int((frac or "0")[:3].ljust(3, "0"))
    return int(base.timestamp()) * 1000 + ms


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def is_input_drop(text: str) -> bool:
    low = text.lower()
    return "real-time buffer" in low and "frame dropped" in low


def parse_log(lines):
    """Records in log order: ('progress', ms, frame) | ('drop', ms, kind) | ('repeat', ms, n|None)
    | ('ffmpeg', ms) | ('argv_fps', ms, fps)."""
    out = []
    for raw in lines:
        m = _TS.match(raw.strip())
        if not m:
            continue
        ms = parse_ts(m.group(1), m.group(2))
        if ms is None:
            continue
        msg = m.group(4)
        if msg.startswith("encoder progress:"):
            f = _FRAME.search(msg)
            if f:
                out.append(("progress", ms, int(f.group(1))))
            continue
        if msg.startswith("capture ffmpeg argv:"):
            r = _ARGV_R.search(msg + " ")
            if r:
                out.append(("argv_fps", ms, float(r.group(1))))
            continue
        if not msg.startswith("ffmpeg:"):
            continue
        body = msg[len("ffmpeg:"):].strip()
        if is_input_drop(body):
            low = body.lower()
            kind = "video" if "[video input]" in low else "audio" if "[audio input]" in low else "other"
            out.append(("drop", ms, kind))
            continue
        r = _REPEAT.search(body)
        if r:
            n = r.group(1)
            out.append(("repeat", ms, int(n) if n.isdigit() else None))
            continue
        if body:
            out.append(("ffmpeg", ms))
    return out


def _new_drops():
    return {"video_lines": 0, "audio_lines": 0, "other_lines": 0, "repeats": 0, "unread_repeats": 0}


def _messages(d) -> int:
    return d["video_lines"] + d["audio_lines"] + d["other_lines"] + d["repeats"]


def analyse(records, fps: float | None = None) -> dict:
    nominal = fps
    if nominal is None:
        nominal = next((r[2] for r in records if r[0] == "argv_fps"), None)
    fps_source = "argument" if fps is not None else ("capture argv" if nominal is not None else None)
    if nominal is None:
        nominal = 60.0
        fps_source = "default 60 (no argv line)"

    # Drops: totals, per 30 s window from the first record, and a timeline for episodes.
    totals = _new_drops()
    t_first = records[0][1] if records else 0
    windows30: dict[int, dict] = {}
    drop_events = []  # (ms, messages)
    in_run = False
    for r in records:
        kind = r[0]
        if kind == "drop":
            key = f"{r[2]}_lines"
            totals[key] += 1
            w = windows30.setdefault((r[1] - t_first) // DROP_WINDOW_MS, _new_drops())
            w[key] += 1
            drop_events.append((r[1], 1))
            in_run = True
        elif kind == "repeat":
            if in_run:
                w = windows30.setdefault((r[1] - t_first) // DROP_WINDOW_MS, _new_drops())
                if r[2] is None:
                    totals["unread_repeats"] += 1
                    w["unread_repeats"] += 1
                else:
                    totals["repeats"] += r[2]
                    w["repeats"] += r[2]
                    drop_events.append((r[1], r[2]))
        elif kind == "ffmpeg":
            in_run = False

    # Encoder windows. A frame counter that goes back starts a new segment (writer restart).
    prog = [(r[1], r[2]) for r in records if r[0] == "progress"]
    segments = []
    cur: list = []
    for p in prog:
        if cur and (p[1] < cur[-1][1] or p[0] < cur[-1][0]):
            segments.append(cur)
            cur = []
        cur.append(p)
    if cur:
        segments.append(cur)

    slow_fps = nominal * SLOW_RATIO
    ok_fps = nominal * OK_RATIO
    out_segments = []
    all_windows = []
    episodes = []
    for seg in segments:
        t0, f0 = seg[0]
        wins = []
        for (ta, fa), (tb, fb) in zip(seg, seg[1:]):
            ms = tb - ta
            if ms <= 0:
                continue
            rate = (fb - fa) * 1000.0 / ms
            deficit = (tb - t0) / 1000.0 - (fb - f0) / nominal
            wins.append({"end": iso(tb), "end_ms": tb, "ms": ms, "frames": fb - fa, "fps": round(rate, 2),
                         "deficit_s": round(deficit, 2), "slow": rate < slow_fps})
        all_windows.extend(wins)
        # Episodes: >= 2 slow windows in a row covering >= 9.5 s; closed by 30 s at >= ok_fps.
        streak = []
        ok_ms = 0
        ep = None
        for w in wins:
            if w["slow"]:
                streak.append(w)
                ok_ms = 0
                if ep is None and len(streak) >= WARN_MIN_WINDOWS and sum(x["ms"] for x in streak) >= WARN_AFTER_MS:
                    ep = {"start_ms": streak[0]["end_ms"] - streak[0]["ms"], "windows": list(streak)}
                elif ep is not None:
                    ep["windows"].append(w)
            else:
                streak = []
                ok_ms = ok_ms + w["ms"] if w["fps"] >= ok_fps else 0
                if ep is not None:
                    ep["windows"].append(w)
                    if ok_ms >= RECOVER_MS:
                        ep["end_ms"] = w["end_ms"]
                        ep["closed"] = "back to real time"
                        episodes.append(ep)
                        ep = None
        if ep is not None:
            ep["end_ms"] = ep["windows"][-1]["end_ms"]
            ep["closed"] = "still below real time at the last progress line"
            episodes.append(ep)
        peak = max(wins, key=lambda w: w["deficit_s"]) if wins else None
        out_segments.append({
            "first": iso(t0), "last": iso(seg[-1][0]), "progress_lines": len(seg), "windows": len(wins),
            "slow_windows": sum(w["slow"] for w in wins),
            "min_fps": min((w["fps"] for w in wins), default=None),
            "peak_deficit_s": peak["deficit_s"] if peak else None,
            "peak_deficit_at": peak["end"] if peak else None,
        })

    out_episodes = []
    for i, ep in enumerate(episodes, 1):
        slow = [w for w in ep["windows"] if w["slow"]]
        msgs = sum(n for t, n in drop_events if ep["start_ms"] <= t <= ep["end_ms"])
        out_episodes.append({
            "n": i, "start": iso(ep["start_ms"]), "end": iso(ep["end_ms"]), "closed": ep["closed"],
            "slow_windows": len(slow), "min_fps": min(w["fps"] for w in slow),
            "max_deficit_s": max(w["deficit_s"] for w in ep["windows"]),
            "drop_messages": msgs,
        })

    by30 = []
    for k in sorted(windows30):
        d = windows30[k]
        by30.append({"start": iso(t_first + k * DROP_WINDOW_MS), **d, "messages": _messages(d)})

    for w in all_windows:
        w.pop("end_ms", None)
    return {
        "tool": "tools/encoder_pace.py",
        "basis": "MEASURED from the QorCap log; info only. Wall time is when QorCap logged each line.",
        "nominal_fps": nominal,
        "nominal_fps_source": fps_source,
        "slow_below_fps": round(slow_fps, 3),
        "segments": out_segments,
        "windows": all_windows,
        "episodes": out_episodes,
        "drops": {
            **totals,
            "lines": totals["video_lines"] + totals["audio_lines"] + totals["other_lines"],
            "messages": _messages(totals),
            "dropped_frames": UNMEASURED,
            "note": "Warning messages, not frames. Repeat sums are a lower bound. Dropped frames UNMEASURED.",
            "per_30s": by30,
        },
        "cause": UNMEASURED,
    }


def table(rep: dict) -> str:
    out = [f"nominal fps {rep['nominal_fps']} ({rep['nominal_fps_source']}); slow below {rep['slow_below_fps']} fps"]
    for s in rep["segments"]:
        out.append(f"segment {s['first']} .. {s['last']}: {s['slow_windows']}/{s['windows']} windows slow, "
                   f"min {s['min_fps']} fps, peak deficit {s['peak_deficit_s']} s at {s['peak_deficit_at']}")
    for e in rep["episodes"]:
        out.append(f"episode {e['n']} {e['start']} .. {e['end']} ({e['closed']}): {e['slow_windows']} slow windows, "
                   f"min {e['min_fps']} fps, max deficit {e['max_deficit_s']} s, {e['drop_messages']} drop messages")
    d = rep["drops"]
    out.append(f"drop messages: {d['messages']} (lines {d['lines']}: video {d['video_lines']}, audio {d['audio_lines']}, "
               f"other {d['other_lines']}; repeats {d['repeats']}; unread repeats {d['unread_repeats']}); dropped frames UNMEASURED")
    out.append("cause: UNMEASURED")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("log", type=Path)
    ap.add_argument("--fps", type=float, default=None, help="nominal output fps (default: from the capture argv line, else 60)")
    ap.add_argument("--table", action="store_true", help="print a short text table instead of JSON")
    a = ap.parse_args(argv)
    if not a.log.is_file():
        print(f"no such file: {a.log}", file=sys.stderr)
        return 2
    if a.fps is not None and not (a.fps > 0):
        print("--fps must be > 0", file=sys.stderr)
        return 2
    lines = a.log.read_text(encoding="utf-8", errors="replace").splitlines()
    rep = analyse(parse_log(lines), a.fps)
    print(table(rep) if a.table else json.dumps(rep, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
