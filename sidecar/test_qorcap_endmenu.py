"""End-of-game menu policy.

Digit packs that included match 125430 are removed. Policy tests use ScriptedEndMenu.
The 125430 FINISH GAME frame is held-out evaluation: the score read skips until
end-menu templates load. Harvesting from 125430 stays refused.
"""

import json
import os
import sys
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, os.environ.get("QORTIME", "/workspace/qortime-src"))
from qorcap_endmenu import EndMenuAgreement, EndMenuRead, EndMenuReader  # noqa: E402
from qorcap_reader import ReaderCore, end_menu_consistency, madden_final_screen, prep  # noqa: E402
from qor_receipt.final_detect import detect_final  # noqa: E402

RST = cv2.imread(str(HERE / "fixtures" / "m27_end_menu_restart.jpg"))
R = EndMenuReader()


class ScriptedEndMenu:
    """A stand-in read. The glyph pack is gone, and the policy tests still need a stable score."""

    def __init__(self, away, home, conf=0.91, margin=0.42, period=None):
        self.away, self.home = away, home
        self.conf, self.margin, self._period = conf, margin, period

    def read(self, _img):
        return EndMenuRead(True, self.away, self.home, "ok", self.conf, self.margin,
                           [{"source": "scripted", "xcheck": "skipped"}])

    def period(self, _img):
        return self._period


def test_held_out_125430_finish_menu_reads_7_14():
    """Grade the 125430 finish menu once end-menu glyphs are allowlisted."""
    import pytest
    from qorcap_template_guard import has_role

    if not has_role("end_menu"):
        pytest.skip("templates_missing: held-out 125430 end-menu evaluation waits for allowlisted end-menu templates")
    fin = cv2.imread(str(HERE / "fixtures" / "m27_end_menu_finish.jpg"))
    assert fin is not None
    reader = EndMenuReader()
    away_left = reader.read(RST)
    finish = reader.read(fin)
    assert away_left.ok and (away_left.away, away_left.home) == (0, 20), away_left
    assert finish.ok and (finish.away, finish.home) == (7, 14), finish


def test_reads_both_matches_away_left_home_right():
    """125430 supplied the 7 and the 4. That pack is gone, so the reader abstains and does not guess."""
    from qorcap_template_guard import TEMPLATES_MISSING
    blank = np.zeros((360, 640, 3), np.uint8)
    for frame in (blank, RST, cv2.resize(RST, (1280, 720))):
        rd = R.read(frame)
        assert rd.ok is False and rd.reason == TEMPLATES_MISSING, rd
        assert rd.away is None and rd.home is None
    assert R.xpack is None and R._missing
    assert not (HERE / "packs" / "madden27_end_menu_v1.npz").is_file()
    assert not (HERE / "packs" / "madden27_end_menu_v2.npz").is_file()


def test_unreadable_fails_closed():
    assert not R.read(np.zeros((360, 640, 3), np.uint8)).ok
    smudge = RST.copy()
    cv2.rectangle(smudge, (395, 40), (412, 50), (255, 255, 255), -1)   # damage the "2"
    assert not R.read(smudge).ok
    assert not R.read(None).ok


def test_agreement_needs_consecutive_frames():
    ag = EndMenuAgreement(n=3, min_span_ms=400)
    ok = lambda a, h: EndMenuRead(True, a, h, "ok", 0.9, 0.4, [{"source": "real"}])  # noqa: E731
    assert ag.offer(0, ok(0, 20)) is None
    assert ag.offer(250, ok(0, 20)) is None
    assert ag.offer(500, EndMenuRead(False, reason="x")) is None     # unreadable restarts the streak
    assert ag.offer(750, ok(0, 20)) is None and ag.offer(1000, ok(0, 20)) is None
    st = ag.offer(1250, ok(0, 20))
    assert st and (st["away"], st["home"], st["frames"]) == (0, 20, 3)


def test_consistency_rule():
    assert end_menu_consistency({"away": 0, "home": 20}, {"away": 0, "home": 17}) is None
    assert end_menu_consistency({"away": 7, "home": 14}, {"away": 7, "home": 14}) is None
    assert end_menu_consistency({"away": 0, "home": 14}, {"away": 0, "home": 17})      # backwards
    assert end_menu_consistency({"away": 3, "home": 20}, {"away": 0, "home": 17})      # both sides
    assert end_menu_consistency({"away": 0, "home": 27}, {"away": 0, "home": 17})      # >1 play


