"""QorCap scorebug sidecar: runs QorTime's own reader inline on QorCap's frames.

    python sidecar/qorcap_reader.py --qortime C:\\path\\to\\qortime [--qorcap http://127.0.0.1:8788]

Pulls /snapshot.jpg from QorCap (4 fps, full card resolution), and uses QorTime's code
unchanged: the vendored Qoresence ScorebugReader + AgreementWindow (>=3 agreeing frames over
>=400 ms), the agent's PauseDetector, and ``qor_receipt.final_detect.detect_final`` (clock_zero with
5 agreeing reads for Madden's red clock / scorebug_gone / final_screen). Madden draws AWAY on the
LEFT and HOME on the right; reads are mapped left->away, right->home exactly as QorTime does.

It POSTs to QorCap /events: score reads (QorCap checkpoints + auto-clips score changes),
obs spans, and one ``final`` event (QorCap saves the final clip and writes the match bundle).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

NOT_VISIBLE = {"layout_unknown", "frame_invalid", "frame_too_small", "frame_aspect"}
QUARTER_SOFT = {"quarter_unsure", "quarter_missing"}
# A game clock that has already reached the final seconds means that period is over. A later "1"
# is not the 1st quarter (match-20261017-091651: 2ND matched the "1" template after the clock reset).
PERIOD_END_CLOCK_S = 5
# 10:00 or higher is a new period (a quarter opens at 15:00). 2:00 is not: that is the 091651 misread.
QUARTER_RESET_CLOCK_S = 10 * 60
# How long a "1" stays unknown after the clock runs down. The window ends on its own, and a real
# quarter of 2 or higher ends it immediately. It must not stay shut for the rest of the match
# (match-20261017-162055: from the Q1/Q2 boundary every read was quarter null).
QUARTER_ONE_SUPPRESS_MS = 15_000
# v0.3.21: short quarters. match-20261017-232726 and match-20261018-002316 were 6:00 quarters (Thursday Night), so a
# reset never reached 10:00 and no period was ever counted. A reset also counts when the clock comes back to within
# RESET_MARGIN_S of the highest clock seen so far (never below MIN_RESET_CLOCK_S, never above the 10:00 rule), but
# only after a real rundown: the read before the first 0:0x was already inside the last RUNDOWN_PREV_MAX_S, or two
# 0:0x reads in a row. A lone 0:0x misread in the middle of a quarter does not end it.
RESET_MARGIN_S = 30
MIN_RESET_CLOCK_S = 60
RUNDOWN_PREV_MAX_S = 120
# Wall-clock stand-in for one period when the game clock never comes back after a rundown.
ELAPSED_PERIOD_MS = 12 * 60 * 1000
# Q4 end menu: the scorebug's last red clock in the final 5 seconds (110028 locked 0:03, then the menu).
END_MENU_RED_CLOCK_S = 5


class PeriodTrack:
    """Clock rundowns and the resets that follow them.

    A posted clock at or under ``PERIOD_END_CLOCK_S`` opens a short window in which a quarter glyph of
    "1" is unknown. A later read of 10:00+ counts as the next period starting. The window expires on
    its own so a stuck "1" cannot blank the quarter for the rest of the game. Three rundowns, or a
    long stretch with no clock after the first rundown, is evidence the match reached the fourth quarter.
    """

    def __init__(self) -> None:
        self.saw_low = False
        self.period_ended = False
        self.period_end_ms: int | None = None
        self.first_period_end_ms: int | None = None
        self.periods_completed = 0
        self.clock_unreadable = False
        self.suppress_until_ms: int | None = None
        self.max_clock_s: int | None = None
        self.prev_clock_s: int | None = None
        self.rundown_confirmed = False

    def reset_clock_s(self) -> int:
        """Clock that opens a new period after a confirmed rundown: 10:00, or near the longest clock seen if shorter."""
        if self.max_clock_s is None:
            return QUARTER_RESET_CLOCK_S
        return max(MIN_RESET_CLOCK_S, min(QUARTER_RESET_CLOCK_S, self.max_clock_s - RESET_MARGIN_S))

    def note(self, t_ms: int, clock_s) -> None:
        if isinstance(clock_s, bool) or not isinstance(clock_s, int):
            if self.first_period_end_ms is not None:
                self.clock_unreadable = True
            return
        self.clock_unreadable = False
        prev, self.prev_clock_s = self.prev_clock_s, int(clock_s)
        if clock_s <= PERIOD_END_CLOCK_S:
            if self.saw_low or (prev is not None and prev <= RUNDOWN_PREV_MAX_S):
                self.rundown_confirmed = True
            if not self.saw_low:
                self.suppress_until_ms = int(t_ms) + QUARTER_ONE_SUPPRESS_MS
                self.period_end_ms = int(t_ms)
                if self.first_period_end_ms is None:
                    self.first_period_end_ms = int(t_ms)
            self.saw_low = True
            self.period_ended = True
            return
        if self.saw_low and (clock_s >= QUARTER_RESET_CLOCK_S
                             or (self.rundown_confirmed and clock_s >= self.reset_clock_s())):
            self.periods_completed += 1
            self.saw_low = False
            self.rundown_confirmed = False
            self.period_ended = False
            self.period_end_ms = int(t_ms)
        self.max_clock_s = int(clock_s) if self.max_clock_s is None else max(self.max_clock_s, int(clock_s))

    def one_suppressed(self, t_ms: int) -> bool:
        """True while a glyph of "1" must stay unknown. The window is bounded."""
        until = self.suppress_until_ms
        if until is None:
            return False
        if int(t_ms) > until:
            self.suppress_until_ms = None
            self.period_ended = False
            return False
        return True

    def release_for_new_quarter(self) -> None:
        """A quarter of 2 or higher was actually read. Stop blanking a later "1" for the old window."""
        self.suppress_until_ms = None
        self.period_ended = False

    def inferred_quarter(self, t_ms: int) -> int | None:
        """Quarter implied by completed resets, or by wall time when the clock stayed unreadable."""
        n = self.periods_completed
        if self.clock_unreadable and self.first_period_end_ms is not None and t_ms >= self.first_period_end_ms:
            n = max(n, int((int(t_ms) - self.first_period_end_ms) // ELAPSED_PERIOD_MS))
        if n <= 0:
            return None
        return min(1 + n, 5)

    def reached_end_of_game(self, t_ms: int) -> bool:
        """The fourth quarter has run down, or the clock died at the Q1/Q2 break and enough time has passed."""
        if self.periods_completed >= 3 and self.saw_low:
            return True
        if self.clock_unreadable and self.periods_completed <= 1 and self.first_period_end_ms is not None:
            return int(t_ms) - self.first_period_end_ms >= 3 * ELAPSED_PERIOD_MS
        return False


class ScoreClockLock:
    """Lock (left score, right score) when the quarter glyph failed.

    Per-digit NCC gates have already run inside the reader. This window is the same shape as QorTime's
    ``AgreementWindow`` except the key does not include a quarter: 3 agreeing samples, >=200 ms apart,
    spanning >=400 ms, gap <=2 s. A clock that runs backwards resets the run. An implausible jump off the
    last locked pair needs 9 samples over 2 s. The quarter on the posted read stays unknown.
    """

    def __init__(self) -> None:
        self.samples: list[tuple[int, Any]] = []
        self.key: tuple[int, int] | None = None
        self.last_pair: tuple[int, int] | None = None
        self.last_pair_ns = 0

    def remember(self, pair, clock_ns: int) -> None:
        if pair is None:
            return
        self.last_pair = (int(pair[0]), int(pair[1]))
        self.last_pair_ns = int(clock_ns)

    def offer(self, read, *, clock_ns: int):
        from types import SimpleNamespace
        from qor_receipt._vendor.qoresence.sync.digit_integrity import implausible_transition_reason
        from qor_receipt._vendor.qoresence.vision.local_scorebug.agreement import AgreementConfig
        cfg = AgreementConfig()
        pair = getattr(read, "pair", None)
        if pair is None:
            self.samples, self.key = [], None
            return SimpleNamespace(agreed=False, read=None, reason=getattr(read, "reason", None) or "blank")
        key = (int(pair[0]), int(pair[1]))
        last = self.samples[-1] if self.samples else None
        restart = (
            self.key != key or last is None or clock_ns - last[0] > cfg.max_gap_ns or clock_ns < last[0]
            or (read.clock_seconds is not None and last[1].clock_seconds is not None
                and read.clock_seconds > last[1].clock_seconds)
        )
        if restart:
            self.key = key
            self.samples = [(clock_ns, read)]
        elif clock_ns - last[0] >= cfg.min_interval_ns:
            self.samples.append((clock_ns, read))
        run = len(self.samples)
        span = self.samples[-1][0] - self.samples[0][0]
        suspicious = bool(
            self.last_pair is not None and self.last_pair != key
            and clock_ns - self.last_pair_ns <= cfg.jump_memory_ns
            and implausible_transition_reason(*self.last_pair, *key)
        )
        need = cfg.need_suspicious if suspicious else cfg.need
        need_span = cfg.min_span_suspicious_ns if suspicious else cfg.min_span_ns
        if run < need or span < need_span:
            return SimpleNamespace(agreed=False, read=None, reason="recheck" if suspicious else "agreeing", run=run)
        self.last_pair = key
        self.last_pair_ns = self.samples[-1][0]
        return SimpleNamespace(agreed=True, read=self.samples[-1][1], reason="agreed", run=run)


READER_SIZE = (640, 360)  # QorTime's reader/profile is calibrated on 640x360 frames


def prep(img):
    """Downscale the card frame (e.g. 1280x720) to the reader's 640x360."""
    import cv2
    if img is None or (img.shape[1], img.shape[0]) == READER_SIZE:
        return img
    return cv2.resize(img, READER_SIZE, interpolation=cv2.INTER_AREA)


