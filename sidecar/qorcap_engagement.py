"""Press-window engagement capture (PWEC): an info-only engagement window index.

After a practice Call of Duty match stops, this reads the two files QorCap already wrote in the
match folder, ``inputs.jsonl`` (controller log, ``qorcap-inputs/v0``) and ``frame_clock.jsonl``
(``qorcap-frame-clock/v0``), and writes ``engagement_windows.jsonl`` plus a small summary to its
own folder **outside** the match folder (default ``%LOCALAPPDATA%\\QorCapEngagement\\<match>\\``,
the same pattern as the Glimpse watcher's ``QorCapGlimpse``).

What a window is (controller input only):

* A trigger is *held* while its analog value is > 60. Rows are logged on change, so the state at
  time t is the last row at or before t. A hold runs from the row where the value goes above 60
  to the row where it drops to 60 or below.
* An *episode* joins every R2 and L2 hold whose start is within 500 ms of the running end.
  Core = [first trigger down, last trigger up]. Window = [core start - 500 ms, core end + 1000 ms].
* Class: ``L2+R2`` (some R2-held time overlaps L2-held time), ``R2-only``, ``L2-only``, or
  ``split`` (both held, never at the same time).

A window says "L2 and R2 were held (input)". It does not say that ADS was seen on screen or that
there was a gunfight. Nothing here reads pixels, calls a model, or opens a network socket.

Frame mapping and its uncertainty: frame time T = pts_90k * 100 / 9 + env, with
env = min over ``video.mp4`` frames of (t_mono_us - pts_90k * 100 / 9). The capture offset between
that time and the controller clock is UNMEASURED, so each frame carries the window
W(T) = [T - 233.3 ms, T + 16.7 ms]. Three frame ranges are written per window:
``window`` (W touches the padded window), ``core_possible`` (W touches the core) and
``core_certain`` (W lies wholly inside the core; only these frames may carry "trigger held at this
frame" as an input fact).

Exclusion flags (the window is still written, with ``excluded: true``):
``out_of_span``, ``open_at_log_end``, ``long_core`` (core > 8 s), ``hid_doubt`` (controller
heartbeat faults, stale/short/parse/seq faults, rx gap > 50 ms, heartbeats > 1.5 s apart, or no
heartbeat after the window), ``backlog`` (a frame whose arrival trails T by > 500 ms within
[start - 1 s, end + 0.5 s]) and ``match_b4`` (only when the caller passes it: the B4 alignment
check is not run here, so the summary records it as UNMEASURED).

Info only. Never Counted. Nothing here feeds a receipt, a lock, a timeline, a referee submit, an
upload or any Counted value. The match folder is read, never written. Any error is caught by
``maybe_write_engagement_windows``; it prints one line and returns False.
"""
from __future__ import annotations

import argparse
import bisect
import collections
import hashlib
import json
import os
import sys
from pathlib import Path

SCHEMA = "qorcap-engagement-windows/v0-info"
SUMMARY_SCHEMA = "qorcap-engagement-windows-summary/v0-info"
VERSION = "pwec-0.1.0"
FILE_NAME = "engagement_windows.jsonl"
SUMMARY_NAME = "engagement_windows.summary.json"
OUT_DIR_NAME = "QorCapEngagement"

ENV_LANE = "QORCAP_LANE"
ENV_FLAG = "QORCAP_PWEC"          # "0"/"false"/"no"/"off" turns it off on the practice lane
ENV_OUT = "QORCAP_PWEC_OUT"       # output root override (must be outside the match folder)

# Plan defaults (PLAN-press-window-engagement-capture.md section 1; kept by Con, 12:23 PM CST).
THRESHOLD = 60                    # held = value > 60
W_LO_US = 233_333                 # W(T) = [T - 233.3 ms, T + 16.7 ms]
W_HI_US = 16_667
MERGE_GAP_US = 500_000
PRE_US = 500_000
POST_US = 1_000_000
MAX_CORE_US = 8_000_000
BACKLOG_LAG_US = 500_000
BACKLOG_BEFORE_US = 1_000_000
BACKLOG_AFTER_US = 500_000
HB_MAX_GAP_US = 1_500_000
HB_MAX_RX_GAP_US = 50_000
FRAME_FILE = "video.mp4"

CLASSES = ("L2+R2", "R2-only", "L2-only", "split")
WORDING = ("Controller input only: trigger values > 60 on the QorCap clock. Not ADS seen on screen, "
           "not a gunfight, not a judgement of play. Info only; never Counted.")


class PwecError(Exception):
    pass


# ---------------------------------------------------------------------------------------------- parse
def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _hb_bad(r: dict) -> bool:
    return bool(r.get("faults") or r.get("stale_over_50ms") or r.get("seq_breaks") or r.get("short_reads")
                or r.get("parse_faults") or (r.get("max_rx_gap_us") or 0) > HB_MAX_RX_GAP_US)