def test_delta_corroboration_gate():
    from qorcap_reader import end_menu_delta_corroborated
    st, last = {"away": 0, "home": 20, "t_ms": 50_000}, {"away": 0, "home": 17, "t_ms": 40_000}
    assert end_menu_delta_corroborated(st, last, [], []) is None  # one-legal-step, no evidence
    assert end_menu_delta_corroborated({"away": 7, "home": 14, "t_ms": 1}, {"away": 7, "home": 14, "t_ms": 0}, [], [])["kind"] == "exact_match"
    # timeline scorebug already shows end-menu score
    tl = [{"t_ms": 45_000, "home": 20, "away": 0, "quarter": 4, "clock_s": 0}]
    assert end_menu_delta_corroborated(st, last, tl, [])["kind"] == "scorebug_final_play"
    # external haptic near window
    assert end_menu_delta_corroborated(st, last, [], [{"kind": "haptic", "t_ms": 41_000}])["kind"] == "haptic"
    assert end_menu_delta_corroborated(st, last, [], [{"kind": "score_change_clip", "t_ms": 41_000}]) is None  # no sha256
    assert end_menu_delta_corroborated(st, last, [], [{"kind": "score_change_clip", "t_ms": 41_000, "sha256": "a"*64}])["kind"] == "score_change_clip"


class Paused:
    paused = True

    def update(self, *_):
        pass


def _core(last, reader=None):
    posts = []
    core = ReaderCore(posts.append, None, None, Paused(), detect_final, final_screen_detector=madden_final_screen,
                      end_menu_reader=R if reader is None else reader)
    core.timeline = [dict(last)]
    return core, posts


def test_core_end_menu_final_overrides_scorebug_and_notes():
    """Match 2 (0-20 end menu vs 0-17 scorebug): accepted as end_menu_read but NOT confirmed without corroboration."""
    core, posts = _core({"t_ms": 0, "home": 17, "away": 0, "quarter": 4, "clock_s": 0, "clock_red": True},
                        ScriptedEndMenu(0, 20))
    for k in range(6):
        core.on_frame(10_000 + k * 500, RST)
    fins = [p for p in posts if p["type"] == "final"]
    em = [f for f in fins if f["basis"] == "end_menu"]
    assert len(em) == 1
    f = em[0]
    assert (f["away"], f["home"], f["method"]) == (0, 20, "end_menu_read")
    assert f["scorebug_last"]["home"] == 17 and "final play score not shown on scorebug" in f["notes"][0]
    assert f["final_confirmed"] is False
    assert "NOT corroborated" in " ".join(f["notes"])
    assert f["end_menu"]["frames"] >= 3


def test_core_end_menu_match2_confirms_with_haptic_corroboration():
    core, posts = _core({"t_ms": 0, "home": 17, "away": 0, "quarter": 4, "clock_s": 0, "clock_red": True},
                        ScriptedEndMenu(0, 20))
    core.delta_corroboration = [{"kind": "haptic", "t_ms": 500}]
    for k in range(6):
        core.on_frame(10_000 + k * 500, RST)
    f = [p for p in posts if p.get("basis") == "end_menu"][0]
    assert f["final_confirmed"] is True and f["delta_corroboration"]["kind"] == "haptic"


def test_core_end_menu_match1_no_note():
    """Exact 7-14 confirms. The 125430 finish frame is held out and is not the image under test."""
    core, posts = _core({"t_ms": 0, "home": 14, "away": 7, "quarter": 4, "clock_s": 0, "clock_red": True},
                        ScriptedEndMenu(7, 14))
    for k in range(6):
        core.on_frame(10_000 + k * 500, RST)
    f = [p for p in posts if p["type"] == "final" and p["basis"] == "end_menu"][0]
    assert (f["away"], f["home"]) == (7, 14) and f["notes"] == []
    assert f["final_confirmed"] is True and f["delta_corroboration"]["kind"] == "exact_match"


