#!/usr/bin/env python3
"""QorCap capture A/B. Run this on the Windows laptop that has the capture card.

Today's capture (MJPEG 1280x720@30, 512M video rtbuf, JPEG q:v 3 snapshot) is the
baseline. Each other variant turns on one opt-in flag. Nothing here changes
scoring, receipts, or signing. A flag stays off unless this report says it may
be promoted. The baseline row is the reference, not a candidate.

Plain ``py -3`` on the laptop does not have cv2, so score reads will not run.
Use the QorCap venv:

    %LOCALAPPDATA%\\QorCap\\venv\\Scripts\\python.exe tools\\capture_ab.py --secs 15 --stable-picture

ffmpeg is taken from --ffmpeg, from ffmpeg.exe beside qorcap.exe (the release
bundle), or from %LOCALAPPDATA%\\QorCap\\ffmpeg.exe. If none of those exist the
harness stops and asks for --ffmpeg. qortime is not downloaded; pass --qortime
or put the bundle's qortime folder next to qorcap.exe.

Pause Madden on a readable scorebug for the whole run and pass --stable-picture.
That flag compares the scorebug bar (the same 1280x720 crop Glimpse uses) across
the take and against the baseline take. A take whose bar changes is retried once
and then flagged, so live play cannot silently pass as the same footage.

--yuy2-720p60 is not recommended for the MS2131. A live 15s Madden take on
2026-10-20 (score held at 3-0) delivered 11.6 fps, 27 "real-time buffer too full"
drops, a 400 MB video queue, and missed the 3-0. The USB link check on that run
returned unknown (0 bytes of PnP text, 30 modes) because the PnP probe produced
nothing; the mode list is a separate ffmpeg query. Leave the flag off.

Promotion rule
--------------
A flag may become the default only when, against this baseline:

* zero new wrong score reads (no locked home/away pair the baseline did not lock)
* no missed locked scores (every baseline home/away pair was locked again)
* read success rate (agreed frames / frames the reader attempted) is not worse than the
  sampling noise of that rate. A 1–3 read gap on ~50 snapshots is noise: every take on
  2026-10-20 agreed exactly two fewer frames than it saved. Both sides still need at
  least 8 successful reads, or the reads gate is undecided.
* at least one metric is measurably better (a tie is not a promotion)
* the baseline actually locked at least one score (otherwise score quality is not proven)
* the comparison is on the same footage (same-footage re-encode, or live takes whose
  scorebug region stayed stable under --stable-picture)
* dropped frames are not higher, and duplicate frames are not higher
* audio lag is not worse (variant <= baseline + 0.5s; a new lag above 0.5s fails)
* video real-time queue is not higher by more than 1 MB
* CPU of the qorcap process tree (qorcap + ffmpeg) is not meaningfully higher: not
  above baseline by more than max(8 percentage points, 25% of the baseline CPU).
  CPU is the sum of one-core percents, so two busy cores read as 200%.
  A missing sample or a zero delta is not measured and does not pass.
* disk MB/min, from the recording's ffprobe duration (not wall-clock), is not higher
  by more than max(4 MB/min, 10% of the baseline rate). The laptop this was sized
  for had about 6.4 GB free.

If a number was not measured, the flag is not promoted.

The capture window is `secs` of snapshots after the first frame. qorcap's `--duration`
starts when the process starts, before the card opens, so a 15s budget used to end
with only ~46–51 of the 60 frames ffmpeg writes at 4/s.

Measured on the MS2131 (paused Madden, score held at 3-0)
---------------------------------------------------------
fc061e0, first harness: CPU was 0.0% on 9 of 11 takes, 2–7 snapshots per 15s, disk
43–62 MB/min, and `--stable-picture` did not look at pixels. Not used for promotion.

637145d, MyTurn, 5:22 AM CT, 4m03s: CPU measured on all 11 takes (30–32 samples,
Toolhelp32, baseline 3.5%, every take <= 5.4%). ffprobe disk 59.6–61.3 MB/min.
Scorebug bar stayed stable, no retries. USB link `superspeed` (1082 bytes of PnP
text, 30 modes). Every read was 3-0, nothing wrong or missed. Baseline 49/51 reads
from 51/60 snapshots. reader_hq and reader_png tied the same-footage re-encode
(54 vs 54). Eight other flags were 46–48 reads against 49 only because
reads == snapshots saved − 2 and the saves were 46–51 of 60. No flag promoted.
Capture CPU on this card is about 3–5%.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# Noise floors and the CPU gate. These are the promotion rule, not suggestions.
CPU_ABS_POINTS = 8.0
CPU_REL = 0.25
# 8 Mbit/s CBR plus 160 kbit/s audio is about 58 MiB/min. A 15s ffprobe duration
# that is off by a few tenths of a second, plus container padding, moves the rate
# by a few MB/min. The old 0.05 MB/min floor flagged that noise.
DISK_ABS_MB_PER_MIN = 4.0
DISK_REL = 0.10
LAG_NOISE_S = 0.5
VIDEO_QUEUE_NOISE_MB = 1.0
AUDIO_BYTES_PER_SEC = 48_000 * 2 * 2  # 16-bit stereo 48 kHz, the card's native format
MIN_SUCCESSFUL_READS = 8
# Two-proportion noise on the read rate. 49/51 vs 46/48 is inside this; 30/40 vs 10/40 is not.
READ_RATE_ABS = 0.03
READ_RATE_Z = 1.96
FPS_ABS = 1.0
FPS_REL = 0.05
MEDIA_ABS_S = 1.0
MEDIA_REL = 0.10
SNAP_HZ = 4.0
SNAP_INTERVAL_S = 1.0 / SNAP_HZ
CPU_INTERVAL_S = 0.5
CPU_MIN_SPAN_S = 0.5
# qorcap --duration starts at process start, before the device opens. This backstop
# keeps the process alive; the harness itself stops `secs` after the first snapshot.
STARTUP_BACKSTOP_S = 45
SNAP_POLL_S = 0.02
STATUS_INTERVAL_S = 0.5
# Same bar Glimpse crops: y0, y1, x0, x1 on a 1280x720 frame (standard / SNP / TNP / MNP).
SCOREBUG_BOX = (640, 720, 300, 980)
MEAN_LIMIT = 8.0
TILE_LIMIT = 24.0
TILE_PX = 16
# A couple of startup or glitch frames must not fail a paused take. A score change
# that covers more than this fraction of the saved strips is a different picture.
CHANGED_FRACTION = 0.15

# Measured on the MS2131 with live Madden held at 3-0 (2026-10-20, 15s). Not a default.
YUY2_NOT_RECOMMENDED = (
    "Not recommended for the MS2131. Two paused-at-3-0 takes on 2026-10-20 both failed. "
    "fc061e0: 11.6 fps, 27 real-time buffer drops, a 400 MB queue, missed the 3-0, USB link "
    "unknown (0 bytes of PnP text, 30 modes). 637145d on confirmed SuperSpeed (1082 bytes of "
    "PnP text, 30 modes): 21 fps reported but only 5.9 s of media in a 16.6 s take, 39 buffer "
    "drops, a 366 MB queue. Leave --yuy2-720p60 off. No flag was promoted. Capture CPU was about 3-5%."
)

# Baseline is today's Start Match capture: explicit mjpeg 720p30, no tune flags.
# (The qorcap CLI default for --input-format is `auto`. Start Match passes mjpeg.)
VARIANTS: list[tuple[str, list[str]]] = [
    ("baseline", []),
    ("yuy2_720p60", ["--yuy2-720p60", "--check-usb-link"]),
    ("reader_hq", ["--reader-snapshot", "hq"]),
    ("reader_png", ["--reader-snapshot", "png"]),
    ("rtbuf_128m", ["--rtbufsize", "128M"]),
    ("audio_rtbuf_512k", ["--audio-rtbufsize", "512K"]),
    ("short_probe", ["--short-probe"]),
    ("buffer_watch", ["--buffer-watch"]),
    ("buffer_restart", ["--rtbufsize", "128M", "--audio-rtbufsize", "512K", "--short-probe", "--buffer-watch", "--buffer-restart"]),
    ("no_signal", ["--no-signal"]),
    ("replug_resilient", ["--replug-resilient"]),
]

# Same-footage reader-feed check. These do not change the recording.
SAME_FOOTAGE = {
    "q3": ["-q:v", "3"],
    "hq": ["-q:v", "2"],
    "png": ["-c:v", "png", "-compression_level", "1"],
}

_UNDECIDED_MARKERS = (
    "was not measured",
    "too few score reads",
    "could not be verified",
    "did not run",
    "not proven",
)


def venv_python_hint() -> str:
    local = os.environ.get("LOCALAPPDATA") or "%LOCALAPPDATA%"
    return str(Path(local) / "QorCap" / "venv" / "Scripts" / "python.exe")


def record_command(qorcap: str, ffmpeg: str | None, port: int, log: str, out: str, secs: int,
                   extra: list[str], device: str = "USB3.0 Video") -> list[str]:
    cmd = [qorcap, "--port", str(port), "--log", log]
    if ffmpeg:
        cmd += ["--ffmpeg", ffmpeg]
    cmd += [
        "record", "--device", device, "--res", "1280x720", "--fps", "30",
        "--input-format", "mjpeg", "--audio", "auto",
        "--out", out, "--duration", str(secs), "--no-controller",
    ]
    return cmd + list(extra)


def parse_buffer_warning(line: str) -> dict | None:
    """Same shape as the Rust parser: a dshow 'real-time buffer ... full' line, or None."""
    raw = line.strip()
    lower = raw.lower()
    if "real-time buffer" not in lower or "full" not in lower:
        return None
    stream = ""
    if "[" in raw and "]" in raw[raw.find("[") + 1:]:
        i = raw.find("[")
        stream = raw[i + 1:raw.find("]", i)].strip()
    percent = _first_percent(lower)
    size = _last_size(raw)
    kind = _buffer_kind(stream, size)
    queued = int(size * percent / 100) if size and percent is not None else 0
    lag = (queued / AUDIO_BYTES_PER_SEC) if kind == "audio" and queued else None
    return {
        "stream": stream,
        "percent": percent,
        "size_bytes": size,
        "kind": kind,
        "queued_bytes": queued,
        "queued_mb": queued / (1024 * 1024) if queued else 0.0,
        "lag_s": lag,
    }


def _first_percent(lower: str) -> int | None:
    i = lower.find("%")
    if i < 0:
        return None
    start = i
    while start > 0 and lower[start - 1].isdigit():
        start -= 1
    if start == i:
        return None
    try:
        return int(lower[start:i])
    except ValueError:
        return None


def _last_size(text: str) -> int | None:
    last = None
    cur = ""
    def flush() -> None:
        nonlocal last, cur
        if cur:
            try:
                n = int(cur)
            except ValueError:
                n = 0
            if n > 100:
                last = n
        cur = ""
    for c in text:
        if c.isdigit():
            cur += c
        elif c == "," and cur:
            continue
        else:
            flush()
    flush()
    return last


def _buffer_kind(stream: str, size: int | None) -> str:
    s = stream.lower()
    if "audio" in s:
        return "audio"
    if "video" in s:
        return "video"
    if size is None:
        return "unknown"
    return "audio" if size <= 16 * 1024 * 1024 else "video"


def summarize_buffer(text: str) -> dict:
    audio = None
    video = None
    n = 0
    for line in text.splitlines():
        w = parse_buffer_warning(line)
        if not w:
            continue
        n += 1
        if w["lag_s"] is not None:
            audio = w["lag_s"] if audio is None else max(audio, w["lag_s"])
        if w["kind"] == "video" and w["queued_mb"]:
            video = w["queued_mb"] if video is None else max(video, w["queued_mb"])
    return {"buffer_warnings": n, "audio_lag_s": audio, "video_queued_mb": video}


def cpu_percent(cpu_seconds: float | None, wall_seconds: float | None) -> float | None:
    """Percent of one core. A missing or non-positive CPU delta is not a measurement."""
    if cpu_seconds is None or wall_seconds is None or wall_seconds <= 0 or cpu_seconds <= 0:
        return None
    return 100.0 * cpu_seconds / wall_seconds


def cpu_percent_from_samples(samples: list[tuple[float, float]]) -> float | None:
    """CPU% from (monotonic_time, process-tree cpu seconds) samples.

    Needs two samples at least CPU_MIN_SPAN_S apart whose CPU-seconds increased.
    A flat or all-zero series is not measured (the old harness reported that as 0.0%
    and the gate treated 0.0 as a pass).
    """
    if len(samples) < 2:
        return None
    t0, s0 = samples[0]
    t1, s1 = samples[-1]
    wall = t1 - t0
    if wall < CPU_MIN_SPAN_S or s1 <= 0:
        return None
    delta = s1 - s0
    if delta <= 0:
        return None
    return cpu_percent(delta, wall)


def disk_limit(baseline_rate: float) -> float:
    return baseline_rate + max(DISK_ABS_MB_PER_MIN, DISK_REL * baseline_rate)


def disk_mb_per_min(nbytes: int | None, seconds: float | None) -> float | None:
    if nbytes is None or seconds is None or seconds <= 0:
        return None
    return (nbytes / (1024 * 1024)) / (seconds / 60.0)


def parse_duration(text: str) -> float | None:
    try:
        dur = float((text or "").strip())
    except ValueError:
        return None
    if dur <= 0:
        return None
    return dur


def score_delta(baseline_rows: list[dict], variant_rows: list[dict]) -> dict:
    def pairs(rows: list[dict]) -> set[tuple]:
        out = set()
        for r in rows:
            if "home" in r and "away" in r:
                out.add((r["home"], r["away"]))
        return out
    b, v = pairs(baseline_rows), pairs(variant_rows)
    wrong = sorted(v - b)
    missed = sorted(b - v)
    return {
        "wrong_reads": len(wrong),
        "wrong_pairs": [list(p) for p in wrong],
        "missed_scores": len(missed),
        "missed_pairs": [list(p) for p in missed],
        "baseline_locks": len(b),
        "variant_locks": len(v),
    }


def read_counts(row: dict) -> tuple[int, int] | None:
    """(agreed, attempted). Attempted is the frames the reader saw, else snapshots saved."""
    ok = row.get("reads_succeeded")
    if ok is None:
        return None
    attempted = row.get("reads_attempted")
    if attempted is None:
        attempted = row.get("snapshots_attempted")
    if attempted is None:
        attempted = row.get("snapshots_saved")
    if attempted is None:
        return None
    return int(ok), int(attempted)


def read_rate_margin(b_ok: int, b_n: int, v_n: int) -> float:
    """How far a variant rate may sit below the baseline and still be the same rate.

    Uses the two-proportion standard error at the baseline rate, and at least
    READ_RATE_ABS so a perfect 1.0 (zero standard error) still has a small floor.
    """
    if b_n <= 0 or v_n <= 0:
        return 1.0
    p = min(1.0, max(0.0, b_ok / b_n))
    se = 0.0
    if 0.0 < p < 1.0:
        se = math.sqrt(p * (1.0 - p) * (1.0 / b_n + 1.0 / v_n))
    return max(READ_RATE_ABS, READ_RATE_Z * se)


def snapshot_slot(elapsed: float, interval: float = SNAP_INTERVAL_S) -> int:
    """Index of the 4 Hz slot that `elapsed` seconds after the first frame falls in."""
    if elapsed < 0 or interval <= 0:
        return -1
    return int(elapsed / interval + 1e-9)


def anchored_snapshot_count(poll_s: float, window_s: float, interval: float = SNAP_INTERVAL_S) -> int:
    """Distinct slots a fixed poll hits in [0, window). The slot clock does not drift."""
    if poll_s <= 0 or window_s <= 0 or interval <= 0:
        return 0
    saved: set[int] = set()
    t = 0.0
    steps = 0
    while t < window_s and steps < 1_000_000:
        slot = snapshot_slot(t, interval)
        if slot >= 0 and slot * interval < window_s:
            saved.add(slot)
        t += poll_s
        steps += 1
    return len(saved)


def promotion_decision(baseline: dict, variant: dict) -> dict:
    """Return {promote, reasons, disk_raised, tie, benefits}.

    promote is true only when nothing is worse and at least one metric is
    measurably better. A tie stays off.
    """
    reasons: list[str] = []

    def both(key: str):
        if baseline.get(key) is None or variant.get(key) is None:
            reasons.append(f"{key} was not measured")
            return None
        return baseline[key], variant[key]

    if not baseline.get("capture_ok"):
        reasons.append("baseline capture did not produce frames")
    if not variant.get("capture_ok"):
        reasons.append("variant capture did not produce frames")
    drops = both("drop_frames")
    if drops and drops[1] > drops[0]:
        reasons.append(f"more dropped frames ({drops[1]} > {drops[0]})")
    dups = both("dup_frames")
    if dups and dups[1] > dups[0]:
        reasons.append(f"more duplicate frames ({dups[1]} > {dups[0]})")

    b_lag, v_lag = baseline.get("audio_lag_s"), variant.get("audio_lag_s")
    b_warn = int(baseline.get("buffer_warnings") or 0)
    v_warn = int(variant.get("buffer_warnings") or 0)
    if v_lag is None and b_lag is None:
        if v_warn > b_warn:
            reasons.append(f"more real-time buffer warnings ({v_warn} > {b_warn}) and no lag number")
    elif v_lag is not None and b_lag is None and v_lag > LAG_NOISE_S:
        reasons.append(f"audio lag {v_lag:.2f}s appeared; baseline had none")
    elif v_lag is not None and b_lag is not None and v_lag > b_lag + LAG_NOISE_S:
        reasons.append(f"audio lag {v_lag:.2f}s is worse than baseline {b_lag:.2f}s")
    b_q, v_q = baseline.get("video_queued_mb"), variant.get("video_queued_mb")
    if v_q is not None and v_q > (b_q or 0.0) + VIDEO_QUEUE_NOISE_MB:
        reasons.append(f"video queue {v_q:.1f} MB is above baseline {(b_q or 0):.1f} MB")

    b_cpu, v_cpu = baseline.get("cpu_pct"), variant.get("cpu_pct")
    if b_cpu is None or v_cpu is None or b_cpu <= 0 or v_cpu <= 0:
        reasons.append("cpu_pct was not measured")
    else:
        limit = b_cpu + max(CPU_ABS_POINTS, CPU_REL * b_cpu)
        if v_cpu > limit:
            reasons.append(f"CPU {v_cpu:.1f}% is meaningfully above baseline {b_cpu:.1f}% (limit {limit:.1f}%)")
    b_disk, v_disk = baseline.get("disk_mb_per_min"), variant.get("disk_mb_per_min")
    disk_raised = False
    if b_disk is None or v_disk is None or b_disk <= 0 or v_disk <= 0:
        reasons.append("disk_mb_per_min was not measured")
    else:
        limit = disk_limit(b_disk)
        if v_disk > limit:
            disk_raised = True
            slack = limit - b_disk
            reasons.append(
                f"disk {v_disk:.2f} MB/min is above baseline {b_disk:.2f} MB/min "
                f"(tolerance +{slack:.2f} MB/min from ffprobe duration)"
            )

    if not variant.get("same_footage"):
        reasons.append(variant.get("picture_reason") or "scorebug comparison is not on the same footage")
    b_fps, v_fps = baseline.get("delivered_fps"), variant.get("delivered_fps")
    if b_fps is not None and v_fps is not None and b_fps > 0:
        fps_slack = max(FPS_ABS, FPS_REL * b_fps)
        if v_fps + 1e-9 < b_fps - fps_slack:
            reasons.append(f"delivered fps {v_fps:.1f} is below baseline {b_fps:.1f} (tolerance {fps_slack:.1f})")
    b_media, v_media = baseline.get("media_duration_s"), variant.get("media_duration_s")
    if b_media is not None and v_media is not None and b_media > 0:
        media_slack = max(MEDIA_ABS_S, MEDIA_REL * b_media)
        if v_media + 1e-9 < b_media - media_slack:
            reasons.append(
                f"media duration {v_media:.1f}s is shorter than baseline {b_media:.1f}s (tolerance {media_slack:.1f}s)"
            )

    counts_b = read_counts(baseline)
    counts_v = read_counts(variant)
    if baseline.get("reads_succeeded") is None or variant.get("reads_succeeded") is None or counts_b is None or counts_v is None:
        reasons.append("scorebug reader did not run")
    else:
        b_ok, b_n = counts_b
        v_ok, v_n = counts_v
        if b_ok < MIN_SUCCESSFUL_READS or v_ok < MIN_SUCCESSFUL_READS or b_n < MIN_SUCCESSFUL_READS or v_n < MIN_SUCCESSFUL_READS:
            reasons.append(
                f"too few score reads to decide (baseline {b_ok}/{b_n}, variant {v_ok}/{v_n}; "
                f"need at least {MIN_SUCCESSFUL_READS} successful reads each)"
            )
        else:
            if int(baseline.get("baseline_locks") or 0) <= 0:
                reasons.append("baseline locked no scores; score quality is not proven")
            margin = read_rate_margin(b_ok, b_n, v_n)
            b_rate = b_ok / b_n
            v_rate = v_ok / v_n if v_n else 0.0
            if v_rate + 1e-12 < b_rate - margin:
                reasons.append(
                    f"read success rate {v_rate:.1%} ({v_ok}/{v_n}) is below baseline "
                    f"{b_rate:.1%} ({b_ok}/{b_n}); margin {margin:.1%}"
                )
            if int(variant.get("wrong_reads") or 0) > 0:
                reasons.append(f"new wrong score reads: {variant.get('wrong_pairs')}")
            if int(variant.get("missed_scores") or 0) > 0:
                reasons.append(f"missed scores the baseline locked: {variant.get('missed_pairs')}")
    seen = set()
    uniq = []
    for r in reasons:
        if r not in seen:
            seen.add(r)
            uniq.append(r)
    benefits = measurable_benefits(baseline, variant) if not uniq else []
    tie = False
    if not uniq and not benefits:
        tie = True
        uniq.append("tie: no metric is measurably better than the baseline")
    return {"promote": not uniq, "reasons": uniq, "disk_raised": disk_raised, "tie": tie, "benefits": benefits}


def measurable_benefits(baseline: dict, variant: dict) -> list[str]:
    """Improvements larger than the same noise floors the regression gates use."""
    benefits: list[str] = []
    b_drop, v_drop = baseline.get("drop_frames"), variant.get("drop_frames")
    if b_drop is not None and v_drop is not None and v_drop < b_drop:
        benefits.append(f"fewer dropped frames ({v_drop} < {b_drop})")
    b_dup, v_dup = baseline.get("dup_frames"), variant.get("dup_frames")
    if b_dup is not None and v_dup is not None and v_dup < b_dup:
        benefits.append(f"fewer duplicate frames ({v_dup} < {b_dup})")
    b_lag, v_lag = baseline.get("audio_lag_s"), variant.get("audio_lag_s")
    if b_lag is not None and v_lag is not None and b_lag - v_lag > LAG_NOISE_S:
        benefits.append(f"audio lag {v_lag:.2f}s is better than baseline {b_lag:.2f}s")
    b_q, v_q = baseline.get("video_queued_mb"), variant.get("video_queued_mb")
    if b_q is not None and v_q is not None and b_q - v_q > VIDEO_QUEUE_NOISE_MB:
        benefits.append(f"video queue {v_q:.1f} MB is below baseline {b_q:.1f} MB")
    b_cpu, v_cpu = baseline.get("cpu_pct"), variant.get("cpu_pct")
    if b_cpu is not None and v_cpu is not None and b_cpu > 0 and v_cpu > 0:
        slack = max(CPU_ABS_POINTS, CPU_REL * b_cpu)
        if b_cpu - v_cpu > slack:
            benefits.append(f"CPU {v_cpu:.1f}% is below baseline {b_cpu:.1f}%")
    b_disk, v_disk = baseline.get("disk_mb_per_min"), variant.get("disk_mb_per_min")
    if b_disk is not None and v_disk is not None and b_disk > 0 and v_disk > 0:
        slack = disk_limit(b_disk) - b_disk
        if b_disk - v_disk > slack:
            benefits.append(f"disk {v_disk:.2f} MB/min is below baseline {b_disk:.2f} MB/min")
    b_fps, v_fps = baseline.get("delivered_fps"), variant.get("delivered_fps")
    if b_fps is not None and v_fps is not None and b_fps > 0:
        slack = max(FPS_ABS, FPS_REL * b_fps)
        if v_fps > b_fps + slack:
            benefits.append(f"delivered fps {v_fps:.1f} is above baseline {b_fps:.1f}")
    b_media, v_media = baseline.get("media_duration_s"), variant.get("media_duration_s")
    if b_media is not None and v_media is not None and b_media > 0:
        slack = max(MEDIA_ABS_S, MEDIA_REL * b_media)
        if v_media > b_media + slack:
            benefits.append(f"media duration {v_media:.1f}s is longer than baseline {b_media:.1f}s")
    counts_b, counts_v = read_counts(baseline), read_counts(variant)
    if counts_b and counts_v:
        b_ok, b_n = counts_b
        v_ok, v_n = counts_v
        if b_ok >= MIN_SUCCESSFUL_READS and v_ok >= MIN_SUCCESSFUL_READS and b_n >= MIN_SUCCESSFUL_READS and v_n >= MIN_SUCCESSFUL_READS and b_n and v_n:
            margin = read_rate_margin(b_ok, b_n, v_n)
            b_rate, v_rate = b_ok / b_n, v_ok / v_n
            if v_rate > b_rate + margin:
                benefits.append(f"read success rate {v_rate:.1%} is above baseline {b_rate:.1%}")
    return benefits


def promotion_label(row: dict) -> str:
    """Baseline is 'ref'. A pass is 'YES'. Measurement gaps are 'undecided', not a promotion."""
    if row.get("role") == "reference":
        return "ref"
    if row.get("tie"):
        return "tie"
    if row.get("promote"):
        return "YES"
    reasons = row.get("reasons") or []
    if reasons and all(any(m in r for m in _UNDECIDED_MARKERS) for r in reasons):
        return "undecided"
    return "no"


def region_distance(a: bytes, b: bytes, width: int, tile: int = TILE_PX) -> tuple[float, float]:
    """Return (mean abs diff, max per-tile mean abs diff) for two equal gray strips."""
    if not a or len(a) != len(b) or width <= 0 or len(a) % width != 0:
        return (255.0, 255.0)
    height = len(a) // width
    total = 0
    max_tile = 0.0
    x = 0
    while x < width:
        x1 = min(width, x + tile)
        acc = 0
        n = 0
        for y in range(height):
            row = y * width
            for i in range(x, x1):
                acc += abs(a[row + i] - b[row + i])
                n += 1
        if n:
            max_tile = max(max_tile, acc / n)
        total += acc
        x = x1
    return (total / len(a), max_tile)


def median_strip(samples: list[tuple[int, bytes]]) -> tuple[int, bytes] | None:
    """Per-pixel median of equal-sized strips. This is the still picture, not frame 0."""
    if not samples:
        return None
    width, first = samples[0]
    strips = [s for w, s in samples if w == width and len(s) == len(first)]
    if not strips:
        return None
    if len(strips) == 1:
        return width, strips[0]
    length = len(strips[0])
    mid = len(strips) // 2
    out = bytearray(length)
    for i in range(length):
        vals = sorted(s[i] for s in strips)
        out[i] = vals[mid]
    return width, bytes(out)


def take_picture_stable(samples: list[tuple[int, bytes]]) -> dict:
    """True when the scorebug strip stays near its median for this take.

    Frame 0 is often still black or the previous scene. Those few frames do not
    fail the take. A change that shows up in more than CHANGED_FRACTION of the
    strips (a score that moves and stays moved) does.
    """
    empty = {
        "stable": None,
        "reason": "too few snapshots to check the scorebug region",
        "compared": len(samples),
        "worst_mean": None,
        "worst_tile": None,
        "changed_frames": 0,
    }
    if len(samples) < 2:
        return empty
    width, first = samples[0]
    usable = [(w, s) for w, s in samples if w == width and len(s) == len(first)]
    if len(usable) < 2:
        return {
            "stable": False,
            "reason": "scorebug region size changed during the take",
            "compared": len(samples),
            "worst_mean": None,
            "worst_tile": None,
            "changed_frames": len(samples),
        }
    ref = median_strip(usable)
    if ref is None:
        return empty
    worst_mean = 0.0
    worst_tile = 0.0
    changed = 0
    for _w, s in usable:
        mean, tile_m = region_distance(ref[1], s, width)
        worst_mean = max(worst_mean, mean)
        worst_tile = max(worst_tile, tile_m)
        if mean > MEAN_LIMIT or tile_m > TILE_LIMIT:
            changed += 1
    fraction = changed / len(usable)
    stable = fraction <= CHANGED_FRACTION
    reason = None if stable else (
        f"scorebug region changed during the take ({changed} of {len(usable)} frames)"
    )
    return {
        "stable": stable,
        "reason": reason,
        "compared": len(usable),
        "worst_mean": worst_mean,
        "worst_tile": worst_tile,
        "changed_frames": changed,
    }


def regions_match(a: tuple[int, bytes], b: tuple[int, bytes]) -> dict:
    w1, s1 = a
    w2, s2 = b
    if w1 != w2 or len(s1) != len(s2):
        return {"stable": False, "reason": "scorebug region size differs from the baseline take", "worst_mean": None, "worst_tile": None}
    mean, tile_m = region_distance(s1, s2, w1)
    if mean > MEAN_LIMIT or tile_m > TILE_LIMIT:
        return {
            "stable": False,
            "reason": f"scorebug region differs from the baseline take (mean {mean:.1f}, tile {tile_m:.1f})",
            "worst_mean": mean,
            "worst_tile": tile_m,
        }
    return {"stable": True, "reason": None, "worst_mean": mean, "worst_tile": tile_m}


def _find_qorcap(arg: str | None) -> str:
    here = Path(__file__).resolve().parent.parent
    exe = "qorcap.exe" if os.name == "nt" else "qorcap"
    cands = [arg, here / exe, here / "target" / "release" / exe, here / "target" / "debug" / exe, shutil.which("qorcap")]
    for c in cands:
        if c and Path(c).exists():
            return str(c)
    raise SystemExit("qorcap not found. Pass --qorcap path\\to\\qorcap.exe (build it with cargo build --release).")


def resolve_ffmpeg(arg: str | None, qorcap: str, *, use_path: bool = True) -> str:
    """ffmpeg.exe beside qorcap (the release bundle), then LOCALAPPDATA\\QorCap, then PATH.

    A --ffmpeg path that does not exist is an error. It is not replaced with another binary.
    """
    if arg:
        if Path(arg).exists():
            return arg
        raise SystemExit(
            f"--ffmpeg was set to {arg} but that file does not exist. "
            "Pass ffmpeg.exe from the QorCap release bundle (the folder that contains qorcap.exe)."
        )
    names = ("ffmpeg.exe", "ffmpeg")
    looked: list[str] = []
    roots = [Path(qorcap).resolve().parent]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(Path(local) / "QorCap")
    for root in roots:
        for name in names:
            cand = root / name
            looked.append(str(cand))
            if cand.exists():
                return str(cand)
    if use_path:
        found = shutil.which("ffmpeg")
        if found:
            return found
        looked.append("PATH")
    raise SystemExit(
        "ffmpeg was not found next to qorcap.exe (the release bundle) or in %LOCALAPPDATA%\\QorCap. "
        "Re-run with --ffmpeg path\\to\\ffmpeg.exe from that bundle. Looked at: " + "; ".join(looked)
    )


def resolve_ffprobe(ffmpeg: str) -> str | None:
    parent = Path(ffmpeg).resolve().parent
    for name in ("ffprobe.exe", "ffprobe"):
        cand = parent / name
        if cand.exists():
            return str(cand)
    return shutil.which("ffprobe")


def _find_qortime(arg: str | None, qorcap: str | None = None) -> Path | None:
    cands = [arg, os.getenv("QORTIME")]
    if qorcap:
        cands.append(str(Path(qorcap).resolve().parent / "qortime"))
    cands.append(str(Path(__file__).resolve().parent.parent / "qortime"))
    for c in cands:
        if c and (Path(c) / "qor_receipt" / "receipt.py").exists():
            return Path(c).resolve()
    return None


def _get_json(url: str, timeout: float = 1.0) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def _get_bytes(url: str, timeout: float = 1.0) -> bytes | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read()
    except Exception:
        return None


def _looks_like_image(blob: bytes) -> bool:
    """Complete JPEG (EOI) or complete PNG (IEND). A torn in-place write is not a frame."""
    if len(blob) >= 4 and blob[0] == 0xFF and blob[1] == 0xD8 and blob.endswith(b"\xff\xd9"):
        return True
    return (
        len(blob) >= 24
        and blob.startswith(b"\x89PNG\r\n\x1a\n")
        and blob.endswith(b"\x00\x00\x00\x00IEND\xaeB`\x82")
    )


def _tree_cpu_psutil(psutil, pid: int) -> float | None:
    try:
        root = psutil.Process(pid)
    except Exception:
        return None
    procs = [root]
    try:
        procs.extend(root.children(recursive=True))
    except Exception:
        pass
    total = 0.0
    seen = 0
    for proc in procs:
        try:
            times = proc.cpu_times()
            total += float(times.user) + float(times.system)
            seen += 1
        except Exception:
            continue
    if seen == 0:
        return None
    return total


def _tree_cpu_windows(root_pid: int) -> float | None:
    """Sum user+kernel seconds of root_pid and every descendant. Toolhelp + GetProcessTimes.

    psutil is used when it imports. This is the fallback so the QorCap venv does not
    need another package. It does not spawn PowerShell (that enumeration made the
    snapshot poll run a handful of times per take and report 0.0% CPU).
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    TH32CS_SNAPPROCESS = 0x00000002
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetProcessTimes.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = wintypes.BOOL

    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    invalid = ctypes.c_void_p(-1).value
    if not snap or snap == invalid:
        return None
    parents: dict[int, int] = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    want = {int(root_pid)}
    changed = True
    while changed:
        changed = False
        for pid, ppid in parents.items():
            if ppid in want and pid not in want:
                want.add(pid)
                changed = True

    def ft(value: FILETIME) -> int:
        return (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)

    total = 0
    seen = 0
    for pid in want:
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            continue
        try:
            created, exited, kernel, user = FILETIME(), FILETIME(), FILETIME(), FILETIME()
            if kernel32.GetProcessTimes(
                handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)
            ):
                total += ft(kernel) + ft(user)
                seen += 1
        finally:
            kernel32.CloseHandle(handle)
    if seen == 0:
        return None
    return total / 10_000_000.0


