"""v0.3.22 touchdown jump: a brief +6/+7/+8 locks on fewer reads only when every bar frame corroborates it."""
import os
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, os.environ.get("QORTIME", "/workspace/qortime-src"))
from qor_receipt._vendor.qoresence.vision.local_scorebug.agreement import AgreementWindow  # noqa: E402
from qor_receipt._vendor.qoresence.vision.local_scorebug.reader import FrameRead  # noqa: E402
from qorcap_reader import ReaderCore, TouchdownJump  # noqa: E402


def fr(a, h, q=2, clock=None, ok=None, reason=None, margin=0.4):
    if a is None:
        return FrameRead(ok=False, reason="layout_unknown", profile_id="madden27_thursday_night")
    ok = clock is not None if ok is None else ok
    return FrameRead(ok=ok, reason=reason or ("ok" if ok else "clock_unsure"), profile_id="madden27_thursday_night",
                     left_score=a, right_score=h, quarter=q, clock_seconds=clock, score_conf=0.9, score_margin=margin, quarter_conf=0.9)


class Seq:
    profile_id = "madden27_thursday_night"

    def __init__(self):
        self.next = None

    def read(self, img):
        self.n = getattr(self, "n", 0) + 1
        return self.next

    def reference_band(self, img):
        import numpy as np
        return np.full((2, 2), getattr(self, "n", 0) % 251, np.uint8), "ok"


def run(frames, step=250, touchdown=True, base=(21, 31), td=None):
    """frames: list of FrameRead. Starts with ``base`` Q2 0:10 locked by six good reads."""
    rd, posts = Seq(), []
    core = ReaderCore(posts.append, rd, AgreementWindow(), None, lambda tl: NS(final=False))
    if not touchdown:
        core.touchdown = None
    elif td is not None:
        core.touchdown = td
    t = 0
    for f in [fr(base[0], base[1], 2, 10)] * 6 + list(frames):
        rd.next = f
        core.on_frame(t, None)
        t += step
    return [(p["away"], p["home"], p["quarter"]) for p in posts if p["type"] == "score"], core


# 002316 at 4 fps from 2ND 0:08: the TD, two reads, the TOUCHDOWN banner, then four clock-less reads and two good ones.
D02316 = [fr(21, 37), fr(21, 37)] + [fr(None, None)] * 10 + [fr(21, 37)] * 4 + [fr(21, 37, 2, 7)] * 2 + [fr(None, None)] * 5


def test_002316_touchdown_locks_21_37_q2():
    got, core = run(D02316)
    assert got[-1] == (21, 37, 2) and core.stats["touchdown_jump"] == 1
    old, _ = run(D02316, touchdown=False)
    assert old[-1] == (21, 31, 2)                     # v0.3.21: the TD never locked


@pytest.mark.parametrize("frames,why", [
    ([fr(21, 37)] * 3, "three reads only"),
    ([fr(21, 34)] * 8, "+3 is a field goal: the normal windows only"),
    ([fr(21, 33)] * 8, "+2 safety"),
    ([fr(27, 37)] * 8, "both sides moved"),
    ([fr(21, 37), fr(21, 31), fr(21, 37), fr(21, 37), fr(21, 37)], "the old pair is still on the bar"),
    ([fr(21, 37), fr(21, 37), fr(21, 38), fr(21, 37), fr(21, 37)], "a third pair"),
    ([fr(21, 37, 3)] * 8, "a different quarter"),
    ([fr(21, 37)] * 3 + [fr(21, 37, 2, 40)] * 2 + [fr(21, 37)] * 6, "the clock went up"),
    ([fr(21, 37), fr(21, 37), fr(21, 37, 2, 5), fr(21, 37, 2, 6), fr(21, 37)], "the clock rose between the new reads"),
    ([fr(21, 37)] + [fr(None, None)] * 40 + [fr(21, 37)] * 3, "spread over more than 8 s"),
    ([fr(21, 37), fr(21, 31)] + [fr(21, 37)] * 6, "old pair after the new one blocks it"),
    ([fr(21, 37), fr(21, 38)] + [fr(21, 37)] * 6, "two candidate pairs"),
    ([fr(21, 37, 2, 10, ok=False, reason="quarter_unsure")] * 1 + [fr(21, 31, 2, 10)] + [fr(21, 37)] * 3, "reset by the old pair"),
])
def test_uncorroborated_jumps_do_not_lock_early(frames, why):
    got, core = run(frames)
    assert got[-1] == (21, 31, 2), why
    assert core.stats.get("touchdown_jump", 0) == 0, why


def test_span_needs_750ms():
    got, core = run([fr(21, 37)] * 4, step=200)  # 4 reads, 600 ms
    assert got[-1] == (21, 31, 2) and not core.stats.get("touchdown_jump")


def test_a_normal_lock_is_unchanged():
    """Six good reads of the new pair lock through AgreementWindow as before; the touchdown path is not used."""
    got, core = run([fr(21, 37, 2, 7)] * 6)
    assert got[-1] == (21, 37, 2) and not core.stats.get("touchdown_jump")