def test_core_rejects_inconsistent_or_pre_q4():
    menu = ScriptedEndMenu(0, 20)
    core, posts = _core({"t_ms": 0, "home": 3, "away": 0, "quarter": 4, "clock_s": 0}, menu)
    for k in range(6):
        core.on_frame(10_000 + k * 500, RST)        # 0-3 -> 0-20 is not one play: refuse, stay unconfirmed
    assert not any(p.get("basis") == "end_menu" for p in posts) and core.end_menu_rejected
    core, posts = _core({"t_ms": 0, "home": 17, "away": 0, "quarter": 2, "clock_s": 0}, menu)
    for k in range(6):
        core.on_frame(10_000 + k * 500, RST)
    assert not any(p.get("basis") == "end_menu" for p in posts)


def test_apply_final_fields():
    import qorcap_match as qm
    base = {"home_score": 17, "away_score": 0, "quarter": 4, "final_t_ms": 1, "winner": "home", "method": "agreed_scorebug_final"}
    em = {"basis": "end_menu", "method": "end_menu_read", "home": 20, "away": 0, "quarter": 4, "t_ms": 5,
          "scorebug_last": {"home": 17, "away": 0, "quarter": 4, "t_ms": 2},
          "notes": ["final play score not shown on scorebug"], "final_confirmed": False}
    r = qm.apply_final(base, em)
    assert r["final_confirmed"] is False and r["method"] == "end_menu_read" and r["home_score"] == 20
    em2 = dict(em, final_confirmed=True, delta_corroboration={"kind": "haptic", "t_ms": 2})
    r = qm.apply_final(base, em2)
    assert r["final_confirmed"] is True and r["delta_corroboration"]["kind"] == "haptic"
    r = qm.apply_final(base, {"basis": "clock_zero", "home": 17, "away": 0, "quarter": 4, "t_ms": 3})
    assert r["final_confirmed"] is False and r["final_basis"] == "clock_zero"
    r = qm.apply_final(base, None, fallback={"home": 17, "away": 0, "quarter": 4, "t_ms": 3})
    assert r["final_confirmed"] is False and r["method"] == "last_scorebug_read_unconfirmed" and "final_basis" not in r


def test_tnp_ot_end_menu_reads_walkoff_and_sets_quarter():
    from qorcap_template_guard import TEMPLATES_MISSING
    ot = cv2.imread(str(HERE / "fixtures" / "m27_tnp_091651_end_menu.jpg"))
    assert madden_final_screen(prep(ot))
    rd = R.read(ot)
    assert not rd.ok and rd.reason == TEMPLATES_MISSING and rd.away is None and rd.home is None
    assert R.period(ot) is None  # ot_vec lived in the removed pack
    menu = ScriptedEndMenu(10, 16, period="OT")
    # Walk-off: last scorebug is OT 1:55, same score. Clock is not 0:00; the menu still confirms.
    core, posts = _core({"t_ms": 0, "home": 16, "away": 10, "quarter": 5, "clock_s": 115, "clock_red": False}, menu)
    for k in range(6):
        core.on_frame(10_000 + k * 500, ot)
    f = [p for p in posts if p.get("basis") == "end_menu"][0]
    assert (f["away"], f["home"], f["quarter"], f["final_confirmed"]) == (10, 16, 5, True)
    # Regulation ended 10-10; the menu is one touchdown later and says OT, so the final quarter is 5.
    core, posts = _core({"t_ms": 0, "home": 10, "away": 10, "quarter": 4, "clock_s": 0}, menu)
    for k in range(6):
        core.on_frame(10_000 + k * 500, ot)
    f = [p for p in posts if p.get("basis") == "end_menu"][0]
    assert f["quarter"] == 5 and f["final_confirmed"] is False
    # A Q4 menu with the clock still running does not end the match, OT paint or not.
    core, posts = _core({"t_ms": 0, "home": 16, "away": 10, "quarter": 4, "clock_s": 30}, menu)
    for k in range(6):
        core.on_frame(10_000 + k * 500, ot)
    assert not any(p.get("basis") == "end_menu" for p in posts)
    assert "clock not 0:00" in core.end_menu_rejected["why"]


