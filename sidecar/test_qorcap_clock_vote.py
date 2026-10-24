"""Clock rules, the running-clock re-anchor, the lock vote, and inferred quarters.

No clips and no ffmpeg. Both CI jobs run this. Held-out matches are not read here.
"""
import json
import os
import random
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, os.environ.get("QORTIME", "/workspace/qortime-src"))

from qor_receipt._vendor.qoresence.vision.local_scorebug.agreement import (  # noqa: E402
    AgreementConfig, AgreementWindow,
)
from qor_receipt._vendor.qoresence.vision.local_scorebug.reader import FrameRead  # noqa: E402
from qorcap_clock import (  # noqa: E402
    QUARTER_CLOCK_MAX_S, ClockHistory, apply_inferred_quarter, countdown_run_ok, posted_quarter,
)
from qorcap_match import apply_final  # noqa: E402
from qorcap_reader import ReaderCore  # noqa: E402
from qorcap_score_grammar import ScoreGrammar, lock_agreement_config  # noqa: E402


def _fr(away, home, conf=0.91, margin=0.2, quarter=4, clock=None):
    return FrameRead(
        ok=True, reason="ok", profile_id="madden27_standard",
        left_score=away, right_score=home, quarter=quarter,
        score_conf=conf, score_margin=margin, clock_seconds=clock,
    )


def _blank():
    return FrameRead(ok=False, reason="score_shape", profile_id="madden27_standard", quarter=4)


def _locked(away, home):
    g = ScoreGrammar()
    g.note_lock(0, away, home)
    return g


def _seed(grammar, agree, away, home, quarter, t0=0):
    locked = False
    last = t0
    for i in range(6):
        t = t0 + i * 200
        last = t
        fr = _fr(away, home, quarter=quarter)
        res = agree.offer(fr, clock_ns=t * 1_000_000, crop_hash="crop", session_id="s")
        if res.agreed:
            grammar.note_lock(t, away, home)
            locked = True
    assert locked and grammar.locked == (away, home)
    assert grammar.allows_final_fallback() is True
    return last


def _final_from(grammar, fallback):
    base = {
        "home_score": fallback["home"], "away_score": fallback["away"], "quarter": fallback["quarter"],
        "winner": "home" if fallback["home"] > fallback["away"] else "away",
        "method": "last_scorebug_read_unconfirmed",
    }
    return apply_final(base, None, fallback=fallback, grammar_unresolved=not grammar.allows_final_fallback())


# --- clock rules ---

def test_clock_holds_and_counts_down_inside_a_quarter():
    h = ClockHistory()
    assert h.filter(63, 1, 0) == 63
    assert h.filter(63, 1, 500) == 63  # stop
    assert h.filter(62, 1, 1_000) == 62  # one second in one wall second
    assert h.rule_note is None and h.last_s == 62


def test_clock_increase_inside_a_quarter_blanks_and_is_noted():
    h = ClockHistory()
    assert h.filter(60, 1, 0) == 60
    assert h.filter(61, 1, 1_000) is None
    assert h.rule_note == "clock_increase" and h.last_s == 60
    assert h.reanchored is False


def test_a_held_increase_does_not_reanchor():
    """The stoppage gate used to accept a higher second that sat still. It must not."""
    h = ClockHistory()
    assert h.filter(60, 1, 0) == 60
    for t in (100, 800, 1_500, 2_100, 3_000):
        assert h.filter(61, 1, t) is None
        assert h.rule_note == "clock_increase"
    assert h.last_s == 60 and h.reanchor_kind is None


def test_clock_resets_at_a_higher_quarter_and_rejects_above_the_cap():
    h = ClockHistory()
    assert h.filter(30, 1, 0) == 30
    assert h.filter(QUARTER_CLOCK_MAX_S, 2, 1_000) == QUARTER_CLOCK_MAX_S
    assert h.last_q == 2 and h.continuation is False
    assert h.filter(QUARTER_CLOCK_MAX_S + 1, 3, 2_000) is None
    assert h.rule_note == "clock_above_max" and h.last_s == QUARTER_CLOCK_MAX_S


def test_a_negative_clock_blanks():
    h = ClockHistory()
    assert h.filter(10, 1, 0) == 10
    assert h.filter(-1, 1, 1_000) is None
    assert h.rule_note == "clock_negative" and h.last_s == 10


def test_quarter_going_backwards_is_a_cut():
    h = ClockHistory()
    assert h.filter(47, 4, 0) == 47
    assert h.filter(59, 3, 2_000) == 59
    assert h.last_q == 3 and h.continuation is False


