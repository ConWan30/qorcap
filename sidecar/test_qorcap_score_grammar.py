"""Score-transition grammar. No clips and no ffmpeg: both CI jobs run this.

Illegal jumps abstain and are not snapped to a nearby legal score. Legal football steps pass.
The grammar never raises a confidence.
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, os.environ.get("QORTIME", "/workspace/qortime-src"))

from qor_receipt._vendor.qoresence.vision.local_scorebug.reader import FrameRead  # noqa: E402
from qorcap_clock import ClockHistory  # noqa: E402
from qorcap_score_grammar import BANNER_WINDOW_MS, TRY_WINDOW_MS, ScoreGrammar, judge  # noqa: E402


def _fr(away, home, conf=0.91, margin=0.2, quarter=3):
    return FrameRead(
        ok=True, reason="ok", profile_id="madden27_standard",
        left_score=away, right_score=home, quarter=quarter,
        score_conf=conf, score_margin=margin,
    )


def _locked(away, home):
    g = ScoreGrammar()
    g.note_lock(0, away, home)
    return g


def test_windows_are_the_documented_defaults():
    g = ScoreGrammar()
    assert g.banner_window_ms == BANNER_WINDOW_MS == 12_000
    assert g.try_window_ms == TRY_WINDOW_MS == 45_000
    g.note_lock(0, 0, 0)
    g.note_banner(0, "fg")
    assert g.active_banner(12_000) == "fg"
    assert g.active_banner(12_001) is None


def test_illegal_jumps_abstain_and_are_not_rewritten():
    g = _locked(3, 0)
    # 3-0 -> 7-0 is +4: no touchdown banner and no try.
    out = g.filter(_fr(7, 0), 1000, None)
    assert (out.left_score, out.right_score) == (None, None)
    assert out.reason == "score_transition" and out.quarter == 3
    assert g.locked == (3, 0)  # a read does not move the lock, and nothing is snapped to 6 or 9
    # 3-0 -> 4-0 is a bare +1.
    assert g.filter(_fr(4, 0), 2000, None).left_score is None
    # A decrease is always illegal.
    assert g.filter(_fr(2, 0), 3000, None).left_score is None
    assert g.filter(_fr(3, 0), 3000, None).right_score == 0  # same score is not a decrease
    down = _locked(3, 1)
    assert down.filter(_fr(3, 0), 1000, None).right_score is None
    # +3 needs a field goal. A touchdown banner does not make it legal.
    td = _locked(3, 0)
    td.note_banner(1000, "td")
    assert td.filter(_fr(6, 0), 1000, "td").left_score is None
    # +6 needs a touchdown. A field-goal banner does not make it legal.
    fg = _locked(3, 0)
    fg.note_banner(1000, "fg")
    assert fg.filter(_fr(9, 0), 1000, "fg").left_score is None
    # +7 with no banner is not deferred.
    bare = _locked(3, 0)
    assert bare.filter(_fr(10, 0), 1000, None).left_score is None


def test_legal_steps_pass_and_confidence_is_not_raised():
    # Touchdown, then +6, then the try's +1.
    g = _locked(0, 0)
    g.note_banner(1000, "td")
    step = g.filter(_fr(6, 0, conf=0.72, margin=0.09), 1000, "td")
    assert (step.left_score, step.right_score) == (6, 0)
    assert step.score_conf == 0.72 and step.score_margin == 0.09 and step.ok
    g.note_lock(1000, 6, 0)
    assert g.phase == "try" and g.td_side == "away"
    pat = g.filter(_fr(7, 0, conf=0.72), 2000, None)
    assert (pat.left_score, pat.right_score) == (7, 0)
    assert pat.score_conf == 0.72
    # Combined +7 in one step, then +8 in one step. The try is already inside the step.
    both = _locked(3, 0)
    both.note_banner(100, "td")
    assert both.filter(_fr(10, 0), 100, "td").pair == (10, 0)
    both.note_lock(100, 10, 0)
    assert both.phase is None
    eight = _locked(0, 0)
    eight.note_banner(100, "td")
    assert eight.filter(_fr(8, 0), 100, "td").pair == (8, 0)
    # Safety is +2 with no banner.
    safety = _locked(0, 0)
    assert safety.filter(_fr(0, 2), 50, None).pair == (0, 2)
    # Unknown team: the banner names neither side, so the other side is a candidate too.
    home_fg = _locked(0, 0)
    home_fg.note_banner(10, "fg")
    assert home_fg.filter(_fr(0, 3), 10, "fg").pair == (0, 3)
    home_td = _locked(0, 0)
    home_td.note_banner(10, "td")
    assert home_td.filter(_fr(0, 6), 10, "td").pair == (0, 6)
    # Defensive two-point return: +2 on the other side during the try.
    ret = _locked(0, 0)
    ret.note_banner(10, "td")
    ret.note_lock(10, 6, 0)
    assert ret.filter(_fr(6, 2), 20, None).pair == (6, 2)


def test_no_banner_defers_a_field_goal_and_a_touchdown_to_the_ordinary_path():
    g = _locked(0, 0)
    fg = g.filter(_fr(3, 0), 100, None)
    assert fg.pair == (3, 0) and fg.ok and fg.reason == "ok"
    assert judge((0, 0), (3, 0), None, None, None) == "defer"
    assert judge((3, 0), (9, 0), None, None, None) == "defer"
    assert judge((0, 0), (3, 0), "fg", None, None) == "accept"


def test_a_spent_banner_cannot_authorise_a_second_jump():
    g = _locked(0, 0)
    g.note_banner(100, "fg")
    g.note_lock(200, 3, 0)
    assert g.active_banner(300) is None
    # The banner is still in the crop, but it was spent. +6 is the ordinary path, not an FG abstain.
    out = g.filter(_fr(9, 0), 400, "fg")
    assert out.pair == (9, 0)
    assert g.active_banner(400) is None
    g.note_lock(400, 9, 0)
    # A different kind can arm, and the +6 it explains is kept.
    hit = g.filter(_fr(15, 0), 500, "td")
    assert hit.pair == (15, 0)
    assert g.active_banner(500) == "td"


def test_try_window_is_configurable_and_a_late_point_abstains():
    g = ScoreGrammar(banner_window_ms=500, try_window_ms=1000)
    g.note_lock(0, 0, 0)
    g.note_banner(0, "td")
    g.note_lock(10, 6, 0)
    assert g.filter(_fr(7, 0), 1010, None).pair == (7, 0)
    late = g.filter(_fr(7, 0), 1011, None)
    assert late.left_score is None and late.reason == "score_transition"


def test_a_backward_timestamp_starts_a_new_sequence():
    g = _locked(9, 0)
    g.note_lock(5_000, 9, 0)
    g.note_banner(100, "fg")
    assert g.locked is None
    assert g.active_banner(100) == "fg"


def test_clock_history_holds_or_counts_down_and_ignores_a_missing_timestamp():
    h = ClockHistory()
    assert h.filter(63, 3, None) == 63 and h.last_s is None
    assert h.filter(63, 3, 0) == 63
    assert h.filter(62, 3, 400) == 62
    assert h.filter(63, 3, 800) is None  # upward flicker
    assert h.last_s == 62  # the baseline did not follow it
    assert h.filter(62, 3, 1200) == 62  # stoppage holds
    assert h.filter(900, 4, 2000) == 900  # a new quarter may reset, up to 15:00
    assert h.filter(10, 4, 100) == 10  # a new clip rewinds
    assert h.last_s == 10