def test_unit_needs_a_posted_pair():
    td = TouchdownJump()
    for t in range(0, 3000, 250):
        assert td.offer(fr(7, 0), t, None, None) is None


# ---- v0.3.23 safeguard ---------------------------------------------------------------------------------------------
from qorcap_reader import TD_VETO_WAIT_MS, td_confusable  # noqa: E402

NO = [fr(None, None)] * 12


def test_lookalike_table():
    assert td_confusable(0, 8) and td_confusable(0, 6) and td_confusable(10, 18) and td_confusable(3, 9)
    assert not td_confusable(31, 37) and not td_confusable(7, 13) and not td_confusable(0, 7) and not td_confusable(14, 20)


def test_021003_zero_read_as_eight_does_not_lock():
    # QOR 13 - EVE 0 on the bar; the 0 matched the 8 template on four frames (margin ~0.09), then 13-0 came back.
    bad = [fr(13, 8, 2, None, margin=0.09)] * 4 + [fr(13, 0, 2, None, margin=0.21)] + NO
    got, core = run(bad, base=(13, 0))
    assert (13, 8, 2) not in got and core.stats.get("touchdown_jump", 0) == 0


def test_weak_margin_cancels_even_without_the_old_pair():
    got, core = run([fr(21, 37, margin=0.1)] * 6 + NO)
    assert got[-1] == (21, 31, 2) and core.stats.get("touchdown_jump", 0) == 0


def test_reads_without_any_margin_never_take_the_fast_path():
    got, core = run([fr(21, 37, margin=None)] * 6 + NO)
    assert got[-1] == (21, 31, 2)


def test_old_pair_during_the_hold_cancels():
    got, core = run([fr(21, 37)] * 4 + [fr(21, 31)] + NO)
    assert got[-1] == (21, 31, 2) and core.stats.get("touchdown_jump", 0) == 0


def test_third_pair_during_the_hold_cancels():
    got, core = run([fr(21, 37)] * 4 + [fr(21, 38)] + NO)
    assert got[-1] == (21, 31, 2)


def test_hold_delays_the_post_by_at_least_one_second():
    events = []
    td = TouchdownJump(listener=events.append)
    got, core = run([fr(21, 37)] * 4 + NO, td=td)
    assert got[-1] == (21, 37, 2)
    pend = next(e for e in events if e["event"] == "pending")
    post = next(e for e in events if e["event"] == "posted")
    assert post["t_ms"] - pend["t_ms"] >= 1000 and post["cand"] == [21, 37] and pend["old"] == [21, 31]


def test_lookalike_needs_strong_margins_and_confirmation():
    # 13-0 -> 13-8 with strong matches: still needs two more reads of 13-8 during a 2 s hold.
    unconfirmed = [fr(13, 8, 2, None, margin=0.3)] * 4 + NO * 3
    got, _ = run(unconfirmed, base=(13, 0))
    assert (13, 8, 2) not in got
    medium = [fr(13, 8, 2, None, margin=0.16)] * 4 + [fr(13, 8, 2, None, margin=0.16)] * 3 + NO
    got, _ = run(medium, base=(13, 0))
    assert (13, 8, 2) not in got  # 0.16 < 0.18: no fast path (normal windows only)
    confirmed = [fr(13, 8, 2, None, margin=0.3)] * 4 + [fr(None, None)] * 2 + [fr(13, 8, 2, None, margin=0.3)] * 2 + NO
    got, core = run(confirmed, base=(13, 0))
    assert got[-1] == (13, 8, 2) and core.stats["touchdown_jump"] == 1


def test_listener_errors_never_break_the_reader():
    def boom(_):
        raise RuntimeError("x")
    got, core = run(D02316, td=TouchdownJump(listener=boom))
    assert got[-1] == (21, 37, 2)


def test_veto_off_by_default():
    assert TouchdownJump().veto is None
    rd = Seq()
    core = ReaderCore(lambda b: None, rd, AgreementWindow(), None, lambda tl: NS(final=False))
    assert core.touchdown.veto is None and core.touchdown.listener is None


def test_enforcing_veto_can_only_cancel_or_delay():
    calls = []
    got, core = run(D02316, td=TouchdownJump(veto=lambda pd: calls.append(pd["cand"]) or "disagree"))
    assert got[-1] == (21, 31, 2) and calls == [(21, 37)]
    got, core = run(D02316, td=TouchdownJump(veto=lambda pd: "agree"))
    assert got[-1] == (21, 37, 2)
    # no verdict: the lock waits up to TD_VETO_WAIT_MS, then posts (a delay, never a block)
    n = TD_VETO_WAIT_MS // 250 + 8
    got, core = run(D02316 + [fr(None, None)] * n, td=TouchdownJump(veto=lambda pd: None))
    assert got[-1] == (21, 37, 2)
    # the veto is never asked when nothing is pending, so it cannot create a lock
    asked = []
    got, core = run([fr(21, 31, 2, 9)] * 10, td=TouchdownJump(veto=lambda pd: asked.append(1) or "agree"))
    assert asked == [] and got[-1] == (21, 31, 2)
