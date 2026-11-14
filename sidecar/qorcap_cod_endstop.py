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

That is the ``sustained`` path. In practice match-20261111-165658 the confirmed word was on screen
for about 0.74 s (2 reads) before the board and menu replaced it, so that path cannot fire on a
normal match end. The ``splash`` path covers it:

- CANDIDATE: at least 2 confirmed reads of the same word within 1.5 s (strictly later timestamps,
  armed, the other word not read in between).
- While a candidate is open (at most 30 s), the watcher also reads Qortrola30's end-scoreboard row on
  every frame. The frame loop reads that row only on frames that show the word, which is why the
  row was never read in that match.
- CONFIRMED by the first of: Qortrola30's row read (``splash+owner_row``); the same word read again
  at least 2 s after the candidate formed (``splash+word_again``); or 8 s of frames with no gameplay
  HUD event (``splash+quiet``). A gameplay HUD event is a calibrated hitmarker, kill popup or
  killfeed icon read; one restarts the 8 s count.
- CANCELLED by the other word, or by 30 s without a confirmation (gameplay that keeps producing HUD
  events never reaches the 8 s). A new candidate can form afterwards.

Killcam: a killcam does not show VICTORY or DEFEAT, so it cannot open a candidate. A killcam right
after the splash replays HUD-like overlays; any hitmarker / popup / feed read there only restarts the
quiet count. Honest limit: on the real match above only 2 HUD regions read across 639 frames, so the
quiet count mostly measures time. The splash path then rests on the end reader's precision (2
integrity-checked reads of one word within 1.5 s, after arming). There is no killcam footage in the
repo or on the build box to measure that.

The tail is measured from confirmation, not from the splash. On the splash path the stop is then at
least 8 + 12 s after the word unless the row or a second word read confirms sooner, so the end
scoreboard that follows the splash is in the recording.

Best Play stop (default on). Call of Duty shows the splash, then the end scoreboard (about 7 s),
then a black cut, then the "Best Play of the Match" replay with a BEST PLAY title. In practice
match-20261113-154154 the old fixed 12 s tail ended in that replay, so the board was recorded, but
Qortrola30's row was only read while a candidate was open. After confirmation the watcher now keeps
reading Qortrola30's row on every frame until it is read, and reads the BEST PLAY title
(``qorcap_cod_bestplay``). It stops at the first of:

- the title read on ``best_play_reads`` (2) frames in a row within 1.5 s, once the board read has had
  its chance: Qortrola30's row was read (before or after confirmation), or ``board_floor_s`` (8 s)
  have passed since confirmation;
- ``best_play_max_s`` (``QORCAP_AUTOCLOSE_BEST_PLAY_MAX_S``, default 25 s, clamped 15..45) after
  confirmation, whether or not the title was read.

``QORCAP_AUTOCLOSE_BEST_PLAY=0`` keeps the older fixed tail (``tail_s``). Either way the stop reason
stays ``post_final_tail`` (or ``final``); which rule fired is written to ``cod_end_stop.json`` only.

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
SPLASH_READS = 2
SPLASH_PAIR_MS = 1500
CANDIDATE_WINDOW_MS = 30_000
QUIET_MS = 8000
RESIGHT_MS = 2000
ENV_BEST_PLAY = "QORCAP_AUTOCLOSE_BEST_PLAY"
ENV_BEST_PLAY_MAX = "QORCAP_AUTOCLOSE_BEST_PLAY_MAX_S"
# Observed on five practice matches (154154, 145809, 133025, 183311, 194037): the BEST PLAY title
# came 13.75-15.5 s after the splash and 10.4-12.7 s after confirmation (154154, 145809). The end
# board ended 4.5-6.6 s after confirmation (same two). See docs/COD_BEST_PLAY_STOP.md.
BOARD_FLOOR_S = 8.0
BEST_PLAY_MAX_DEFAULT_S = 25.0
BEST_PLAY_MAX_MIN_S = 15.0
BEST_PLAY_MAX_MAX_S = 45.0
BEST_PLAY_READS = 2
BEST_PLAY_PAIR_MS = 1500
GAMEPLAY_HUD = ("hitmarker", "kill_popup", "killfeed")
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