def parse_inputs(lines) -> dict:
    """Trigger rows, heartbeats and the last t_us from inputs.jsonl lines. Bad lines are counted, not fatal."""
    rows, hb = [], []
    last = 0
    bad_lines = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except ValueError:
            bad_lines += 1
            continue
        if not isinstance(r, dict):
            bad_lines += 1
            continue
        t = r.get("t_us")
        if isinstance(t, bool) or not isinstance(t, (int, float)):
            t = None
        if t is not None:
            last = max(last, t)
        typ = r.get("type")
        if typ == "in" and t is not None and r.get("r2") is not None and r.get("l2") is not None:
            rows.append((t, r["r2"], r["l2"]))
        elif typ == "hb" and t is not None:
            hb.append((t, _hb_bad(r)))
    return {"rows": rows, "hb": hb, "last_t_us": last, "bad_lines": bad_lines}


def parse_frame_clock(lines, file: str = FRAME_FILE) -> dict:
    frames = []
    other_files = collections.Counter()
    bad_lines = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except ValueError:
            bad_lines += 1
            continue
        if not isinstance(r, dict) or r.get("type") != "frame":
            continue
        f = r.get("file", FRAME_FILE)
        if f != file:
            other_files[str(f)] += 1
            continue
        try:
            frames.append((int(r["frame"]), r["t_mono_us"], r["pts_90k"]))
        except (KeyError, TypeError, ValueError):
            bad_lines += 1
    return {"frames": frames, "other_files": dict(other_files), "bad_lines": bad_lines}


# ---------------------------------------------------------------------------------------------- core
def hold_intervals(rows, key: int, t_end):
    """[(down_t, up_t, open_at_end)] for one trigger. ``key`` 1 = r2, 2 = l2 in ``rows``."""
    iv, down = [], None
    for row in rows:
        t, v = row[0], row[key]
        held = v > THRESHOLD
        if held and down is None:
            down = t
        elif not held and down is not None:
            iv.append((down, t, False))
            down = None
    if down is not None:
        iv.append((down, t_end, True))
    return iv


def build_episodes(r2iv, l2iv, gap: int = MERGE_GAP_US):
    ev = sorted([(a, b, "R", o) for a, b, o in r2iv] + [(a, b, "L", o) for a, b, o in l2iv])
    eps = []
    for a, b, k, o in ev:
        if eps and a <= eps[-1]["end"] + gap:
            e = eps[-1]
            e["end"] = max(e["end"], b)
            e[k].append((a, b))
            e["open"] |= o
        else:
            eps.append({"start": a, "end": b, "R": [(a, b)] if k == "R" else [], "L": [(a, b)] if k == "L" else [],
                        "open": o})
    for e in eps:
        ov = sum(max(0, min(b1, b2) - max(a1, a2)) for a1, b1 in e["R"] for a2, b2 in e["L"])
        if ov > 0:
            e["cls"] = "L2+R2"
        elif e["R"] and not e["L"]:
            e["cls"] = "R2-only"
        elif e["L"] and not e["R"]:
            e["cls"] = "L2-only"
        else:
            e["cls"] = "split"
        e["overlap_us"] = ov
    return eps


def frame_times(frames):
    """(T list, frame index list, env_us, lag-bad T list, fps or None). T on the inputs t_us clock."""
    if not frames:
        raise PwecError(f"no {FRAME_FILE} frames in frame_clock.jsonl")
    env = min(t - p * 100 / 9 for _, t, p in frames)
    pairs = sorted(((p * 100 / 9 + env, f, t) for f, t, p in frames), key=lambda x: x[0])
    T = [x[0] for x in pairs]
    FI = [x[1] for x in pairs]
    lagbad = [x[0] for x in pairs if x[2] - x[0] > BACKLOG_LAG_US]
    steps = collections.Counter(b[2] - a[2] for a, b in zip(frames, frames[1:]))
    step = steps.most_common(1)[0][0] if steps else 0
    fps = 90000 / step if step > 0 else None
    return T, FI, env, lagbad, fps


def _range(FI, i, j):
    n = max(0, j - i)
    if n == 0:
        return {"first": None, "last": None, "count": 0}
    return {"first": FI[i], "last": FI[j - 1], "count": n}


