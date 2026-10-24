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
import os
import sys
import threading
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
# A final_screen observation is posted only once it has lasted as long as QorTime's detect_final
# already requires (3 frames and 3 seconds). A shorter flash is dropped and is not posted, so it
# cannot mark the session closed. A real end menu stays up for minutes and still commits.
FINAL_SCREEN_MIN_FRAMES = 3
FINAL_SCREEN_MIN_SPAN_MS = 3000
# DualSense OPTIONS is button bit 9 (src/hid.rs). Records live in inputs.jsonl field "b".
OPTIONS_BIT = 1 << 9
# The dark picture must start within this long after OPTIONS, and the last scorebug read must
# still have been on screen at the press (the scorebug leaves about 0.8 s after OPTIONS).
OPTIONS_MENU_LEAD_MS = 4000
SCOREBUG_VISIBLE_MS = 3000
# A real ending that happened to coincide with OPTIONS is still accepted once that same picture
# has stayed up this long and the end-of-game clock gates hold. A scripted pause is a few seconds.
OPTIONS_COINCIDENT_HOLD_MS = 12000
# AgreementWindow restarts a run when two samples are more than max_gap_ns apart (2 s). A wider
# gap would weaken the vote, so a slow frame loop is reported and is never turned into a score.
AGREE_GAP_MS_DEFAULT = 2000.0
# The bar can be readable for this long with no agreed row before that state is logged.
UNAGREED_MS = 10_000
# A single in-budget frame must not close a stall span. Alternating gaps otherwise
# open and close a row on every pair. Three quiet frames is the same count the
# end-menu lock uses, and it is still well under a second at 4 Hz.
HEALTH_CLEAR_FRAMES = 3
# One console warning per open span, and never again inside this window of frame time.
# A closed span is held and merged with later spans of the same kind that start
# inside 30 s of the held row's first t_ms. The end of the span does not move that
# window, so a flap still writes about one row per 30 s. Whatever is still held
# or open is written when the reader exits.
HEALTH_WARN_MS = 30_000


def _seconds_text(ms: float) -> str:
    """Frame gap as seconds. 2001 ms is '2.001'; 2100 ms is '2.1'."""
    text = f"{float(ms) / 1000.0:.3f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


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


# v0.3.22: a touchdown that the bar shows only briefly. match-20261018-002316 ended on a TD at 2ND 0:07 (21-31 ->
# 21-37): the bar showed 21-37 on 8 of 24 frames between a TOUCHDOWN banner and the menus, and only 2 consecutive
# frames read the clock, so neither window locked and the offline receipt fell back to 21-31.
TD_DELTAS = (6, 7, 8)
TD_NEED = 4                 # reads of the new pair
TD_MIN_SPAN_MS = 750
TD_WINDOW_MS = 8_000        # first to last read of the new pair
TD_MIN_INTERVAL_MS = 200
# v0.3.23 safeguard (local only). match-20261017-021003: the standard bar showed QOR 13 - EVE 0 and v0.3.22 locked
# 13-8: the "0" matched the "8" template on 4 frames in a row (score margin ~0.09, conf ~0.83; real reads of that
# bar: margin ~0.21, conf ~0.90), and 13-0 came straight back 250 ms after the lock. So:
# * every fast-path read that carries a template margin needs at least TD_MIN_MARGIN (Thursday Night 21-37 on
#   002316: 0.26-0.41), and at least TD_MIN_MARGIN_READS of them must carry one; a weaker read cancels the pair;
# * a jump that only swaps one glyph for a look-alike (0->6, 0->8, 0->9, 3->9: closed loops) needs a margin on
#   every read of at least TD_CONFUSABLE_MARGIN and TD_CONFUSABLE_CONFIRM more reads of the new pair during the hold;
# * after the reads agree, the lock is held for TD_HOLD_MS (TD_CONFUSABLE_HOLD_MS): one read of the old pair or of a
#   third pair in that time cancels it. Frames with no bar (banner, replay, menus) neither confirm nor cancel.
TD_MIN_MARGIN = 0.15
TD_MIN_MARGIN_READS = 2
TD_CONFUSABLE = frozenset({(0, 6), (0, 8), (0, 9), (3, 9)})
TD_CONFUSABLE_MARGIN = 0.18
TD_CONFUSABLE_CONFIRM = 2
TD_HOLD_MS = 1_000
TD_CONFUSABLE_HOLD_MS = 2_000
TD_HOLD_MAX_MS = 6_000      # a confusable jump that never shows again is dropped (the normal windows still apply)
# Glimpse second opinion, ENFORCING mode (off by default; needs Con's explicit OK; QORCAP_GLIMPSE_TD_VETO=1):
# a held fast-path lock may wait up to this long for a verdict. Glimpse can only veto or delay, never create a lock.
TD_VETO_WAIT_MS = 4_000


def td_confusable(old: int, new: int) -> bool:
    """True when ``new`` differs from ``old`` in exactly one digit and that digit pair is a look-alike."""
    a, b = str(int(old)), str(int(new))
    if len(a) != len(b):
        return False
    diff = [(int(x), int(y)) for x, y in zip(a, b) if x != y]
    return len(diff) == 1 and diff[0] in TD_CONFUSABLE