def _tree_cpu_proc(pid: int) -> float | None:
    if pid <= 0:
        return None
    ticks = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
    total = 0.0
    seen = False
    pids = [pid]
    try:
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                ppid = int(entry.joinpath("status").read_text().split("PPid:")[1].split()[0])
            except Exception:
                continue
            if ppid == pid:
                pids.append(int(entry.name))
    except Exception:
        pass
    for p in pids:
        try:
            stat = Path(f"/proc/{p}/stat").read_text()
            rest = stat.rsplit(")", 1)[-1].split()
            total += int(rest[11]) + int(rest[12])
            seen = True
        except Exception:
            continue
    if not seen:
        return None
    return total / float(ticks)


class TreeCpu:
    """CPU seconds of one process and its descendants, sampled on the caller's interval."""

    def __init__(self, pid: int):
        self.pid = pid
        self.samples: list[tuple[float, float]] = []
        self.failures = 0
        try:
            import psutil
            self._psutil = psutil
        except Exception:
            self._psutil = None

    def sample(self) -> None:
        secs = self._seconds()
        if secs is None:
            self.failures += 1
            return
        self.samples.append((time.monotonic(), float(secs)))

    def percent(self) -> float | None:
        return cpu_percent_from_samples(self.samples)

    def _seconds(self) -> float | None:
        if self._psutil is not None:
            return _tree_cpu_psutil(self._psutil, self.pid)
        if os.name == "nt":
            return _tree_cpu_windows(self.pid)
        return _tree_cpu_proc(self.pid)