def madden_final_screen(img) -> bool:
    """Madden's end-of-game menu (FINISH GAME / VIEW HIGHLIGHTS / ... / RESTART over the final score panel).

    v0.3.3: independent of where the menu cursor is. v0.3.2 required the white highlight bar on the FIRST
    item; on match-20261016-143212 the cursor sat on RESTART (6th item, expanded with a description line)
    for minutes, so the end menu was never seen live and the static screen was swallowed by the pause
    detector. Features (640x360 coordinates, any input size):

    * bottom (y 200-345, minus the gamertag banner rows 295-328) is near-black and flat: end menu mean ~0.5, std ~9; pause menus over the faded
      field std ~18;
    * a white highlight box (>= 8 rows that are >70% bright) somewhere in the menu column x 20-150,
      y 15-130, plus menu text in that column;
    * the column under the menu (y 132-175) is empty: the end menu has 6 items (last one, even highlighted
      and expanded, ends by y~126); the pause menu has 8 (QUIT GAME at y~140).
    Only used behind QorTime's Q4-low-clock gate (``final_screen`` basis), so a halftime menu cannot end a game.
    """
    import numpy as np
    if img is None:
        return False
    g = img.mean(axis=2) if img.ndim == 3 else img.astype(float)
    h, w = g.shape[:2]
    sy, sx = h / 360.0, w / 640.0
    ys = lambda a, b: slice(int(a * sy), int(b * sy))  # noqa: E731
    xs = lambda a, b: slice(int(a * sx), int(b * sx))  # noqa: E731
    # v0.3.5: rows 295-328 skipped: online matches draw the two gamertag banners there (match-20261016-170000)
    bottom = np.vstack([g[ys(200, 295), :], g[ys(328, 345), :]])
    # v0.3.12: Thursday Night's end screen is the stadium, not a black void (mean ~19, std ~12 on
    # match-20261017-091651). Still flat. A faded field (std ~18, or a flat grey of 40) stays rejected.
    if not (bottom.mean() < 25 and (bottom > 60).mean() < 0.02 and bottom.std() < 14):
        return False
    col = g[ys(15, 130), xs(20, 150)]
    box_rows = int((((col > 190).mean(axis=1)) > 0.7).sum())
    if box_rows < int(8 * sy):
        return False
    text = (col > 150).mean()
    below = (g[ys(132, 175), xs(20, 150)] > 90).mean()
    return bool(text > 0.05 and below < 0.004)


