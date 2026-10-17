"""Recorder-side challenge recheck for a USB capture card and a Bluetooth pad (QorCap 0.3.14).

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

The recorder recheck keeps the same rule shape and a hard bound:

* pause must still be *after* the answering OPTIONS press (``video_before_input`` otherwise);
* the menu is the dark Madden pause screen (a little white text, not a pure-black fade, not gameplay);
* luma caps are full <= 26 and lower <= 16, which cover this menu and still reject the gameplay
  frames around the press (luma 60–110);
* the menu must appear by 2500 ms. That is the measured ~1750 ms onset plus 750 ms for a slower fade
  on the same path, and it stays inside the 5 s search horizon. A relayed stream whose pause shows
  up later than 2.5 s still fails.

The official referee keeps 600 ms. Nothing here is submitted to it. An ok result is
``reverified_by: recorder`` only.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

CAPTURE_PAUSE_MAX_LATENCY_MS = 2_500
# Measured pause menus: kickoff full 16.2–16.6, end full 21.1 / lower 13.3. Gameplay on the same
# capture is luma 60–110. 26 / 16 sits between those and still requires the white-text fraction.
CAPTURE_PAUSE_MAX_LUMA = 26.0
CAPTURE_PAUSE_MAX_LOWER = 16.0


def capture_pause_detector():
    from qor_receipt.agent.pause_detect import PauseDetector, PauseThresholds

    return PauseDetector(PauseThresholds(
        dark_menu_max_luma=CAPTURE_PAUSE_MAX_LUMA,
        dark_menu_max_lower=CAPTURE_PAUSE_MAX_LOWER,
    ))


def challenge_results(d: str | Path, challenges: list[dict[str, Any]], *, decode: bool = True) -> list[dict[str, Any]]:
    """Same recheck as QorTime ``review.local.challenge_results``, with this capture's pause detector and bound."""
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

    def onsets_for(lo_wall: int, hi_wall: int) -> list[int]:
        if not decode or not M.ffmpeg_bin():
            raise RuntimeError("ffmpeg not available")
        lo, hi = lo_wall - wmm, hi_wall - wmm
        seqs = [s for s, (a, z) in rng.items() if z >= lo and a <= hi]

        def frames():
            for run in M.runs(seqs, b):
                for t, img in M.decode_run(b, run, rd, hz=30.0, size=(640, 360)):
                    yield t + wmm, img

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