def _save_snap(blob: bytes, dest: Path, n: int) -> Path:
    ext = ".png" if blob.startswith(b"\x89PNG") else ".jpg"
    path = dest / f"snap{n:04d}{ext}"
    path.write_bytes(blob)
    return path


def media_duration_s(ffprobe: str | None, path: Path) -> float | None:
    """Container duration. Wall-clock and /status recording_secs are not the media rate."""
    if not ffprobe or not path.exists() or path.stat().st_size <= 0:
        return None
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_duration(out.stdout or "")


def run_variant(name: str, extra: list[str], *, qorcap: str, ffmpeg: str | None, ffprobe: str | None,
                secs: int, device: str, root: Path, port: int) -> dict:
    d = root / name
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    snaps = d / "snaps"
    snaps.mkdir(exist_ok=True)
    out = d / "video.mp4"
    log = d / "qorcap.log"
    # --duration inside qorcap starts at process start, before the card opens. A 15s
    # budget on 2026-10-20 left only 46-51 of 60 snapshot slots. The backstop keeps
    # the process up; this loop stops `secs` after the first complete snapshot and
    # saves on a fixed 4 Hz grid so poll latency does not stretch the interval.
    backstop = secs + STARTUP_BACKSTOP_S
    cmd = record_command(qorcap, ffmpeg, port, str(log), str(out), backstop, extra, device)
    (d / "command.txt").write_text(" ".join(cmd), encoding="utf-8")
    proc = subprocess.Popen(cmd)
    t0 = time.monotonic()
    sampler = TreeCpu(proc.pid)
    sampler.sample()
    next_cpu = t0 + CPU_INTERVAL_S
    next_status = t0
    origin = None
    last_slot = -1
    last: dict = {}
    seen: set[bytes] = set()
    saved = 0
    deadline = t0 + backstop + 15
    while proc.poll() is None and time.monotonic() < deadline:
        now = time.monotonic()
        if origin is not None and now >= origin + secs:
            break
        due = origin is None or snapshot_slot(now - origin) > last_slot
        if due:
            blob = _get_bytes(f"http://127.0.0.1:{port}/snapshot.jpg", timeout=0.2)
            if blob and _looks_like_image(blob):
                digest = hashlib.sha256(blob).digest()
                if digest not in seen:
                    seen.add(digest)
                saved += 1
                _save_snap(blob, snaps, saved)
                if origin is None:
                    origin = time.monotonic()
                    last_slot = 0
                else:
                    last_slot = snapshot_slot(time.monotonic() - origin)
        now = time.monotonic()
        if now >= next_status:
            st = _get_json(f"http://127.0.0.1:{port}/status", timeout=0.25)
            if st:
                last = st
            next_status = now + STATUS_INTERVAL_S
        if now >= next_cpu:
            sampler.sample()
            next_cpu = now + CPU_INTERVAL_S
        if origin is None:
            time.sleep(SNAP_POLL_S)
        else:
            nxt = origin + (last_slot + 1) * SNAP_INTERVAL_S
            delay = min(SNAP_POLL_S, max(0.0, nxt - time.monotonic()))
            if delay:
                time.sleep(delay)
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    sampler.sample()
    wall = max(0.001, time.monotonic() - t0)
    nbytes = out.stat().st_size if out.exists() else 0
    duration = media_duration_s(ffprobe, out)
    log_text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
    buf = summarize_buffer(log_text)
    # /status counts only exist when --buffer-watch is on. The log count is the fair comparison.
    if last.get("buffer_lag_s") is not None:
        buf["audio_lag_s"] = max(buf["audio_lag_s"] or 0.0, float(last["buffer_lag_s"]))
    if last.get("buffer_video_queued_mb") is not None:
        buf["video_queued_mb"] = max(buf["video_queued_mb"] or 0.0, float(last["buffer_video_queued_mb"]))
    frames = int(last.get("frames") or 0)
    rec_secs = float(last.get("recording_secs") or 0.0) or wall
    expected = int(secs * SNAP_HZ)
    return {
        "name": name,
        "extra": extra,
        "exit_code": proc.returncode,
        "frames": frames,
        "fps_reported": last.get("fps_actual"),
        "delivered_fps": (frames / rec_secs) if rec_secs else None,
        "drop_frames": int(last.get("drop_frames") or 0) if last else None,
        "dup_frames": int(last.get("dup_frames") or 0) if last else None,
        "cpu_pct": sampler.percent(),
        "cpu_samples": len(sampler.samples),
        "cpu_sample_failures": sampler.failures,
        "disk_mb_per_min": disk_mb_per_min(nbytes, duration) if nbytes else None,
        "media_duration_s": duration,
        "recording_bytes": nbytes,
        "recording_secs": rec_secs,
        "wall_secs": wall,
        "snapshots_saved": saved,
        "snapshots_unique": len(seen),
        "snapshots_expected": expected,
        "snap_dir": str(snaps),
        "warning": last.get("warning"),
        "no_signal": bool(last.get("no_signal")),
        "usb_link": last.get("usb_link"),
        "capture_ok": frames > 0 and nbytes > 0,
        **buf,
    }