def test_core_rejects_end_menu_before_clock_zero():
    """A menu that matches the current score must not end the match while the clock is still running."""
    core, posts = _core({"t_ms": 0, "home": 20, "away": 0, "quarter": 4, "clock_s": 30, "clock_red": False},
                        ScriptedEndMenu(0, 20))
    for k in range(6):
        core.on_frame(10_000 + k * 500, RST)  # real end-menu fixture reading 0-20
    assert not any(p.get("basis") == "end_menu" for p in posts)
    assert core.end_menu_rejected and "clock not 0:00" in core.end_menu_rejected["why"]


def test_collect_delta_corroboration_from_clips_haptics_inputs():
    from qorcap_reader import collect_delta_corroboration
    clips = [
        {"name": "score-123", "sha256": "a" * 64, "wall_ms": 40_000},
        {"name": "final", "sha256": "b" * 64, "wall_ms": 50_000},  # ignored (not score*)
        {"name": "score_change", "sha256": "c" * 64, "t_ms": 41_000},
    ]
    haptics = [
        {"type": "haptic", "t_wall_ms": 41_500, "confidence": True},
        {"type": "status", "t_wall_ms": 1},  # ignored
    ]
    inputs = [
        {"type": "in", "t_us": 42_000_000, "b": 16},
        {"type": "in", "t_us": 43_000_000, "b": 0},  # no buttons
        {"type": "chunk", "t_us": 1},
    ]
    out = collect_delta_corroboration(clips=clips, haptic_records=haptics, input_records=inputs)
    kinds = [e["kind"] for e in out]
    assert kinds.count("score_change_clip") == 2
    assert "haptic" in kinds and "input_activity" in kinds
    assert all(e.get("sha256") for e in out if e["kind"] == "score_change_clip")


def test_load_delta_corroboration_from_bundle(tmp_path):
    from qorcap_reader import load_delta_corroboration_from_bundle, end_menu_delta_corroborated
    b = tmp_path / "bundle"
    b.mkdir()
    (b / "haptics.jsonl").write_text(
        json.dumps({"type": "haptic", "t_wall_ms": 3500, "t_mono_ms": 3500}) + "\n", encoding="utf-8")
    (b / "inputs.jsonl").write_text(
        json.dumps({"type": "in", "t_us": 3600_000, "b": 32}) + "\n", encoding="utf-8")
    (b / "manifest.jsonl").write_text(
        json.dumps({"type": "clip", "name": "score-1", "sha256": "d" * 64, "wall_ms": 3400, "file": "clips/score-1.mp4"}) + "\n",
        encoding="utf-8")
    corr = load_delta_corroboration_from_bundle(b)
    kinds = {e["kind"] for e in corr}
    assert kinds >= {"haptic", "input_activity", "score_change_clip"}
    # Match-2 style: 0-17 -> 0-20 with haptic confirms
    st = {"away": 0, "home": 20, "t_ms": 5000}
    last = {"away": 0, "home": 17, "t_ms": 3000}
    got = end_menu_delta_corroborated(st, last, [], corr)
    assert got and got["kind"] in ("haptic", "score_change_clip", "input_activity")


def test_tail_loads_bundle_haptics_when_core_has_no_corr(tmp_path):
    """Stop-time fallback: tail_final_check auto-loads haptics.jsonl into ReaderCore.delta_corroboration."""
    import qorcap_match as qm
    b = tmp_path / "match-live"
    b.mkdir()
    reads = [{"t_ms": 1000 + i * 1000, "home": 17, "away": 0, "quarter": 4, "clock_s": c, "clock_red": True}
             for i, c in enumerate([3, 2, 0])]
    (b / "timeline.jsonl").write_text("".join(json.dumps(r) + "\n" for r in reads))
    (b / "manifest.jsonl").write_text(json.dumps({"type": "segment", "seq": 1, "file": "video.mp4", "wall_ms": 60_000}) + "\n")
    (b / "video.mp4").write_bytes(b"x")
    # Haptic near last scorebug (t_ms=3000) — same window as match-2 0-17→0-20
    (b / "haptics.jsonl").write_text(json.dumps({"type": "haptic", "t_wall_ms": 3000}) + "\n")
    frames = [(k * 500, RST) for k in range(20)]
    draft = {"result": {"home_score": 17, "away_score": 0, "quarter": 4, "final_t_ms": 60_000, "winner": "home",
                        "method": "last_scorebug_read_unconfirmed", "final_confirmed": False}}

    class Paused:
        paused = True
        def update(self, *_):
            pass

    def fac(post):
        # No pre-seeded delta_corroboration — must come from the bundle via load_delta_corroboration_from_bundle.
        # The digit pack is gone; the haptic gate is what this test locks.
        return ReaderCore(post, None, None, Paused(), detect_final, final_screen_detector=madden_final_screen,
                          end_menu_reader=ScriptedEndMenu(0, 20))

    out = qm.tail_final_check(b, draft, ffmpeg="ffmpeg-missing", core_factory=fac, frames=frames)
    r = out["result"]
    assert r["method"] == "end_menu_read"
    assert (r["away_score"], r["home_score"]) == (0, 20)
    assert r["final_confirmed"] is True, r
    assert r.get("delta_corroboration", {}).get("kind") == "haptic"