def test_missing_time_and_a_quarterless_start_do_not_lock_the_baseline():
    h = ClockHistory()
    assert h.filter(63, 3, None) == 63 and h.last_s is None
    assert h.filter(61, None, 0) == 61 and h.last_s is None
    assert h.filter(900, 2, 1_000) == 900


def _feed_rate(h, start, t0, ms, step_ms, per_ms):
    """Countdown that drops one displayed second every ``per_ms`` of wall time."""
    accepted = []
    for elapsed in range(0, ms + 1, step_ms):
        secs = start - elapsed // per_ms
        got = h.filter(secs, 1, t0 + elapsed)
        if got is not None:
            accepted.append((t0 + elapsed, got))
    return accepted


def test_running_clock_reanchor_accepts_a_consistent_countdown():
    h = ClockHistory()
    assert h.filter(90, 1, 0) == 90
    # 90 -> 60 in 100 ms is a runoff. The following 1:1 run is the FU1 gate.
    accepted = _feed_rate(h, 60, 100, 3_000, 100, 1_000)
    assert accepted, "a 1:1 countdown after a runoff must re-anchor"
    t, secs = accepted[0]
    assert t - 100 >= h.run_span_ms
    assert h.reanchor_kind == "run" and h.last_run is not None
    assert countdown_run_ok(h.last_run, h.run_tol_s)
    assert h.last_run[0][1] > h.last_run[-1][1]
    assert secs == h.last_run[-1][1] <= 60
    # Once the baseline is the running clock, the next tick is an ordinary countdown.
    assert h.filter(secs - 1, 1, t + 1_000) == secs - 1
    assert h.reanchor_kind is None


def test_running_clock_reanchor_rejects_drift():
    h = ClockHistory()
    assert h.filter(90, 1, 0) == 90
    # Two game-seconds per wall-second.
    assert _feed_rate(h, 60, 100, 8_000, 100, 500) == []
    assert h.last_s == 90 and h.reanchor_kind is None
    # Half speed after the same jump. A hold is the stoppage path; this is neither.
    slow = ClockHistory()
    assert slow.filter(90, 1, 0) == 90
    assert _feed_rate(slow, 60, 100, 8_000, 100, 2_000) == []
    assert slow.last_s == 90


def test_running_clock_reanchor_rejects_an_increase():
    h = ClockHistory()
    assert h.filter(90, 1, 0) == 90
    assert h.filter(60, 1, 100) is None  # runoff candidate, not yet a run
    assert h.filter(91, 1, 200) is None
    assert h.rule_note == "clock_increase" and h.last_s == 90
    # Counting down from the increased second never keeps a value above the baseline.
    accepted = _feed_rate(h, 91, 200, 5_000, 100, 1_000)
    assert all(secs <= 90 for _, secs in accepted)
    assert h.last_s is not None and h.last_s <= 90
    assert h.filter(95, 1, 20_000) is None and h.rule_note == "clock_increase"


def test_stoppage_reanchor_still_accepts_a_held_lower_clock():
    h = ClockHistory()
    assert h.filter(90, 1, 0) == 90
    assert h.filter(60, 1, 100) is None
    assert h.filter(60, 1, 800) is None
    assert h.filter(60, 1, 1_500) is None
    assert h.filter(60, 1, 2_100) == 60
    assert h.reanchor_kind == "stop"
    assert h.last_s == 60


# --- vote ---

def test_lock_vote_matches_the_agreement_window():
    cfg = lock_agreement_config()
    assert cfg == AgreementConfig()
    assert cfg.need == 3 and cfg.min_span_ns == 400_000_000
    assert cfg.need_suspicious == 9 and cfg.min_span_suspicious_ns == 2_000_000_000


def test_one_outlier_does_not_move_the_lock_or_the_final():
    g = _locked(7, 14)
    out = g.filter(_fr(7, 7), 5_000, None)
    assert (out.left_score, out.right_score) == (None, None)
    assert g.locked == (7, 14)
    assert g.allows_final_fallback() is True
    published = _final_from(g, {"home": 14, "away": 7, "quarter": 4, "t_ms": 5_000})
    assert (published["away_score"], published["home_score"]) == (7, 14)
    assert published["final_confirmed"] is False