def _build_reader(qortime: Path, posts: list):
    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(qortime))
    sys.path.insert(0, str(root / "sidecar"))
    import cv2  # noqa: F401
    from qor_receipt._vendor.qoresence.vision.local_scorebug.agreement import AgreementWindow
    from qor_receipt.agent.pause_detect import PauseDetector
    from qor_receipt.final_detect import detect_final
    from qorcap_endmenu import EndMenuReader
    from qorcap_mnp import AutoProfileReader
    from qorcap_playcall import PauseCapableGate, PlayCallDetector
    from qorcap_reader import ReaderCore, madden_final_screen, prep
    from qorcap_standard import standard_reader
    # Same ReaderCore Start Match builds. Glimpse is not wired: it is observer-only.
    return ReaderCore(posts.append, AutoProfileReader(standard_reader(), prep=prep), AgreementWindow(),
                      PauseDetector(), detect_final, final_screen_detector=madden_final_screen,
                      end_menu_reader=EndMenuReader(), playcall=PlayCallDetector(), gate=PauseCapableGate())


def score_paths(paths: list[Path], qortime: Path | None) -> dict:
    if qortime is None:
        return {"error": "qortime not found (pass --qortime); score reads were not run",
                "reads_attempted": None, "reads_succeeded": None, "locks": []}
    posts: list = []
    try:
        core = _build_reader(qortime, posts)
        import cv2
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "reads_attempted": None, "reads_succeeded": None, "locks": []}
    for i, p in enumerate(paths):
        img = cv2.imread(str(p), cv2.IMREAD_COLOR)
        if img is None:
            continue
        try:
            core.on_frame(i * 250, img)
        except Exception:
            core.stats["errors"] = core.stats.get("errors", 0) + 1
    rows = [e for e in posts if e.get("type") == "score"]
    return {
        "reads_attempted": int(core.stats.get("frames") or 0),
        "reads_succeeded": int(core.stats.get("agreed") or 0),
        "locks": [{"home": r.get("home"), "away": r.get("away"), "quarter": r.get("quarter"), "t_ms": r.get("t_ms")} for r in rows],
        "error": None,
    }