def _falsy(v) -> bool:
    return isinstance(v, str) and v.strip().lower() in ("0", "false", "no", "off")


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
    splash_reads: int = SPLASH_READS
    splash_pair_ms: int = SPLASH_PAIR_MS
    window_ms: int = CANDIDATE_WINDOW_MS
    quiet_ms: int = QUIET_MS
    resight_ms: int = RESIGHT_MS
    splash: bool = True  # False keeps only the sustained path (tests)
    best_play: bool = True  # False keeps the older fixed tail_s after confirmation
    board_floor_s: float = BOARD_FLOOR_S
    best_play_max_s: float = BEST_PLAY_MAX_DEFAULT_S
    best_play_reads: int = BEST_PLAY_READS
    best_play_pair_ms: int = BEST_PLAY_PAIR_MS

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
        best_play=not _falsy(env.get(ENV_BEST_PLAY)),
        best_play_max_s=_num(env.get(ENV_BEST_PLAY_MAX), BEST_PLAY_MAX_DEFAULT_S,
                             BEST_PLAY_MAX_MIN_S, BEST_PLAY_MAX_MAX_S),
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


def gameplay_hud_event(hud) -> bool:
    """A calibrated hitmarker, kill popup or killfeed icon on this frame.

    Crosshair and pause are stubs that are never read, and the K/A scoreboard hook is not used
    here (it is a board read, not gameplay).
    """
    for rd in hud or ():
        if getattr(rd, "region", None) in GAMEPLAY_HUD and getattr(rd, "ok", False) and getattr(rd, "calibrated", False):
            return True
    return False


def _default_owner_reader(img):
    from qorcap_cod_hud import read_owner_row
    return read_owner_row(img)


def _default_best_play_reader(img):
    from qorcap_cod_bestplay import read_best_play
    return read_best_play(img)


def _int(v):
    return v if isinstance(v, int) and not isinstance(v, bool) else None