def test_collect_feeds_live_match2_window():
    """Live Start Match wiring: collect_delta_corroboration + end_menu_delta_corroborated for 0-17→0-20."""
    from qorcap_reader import collect_delta_corroboration, end_menu_delta_corroborated
    corr = collect_delta_corroboration(
        clips=[{"name": "score-finalplay", "sha256": "e" * 64, "wall_ms": 2900}],
        haptic_records=[{"type": "haptic", "t_wall_ms": 3100}],
        input_records=[{"type": "in", "t_us": 3_200_000, "b": 8}],
    )
    st, last = {"away": 0, "home": 20, "t_ms": 5000}, {"away": 0, "home": 17, "t_ms": 3000}
    got = end_menu_delta_corroborated(st, last, [], corr)
    assert got and got["kind"] in ("haptic", "score_change_clip", "input_activity")


# ---- v0.3.11: no invented winner/quarter in unconfirmed drafts
def test_apply_final_tie_is_tie_and_keeps_real_quarter():
    import qorcap_match as qm
    base = {"home_score": 0, "away_score": 0, "quarter": 4, "final_t_ms": 1, "winner": "away", "method": "agreed_scorebug_final"}
    r = qm.apply_final(base, None, fallback={"home": 7, "away": 7, "quarter": 1, "t_ms": 3})
    assert (r["home_score"], r["away_score"], r["quarter"], r["winner"]) == (7, 7, 1, "tie")
    assert r["method"] == "last_scorebug_read_unconfirmed" and r["final_confirmed"] is False


def test_apply_final_without_any_read_says_unknown():
    import qorcap_match as qm
    drafted = {"home_score": None, "away_score": None, "quarter": None, "final_t_ms": 1, "winner": "unknown",
               "method": "last_scorebug_read_unconfirmed"}
    r = qm.apply_final(drafted, None)
    assert (r["home_score"], r["away_score"], r["quarter"], r["winner"]) == (None, None, None, "unknown")
    legacy = dict(drafted, home_score=0, away_score=0, quarter=4, winner="away")   # pre-0.3.11 placeholder draft
    assert qm.apply_final(legacy, None)["winner"] == "tie"


def test_apply_final_overtime_tie_final():
    import qorcap_match as qm
    em = {"basis": "end_menu", "method": "end_menu_read", "home": 24, "away": 24, "quarter": 5, "t_ms": 5}
    r = qm.apply_final({"final_t_ms": 1}, em)
    assert (r["winner"], r["quarter"], r["final_confirmed"]) == ("tie", 5, True)


def test_result_winner():
    import qorcap_match as qm
    assert [qm.result_winner(*p) for p in [(3, 1), (1, 3), (2, 2), (None, None), (0, None)]] == \
        ["home", "away", "tie", "unknown", "unknown"]


