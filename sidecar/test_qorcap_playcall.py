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


def test_gate_unknown_quarter_uses_clock_reset_and_not_q1_for_the_end():
    """No quarter: a running clock after two period resets opens halftime. Q1 never opens the end."""
    g = PauseCapableGate(proxy_after_ms=1_000)
    _read(g, 0, None, 900, 3, 0)              # score already on the board, no period seen: nothing
    _read(g, 1_000, None, 880, 3, 0)
    assert [g.on_frame(t, False) for t in range(1_000, 20_000, 500)] == [None] * 38
    g = PauseCapableGate(proxy_after_ms=1_000)
    _read(g, 0, None, 900)                    # 0-0 with a running clock is the opening quarter: kickoff only
    _read(g, 1_000, None, 880)
    evs = [e for e in (g.on_frame(t, False) for t in range(1_000, 20_000, 500)) if e]
    assert [(e.phase, e.quarter) for e in evs] == [("kickoff", 1)]

    g = PauseCapableGate(proxy_after_ms=1_000)
    _read(g, 0, 1, 1)                         # Q1 ran out
    _read(g, 2_000, None, 900)                # Q2 at 15:00, quarter glyph missed
    _read(g, 3_000, None, 880)
    early = [e for e in (g.on_frame(t, False) for t in range(3_000, 8_000, 500)) if e]
    assert [e.phase for e in early] == ["kickoff"]
    _read(g, 9_000, None, 0)                  # Q2 ran out
    _read(g, 10_000, None, 900)               # Q3
    _read(g, 11_000, None, 880)
    evs = [e for e in (g.on_frame(t, False) for t in range(11_000, 16_000, 500)) if e]
    assert len(evs) == 1 and evs[0].basis == "clock_proxy" and evs[0].phase == "halftime" and evs[0].quarter == 3


def test_gate_elapsed_fallback_when_the_clock_dies_at_the_quarter_break():
    """Clock empty after Q1 0:00: halftime then end by elapsed time. Nothing before the clock runs out."""
    from qorcap_reader import ELAPSED_PERIOD_MS

    g = PauseCapableGate()
    _read(g, 0, None, None)
    assert g.on_frame(4 * ELAPSED_PERIOD_MS, False) is None
    g = PauseCapableGate()
    _read(g, 0, 1, 1)
    _read(g, 1_000, None, None)
    _read(g, 1_000 + ELAPSED_PERIOD_MS - 5_000, None, None)              # the scorebug is still up, clock blank
    assert g.on_frame(1_000 + ELAPSED_PERIOD_MS, False) is None          # one period later is still Q2
    _read(g, 1_000 + 2 * ELAPSED_PERIOD_MS - 5_000, None, None)
    half = g.on_frame(1_000 + 2 * ELAPSED_PERIOD_MS, False)
    assert half is not None and half.basis == "elapsed" and half.phase == "halftime"
    _read(g, 1_000 + 3 * ELAPSED_PERIOD_MS - 5_000, None, None)
    end = g.on_frame(1_000 + 3 * ELAPSED_PERIOD_MS, False)
    assert end is not None and end.basis == "elapsed" and end.phase == "end" and end.quarter == 4


def test_gate_elapsed_fallback_needs_the_scorebug_on_screen():
    """match-20261018-002316 was quit at the half. 24 min after the Q1 rundown it was in the menus with no
    scorebug for almost 6 min; v0.3.20 issued "halftime" there. A stale last read must not open a phase."""
    from qorcap_playcall import ELAPSED_READ_FRESH_MS
    from qorcap_reader import ELAPSED_PERIOD_MS

    g = PauseCapableGate()
    _read(g, 0, None, 1, 21, 31)
    _read(g, 1_000, None, None, 21, 37)
    t = 1_000 + 2 * ELAPSED_PERIOD_MS
    assert g.on_frame(t, False) is None
    _read(g, t + 1_000 - ELAPSED_READ_FRESH_MS + 2_000, None, None, 21, 37)  # scorebug back: the fallback may fire
    assert g.on_frame(t + 1_000, False).phase == "halftime"
    g = PauseCapableGate()
    _read(g, 0, None, 1, 21, 31)
    _read(g, 1_000, None, None, 21, 37)
    _read(g, t, None, None, 0, 0)                 # the next game's 0-0 intro after the menus: not a second half
    assert g.on_frame(t + 1_000, False) is None