class ReaderCore:
    """Frame -> agreed reads / obs spans -> events. Pure logic; I/O injected (unit-testable)."""

    def __init__(self, post: Callable[[dict], Any], reader=None, agree=None, pause=None, detect_final=None,
                 final_screen_detector=None, end_menu_reader=None, end_menu_agree=None, playcall=None, gate=None) -> None:
        self.post, self.reader, self.agree, self.pause = post, reader, agree, pause
        # v0.3.11: pause-capable moments for the official challenges (qorcap_playcall). Optional: the referee's
        # re-read and old callers pass neither.
        self.playcall, self.gate = playcall, gate
        self.detect_final = detect_final
        self.final_screen_detector = final_screen_detector  # QorTime ships none; QorCap: madden_final_screen
        self.timeline: list[dict] = []
        self._absent: dict | None = None
        self._screen: dict | None = None
        self.final: dict | None = None
        self.stats = {"frames": 0, "agreed": 0, "errors": 0, "paused": 0}
        # v0.3.4: the end-of-game menu's score panel is the authoritative final (qorcap_endmenu).
        self.end_menu_reader = end_menu_reader
        if end_menu_reader is not None and end_menu_agree is None:
            from qorcap_endmenu import EndMenuAgreement
            end_menu_agree = EndMenuAgreement()
        self.end_menu_agree = end_menu_agree
        self.end_menu: dict | None = None
        self.end_menu_rejected: dict | None = None
        self.score_lock = ScoreClockLock()
        self._max_quarter: int | None = None  # highest quarter actually posted; a later read may not go backwards
        self.periods = PeriodTrack()
        self._quarter_reject: str | None = None
        # External corroboration for one-legal-step end-menu deltas (set by match runner):
        # each entry: {"kind": "scorebug_final_play"|"score_change_clip"|"input_activity"|"haptic",
        #              "t_ms": int, ...optional "home","away","sha256"}
        self.delta_corroboration: list[dict] = []

    def on_frame(self, t_ms: int, full) -> None:
        """``full`` may be a full-resolution frame (v0.3.5): the scorebug reader gets it when it asks for it
        (``wants_full_res``, e.g. the Monday Night profile); everything else gets the 640x360 reader frame."""
        self.stats["frames"] += 1
        img = prep(full)
        rimg = full if getattr(self.reader, "wants_full_res", False) else img
        # The end-of-game menu is static, so the pause detector would swallow it; check it first.
        if self.final_screen_detector is not None and self.final_screen_detector(img):
            self.stats["final_screen"] = self.stats.get("final_screen", 0) + 1
            self._observe(t_ms, img, True, screen=True)
            self._end_menu(t_ms, img)
            return
        if self.pause is not None:
            self.pause.update(t_ms, img)
            if self.pause.paused:
                self.stats["paused"] += 1
                self._close_spans()
                if self.gate is not None:
                    self.gate.on_frame(t_ms, False, paused=True)
                return
        if self.gate is not None:
            pc = False
            if self.playcall is not None:
                try:
                    pc = self.playcall.detect(img)
                except Exception as e:  # never silent, never fatal
                    print(f"play-call detector error: {type(e).__name__}: {e}", file=sys.stderr)
            if pc:
                self.stats["play_call"] = self.stats.get("play_call", 0) + 1
            ev = self.gate.on_frame(t_ms, pc)
            if ev is not None:
                self.stats["pause_capable"] = self.stats.get("pause_capable", 0) + 1
                print(f"pause-capable moment ({ev.basis}): Q{ev.quarter} -> {ev.phase} challenge may open", file=sys.stderr)
        try:
            fr = self.reader.read(rimg)
            band, _ = self.reader.reference_band(rimg) if hasattr(self.reader, "reference_band") else (None, None)
        except Exception as e:  # never silent
            self.stats["errors"] += 1
            print(f"scorebug reader error: {type(e).__name__}: {e}", file=sys.stderr)
            return
        fr = self._hold_quarter(fr, t_ms)
        absent = (not fr.ok) and fr.reason in NOT_VISIBLE
        self._observe(t_ms, img, absent)
        ch = hashlib.sha256(band.tobytes()).hexdigest()[:16] if band is not None else ""
        # A failed quarter must not be offered to AgreementWindow: that call resets a good run, which is why a
        # whole Thursday Night game produced no score rows. Scores (already per-digit gated) lock on their own.
        soft = (not fr.ok) and fr.reason in QUARTER_SOFT and getattr(fr, "pair", None) is not None
        try:
            if soft:
                res = self.score_lock.offer(fr, clock_ns=t_ms * 1_000_000)
            else:
                res = self.agree.offer(fr, clock_ns=t_ms * 1_000_000, crop_hash=ch, session_id="qorcap")
        except Exception as e:  # never silent
            self.stats["errors"] += 1
            print(f"score agreement error: {type(e).__name__}: {e}", file=sys.stderr)
            return
        if not (res.agreed and res.read is not None):
            return
        if soft and hasattr(self.agree, "last_agreed"):
            self.agree.last_agreed = res.read.pair
            self.agree.last_agreed_ns = t_ms * 1_000_000
        elif not soft:
            self.score_lock.remember(getattr(res.read, "pair", None), t_ms * 1_000_000)
        self.stats["agreed"] += 1
        rd = {"t_ms": t_ms, "away": res.read.left_score, "home": res.read.right_score,  # Madden: AWAY on the LEFT
              "quarter": res.read.quarter, "clock_s": res.read.clock_seconds,
              "clock_red": bool(getattr(res.read, "clock_red", False))}
        prev = next((r for r in reversed(self.timeline) if "obs" not in r), None)
        key = lambda r: (r["home"], r["away"], r["quarter"], r["clock_s"])  # noqa: E731
        if self.gate is not None:
            self.gate.on_read(rd)
        if prev is None or key(prev) != key(rd) or rd["t_ms"] - prev["t_ms"] >= 1_000:
            self.timeline.append(rd)
            self.post({"type": "score", **rd})
            q = rd.get("quarter")
            if isinstance(q, int) and not isinstance(q, bool) and q >= 1:
                self._max_quarter = q if self._max_quarter is None else max(self._max_quarter, q)
            if isinstance(q, int) and not isinstance(q, bool) and q >= 2:
                self.periods.release_for_new_quarter()
            self.periods.note(t_ms, rd.get("clock_s"))
        self._close_spans()  # a fresh score read ends any absence span
        self._check_final(t_ms)

    def _observe(self, t_ms: int, img, absent: bool, screen: bool | None = None) -> None:
        def span(cur, name, on):
            if not on:
                if cur is not None:
                    self.post({"type": "obs", **cur})
                return None
            if cur is None:
                cur = {"t_ms": t_ms, "until_ms": t_ms, "obs": name, "n": 0}
                self.timeline.append(cur)
            cur["until_ms"], cur["n"] = t_ms, cur["n"] + 1
            return cur
        self._absent = span(self._absent, "scorebug_absent", absent)
        if screen is None:
            screen = bool(absent and self.final_screen_detector and self.final_screen_detector(img))
        self._screen = span(self._screen, "final_screen", screen)
        if absent:
            self._check_final(t_ms)

    def _close_spans(self) -> None:
        for s in (self._absent, self._screen):
            if s is not None:
                self.post({"type": "obs", **s})
        self._absent = self._screen = None

    def last_scorebug(self) -> dict | None:
        return next((r for r in reversed(self.timeline) if "obs" not in r and "home" in r), None)

    def _blank_quarter(self, fr, why: str):
        if self._quarter_reject != why:
            print(why, file=sys.stderr)
            self._quarter_reject = why
        try:
            from dataclasses import replace
            return replace(fr, ok=False, reason="quarter_unsure", quarter=None)
        except TypeError:  # local stand-in used when QorTime is not imported
            fr.ok, fr.reason, fr.quarter = False, "quarter_unsure", None
            return fr

    @property
    def _period_ended(self) -> bool:
        return self.periods.period_ended

    def _hold_quarter(self, fr, t_ms: int):
        """A quarter that goes backwards, or a "1" just after the clock has run down, is unknown.

        The score and clock stay, so the lock can continue. match-20261017-091651 posted 27 reads as Q1
        during the real 2nd quarter: the clock had already hit 0:01 with no quarter label, then reset,
        and the 2ND glyph still matched the "1" template. That "1" fails closed, for a bounded window.
        A clock that resets to 15:00, or a quarter of 2 or higher, releases the window. The window also
        expires on its own: match-20261017-162055 left it shut from the Q1/Q2 whistle to the final.
        """
        q = getattr(fr, "quarter", None)
        if isinstance(q, int) and not isinstance(q, bool) and self._max_quarter is not None and q < self._max_quarter:
            return self._blank_quarter(fr, f"quarter {q} rejected: already saw Q{self._max_quarter} (quarters do not go backwards)")
        if isinstance(q, int) and not isinstance(q, bool) and q == 1 and self.periods.one_suppressed(t_ms):
            return self._blank_quarter(fr, "quarter 1 rejected: the game clock already ran down (a new period is not the 1st quarter)")
        return fr

    def _scorebug_for_final(self) -> dict | None:
        """Latest score, with the quarter taken from the last read that actually had one.

        A later lock whose ordinal glyph failed (quarter null) must not erase Q4/OT. That is what dropped the
        Thursday Night end menu on match-20261017-091651: 463 end-menu reads, final null, no rejection line.
        """
        rows = [r for r in self.timeline if "obs" not in r and "home" in r]
        if not rows:
            return None
        last = dict(rows[-1])

        def qof(r):
            q = r.get("quarter")
            if isinstance(q, bool) or not isinstance(q, int):
                return None
            return q if q >= 1 else None

        if qof(last) is None:
            prev = next((r for r in reversed(rows) if qof(r) is not None), None)
            if prev is not None:
                last["quarter"] = qof(prev)
                if last.get("clock_s") is None:
                    last["clock_s"] = prev.get("clock_s")
        return last

    def _quarter_ready_for_end_menu(self, last: dict, t_ms: int) -> bool:
        """Q4 or OT from a scorebug read, or from the period track when the quarter glyph stayed unknown."""
        q_last = last.get("quarter")
        if isinstance(q_last, int) and not isinstance(q_last, bool) and q_last >= 4:
            return True
        return self.periods.reached_end_of_game(t_ms)

    def _clock_ready_for_end_menu(self, last: dict, t_ms: int | None = None) -> bool:
        """Q4 menu once the scorebug clock is 0:00, or already red inside the final 5 seconds.

        OT is ready whenever the quarter is (a walk-off can leave time on the clock). A red 0:14 is
        still a running quarter: match-20261017-110028's menu arrived after the last lock of 0:03 red.
        When the quarter and the clock both died at the Q1/Q2 break, a menu after three period-lengths
        of wall time (or after the fourth-quarter clock has itself run down) is the same gate.
        """
        q_last = last.get("quarter")
        if isinstance(q_last, int) and not isinstance(q_last, bool) and q_last >= 5:
            return True
        if t_ms is not None and self.periods.reached_end_of_game(t_ms):
            return True
        if last.get("clock_s") == 0:
            return True
        rows = [r for r in self.timeline if "obs" not in r and "home" in r
                and isinstance(r.get("clock_s"), int) and not isinstance(r.get("clock_s"), bool)]
        if not rows:
            return False
        c = rows[-1]
        return bool(c.get("clock_red")) and int(c["clock_s"]) <= END_MENU_RED_CLOCK_S

    def _reject_end_menu(self, st: dict, why: str) -> None:
        prev = (self.end_menu_rejected or {}).get("why")
        self.end_menu_rejected = dict(st, why=why)
        if prev != why:
            print(f"END MENU rejected: AWAY {st['away']} - HOME {st['home']}: {why}", file=sys.stderr)

    def _end_menu(self, t_ms: int, img) -> None:
        """Read the end-menu score panel; once stable over consecutive frames, post it as the final."""
        if self.end_menu_reader is None:
            return
        try:
            rd = self.end_menu_reader.read(img)
        except Exception as e:  # never silent
            self.stats["errors"] += 1
            print(f"end-menu reader error: {type(e).__name__}: {e}", file=sys.stderr)
            return
        if not rd.ok and hasattr(self.end_menu_reader, "corroborate"):
            rd = self._corroborate(img, rd)
        k = "end_menu_reads" if rd.ok else "end_menu_unreadable"
        self.stats[k] = self.stats.get(k, 0) + 1
        st = self.end_menu_agree.offer(t_ms, rd)
        if st is None or (self.end_menu and (self.end_menu["away"], self.end_menu["home"]) == (st["away"], st["home"])):
            if st is not None and self.end_menu is not None:
                self.end_menu.update(until_ms=st["until_ms"], frames=st["frames"])
            return
        last = self._scorebug_for_final()
        # Q4 gate (same intent as QorTime's final_screen basis): a game must have reached the 4th quarter.
        # A trailing lock with a null quarter does not erase an earlier Q4, and a clock that died at the
        # Q1/Q2 break can still reach this gate on elapsed time (match-20261017-162055).
        if last is None or not self._quarter_ready_for_end_menu(last, t_ms):
            self._reject_end_menu(st, "no Q4 scorebug read before the end-of-game menu")
            return
        # Clock gate: the end-of-game menu is only authoritative once the scorebug has shown 0:00 in Q4.
        # Without this, a doctored end-menu-looking frame matching the current mid-Q4 score ends the match early.
        period = None
        if hasattr(self.end_menu_reader, "period"):
            try:
                period = self.end_menu_reader.period(img)
            except Exception as e:  # never silent
                print(f"end-menu period error: {type(e).__name__}: {e}", file=sys.stderr)
        # OT can end on a walk-off with time still on the clock (match-20261017-091651: 10-16 at OT 1:55).
        # A Q4 menu with the clock still running is unchanged: it is rejected.
        q_last = last.get("quarter")
        if not self._clock_ready_for_end_menu(last, t_ms):
            self._reject_end_menu(st, "scorebug clock not 0:00 before the end-of-game menu")
            return
        why = end_menu_consistency(st, last)
        if why:
            self._reject_end_menu(st, why)
            return
        if st["home"] == st["away"]:
            self._reject_end_menu(st, "tied score on the end-of-game menu")
            return
        self.end_menu = dict(st)
        if "corroborated" in (st.get("template_sources") or []):
            notes_c = [("end-of-game menu score partly covered (online name tags): the scorebug final was corroborated "
                        "from the lower part of the end-menu digits against every one-play alternative")]
        else:
            notes_c = []
        sb = {"home": last["home"], "away": last["away"], "quarter": last["quarter"], "clock_s": last.get("clock_s"),
              "t_ms": last["t_ms"]}
        notes = list(notes_c)
        corr = end_menu_delta_corroborated(st, last, self.timeline, self.delta_corroboration)
        confirmed = corr is not None and corr.get("kind") != "no_scorebug"
        if (sb["away"], sb["home"]) != (st["away"], st["home"]):
            notes.append(f"final play score not shown on scorebug: last scorebug read AWAY {sb['away']} - HOME {sb['home']}"
                         f", end-of-game menu AWAY {st['away']} - HOME {st['home']}")
            if confirmed and corr:
                notes.append(f"one-legal-step delta corroborated via {corr.get('kind')}")
            else:
                notes.append("one-legal-step delta NOT corroborated (no scorebug final-play read / "
                             "score-change clip / input-haptic near scoring): final_confirmed=false")
                confirmed = False
        prev = self.final
        quarter = max(4, int(q_last) if isinstance(q_last, int) else 4)
        if period == "OT" or (isinstance(q_last, int) and q_last >= 5):
            quarter = max(quarter, 5)
        self.final = {"type": "final", "basis": "end_menu", "method": "end_menu_read", "home": st["home"], "away": st["away"],
                      "quarter": quarter, "t_ms": st["t_ms"], "end_menu": st, "scorebug_last": sb,
                      "notes": notes, "final_confirmed": confirmed,
                      "delta_corroboration": corr}
        if prev is not None:
            self.final["amends"] = {k: prev.get(k) for k in ("basis", "home", "away", "t_ms")}
        self._close_spans()
        tag = "CONFIRMED" if confirmed else "UNCONFIRMED"
        print(f"FINAL (end-of-game menu, {st['frames']} frames, {tag}): away {st['away']} - home {st['home']}"
              + (f"  [{notes[0]}]" if notes else ""), file=sys.stderr)
        self.post(self.final)

    def _corroborate(self, img, rd):
        """v0.3.6: a covered end-menu panel can only CONFIRM the scorebug's Q4 final (never read a new score)."""
        last = self.last_scorebug()
        if last is None or (last.get("quarter") or 0) < 4 or self.final is None:
            return rd
        try:
            c = self.end_menu_reader.corroborate(img, {"away": last["away"], "home": last["home"]})
        except Exception as e:  # never silent
            print(f"end-menu corroboration error: {type(e).__name__}: {e}", file=sys.stderr)
            return rd
        self.end_menu_corroboration = c
        if not c.get("ok"):
            return rd
        from qorcap_endmenu import EndMenuRead
        sc = min(v["score"] for v in c["sides"].values())
        mg = min(v["score"] - v["alt_score"] for v in c["sides"].values())
        return EndMenuRead(True, last["away"], last["home"], "corroborated", sc, mg,
                           [dict(side=k, source="corroborated", xcheck="n/a", **v) for k, v in c["sides"].items()])

    def _check_final(self, t_ms: int) -> None:
        if self.final is not None:
            return
        last = self.last_scorebug()
        if last is not None and last.get("quarter") is None:
            return  # a quarter-less lock is not evidence the game ended, and detect_final compares quarter as an int
        fin = self.detect_final(self.timeline)
        if fin.final:
            self._close_spans()
            self.final = {"type": "final", "basis": fin.basis, "home": fin.home, "away": fin.away,
                          "quarter": fin.quarter, "t_ms": fin.t_ms}
            print(f"FINAL ({fin.basis}): away {fin.away} - home {fin.home}"
                  + ("  (scorebug; waiting for the end-of-game menu to confirm)" if self.end_menu_reader else ""), file=sys.stderr)
            self.post(self.final)