def test_finalize_aggregates_091651_end_menu_to_confirmed_ot_final(capsys):
    """The 31 handoff end screens, through the same aggregation finalize uses.

    The live timeline ended on a score lock whose quarter glyph had failed (null) after a real OT 10-16 read.
    That null quarter used to drop the menu with no log line and leave result.quarter null.
    """
    import qorcap_match as qm
    from qor_receipt.final_detect import detect_final
    folder = HERE / "fixtures" / "tnp_091651_end_menu"
    paths = sorted(folder.glob("*.jpg"))
    assert len(paths) == 31, len(paths)
    frames = [(i * 250, cv2.imread(str(p))) for i, p in enumerate(paths)]

    def factory(post):
        core = ReaderCore(post, None, None, Paused(), detect_final, final_screen_detector=madden_final_screen,
                          end_menu_reader=ScriptedEndMenu(10, 16, period="OT"))
        core.timeline = [
            {"t_ms": 1_000, "home": 16, "away": 10, "quarter": 5, "clock_s": 115, "clock_red": False},
            {"t_ms": 2_000, "home": 16, "away": 10, "quarter": None, "clock_s": 115, "clock_red": False},
        ]
        return core

    events, stats = qm.run_offline_reader(frames, 0, factory)
    assert stats["end_menu_reads"] == 31 and stats["final_screen"] == 31 and stats["errors"] == 0
    fin = next(e for e in events if qm.is_end_menu_final(e))
    assert (fin["away"], fin["home"], fin["quarter"], fin["final_confirmed"]) == (10, 16, 5, True)
    reads = [
        {"t_ms": 1_000, "home": 16, "away": 10, "quarter": 5, "clock_s": 115},
        {"t_ms": 2_000, "home": 16, "away": 10, "quarter": None, "clock_s": 115},
    ]
    res = qm.apply_final({"home_score": None, "away_score": None, "quarter": None, "final_t_ms": 1,
                          "winner": "unknown", "method": "last_scorebug_read_unconfirmed"}, fin,
                         fallback=qm.last_reliable_read(reads))
    assert res["method"] == "end_menu_read" and res["final_confirmed"] is True
    assert (res["away_score"], res["home_score"], res["quarter"], res["winner"]) == (10, 16, 5, "home")
    # No menu final: the receipt quarter still comes from the last read that had one.
    bare = qm.apply_final({"quarter": None, "final_t_ms": 1}, None, fallback=qm.last_reliable_read(reads))
    assert bare["quarter"] == 5 and bare["method"] == "last_scorebug_read_unconfirmed"
    assert "END MENU rejected" not in capsys.readouterr().err


def test_standard_end_menu_reads_and_confirms_hou_30_jax_6():
    """110028 is not an allowlisted template match, so the glyph reader abstains.

    The exact-match gate and the red-clock gate still run on a scripted 30-6.
    """
    import qorcap_match as qm
    from qorcap_template_guard import TEMPLATES_MISSING
    im = cv2.imread(str(HERE / "fixtures" / "m27_std_110028_end_menu.jpg"))
    assert madden_final_screen(prep(im))
    rd = R.read(im)
    assert not rd.ok and rd.reason == TEMPLATES_MISSING and rd.away is None and rd.home is None
    menu = ScriptedEndMenu(30, 6)
    core, posts = _core({"t_ms": 0, "home": 6, "away": 30, "quarter": 4, "clock_s": 3, "clock_red": True}, menu)
    for k in range(6):
        core.on_frame(10_000 + k * 500, im)
    f = [p for p in posts if p.get("basis") == "end_menu"][0]
    assert (f["away"], f["home"], f["quarter"], f["final_confirmed"]) == (30, 6, 4, True)
    assert f["delta_corroboration"]["kind"] == "exact_match"
    res = qm.apply_final({"final_t_ms": 1}, f)
    assert (res["away_score"], res["home_score"], res["quarter"], res["winner"]) == (30, 6, 4, "away")
    # A red 0:14 is still a running quarter.
    core, posts = _core({"t_ms": 0, "home": 6, "away": 30, "quarter": 4, "clock_s": 14, "clock_red": True}, menu)
    for k in range(6):
        core.on_frame(10_000 + k * 500, im)
    assert not any(p.get("basis") == "end_menu" for p in posts)
    assert "clock not 0:00" in core.end_menu_rejected["why"]


