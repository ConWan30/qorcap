#!/usr/bin/env python3
"""QorCap capture A/B. Run this on the Windows laptop that has the capture card.

Today's capture (MJPEG 1280x720@30, 512M video rtbuf, JPEG q:v 3 snapshot) is the
baseline. Each other variant turns on one opt-in flag. Nothing here changes
scoring, receipts, or signing. A flag stays off unless this report says it may
be promoted.

    py -3 tools\\capture_ab.py --secs 15 --stable-picture --qortime C:\\path\\to\\qortime

Pause Madden on a readable scorebug for the whole run and pass --stable-picture
so live takes can be compared. Without that, only the same-footage snapshot
re-encode (q3 vs hq vs png of the baseline recording) is allowed to speak about
score reads. YUY2 vs MJPEG is a different photon stream unless the picture is held.

Promotion rule
--------------
A flag may become the default only when, against this baseline:

* zero new wrong score reads (no locked home/away pair the baseline did not lock)
* no missed locked scores (every baseline home/away pair was locked again)
* successful reads (agreed frames) are not fewer
* the baseline actually locked at least one score (otherwise score quality is not proven)
* the comparison is on the same footage (same-footage re-encode, or live takes with --stable-picture)
* dropped frames are not higher, and duplicate frames are not higher
* audio lag is not worse (variant <= baseline + 0.5s; a new lag above 0.5s fails)
* video real-time queue is not higher by more than 1 MB
* CPU of qorcap+ffmpeg is not meaningfully higher: not above baseline by more than
  max(8 percentage points, 25% of the baseline CPU). CPU is the sum of one-core
  percents, so two busy cores read as 200%.
* disk MB/min of the recording is not higher by more than 0.05 MB/min
  (the noise floor). Anything above that is flagged. The laptop this was sized
  for had about 6.4 GB free.

If a number was not measured, the flag is not promoted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
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
DISK_NOISE_MB_PER_MIN = 0.05
LAG_NOISE_S = 0.5
VIDEO_QUEUE_NOISE_MB = 1.0
AUDIO_BYTES_PER_SEC = 48_000 * 2 * 2  # 16-bit stereo 48 kHz, the card's native format

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
    if cpu_seconds is None or wall_seconds is None or wall_seconds <= 0:
        return None
    return 100.0 * cpu_seconds / wall_seconds


def disk_mb_per_min(nbytes: int | None, seconds: float | None) -> float | None:
    if nbytes is None or seconds is None or seconds <= 0:
        return None
    return (nbytes / (1024 * 1024)) / (seconds / 60.0)


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


def promotion_decision(baseline: dict, variant: dict) -> dict:
    """Return {promote, reasons, disk_raised}. promote is true only when every gate passes."""
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

    cpu = both("cpu_pct")
    if cpu:
        limit = cpu[0] + max(CPU_ABS_POINTS, CPU_REL * cpu[0])
        if cpu[1] > limit:
            reasons.append(f"CPU {cpu[1]:.1f}% is meaningfully above baseline {cpu[0]:.1f}% (limit {limit:.1f}%)")
    disk = both("disk_mb_per_min")
    disk_raised = False
    if disk and disk[1] > disk[0] + DISK_NOISE_MB_PER_MIN:
        disk_raised = True
        reasons.append(
            f"disk {disk[1]:.2f} MB/min is above baseline {disk[0]:.2f} MB/min "
            f"(laptop free space is tight; anything over +{DISK_NOISE_MB_PER_MIN} MB/min is flagged)"
        )

    if not variant.get("same_footage"):
        reasons.append("scorebug comparison is not on the same footage")
    if baseline.get("reads_succeeded") is None or variant.get("reads_succeeded") is None:
        reasons.append("scorebug reader did not run")
    else:
        if int(baseline.get("baseline_locks") or 0) <= 0 or int(baseline.get("reads_succeeded") or 0) <= 0:
            reasons.append("baseline locked no scores; score quality is not proven")
        if variant["reads_succeeded"] < baseline["reads_succeeded"]:
            reasons.append(f"fewer successful reads ({variant['reads_succeeded']} < {baseline['reads_succeeded']})")
        if int(variant.get("wrong_reads") or 0) > 0:
            reasons.append(f"new wrong score reads: {variant.get('wrong_pairs')}")
        if int(variant.get("missed_scores") or 0) > 0:
            reasons.append(f"missed scores the baseline locked: {variant.get('missed_pairs')}")
    # de-duplicate while keeping order
    seen = set()
    uniq = []
    for r in reasons:
        if r not in seen:
            seen.add(r)
            uniq.append(r)
    return {"promote": not uniq, "reasons": uniq, "disk_raised": disk_raised}


def _find_qorcap(arg: str | None) -> str:
    here = Path(__file__).resolve().parent.parent
    exe = "qorcap.exe" if os.name == "nt" else "qorcap"
    cands = [arg, here / exe, here / "target" / "release" / exe, here / "target" / "debug" / exe, shutil.which("qorcap")]
    for c in cands:
        if c and Path(c).exists():
            return str(c)
    raise SystemExit("qorcap not found. Pass --qorcap path\\to\\qorcap.exe (build it with cargo build --release).")


def _find_ffmpeg(arg: str | None, qorcap: str) -> str | None:
    if arg and Path(arg).exists():
        return arg
    beside = Path(qorcap).resolve().parent / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    if beside.exists():
        return str(beside)
    return shutil.which("ffmpeg")


def _find_qortime(arg: str | None) -> Path | None:
    cands = [arg, os.getenv("QORTIME"), Path(__file__).resolve().parent.parent / "qortime"]
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


def _cpu_seconds(pid: int) -> float | None:
    if pid <= 0:
        return None
    if os.name == "nt":
        script = (
            f"$ids = @({pid}); "
            f"Get-CimInstance Win32_Process | Where-Object {{ $_.ParentProcessId -eq {pid} }} | "
            "ForEach-Object { $ids += $_.ProcessId }; "
            "$sum = 0.0; foreach ($id in $ids) { "
            "$p = Get-Process -Id $id -ErrorAction SilentlyContinue; "
            "if ($p -and $null -ne $p.CPU) { $sum += [double]$p.CPU } }; Write-Output $sum"
        )
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                                 capture_output=True, text=True, timeout=8)
            return float((out.stdout or "").strip().splitlines()[-1])
        except Exception:
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
            total += int(rest[11]) + int(rest[12])  # utime, stime after comm
            seen = True
        except Exception:
            continue
    if not seen:
        return None
    return total / float(ticks)


def _save_snap(blob: bytes, dest: Path, n: int) -> Path:
    ext = ".png" if blob.startswith(b"\x89PNG") else ".jpg"
    path = dest / f"snap{n:04d}{ext}"
    path.write_bytes(blob)
    return path


def run_variant(name: str, extra: list[str], *, qorcap: str, ffmpeg: str | None, secs: int,
                device: str, root: Path, port: int) -> dict:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    snaps = d / "snaps"
    snaps.mkdir(exist_ok=True)
    out = d / "video.mp4"
    log = d / "qorcap.log"
    cmd = record_command(qorcap, ffmpeg, port, str(log), str(out), secs, extra, device)
    (d / "command.txt").write_text(" ".join(cmd), encoding="utf-8")
    proc = subprocess.Popen(cmd)
    t0 = time.monotonic()
    cpu0 = _cpu_seconds(proc.pid)
    last_cpu = cpu0
    last: dict = {}
    seen: set[bytes] = set()
    saved = 0
    deadline = t0 + secs + 40
    while proc.poll() is None and time.monotonic() < deadline:
        st = _get_json(f"http://127.0.0.1:{port}/status")
        if st:
            last = st
        blob = _get_bytes(f"http://127.0.0.1:{port}/snapshot.jpg")
        if blob and len(blob) > 8:
            h = hashlib.sha256(blob).digest()
            if h not in seen:
                seen.add(h)
                saved += 1
                _save_snap(blob, snaps, saved)
        sample = _cpu_seconds(proc.pid)
        if sample is not None:
            last_cpu = sample
        time.sleep(0.25)
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    wall = max(0.001, time.monotonic() - t0)
    cpu = None
    if cpu0 is not None and last_cpu is not None:
        cpu = cpu_percent(max(0.0, last_cpu - cpu0), wall)
    nbytes = out.stat().st_size if out.exists() else 0
    log_text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
    buf = summarize_buffer(log_text)
    # /status counts only exist when --buffer-watch is on. The log count is the fair comparison.
    if last.get("buffer_lag_s") is not None:
        buf["audio_lag_s"] = max(buf["audio_lag_s"] or 0.0, float(last["buffer_lag_s"]))
    if last.get("buffer_video_queued_mb") is not None:
        buf["video_queued_mb"] = max(buf["video_queued_mb"] or 0.0, float(last["buffer_video_queued_mb"]))
    frames = int(last.get("frames") or 0)
    rec_secs = float(last.get("recording_secs") or 0.0) or wall
    return {
        "name": name,
        "extra": extra,
        "exit_code": proc.returncode,
        "frames": frames,
        "fps_reported": last.get("fps_actual"),
        "delivered_fps": (frames / rec_secs) if rec_secs else None,
        "drop_frames": int(last.get("drop_frames") or 0) if last else None,
        "dup_frames": int(last.get("dup_frames") or 0) if last else None,
        "cpu_pct": cpu,
        "disk_mb_per_min": disk_mb_per_min(nbytes, rec_secs) if nbytes else None,
        "recording_bytes": nbytes,
        "recording_secs": rec_secs,
        "wall_secs": wall,
        "snapshots_saved": saved,
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


def _metric_row(live: dict, scores: dict, same: bool, delta: dict | None) -> dict:
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
        "reads_attempted": scores.get("reads_attempted"),
        "reads_succeeded": scores.get("reads_succeeded"),
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
    print("promotion: zero new wrong scores, no missed scores, no fewer successful reads,")
    print("no more drops or duplicates, lag no worse, CPU not meaningfully higher,")
    print(f"disk not above +{DISK_NOISE_MB_PER_MIN} MB/min. Flags stay off until that passes.")
    print()
    hdr = f"{'variant':<20} {'fps':>6} {'drop':>5} {'dup':>5} {'lag_s':>6} {'cpu%':>7} {'MB/min':>8} {'reads':>7} {'wrong':>5} {'promote':>8}"
    print(hdr)
    print("-" * len(hdr))
    for name, row in report["promotion"].items():
        live = report["variants"].get(name, {})
        print(f"{name:<20} {_fmt(live.get('delivered_fps'), '.1f'):>6} {_fmt(live.get('drop_frames')):>5} "
              f"{_fmt(live.get('dup_frames')):>5} {_fmt(live.get('audio_lag_s'), '.1f'):>6} "
              f"{_fmt(live.get('cpu_pct'), '.1f'):>7} {_fmt(live.get('disk_mb_per_min'), '.2f'):>8} "
              f"{_fmt(row.get('reads_succeeded')):>7} {_fmt(row.get('wrong_reads')):>5} "
              f"{'YES' if row['promote'] else 'no':>8}")
        if not row["promote"]:
            for reason in row["reasons"]:
                print(f"    - {reason}")
    if report.get("scorebug_error"):
        print()
        print("scorebug: " + report["scorebug_error"])
    print()
    print("full report:", report["report_path"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Record a short capture A/B and apply the promotion rule.")
    ap.add_argument("--qorcap")
    ap.add_argument("--ffmpeg")
    ap.add_argument("--qortime", help="qortime checkout (the scorebug reader). Not downloaded.")
    ap.add_argument("--device", default="USB3.0 Video")
    ap.add_argument("--secs", type=int, default=15)
    ap.add_argument("--out", help="directory for the takes (default: a temp folder)")
    ap.add_argument("--stable-picture", action="store_true",
                    help="the scorebug was paused/static, so live takes are the same footage")
    ap.add_argument("--variants", help="comma list; baseline is always included. Default: all")
    ap.add_argument("--port", type=int, default=8791)
    args = ap.parse_args(argv)
    if os.name != "nt":
        print("This harness records from a DirectShow capture card. Run it on the Windows laptop.", file=sys.stderr)
        print("The promotion rule is covered by: python -m pytest -q tools/test_capture_ab.py", file=sys.stderr)
        return 2
    qorcap = _find_qorcap(args.qorcap)
    ffmpeg = _find_ffmpeg(args.ffmpeg, qorcap)
    if not ffmpeg:
        print("ffmpeg not found. Pass --ffmpeg path\\to\\ffmpeg.exe", file=sys.stderr)
        return 2
    qortime = _find_qortime(args.qortime)
    root = Path(args.out) if args.out else Path(os.environ.get("TEMP", ".")) / "qorcap-capture-ab"
    root.mkdir(parents=True, exist_ok=True)
    free_gb = shutil.disk_usage(root).free / (1024 ** 3)
    if free_gb < 1.5:
        print(f"only {free_gb:.2f} GB free under {root}. Free space before an A/B (the laptop was ~6.4 GB).", file=sys.stderr)
        return 2
    wanted = {v.strip() for v in args.variants.split(",")} if args.variants else None
    plan = [("baseline", [])] + [v for v in VARIANTS if v[0] != "baseline" and (wanted is None or v[0] in wanted)]
    print(f"recording {len(plan)} variants, {args.secs}s each, into {root}")
    print(f"free {free_gb:.2f} GB. Pause on a readable scorebug and pass --stable-picture for live score comparison.")
    lives = {}
    for i, (name, extra) in enumerate(plan):
        print(f"  [{i + 1}/{len(plan)}] {name} {' '.join(extra)}".rstrip())
        lives[name] = run_variant(name, extra, qorcap=qorcap, ffmpeg=ffmpeg, secs=args.secs,
                                  device=args.device, root=root, port=args.port + i)
        print(f"     frames={lives[name]['frames']} fps={lives[name]['delivered_fps']} "
              f"drops={lives[name]['drop_frames']} disk={lives[name]['disk_mb_per_min']}")
    score_err = None
    same_scores = {}
    base_video = root / "baseline" / "video.mp4"
    if qortime is None:
        score_err = "qortime not found; pass --qortime. Score reads were not run, so nothing can be promoted."
    elif ffmpeg and base_video.exists() and base_video.stat().st_size > 0:
        for mode in ("q3", "hq", "png"):
            frames = extract_mode(ffmpeg, base_video, root / "same-footage" / mode, mode)
            same_scores[mode] = score_paths(frames, qortime)
            if same_scores[mode].get("error") and score_err is None:
                score_err = same_scores[mode]["error"]
    else:
        score_err = "baseline recording missing; same-footage score reads were not run"
    live_scores = {}
    if qortime is not None:
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
        if name in ("reader_hq", "reader_png"):
            mode = "hq" if name == "reader_hq" else "png"
            scores = same_scores.get(mode) or {"reads_attempted": None, "reads_succeeded": None, "locks": []}
            base_scores = base_same
            same = True
            # Live capture metrics still come from the flagged recording. Scores come from one file.
            metric_base = dict(base_live)
            metric_base["reads_succeeded"] = base_scores.get("reads_succeeded")
            metric_base["reads_attempted"] = base_scores.get("reads_attempted")
            metric_base["baseline_locks"] = score_delta(base_scores.get("locks") or [], []).get("baseline_locks")
            # score_delta(base, []) puts baseline pairs in missed and baseline_locks = len(base).
            # Recompute properly:
            delta = score_delta(base_scores.get("locks") or [], scores.get("locks") or [])
            metric_base["baseline_locks"] = delta["baseline_locks"]
            metric_base["capture_ok"] = base_live.get("capture_ok")
            metric_var = dict(live)
            metric_var.update({
                "same_footage": True,
                "reads_attempted": scores.get("reads_attempted"),
                "reads_succeeded": scores.get("reads_succeeded"),
                **{k: delta[k] for k in ("wrong_reads", "wrong_pairs", "missed_scores", "missed_pairs")},
                "baseline_locks": delta["baseline_locks"],
            })
            metric_base["same_footage"] = True
            metric_base["reads_succeeded"] = base_scores.get("reads_succeeded")
            metric_base["wrong_reads"] = 0
            metric_base["missed_scores"] = 0
            promotion[name] = promotion_decision(metric_base, metric_var)
            promotion[name]["reads_succeeded"] = scores.get("reads_succeeded")
            promotion[name]["wrong_reads"] = delta["wrong_reads"]
        else:
            scores = live_scores.get(name) or {"reads_attempted": None, "reads_succeeded": None, "locks": []}
            base_scores = live_scores.get("baseline") or {"locks": [], "reads_attempted": None, "reads_succeeded": None}
            same = bool(args.stable_picture) or name == "baseline"
            delta = score_delta(base_scores.get("locks") or [], scores.get("locks") or [])
            base_row = _metric_row(base_live, base_scores, True, score_delta(base_scores.get("locks") or [], base_scores.get("locks") or []))
            var_row = _metric_row(live, scores, same, delta)
            # baseline compared to itself has zero wrong/missed and same lock count
            promotion[name] = promotion_decision(base_row, var_row)
            promotion[name]["reads_succeeded"] = scores.get("reads_succeeded")
            promotion[name]["wrong_reads"] = delta["wrong_reads"]
    report = {
        "promotion_rule": (
            "A flag may become the default only with zero new wrong score reads, no missed baseline "
            "scores, no fewer successful reads, a baseline that locked at least one score, the same "
            "footage, no more drops or duplicate frames, audio lag no worse than +0.5s, video queue "
            "no worse than +1 MB, CPU not above baseline by more than max(8 points, 25% relative), "
            "and disk MB/min not above baseline by more than 0.05. Otherwise it stays off."
        ),
        "variants": lives,
        "same_footage": same_scores,
        "live_scores": {k: {kk: vv for kk, vv in v.items() if kk != "locks"} | {"locks": v.get("locks")} for k, v in live_scores.items()},
        "promotion": promotion,
        "scorebug_error": score_err,
        "stable_picture": bool(args.stable_picture),
        "free_gb": free_gb,
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