LEGAL_STEPS = {1, 2, 3, 6, 7, 8}  # same as qortime referee_ai.integrity.LEGAL_STEPS


def end_menu_delta(st: dict, last: dict | None) -> list[int] | None:
    """Return [d_away, d_home] or None if last is missing."""
    if last is None:
        return None
    return [st["away"] - last["away"], st["home"] - last["home"]]


def end_menu_one_legal_step(d: list[int]) -> bool:
    return min(d) >= 0 and sorted(d)[0] == 0 and max(d) in LEGAL_STEPS


def end_menu_consistency(st: dict, last: dict | None) -> str | None:
    """None when the end-menu score equals the last scorebug read or is ONE legal scoring play past it on one side.
    A one-legal-step *delta* still needs corroboration before ``final_confirmed`` (see ``end_menu_delta_corroborated``);
    this function only rejects impossible jumps (digit-splice outside one play)."""
    if last is None:
        return None
    d = end_menu_delta(st, last)
    assert d is not None
    if d == [0, 0] or end_menu_one_legal_step(d):
        return None
    return (f"end-of-game menu AWAY {st['away']} - HOME {st['home']} is not one legal scoring play past the last "
            f"scorebug read AWAY {last['away']} - HOME {last['home']}")


def end_menu_delta_corroborated(st: dict, last: dict | None, timeline: list[dict] | None,
                                external: list[dict] | None) -> dict | None:
    """For a one-legal-step end-menu delta, return a corroboration record or None.

    Acceptable evidence (any one):
    - a prior scorebug timeline read that already equals the end-menu score (final play briefly drawn)
    - an external score-change clip hash bound near the scoring time
    - input / haptic activity flagged near the last scorebug / end-menu window
    Exact match (no delta) needs no corroboration and returns {"kind": "exact_match"}.
    """
    if last is None:
        return {"kind": "no_scorebug"}
    d = end_menu_delta(st, last)
    assert d is not None
    if d == [0, 0]:
        return {"kind": "exact_match"}
    if not end_menu_one_legal_step(d):
        return None
    # Timeline: any agreed scorebug read matching end-menu scores
    for r in reversed(timeline or []):
        if "home" in r and "obs" not in r and (r.get("home"), r.get("away")) == (st["home"], st["away"]):
            return {"kind": "scorebug_final_play", "t_ms": r.get("t_ms"), "home": r["home"], "away": r["away"]}
    lo = int(last.get("t_ms") or 0) - 30_000
    hi = int(st.get("t_ms") or last.get("t_ms") or 0) + 120_000
    for ev in external or []:
        kind = ev.get("kind") or ""
        t = ev.get("t_ms")
        if t is not None and not (lo <= int(t) <= hi):
            continue
        if kind in ("score_change_clip", "input_activity", "haptic", "scorebug_final_play"):
            if kind == "score_change_clip" and not ev.get("sha256"):
                continue
            return {"kind": kind, **{k: ev[k] for k in ev if k != "kind"}}
    return None



