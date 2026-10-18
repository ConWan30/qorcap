"""Verified highlight clips: cut a short clip around each locked score change of a FINISHED match bundle.

    qorcap highlights [BUNDLE] [--pre 8] [--post 4] [--out DIR] [--no-burn] [--max N]
    qorcap highlights --check BUNDLE/highlights/manifest.json [--bundle BUNDLE]
    python sidecar/qorcap_highlights.py ...

Off by default. It never runs while QorCap is recording: a fresh ``.qorcap-run/recording.lock`` (touched on every
~2 s video segment) makes it refuse (exit 3), and it needs the signed ``receipt.json`` that Start Match / Finalize only
write after the stop (exit 4 without it).

What a "lock" is: a score change that QorCap committed into the checkpoint hash chain, i.e. a checkpoint whose
``qorcap.trigger`` is ``score_change`` (live) and whose ``frames_hash`` equals ``sha256(JCS(evidence))``. Locks whose
checkpoint hash is inside the signed ``receipt.json`` are marked ``in_receipt``. If the live chain has none, the offline
``score_checkpoints.jsonl`` written by ``qorcap finalize`` is used (hash-chained and bound to the video chain head, but
not inside the receipt signature). A lock that a later lock contradicts (a side goes down) is skipped as a misread.

Each clip is re-encoded with a small burned-in overlay (locked score in gold, lock hash, receipt hash) and gets a
WebVTT sidecar with the same text. ``manifest.json`` (schema ``qorcap-highlights/v0``) links every clip to its lock, the
receipt's SHA-256 and the hash-chained source segments that cover the window, so anyone can check it. A re-encoded clip
cannot be byte-matched to the source; what is checkable is the lock, the receipt and the source segment hashes. It shows
what was on screen, not who held the controller.
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

SCHEMA = "qorcap-highlights/v0"
VERSION = "0.1.0"
FPS = 30.0
PRE_S, POST_S = 8.0, 4.0
MARKER = Path(".qorcap-run") / "recording.lock"
MARKER_FRESH_S = 30  # = src/inputproof.rs MARKER_FRESH_SECS
FOOTBALL_DELTAS = {1, 2, 3, 6, 7, 8}
CLAIMS = ("Each clip shows hash-chained recording bytes around a score change that QorCap committed into its checkpoint "
          "chain. Clips are re-encoded, so check the lock, the receipt hash and the source segment hashes, not the clip "
          "bytes against the video. Integrity and timing only: this does not show who was playing.")


class Refuse(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code


def say(msg: str = "") -> None:
    print(msg, flush=True)


def read_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def jcs_sha(obj) -> str:
    """sha256 of the RFC 8785 canonical JSON (QorTime's jcs when importable; same bytes for QorCap's int/str evidence)."""
    try:
        from qor_receipt.jcs import sha256_hex
        return sha256_hex(obj)
    except ImportError:
        return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def recorder_running(bundle: Path, now: float | None = None) -> bool:
    p = bundle / MARKER
    try:
        age = (time.time() if now is None else now) - p.stat().st_mtime
    except OSError:
        return False
    return age < MARKER_FRESH_S


# ------------------------------------------------------------------ time: wall ms -> (video file, seconds)
class VideoClock:
    """Per video part: wall_ms -> seconds into that file, from manifest segments (ffmpeg frame counter / 30 fps).

    ffmpeg restarts (a new ``video.partN.mp4``) reset the frame counter, so every file gets its own curve."""

    def __init__(self, manifest: list[dict]):
        self.files: dict[str, list[tuple[int, float]]] = {}
        self.segs: list[dict] = [m for m in manifest if m.get("type") == "segment"]
        for m in self.segs:
            if isinstance(m.get("frames"), (int, float)) and "wall_ms" in m:
                self.files.setdefault(m.get("file") or "video.mp4", []).append((int(m["wall_ms"]), m["frames"] / FPS))
        for v in self.files.values():
            v.sort()

    def locate(self, wall_ms: int) -> tuple[str, float] | None:
        best = None
        for f, pts in self.files.items():
            if len(pts) < 2:
                continue
            lo, hi = pts[0][0], pts[-1][0]
            if lo - 4000 <= wall_ms <= hi + 4000:
                d = 0 if lo <= wall_ms <= hi else min(abs(wall_ms - lo), abs(wall_ms - hi))
                if best is None or d < best[0]:
                    best = (d, f, pts)
        if not best:
            return None
        _, f, pts = best
        xs = [p[0] for p in pts]
        i = min(max(bisect.bisect_left(xs, wall_ms), 1), len(xs) - 1)
        (x0, y0), (x1, y1) = pts[i - 1], pts[i]
        s = y0 if x1 == x0 else y0 + (wall_ms - x0) * (y1 - y0) / (x1 - x0)
        return f, max(0.0, s)

    def segments_between(self, file: str, wall_a: int, wall_b: int) -> list[dict]:
        """Hash-chained segments of ``file`` that cover [wall_a, wall_b] (a segment is stamped when it completes)."""
        fs = [m for m in self.segs if (m.get("file") or "video.mp4") == file and m.get("kind") != "init"]
        out, started = [], False
        for m in fs:
            if m.get("wall_ms", 0) >= wall_a:
                started = True
            if started:
                out.append({"seq": m.get("seq"), "sha256": m.get("sha256"), "chain_head": m.get("chain_head"),
                            "wall_ms": m.get("wall_ms")})
                if m.get("wall_ms", 0) >= wall_b:
                    break
        return out


# ------------------------------------------------------------------ locks
def _score(ev: dict) -> tuple[int, int] | None:
    s = ev.get("score") if isinstance(ev.get("score"), dict) else None
    if s is None and isinstance(ev.get("final"), dict):
        s = ev["final"]
    if not s or not isinstance(s.get("away"), int) or not isinstance(s.get("home"), int):
        return None
    return s["away"], s["home"]


def play_label(delta: tuple[int, int]) -> str:
    d = max(delta)
    return {6: "touchdown", 7: "touchdown + PAT", 8: "touchdown + 2-pt", 3: "field goal", 2: "safety / 2-pt",
            1: "extra point"}.get(d, "score change")


def find_locks(bundle: Path, receipt: dict) -> tuple[list[dict], list[dict], str]:
    """(locks, skipped, source). Locks are ordered score changes committed into a checkpoint chain."""
    in_receipt = set()
    for cps in (receipt.get("checkpoints") or {}).values():
        for c in cps or []:
            if isinstance(c, dict) and c.get("hash"):
                in_receipt.add(c["hash"])
    live = [c for c in read_jsonl(bundle / "checkpoints.jsonl") if (c.get("qorcap") or {}).get("trigger") == "score_change"]
    source = "live_checkpoint"
    cands = live
    if not live:
        cands = [c for c in read_jsonl(bundle / "score_checkpoints.jsonl")
                 if (c.get("qorcap") or {}).get("trigger") in ("score_change", "final")]
        source = "offline_score_checkpoint"
    cands.sort(key=lambda c: (c.get("t_ms", 0), c.get("seq", 0)))
    raw, prev = [], None
    for c in cands:
        q = c.get("qorcap") or {}
        sc = _score(q)
        if sc is None:
            continue
        if prev is None:
            prev = sc
            if sc == (0, 0):
                continue  # opening 0-0 is the baseline, not a score change
            prev = (0, 0)
        if sc == prev:
            continue
        s = q.get("score") or q.get("final") or {}
        raw.append({"source": source, "seq": c.get("seq"), "t_ms": c.get("t_ms"), "hash": c.get("hash"), "prev": c.get("prev"),
                    "frames_hash": c.get("frames_hash"), "frames_hash_matches_evidence": c.get("frames_hash") == jcs_sha(q),
                    "in_receipt": c.get("hash") in in_receipt, "trigger": q.get("trigger"),
                    "score_before": {"away": prev[0], "home": prev[1]}, "score_after": {"away": sc[0], "home": sc[1]},
                    "quarter": s.get("quarter"), "clock_s": s.get("clock_s"),
                    "segment_seq": q.get("segment_seq"), "segment_hash": q.get("segment_hash"), "chain_head": q.get("chain_head"),
                    "evidence": q})
        prev = sc
    locks, skipped = [], []
    for i, lk in enumerate(raw):
        a, h = lk["score_after"]["away"], lk["score_after"]["home"]
        b = lk["score_before"]
        delta = (a - b["away"], h - b["home"])
        lk["delta"] = {"away": delta[0], "home": delta[1]}
        later_lower = any(x["score_after"]["away"] < a or x["score_after"]["home"] < h for x in raw[i + 1:])
        legal = all(d >= 0 for d in delta) and sum(1 for d in delta if d) == 1 and max(delta) in FOOTBALL_DELTAS
        why = ("frames_hash does not match the checkpoint evidence" if not lk["frames_hash_matches_evidence"] else
               "a later lock lowers this score (scorebug misread)" if later_lower else
               "not a single-side football increment" if not legal else None)
        lk["play"] = play_label(delta) if legal else None
        if why:
            skipped.append({"seq": lk["seq"], "t_ms": lk["t_ms"], "hash": lk["hash"], "score_after": lk["score_after"], "reason": why})
        else:
            locks.append(lk)
    return locks, skipped, source


# ------------------------------------------------------------------ ffmpeg
def _font() -> str | None:
    for f in (os.getenv("QORCAP_FONT"), r"C:\Windows\Fonts\consolab.ttf", r"C:\Windows\Fonts\consola.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
              "/System/Library/Fonts/Menlo.ttc"):
        if f and Path(f).exists():
            return f
    return None


def _esc(p: str) -> str:
    """Path for an ffmpeg filter option value."""
    return p.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def probe_height(ffmpeg: str, src: Path) -> int:
    """Video height (for overlay sizing); 720 (Start Match's default) when ffprobe is unavailable."""
    fp = Path(ffmpeg)
    probe = str(fp.with_name(fp.name.replace("ffmpeg", "ffprobe"))) if fp.name.startswith("ffmpeg") else "ffprobe"
    try:
        r = subprocess.run([probe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=height", "-of", "csv=p=0",
                            str(src)], check=False, capture_output=True, text=True, timeout=30)
        return int(r.stdout.strip().splitlines()[0])
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return 720


def overlay_filter(score_txt: str, meta_txt: str, workdir: Path, stem: str, font: str | None, height: int = 720) -> str:
    """Small navy glass panel at the TOP-left (the game's own scorebug at the bottom stays visible), locked score in gold."""
    (workdir / f"{stem}.score.txt").write_text(score_txt, encoding="utf-8")
    (workdir / f"{stem}.meta.txt").write_text(meta_txt, encoding="utf-8")
    ff = f":fontfile='{_esc(font)}'" if font else ""
    k = max(0.5, height / 360)
    x, y, w, h = int(10 * k), int(10 * k), int(330 * k), int(58 * k)
    return (f"drawbox=x={x}:y={y}:w={w}:h={h}:color=0x081122@0.80:t=fill,"
            f"drawbox=x={x}:y={y}:w={max(2, int(2 * k))}:h={h}:color=0x3fe0d0@0.95:t=fill,"
            f"drawtext=text='LOCKED SCORE'{ff}:fontsize={int(9 * k)}:fontcolor=0x3fe0d0:x={x + int(9 * k)}:y={y + int(6 * k)},"
            f"drawtext=textfile='{_esc(str(workdir / (stem + '.score.txt')))}'{ff}:fontsize={int(18 * k)}:fontcolor=0xf0c95c"
            f":x={x + int(9 * k)}:y={y + int(18 * k)},"
            f"drawtext=textfile='{_esc(str(workdir / (stem + '.meta.txt')))}'{ff}:fontsize={int(8 * k)}:fontcolor=0xb8c7da"
            f":x={x + int(9 * k)}:y={y + int(43 * k)}")


def cut(ffmpeg: str, src: Path, start: float, dur: float, out: Path, vf: str | None) -> tuple[bool, str]:
    args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{dur:.3f}"]
    if vf:
        args += ["-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
                 "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart"]
    else:
        args += ["-c", "copy", "-movflags", "+faststart"]
    args.append(str(out))
    r = subprocess.run(args, check=False, capture_output=True, text=True)
    return r.returncode == 0 and out.exists() and out.stat().st_size > 0, (r.stderr or "")[-400:]


def fmt_clock(s) -> str:
    return f"{int(s) // 60}:{int(s) % 60:02d}" if isinstance(s, (int, float)) else "--:--"


def vtt_time(s: float) -> str:
    return f"{int(s // 3600):02d}:{int(s % 3600 // 60):02d}:{s % 60:06.3f}"


# ------------------------------------------------------------------ main work
def make_highlights(bundle: Path, out: Path | None = None, pre: float = PRE_S, post: float = POST_S, burn: bool = True,
                    max_clips: int | None = None, ffmpeg: str = "ffmpeg", now: float | None = None) -> dict:
    bundle = Path(bundle)
    if recorder_running(bundle, now):
        raise Refuse(3, f"QorCap is still recording into {bundle}. Highlights only run after the match stops.")
    rp = bundle / "receipt.json"
    if not rp.exists():
        raise Refuse(4, "no signed receipt.json in this bundle. Finish the match (or run `qorcap finalize`) first.")
    receipt = json.loads(rp.read_text(encoding="utf-8"))
    manifest = read_jsonl(bundle / "manifest.jsonl")
    clock = VideoClock(manifest)
    locks, skipped, source = find_locks(bundle, receipt)
    if max_clips is not None:
        locks = locks[:max_clips]
    out = Path(out) if out else bundle / "highlights"
    out.mkdir(parents=True, exist_ok=True)
    work = out / ".work"
    work.mkdir(exist_ok=True)
    font = _font() if burn else None
    receipt_sha = sha256_file(rp)
    m = receipt.get("match") or {}
    match_id = m.get("match_id")
    clips, heights = [], {}
    for n, lk in enumerate(locks):
        loc = clock.locate(int(lk["t_ms"]))
        sa = lk["score_after"]
        stem = f"hl{n:02d}-q{lk.get('quarter') or 0}-a{sa['away']}-h{sa['home']}"
        if loc is None:
            skipped.append({"seq": lk["seq"], "t_ms": lk["t_ms"], "hash": lk["hash"], "score_after": sa,
                            "reason": "lock time is outside the recorded video (no manifest frame clock)"})
            continue
        vfile, at = loc
        src = bundle / vfile
        if not src.exists():
            skipped.append({"seq": lk["seq"], "t_ms": lk["t_ms"], "hash": lk["hash"], "score_after": sa,
                            "reason": f"{vfile} is not in the bundle folder"})
            continue
        a = max(0.0, at - pre)
        dur = (at + post) - a
        score_txt = f"AWAY {sa['away']} - HOME {sa['home']}   Q{lk.get('quarter') or '?'} {fmt_clock(lk.get('clock_s'))}"
        meta_txt = f"lock #{lk['seq']} {lk['hash'][:12]}  receipt {receipt_sha[:12]}"
        vf = overlay_filter(score_txt, meta_txt, work, stem, font, heights.setdefault(vfile, probe_height(ffmpeg, src))) if burn else None
        f = out / f"{stem}.mp4"
        ok, err = cut(ffmpeg, src, a, dur, f, vf)
        overlay = "burned+sidecar" if (ok and burn) else "sidecar"
        if not ok and burn:  # e.g. an ffmpeg without libx264/freetype: fall back to a stream copy + sidecar overlay
            ok, err = cut(ffmpeg, src, a, dur, f, None)
        if not ok:
            skipped.append({"seq": lk["seq"], "t_ms": lk["t_ms"], "hash": lk["hash"], "score_after": sa,
                            "reason": f"ffmpeg failed: {err.strip()[:200]}"})
            continue
        lock_at = round(at - a, 3)
        (out / f"{stem}.vtt").write_text(
            "WEBVTT\n\n" f"1\n{vtt_time(0)} --> {vtt_time(dur)}\nLOCKED {score_txt}\n{meta_txt}\n\n"
            f"2\n{vtt_time(lock_at)} --> {vtt_time(min(dur, lock_at + 2))}\nscore locked here\n", encoding="utf-8")
        wall_a, wall_b = int(lk["t_ms"] - pre * 1000), int(lk["t_ms"] + post * 1000)
        clips.append({
            "id": stem, "file": f.name, "sha256": sha256_file(f), "bytes": f.stat().st_size, "vtt": f"{stem}.vtt",
            "duration_s": round(dur, 3), "lock_at_s": lock_at, "overlay": overlay, "overlay_text": [score_txt, meta_txt],
            "source": {"file": vfile, "start_s": round(a, 3), "end_s": round(at + post, 3),
                       "segments": clock.segments_between(vfile, wall_a, wall_b)},
            "lock": {k: v for k, v in lk.items() if k != "evidence"} | {"evidence": lk["evidence"]},
        })
        say(f"  clip {stem}: {lk['play']} -> AWAY {sa['away']} - HOME {sa['home']} (lock #{lk['seq']}, {overlay})")
    shutil.rmtree(work, ignore_errors=True)
    sig = receipt.get("signatures") or {}
    man = {
        "schema": SCHEMA, "tool": f"qorcap-highlights {VERSION}", "generated_ms": int(time.time() * 1000),
        "match_id": match_id, "params": {"pre_s": pre, "post_s": post, "burn": burn, "lock_source": source},
        "receipt": {"file": "receipt.json", "sha256": receipt_sha, "receipt_id": receipt.get("receipt_id") or match_id,
                    "schema": receipt.get("schema"), "result": receipt.get("result"),
                    "claimed_trust_tier": receipt.get("trust_tier"),
                    "referee": {k: (receipt.get("referee") or {}).get(k) for k in ("key_id", "key_kind")},
                    "signed_by_referee": bool(sig.get("referee")), "signed_by_players": sorted((sig.get("players") or {}).keys()),
                    "note": "claimed_trust_tier is what the receipt says; the tier that counts is the one the QorTime verifier computes."},
        "video": {"chain_head": (manifest[-1] if manifest else {}).get("chain_head"),
                  "files": sorted(clock.files.keys())},
        "claims": CLAIMS,
        "how_to_check": [
            "sha256 each clip file and compare with clips[].sha256",
            "sha256 receipt.json and compare with receipt.sha256; verify the receipt at https://conwan30.github.io/qortime/verify/",
            ("for each clip: lock.hash must appear in receipt.checkpoints when lock.in_receipt is true, and "
             "lock.frames_hash must equal sha256(JCS(lock.evidence))"),
            "source.segments[].sha256 must match manifest.jsonl, which `qorcap verify-bundle` checks against the video bytes",
            "`qorcap highlights --check manifest.json --bundle BUNDLE` runs all of the above",
        ],
        "clips": clips, "skipped": skipped,
    }
    (out / "manifest.json").write_text(json.dumps(man, indent=2), encoding="utf-8")
    return man


def check_manifest(manifest_path: Path, bundle: Path | None = None) -> list[str]:
    """Problems found (empty = OK)."""
    mp = Path(manifest_path)
    man = json.loads(mp.read_text(encoding="utf-8"))
    errs = []
    if man.get("schema") != SCHEMA:
        errs.append(f"schema is {man.get('schema')!r}, expected {SCHEMA}")
    bundle = Path(bundle) if bundle else mp.parent.parent
    rp = bundle / "receipt.json"
    if not rp.exists() and (mp.parent / "receipt.json").exists():
        rp = mp.parent / "receipt.json"
    receipt = None
    if rp.exists():
        if sha256_file(rp) != (man.get("receipt") or {}).get("sha256"):
            errs.append("receipt.json sha256 does not match the manifest")
        receipt = json.loads(rp.read_text(encoding="utf-8"))
    else:
        errs.append("receipt.json not found next to the bundle / manifest")
    hashes = {c.get("hash") for cps in ((receipt or {}).get("checkpoints") or {}).values() for c in (cps or [])}
    mf = {m.get("seq"): m.get("sha256") for m in read_jsonl(bundle / "manifest.jsonl") if m.get("type") == "segment"}
    for c in man.get("clips") or []:
        f = mp.parent / c["file"]
        if not f.exists():
            errs.append(f"{c['id']}: clip file missing")
        elif sha256_file(f) != c.get("sha256"):
            errs.append(f"{c['id']}: clip sha256 mismatch")
        lk = c.get("lock") or {}
        if lk.get("frames_hash") != jcs_sha(lk.get("evidence")):
            errs.append(f"{c['id']}: lock frames_hash does not match its evidence")
        if lk.get("in_receipt") and receipt is not None and lk.get("hash") not in hashes:
            errs.append(f"{c['id']}: lock checkpoint {str(lk.get('hash'))[:12]} is not in receipt.json")
        if mf:
            for s in (c.get("source") or {}).get("segments") or []:
                if mf.get(s.get("seq")) != s.get("sha256"):
                    errs.append(f"{c['id']}: source segment {s.get('seq')} does not match manifest.jsonl")
    return errs


def newest_bundle() -> Path | None:
    root = Path(os.path.expanduser("~")) / "Documents" / "QorCap Matches"
    ds = sorted((d for d in root.glob("match-*") if d.is_dir()), key=lambda d: d.stat().st_mtime) if root.exists() else []
    return ds[-1] if ds else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="qorcap highlights", description="Verified highlight clips around locked score changes")
    ap.add_argument("bundle", nargs="?", help="finished match bundle (default: newest in Documents/QorCap Matches)")
    ap.add_argument("--pre", type=float, default=PRE_S, help="seconds before the lock (default 8)")
    ap.add_argument("--post", type=float, default=POST_S, help="seconds after the lock (default 4)")
    ap.add_argument("--out", help="output folder (default BUNDLE/highlights)")
    ap.add_argument("--no-burn", action="store_true", help="stream-copy clips; overlay only in the .vtt sidecar")
    ap.add_argument("--max", type=int, help="at most N clips")
    ap.add_argument("--check", metavar="MANIFEST", help="verify an existing highlights manifest instead")
    ap.add_argument("--ffmpeg", default=None)
    ap.add_argument("--qortime")
    ap.add_argument("--qorcap")
    a, _ = ap.parse_known_args(argv)
    if a.qortime and Path(a.qortime).exists():
        sys.path.insert(0, str(Path(a.qortime)))
    if a.check:
        errs = check_manifest(Path(a.check), Path(a.bundle) if a.bundle else None)
        for e in errs:
            say(f"FAIL: {e}")
        say("OK: clips, locks, receipt hash and source segments match." if not errs else "FAILED")
        return 0 if not errs else 1
    b = Path(a.bundle) if a.bundle else newest_bundle()
    if b is None or not b.is_dir():
        say("error: no match bundle folder given or found")
        return 2
    ff = a.ffmpeg or shutil.which("ffmpeg") or "ffmpeg"
    say(f"Highlights for {b} (lock -{a.pre:g} s .. +{a.post:g} s)")
    try:
        man = make_highlights(b, Path(a.out) if a.out else None, a.pre, a.post, not a.no_burn, a.max, ff)
    except Refuse as e:
        say(f"error: {e}")
        return e.code
    say(f"{len(man['clips'])} clip(s), {len(man['skipped'])} skipped -> {Path(a.out) if a.out else b / 'highlights'}"
        + "/manifest.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