class TouchdownJump:
    """Lock a +6/+7/+8 on one side, other side unchanged, from fewer agreeing reads, only when corroborated.

    Every frame that shows the bar must agree: any read of the old pair or of a third pair after the new pair
    appeared cancels it, as does a quarter that differs from the posted quarter or a clock above the last posted
    clock (or rising between the new reads); a cancelled pair stays on the normal windows until the next normal
    lock. Frames with no bar (banner, replay) neither count nor cancel. Needs
    ``TD_NEED`` reads >= ``TD_MIN_INTERVAL_MS`` apart spanning ``TD_MIN_SPAN_MS`` within ``TD_WINDOW_MS``.
    v0.3.23: template-margin gate, look-alike rule and a hold (see the constants). ``offer`` never posts; ``due``
    returns the held read once the hold is over. Only consulted when the normal windows did not agree on the frame.
    """

    def __init__(self, listener=None, veto=None) -> None:
        self.cand: tuple[int, int] | None = None
        self.reads: list[tuple[int, Any]] = []
        self.blocked: set[tuple[int, int]] = set()   # contradicted once: normal windows only until the next lock
        self.pending: dict | None = None
        self.listener = listener   # shadow log only (Glimpse second opinion); never changes a lock
        self.veto = veto           # ENFORCING mode only; None = off (default)

    def _emit(self, ev: dict) -> None:
        if self.listener is None:
            return
        try:
            self.listener(dict(ev, schema="qorcap-td-jump/v1"))
        except Exception as e:  # never fatal, never silent
            print(f"touchdown listener error: {type(e).__name__}: {e}", file=sys.stderr)

    def reset(self) -> None:
        """A normal lock happened: start over."""
        if self.pending is not None:
            self._emit({"event": "superseded", "cand": list(self.pending["cand"]), "old": list(self.pending["old"]),
                        "id": self.pending["id"]})
        self.cand, self.reads, self.blocked, self.pending = None, [], set(), None

    def _cancel(self, why: str = "contradicted", t_ms: int | None = None) -> None:
        if self.pending is not None:
            self.blocked.add(self.pending["cand"])
            self._emit({"event": "cancelled", "why": why, "t_ms": t_ms, "cand": list(self.pending["cand"]),
                        "old": list(self.pending["old"]), "id": self.pending["id"]})
            self.pending = None
        if self.cand is not None:
            self.blocked.add(self.cand)
        self.cand, self.reads = None, []

    @staticmethod
    def _int(v):
        return v if isinstance(v, int) and not isinstance(v, bool) else None

    @staticmethod
    def _margin(fr):
        m = getattr(fr, "score_margin", None)
        return float(m) if isinstance(m, (int, float)) and not isinstance(m, bool) else None

    def _pending_offer(self, fr, t_ms: int, last: dict | None, posted_quarter: int | None) -> None:
        pd = self.pending
        a, h = int(fr.pair[0]), int(fr.pair[1])
        if (a, h) != pd["cand"]:
            self._cancel("old_pair" if (a, h) == pd["old"] else "third_pair", t_ms)
            return
        q = self._int(getattr(fr, "quarter", None))
        if q is not None and posted_quarter is not None and q != posted_quarter:
            self._cancel("quarter", t_ms)
            return
        c, lc = self._int(getattr(fr, "clock_seconds", None)), self._int((last or {}).get("clock_s"))
        if c is not None and lc is not None and c > lc:
            self._cancel("clock", t_ms)
            return
        m = self._margin(fr)
        if m is not None and m < pd["min_margin"]:
            self._cancel("weak_margin", t_ms)
            return
        if t_ms - pd["last_t"] >= TD_MIN_INTERVAL_MS:
            pd["confirms"] += 1
            pd["last_t"] = t_ms

    def offer(self, fr, t_ms: int, last: dict | None, posted_quarter: int | None):
        """Feed one frame's read. Returns None (v0.3.23: the lock comes from ``due``)."""
        pair = getattr(fr, "pair", None)
        if pair is None:
            return None
        if self.pending is not None:
            self._pending_offer(fr, t_ms, last, posted_quarter)
            return None
        if last is None or self._int(last.get("away")) is None or self._int(last.get("home")) is None:
            self._cancel()
            return None
        a, h = int(pair[0]), int(pair[1])
        la, lh = int(last["away"]), int(last["home"])
        da, dh = a - la, h - lh
        if not ((da == 0 and dh in TD_DELTAS) or (dh == 0 and da in TD_DELTAS)):
            self._cancel()                    # the old pair, or anything else: the bar disagrees
            return None
        if (a, h) in self.blocked:
            return None
        if self.cand is not None and self.cand != (a, h):
            self._cancel()                    # a second candidate pair: neither is corroborated
            self.blocked.add((a, h))
            return None
        q = self._int(getattr(fr, "quarter", None))
        if q is not None and posted_quarter is not None and q != posted_quarter:
            self.cand = (a, h)
            self._cancel()
            return None
        c, lc = self._int(getattr(fr, "clock_seconds", None)), self._int(last.get("clock_s"))
        if c is not None and lc is not None and c > lc:
            self.cand = (a, h)
            self._cancel()
            return None
        conf = td_confusable(la, a) if da else td_confusable(lh, h)
        floor = TD_CONFUSABLE_MARGIN if conf else TD_MIN_MARGIN
        m = self._margin(fr)
        if (m is not None and m < floor) or (conf and m is None):
            self.cand = (a, h)
            self._cancel()                    # a weak (or, for a look-alike, unmeasured) glyph match: no fast path
            return None
        if self.reads and t_ms - self.reads[0][0] > TD_WINDOW_MS:
            self._cancel()                    # too slow to be one brief showing: normal windows only
            return None
        if self.cand is None:
            self.cand, self.reads = (a, h), []
        if self.reads:
            pc = next((self._int(r.clock_seconds) for _, r in reversed(self.reads)
                       if self._int(getattr(r, "clock_seconds", None)) is not None), None)
            if c is not None and pc is not None and c > pc:
                self._cancel()
                return None
            if t_ms - self.reads[-1][0] < TD_MIN_INTERVAL_MS:
                return None
        self.reads.append((t_ms, fr))
        if len(self.reads) >= TD_NEED and self.reads[-1][0] - self.reads[0][0] >= TD_MIN_SPAN_MS:
            margins = [self._margin(r) for _, r in self.reads]
            if sum(x is not None for x in margins) < TD_MIN_MARGIN_READS:
                return None                   # keep collecting: not enough measured glyph matches yet
            got = self.reads[-1][1]
            hold = TD_CONFUSABLE_HOLD_MS if conf else TD_HOLD_MS
            self.pending = {"cand": (a, h), "old": (la, lh), "read": got, "t0": t_ms, "due": t_ms + hold,
                            "last_t": t_ms, "confirms": 0, "need": TD_CONFUSABLE_CONFIRM if conf else 0,
                            "confusable": conf, "min_margin": floor, "id": f"td{t_ms}_{a}_{h}", "verdict": None}
            self._emit({"event": "pending", "id": self.pending["id"], "t_ms": t_ms, "cand": [a, h], "old": [la, lh],
                        "confusable": conf, "hold_ms": hold, "margins": margins,
                        "quarter": self._int(getattr(got, "quarter", None))})
            self.cand, self.reads = None, []
        return None

    def due(self, t_ms: int):
        """The held read once its hold is over (and, for a look-alike, confirmed), else None."""
        pd = self.pending
        if pd is None or t_ms < pd["due"]:
            return None
        if pd["confirms"] < pd["need"]:
            if t_ms - pd["t0"] > TD_HOLD_MAX_MS:
                self._cancel("unconfirmed", t_ms)
            return None
        if self.veto is not None:             # ENFORCING mode (off by default): may veto or delay, never create
            try:
                v = self.veto(dict(pd))
            except Exception as e:  # a broken veto never blocks a lock forever
                print(f"touchdown veto error: {type(e).__name__}: {e}", file=sys.stderr)
                v = None
            if v == "disagree":
                self._cancel("glimpse_veto", t_ms)
                return None
            if v != "agree" and t_ms - pd["due"] < TD_VETO_WAIT_MS:
                return None
        got = pd["read"]
        self._emit({"event": "posted", "id": pd["id"], "t_ms": t_ms, "cand": list(pd["cand"]), "old": list(pd["old"]),
                    "confirms": pd["confirms"]})
        self.pending = None
        self.blocked.discard(pd["cand"])
        return got


# Glimpse second opinion on held touchdown jumps. SHADOW mode (default): the sidecar only writes the held candidates to
# the watcher's own folder (%LOCALAPPDATA%\QorCapGlimpse\<match>\td_jumps.jsonl, never the match folder); the
# watcher logs Glimpse's read of the next frame. Nothing comes back into the lock or the receipt.
# ENFORCING mode is OFF unless QORCAP_GLIMPSE_TD_VETO=1, and must not be turned on without Con's explicit OK: then a
# held jump waits up to TD_VETO_WAIT_MS for the watcher's verdict; "disagree" cancels that fast-path lock (the normal
# windows still apply), "agree" posts it, no verdict posts it after the wait. It can only veto or delay, never create.
TD_VETO_ENV = "QORCAP_GLIMPSE_TD_VETO"


def glimpse_match_dir(match_name: str) -> Path:
    import os
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(base) / "QorCapGlimpse" / match_name


