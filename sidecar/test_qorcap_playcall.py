"""v0.3.11: challenges open only at pause-capable moments (after a kickoff and a play call)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from qorcap_playcall import PauseCapableGate, PlayCallDetector, phase_for_quarter

FIX = HERE / "fixtures" / "playcall"


def _img(p):
    cv2 = pytest.importorskip("cv2")
    im = cv2.imread(str(p))
    assert im is not None, p
    return im


@pytest.mark.parametrize("p", sorted(FIX.glob("pc_*.jpg")), ids=lambda p: p.stem)
def test_play_call_screens_detected(p):
    """Defense / offense cards and the personnel menu, in standard, Sunday Night and Thursday Night presentations."""
    assert PlayCallDetector().detect(_img(p))


@pytest.mark.parametrize("p", sorted(FIX.glob("no_*.jpg")) + [HERE / "fixtures" / "m27_pause_menu.jpg"],
                         ids=lambda p: p.stem)
def test_not_play_call(p):
    """Pre-snap at the line (pause-capable, but the cards are gone), stadium shadow, menus, the pause menu."""
    assert not PlayCallDetector().detect(_img(p))


def test_phase_for_quarter():
    assert [phase_for_quarter(q) for q in (None, 0, 1, 2, 3, 4, 5)] == [None, None, "kickoff", "kickoff", "halftime",
                                                                         "end", "end"]


def _read(g, t, q, c, a=0, h=0):
    g.on_read({"t_ms": t, "quarter": q, "clock_s": c, "away": a, "home": h})


def _call(g, t0, dur_ms, step=250):
    out = []
    for t in range(t0, t0 + dur_ms, step):
        out.append(g.on_frame(t, True))
    t = t0 + dur_ms
    while t < t0 + dur_ms + 1_500:
        out.append(g.on_frame(t, False))
        t += step
    return [e for e in out if e is not None]


def test_gate_waits_for_kickoff_then_play_call():
    """Kickoff play-call screen (clock not yet running) never opens a window; the first play call after the clock
    has run does (the players are at the line, pre-snap: OPTIONS works online)."""
    g = PauseCapableGate()
    _read(g, 1_000, 1, 360)
    assert _call(g, 2_000, 4_000) == []            # kickoff / return selection: clock has not run
    _read(g, 10_000, 1, 360)
    _read(g, 20_000, 1, 352)                        # return fielded: the clock runs
    assert g.running_since == 20_000
    for t in range(20_000, 26_000, 250):
        assert g.on_frame(t, False) is None         # live return: nothing opens
    ev = _call(g, 26_000, 5_000)
    assert len(ev) == 1 and ev[0].basis == "play_call" and ev[0].phase == "kickoff" and ev[0].quarter == 1
    assert g.ready("kickoff", 0) is ev[0] and g.ready("halftime", 0) is None


def test_gate_blips_and_pause_do_not_count():
    g = PauseCapableGate()
    _read(g, 0, 1, 360)
    _read(g, 1_000, 1, 350)
    assert _call(g, 2_000, 500) == []                # a 0.5 s look-alike (score graphic) is not a play call
    for t in range(3_000, 6_000, 250):
        g.on_frame(t, True)
    g.on_frame(6_000, False, paused=True)            # paused mid-way: forget the run
    assert [g.on_frame(t, False) for t in range(6_250, 9_000, 250)] == [None] * 11


def test_gate_second_half_needs_its_own_kickoff():
    g = PauseCapableGate()
    _read(g, 0, 2, 5)
    _read(g, 1_000, 2, 0)
    _read(g, 60_000, 3, 360)                         # Q3 first read = second-half kickoff about to happen
    assert _call(g, 61_000, 4_000) == []            # second-half kickoff selection
    _read(g, 70_000, 3, 355)
    ev = _call(g, 80_000, 3_000)
    assert [e.phase for e in ev] == ["halftime"]


def test_gate_clock_proxy_fallback_once_per_quarter():
    g = PauseCapableGate(proxy_after_ms=45_000)
    _read(g, 0, 4, 900)
    _read(g, 5_000, 4, 890)
    evs = [g.on_frame(t, False) for t in range(5_000, 60_000, 500)]
    evs = [e for e in evs if e]
    assert len(evs) == 1 and evs[0].basis == "clock_proxy" and evs[0].phase == "end"
    assert evs[0].t_ms == 50_000


def test_reader_core_feeds_gate():
    """ReaderCore calls the detector on every unpaused frame and the gate on every agreed read."""
    from types import SimpleNamespace as NS

    from qorcap_reader import ReaderCore

    class R:
        def __init__(self):
            self.clock = 360

        def read(self, img):
            return NS(ok=True, reason="ok")

    class A:
        def __init__(self, r):
            self.r = r

        def offer(self, fr, **kw):
            return NS(agreed=True, read=NS(left_score=3, right_score=7, quarter=1, clock_seconds=self.r.clock))

    class PC:
        flag = False

        def detect(self, img):
            return self.flag

    np = pytest.importorskip("numpy")
    r, pc = R(), PC()
    g = PauseCapableGate()
    core = ReaderCore(lambda b: None, r, A(r), None, lambda tl: NS(final=False), playcall=pc, gate=g)
    img = np.zeros((360, 640, 3), np.uint8)
    core.on_frame(0, img)
    r.clock = 350
    core.on_frame(1_000, img)
    pc.flag = True
    for t in range(2_000, 5_000, 250):
        core.on_frame(t, img)
    pc.flag = False
    for t in range(5_000, 7_000, 250):
        core.on_frame(t, img)
    assert [e.phase for e in g.events] == ["kickoff"] and core.stats["pause_capable"] == 1
    assert g.events[0].to_dict()["score"] == {"away": 3, "home": 7}