class EndStop:
    """Debounce, arming and tail for the practice CoD auto stop.

    ``observe`` runs on the reader thread with the reads the frame loop already made.
    ``stop_due`` runs on the main loop. ``clock`` is injectable for tests.
    """

    def __init__(self, cfg: Config | None = None, clock=time.monotonic, wall=lambda: int(time.time() * 1000),
                 owner_reader=_default_owner_reader, best_play_reader=_default_best_play_reader):
        self.cfg = cfg or Config()
        self._owner_reader = owner_reader
        self._bp_reader = best_play_reader
        # After confirmation (Best Play rule only).
        self.post_frames = 0
        self.post_owner_reads = 0
        self._post_last_t = None
        self._post_owner_t = None
        self._bp_pend = None
        self._bp_best = None
        self.best_play: dict | None = None
        self._bp_mono = None
        self.stop_rule: str | None = None
        self._pend = None
        self._cand = None
        self.candidates = 0
        self.candidates_cancelled: dict = {}
        self.owner_reads_attempted = 0
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

    def observe(self, t_ms, end, owner=None, source=None, hud=None, img=None) -> bool:
        """One sampled frame. True only on the frame that confirms."""
        t = _int(t_ms)
        with self._lock:
            if t is None:
                return False
            if self.confirmed is not None:
                self._after_confirm(t, owner, img, source if isinstance(source, dict) else None)
                return False
            self.frames_seen += 1
            if self._first_t is None:
                self._first_t = t
            armed = t - self._first_t >= self.cfg.arm_ms
            word = end_word(end)
            if word is not None:
                self.positives += 1
            if self._cand is not None and owner is None and img is not None and self._owner_reader is not None:
                self.owner_reads_attempted += 1
                try:
                    owner = self._owner_reader(img)
                except Exception:
                    owner = None
            owner_ok = owner_row_seen(owner)
            src = source if isinstance(source, dict) else None
            if self._sustain_step(t, word, owner_ok, armed, src):
                return self._confirm_sustained(t, src)
            if not self.cfg.splash:
                return False
            path = self._splash_step(t, word, owner_ok, gameplay_hud_event(hud), armed, src)
            if path is not None:
                return self._confirm_splash(t, path, src)
            return False

    def _sustain_step(self, t, word, owner_ok, armed, src) -> bool:
        if word is None or not armed:
            # Not an end word, or not armed yet (a lobby or pre-match screen): no run.
            self._reset()
            return False
        same_run = (
            self._n > 0 and word == self._word and isinstance(self._last_t, int)
            and t > self._last_t and t - self._last_t <= self.cfg.gap_ms
        )
        if not same_run:
            self._reset()
            self._word, self._n, self._t0, self._last_t = word, 1, t, t
            self._start_src = src
        else:
            self._n += 1
            self._last_t = t
        if owner_ok:
            self._owner = True
        span = t - self._t0
        if self._owner:
            return self._n >= self.cfg.frames and span >= self.cfg.span_ms
        return self._n >= self.cfg.solo_frames and span >= self.cfg.solo_span_ms

    def _cancel(self, why: str) -> None:
        self.candidates_cancelled[why] = self.candidates_cancelled.get(why, 0) + 1
        self._cand = None

    def _splash_step(self, t, word, owner_ok, hud_event, armed, src):
        c = self._cand
        if c is not None:
            if word is not None and word != c["word"]:
                self._cancel("other_word")
                self._pend = {"word": word, "first": t, "last": t, "n": 1, "src": src} if armed else None
                return None
            if t <= c["last_t"]:
                return None
            c["last_t"] = t
            c["frames"] += 1
            if hud_event:
                c["hud_events"] += 1
                c["quiet_from"] = t
            if owner_ok:
                return "splash+owner_row"
            if word == c["word"] and t - c["formed_t"] >= self.cfg.resight_ms:
                return "splash+word_again"
            if t - c["quiet_from"] >= self.cfg.quiet_ms:
                return "splash+quiet"
            if t - c["t0"] > self.cfg.window_ms:
                self._cancel("window_expired")
            return None
        if word is None:
            if self._pend is not None and t - self._pend["first"] > self.cfg.splash_pair_ms:
                self._pend = None
            return None
        if not armed:
            self._pend = None
            return None
        p = self._pend
        if p is not None and p["word"] == word and t > p["last"] and t - p["first"] <= self.cfg.splash_pair_ms:
            p["n"] += 1
            p["last"] = t
        else:
            p = self._pend = {"word": word, "first": t, "last": t, "n": 1, "src": src}
        if p["n"] < self.cfg.splash_reads:
            return None
        self._pend = None
        self.candidates += 1
        self._cand = {"word": word, "t0": p["first"], "formed_t": t, "last_t": t, "quiet_from": t,
                      "reads": p["n"], "frames": 0, "hud_events": 0, "src": p["src"]}
        if owner_ok:
            return "splash+owner_row"
        return None

    def _stamp(self, doc: dict, t: int, src) -> bool:
        src = src or {}
        doc.update({
            "confirm_t_ms": t,
            "confirm_frame_seq": _int(src.get("seq")),
            "confirm_pts_us": _int(src.get("pts_us")),
            "elapsed_ms": t - self._first_t,
            "confirm_wall_ms": self._wall(),
        })
        self._confirm_mono = float(self._clock())
        self.confirmed = doc
        return True

    def _confirm_sustained(self, t, src) -> bool:
        start = self._start_src or {}
        return self._stamp({
            "path": "sustained",
            "result": self._word.upper(),
            "reader": "cod_end_screen" + ("+owner_row" if self._owner else ""),
            "owner_row_seen": self._owner,
            "consecutive": self._n,
            "span_ms": t - self._t0,
            "first_t_ms": self._t0,
            "first_frame_seq": _int(start.get("seq")),
        }, t, src)

    def _confirm_splash(self, t, path, src) -> bool:
        c = self._cand
        start = c["src"] or {}
        follow = {"owner_row": "owner_row", "word_again": "word_again", "quiet": "quiet"}[path.split("+", 1)[1]]
        doc = {
            "path": path,
            "result": c["word"].upper(),
            "reader": "cod_end_screen" + ("+owner_row" if follow == "owner_row" else ""),
            # Only the follow-up that confirmed. A row read after confirmation is
            # owner_row_after_confirm_t_ms, not this. Neither means the row was accepted:
            # that is cod_reader_stats.json owner_row_accepted.
            "owner_row_seen": follow == "owner_row",
            "consecutive": c["reads"],
            "span_ms": c["formed_t"] - c["t0"],
            "first_t_ms": c["t0"],
            "first_frame_seq": _int(start.get("seq")),
            "follow_up": follow,
            "follow_up_ms": t - c["formed_t"],
            "follow_up_frames": c["frames"],
            "hud_events_in_candidate": c["hud_events"],
            "quiet_ms": t - c["quiet_from"],
        }
        self._cand = None
        return self._stamp(doc, t, src)

    def _owner_any(self) -> bool:
        c = self.confirmed or {}
        return bool(c.get("owner_row_seen")) or self._post_owner_t is not None

    def _after_confirm(self, t, owner, img, src) -> None:
        """Frames after confirmation: keep reading Qortrola30's row until read, and the BEST PLAY title."""
        if not self.cfg.best_play or self.best_play is not None:
            return
        if self._post_last_t is not None and t <= self._post_last_t:
            return
        self._post_last_t = t
        self.post_frames += 1
        if not self._owner_any():
            if owner is None and img is not None and self._owner_reader is not None:
                self.post_owner_reads += 1
                try:
                    owner = self._owner_reader(img)
                except Exception:
                    owner = None
            if owner_row_seen(owner):
                self._post_owner_t = t
        rd = None
        if img is not None and self._bp_reader is not None:
            try:
                rd = self._bp_reader(img)
            except Exception:
                rd = None
        score = getattr(rd, "score", None)
        if isinstance(score, (int, float)) and (self._bp_best is None or score > self._bp_best):
            self._bp_best = float(score)
        if not getattr(rd, "seen", False):
            self._bp_pend = None  # the reads have to be on frames in a row
            return
        p = self._bp_pend
        if p is not None and t - p["first"] <= self.cfg.best_play_pair_ms:
            p["n"] += 1
            p["min_score"] = min(p["min_score"], float(score))
        else:
            p = self._bp_pend = {"first": t, "n": 1, "min_score": float(score), "src": src}
        if p["n"] < self.cfg.best_play_reads:
            return
        c = self.confirmed or {}
        first_src = p["src"] or {}
        self.best_play = {
            "first_t_ms": p["first"],
            "read_t_ms": t,
            "reads": p["n"],
            "min_score": round(p["min_score"], 4),
            "first_frame_seq": _int(first_src.get("seq")),
            "read_frame_seq": _int((src or {}).get("seq")),
            "after_confirm_ms": t - c["confirm_t_ms"] if isinstance(c.get("confirm_t_ms"), int) else None,
        }
        self._bp_mono = float(self._clock())

    def stop_due(self, now: float | None = None) -> bool:
        with self._lock:
            if self._confirm_mono is None:
                return False
            now = float(self._clock()) if now is None else float(now)
            el = now - self._confirm_mono
            if not self.cfg.best_play:
                rule = "tail" if el >= self.cfg.tail_s else None
            elif el >= self.cfg.best_play_max_s:
                rule = "best_play_timeout"
            elif self.best_play is not None and (self._owner_any() or el >= self.cfg.board_floor_s):
                rule = "best_play"
            else:
                rule = None
            if rule is not None and self.stop_rule is None:
                self.stop_rule = rule
            return rule is not None

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
        if c.get("path") == "sustained":
            return (f"Call of Duty {c['result']} end screen confirmed ({c['consecutive']} reads in a row over "
                    f"{c['span_ms'] / 1000:.1f} s, {'owner row read' if c['owner_row_seen'] else 'owner row not read'}).")
        why = {"owner_row": "Qortrola30's row read", "word_again": "the word read again",
               "quiet": f"{c.get('quiet_ms', 0) / 1000:.1f} s without gameplay HUD"}.get(c.get("follow_up"), "")
        return (f"Call of Duty {c['result']} end screen confirmed ({c['consecutive']} splash reads, then {why} "
                f"{c.get('follow_up_ms', 0) / 1000:.1f} s later).")

    def stop_plan(self) -> str:
        """What happens after confirmation, in one sentence."""
        c = self.cfg
        if not c.best_play:
            return f"Capture continues {c.tail_s:g} s, then stops the same way as Enter."
        return (f"Capture continues until the BEST PLAY replay title is read (not before Qortrola30's board row "
                f"is read or {c.board_floor_s:g} s have passed), at most {c.best_play_max_s:g} s, then stops the "
                f"same way as Enter.")

    def log_note(self, stop_reason: str) -> str:
        """One printable-ASCII line for qorcap.log. Practice only, info only."""
        c = self.confirmed or {}
        parts = [
            "practice autoclose",
            f"cod end screen {c.get('result')}",
            f"path {c.get('path')}",
            f"reader {c.get('reader')}",
            f"reads {c.get('consecutive')}",
            f"span_ms {c.get('span_ms')}",
            f"follow_up {c.get('follow_up')} after {c.get('follow_up_ms')} ms",
            f"frame_seq {c.get('confirm_frame_seq')}",
            f"t_ms {c.get('confirm_t_ms')}",
            (f"tail_s {self.cfg.tail_s:g}" if not self.cfg.best_play
             else f"rule {self.stop_rule} best_play_t_ms {(self.best_play or {}).get('read_t_ms')}"),
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
                           "gap_ms": self.cfg.gap_ms, "arm_ms": self.cfg.arm_ms, "tail_s": self.cfg.tail_s,
                           "splash_reads": self.cfg.splash_reads, "splash_pair_ms": self.cfg.splash_pair_ms,
                           "window_ms": self.cfg.window_ms, "quiet_ms": self.cfg.quiet_ms,
                           "resight_ms": self.cfg.resight_ms, "best_play": self.cfg.best_play,
                           "board_floor_s": self.cfg.board_floor_s, "best_play_max_s": self.cfg.best_play_max_s,
                           "best_play_reads": self.cfg.best_play_reads,
                           "best_play_pair_ms": self.cfg.best_play_pair_ms},
                "confirmed": self.confirmed,
                "auto_stopped": bool(fired),
                "stop_reason": stop_reason,
                "stop_wall_ms": _int(stop_wall_ms),
                "frames_seen": self.frames_seen,
                "end_word_frames": self.positives,
                "runs_broken": self.runs_broken,
                "splash_candidates": self.candidates,
                "splash_candidates_cancelled": dict(self.candidates_cancelled),
                "owner_reads_in_candidate": self.owner_reads_attempted,
                "stop_rule": self.stop_rule,
                "best_play": self.best_play,
                "best_play_best_score": None if self._bp_best is None else round(self._bp_best, 4),
                # First post-confirm frame whose row passed the per-frame read (one frame, not accepted).
                "owner_row_after_confirm_t_ms": self._post_owner_t,
                "frames_after_confirm": self.post_frames,
                "owner_reads_after_confirm": self.post_owner_reads,
            }
        # Atomic: a reader never sees a half-written file, and a second write replaces the first.
        path = bundle / FILE_NAME
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
