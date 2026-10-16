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

    * bottom (y 200-345) is near-black and flat: end menu mean ~0.5, std ~9; pause menus over the faded
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
    bottom = g[ys(200, 345), :]
    if not (bottom.mean() < 8 and (bottom > 60).mean() < 0.02 and bottom.std() < 14):
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
                 final_screen_detector=None) -> None:
        self.post, self.reader, self.agree, self.pause = post, reader, agree, pause
        self.detect_final = detect_final
        self.final_screen_detector = final_screen_detector  # QorTime ships none; QorCap: madden_final_screen
        self.timeline: list[dict] = []
        self._absent: dict | None = None
        self._screen: dict | None = None
        self.final: dict | None = None
        self.stats = {"frames": 0, "agreed": 0, "errors": 0, "paused": 0}

    def on_frame(self, t_ms: int, img) -> None:
        self.stats["frames"] += 1
        # The end-of-game menu is static, so the pause detector would swallow it; check it first.
        if self.final_screen_detector is not None and self.final_screen_detector(img):
            self.stats["final_screen"] = self.stats.get("final_screen", 0) + 1
            self._observe(t_ms, img, True, screen=True)
            return
        if self.pause is not None:
            self.pause.update(t_ms, img)
            if self.pause.paused:
                self.stats["paused"] += 1
                self._close_spans()
                return
        try:
            fr = self.reader.read(img)
            band, _ = self.reader.reference_band(img) if hasattr(self.reader, "reference_band") else (None, None)
        except Exception as e:  # never silent
            self.stats["errors"] += 1
            print(f"scorebug reader error: {type(e).__name__}: {e}", file=sys.stderr)
            return
        absent = (not fr.ok) and fr.reason in NOT_VISIBLE
        self._observe(t_ms, img, absent)
        ch = hashlib.sha256(band.tobytes()).hexdigest()[:16] if band is not None else ""
        res = self.agree.offer(fr, clock_ns=t_ms * 1_000_000, crop_hash=ch, session_id="qorcap")
        if not (res.agreed and res.read is not None):
            return
        self.stats["agreed"] += 1
        rd = {"t_ms": t_ms, "away": res.read.left_score, "home": res.read.right_score,  # Madden: AWAY on the LEFT
              "quarter": res.read.quarter, "clock_s": res.read.clock_seconds,
              "clock_red": bool(getattr(res.read, "clock_red", False))}
        prev = next((r for r in reversed(self.timeline) if "obs" not in r), None)
        key = lambda r: (r["home"], r["away"], r["quarter"], r["clock_s"])  # noqa: E731
        if prev is None or key(prev) != key(rd) or rd["t_ms"] - prev["t_ms"] >= 1_000:
            self.timeline.append(rd)
            self.post({"type": "score", **rd})
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

    def _check_final(self, t_ms: int) -> None:
        if self.final is not None:
            return
        fin = self.detect_final(self.timeline)
        if fin.final:
            self._close_spans()
            self.final = {"type": "final", "basis": fin.basis, "home": fin.home, "away": fin.away,
                          "quarter": fin.quarter, "t_ms": fin.t_ms}
            print(f"FINAL ({fin.basis}): away {fin.away} - home {fin.home}", file=sys.stderr)
            self.post(self.final)


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
    import cv2
    import numpy as np
    from qor_receipt._vendor.qoresence.vision.local_scorebug.agreement import AgreementWindow
    from qor_receipt._vendor.qoresence.vision.local_scorebug.reader import ScorebugReader
    from qor_receipt.agent.pause_detect import PauseDetector
    from qor_receipt.final_detect import detect_final

    core = ReaderCore(lambda b: http_post(a.qorcap, b), ScorebugReader(), AgreementWindow(), PauseDetector(), detect_final,
                      final_screen_detector=madden_final_screen)
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
                core.on_frame(int(time.time() * 1000), prep(img))
        if time.monotonic() - t_print > 30:
            t_print = time.monotonic()
            print(f"health {core.stats} timeline={len(core.timeline)}", file=sys.stderr)
        time.sleep(max(0.0, period - (time.monotonic() - t0)))


if __name__ == "__main__":
    sys.exit(main())
