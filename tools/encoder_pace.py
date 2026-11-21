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

Segments: each ``capture ffmpeg argv:`` line starts a new segment with that argv's ``-r`` as its
nominal fps (a qorcap.log is opened in append mode, so one file can hold several sessions and
writer restarts). A frame counter or clock that goes backwards also starts one. No window, deficit,
episode or drop run ever spans two segments.

UNKNOWN, not 0: with no readable progress window, pace is UNKNOWN (null). A segment is
drop-capable only if it has its argv line or at least one ``ffmpeg:`` line; otherwise its drop
counts are UNKNOWN (null), because an absence of drop lines there says nothing. If pace or drops
are UNKNOWN for the whole file the report is still printed and the exit code is 3.
Exit 2: missing file, or an fps (``--fps`` or an argv ``-r``) that is not a finite number > 0.

    python tools/encoder_pace.py path/to/qorcap.log [--fps 60] [--table]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
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
DEFAULT_FPS = 60.0
UNMEASURED = "UNMEASURED"
UNKNOWN = "UNKNOWN"
EXIT_UNKNOWN = 3

_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?Z\s+\[(\w+)\]\s?(.*)$")
_FRAME = re.compile(r"\bframe=(\d+)\b")
_ARGV_R = re.compile(r"\s-r\s+(\S+)")
_REPEAT = re.compile(r"Last message repeated\s+(\S+)\s+times?")


class BadRate(ValueError):
    """An fps that is not a finite number > 0."""


