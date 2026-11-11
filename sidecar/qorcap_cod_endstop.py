"""Practice-lane auto stop after a Call of Duty VICTORY or DEFEAT end screen.

Off unless both are set: ``QORCAP_AUTOCLOSE=1`` and ``QORCAP_LANE=practice``, and the game set is
Call of Duty. The practice LIVE launcher sets both. REAL MATCH does not use this.

What it does. The CoD frame loop already reads the end word on every sampled frame
(``CodEndScreenReader``: VICTORY on the splash or match-complete header, DEFEAT on the header) and,
on an end frame, Qortrola30's scoreboard row. This module watches those reads. It does not read
pixels itself and does not post anything to ``/events``. Once the end screen is confirmed it waits a
tail (``QORCAP_AUTOCLOSE_TAIL_S``, default 12 s, clamped 10..15) and then ``qorcap_match`` stops with
the same code path as Enter: ``/shutdown``, seal, countersign, bundle, receipt.

The guard. A frame counts only when the end reader returns a calibrated, confirmed ``victory`` or
``defeat`` word. Confirmation needs a run of such frames:

- the same word on every frame of the run, with strictly later capture timestamps and no gap over
  ``gap_ms`` (3 s, the reader's own agreement gap);
- any other frame (killcam, gameplay, lobby, a mid-match Tab scoreboard with no end word, a blurred
  or cut word, the other word) ends the run, so a one-frame flicker never confirms;
- with Qortrola30's end-scoreboard row read on at least one frame of the run: ``frames`` reads
  (``QORCAP_AUTOCLOSE_FRAMES``, default 8) across at least 3 s;
- with no owner row read (the splash comes before the board, or the row did not read): twice as
  many reads across at least 6 s;
- the whole run must start at least ``QORCAP_AUTOCLOSE_ARM_S`` (default 120 s) after the first
  frame this watcher saw, so a lobby or pre-match screen cannot stop the recording.

What it is not. The trigger is a recording-control decision. It is not a final, not a score, not a
receipt field, and not a verification input. What happened is written to qorcap.log (one
``stop note:`` line) and to ``cod_end_stop.json`` beside the bundle. That file is not bundle.json,
not the receipt, and not hashed into either.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

ENV_FLAG = "QORCAP_AUTOCLOSE"
ENV_LANE = "QORCAP_LANE"
ENV_TAIL = "QORCAP_AUTOCLOSE_TAIL_S"
ENV_FRAMES = "QORCAP_AUTOCLOSE_FRAMES"
ENV_ARM = "QORCAP_AUTOCLOSE_ARM_S"
FILE_NAME = "cod_end_stop.json"
SCHEMA = "qorcap-cod-end-stop/v0"

TAIL_DEFAULT_S = 12.0
TAIL_MIN_S = 10.0
TAIL_MAX_S = 15.0
FRAMES_DEFAULT = 8
FRAMES_MIN = 3
FRAMES_MAX = 60
ARM_DEFAULT_S = 120.0
ARM_MAX_S = 1800.0
SPAN_MS = 3000
GAP_MS = 3000
END_WORDS = ("victory", "defeat")


def _truthy(v) -> bool:
    return isinstance(v, str) and v.strip().lower() in ("1", "true", "yes", "on")


def enabled(game, environ=None) -> bool:
    """Practice lane, the autoclose flag, and Call of Duty. Anything else is off."""
    env = os.environ if environ is None else environ
    lane = env.get(ENV_LANE)
    return (
        game == "cod"
        and _truthy(env.get(ENV_FLAG))
        and isinstance(lane, str) and lane.strip().lower() == "practice"
    )


def _num(raw, default, lo, hi):
    if raw is None or str(raw).strip() == "":
        return default
    try:
        n = float(str(raw).strip())
    except ValueError:
        return default
    if not math.isfinite(n):
        return default
    return min(hi, max(lo, n))


@dataclass(frozen=True)
class Config:
    frames: int = FRAMES_DEFAULT
    span_ms: int = SPAN_MS
    gap_ms: int = GAP_MS
    arm_ms: int = int(ARM_DEFAULT_S * 1000)
    tail_s: float = TAIL_DEFAULT_S

    @property
    def solo_frames(self) -> int:
        return self.frames * 2

    @property
    def solo_span_ms(self) -> int:
        return self.span_ms * 2


def config_from_env(environ=None) -> Config:
    env = os.environ if environ is None else environ
    return Config(
        frames=int(_num(env.get(ENV_FRAMES), FRAMES_DEFAULT, FRAMES_MIN, FRAMES_MAX)),
        arm_ms=int(_num(env.get(ENV_ARM), ARM_DEFAULT_S, 0.0, ARM_MAX_S) * 1000),
        tail_s=_num(env.get(ENV_TAIL), TAIL_DEFAULT_S, TAIL_MIN_S, TAIL_MAX_S),
    )


def end_word(end) -> str | None:
    """``victory`` / ``defeat`` when the end reader confirmed that word on this frame, else None."""
    if end is None:
        return None
    reason = getattr(end, "reason", None)
    if not (getattr(end, "ok", False) and getattr(end, "calibrated", False) and getattr(end, "confirm", False)):
        return None
    return reason if isinstance(reason, str) and reason in END_WORDS else None


def owner_row_seen(owner) -> bool:
    """Qortrola30's own end-scoreboard row was read on this frame."""
    if owner is None or not (getattr(owner, "ok", False) and getattr(owner, "calibrated", False)):
        return False
    try:
        from qorcap_cod import same_owner_tag
    except Exception:
        return False
    return bool(same_owner_tag(getattr(owner, "player_tag", None) or ""))


def _int(v):
    return v if isinstance(v, int) and not isinstance(v, bool) else None