def collect_delta_corroboration(*, clips: list[dict] | None = None,
                                haptic_records: list[dict] | None = None,
                                input_records: list[dict] | None = None) -> list[dict]:
    """Build ``ReaderCore.delta_corroboration`` entries from live API clips and bundle logs.

    Same kinds as ``end_menu_delta_corroborated`` accepts:
    - score_change_clip (needs sha256; t_ms from wall_ms or t_ms)
    - haptic (t_ms from t_wall_ms / t_mono_ms / t_us)
    - input_activity (significant button presses near scoring)
    """
    out: list[dict] = []
    for c in clips or []:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or "")
        sha = c.get("sha256")
        if not sha or len(str(sha)) < 32:
            continue
        # score-* auto clips and explicit score_change names
        if not (name.startswith("score") or name in ("score_change", "score-change") or "score-" in name):
            continue
        t_ms = c.get("t_ms")
        if t_ms is None:
            t_ms = c.get("wall_ms")
        if t_ms is None:
            continue
        out.append({"kind": "score_change_clip", "t_ms": int(t_ms), "sha256": str(sha).lower(),
                    "name": name, "file": c.get("file")})
    for h in haptic_records or []:
        if not isinstance(h, dict):
            continue
        if h.get("type") not in (None, "haptic"):
            continue
        t_ms = h.get("t_ms")
        if t_ms is None and h.get("t_wall_ms") is not None:
            t_ms = h["t_wall_ms"]
        if t_ms is None and h.get("t_mono_ms") is not None:
            t_ms = h["t_mono_ms"]
        if t_ms is None and h.get("t_us") is not None:
            t_ms = int(h["t_us"]) // 1000
        if t_ms is None:
            continue
        out.append({"kind": "haptic", "t_ms": int(t_ms),
                    "confidence": h.get("confidence") or h.get("high_confidence")})
    for r in input_records or []:
        if not isinstance(r, dict) or r.get("type") not in ("in", "press", "buttons"):
            continue
        # Prefer records that look like discrete presses (not stick noise)
        t_ms = r.get("t_ms")
        if t_ms is None and r.get("t_us") is not None:
            t_ms = int(r["t_us"]) // 1000
        if t_ms is None:
            continue
        buttons = r.get("buttons") if r.get("buttons") is not None else r.get("b")
        if buttons is None:
            buttons = r.get("pressed") or r.get("pad")
        if buttons in (None, 0, [], {}, ""):
            continue
        out.append({"kind": "input_activity", "t_ms": int(t_ms)})
    return out