def td_shadow_listener(match_name: str):
    out = glimpse_match_dir(match_name)

    def write(ev: dict) -> None:
        out.mkdir(parents=True, exist_ok=True)
        with open(out / "td_jumps.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(dict(ev, t_wall_ms=int(time.time() * 1000)), default=str) + "\n")
    return write


def td_veto_enabled() -> bool:
    import os
    return os.environ.get(TD_VETO_ENV, "").strip() == "1"


def td_glimpse_veto(match_name: str):
    """ENFORCING mode reader of the watcher's verdicts. Only wired when ``td_veto_enabled()``."""
    path = glimpse_match_dir(match_name) / "td_verdicts.jsonl"

    def veto(pd: dict):
        found = None
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        v = json.loads(line)
                    except ValueError:
                        continue
                    if v.get("id") != pd.get("id"):
                        continue
                    verdict = v.get("verdict")
                    # A later disagree must not be hidden by an earlier no_read or agree.
                    # Disagree only cancels a lock the reader already held. It cannot create one.
                    if isinstance(verdict, str) and verdict.startswith("disagree"):
                        return "disagree"
                    if verdict == "agree":
                        found = "agree"
        except OSError:
            return None
        return found
    return veto


def wire_touchdown_glimpse(core, match_name: str | None) -> str:
    """Live only. Returns the mode: "off" (no match), "shadow", or "enforcing"."""
    if core.touchdown is None or not match_name:
        return "off"
    core.touchdown.listener = td_shadow_listener(match_name)
    if td_veto_enabled():
        core.touchdown.veto = td_glimpse_veto(match_name)
        return "enforcing"
    return "shadow"


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
    if img is None or (img.shape[1], img.shape[0]) == READER_SIZE:
        return img
    import cv2
    return cv2.resize(img, READER_SIZE, interpolation=cv2.INTER_AREA)


def _new_fold_log() -> dict:
    """One 'skipped a row' line per inputs file, not per process."""
    return {"logged": False}


def _skip_fold_row(log: dict, exc: BaseException) -> None:
    """One line for a bad inputs row. The rest of the batch is still folded."""
    if log.get("logged"):
        return
    log["logged"] = True
    print(f"inputs fold skipped a row: {type(exc).__name__}: {exc}", file=sys.stderr)


def _fold_options(records, presses: list[int], prev: bool, anchor, log=None):
    """Fold ``records`` into an OPTIONS rising-edge list. ``presses`` is updated in place."""
    if log is None:
        log = _new_fold_log()
    for r in records:
        try:
            if not isinstance(r, dict):
                continue
            if r.get("type") == "header":
                w = r.get("wall_anchor_ms")
                if anchor is None and isinstance(w, int) and not isinstance(w, bool):
                    anchor = w
                continue
            if r.get("type") != "in":
                continue
            b = r.get("b")
            if isinstance(b, bool) or not isinstance(b, int):
                prev = False
                continue
            down = bool(b & OPTIONS_BIT)
            if down and not prev:
                t_ms = r.get("t_ms")
                t_us = r.get("t_us")
                if isinstance(t_ms, int) and not isinstance(t_ms, bool):
                    presses.append(t_ms)
                elif anchor is not None and isinstance(t_us, int) and not isinstance(t_us, bool):
                    presses.append(int(anchor + t_us // 1000))
            prev = down
        except Exception as e:
            _skip_fold_row(log, e)
    return presses, prev, anchor


def _fold_activity(records, log=None) -> list[dict]:
    """Input rows that ``collect_delta_corroboration`` would emit, in the same order."""
    if log is None:
        log = _new_fold_log()
    out: list[dict] = []
    for r in records or []:
        try:
            if not isinstance(r, dict) or r.get("type") not in ("in", "press", "buttons"):
                continue
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
        except Exception as e:
            _skip_fold_row(log, e)
    return out


def options_presses_ms(records, wall_anchor_ms=None) -> list[int]:
    """Rising edges of OPTIONS (button bit 9) as wall-clock milliseconds.

    ``records`` are ``inputs.jsonl`` objects. Wall time is ``wall_anchor_ms + t_us/1000`` from the
    header. A record that already carries ``t_ms`` uses that.
    """
    presses, _, _ = _fold_options(records, [], False, wall_anchor_ms)
    return presses


def _parse_json_line(raw: bytes):
    """One jsonl line, or None when it is empty or not JSON yet."""
    if raw.endswith(b"\r"):
        raw = raw[:-1]
    line = raw.strip()
    if not line:
        return None
    try:
        return json.loads(line.decode("utf-8", "replace"))
    except Exception:
        return None


# Bytes kept from the start of the file, and from just before the read offset.
# A rewrite that keeps the prefix but changes the tail (truncate-then-regrow, inode
# reuse, in-place replace) fails the second check and is parsed again from byte 0.
_TAIL_MARK = 64


class JsonlTail:
    """Byte-offset tail of a jsonl file.

    Complete lines are kept. A trailing partial line stays in the buffer until a
    newline, and is included in the snapshot when it is already valid JSON so the
    snapshot matches a full parse of the bytes on disk. A shrink, a new inode, a
    changed prefix, or a changed window just before the read offset rereads from
    byte 0. ``snapshot`` and ``take`` share one lock; the end-of-match check and
    the refresh thread can both touch a cached tail.
    """

    def __init__(self, path) -> None:
        self.path = Path(path)
        self.off = 0
        self.buf = b""
        self.rows: list = []
        self.dev = None
        self.ino = None
        self.prefix: bytes | None = None
        self.tail_mark: bytes | None = None
        self._lock = threading.Lock()

    def _reset(self) -> None:
        self.off = 0
        self.buf = b""
        self.rows = []
        self.dev = None
        self.ino = None
        self.prefix = None
        self.tail_mark = None

    def snapshot(self) -> list:
        """Parsed records equal to a full parse of the file as it is now."""
        with self._lock:
            reset, new = self._pull()
            if reset:
                self.rows = list(new)
            else:
                self.rows.extend(new)
            extra = _parse_json_line(self.buf)
            if extra is None:
                return list(self.rows)
            return self.rows + [extra]

    def take(self) -> tuple[bool, list, object]:
        """``(reset, new complete rows, provisional last object)``.

        ``reset`` means the returned rows are the whole file and any aggregate
        built from earlier rows has to be discarded. The provisional object is
        included by a full parse and is not part of ``new``.
        """
        with self._lock:
            reset, new = self._pull()
            return reset, new, _parse_json_line(self.buf)

    def _pull(self) -> tuple[bool, list]:
        """Caller holds ``self._lock``. Newly completed rows only, unless ``reset``."""
        reset = False
        try:
            st = os.stat(self.path)
        except OSError:
            had = self.prefix is not None or self.off != 0 or bool(self.buf) or bool(self.rows)
            self._reset()
            return had, []
        size = st.st_size
        ident = (getattr(st, "st_dev", None), getattr(st, "st_ino", None))
        try:
            with open_shared_binary(self.path) as fh:
                replaced = self.dev is not None and ident != (self.dev, self.ino)
                shrunk = size < self.off
                mismatch = False
                if not replaced and not shrunk and (self.prefix is not None or self.tail_mark):
                    if self.prefix is not None and fh.read(len(self.prefix)) != self.prefix:
                        mismatch = True
                    if self.tail_mark and self.off >= len(self.tail_mark):
                        fh.seek(self.off - len(self.tail_mark))
                        if fh.read(len(self.tail_mark)) != self.tail_mark:
                            mismatch = True
                if replaced or shrunk or mismatch:
                    self._reset()
                    reset = True
                    try:
                        st = os.stat(self.path)
                    except OSError:
                        return True, []
                    size = st.st_size
                    ident = (getattr(st, "st_dev", None), getattr(st, "st_ino", None))
                if not reset and size == self.off and self.prefix is not None:
                    self.dev, self.ino = ident
                    return False, []
                if self.prefix is None:
                    fh.seek(0)
                    self.prefix = fh.read(min(_TAIL_MARK, size))
                fh.seek(self.off)
                data = fh.read(max(0, size - self.off))
        except OSError:
            return reset, []
        self.dev, self.ino = ident
        self.off += len(data)
        if data:
            self.tail_mark = ((self.tail_mark or b"") + data)[-_TAIL_MARK:]
            self.buf += data
        parts = self.buf.split(b"\n")
        self.buf = parts.pop() if parts else b""
        rows = []
        for line in parts:
            parsed = _parse_json_line(line)
            if parsed is not None:
                rows.append(parsed)
        return reset, rows


class InputsShare:
    """One incremental parse of an inputs.jsonl, shared by OPTIONS and corroboration.

    Raw rows are not kept. Presses and input-activity candidates grow only with
    newly completed lines, so a refresh does not walk the whole file under the GIL.
    """

    def __init__(self, path) -> None:
        self.path = Path(path)
        self.tail = JsonlTail(self.path)
        self._lock = threading.Lock()
        self._presses: list[int] = []
        self._prev = False
        self._anchor = None
        self._activity: list[dict] = []
        self._published_presses: list[int] = []
        self._published_activity: list[dict] = []
        self._saw_extra = False
        self._fold_log = _new_fold_log()

    def view(self) -> tuple[list[int], list[dict]]:
        """Current presses and input-activity rows, including a valid partial last line."""
        with self._lock:
            reset, new, extra = self.tail.take()
            if reset:
                self._presses = []
                self._prev = False
                self._anchor = None
                self._activity = []
                self._saw_extra = False
            if new:
                self._presses, self._prev, self._anchor = _fold_options(
                    new, self._presses, self._prev, self._anchor, self._fold_log)
                self._activity.extend(_fold_activity(new, self._fold_log))
            if reset or new or (extra is not None) or self._saw_extra:
                if extra is None:
                    self._published_presses = list(self._presses)
                    self._published_activity = list(self._activity)
                else:
                    self._published_presses, _, _ = _fold_options(
                        [extra], list(self._presses), self._prev, self._anchor, self._fold_log)
                    self._published_activity = self._activity + _fold_activity([extra], self._fold_log)
                self._saw_extra = extra is not None
            return self._published_presses, self._published_activity


_INPUTS: dict[str, InputsShare] = {}
_INPUTS_LOCK = threading.Lock()


def shared_inputs(path) -> InputsShare:
    """The process-wide parse of this inputs file. A second tail would keep a second copy."""
    p = Path(path)
    try:
        key = str(p.resolve())
    except OSError:
        key = str(p)
    with _INPUTS_LOCK:
        share = _INPUTS.get(key)
        if share is None:
            share = InputsShare(key)
            _INPUTS[key] = share
        return share


def note_options_from_inputs(core, path) -> None:
    """Read OPTIONS rising edges from an inputs.jsonl. Missing file is a no-op.

    Repeat calls fold only the bytes appended since the last call. The press list
    is replaced by that snapshot, so a truncated or rotated file does not keep stale presses.
    The same parse feeds delta corroboration.
    """
    p = Path(path)
    if not p.is_file():
        return
    try:
        presses, _activity = shared_inputs(p).view()
    except Exception:
        return
    core.set_options(presses)


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



def jcs_safe(v):
    """Copy of an event with every float replaced by an integer ``<key>_permille`` (round(x * 1000)).

    QorCap commits events into its checkpoint chain and receipt with RFC 8785 JCS under the QorTime profile, which
    refuses floats. v0.3.4 to v0.3.24 posted the end-of-game-menu final with ``end_menu.conf``/``margin`` as floats
    (``round(x, 3)``); qorcap.exe panicked hashing it and the recording stopped (match-20261018-193534).
    The reader keeps its float state internally (the referee's end-menu fallback reads ``conf``); only what is
    posted is converted. A float list element (no key to rename) becomes its permille integer."""
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, float):
        return round(v * 1000)
    if isinstance(v, dict):
        out = {}
        for k, x in v.items():
            if isinstance(x, float) and not isinstance(x, bool):
                out[f"{k}_permille"] = round(x * 1000)
            else:
                out[k] = jcs_safe(x)
        return out
    if isinstance(v, (list, tuple)):
        return [jcs_safe(x) for x in v]
    return v


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
        self.touchdown: TouchdownJump | None = TouchdownJump()
        self._max_quarter: int | None = None  # highest quarter actually posted; a later read may not go backwards
        self.periods = PeriodTrack()
        self._quarter_reject: str | None = None
        # External corroboration for one-legal-step end-menu deltas (set by match runner):
        # each entry: {"kind": "scorebug_final_play"|"score_change_clip"|"input_activity"|"haptic",
        #              "t_ms": int, ...optional "home","away","sha256"}
        self.delta_corroboration: list[dict] = []
        # Pause menu vs end screen. ``_pixel_match_since`` is the first frame of the current dark picture.
        # A non-matching frame clears it, and a short labelled span is then dropped.
        # ``_end_screen_earliest`` is the first match of this absence and stays until the next
        # scorebug read, so a one-frame gap cannot move the input cutoff forward.
        self._pixel_match_since: int | None = None
        self._end_screen_earliest: int | None = None
        # Earliest pixel match of this capture. A scorebug read clears ``_end_screen_earliest``
        # so the next screen has its own cutoff, but evidence from this first match through
        # that read is still not the scoring play.
        self._end_screen_seen_ms: int | None = None
        self._options_presses_ms: list[int] = []
        self._options_lock = threading.Lock()
        # Frame cadence and a visible-but-unlocked bar. These spans are diagnostics.
        # They are not timeline observations and they never become a score.
        self._last_frame_ms: int | None = None
        self._lag: dict | None = None
        self._lag_clear = 0
        self._unagreed: dict | None = None
        self._unagreed_clear = 0
        self._unagreed_since: int | None = None
        self._unagreed_reasons: dict[str, int] = {}
        self._warn_ms: dict[str, int] = {}
        self.health: list[dict] = []
        self.health_write = None
        # Closed spans waiting out the 30 s row window. Not yet in ``health``.
        self._health_pending: dict[str, dict] = {}

    def note_options(self, presses_ms) -> None:
        """Remember OPTIONS press times (wall ms). Repeating a time is a no-op."""
        with self._options_lock:
            seen = set(self._options_presses_ms)
            for t in presses_ms or []:
                if isinstance(t, bool) or not isinstance(t, int):
                    continue
                if t not in seen:
                    self._options_presses_ms.append(t)
                    seen.add(t)
            self._options_presses_ms.sort()

    def set_options(self, presses_ms) -> None:
        """Replace OPTIONS press times with this snapshot. The frame thread copies the list."""
        clean = []
        seen = set()
        for t in presses_ms or []:
            if isinstance(t, bool) or not isinstance(t, int):
                continue
            if t not in seen:
                clean.append(t)
                seen.add(t)
        clean.sort()
        with self._options_lock:
            self._options_presses_ms = clean

    def _options_snapshot(self) -> list[int]:
        with self._options_lock:
            return list(self._options_presses_ms)

    def _end_context(self, t_ms: int) -> bool:
        """Same gates as the end-of-game menu: Q4/OT (or elapsed periods) and a finished clock."""
        last = self._scorebug_for_final()
        if last is None:
            return False
        return self._quarter_ready_for_end_menu(last, t_ms) and self._clock_ready_for_end_menu(last, t_ms)

    def _opened_by_options(self, t_ms: int) -> bool:
        """This dark picture started just after OPTIONS while the scorebug was still up, and no scorebug read has returned."""
        presses = self._options_snapshot()
        if self._pixel_match_since is None or not presses:
            return False
        start = self._pixel_match_since
        last = self.last_scorebug()
        if last is None:
            return False
        lt = last.get("t_ms")
        if isinstance(lt, bool) or not isinstance(lt, int):
            return False
        for press in presses:
            if press > t_ms:
                break
            if not (-500 <= start - press <= OPTIONS_MENU_LEAD_MS):
                continue
            if lt > press or press - lt > SCOREBUG_VISIBLE_MS:
                continue
            return True
        return False

    def _accept_final_screen(self, t_ms: int) -> bool:
        """Label ``final_screen`` only in end-of-game context, and not on an OPTIONS pause unless it stays up.

        A tied scorebug is still this picture. QorTime imports ``ReaderCore``. Whether that
        tie starts the post-final tail is decided in the match runner, not here.
        """
        if not self._end_context(t_ms):
            return False
        held = 0 if self._pixel_match_since is None else max(0, t_ms - self._pixel_match_since)
        if self._opened_by_options(t_ms) and held < OPTIONS_COINCIDENT_HOLD_MS:
            return False
        return True

    def on_frame(self, t_ms: int, full) -> None:
        """``full`` may be a full-resolution frame (v0.3.5): the scorebug reader gets it when it asks for it
        (``wants_full_res``, e.g. the Monday Night profile); everything else gets the 640x360 reader frame."""
        self.stats["frames"] += 1
        self._note_frame_time(t_ms)
        self._flush_touchdown(t_ms)
        img = prep(full)
        rimg = full if getattr(self.reader, "wants_full_res", False) else img
        # The end-of-game menu is static, so the pause detector would swallow it; check it first.
        # A pixel match is not by itself a final: Practice/Play Now pause (match-20261020-171032)
        # can look like the end menu for a moment after OPTIONS. Label final_screen only in
        # end-of-game context, and treat an OPTIONS press that hid the scorebug as a pause until
        # the picture has stayed up long enough that it cannot be the scripted pause menu.
        if self.final_screen_detector is not None and self.final_screen_detector(img):
            self._note_end_screen_time(t_ms)
            if self._pixel_match_since is None:
                self._pixel_match_since = t_ms
            if self._end_screen_earliest is None:
                self._end_screen_earliest = t_ms
            accept = self._accept_final_screen(t_ms)
            if self._opened_by_options(t_ms) and not accept:
                self.stats["final_screen_rejected"] = self.stats.get("final_screen_rejected", 0) + 1
                self._observe(t_ms, img, True, screen=False)
                if self.gate is not None:
                    self.gate.on_frame(t_ms, False, paused=True)
                return
            if accept:
                self.stats["final_screen"] = self.stats.get("final_screen", 0) + 1
            self._observe(t_ms, img, True, screen=accept)
            self._end_menu(t_ms, img)
            return
        # Current run only. The earliest match stays until a scorebug read.
        self._pixel_match_since = None
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
            if getattr(self.reader, "accepts_time", False):
                fr = self.reader.read(rimg, t_ms=t_ms)
            else:
                fr = self.reader.read(rimg)
            band, _ = self.reader.reference_band(rimg) if hasattr(self.reader, "reference_band") else (None, None)
        except Exception as e:  # never silent
            self.stats["errors"] += 1
            print(f"scorebug reader error: {type(e).__name__}: {e}", file=sys.stderr)
            return
        self._note_reanchor(t_ms, fr)
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
        self._note_agreement(t_ms, fr, absent, res)
        if res.agreed and res.read is not None:
            if self.touchdown is not None:
                self.touchdown.reset()
        elif self.touchdown is not None:
            self.touchdown.offer(fr, t_ms, self.last_scorebug(), self._max_quarter)
            return
        else:
            return
        self._post_agreed(t_ms, res.read, soft)

    def _agree_gap_ms(self) -> float:
        """The same gap AgreementWindow uses. Never wider than that window's own config."""
        cfg = getattr(self.agree, "config", None)
        ns = getattr(cfg, "max_gap_ns", None)
        if isinstance(ns, bool) or not isinstance(ns, (int, float)) or ns <= 0:
            return AGREE_GAP_MS_DEFAULT
        return float(ns) / 1_000_000.0

    def _warn_health(self, kind: str, t_ms: int, text: str) -> None:
        """One console line when a span opens, and at most one of those per 30 s."""
        last = self._warn_ms.get(kind)
        if last is not None and t_ms - last < HEALTH_WARN_MS:
            return
        self._warn_ms[kind] = t_ms
        print(text, file=sys.stderr)

    def _note_frame_time(self, t_ms: int) -> None:
        """Open a reader_lag span when this frame is too far from the previous one to agree."""
        self._flush_due_health(t_ms)
        prev = self._last_frame_ms
        self._last_frame_ms = t_ms
        if prev is None:
            return
        gap = t_ms - prev
        if gap > self._agree_gap_ms():
            self.stats["frame_gap"] = self.stats.get("frame_gap", 0) + 1
            self._lag_clear = 0
            opening = self._lag is None
            self._lag = self._health_span(self._lag, "reader_lag", True, t_ms, max_gap_ms=gap)
            if opening:
                self._warn_health(
                    "reader_lag", t_ms,
                    f"Score reader behind: frames {_seconds_text(gap)} s apart; scores held",
                )
            return
        if self._lag is None:
            return
        self._lag_clear += 1
        if self._lag_clear < HEALTH_CLEAR_FRAMES:
            return
        self._lag = self._health_span(self._lag, "reader_lag", False, t_ms)
        self._lag_clear = 0

    def _note_agreement(self, t_ms: int, fr, absent: bool, res) -> None:
        """Log a visible bar that has not agreed. Does not invent a row."""
        visible = bool(getattr(fr, "ok", False) and not absent)
        agreed = bool(getattr(res, "agreed", False) and getattr(res, "read", None) is not None)
        if not visible or agreed:
            if self._unagreed is None:
                self._unagreed_since = None
                self._unagreed_reasons = {}
                self._unagreed_clear = 0
                return
            self._unagreed_clear += 1
            if self._unagreed_clear < HEALTH_CLEAR_FRAMES:
                return
            self._unagreed_since = None
            self._unagreed_reasons = {}
            self._unagreed = self._health_span(self._unagreed, "score_unagreed", False, t_ms)
            self._unagreed_clear = 0
            return
        self._unagreed_clear = 0
        reason = str(getattr(res, "reason", None) or "unagreed")
        self._unagreed_reasons[reason] = self._unagreed_reasons.get(reason, 0) + 1
        if self._unagreed_since is None:
            self._unagreed_since = t_ms
        if t_ms - self._unagreed_since <= UNAGREED_MS:
            return
        opening = self._unagreed is None
        self._unagreed = self._health_span(self._unagreed, "score_unagreed", True, t_ms)
        if self._unagreed is not None:
            self._unagreed["reasons"] = dict(self._unagreed_reasons)
        if opening:
            held_ms = t_ms - self._unagreed_since
            self._warn_health(
                "score_unagreed", t_ms,
                f"Score visible but not agreed for {_seconds_text(held_ms)} s; scores held",
            )

    def _health_span(self, cur, name, on, t_ms, max_gap_ms=None):
        """Same shape as a timeline span. Closed spans are diagnostics, not /events posts."""
        if not on:
            if cur is not None:
                self._commit_health(cur)
            return None
        if cur is None:
            cur = {"t_ms": t_ms, "until_ms": t_ms, "obs": name, "n": 0}
        cur["until_ms"] = t_ms
        cur["n"] = cur["n"] + 1
        if max_gap_ms is not None:
            cur["max_gap_ms"] = max(int(cur.get("max_gap_ms") or 0), int(max_gap_ms))
        return cur

    def _health_due(self, row: dict, t_ms: int) -> bool:
        """The 30 s window opens at the held row's first t_ms, not its growing end."""
        return int(t_ms) >= int(row.get("t_ms") or 0) + HEALTH_WARN_MS

    def _commit_health(self, span: dict) -> None:
        """Hold a closed span. Later spans of the same kind merge until the window is due."""
        row = dict(span)
        if "reasons" in span:
            row["reasons"] = dict(span["reasons"])
        row["count"] = 1
        kind = str(row.get("obs") or "")
        pending = self._health_pending.get(kind)
        if pending is not None and not self._health_due(pending, int(row.get("t_ms") or 0)):
            pending["count"] = int(pending.get("count") or 1) + 1
            pending["n"] = int(pending.get("n") or 0) + int(row.get("n") or 0)
            pending["until_ms"] = max(int(pending.get("until_ms") or 0), int(row.get("until_ms") or 0))
            if "max_gap_ms" in row:
                pending["max_gap_ms"] = max(int(pending.get("max_gap_ms") or 0), int(row["max_gap_ms"]))
            if row.get("open_at_exit"):
                pending["open_at_exit"] = True
            if row.get("reasons"):
                reasons = dict(pending.get("reasons") or {})
                for key, n in row["reasons"].items():
                    reasons[key] = int(reasons.get(key) or 0) + int(n)
                pending["reasons"] = reasons
            return
        if pending is not None:
            self._emit_health(kind)
        self._health_pending[kind] = row

    def _flush_due_health(self, t_ms: int) -> None:
        """Write a held row once frame time is 30 s past the row's first t_ms."""
        for kind, row in list(self._health_pending.items()):
            if self._health_due(row, t_ms):
                self._emit_health(kind)

    def flush_health(self) -> None:
        """Write held rows and any span still open. Runs when the reader thread exits."""
        for attr in ("_lag", "_unagreed"):
            span = getattr(self, attr, None)
            if span is None:
                continue
            open_row = dict(span)
            if "reasons" in span:
                open_row["reasons"] = dict(span["reasons"])
            open_row["open_at_exit"] = True
            self._commit_health(open_row)
            setattr(self, attr, None)
        self._lag_clear = 0
        self._unagreed_clear = 0
        for kind in list(self._health_pending):
            self._emit_health(kind)

    def _emit_health(self, kind: str) -> None:
        row = self._health_pending.pop(kind, None)
        if row is None:
            return
        self.health.append(row)
        write = self.health_write
        if write is None:
            return
        try:
            write(row)
        except Exception as e:
            print(f"reader health write failed: {type(e).__name__}: {e}", file=sys.stderr)

    def health_observations(self) -> list[dict]:
        """Written rows, a span held for the 30 s window, and a span still open. Never a score."""
        out = []
        seen = list(self.health) + list(self._health_pending.values())
        for span in (self._lag, self._unagreed):
            if span is not None:
                seen.append(span)
        for row in seen:
            item = dict(row)
            if "reasons" in row:
                item["reasons"] = dict(row["reasons"])
            out.append(item)
        return out

    def _flush_touchdown(self, t_ms: int) -> None:
        """Post a held touchdown jump whose hold is over (runs first on every frame, menus and pauses included)."""
        if self.touchdown is None or self.touchdown.pending is None:
            return
        got = self.touchdown.due(t_ms)
        if got is None:
            return
        self.stats["touchdown_jump"] = self.stats.get("touchdown_jump", 0) + 1
        print(f"touchdown jump locked: away {got.left_score} - home {got.right_score}", file=sys.stderr)
        self.score_lock.remember(got.pair, t_ms * 1_000_000)
        self._post_agreed(t_ms, got, True, touchdown_hold=True)

    def _note_reanchor(self, t_ms: int, fr) -> None:
        """Record a re-anchor on the sidecar callback. The score event itself is unchanged.

        ``score_timeline.v0`` has no field for these flags, and the checkpoint evidence
        is the existing score object. The note stays out of both.
        """
        detail = getattr(fr, "detail", None) or {}
        grammar = detail.get("grammar_reanchor") is True
        clock = detail.get("clock_reanchor") is True
        if not grammar and not clock:
            return
        row = {
            "t_ms": int(t_ms),
            "away": getattr(fr, "left_score", None),
            "home": getattr(fr, "right_score", None),
        }
        quarter = getattr(fr, "quarter", None)
        if quarter is not None:
            row["quarter"] = quarter
        if grammar:
            row["grammar_reanchor"] = True
        if clock:
            row["clock_reanchor"] = True
        note = getattr(self, "reanchor_note", None)
        if note is not None:
            note(row)

    def _post_agreed(self, t_ms: int, read, soft: bool, *, touchdown_hold: bool = False) -> None:
        from types import SimpleNamespace
        note = getattr(self.reader, "note_lock", None)
        if note is not None and getattr(read, "left_score", None) is not None and getattr(read, "right_score", None) is not None:
            note(t_ms, read.left_score, read.right_score)
        res = SimpleNamespace(read=read)
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
            # The hold is a posted score. It is not a scorebug read that came back, and the
            # flag stays on the in-memory row. The posted score event does not carry it.
            row = dict(rd, touchdown_hold=True) if touchdown_hold else rd
            self.timeline.append(row)
            self.post(jcs_safe({"type": "score", **rd}))
            q = rd.get("quarter")
            if isinstance(q, int) and not isinstance(q, bool) and q >= 1:
                self._max_quarter = q if self._max_quarter is None else max(self._max_quarter, q)
            if isinstance(q, int) and not isinstance(q, bool) and q >= 2:
                self.periods.release_for_new_quarter()
            self.periods.note(t_ms, rd.get("clock_s"))
        # The scorebug is back. A new end screen starts its own cutoff.
        # A posted end menu stays posted. A later menu with a different score does not
        # replace it and does not confirm it (see ``_note_end_menu_disagreement``).
        # Frames from before this read are not the same showing. Drop the streak and any
        # score it had already reached, including one the clock gate had rejected.
        agree = self.end_menu_agree
        if agree is not None:
            agree.streak = []
            agree.stable = None
        self._end_screen_earliest = None
        self._close_spans()  # a fresh score read ends any absence span
        self._check_final(t_ms)

    def _observe(self, t_ms: int, img, absent: bool, screen: bool | None = None) -> None:
        def span(cur, name, on):
            if not on:
                self._commit_span(cur)
                return None
            if cur is None:
                # A picture that was held through the OPTIONS pause still started when the pixels matched.
                start = t_ms
                if name == "final_screen" and self._pixel_match_since is not None:
                    start = self._pixel_match_since
                cur = {"t_ms": start, "until_ms": t_ms, "obs": name, "n": 0}
                self.timeline.append(cur)
            cur["until_ms"], cur["n"] = t_ms, cur["n"] + 1
            return cur
        self._absent = span(self._absent, "scorebug_absent", absent)
        if screen is None:
            # Same gate as the early path. A raw pixel match is not a final_screen by itself.
            raw = bool(absent and self.final_screen_detector and self.final_screen_detector(img))
            if raw and self._pixel_match_since is None:
                self._pixel_match_since = t_ms
            if raw:
                self._note_end_screen_time(t_ms)
            if raw and self._end_screen_earliest is None:
                self._end_screen_earliest = t_ms
            screen = bool(raw and self._accept_final_screen(t_ms))
        self._screen = span(self._screen, "final_screen", screen)
        if absent:
            self._check_final(t_ms)

    def _commit_span(self, s: dict | None) -> None:
        """Post a closed observation. A final_screen shorter than detect_final's bar is dropped, not posted."""
        if s is None:
            return
        if s.get("obs") == "final_screen":
            n = s.get("n") or 0
            dur = int(s.get("until_ms") or 0) - int(s.get("t_ms") or 0)
            if n < FINAL_SCREEN_MIN_FRAMES or dur < FINAL_SCREEN_MIN_SPAN_MS:
                self.timeline = [x for x in self.timeline if x is not s]
                return
        self.post(jcs_safe({"type": "obs", **s}))

    def _close_spans(self) -> None:
        self._commit_span(self._absent)
        self._commit_span(self._screen)
        self._absent = self._screen = None

    def _note_end_screen_time(self, t_ms: int) -> None:
        """Remember the earliest end-screen pixel match. A later scorebug read does not erase it."""
        if isinstance(t_ms, bool) or not isinstance(t_ms, int):
            return
        seen = self._end_screen_seen_ms
        if seen is None or t_ms < seen:
            self._end_screen_seen_ms = t_ms

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
        # A walk-off can leave time on the clock, so OT stays clock-ready here.
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
        if st is None:
            return
        if self.end_menu is not None:
            if (self.end_menu["away"], self.end_menu["home"]) == (st["away"], st["home"]):
                self.end_menu.update(until_ms=st["until_ms"], frames=st["frames"])
            else:
                # A later read with a different score must not amend the posted final.
                self._note_end_menu_disagreement(st)
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
        corr = end_menu_delta_corroborated(
            st, last, self.timeline, self.delta_corroboration, pixel_match_ms=self._end_screen_earliest,
            first_screen_ms=self._end_screen_seen_ms)
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
        self.post(jcs_safe(self.final))

    def _scorebug_between_menus(self, later_t) -> bool:
        """True only when a scorebug row is strictly after the posted menu ended and before the later menu.

        The posted menu's ``until_ms`` and the later menu's ``t_ms`` are the bounds. A row
        at either bound is not between them. A missing bound is not a scorebug read.
        An observation, a row with no home score, and a flushed touchdown hold are not
        the scorebug coming back.
        """
        menu = self.end_menu if isinstance(self.end_menu, dict) else {}
        start = _as_ms(menu.get("until_ms"))
        later = _as_ms(later_t)
        if start is None or later is None or later <= start:
            return False
        for r in self.timeline or []:
            if not isinstance(r, dict) or "obs" in r or "home" not in r or r.get("touchdown_hold") is True:
                continue
            t = _as_ms(r.get("t_ms"))
            if t is not None and start < t < later:
                return True
        return False

    def _note_end_menu_disagreement(self, st: dict) -> None:
        """Keep the first posted score and leave it unconfirmed. Do not adopt the new score.

        A scorebug read between two end menus does not reopen the final. The first
        score stays. The later score is not the result. Confirmation stays false.
        A contradicting later menu must never become a confirmed final.

        The signed note names that scorebug only when a scorebug row lies strictly
        between the posted menu's ``until_ms`` and the later menu's ``t_ms``. An
        observation, a row with no home score, and a flushed touchdown hold do not
        count. Otherwise the note is the previous sentence, with no claim that a
        scorebug returned.
        """
        if getattr(self, "_end_menu_disputed", False):
            return
        self._end_menu_disputed = True
        if not isinstance(self.final, dict):
            return
        notes = [n for n in (self.final.get("notes") or []) if isinstance(n, str)]
        between = ""
        if self._scorebug_between_menus(st.get("t_ms") if isinstance(st, dict) else None):
            between = " after a scorebug read came back between the two menus"
        notes.append(
            f"later end-of-game menu read AWAY {st['away']} - HOME {st['home']} disagreed with the posted "
            f"final AWAY {self.final.get('away')} - HOME {self.final.get('home')}{between}; score not replaced; "
            "final_confirmed=false")
        self.final = dict(self.final)
        self.final["final_confirmed"] = False
        self.final["end_menu_disputed"] = True
        self.final["notes"] = notes
        print(f"END MENU disagreed: posted AWAY {self.final.get('away')} - HOME {self.final.get('home')} kept, "
              f"later read AWAY {st['away']} - HOME {st['home']} not used; final_confirmed=false",
              file=sys.stderr)
        self.post(jcs_safe(self.final))

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
            self.post(jcs_safe(self.final))


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
                                external: list[dict] | None, pixel_match_ms: int | None = None,
                                first_screen_ms: int | None = None) -> dict | None:
    """For a one-legal-step end-menu delta, return a corroboration record or None.

    Acceptable evidence (any one):
    - a prior scorebug timeline read that already equals the end-menu score (final play briefly drawn)
    - an external score-change clip hash bound near the scoring time
    - input / haptic activity flagged near the last scorebug / end-menu window
    Exact match (no delta) needs no corroboration and returns {"kind": "exact_match"}.

    Button and haptic evidence at or after the end screen does not count. That time is
    the first pixel match or the first agreeing menu frame, and also any ``final_screen``
    row already on the timeline. A time shared with the latest scorebug read still counts:
    a held touchdown can post on the menu frame. Evidence from the first end screen
    through that scorebug read does not count either. The scorebug that came back still
    showed the old score, so that input is not the scoring play. Only evidence after the
    latest scorebug read, and before the next screen, counts. A missing label is not a
    missing screen: the menu frame and the pixel match are enough. Menu navigation during
    the post-final tail must not flip ``final_confirmed``. A score-change clip from that
    same moment onward is ignored for the same reason. A scorebug read already on the
    timeline still counts: that is the score, not an input.
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
    menu_t = st.get("t_ms") if isinstance(st, dict) else None
    cutoff = end_screen_evidence_cutoff_ms(timeline, menu_t_ms=menu_t, pixel_match_ms=pixel_match_ms)
    first = _earliest_end_screen_ms(timeline, menu_t_ms=menu_t, pixel_match_ms=pixel_match_ms)
    remembered = _as_ms(first_screen_ms)
    if remembered is not None:
        first = remembered if first is None else min(first, remembered)
    scorebug_t = _latest_scorebug_ms(timeline)
    for ev in external or []:
        kind = ev.get("kind") or ""
        t = ev.get("t_ms")
        if t is not None and not (lo <= int(t) <= hi):
            continue
        if kind in ("score_change_clip", "input_activity", "haptic", "scorebug_final_play"):
            if kind == "score_change_clip" and not ev.get("sha256"):
                continue
            # Tail input is not evidence of the scoring play. Clips cut off at the same instant.
            # Evidence on an earlier screen, up through the scorebug that came back, is not either.
            if kind in ("input_activity", "haptic", "score_change_clip"):
                if (first is not None and scorebug_t is not None and t is not None
                        and not isinstance(t, bool) and first <= int(t) <= scorebug_t):
                    continue
                if cutoff is not None and (t is None or int(t) >= cutoff):
                    continue
            return {"kind": kind, **{k: ev[k] for k in ev if k != "kind"}}
    return None


def _as_ms(t):
    if isinstance(t, bool) or not isinstance(t, int):
        return None
    return t


def _latest_scorebug_ms(timeline: list[dict] | None) -> int | None:
    """Latest scorebug row. Observation rows are not reads."""
    latest = None
    for r in timeline or []:
        if not isinstance(r, dict) or "obs" in r or "home" not in r:
            continue
        t = _as_ms(r.get("t_ms"))
        if t is None:
            continue
        if latest is None or t > latest:
            latest = t
    return latest


def _end_screen_candidates_ms(timeline: list[dict] | None, *, menu_t_ms=None,
                              pixel_match_ms=None) -> list[int]:
    times = []
    for t in (menu_t_ms, pixel_match_ms):
        ms = _as_ms(t)
        if ms is not None:
            times.append(ms)
    for r in timeline or []:
        if isinstance(r, dict) and r.get("obs") == "final_screen":
            ms = _as_ms(r.get("t_ms"))
            if ms is not None:
                times.append(ms)
    return times


def _earliest_end_screen_ms(timeline: list[dict] | None, *, menu_t_ms=None,
                            pixel_match_ms=None) -> int | None:
    times = _end_screen_candidates_ms(timeline, menu_t_ms=menu_t_ms, pixel_match_ms=pixel_match_ms)
    return min(times) if times else None


def end_screen_evidence_cutoff_ms(timeline: list[dict] | None, *, menu_t_ms=None,
                                  pixel_match_ms=None) -> int | None:
    """When the end screen appeared, so later button, haptic, and clip evidence does not count.

    The screen is the first pixel match or the first agreeing menu frame. A ``final_screen``
    timeline row is the same picture when the classifier labelled it. The cutoff is the
    earliest of those that is at or after the latest scorebug read. A shared millisecond
    counts: a held touchdown posts on the menu frame, and duplicate timestamps do too.

    A labelled screen from before that read is an earlier picture. If every candidate is
    earlier, including when a frame timestamp went backwards, the cutoff is the earliest
    candidate. It does not disappear. With no scorebug read, every candidate still counts.
    With no candidate, there is no screen and the result is None.
    """
    times = _end_screen_candidates_ms(timeline, menu_t_ms=menu_t_ms, pixel_match_ms=pixel_match_ms)
    if not times:
        return None
    scorebug_t = _latest_scorebug_ms(timeline)
    if scorebug_t is None:
        return min(times)
    later = [t for t in times if t >= scorebug_t]
    return min(later) if later else min(times)



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
    out.extend(_fold_activity(input_records))
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


def open_shared_binary(path):
    """Binary shared read. Same Windows share flags as ``open_shared_text`` so an append is not blocked."""
    if os.name != "nt":
        return open(path, "rb")
    import ctypes
    import msvcrt
    from ctypes import wintypes
    share = 0x1 | 0x2 | 0x4
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                              wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k.CreateFileW.restype = wintypes.HANDLE
    h = k.CreateFileW(os.fspath(path), 0x80000000, share, None, 3, 0x80, None)
    if h is None or int(ctypes.cast(h, ctypes.c_void_p).value or 0) in (0, 0xFFFFFFFF, -1):
        raise OSError(ctypes.get_last_error(), f"cannot open {path}")
    fd = msvcrt.open_osfhandle(int(ctypes.cast(h, ctypes.c_void_p).value), os.O_RDONLY | os.O_BINARY)
    return open(fd, "rb", closefd=True)


class BundleCorrTail:
    """Incremental view of the files ``load_delta_corroboration_from_bundle`` used to reread whole.

    ``bundle.json`` is rewritten, so it is read in full (it stays small). manifest, haptics, and
    inputs are append-only jsonl and are tailed. A score clip's hash is reused while its size and
    mtime are unchanged.
    """

    def __init__(self, bundle) -> None:
        self.bundle = Path(bundle)
        self.manifest = JsonlTail(self.bundle / "manifest.jsonl")
        self.haptics = JsonlTail(self.bundle / "haptics.jsonl")
        # Same parse note_options_from_inputs uses. A second inputs tail kept a second copy of every row.
        self.inputs = shared_inputs(self.bundle / "inputs.jsonl")
        self._clip_sha: dict[str, tuple] = {}

    def snapshot(self) -> list[dict]:
        b = self.bundle
        clips: list[dict] = []
        bj = b / "bundle.json"
        if bj.is_file():
            try:
                with open_shared_text(bj) as fh:
                    clips.extend(json.loads(fh.read()).get("clips") or [])
            except Exception:
                pass
        for o in self.manifest.snapshot():
            if isinstance(o, dict) and o.get("type") == "clip":
                clips.append(o)
        cdir = b / "clips"
        if cdir.is_dir():
            known = {c.get("file") for c in clips if isinstance(c, dict)}
            for f in sorted(cdir.glob("*.mp4")):
                if not (f.name.startswith("score") or "score-" in f.name):
                    continue
                if str(f) in known or any(f.name in str(c.get("file") or "") for c in clips):
                    continue
                try:
                    st = f.stat()
                except OSError:
                    continue
                key = (st.st_size, getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))
                cached = self._clip_sha.get(str(f))
                if cached is not None and cached[0] == key:
                    sha = cached[1]
                else:
                    try:
                        sha = hashlib.sha256(f.read_bytes()).hexdigest()
                    except Exception:
                        continue
                    self._clip_sha[str(f)] = (key, sha)
                clips.append({"type": "clip", "name": f.stem, "file": str(f), "sha256": sha,
                              "wall_ms": int(st.st_mtime * 1000)})
        haptic_rows = [r for r in self.haptics.snapshot() if isinstance(r, dict)]
        _presses, activity = self.inputs.view()
        out = collect_delta_corroboration(clips=clips, haptic_records=haptic_rows, input_records=None)
        out.extend(activity)
        return out


_BUNDLE_TAILS: dict[str, BundleCorrTail] = {}


def bundle_corr_tail(bundle) -> BundleCorrTail:
    b = Path(bundle)
    try:
        key = str(b.resolve())
    except OSError:
        key = str(b)
    tail = _BUNDLE_TAILS.get(key)
    if tail is None:
        tail = BundleCorrTail(b)
        _BUNDLE_TAILS[key] = tail
    return tail


def load_delta_corroboration_from_bundle(bundle) -> list[dict]:
    """Read clips (manifest/bundle.json) + haptics.jsonl + inputs.jsonl from a match bundle dir.

    Repeat calls tail the jsonl files. The result matches a full parse, including a partial last line.
    """
    return bundle_corr_tail(bundle).snapshot()


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
