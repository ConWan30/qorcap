"""Score-transition grammar. No clips and no ffmpeg: both CI jobs run this.

Illegal jumps abstain and are not snapped to a nearby legal score. Legal football steps pass.
The grammar never raises a confidence.
"""
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, os.environ.get("QORTIME", "/workspace/qortime-src"))

from qor_receipt._vendor.qoresence.vision.local_scorebug.reader import FrameRead  # noqa: E402
from qorcap_clock import ClockHistory  # noqa: E402
from qorcap_score_grammar import (  # noqa: E402
    BANNER_WINDOW_MS, REANCHOR_READS, REANCHOR_SPAN_MS, TRY_WINDOW_MS, VOTE_READS, VOTE_SPAN_MS,
    ScoreGrammar, judge,
)


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
    assert g.reanchor_reads == REANCHOR_READS == 4
    assert g.reanchor_span_ms == REANCHOR_SPAN_MS == 2_000
    assert g.vote_reads == VOTE_READS == 3
    assert g.vote_span_ms == VOTE_SPAN_MS == 400
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


def _feed(g, away, home, t0, n, step):
    last = None
    for i in range(n):
        last = g.filter(_fr(away, home, conf=0.81), t0 + i * step, None)
    return last


def test_a_stable_illegal_score_reanchors_without_raising_confidence():
    g = ScoreGrammar(reanchor_reads=4, reanchor_span_ms=1_000)
    g.note_lock(0, 3, 7)  # wrong anchor; truth is 3-0
    early = _feed(g, 3, 0, 0, 3, 200)
    assert early.left_score is None and g.locked == (3, 7)
    hit = g.filter(_fr(3, 0, conf=0.81, margin=0.11), 1_000, None)
    assert hit.pair == (3, 0) and hit.score_conf == 0.81 and hit.score_margin == 0.11
    assert hit.detail.get("grammar_reanchor") is True and hit.ok
    assert g.locked == (3, 0) and g.allows_final_fallback() is False
    # A wrong +3 off the old anchor must not survive the re-anchor.
    assert g.filter(_fr(3, 10), 1_200, None).right_score is None
    g.note_lock(1_000, 3, 0)
    assert g.allows_final_fallback() is True


def test_an_unconfirmed_reanchor_or_a_persistent_disagreement_blocks_the_final():
    from qorcap_match import apply_final
    stuck = _locked(0, 6)
    assert stuck.filter(_fr(0, 7), 1_000, None).left_score is None
    assert stuck.locked == (0, 6)
    # One illegal frame blanks that frame. It does not abstain the final (FU2).
    assert stuck.allows_final_fallback() is True
    assert stuck.filter(_fr(0, 7), 1_200, None).left_score is None
    assert stuck.allows_final_fallback() is True
    assert stuck.filter(_fr(0, 7), 1_400, None).left_score is None
    assert stuck.allows_final_fallback() is False
    base = {"home_score": 6, "away_score": 0, "quarter": 1, "final_t_ms": 5, "winner": "home",
            "method": "last_scorebug_read_unconfirmed"}
    frozen = {"home": 6, "away": 0, "quarter": 1, "t_ms": 5}
    out = apply_final(base, None, fallback=frozen, grammar_unresolved=True)
    assert (out["home_score"], out["away_score"], out["winner"]) == (None, None, "unknown")
    assert out["final_confirmed"] is False and "final_basis" not in out
    menu = {"basis": "end_menu", "home": 14, "away": 7, "quarter": 4, "t_ms": 9, "final_confirmed": True}
    kept = apply_final(base, menu, fallback=frozen, grammar_unresolved=True)
    assert (kept["away_score"], kept["home_score"]) == (7, 14)


def test_a_cut_keeps_a_try_that_is_then_read_on_the_same_score():
    g = _locked(0, 0)
    g.note_banner(1_000, "td")
    g.note_lock(1_000, 0, 6)
    assert g.phase == "try" and g.td_side == "home"
    # Highlight cut: time goes backwards. The try is not discarded if 0-6 is locked again.
    again = g.filter(_fr(0, 6), 100, None)
    assert again.pair == (0, 6) and g.locked is None
    g.note_lock(100, 0, 6)
    assert g.phase == "try" and g.td_side == "home"
    assert g.filter(_fr(0, 7), 400, None).pair == (0, 7)


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


def test_harvest_refuses_held_out_footage(tmp_path):
    sys.path.insert(0, str(HERE.parent / "tools"))
    from harvest_standard_clock_banner import held_out_reason, main
    assert held_out_reason("match-20261023-170123/f175.png") == "170123"
    assert held_out_reason("hl00-q3-a3-h0/f175.png") == "hl00-q3-a3-h0"
    assert held_out_reason("hl04-q2-a7-h13/f090.png") == "hl04-q2-a7-h13"
    assert held_out_reason("match-20261018-084523/b0010.png") == "084523"
    assert held_out_reason("match-20261017-110028/f100.png") is None
    banned = tmp_path / "170123"
    banned.mkdir()
    assert main([str(banned)]) == 2
    import hashlib
    import harvest_standard_clock_banner as harvest
    payload = b"renamed-held-out-clip"
    digest = hashlib.sha256(payload).hexdigest()
    commit, table = harvest.held_out_table()
    assert commit == "585ccc4f2af3d67880d40e75553864df01b6e7b7"
    assert harvest.clip_sha256("170123", "hl00-q3-a3-h0") == (
        "46e732640b65a1cdbf8e4f593713cfecea9abb1e6910dad55f070045d19ec70b")
    cloned = dict(table)
    cloned[digest] = "site/matches/170123/clips/hl00-q3-a3-h0.mp4"
    harvest._SHA256 = cloned
    try:
        folder = tmp_path / "clean-name"
        folder.mkdir()
        (folder / "frame.png").write_bytes(payload)
        assert main([str(folder)]) == 2
    finally:
        harvest._SHA256 = table