def extract_mode(ffmpeg: str, video: Path, dest: Path, mode: str) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    ext = "png" if mode == "png" else "jpg"
    pattern = str(dest / f"f%04d.{ext}")
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-i", str(video), "-vf", "fps=4",
           *SAME_FOOTAGE[mode], pattern]
    subprocess.run(cmd, check=False)
    return sorted(dest.glob(f"f*.{ext}"))


def extract_scorebug_gray(path: Path, ffmpeg: str | None) -> tuple[int, bytes] | None:
    """Gray scorebug strip. cv2 when the venv has it, otherwise one ffmpeg rawvideo crop."""
    y0, y1, x0, x1 = SCOREBUG_BOX
    width, height = x1 - x0, y1 - y0
    try:
        import cv2
        img = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if img is not None:
            if img.shape[1] != 1280 or img.shape[0] != 720:
                img = cv2.resize(img, (1280, 720))
            crop = img[y0:y1, x0:x1]
            gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
            return int(gray.shape[1]), gray.tobytes()
    except Exception:
        pass
    if not ffmpeg:
        return None
    vf = f"scale=1280:720,crop={width}:{height}:{x0}:{y0}"
    try:
        out = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(path), "-frames:v", "1",
             "-vf", vf, "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"],
            capture_output=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    raw = out.stdout or b""
    if len(raw) != width * height:
        return None
    return width, raw