def check_fps(v: float, where: str) -> float:
    if not (math.isfinite(v) and v > 0):
        raise BadRate(f"{where}: fps must be a finite number > 0 (got {v!r})")
    return v


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
    | ('ffmpeg', ms) | ('argv', ms, fps|None). Returns (records, stats)."""
    out = []
    stats = {"lines": 0, "parsed_lines": 0, "skipped_lines": 0, "blank_lines": 0}
    for raw in lines:
        stats["lines"] += 1
        line = raw.strip()
        if not line:
            stats["blank_lines"] += 1
            continue
        m = _TS.match(line)
        ms = parse_ts(m.group(1), m.group(2)) if m else None
        if ms is None:
            stats["skipped_lines"] += 1
            continue
        stats["parsed_lines"] += 1
        msg = m.group(4)
        if msg.startswith("encoder progress:"):
            f = _FRAME.search(msg)
            if f:
                out.append(("progress", ms, int(f.group(1))))
            continue
        if msg.startswith("capture ffmpeg argv:"):
            r = _ARGV_R.search(msg)
            fps = None
            if r:
                try:
                    fps = float(r.group(1))
                except ValueError:
                    fps = float("nan")
            out.append(("argv", ms, fps))
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
        # Any other ffmpeg line (including an empty one) shows ffmpeg's log reached this file.
        out.append(("ffmpeg", ms))
    return out, stats


def _new_drops():
    return {"video_lines": 0, "audio_lines": 0, "other_lines": 0, "repeats": 0, "unread_repeats": 0}


def _messages(d) -> int:
    return d["video_lines"] + d["audio_lines"] + d["other_lines"] + d["repeats"]


def _split(records, fps_arg):
    """Segments at each argv line and at a frame counter or clock that goes backwards."""
    segs = []
    cur = None

    def open_seg(reason, fps, source, has_argv, start_ms):
        return {"reason": reason, "fps": fps, "fps_source": source, "has_argv": has_argv, "start_ms": start_ms, "records": []}

    for r in records:
        if r[0] == "argv":
            if fps_arg is not None:
                fps, source = fps_arg, "argument"
            elif r[2] is None:
                fps, source = DEFAULT_FPS, "default 60 (argv has no -r)"
            else:
                fps, source = check_fps(r[2], f"capture argv at {iso(r[1])}"), "capture argv"
            if cur is not None and cur["records"]:
                segs.append(cur)
            cur = open_seg("capture argv", fps, source, True, r[1])
            continue
        if cur is None:
            cur = open_seg("start of file", fps_arg if fps_arg is not None else DEFAULT_FPS,
                           "argument" if fps_arg is not None else "default 60 (no argv line)", False, r[1])
        if r[0] == "progress":
            last = next((x for x in reversed(cur["records"]) if x[0] == "progress"), None)
            if last is not None and (r[2] < last[2] or r[1] < last[1]):
                segs.append(cur)
                cur = open_seg("frame counter or clock went back", cur["fps"], cur["fps_source"], cur["has_argv"], r[1])
        cur["records"].append(r)
    if cur is not None and cur["records"]:
        segs.append(cur)
    return segs


def _segment(seg):
    nominal = seg["fps"]
    recs = seg["records"]
    t_seg = seg["start_ms"]
    drop_capable = seg["has_argv"] or any(r[0] in ("ffmpeg", "drop", "repeat") for r in recs)

    totals = _new_drops()
    windows30: dict[int, dict] = {}
    drop_events = []
    in_run = False
    for r in recs:
        if r[0] == "drop":
            key = f"{r[2]}_lines"
            totals[key] += 1
            windows30.setdefault((r[1] - t_seg) // DROP_WINDOW_MS, _new_drops())[key] += 1
            drop_events.append((r[1], 1))
            in_run = True
        elif r[0] == "repeat":
            if in_run:
                w = windows30.setdefault((r[1] - t_seg) // DROP_WINDOW_MS, _new_drops())
                if r[2] is None:
                    totals["unread_repeats"] += 1
                    w["unread_repeats"] += 1
                else:
                    totals["repeats"] += r[2]
                    w["repeats"] += r[2]
                    drop_events.append((r[1], r[2]))
        elif r[0] == "ffmpeg":
            in_run = False

    prog = [(r[1], r[2]) for r in recs if r[0] == "progress"]
    slow_fps = nominal * SLOW_RATIO
    ok_fps = nominal * OK_RATIO
    wins = []
    if prog:
        t0, f0 = prog[0]
        for (ta, fa), (tb, fb) in zip(prog, prog[1:]):
            ms = tb - ta
            if ms <= 0:
                continue
            rate = (fb - fa) * 1000.0 / ms
            deficit = (tb - t0) / 1000.0 - (fb - f0) / nominal
            wins.append({"end": iso(tb), "end_ms": tb, "ms": ms, "frames": fb - fa, "fps": round(rate, 2),
                         "deficit_s": round(deficit, 2), "slow": rate < slow_fps})

    episodes = []
    streak, ok_ms, ep = [], 0, None
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
                    ep["end_ms"], ep["closed"] = w["end_ms"], "back to real time"
                    episodes.append(ep)
                    ep = None
    if ep is not None:
        ep["end_ms"], ep["closed"] = ep["windows"][-1]["end_ms"], "still below real time at the last progress line"
        episodes.append(ep)

    out_eps = []
    for e in episodes:
        slow = [w for w in e["windows"] if w["slow"]]
        out_eps.append({
            "start": iso(e["start_ms"]), "end": iso(e["end_ms"]), "closed": e["closed"],
            "slow_windows": len(slow), "min_fps": min(w["fps"] for w in slow),
            "max_deficit_s": max(w["deficit_s"] for w in e["windows"]),
            "drop_messages": sum(n for t, n in drop_events if e["start_ms"] <= t <= e["end_ms"]) if drop_capable else None,
        })

    peak = max(wins, key=lambda w: w["deficit_s"]) if wins else None
    if drop_capable:
        drops = {**totals, "status": "MEASURED",
                 "lines": totals["video_lines"] + totals["audio_lines"] + totals["other_lines"],
                 "messages": _messages(totals),
                 "per_30s": [{"start": iso(t_seg + k * DROP_WINDOW_MS), **windows30[k], "messages": _messages(windows30[k])}
                             for k in sorted(windows30)]}
    else:
        drops = {k: None for k in _new_drops()}
        drops.update({"status": UNKNOWN, "lines": None, "messages": None, "per_30s": None})
    for w in wins:
        w.pop("end_ms", None)
    return {
        "first": iso(t_seg), "last": iso(recs[-1][1]), "reason": seg["reason"],
        "nominal_fps": nominal, "nominal_fps_source": seg["fps_source"],
        "slow_below_fps": round(slow_fps, 3),
        "progress_lines": len(prog), "windows": len(wins),
        "pace_status": "MEASURED" if wins else UNKNOWN,
        "slow_windows": sum(w["slow"] for w in wins) if wins else None,
        "min_fps": min(w["fps"] for w in wins) if wins else None,
        "peak_deficit_s": peak["deficit_s"] if peak else None,
        "peak_deficit_at": peak["end"] if peak else None,
        "episodes": out_eps,
        "drops": drops,
        "window_list": wins,
    }


def analyse(records, fps: float | None = None, stats: dict | None = None) -> dict:
    if fps is not None:
        check_fps(fps, "--fps")
    segs = [_segment(s) for s in _split(records, fps)]
    windows, episodes = [], []
    for i, s in enumerate(segs):
        windows.extend(dict(w, segment=i) for w in s.pop("window_list"))
        episodes.extend(dict(e, segment=i) for e in s["episodes"])
    for n, e in enumerate(episodes, 1):
        e["n"] = n
    known = [s["drops"] for s in segs if s["drops"]["status"] == "MEASURED"]
    if known:
        tot = {k: sum(d[k] for d in known) for k in _new_drops()}
        drops = {**tot, "status": "MEASURED" if len(known) == len(segs) else "PARTIAL",
                 "lines": tot["video_lines"] + tot["audio_lines"] + tot["other_lines"], "messages": _messages(tot)}
    else:
        drops = {**{k: None for k in _new_drops()}, "status": UNKNOWN, "lines": None, "messages": None}
    drops.update({"dropped_frames": UNMEASURED,
                  "note": "Warning messages, not frames. Repeat sums are a lower bound. Dropped frames UNMEASURED. "
                          "Totals cover drop-capable segments only."})
    pace_status = "MEASURED" if windows else UNKNOWN
    return {
        "tool": "tools/encoder_pace.py",
        "basis": "From the QorCap log; info only. Wall time is when QorCap logged each line.",
        "lines": stats or {},
        "pace_status": pace_status,
        "segments": segs,
        "windows": windows,
        "episodes": episodes,
        "drops": drops,
        "cause": UNMEASURED,
    }


def is_unknown(rep: dict) -> bool:
    return rep["pace_status"] == UNKNOWN or rep["drops"]["status"] == UNKNOWN


def table(rep: dict) -> str:
    out = []
    if "input_sha256" in rep:
        out.append(f"input sha256 {rep['input_sha256']}")
    st = rep.get("lines") or {}
    if st:
        out.append(f"lines {st['lines']}: parsed {st['parsed_lines']}, skipped {st['skipped_lines']}, blank {st['blank_lines']}")
    for i, s in enumerate(rep["segments"]):
        pace = (f"{s['slow_windows']}/{s['windows']} windows slow, min {s['min_fps']} fps, "
                f"peak deficit {s['peak_deficit_s']} s at {s['peak_deficit_at']}") if s["pace_status"] != UNKNOWN else "pace UNKNOWN"
        dm = s["drops"]["messages"]
        out.append(f"segment {i} {s['first']} .. {s['last']} ({s['reason']}; {s['nominal_fps']} fps, {s['nominal_fps_source']}): "
                   f"{pace}; drop messages {dm if dm is not None else 'UNKNOWN'}")
    for e in rep["episodes"]:
        dm = e["drop_messages"]
        out.append(f"episode {e['n']} (segment {e['segment']}) {e['start']} .. {e['end']} ({e['closed']}): {e['slow_windows']} slow windows, "
                   f"min {e['min_fps']} fps, max deficit {e['max_deficit_s']} s, {dm if dm is not None else 'UNKNOWN'} drop messages")
    d = rep["drops"]
    if d["status"] == UNKNOWN:
        out.append("drop messages: UNKNOWN (no drop-capable lines); dropped frames UNMEASURED")
    else:
        out.append(f"drop messages ({d['status']}): {d['messages']} (lines {d['lines']}: video {d['video_lines']}, audio {d['audio_lines']}, "
                   f"other {d['other_lines']}; repeats {d['repeats']}; unread repeats {d['unread_repeats']}); dropped frames UNMEASURED")
    if rep["pace_status"] == UNKNOWN:
        out.append("pace: UNKNOWN (no readable progress window)")
    out.append("cause: UNMEASURED")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("log", type=Path)
    ap.add_argument("--fps", type=float, default=None, help="nominal output fps for every segment (default: each segment's argv -r, else 60)")
    ap.add_argument("--table", action="store_true", help="print a short text table instead of JSON")
    a = ap.parse_args(argv)
    if not a.log.is_file():
        print(f"no such file: {a.log}", file=sys.stderr)
        return 2
    data = a.log.read_bytes()
    lines = data.decode("utf-8", errors="replace").splitlines()
    records, stats = parse_log(lines)
    try:
        rep = analyse(records, a.fps, stats)
    except BadRate as e:
        print(str(e), file=sys.stderr)
        return 2
    rep = {"input": str(a.log), "input_sha256": hashlib.sha256(data).hexdigest(), **rep}
    print(table(rep) if a.table else json.dumps(rep, indent=2))
    if is_unknown(rep):
        print("pace or drop messages UNKNOWN for this file (see report); exit 3", file=sys.stderr)
        return EXIT_UNKNOWN
    return 0


if __name__ == "__main__":
    sys.exit(main())
