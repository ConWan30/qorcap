"""Recorder-side challenge recheck for a USB capture card and a Bluetooth pad (QorCap 0.3.15).

QorTime's contract still wants the pause menu 0..600 ms after OPTIONS (``contracts/compat.json``).
That bound does not fit this capture path. On match-20261017-110028 the frames are 0.25 s apart and
the OPTIONS time is the HID press:

* kickoff: the dark pause menu is stable at +1750 ms (full luma 16.6, lower 6.7). The detector's
  full-luma cap of 16.0 missed it, so the recheck reported 3759 ms — the moment a later frame dipped
  under the cap, not the moment the menu appeared.
* halftime: the same menu is stable at +1750 ms and the detector already fired there (the recheck's
  3708 ms was that late dip).
* end: the pause menu is stable from +1750 ms but brighter (full luma 21, lower 13). The 16/8 caps
  never matched, so the recheck said ``pause_not_seen_on_video``.

v0.3.14 raised the luma caps and the bound, and the field recheck then saw all three menus. It still
reported them late: kickoff 3593 ms, halftime 3641 ms, end 3522 ms. The frames themselves show the
menu steady at about +1.75 s, so the miss was the video-to-wall clock.

``manifest.jsonl`` ``mono_ms`` / ``wall_ms`` are taken in ``Engine::on_segment`` when the fMP4
fragment is hashed, which is when the next ``moof`` arrives. That is the fragment's close, not its
first frame. ``segment_ranges`` already treats the stamp as an end and uses the previous close as
this fragment's start. On a QorCap test-pattern capture that start is still ~1.6 s after the first
frame inside the fragment: the remuxer is behind the capture clock, so the close stamp (and the
previous close) land about one fragment late. Match 110028's frame fit puts video start at
11:00:40.35 CDT; the old recheck's segment-0 fallback (``close - 2000``) puts it at 11:00:42.5.

The recheck now stamps each decoded frame from the fragment's own base media decode time (``tfdt``,
the first sample's PTS) through the manifest frame fit — the same fit ``align.rs`` uses for the
input proof (cumulative ``frames`` vs ``mono_ms``). A manifest whose ``frames`` field does not
advance (the synthetic review fixture) keeps PTS as the mono clock, which is how that fixture is
stamped. If a fragment has no ``tfdt``, the old ``segment_ranges`` time is kept.

The recorder recheck keeps the same rule shape and a hard bound:

* pause must still be *after* the answering OPTIONS press (``video_before_input`` otherwise);
* the menu is the dark Madden pause screen (a little white text, not a pure-black fade, not gameplay);
* luma caps are full <= 26 and lower <= 16, which cover this menu and still reject the gameplay
  frames around the press (luma 60–110);
* the menu must appear by 2500 ms. That is the measured ~1750 ms onset plus 750 ms for a slower fade
  on the same path, and it stays inside the 5 s search horizon. A relayed stream whose pause shows
  up later than 2.5 s still fails.

Live challenge gating does not use this mapping. ``PauseCapableGate`` / ``OfficialLive`` see the
live frame clock passed to ``ReaderCore.on_frame`` (wall time while recording). The recording path
only writes the close stamp. The official referee's ``review.engine`` still uses ``segment_ranges``
and 600 ms. Nothing here is submitted to it. An ok result is ``reverified_by: recorder`` only.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

CAPTURE_PAUSE_MAX_LATENCY_MS = 2_500
# Measured pause menus: kickoff full 16.2–16.6, end full 21.1 / lower 13.3. Gameplay on the same
# capture is luma 60–110. 26 / 16 sits between those and still requires the white-text fraction.
CAPTURE_PAUSE_MAX_LUMA = 26.0
CAPTURE_PAUSE_MAX_LOWER = 16.0
_NOMINAL_MS = 1000.0 / 30.0


def capture_pause_detector():
    from qor_receipt.agent.pause_detect import PauseDetector, PauseThresholds

    return PauseDetector(PauseThresholds(
        dark_menu_max_luma=CAPTURE_PAUSE_MAX_LUMA,
        dark_menu_max_lower=CAPTURE_PAUSE_MAX_LOWER,
    ))


def _boxes(data: bytes):
    i, n = 0, len(data)
    while i + 8 <= n:
        size = int.from_bytes(data[i:i + 4], "big")
        typ = data[i + 4:i + 8]
        hdr = 8
        if size == 1 and i + 16 <= n:
            size = int.from_bytes(data[i + 8:i + 16], "big")
            hdr = 16
        if size < hdr or i + size > n:
            return
        yield typ, data[i + hdr:i + size]
        i += size


def _video_track(init: bytes) -> tuple[int | None, int | None]:
    """(track id, media timescale) of the video track in an fMP4 init segment."""
    moov = next((p for t, p in _boxes(init) if t == b"moov"), None)
    if not moov:
        return None, None
    for typ, trak in _boxes(moov):
        if typ != b"trak":
            continue
        tid = ts = None
        vide = False
        for t, p in _boxes(trak):
            if t == b"tkhd" and p:
                off = 20 if p[0] == 1 else 12
                if len(p) >= off + 4:
                    tid = int.from_bytes(p[off:off + 4], "big")
            elif t == b"mdia":
                for t2, p2 in _boxes(p):
                    if t2 == b"hdlr" and len(p2) >= 12 and p2[8:12] == b"vide":
                        vide = True
                    elif t2 == b"mdhd" and p2:
                        off = 20 if p2[0] == 1 else 12
                        if len(p2) >= off + 4:
                            ts = int.from_bytes(p2[off:off + 4], "big")
        if vide and tid and ts:
            return tid, ts
    return None, None


def fragment_pts0_ms(init: bytes, fragment: bytes) -> float | None:
    """PTS of the first sample in an fMP4 fragment, in milliseconds. None when the boxes are absent."""
    tid, timescale = _video_track(init)
    if not timescale:
        return None
    moof = next((p for t, p in _boxes(fragment) if t == b"moof"), None)
    if not moof:
        return None
    found: list[tuple[int | None, int]] = []
    for typ, traf in _boxes(moof):
        if typ != b"traf":
            continue
        track = base = None
        for t, p in _boxes(traf):
            if t == b"tfhd" and len(p) >= 8:
                track = int.from_bytes(p[4:8], "big")
            elif t == b"tfdt" and len(p) >= 8:
                base = int.from_bytes(p[4:12], "big") if p[0] == 1 and len(p) >= 12 else int.from_bytes(p[4:8], "big")
        if base is not None:
            found.append((track, base))
    if not found:
        return None
    base = next((b for track, b in found if tid is not None and track == tid), None)
    if base is None:
        base = found[0][1]
    return base * 1000.0 / timescale


def capture_clock(segments: list[dict[str, Any]]) -> tuple[float, float]:
    """(mono_ms of video PTS 0, ms per frame) from the manifest.

    Media segments whose ``frames`` count advances are the live capture counter at each close.
    Fitting that against ``mono_ms`` is the clock in ``align.rs`` ``file_clocks``: frame 0 of the
    file, extrapolated back from the closes. A manifest that stores a constant ``frames`` (the
    synthetic review fixture) did not use that counter; its PTS is already the mono clock.
    """
    media = []
    for s in segments:
        if s.get("kind") == "init":
            continue
        fr, mono = s.get("frames"), s.get("mono_ms")
        if isinstance(fr, bool) or isinstance(mono, bool):
            continue
        if isinstance(fr, (int, float)) and isinstance(mono, (int, float)) and fr > 0:
            media.append((float(fr), float(mono)))
    if len(media) >= 5 and media[-1][0] - media[0][0] >= 10 * 30:
        n = float(len(media))
        mx = sum(p[0] for p in media) / n
        my = sum(p[1] for p in media) / n
        sxx = sum((p[0] - mx) ** 2 for p in media)
        sxy = sum((p[0] - mx) * (p[1] - my) for p in media)
        if sxx > 0:
            slope = sxy / sxx
            if abs(slope / _NOMINAL_MS - 1.0) <= 0.05:
                return my - slope * mx, slope
    return 0.0, _NOMINAL_MS


def frame_mono_ms(clock: tuple[float, float], pts0_ms: float, n: int, hz: float) -> int:
    """Capture mono time of decoded frame ``n`` in a run whose first sample PTS is ``pts0_ms``."""
    intercept, slope = clock
    frame = (pts0_ms + n * 1000.0 / hz) * (hz / 1000.0)
    return int(round(intercept + slope * frame))


def challenge_results(d: str | Path, challenges: list[dict[str, Any]], *, decode: bool = True) -> list[dict[str, Any]]:
    """Same recheck as QorTime ``review.local.challenge_results``, with this capture's pause detector,
    bound, and fragment-PTS clock."""
    from qor_receipt.challenge import response_press_ms, verify_response
    from qor_receipt.jcs import sha256_hex
    from qor_receipt.review import bundle as B
    from qor_receipt.review import media as M
    from qor_receipt.review.engine import pause_seen_for
    from qor_receipt.review.local import load_dir, segment_reader, wall_minus_mono

    d = Path(d)
    b = load_dir(d)
    presses = B.press_events(b)
    wmm = wall_minus_mono(b)
    rd = segment_reader(d, b)
    rng = B.segment_ranges(b)
    clock = capture_clock(b.segments)
    hz = 30.0

    def onsets_for(lo_wall: int, hi_wall: int) -> list[int]:
        if not decode or not M.ffmpeg_bin():
            raise RuntimeError("ffmpeg not available")
        lo, hi = lo_wall - wmm, hi_wall - wmm
        seqs = [s for s, (a, z) in rng.items() if z >= lo and a <= hi]

        def frames():
            for run in M.runs(seqs, b):
                init = M.init_seq_for(b, run[0])
                pts0 = fragment_pts0_ms(rd(init) or b"", rd(run[0]) or b"") if init is not None else None
                for n, (t, img) in enumerate(M.decode_run(b, run, rd, hz=hz, size=(640, 360))):
                    mono = t if pts0 is None else frame_mono_ms(clock, pts0, n, hz)
                    yield mono + wmm, img

        return M.pause_onsets(frames(), capture_pause_detector())

    def rerun(c: dict[str, Any]) -> dict[str, Any]:
        try:
            issued, deadline = int(c["issued_ms"]), int(c["deadline_ms"])
        except (KeyError, TypeError, ValueError):
            return {"result": None, "local": {"ok": False, "reason": "challenge_timing_malformed"}}
        pat = c.get("pattern") or []
        win = [p for p in presses if p.get("down", True) and issued <= p["t_ms"] <= deadline]
        pp = response_press_ms(pat, win, issued_ms=issued, deadline_ms=deadline)
        pause_seen, note = None, None
        if pp is not None:
            try:
                pause_seen = pause_seen_for(pp, onsets_for(pp - 1_000, pp + 5_000))
            except Exception as e:
                note = f"pause decode failed: {type(e).__name__}: {e}"
        res = verify_response(
            pat, [{"t_ms": p["t_ms"], "button": p["button"], "down": True} for p in win],
            issued_ms=issued, deadline_ms=deadline, pause_seen_ms=pause_seen,
            max_latency_ms=CAPTURE_PAUSE_MAX_LATENCY_MS,
        ).to_dict()
        res["hid_sha256"] = sha256_hex(win)
        res["reverified_by"] = "recorder"
        local = dict(res, phase=c.get("phase"), id=c.get("id"), presses_in_window=len(win),
                     capture_pause_max_ms=CAPTURE_PAUSE_MAX_LATENCY_MS)
        if note:
            local["note"] = note
        return {"result": res if res["ok"] else None, "local": local}

    return [rerun(c) for c in challenges]