def test_a_persistent_disagreement_abstains_and_a_gap_does_not_clear_it():
    g = _locked(7, 14)
    # +4, +5 and +9 are illegal. +2 would be a safety and would clear the vote.
    assert g.filter(_fr(11, 14), 1_000, None).left_score is None
    assert g.allows_final_fallback() is True
    assert g.filter(_fr(12, 14), 1_200, None).left_score is None
    assert g.allows_final_fallback() is True
    assert g.filter(_fr(16, 14), 1_400, None).left_score is None
    assert g.locked == (7, 14) and g.allows_final_fallback() is False
    # Three seconds of silence, then one more illegal frame. The vote count restarts.
    # The disagreement that already persisted stays. The pairs differ, so this is not a re-anchor.
    assert g.filter(_blank(), 2_000, None).left_score is None
    assert g.filter(_fr(11, 14), 4_000, None).left_score is None
    assert g.locked == (7, 14)
    assert g.allows_final_fallback() is False
    published = _final_from(g, {"home": 14, "away": 7, "quarter": 4, "t_ms": 4_000})
    assert (published["home_score"], published["away_score"], published["winner"]) == (None, None, "unknown")
    assert published["final_confirmed"] is False


def test_a_legal_read_clears_a_disagreement_and_a_close_burst_is_one_sample():
    g = _locked(7, 14)
    for t in (0, 50, 100, 150):
        g.filter(_fr(7, 7), t, None)
    assert g.allows_final_fallback() is True  # closer than 200 ms: one sample
    back = g.filter(_fr(7, 14), 500, None)
    assert back.pair == (7, 14) and g.locked == (7, 14)
    assert g.allows_final_fallback() is True


def test_forged_frames_do_not_name_7_7():
    """F1, F1b, F2, and F4 from the 9d86d04 report. F1b may keep 7-14. None may name 7-7."""
    fallback = {"home": 14, "away": 7, "quarter": 4, "t_ms": 1}

    def fresh():
        g = ScoreGrammar()
        agree = AgreementWindow(lock_agreement_config())
        end = _seed(g, agree, 7, 14, 4, t0=0)
        return g, agree, end

    # F1b: one forged frame as the last read. The final keeps 7-14.
    g, agree, end = fresh()
    t = end + 1_000
    forged = g.filter(_fr(7, 7), t, None)
    res = agree.offer(forged, clock_ns=t * 1_000_000, crop_hash="crop", session_id="s")
    assert res.agreed is False and g.locked == (7, 14)
    assert g.allows_final_fallback() is True
    kept = _final_from(g, fallback)
    assert (kept["away_score"], kept["home_score"]) == (7, 14)

    # F1: that forged frame, then the true 7-14.
    true = g.filter(_fr(7, 14), t + 500, None)
    assert true.pair == (7, 14) and g.locked == (7, 14) and g.allows_final_fallback() is True

    # F4: 7-7 sustained for 3 s. Re-anchor may fire. The final must not name 7-7.
    g, agree, end = fresh()
    published = None
    for i in range(31):
        t = end + 1_000 + i * 100
        fr = g.filter(_fr(7, 7), t, None)
        res = agree.offer(fr, clock_ns=t * 1_000_000, crop_hash="crop", session_id="s")
        if res.agreed and fr.pair is not None:
            g.note_lock(t, fr.pair[0], fr.pair[1])
            published = fr.pair
    assert published is None
    assert g.allows_final_fallback() is False
    blocked = _final_from(g, fallback)
    assert (blocked["home_score"], blocked["away_score"]) == (None, None)
    assert blocked["winner"] == "unknown"

    # F2: four forged reads over 2.1 s with blanks between. Still not a confirmed 7-7.
    g, agree, end = fresh()
    published = None
    for i, step in enumerate((0, 700, 1_400, 2_100)):
        t = end + 1_000 + step
        g.filter(_blank(), t - 50, None)
        fr = g.filter(_fr(7, 7), t, None)
        res = agree.offer(fr, clock_ns=t * 1_000_000, crop_hash="crop", session_id="s")
        if res.agreed and fr.pair is not None:
            g.note_lock(t, fr.pair[0], fr.pair[1])
            published = fr.pair
    assert published is None and g.allows_final_fallback() is False
    assert _final_from(g, fallback)["winner"] == "unknown"


