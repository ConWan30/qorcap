"""Pause-capable moments for the official challenges (v0.3.11).

Online Madden 27 does not let a player pause during a kickoff or a kickoff return: OPTIONS only opens the pause menu
once a play has been called (pre-snap). Every v0.3.10 "kickoff" challenge was issued ~10 s into the recording, before
or during the opening kickoff, with a 20 s window, so it could not be answered. Halftime/Q4 were tied to the first Q3/Q4
scorebug read, which is the second-half kickoff itself (and on Sunday/Thursday Night no quarter was read at all).

``PlayCallDetector`` recognises Madden's play-call screen from one frame. It is the same UI in every presentation
(standard, Monday/Sunday/Thursday Night): a dark, near-neutral overlay across the whole lower-middle band (the play
cards) with thin white text/lines, over a not-dark upper picture. Tuned on 839 live snapshots (four matches,
2026-10-16/17); pause menus (whole screen dark) and stadium shadow (luma 40-60) do not match.

``PauseCapableGate`` turns frames + agreed scorebug reads into "a play has just been called" events:

1. the game clock is seen RUNNING in the current quarter (a read below the first read of that quarter), so the
   kickoff of that half has been fielded (or, after a touchback, the first snap has happened);
2. after that, a play-call screen is seen for >= ``min_call_ms`` and then goes away for >= ``settle_ms`` while the
   game is not paused: the players are at the line, pre-snap, and OPTIONS works;
3. fallback ``clock_proxy``: no play-call screen within ``proxy_after_ms`` of the clock starting to run.

An event can open at most one phase: ``kickoff`` (Q1-Q2), ``halftime`` (Q3) or ``end`` (Q4 and overtime). The window
(90 s, set by the referee) covers a quick snap by the opponent: the player pauses at the next pre-snap.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

PHASE_QUARTERS = (("kickoff", 1), ("halftime", 3), ("end", 4))


def phase_for_quarter(q: int | None) -> str | None:
    if q is None or q < 1:
        return None
    return "kickoff" if q <= 2 else "halftime" if q == 3 else "end"


@dataclass
class PlayCallThresholds:
    band: tuple[float, float] = (0.53, 0.78)
    thirds: tuple[tuple[float, float], ...] = ((0.04, 0.34), (0.36, 0.64), (0.66, 0.96))
    dark_luma: int = 30
    min_dark_frac: float = 0.45     # every third of the band is mostly dark (cards span the whole width)
    text_luma: int = 150
    min_text_frac: float = 0.003    # the cards carry white play names / lines (a black fade has none)
    max_text_frac: float = 0.25
    max_chroma: float = 40.0        # the overlay is near neutral (a navy end-zone wall is not)
    min_top_luma: float = 31.0      # the picture above the cards is visible (the pause menu darkens everything: ~27)


class PlayCallDetector:
    def __init__(self, th: PlayCallThresholds | None = None) -> None:
        self.th = th or PlayCallThresholds()
        self.last: dict[str, Any] = {}

    def features(self, img) -> dict[str, Any]:
        import cv2
        import numpy as np

        s = cv2.resize(img, (320, 180), interpolation=cv2.INTER_AREA).astype(np.int16)
        if s.ndim == 2:
            s = np.repeat(s[..., None], 3, axis=2)
        g = 0.114 * s[..., 0] + 0.587 * s[..., 1] + 0.299 * s[..., 2]
        chroma = s.max(axis=2) - s.min(axis=2)
        h, w = g.shape
        th = self.th
        y0, y1 = int(th.band[0] * h), int(th.band[1] * h)
        thirds = [float((g[y0:y1, int(a * w):int(b * w)] < th.dark_luma).mean()) for a, b in th.thirds]
        band = g[y0:y1, int(0.04 * w):int(0.96 * w)]
        bc = chroma[y0:y1, int(0.04 * w):int(0.96 * w)]
        dk = band < th.dark_luma
        return {"thirds": thirds, "text": float((band > th.text_luma).mean()),
                "chroma": float(bc[dk].mean()) if dk.any() else 255.0,
                "top": float(g[int(0.08 * h):int(0.45 * h)].mean())}

    def detect(self, img) -> bool:
        f = self.features(img)
        th = self.th
        f["play_call"] = bool(min(f["thirds"]) >= th.min_dark_frac and th.min_text_frac <= f["text"] <= th.max_text_frac
                              and f["chroma"] <= th.max_chroma and f["top"] >= th.min_top_luma)
        self.last = f
        return f["play_call"]


@dataclass
class GateEvent:
    t_ms: int
    basis: str                      # "play_call" | "clock_proxy"
    quarter: int
    phase: str
    clock_s: int | None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"t_ms": self.t_ms, "basis": self.basis, "quarter": self.quarter, "phase": self.phase,
                "clock_s": self.clock_s, **self.detail}


class PauseCapableGate:
    """Feed every reader frame (``on_frame``) and every agreed scorebug read (``on_read``); ``events`` collects
    the pause-capable moments, newest last. ``ready(phase, after_ms)`` is what the challenge client asks."""

    def __init__(self, *, min_call_ms: int = 1_500, settle_ms: int = 1_000, proxy_after_ms: int = 45_000,
                 read_fresh_ms: int = 6_000) -> None:
        self.min_call_ms, self.settle_ms, self.proxy_after_ms = min_call_ms, settle_ms, proxy_after_ms
        self.read_fresh_ms = read_fresh_ms
        self.quarter: int | None = None
        self.q_first_clock: int | None = None
        self.running_since: int | None = None   # t_ms the clock was first seen running in this quarter
        self.last_read: dict[str, Any] | None = None
        self.call_start: int | None = None
        self.call_last: int | None = None
        self.calls_seen = 0
        self.events: list[GateEvent] = []
        self._proxy_done_q: set[int] = set()

    # -- scorebug
    def on_read(self, rd: dict[str, Any]) -> None:
        q, c = rd.get("quarter"), rd.get("clock_s")
        if not isinstance(q, int):
            return
        if q != self.quarter:
            self.quarter, self.q_first_clock, self.running_since = q, c, None
            self.call_start = self.call_last = None
        elif isinstance(c, int):
            if self.q_first_clock is None:
                self.q_first_clock = c
            elif self.running_since is None and c < self.q_first_clock:
                self.running_since = int(rd["t_ms"])
        self.last_read = dict(rd)

    # -- frames
    def on_frame(self, t_ms: int, play_call: bool, paused: bool = False) -> GateEvent | None:
        if paused:
            self.call_start = self.call_last = None
            return None
        if play_call:
            if self.call_start is None:
                self.call_start = t_ms
            self.call_last = t_ms
            return None
        ev = None
        if self.call_start is not None and self.call_last is not None and t_ms - self.call_last >= self.settle_ms:
            long_enough = self.call_last - self.call_start >= self.min_call_ms
            started = self.call_start
            self.call_start = self.call_last = None
            if long_enough:
                self.calls_seen += 1
                if self._armed() and self.running_since is not None and started >= self.running_since - 2_000:
                    ev = self._emit(t_ms, "play_call", {"call_ms": t_ms - started})
        if ev is None and self._armed() and self.running_since is not None and self.quarter not in self._proxy_done_q \
                and t_ms - self.running_since >= self.proxy_after_ms \
                and not any(e.quarter == self.quarter for e in self.events):
            self._proxy_done_q.add(self.quarter)
            ev = self._emit(t_ms, "clock_proxy", {"clock_running_ms": t_ms - self.running_since})
        return ev

    def _armed(self) -> bool:
        lr = self.last_read
        return self.quarter is not None and lr is not None

    def _emit(self, t_ms: int, basis: str, detail: dict[str, Any]) -> GateEvent:
        lr = self.last_read or {}
        detail = dict(detail, score={"away": lr.get("away"), "home": lr.get("home")},
                      read_age_ms=t_ms - int(lr.get("t_ms", t_ms)))
        ev = GateEvent(t_ms, basis, int(self.quarter), phase_for_quarter(self.quarter), lr.get("clock_s"), detail)
        self.events.append(ev)
        return ev

    def ready(self, phase: str, after_ms: int = 0) -> GateEvent | None:
        """Newest pause-capable event for ``phase`` that happened after ``after_ms`` (and is still fresh)."""
        for e in reversed(self.events):
            if e.t_ms <= after_ms:
                return None
            if e.phase == phase:
                return e
        return None

    def latest(self, after_ms: int = 0) -> GateEvent | None:
        e = self.events[-1] if self.events else None
        return e if e is not None and e.t_ms > after_ms else None
