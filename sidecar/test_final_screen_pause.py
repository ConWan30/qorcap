"""Pause menu must not become final_screen. A real end screen still must.

These tests do not import qor_receipt. The pixel fixture is the 6-item black menu that
``madden_final_screen`` already accepts (the shape a Practice pause can share on the way in).
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from qorcap_endmenu import EndMenuAgreement
from qorcap_reader import (
    OPTIONS_BIT,
    ReaderCore,
    madden_final_screen,
    options_presses_ms,
)


def _menu(items=6, bg=2):
    img = np.full((360, 640, 3), bg, np.uint8)
    img[22:42, 20:150] = 230
    for k in range(1, items):
        y = 30 + 15 * k
        img[y:y + 6, 25:110] = 200
    img[30:60, 300:420] = 220
    return img


class _Paused:
    paused = True

    def update(self, t, img):
        pass


class _Fin:
    def __init__(self, **k):
        self.__dict__.update(k)


def _detect_final(timeline):
    """Stand-in for QorTime detect_final: Q4, clock 0:00, final_screen span of 3 frames and 3 s."""
    scores = [r for r in timeline if isinstance(r, dict) and "home" in r and "obs" not in r]
    if not scores:
        return _Fin(final=False)
    last = scores[-1]
    q = last.get("quarter")
    if isinstance(q, bool) or not isinstance(q, int) or q < 4 or last.get("clock_s") != 0:
        return _Fin(final=False)
    if last.get("home") == last.get("away"):
        return _Fin(final=False)
    for o in timeline:
        if (o.get("obs") == "final_screen" and (o.get("n") or 0) >= 3
                and int(o["until_ms"]) - int(o["t_ms"]) >= 3000 and o["t_ms"] > last["t_ms"]):
            return _Fin(final=True, basis="final_screen", home=last["home"], away=last["away"],
                        quarter=q, t_ms=last["t_ms"])
    return _Fin(final=False)


class _MenuRead:
    ok = True
    reason = "ok"
    conf = 1.0
    margin = 1.0
    digits = []

    def __init__(self, away, home):
        self.away, self.home = away, home


class _MenuReader:
    """Would confirm whatever score it is given, if the reader is allowed to run."""

    def __init__(self, away, home):
        self.away, self.home = away, home

    def read(self, _img):
        return _MenuRead(self.away, self.home)


def _core(score, presses=(), reader=None):
    posts = []
    core = ReaderCore(posts.append, None, None, _Paused(), _detect_final,
                      final_screen_detector=madden_final_screen, end_menu_reader=reader,
                      end_menu_agree=EndMenuAgreement() if reader is not None else None)
    core.timeline = [dict(score)]
    core.note_options(list(presses))
    return core, posts


def _feed(core, img, t0, n, step=250):
    for k in range(n):
        core.on_frame(t0 + k * step, img)


def test_pixel_fixture_would_match_the_end_menu_detector():
    assert madden_final_screen(_menu(6, 2))
    assert not madden_final_screen(_menu(10, 2))


def test_options_presses_are_rising_edges_on_the_wall_clock():
    anchor = 1_700_000_000_000
    recs = [
        {"type": "header", "wall_anchor_ms": anchor},
        {"type": "in", "t_us": 0, "b": 0},
        {"type": "in", "t_us": 5_000_000, "b": OPTIONS_BIT},
        {"type": "in", "t_us": 5_100_000, "b": OPTIONS_BIT},
        {"type": "in", "t_us": 6_000_000, "b": 0},
        {"type": "in", "t_us": 7_000_000, "b": OPTIONS_BIT | 1},
    ]
    assert options_presses_ms(recs) == [anchor + 5_000, anchor + 7_000]


def test_practice_pause_after_options_is_not_a_final_screen():
    """match-20261020-171032: OPTIONS in Practice/Play Now, score still 7-0, game not over."""
    menu = _menu()
    score = {"t_ms": 1_000, "home": 0, "away": 7, "quarter": 1, "clock_s": 400, "clock_red": False}
    press = 2_000
    core, posts = _core(score, presses=[press], reader=_MenuReader(7, 0))
    _feed(core, menu, press + 800, 16)  # 4 s, under the 12 s coincident hold
    obs = [p for p in posts if p.get("obs") == "final_screen"]
    finals = [p for p in posts if p.get("type") == "final"]
    assert obs == []
    assert finals == []
    assert core.final is None
    assert core.end_menu is None
    assert not any(r.get("obs") == "final_screen" for r in core.timeline)
    assert core.stats.get("final_screen_rejected", 0) >= 1


def test_q4_options_pause_does_not_confirm_even_if_the_menu_reader_would():
    """Clock already 0:00. The stub reader would confirm 7-0 if this were treated as the end menu."""
    menu = _menu()
    score = {"t_ms": 1_000, "home": 0, "away": 7, "quarter": 4, "clock_s": 0, "clock_red": True}
    press = 2_000
    core, posts = _core(score, presses=[press], reader=_MenuReader(7, 0))
    _feed(core, menu, press + 800, 16)
    assert [p for p in posts if p.get("type") == "final"] == []
    assert [p for p in posts if p.get("obs") == "final_screen"] == []
    assert core.final is None
    assert core.end_menu is None


def test_genuine_final_screen_is_still_detected_and_does_not_confirm_by_itself():
    """Q4 0:00, no OPTIONS, picture stays past 3 s. Scorebug final is not a confirmed score."""
    menu = _menu()
    score = {"t_ms": 1_000, "home": 0, "away": 7, "quarter": 4, "clock_s": 0, "clock_red": True}
    core, posts = _core(score)
    _feed(core, menu, 5_000, 20)
    screens = [p for p in posts if p.get("obs") == "final_screen"]
    assert screens and screens[0]["n"] >= 3 and screens[0]["until_ms"] - screens[0]["t_ms"] >= 3000
    finals = [p for p in posts if p.get("type") == "final"]
    assert len(finals) == 1
    assert finals[0]["basis"] == "final_screen"
    assert (finals[0]["home"], finals[0]["away"], finals[0]["quarter"]) == (0, 7, 4)
    assert finals[0].get("final_confirmed") is not True


def test_options_coincident_with_a_real_ending_still_confirms_after_the_hold():
    """Fail closed the other way: OPTIONS at the whistle must not hide an ending that stays up."""
    menu = _menu()
    score = {"t_ms": 3_500, "home": 0, "away": 7, "quarter": 4, "clock_s": 0, "clock_red": True}
    press = 4_000
    core, posts = _core(score, presses=[press], reader=_MenuReader(7, 0))
    _feed(core, menu, 4_500, 70)  # 17 s: past the 12 s hold and the 3 s span
    finals = [p for p in posts if p.get("type") == "final" and p.get("basis") == "end_menu"]
    assert finals, posts
    assert finals[0]["final_confirmed"] is True
    assert (finals[0]["home"], finals[0]["away"]) == (0, 7)
    assert any(p.get("obs") == "final_screen" for p in posts)


def test_short_final_screen_flash_is_dropped_and_not_posted():
    menu = _menu()
    score = {"t_ms": 1_000, "home": 0, "away": 7, "quarter": 4, "clock_s": 0, "clock_red": True}
    core, posts = _core(score)
    _feed(core, menu, 5_000, 2, step=200)
    core.on_frame(5_500, np.zeros((360, 640, 3), np.uint8))
    assert [p for p in posts if p.get("obs") == "final_screen"] == []
    assert [p for p in posts if p.get("type") == "final"] == []
    assert not any(r.get("obs") == "final_screen" for r in core.timeline)