def assess_snapshots(paths: list[Path], ffmpeg: str | None) -> tuple[dict, tuple[int, bytes] | None]:
    samples: list[tuple[int, bytes]] = []
    for path in paths:
        gray = extract_scorebug_gray(path, ffmpeg)
        if gray is not None:
            samples.append(gray)
    verdict = take_picture_stable(samples)
    ref = median_strip(samples)
    return verdict, ref


def _apply_picture(live: dict, verdict: dict, *, retried: bool) -> None:
    live["picture_stable"] = verdict["stable"]
    live["picture_reason"] = verdict["reason"]
    live["picture_compared"] = verdict["compared"]
    live["picture_worst_mean"] = verdict["worst_mean"]
    live["picture_worst_tile"] = verdict["worst_tile"]
    live["picture_retried"] = retried


def _live_same(name: str, live: dict, stable_flag: bool) -> tuple[bool, str | None]:
    if name in ("reader_hq", "reader_png"):
        return True, None
    if not stable_flag:
        return False, None
    if live.get("picture_stable") is not True:
        if live.get("picture_stable") is False:
            return False, live.get("picture_reason") or "scorebug region changed during the take"
        return False, "stable-picture could not be verified (scorebug region was not decoded)"
    if live.get("picture_matches_baseline") is not True:
        return False, live.get("picture_cross_reason") or "stable-picture could not be verified against the baseline take"
    return True, None