class EndStop:
    """Debounce, arming and tail for the practice CoD auto stop.

    ``observe`` runs on the reader thread with the reads the frame loop already made.
    ``stop_due`` runs on the main loop. ``clock`` is injectable for tests.
    """

    def __init__(self, cfg: Config | None = None, clock=time.monotonic, wall=lambda: int(time.time() * 1000)):
        self.cfg = cfg or Config()
        self._clock = clock
        self._wall = wall
        self._lock = threading.Lock()
        self._first_t = None
        self._word = None
        self._n = 0
        self._t0 = None
        self._last_t = None
        self._owner = False
        self._start_src = None
        self.frames_seen = 0
        self.positives = 0
        self.runs_broken = 0
        self.confirmed: dict | None = None
        self._confirm_mono = None

    def _reset(self) -> None:
        if self._n:
            self.runs_broken += 1
        self._word = None
        self._n = 0
        self._t0 = None
        self._last_t = None
        self._owner = False
        self._start_src = None

    def observe(self, t_ms, end, owner=None, source=None) -> bool:
        """One sampled frame. True only on the frame that confirms."""
        t = _int(t_ms)
        with self._lock:
            if self.confirmed is not None or t is None:
                return False
            self.frames_seen += 1
            if self._first_t is None:
                self._first_t = t
            word = end_word(end)
            if word is None:
                self._reset()
                return False
            self.positives += 1
            if t - self._first_t < self.cfg.arm_ms:
                # Not armed yet: a lobby or pre-match screen. Do not start a run.
                self._reset()
                return False
            same_run = (
                self._n > 0 and word == self._word and isinstance(self._last_t, int)
                and t > self._last_t and t - self._last_t <= self.cfg.gap_ms
            )
            if not same_run:
                self._reset()
                self._word, self._n, self._t0, self._last_t = word, 1, t, t
                self._start_src = source if isinstance(source, dict) else None
            else:
                self._n += 1
                self._last_t = t
            if owner_row_seen(owner):
                self._owner = True
            span = t - self._t0
            if self._owner:
                ok = self._n >= self.cfg.frames and span >= self.cfg.span_ms
            else:
                ok = self._n >= self.cfg.solo_frames and span >= self.cfg.solo_span_ms
            if not ok:
                return False
            src = source if isinstance(source, dict) else {}
            start = self._start_src or {}
            self._confirm_mono = float(self._clock())
            self.confirmed = {
                "result": self._word.upper(),
                "reader": "cod_end_screen" + ("+owner_row" if self._owner else ""),
                "owner_row_seen": self._owner,
                "consecutive": self._n,
                "span_ms": span,
                "first_t_ms": self._t0,
                "confirm_t_ms": t,
                "confirm_frame_seq": _int(src.get("seq")),
                "confirm_pts_us": _int(src.get("pts_us")),
                "first_frame_seq": _int(start.get("seq")),
                "elapsed_ms": t - self._first_t,
                "confirm_wall_ms": self._wall(),
            }
            return True

    def stop_due(self, now: float | None = None) -> bool:
        with self._lock:
            if self._confirm_mono is None:
                return False
            now = float(self._clock()) if now is None else float(now)
            return now - self._confirm_mono >= self.cfg.tail_s

    def tail_elapsed_s(self, now: float | None = None) -> float | None:
        with self._lock:
            if self._confirm_mono is None:
                return None
            now = float(self._clock()) if now is None else float(now)
            return round(max(0.0, now - self._confirm_mono), 3)

    def describe(self) -> str:
        c = self.confirmed
        if c is None:
            return "Call of Duty end screen not confirmed."
        return (f"Call of Duty {c['result']} end screen confirmed ({c['consecutive']} reads in a row over "
                f"{c['span_ms'] / 1000:.1f} s, {'owner row read' if c['owner_row_seen'] else 'owner row not read'}).")

    def log_note(self, stop_reason: str) -> str:
        """One printable-ASCII line for qorcap.log. Practice only, info only."""
        c = self.confirmed or {}
        parts = [
            "practice autoclose",
            f"cod end screen {c.get('result')}",
            f"reader {c.get('reader')}",
            f"consecutive {c.get('consecutive')}",
            f"span_ms {c.get('span_ms')}",
            f"frame_seq {c.get('confirm_frame_seq')}",
            f"t_ms {c.get('confirm_t_ms')}",
            f"tail_s {self.cfg.tail_s:g}",
            f"then {stop_reason}",
            "info only, not a final",
        ]
        return "; ".join(parts)[:240]

    def record(self, bundle, *, stop_reason: str, stop_wall_ms: int | None, fired: bool) -> None:
        """Write ``cod_end_stop.json`` beside the bundle. Not bundle.json, not the receipt."""
        bundle = Path(bundle)
        if not bundle.is_dir():
            return
        with self._lock:
            doc = {
                "schema": SCHEMA,
                "info_only": True,
                "note": ("Practice-lane recording control. Not a final, not a score, not a receipt or "
                         "bundle.json field, and not read by verification."),
                "config": {"frames": self.cfg.frames, "solo_frames": self.cfg.solo_frames,
                           "span_ms": self.cfg.span_ms, "solo_span_ms": self.cfg.solo_span_ms,
                           "gap_ms": self.cfg.gap_ms, "arm_ms": self.cfg.arm_ms, "tail_s": self.cfg.tail_s},
                "confirmed": self.confirmed,
                "auto_stopped": bool(fired),
                "stop_reason": stop_reason,
                "stop_wall_ms": _int(stop_wall_ms),
                "frames_seen": self.frames_seen,
                "end_word_frames": self.positives,
                "runs_broken": self.runs_broken,
            }
        (bundle / FILE_NAME).write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