def open_shared_text(path, encoding="utf-8"):
    """Read a file without denying writers.

    The live sidecar re-reads ``inputs.jsonl`` every second while the engine appends it. On Windows a
    read that does not share writes makes the append fail with a sharing violation (os error 32) and
    the input hash chain breaks. ``FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE`` lets the
    append through. Other platforms use a normal text open.
    """
    import os
    if os.name != "nt":
        return open(path, "r", encoding=encoding, errors="replace")
    import ctypes
    import msvcrt
    from ctypes import wintypes
    share = 0x1 | 0x2 | 0x4  # FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                              wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k.CreateFileW.restype = wintypes.HANDLE
    h = k.CreateFileW(os.fspath(path), 0x80000000, share, None, 3, 0x80, None)
    if h is None or int(ctypes.cast(h, ctypes.c_void_p).value or 0) in (0, 0xFFFFFFFF, -1):
        raise OSError(ctypes.get_last_error(), f"cannot open {path}")
    fd = msvcrt.open_osfhandle(int(ctypes.cast(h, ctypes.c_void_p).value), os.O_RDONLY | os.O_BINARY)
    return open(fd, "r", encoding=encoding, errors="replace", closefd=True)


def load_delta_corroboration_from_bundle(bundle) -> list[dict]:
    """Read clips (manifest/bundle.json) + haptics.jsonl + inputs.jsonl from a match bundle dir."""
    from pathlib import Path
    b = Path(bundle)
    clips: list[dict] = []
    # Prefer live-written clips on bundle.json; fall back to manifest.jsonl clip lines
    bj = b / "bundle.json"
    if bj.exists():
        try:
            import json
            with open_shared_text(bj) as fh:
                clips.extend(json.loads(fh.read()).get("clips") or [])
        except Exception:
            pass
    man = b / "manifest.jsonl"
    if man.exists():
        with open_shared_text(man) as fh:
            man_text = fh.read()
        for line in man_text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                import json
                o = json.loads(line)
            except Exception:
                continue
            if o.get("type") == "clip":
                clips.append(o)
    # Also scan clips/ directory for score-*.mp4 and hash if missing sha
    cdir = b / "clips"
    if cdir.is_dir():
        import hashlib
        known = {c.get("file") for c in clips if isinstance(c, dict)}
        for f in sorted(cdir.glob("*.mp4")):
            if not (f.name.startswith("score") or "score-" in f.name):
                continue
            if str(f) in known or any(str(f.name) in str(c.get("file") or "") for c in clips):
                continue
            try:
                sha = hashlib.sha256(f.read_bytes()).hexdigest()
            except Exception:
                continue
            # mtime as rough t_ms fallback (wall clock ms)
            t_ms = int(f.stat().st_mtime * 1000)
            clips.append({"type": "clip", "name": f.stem, "file": str(f), "sha256": sha, "wall_ms": t_ms})

    def _jsonl(name: str) -> list[dict]:
        p = b / name
        if not p.exists():
            return []
        import json
        rows = []
        with open_shared_text(p) as fh:
            text = fh.read()
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
        return rows

    return collect_delta_corroboration(clips=clips, haptic_records=_jsonl("haptics.jsonl"),
                                      input_records=_jsonl("inputs.jsonl"))