def build_windows(inputs: dict, frames: list, *, match_flags=()) -> tuple[list, dict]:
    rows, hb, last = inputs["rows"], inputs["hb"], inputs["last_t_us"]
    T, FI, env, lagbad, fps = frame_times(frames)
    hbt = [t for t, _ in hb]

    def hid_doubt(a, b):
        j = bisect.bisect_left(hbt, a)
        k = bisect.bisect_left(hbt, b)
        if j >= len(hb) or k >= len(hb):
            return True
        if j > 0 and hbt[j] - hbt[j - 1] > HB_MAX_GAP_US:
            return True
        if any(hbt[x] - hbt[x - 1] > HB_MAX_GAP_US for x in range(max(j, 1), k + 1)):
            return True
        return any(bd for _, bd in hb[j:k + 1])

    r2iv = hold_intervals(rows, 1, last)
    l2iv = hold_intervals(rows, 2, last)
    eps = build_episodes(r2iv, l2iv, MERGE_GAP_US)
    out = []
    for n, e in enumerate(eps):
        a, b = e["start"], e["end"]
        wa, wb = a - PRE_US, b + POST_US
        i0 = bisect.bisect_left(T, wa - W_HI_US)
        i1 = bisect.bisect_right(T, wb + W_LO_US)
        c0 = bisect.bisect_left(T, a + W_LO_US)
        c1 = bisect.bisect_right(T, b - W_HI_US)
        p0 = bisect.bisect_left(T, a - W_HI_US)
        p1 = bisect.bisect_right(T, b + W_LO_US)
        flags = []
        if wa < T[0] + W_LO_US or wb > T[-1] - W_HI_US:
            flags.append("out_of_span")
        if e["open"]:
            flags.append("open_at_log_end")
        if b - a > MAX_CORE_US:
            flags.append("long_core")
        if hid_doubt(wa, wb):
            flags.append("hid_doubt")
        lj = bisect.bisect_left(lagbad, wa - BACKLOG_BEFORE_US)
        if lj < len(lagbad) and lagbad[lj] <= wb + BACKLOG_AFTER_US:
            flags.append("backlog")
        for f in match_flags:
            if f not in flags:
                flags.append(f)
        out.append({
            "schema": SCHEMA, "version": VERSION, "type": "window", "info_only": True, "counted": False,
            "advisory_only": True, "index": n, "class": e["cls"],
            "input": {
                "core_t_us": [a, b], "core_ms": round((b - a) / 1000, 1),
                "r2_holds": len(e["R"]), "l2_holds": len(e["L"]),
                "r2_held_ms": round(sum(y - x for x, y in e["R"]) / 1000, 1),
                "l2_held_ms": round(sum(y - x for x, y in e["L"]) / 1000, 1),
                "r2l2_overlap_ms": round(e["overlap_us"] / 1000, 1),
                "r2_t_us": [[x, y] for x, y in e["R"]], "l2_t_us": [[x, y] for x, y in e["L"]],
                "open_at_log_end": bool(e["open"]),
            },
            "t_us": [wa, wb],
            "frames": {"window": _range(FI, i0, i1), "core_possible": _range(FI, p0, p1),
                       "core_certain": _range(FI, c0, c1)},
            "flags": flags, "excluded": bool(flags),
        })
    clock = {"basis": "T = pts_90k*100/9 + env (frame_clock.jsonl, video.mp4)", "env_us": round(env, 1),
             "fps": fps, "video_frames": len(T), "video_s": round((T[-1] - T[0]) / 1e6, 1),
             "frame_W_ms": [-233.3, 16.7], "capture_offset": "UNMEASURED",
             "backlog_frames_gt500ms": len(lagbad)}
    return out, {"clock": clock, "r2_holds": len(r2iv), "l2_holds": len(l2iv)}


def summarize(windows, meta, *, match, inputs_info, match_flags=()) -> dict:
    cls = collections.Counter(w["class"] for w in windows)
    clean = collections.Counter(w["class"] for w in windows if not w["flags"])
    flags = collections.Counter(f for w in windows for f in w["flags"])
    return {
        "schema": SUMMARY_SCHEMA, "version": VERSION, "info_only": True, "counted": False, "advisory_only": True,
        "match": match, "wording": WORDING,
        "params": {"threshold_gt": THRESHOLD, "W_lo_ms": 233.3, "W_hi_ms": 16.7, "merge_gap_ms": MERGE_GAP_US / 1000,
                   "pre_roll_ms": PRE_US / 1000, "post_roll_ms": POST_US / 1000, "max_core_ms": MAX_CORE_US / 1000},
        "inputs": inputs_info, **meta,
        "match_b4": ("caller flag: match_b4" if "match_b4" in match_flags
                     else "UNMEASURED (the B4 alignment check is not run here)"),
        "windows": {c: cls.get(c, 0) for c in CLASSES},
        "clean_windows": {c: clean.get(c, 0) for c in CLASSES},
        "flags": dict(sorted(flags.items())),
    }