def _metric_row(live: dict, scores: dict, same: bool, delta: dict | None, picture_reason: str | None = None) -> dict:
    row = {
        "capture_ok": live.get("capture_ok"),
        "drop_frames": live.get("drop_frames"),
        "dup_frames": live.get("dup_frames"),
        "audio_lag_s": live.get("audio_lag_s"),
        "video_queued_mb": live.get("video_queued_mb"),
        "buffer_warnings": live.get("buffer_warnings"),
        "cpu_pct": live.get("cpu_pct"),
        "disk_mb_per_min": live.get("disk_mb_per_min"),
        "same_footage": same,
        "picture_reason": picture_reason,
        "reads_attempted": scores.get("reads_attempted"),
        "reads_succeeded": scores.get("reads_succeeded"),
        "snapshots_attempted": scores.get("reads_attempted") if scores.get("reads_attempted") is not None else live.get("snapshots_saved"),
        "delivered_fps": live.get("delivered_fps"),
        "media_duration_s": live.get("media_duration_s"),
        "wrong_reads": None if delta is None else delta["wrong_reads"],
        "wrong_pairs": None if delta is None else delta["wrong_pairs"],
        "missed_scores": None if delta is None else delta["missed_scores"],
        "missed_pairs": None if delta is None else delta["missed_pairs"],
        "baseline_locks": None if delta is None else delta["baseline_locks"],
    }
    return row


def _fmt(v, spec=""):
    if v is None:
        return "-"
    if spec:
        return format(v, spec)
    return str(v)


def _print_report(report: dict) -> None:
    print()
    print("QorCap capture A/B")
    print("promotion: zero new wrong scores, no missed scores, read rate not worse than sampling noise")
    print(f"(at least {MIN_SUCCESSFUL_READS} agreed frames each, otherwise undecided), no more drops,")
    print("duplicates, lag, queue, or CPU, disk within tolerance, and strictly better on at least one metric.")
    print("A tie is not a promotion. The baseline row is the reference. Flags stay off until a variant prints YES.")
    print(YUY2_NOT_RECOMMENDED)
    print()
    hdr = f"{'variant':<20} {'fps':>6} {'drop':>5} {'dup':>5} {'lag_s':>6} {'cpu%':>7} {'MB/min':>8} {'snaps':>5} {'reads':>7} {'wrong':>5} {'promote':>9}"
    print(hdr)
    print("-" * len(hdr))
    for name, row in report["promotion"].items():
        live = report["variants"].get(name, {})
        label = promotion_label(row)
        print(f"{name:<20} {_fmt(live.get('delivered_fps'), '.1f'):>6} {_fmt(live.get('drop_frames')):>5} "
              f"{_fmt(live.get('dup_frames')):>5} {_fmt(live.get('audio_lag_s'), '.1f'):>6} "
              f"{_fmt(live.get('cpu_pct'), '.1f'):>7} {_fmt(live.get('disk_mb_per_min'), '.2f'):>8} "
              f"{_fmt(live.get('snapshots_saved')):>5} "
              f"{_fmt(row.get('reads_succeeded')):>7} {_fmt(row.get('wrong_reads')):>5} "
              f"{label:>9}")
        if label == "YES":
            for benefit in row.get("benefits") or []:
                print(f"    + {benefit}")
        elif label not in ("ref",):
            for reason in row["reasons"]:
                print(f"    - {reason}")
    if report.get("scorebug_error"):
        print()
        print("scorebug: " + report["scorebug_error"])
    print()
    print("full report:", report["report_path"])