def test_reanchor_notes_stay_off_the_timeline_schema_and_the_checkpoint(tmp_path):
    from qorcap_match import apply_stopped_match_final, score_evidence, tail_final_check
    from qorcap_reader import ReaderCore
    from qorcap_reanchor_notes import FILENAME, append_note, record_unresolved_final

    schema = json.loads((HERE.parent / "contracts" / "score_timeline.v0.json").read_text(encoding="utf-8"))
    props = schema["properties"]["signals"]["properties"]["score_timeline"]["properties"]["events"]["items"]["properties"]
    assert "grammar_reanchor" not in props and "clock_reanchor" not in props
    rows = []
    core = ReaderCore(lambda b: None, None, None, None, None)
    core.reanchor_note = rows.append
    fr = type("F", (), {})()
    fr.detail = {"grammar_reanchor": True, "clock_reanchor": True}
    fr.left_score, fr.right_score, fr.quarter = 6, 7, 2
    core._note_reanchor(1500, fr)
    assert rows == [{
        "t_ms": 1500, "away": 6, "home": 7, "quarter": 2,
        "grammar_reanchor": True, "clock_reanchor": True,
    }]
    append_note(tmp_path, rows[0])
    base = {"home_score": 6, "away_score": 0, "quarter": 1, "final_t_ms": 5, "winner": "home",
            "method": "last_scorebug_read_unconfirmed"}
    stopped = apply_stopped_match_final(
        {"result": dict(base)},
        {"basis": "clock_zero", "home": 6, "away": 0, "quarter": 1, "t_ms": 5},
        grammar_unresolved=True,
    )
    assert stopped["result"]["winner"] == "unknown" and stopped["result"]["home_score"] is None
    kept = apply_stopped_match_final(
        {"result": dict(base)},
        {"basis": "end_menu", "method": "end_menu_read", "home": 14, "away": 7, "quarter": 4,
         "t_ms": 9, "final_confirmed": True},
        grammar_unresolved=True,
    )
    assert kept["result"]["method"] == "end_menu_read" and kept["result"]["home_score"] == 14
    record_unresolved_final(tmp_path, stopped["result"])
    record_unresolved_final(tmp_path, kept["result"])
    lines = [json.loads(line) for line in (tmp_path / FILENAME).read_text(encoding="utf-8").splitlines()]
    assert lines[-1] == {"kind": "final", "grammar_unresolved": True}
    assert sum(1 for row in lines if row.get("kind") == "final") == 1
    event = {"type": "score", "t_ms": 1500, "home": 7, "away": 6, "quarter": 2, "clock_s": 10,
             "clock_red": False, "video_ms": 1500, "grammar_reanchor": True, "clock_reanchor": True}
    evidence = score_evidence(event)
    assert set(evidence) == {"home", "away", "quarter", "clock_s", "clock_red"}
    assert "grammar_reanchor" not in evidence and "clock_reanchor" not in evidence

    class Tail:
        def __init__(self, post):
            self.post = post
            self.stats = {"frames": 0}
            self.final = None
            self.end_menu = None
            self.end_menu_rejected = None

        def on_frame(self, t, img):
            self.stats["frames"] += 1
            self.final = {"basis": "clock_zero", "home": 6, "away": 0, "quarter": 4, "t_ms": t}

    (tmp_path / "timeline.jsonl").write_text(
        json.dumps({"t_ms": 1000, "home": 6, "away": 0, "quarter": 4}) + "\n", encoding="utf-8")
    (tmp_path / "manifest.jsonl").write_text(
        json.dumps({"type": "segment", "seq": 1, "file": "video.mp4", "wall_ms": 60_000}) + "\n", encoding="utf-8")
    (tmp_path / "video.mp4").write_bytes(b"x")
    draft = {"result": dict(base, quarter=4, final_t_ms=60_000)}
    out = tail_final_check(tmp_path, draft, ffmpeg="missing", core_factory=Tail, frames=[(5_000, None)],
                           grammar_unresolved=True)
    assert out["result"]["winner"] == "unknown" and out["result"]["home_score"] is None

    class Menu(Tail):
        def on_frame(self, t, img):
            self.final = {"basis": "end_menu", "method": "end_menu_read", "home": 14, "away": 7,
                          "quarter": 4, "t_ms": t, "final_confirmed": True}

    kept_tail = tail_final_check(tmp_path, draft, ffmpeg="missing", core_factory=Menu, frames=[(5_000, None)],
                                 grammar_unresolved=True)
    assert kept_tail["result"]["method"] == "end_menu_read"
    assert (kept_tail["result"]["away_score"], kept_tail["result"]["home_score"]) == (7, 14)


def test_clock_does_not_lock_a_low_misread_or_a_quarterless_start_or_a_quarter_regression():
    h = ClockHistory()
    assert h.filter(67, 4, 0) == 67  # 1:07
    assert h.filter(7, 4, 200) is None  # one 0:07 does not move the baseline
    assert h.last_s == 67
    t = 1_000
    for secs in range(66, 7, -1):  # the next 59 true reads, 1:06 down to 0:08
        assert h.filter(secs, 4, t) == secs
        t += 1_000
    bare = ClockHistory()
    assert bare.filter(61, None, 0) == 61 and bare.last_s is None
    assert bare.filter(900, 2, 1_000) == 900  # the quarter is not blanked
    back = ClockHistory()
    assert back.filter(47, 4, 0) == 47
    assert back.filter(59, 3, 2_000) == 59  # Q4 then Q3 is a cut
    assert back.last_q == 3