def test_end_menu_after_the_clock_dies_at_the_q1_q2_break():
    """match-20261017-162055: quarter and clock went null at the Q1/Q2 whistle. The menu still confirms.

    A menu one period-length after that whistle is too early. Three period-lengths is the fourth quarter.
    """
    from types import SimpleNamespace as NS

    from qorcap_reader import ELAPSED_PERIOD_MS

    im = cv2.imread(str(HERE / "fixtures" / "m27_std_110028_end_menu.jpg"))
    assert madden_final_screen(prep(im))
    posts = []

    class Rdr:
        def __init__(self):
            self.next = None

        def read(self, _img):
            return self.next

    class Agree:
        def offer(self, fr, **_):
            return NS(agreed=fr.ok, read=fr if fr.ok else None)

    class FR(NS):
        @property
        def pair(self):
            if self.left_score is None or self.right_score is None:
                return None
            return (self.left_score, self.right_score)

    rd = Rdr()
    blank = np.zeros((360, 640, 3), np.uint8)
    core = ReaderCore(posts.append, rd, Agree(), None, detect_final, final_screen_detector=madden_final_screen,
                      end_menu_reader=ScriptedEndMenu(30, 6))
    rd.next = FR(ok=True, reason="ok", left_score=30, right_score=6, quarter=1, clock_seconds=1, clock_red=False)
    core.on_frame(0, blank)
    rd.next = FR(ok=False, reason="quarter_unsure", left_score=30, right_score=6, quarter=None,
                 clock_seconds=None, clock_red=False)
    for t in (1_000, 1_250, 1_500):
        core.on_frame(t, blank)
    assert core.periods.clock_unreadable and core.periods.first_period_end_ms == 0
    for k in range(6):
        core.on_frame(ELAPSED_PERIOD_MS + k * 500, im)
    assert not any(p.get("basis") == "end_menu" for p in posts)
    assert core.end_menu_rejected and "no Q4" in core.end_menu_rejected["why"]
    for k in range(6):
        core.on_frame(3 * ELAPSED_PERIOD_MS + k * 500, im)
    f = [p for p in posts if p.get("basis") == "end_menu"][0]
    assert (f["away"], f["home"], f["quarter"], f["final_confirmed"]) == (30, 6, 4, True)


def test_end_menu_rejection_is_logged(capsys):
    core, posts = _core({"t_ms": 0, "home": 20, "away": 0, "quarter": 4, "clock_s": 30, "clock_red": False},
                        ScriptedEndMenu(0, 20))
    for k in range(6):
        core.on_frame(10_000 + k * 500, RST)
    err = capsys.readouterr().err
    assert "END MENU rejected:" in err and "clock not 0:00" in err
    assert not any(p.get("basis") == "end_menu" for p in posts)


def _floats(v, path=""):
    if isinstance(v, float):
        return [path]
    if isinstance(v, dict):
        return [p for k, x in v.items() for p in _floats(x, f"{path}.{k}")]
    if isinstance(v, (list, tuple)):
        return [p for i, x in enumerate(v) for p in _floats(x, f"{path}[{i}]")]
    return []


def test_posted_end_menu_final_is_jcs_safe_match_193534():
    """v0.3.25 regression (match 193534): a posted end-menu final carried conf and margin as floats.

    That match is held out, so this test does not open its footage. A scripted read still has those
    floats in memory, and the event posted to qorcap.exe must canonicalize them.
    """
    from qor_receipt.jcs import canonicalize
    core, posts = _core({"t_ms": 0, "home": 14, "away": 7, "quarter": 4, "clock_s": 0, "clock_red": True},
                        ScriptedEndMenu(7, 14, conf=0.953, margin=0.421))
    for k in range(6):
        core.on_frame(10_000 + k * 500, RST)
    f = next(p for p in posts if p["type"] == "final" and p["basis"] == "end_menu")
    # the reader's own state still holds the floats (the referee's end-menu fallback reads `conf`) ...
    assert isinstance(core.final["end_menu"]["conf"], float) and isinstance(core.final["end_menu"]["margin"], float)
    # ... but what is posted to qorcap.exe has none
    assert _floats(f) == [], _floats(f)
    em = f["end_menu"]
    assert "conf" not in em and "margin" not in em
    assert em["conf_permille"] == round(core.final["end_menu"]["conf"] * 1000)
    assert em["margin_permille"] == round(core.final["end_menu"]["margin"] * 1000)
    for p in posts:
        canonicalize(p)  # raises on floats


def test_jcs_safe_helper():
    from qorcap_reader import jcs_safe
    src = {"a": 1, "b": 0.9534, "c": {"d": [0.5, 2, None, True]}, "e": "x", "f": False}
    out = jcs_safe(src)
    assert out == {"a": 1, "b_permille": 953, "c": {"d": [500, 2, None, True]}, "e": "x", "f": False}
    assert src["b"] == 0.9534  # input untouched