def _cv2_hint() -> str | None:
    try:
        import cv2  # noqa: F401
    except Exception as e:
        return (
            f"cv2 is not importable ({type(e).__name__}: {e}). "
            f"Plain py -3 does not have it. Re-run with {venv_python_hint()} "
            "or score reads stay unmeasured and nothing can be promoted on score quality."
        )
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Record a short capture A/B and apply the promotion rule.")
    ap.add_argument("--qorcap")
    ap.add_argument("--ffmpeg", help="ffmpeg.exe. Default: the copy beside qorcap.exe in the release bundle")
    ap.add_argument("--qortime", help="qortime checkout (the scorebug reader). Not downloaded.")
    ap.add_argument("--device", default="USB3.0 Video")
    ap.add_argument("--secs", type=int, default=15)
    ap.add_argument("--out", help="directory for the takes (default: a temp folder)")
    ap.add_argument("--stable-picture", action="store_true",
                    help="verify the scorebug bar stays stable during each take and matches the baseline take")
    ap.add_argument("--variants", help="comma list; baseline is always included. Default: all")
    ap.add_argument("--port", type=int, default=8791)
    args = ap.parse_args(argv)
    if os.name != "nt":
        print("This harness records from a DirectShow capture card. Run it on the Windows laptop.", file=sys.stderr)
        print(f"Use {venv_python_hint()} (plain py -3 has no cv2).", file=sys.stderr)
        print("The promotion rule is covered by: python -m pytest -q tools/test_capture_ab.py", file=sys.stderr)
        return 2
    hint = _cv2_hint()
    if hint:
        print(hint, file=sys.stderr)
    qorcap = _find_qorcap(args.qorcap)
    try:
        ffmpeg = resolve_ffmpeg(args.ffmpeg, qorcap)
    except SystemExit as e:
        print(str(e), file=sys.stderr)
        return 2
    ffprobe = resolve_ffprobe(ffmpeg)
    if ffprobe is None:
        print("ffprobe was not found beside ffmpeg. Disk MB/min will be unmeasured and cannot pass.", file=sys.stderr)
    qortime = _find_qortime(args.qortime, qorcap)
    root = Path(args.out) if args.out else Path(os.environ.get("TEMP", ".")) / "qorcap-capture-ab"
    root.mkdir(parents=True, exist_ok=True)
    free_gb = shutil.disk_usage(root).free / (1024 ** 3)
    if free_gb < 1.5:
        print(f"only {free_gb:.2f} GB free under {root}. Free space before an A/B (the laptop was ~6.4 GB).", file=sys.stderr)
        return 2
    wanted = {v.strip() for v in args.variants.split(",")} if args.variants else None
    plan = [("baseline", [])] + [v for v in VARIANTS if v[0] != "baseline" and (wanted is None or v[0] in wanted)]
    print(f"recording {len(plan)} variants, {args.secs}s each, into {root}")
    print(f"free {free_gb:.2f} GB. ffmpeg: {ffmpeg}")
    print("Pause on a readable scorebug. --stable-picture checks that the scorebug bar does not change.")
    lives = {}
    refs: dict[str, tuple[int, bytes] | None] = {}
    for i, (name, extra) in enumerate(plan):
        print(f"  [{i + 1}/{len(plan)}] {name} {' '.join(extra)}".rstrip())
        live = run_variant(name, extra, qorcap=qorcap, ffmpeg=ffmpeg, ffprobe=ffprobe, secs=args.secs,
                           device=args.device, root=root, port=args.port + i)
        paths = sorted((root / name / "snaps").glob("snap*"))
        verdict, ref = assess_snapshots(paths, ffmpeg)
        retried = False
        if args.stable_picture and verdict["stable"] is False:
            print(f"     {verdict['reason']}; retrying this take once")
            live = run_variant(name, extra, qorcap=qorcap, ffmpeg=ffmpeg, ffprobe=ffprobe, secs=args.secs,
                               device=args.device, root=root, port=args.port + i)
            paths = sorted((root / name / "snaps").glob("snap*"))
            verdict, ref = assess_snapshots(paths, ffmpeg)
            retried = True
        _apply_picture(live, verdict, retried=retried)
        refs[name] = ref
        lives[name] = live
        print(f"     frames={live['frames']} fps={live['delivered_fps']} drops={live['drop_frames']} "
              f"snaps={live['snapshots_saved']}/{live['snapshots_expected']} unique={live['snapshots_unique']} "
              f"cpu={live['cpu_pct']} samples={live['cpu_samples']} disk={live['disk_mb_per_min']} "
              f"media_s={live['media_duration_s']}")
    base_ref = refs.get("baseline")
    base_stable = (lives.get("baseline") or {}).get("picture_stable")
    for name, live in lives.items():
        if name == "baseline":
            live["picture_matches_baseline"] = True
            live["picture_cross_reason"] = None
            continue
        if name in ("reader_hq", "reader_png"):
            live["picture_matches_baseline"] = None
            live["picture_cross_reason"] = None
            continue
        ref = refs.get(name)
        if base_stable is False:
            live["picture_matches_baseline"] = False
            live["picture_cross_reason"] = "baseline scorebug region changed during the reference take"
        elif base_ref is None or ref is None:
            live["picture_matches_baseline"] = None
            live["picture_cross_reason"] = "stable-picture could not be verified against the baseline take"
        else:
            match = regions_match(base_ref, ref)
            live["picture_matches_baseline"] = match["stable"]
            live["picture_cross_reason"] = match["reason"]
            live["picture_cross_mean"] = match.get("worst_mean")
            live["picture_cross_tile"] = match.get("worst_tile")
    score_err = hint
    same_scores = {}
    base_video = root / "baseline" / "video.mp4"
    if qortime is None:
        score_err = "qortime not found; pass --qortime or use the bundle folder that contains qortime\\. Score reads were not run, so nothing can be promoted."
    elif hint:
        score_err = hint
    elif ffmpeg and base_video.exists() and base_video.stat().st_size > 0:
        for mode in ("q3", "hq", "png"):
            frames = extract_mode(ffmpeg, base_video, root / "same-footage" / mode, mode)
            same_scores[mode] = score_paths(frames, qortime)
            if same_scores[mode].get("error") and score_err is None:
                score_err = same_scores[mode]["error"]
    else:
        score_err = "baseline recording missing; same-footage score reads were not run"
    live_scores = {}
    if qortime is not None and hint is None:
        for name, live in lives.items():
            paths = sorted(Path(live["snap_dir"]).glob("snap*"))
            live_scores[name] = score_paths(paths, qortime)
            if live_scores[name].get("error") and score_err is None:
                score_err = live_scores[name]["error"]
    base_live = lives.get("baseline") or {}
    base_same = same_scores.get("q3") or {"locks": [], "reads_attempted": None, "reads_succeeded": None}
    promotion = {}
    for name, _extra in plan:
        live = lives[name]
        if name == "baseline":
            scores = live_scores.get(name) or {"reads_attempted": None, "reads_succeeded": None, "locks": []}
            promotion[name] = {
                "promote": False,
                "role": "reference",
                "reasons": [],
                "disk_raised": False,
                "reads_succeeded": scores.get("reads_succeeded"),
                "wrong_reads": 0,
            }
            continue
        if name in ("reader_hq", "reader_png"):
            mode = "hq" if name == "reader_hq" else "png"
            scores = same_scores.get(mode) or {"reads_attempted": None, "reads_succeeded": None, "locks": []}
            base_scores = base_same
            delta = score_delta(base_scores.get("locks") or [], scores.get("locks") or [])
            metric_base = dict(base_live)
            metric_base["same_footage"] = True
            metric_base["reads_succeeded"] = base_scores.get("reads_succeeded")
            metric_base["reads_attempted"] = base_scores.get("reads_attempted")
            metric_base["snapshots_attempted"] = base_scores.get("reads_attempted")
            metric_base["baseline_locks"] = delta["baseline_locks"]
            metric_base["wrong_reads"] = 0
            metric_base["missed_scores"] = 0
            metric_base["capture_ok"] = base_live.get("capture_ok")
            metric_var = dict(live)
            metric_var.update({
                "same_footage": True,
                "picture_reason": None,
                "reads_attempted": scores.get("reads_attempted"),
                "reads_succeeded": scores.get("reads_succeeded"),
                "snapshots_attempted": scores.get("reads_attempted"),
                **{k: delta[k] for k in ("wrong_reads", "wrong_pairs", "missed_scores", "missed_pairs")},
                "baseline_locks": delta["baseline_locks"],
            })
            promotion[name] = promotion_decision(metric_base, metric_var)
            promotion[name]["reads_succeeded"] = scores.get("reads_succeeded")
            promotion[name]["wrong_reads"] = delta["wrong_reads"]
        else:
            scores = live_scores.get(name) or {"reads_attempted": None, "reads_succeeded": None, "locks": []}
            base_scores = live_scores.get("baseline") or {"locks": [], "reads_attempted": None, "reads_succeeded": None}
            same, picture_reason = _live_same(name, live, bool(args.stable_picture))
            delta = score_delta(base_scores.get("locks") or [], scores.get("locks") or [])
            base_delta = score_delta(base_scores.get("locks") or [], base_scores.get("locks") or [])
            base_row = _metric_row(base_live, base_scores, True, base_delta)
            var_row = _metric_row(live, scores, same, delta, picture_reason)
            promotion[name] = promotion_decision(base_row, var_row)
            promotion[name]["reads_succeeded"] = scores.get("reads_succeeded")
            promotion[name]["wrong_reads"] = delta["wrong_reads"]
    report = {
        "promotion_rule": (
            "A flag may become the default only with zero new wrong score reads, no missed baseline "
            "scores, a read success rate inside the sampling margin of the baseline (at least 8 "
            "successful reads on each side, otherwise undecided), a baseline that locked at least one "
            "score, the same footage (--stable-picture must show a stable scorebug bar), no more drops "
            "or duplicate frames, audio lag no worse than +0.5s, video queue no worse than +1 MB, "
            "delivered fps and media duration not meaningfully shorter, CPU not above baseline by more "
            "than max(8 points, 25% relative) with a missing or zero CPU sample counting as not measured, "
            "and disk MB/min (ffprobe media duration) not above baseline by more than max(4 MB/min, 10% "
            "of baseline). A tie is not a promotion: some metric must be measurably better and none worse. "
            "The baseline is a reference. Otherwise the flag stays off. No flag was promoted on the "
            "2026-10-20 MS2131 runs. --yuy2-720p60 is not recommended on that card. Capture CPU was about 3-5%."
        ),
        "yuy2_720p60_note": YUY2_NOT_RECOMMENDED,
        "variants": lives,
        "same_footage": same_scores,
        "live_scores": {k: {kk: vv for kk, vv in v.items() if kk != "locks"} | {"locks": v.get("locks")} for k, v in live_scores.items()},
        "promotion": promotion,
        "scorebug_error": score_err,
        "stable_picture": bool(args.stable_picture),
        "free_gb": free_gb,
        "ffmpeg": ffmpeg,
        "ffprobe": ffprobe,
    }
    path = root / "capture-ab-report.json"
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    report["report_path"] = str(path)
    _print_report(report)
    if not base_live.get("capture_ok"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