def http_post(base: str, body: dict) -> dict:
    rq = urllib.request.Request(base + "/events", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(rq, timeout=5) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"post {body.get('type')} failed: {e}", file=sys.stderr)
        return {}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--qorcap", default="http://127.0.0.1:8788")
    ap.add_argument("--qortime", help="path to a qortime checkout (if qor_receipt is not pip-installed)")
    ap.add_argument("--hz", type=float, default=4.0)
    a = ap.parse_args(argv)
    if a.qortime:
        sys.path.insert(0, str(Path(a.qortime).resolve()))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import cv2
    import numpy as np
    from qor_receipt._vendor.qoresence.vision.local_scorebug.agreement import AgreementWindow
    from qor_receipt.agent.pause_detect import PauseDetector
    from qor_receipt.final_detect import detect_final

    from qorcap_endmenu import EndMenuReader
    from qorcap_mnp import AutoProfileReader
    from qorcap_standard import standard_reader
    core = ReaderCore(lambda b: http_post(a.qorcap, b), AutoProfileReader(standard_reader(), prep=prep), AgreementWindow(),
                      PauseDetector(), detect_final,
                      final_screen_detector=madden_final_screen, end_menu_reader=EndMenuReader())
    last, period, t_print = None, 1.0 / a.hz, time.monotonic()
    print(f"qorcap scorebug sidecar -> {a.qorcap}", file=sys.stderr)
    while True:
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(a.qorcap + "/snapshot.jpg", timeout=3) as r:
                data = r.read()
        except Exception:
            time.sleep(1)
            continue
        h = hashlib.sha256(data).digest()
        if h != last:
            last = h
            img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
            if img is not None:
                core.on_frame(int(time.time() * 1000), img)
        if time.monotonic() - t_print > 30:
            t_print = time.monotonic()
            print(f"health {core.stats} timeline={len(core.timeline)}", file=sys.stderr)
        time.sleep(max(0.0, period - (time.monotonic() - t0)))


if __name__ == "__main__":
    sys.exit(main())