def _six_minute_game(g, q_of=lambda q: None, step=1_000):
    """A Thursday Night game with 6:00 quarters, as on match-20261017-232726: each quarter 6:00 -> 0:00."""
    t = 0
    for q in (1, 2, 3, 4):
        for c in range(360, -1, -2):
            _read(g, t, q_of(q), c, 7 * (q - 1), 3 * (q - 1) if q > 1 else 0)
            g.on_frame(t, False)
            t += step
        t += 60_000                         # quarter break: no scorebug
    return t


def test_gate_short_quarters_open_every_phase_without_a_quarter_glyph():
    """6:00 quarters never reached the 10:00 reset rule, so v0.3.20 never counted a period. Now every reset counts
    after a real rundown: kickoff in Q1 (0-0), halftime in Q3, end in Q4, each once from the clock proxy."""
    g = PauseCapableGate(proxy_after_ms=45_000)
    _six_minute_game(g)
    got = [(e.phase, e.quarter) for e in g.events]
    assert ("kickoff", 1) in got and ("halftime", 3) in got and ("end", 4) in got
    assert all(e.basis == "clock_proxy" for e in g.events)
    assert g.periods.periods_completed == 3
    # Phases arrive in order, each one at least 45 s into its own quarter (never at a reset or a rundown).
    order = [e.phase for e in g.events]
    assert order.index("kickoff") < order.index("halftime") < order.index("end")
    for e in g.events:
        assert e.clock_s is not None and 0 < e.clock_s < 360


def test_gate_short_quarters_same_events_when_the_glyph_is_read():
    a = PauseCapableGate(proxy_after_ms=45_000)
    _six_minute_game(a, q_of=lambda q: q)
    b = PauseCapableGate(proxy_after_ms=45_000)
    _six_minute_game(b)
    assert [(e.phase, e.quarter) for e in a.events if e.phase != "kickoff" or e.quarter == 1] == \
           [(e.phase, e.quarter) for e in b.events if e.phase != "kickoff" or e.quarter == 1]


def test_lone_low_clock_misread_mid_quarter_is_not_a_period():
    """A single 0:03 between 3:00 reads is a misread, not a rundown: the next 5:50 must not count as a new quarter."""
    from qorcap_reader import PeriodTrack

    p = PeriodTrack()
    for t, c in enumerate([360, 300, 240, 180, 3, 178, 350, 170]):
        p.note(t * 1_000, c)
    assert p.periods_completed == 0
    for t, c in enumerate([60, 30, 2, 0, 360, 358], start=10):
        p.note(t * 1_000, c)
    assert p.periods_completed == 1


def test_short_reset_needs_a_clock_near_the_quarter_length():
    """After a real rundown, a 2:00 read is not a 6:00 reset. 15:00 quarters keep the 10:00 rule."""
    from qorcap_reader import PeriodTrack

    p = PeriodTrack()
    for t, c in enumerate([360, 200, 60, 1, 120, 200, 320]):
        p.note(t * 1_000, c)
    assert p.periods_completed == 0          # 320 < 360 - 30
    p.note(10_000, 340)
    assert p.periods_completed == 1
    q = PeriodTrack()
    for t, c in enumerate([900, 300, 50, 0, 590]):
        q.note(t * 1_000, c)
    assert q.periods_completed == 0 and q.reset_clock_s() == 600
    q.note(9_000, 900)
    assert q.periods_completed == 1


def test_opening_quarter_is_not_inferred_after_a_rundown_or_with_points():
    g = PauseCapableGate(proxy_after_ms=1_000)
    _read(g, 1_000, None, 2, 0, 0)            # recording opens on a rundown: whatever comes next is not "Q1"
    _read(g, 2_000, None, None, 0, 0)
    _read(g, 3_000, None, 2, 0, 0)
    assert g.quarter is None
    assert all(g.on_frame(t, False) is None for t in range(3_000, 10_000, 500))
    g = PauseCapableGate(proxy_after_ms=1_000)
    _read(g, 0, None, 300, 7, 0)              # points on the board: Q1 or Q2, unknown
    _read(g, 1_000, None, 290, 7, 0)
    assert g.quarter is None
    assert all(g.on_frame(t, False) is None for t in range(1_000, 10_000, 500))


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