def test_interleaved_true_frames_do_not_lock_the_forgery():
    g = ScoreGrammar()
    agree = AgreementWindow(lock_agreement_config())
    end = _seed(g, agree, 7, 14, 4, t0=0)
    published = None
    for i in range(8):
        t = end + 1_000 + i * 400
        pair = (7, 7) if i % 2 == 0 else (7, 14)
        fr = g.filter(_fr(*pair), t, None)
        res = agree.offer(fr, clock_ns=t * 1_000_000, crop_hash="crop", session_id="s")
        if res.agreed and fr.pair is not None:
            g.note_lock(t, fr.pair[0], fr.pair[1])
            published = fr.pair
    assert g.locked == (7, 14)
    assert published in (None, (7, 14))
    assert g.allows_final_fallback() is True


# --- inferred quarter ---

def test_inferred_quarter_is_labelled_and_never_used_for_a_final_or_a_lock():
    h = ClockHistory()
    assert h.filter(100, 2, 0) == 100
    assert h.filter(99, None, 1_000) == 99
    assert h.continuation is True and h.accepted_now is True and h.last_q == 2
    fr = FrameRead(
        ok=False, reason="quarter_unsure", profile_id="madden27_standard",
        left_score=3, right_score=0, quarter=None, clock_seconds=99, score_conf=0.9,
    )
    out = apply_inferred_quarter(fr, h)
    assert out.quarter is None
    assert out.detail["quarter_inferred"] is True and out.detail["inferred_quarter"] == 2
    assert posted_quarter(out) is None
    agree = AgreementWindow(lock_agreement_config())
    res = agree.offer(out, clock_ns=1_000_000_000, crop_hash="crop", session_id="s")
    assert res.agreed is False

    # A higher clock with no quarter glyph is a reset we can see. Do not carry Q2.
    assert h.filter(400, None, 2_000) is None
    assert h.rule_note == "clock_increase" and h.accepted_now is False
    jumped = apply_inferred_quarter(FrameRead(
        ok=False, reason="quarter_unsure", profile_id="madden27_standard",
        left_score=3, right_score=0, quarter=None, clock_seconds=None,
    ), h)
    assert jumped.detail.get("quarter_inferred") is not True
    assert jumped.quarter is None

    # Even if a caller copies the carried value onto the quarter field, the post strips it.
    hostile = FrameRead(
        ok=True, reason="ok", profile_id="madden27_standard",
        left_score=3, right_score=0, quarter=2, clock_seconds=99,
        detail={"quarter_inferred": True, "inferred_quarter": 2},
    )
    assert posted_quarter(hostile) is None
    def detect(timeline):
        raise AssertionError("inferred quarter reached detect_final")

    core = ReaderCore(lambda body: None, None, None, None, detect)
    core._post_agreed(1_000, hostile, False)
    assert core.timeline[-1]["quarter"] is None
    assert core._max_quarter is None and core.final is None
    stripped = core._strip_inferred_quarter(hostile)
    assert stripped.quarter is None
    notes = []
    core.reanchor_note = notes.append
    core._note_reanchor(1_000, out)
    assert notes == [{
        "t_ms": 1000, "away": 3, "home": 0,
        "quarter_inferred": True, "inferred_quarter": 2,
    }]
    assert "quarter" not in notes[0]

    receipt = (HERE.parent / "contracts" / "receipt.v0.json").read_text(encoding="utf-8")
    timeline = (HERE.parent / "contracts" / "score_timeline.v0.json").read_text(encoding="utf-8")
    assert "quarter_inferred" not in receipt and "inferred_quarter" not in receipt
    assert "quarter_inferred" not in timeline and "clock_rule" not in timeline


def test_only_lowers_and_a_running_clock_obeys_the_countdown():
    rng = random.Random(0)
    h = ClockHistory()
    for _ in range(20_000):
        secs = rng.randint(-1, 20 * 60)
        quarter = rng.choice([None, 1, 2, 3, 4, 5])
        t_ms = None if rng.randrange(12) == 0 else rng.randint(0, 80_000)
        prev_s, prev_q, prev_t = h.last_s, h.last_q, h.last_t
        out = h.filter(secs, quarter, t_ms)
        if secs is None or t_ms is None:
            assert out == secs
            continue
        assert out is None or out == secs
        if h.reanchor_kind == "run":
            assert h.last_run and countdown_run_ok(h.last_run, h.run_tol_s)
            assert h.last_run[-1][1] == out
            assert h.last_run[0][1] > out
        if out is None or prev_s is None or prev_t is None or t_ms < prev_t:
            continue
        q = quarter if isinstance(quarter, int) and not isinstance(quarter, bool) else None
        if q is not None and prev_q is not None and q != prev_q:
            continue
        assert out <= prev_s

    g = ScoreGrammar()
    g.note_lock(0, 0, 0)
    for i in range(20_000):
        away, home = rng.randint(0, 48), rng.randint(0, 48)
        conf = rng.choice([0.4, 0.72, 0.95])
        fr = _fr(away, home, conf=conf, quarter=rng.choice([1, 2, 3, 4]), clock=rng.randint(0, 900))
        out = g.filter(fr, 10_000 + i * 50, rng.choice([None, None, "fg", "td"]))
        assert out.left_score in (None, away) and out.right_score in (None, home)
        assert out.quarter == fr.quarter and out.clock_seconds == fr.clock_seconds
        assert out.ok is False or fr.ok is True
        if fr.score_conf is not None and out.score_conf is not None:
            assert out.score_conf <= fr.score_conf
        assert not (out.score_conf is not None and fr.score_conf is None)
        assert out.score_margin is None or fr.score_margin is None or out.score_margin <= fr.score_margin