# ---------------------------------------------------------------------------------------------- files
def analyze_match_dir(match_dir: Path, *, match_flags=()) -> tuple[list, dict]:
    """Read-only. Opens inputs.jsonl and frame_clock.jsonl for reading only."""
    match_dir = Path(match_dir)
    ip, fp = match_dir / "inputs.jsonl", match_dir / "frame_clock.jsonl"
    for p in (ip, fp):
        if not p.is_file():
            raise PwecError(f"{p.name} not present")
    with open(ip, "r", encoding="utf-8", errors="replace") as f:
        inputs = parse_inputs(f)
    with open(fp, "r", encoding="utf-8", errors="replace") as f:
        fc = parse_frame_clock(f)
    windows, meta = build_windows(inputs, fc["frames"], match_flags=match_flags)
    info = {"inputs_sha256": _sha256(ip), "frame_clock_sha256": _sha256(fp),
            "inputs_bad_lines": inputs["bad_lines"], "frame_clock_bad_lines": fc["bad_lines"],
            "frame_clock_other_files": fc["other_files"]}
    for w in windows:
        w["match"] = match_dir.name
    return windows, summarize(windows, meta, match=match_dir.name, inputs_info=info, match_flags=match_flags)


def default_out_root(environ=None) -> Path:
    env = os.environ if environ is None else environ
    if env.get(ENV_OUT):
        return Path(env[ENV_OUT])
    base = env.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(base) / OUT_DIR_NAME


def _under(child: Path, parent: Path) -> bool:
    c, p = os.path.realpath(child), os.path.realpath(parent)
    try:
        return os.path.commonpath([c, p]) == p
    except ValueError:
        return False


def out_dir_for(match_dir: Path, out_root: Path) -> Path:
    match_dir, out_root = Path(match_dir), Path(out_root)
    out = out_root / match_dir.name
    if _under(out, match_dir) or _under(match_dir, out_root):
        raise PwecError("refusing: the engagement window folder must be outside the match folder")
    return out


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def write_engagement_windows(match_dir: Path, out_root: Path | None = None, *, match_flags=()) -> Path:
    """Analyze ``match_dir`` and write both files under ``out_root/<match>/``. Returns the jsonl path."""
    match_dir = Path(match_dir)
    out = out_dir_for(match_dir, out_root if out_root is not None else default_out_root())
    windows, summary = analyze_match_dir(match_dir, match_flags=match_flags)
    out.mkdir(parents=True, exist_ok=True)
    jl = out / FILE_NAME
    _atomic_write(jl, "".join(json.dumps(w, sort_keys=True, separators=(",", ":")) + "\n" for w in windows))
    _atomic_write(out / SUMMARY_NAME, json.dumps(summary, indent=1, sort_keys=True) + "\n")
    return jl


def _truthy_lane(environ) -> bool:
    lane = environ.get(ENV_LANE)
    return isinstance(lane, str) and lane.strip().lower() == "practice"


def _falsy(v) -> bool:
    return isinstance(v, str) and v.strip().lower() in ("0", "false", "no", "off")


def enabled(game, environ=None) -> bool:
    """Practice lane (``QORCAP_LANE=practice``) and Call of Duty, unless ``QORCAP_PWEC`` is 0/false/no/off."""
    env = os.environ if environ is None else environ
    return game == "cod" and _truthy_lane(env) and not _falsy(env.get(ENV_FLAG))


def maybe_write_engagement_windows(bundle, game, *, environ=None, out_root=None, say=None) -> bool:
    """Post-stop hook. Off unless ``enabled``. Never raises; an error prints one line and returns False."""
    try:
        env = os.environ if environ is None else environ
        if not enabled(game, env):
            return False
        root = Path(out_root) if out_root is not None else default_out_root(env)
        path = write_engagement_windows(Path(bundle), root)
        if say is not None:
            say(f"  Engagement windows (info only, not Counted): {path}")
        return True
    except Exception as e:  # noqa: BLE001 - info-only sidecar: never let it reach the save path
        try:
            if say is not None:
                say(f"  Engagement windows were not written (info only; the match is unaffected): {type(e).__name__}")
        except Exception:
            pass
        return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Write the info-only engagement window index for one match folder.")
    ap.add_argument("--match-dir", required=True)
    ap.add_argument("--out", help=f"output root (default %%LOCALAPPDATA%%\\{OUT_DIR_NAME}); must be outside the match folder")
    ap.add_argument("--match-b4-fail", action="store_true",
                    help="flag every window match_b4 (an outside B4 alignment check failed this match)")
    a = ap.parse_args(argv)
    flags = ("match_b4",) if a.match_b4_fail else ()
    try:
        p = write_engagement_windows(Path(a.match_dir), Path(a.out) if a.out else None, match_flags=flags)
    except (PwecError, OSError) as e:
        print(f"engagement windows not written: {e}", file=sys.stderr)
        return 2
    print(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