def _assert_increase_blanks(history):
    history.filter(60, 1, 0)
    assert history.filter(61, 1, 1_000) is None
    assert history.last_s == 60


def _assert_drift_rejected(history):
    history.filter(90, 1, 0)
    assert _feed_rate(history, 60, 100, 8_000, 100, 500) == []
    assert history.last_s == 90


def _assert_run_needs_the_full_window(history):
    history.filter(90, 1, 0)
    early = _feed_rate(history, 60, 100, 1_000, 100, 1_000)
    assert early == []


def _assert_one_frame_does_not_abstain(grammar):
    grammar.filter(_fr(7, 7), 5_000, None)
    assert grammar.locked == (7, 14)
    assert grammar.allows_final_fallback() is True


def _assert_inferred_is_not_posted(fr):
    assert fr.detail.get("quarter_inferred") is True
    assert posted_quarter(fr) is None


def test_each_new_rule_fails_when_the_rule_is_mutated():
    """The strict predicates go red if the rule is replaced by a pass. CI stays green."""

    class AcceptsIncrease(ClockHistory):
        def filter(self, secs, quarter, t_ms):
            if secs is None or t_ms is None:
                return secs
            self.last_s = int(secs)
            return int(secs)

    with pytest.raises(AssertionError):
        _assert_increase_blanks(AcceptsIncrease())

    with pytest.raises(AssertionError):
        _assert_drift_rejected(ClockHistory(run_tol_s=10))

    with pytest.raises(AssertionError):
        _assert_run_needs_the_full_window(ClockHistory(run_reads=1, run_span_ms=0, run_min_distinct=1))

    class OneFrameVeto(ScoreGrammar):
        def filter(self, fr, t_ms, banner=None):
            out = super().filter(fr, t_ms, banner)
            if getattr(out, "reason", None) == "score_transition":
                self.disagreeing = True
            return out

    _assert_increase_blanks(ClockHistory())
    _assert_drift_rejected(ClockHistory())
    _assert_run_needs_the_full_window(ClockHistory())
    veto = OneFrameVeto()
    veto.note_lock(0, 7, 14)
    with pytest.raises(AssertionError):
        _assert_one_frame_does_not_abstain(veto)
    _assert_one_frame_does_not_abstain(_locked(7, 14))

    carried = FrameRead(
        ok=False, reason="quarter_unsure", profile_id="madden27_standard",
        quarter=None, detail={"quarter_inferred": True, "inferred_quarter": 2},
    )
    _assert_inferred_is_not_posted(carried)

    def posts_the_carried_value(fr):
        return fr.detail.get("inferred_quarter")

    with pytest.raises(AssertionError):
        assert posts_the_carried_value(carried) is None


def test_clock_rule_note_stays_off_the_timeline_schema():
    from qorcap_reader import ReaderCore
    notes = []
    core = ReaderCore(lambda b: None, None, None, None, None)
    core.reanchor_note = notes.append
    fr = FrameRead(
        ok=False, reason="ok", profile_id="madden27_standard",
        left_score=0, right_score=0, quarter=1, clock_seconds=None,
        detail={"clock_rule": "clock_increase"},
    )
    core._note_reanchor(800, fr)
    assert notes == [{"t_ms": 800, "away": 0, "home": 0, "quarter": 1, "clock_rule": "clock_increase"}]
    schema = json.loads((HERE.parent / "contracts" / "score_timeline.v0.json").read_text(encoding="utf-8"))
    props = schema["properties"]["signals"]["properties"]["score_timeline"]["properties"]["events"]["items"]["properties"]
    assert "clock_rule" not in props and "quarter_inferred" not in props
