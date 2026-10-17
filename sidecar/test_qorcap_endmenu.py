"""End-of-game menu score reader (v0.3.4). Fixtures are real frames from Con's 2026-10-16 matches:
m27_end_menu_finish.jpg = match-20261016-125430 (AWAY 7 Saints - HOME 14 Giants, cursor FINISH GAME),
m27_end_menu_restart.jpg = match-20261016-143212 (AWAY 0 - HOME 20, cursor RESTART; scorebug last read 0-17)."""

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
from qorcap_reader import ReaderCore, end_menu_consistency, madden_final_screen  # noqa: E402
from qor_receipt.final_detect import detect_final  # noqa: E402

FIN = cv2.imread(str(HERE / "fixtures" / "m27_end_menu_finish.jpg"))
RST = cv2.imread(str(HERE / "fixtures" / "m27_end_menu_restart.jpg"))
R = EndMenuReader()


def test_reads_both_matches_away_left_home_right():
    a = R.read(RST)
    assert a.ok and (a.away, a.home) == (0, 20), a
    b = R.read(FIN)
    assert b.ok and (b.away, b.home) == (7, 14), b      # match 1: away 7, home 14 (Con confirmed 14-7 home win)
    assert R.read(cv2.resize(RST, (1280, 720))).away == 0  # card resolution is downscaled to 640x360 first
    assert R.xpack is not None and all(d["xcheck"] == d["label"] for d in a.digits + b.digits)


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


def _core(last):
    posts = []
    core = ReaderCore(posts.append, None, None, Paused(), detect_final, final_screen_detector=madden_final_screen,
                      end_menu_reader=R)
    core.timeline = [dict(last)]
    return core, posts


def test_core_end_menu_final_overrides_scorebug_and_notes():
    """Match 2 (0-20 end menu vs 0-17 scorebug): accepted as end_menu_read but NOT confirmed without corroboration."""
    core, posts = _core({"t_ms": 0, "home": 17, "away": 0, "quarter": 4, "clock_s": 0, "clock_red": True})
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
    core, posts = _core({"t_ms": 0, "home": 17, "away": 0, "quarter": 4, "clock_s": 0, "clock_red": True})
    core.delta_corroboration = [{"kind": "haptic", "t_ms": 500}]
    for k in range(6):
        core.on_frame(10_000 + k * 500, RST)
    f = [p for p in posts if p.get("basis") == "end_menu"][0]
    assert f["final_confirmed"] is True and f["delta_corroboration"]["kind"] == "haptic"


def test_core_end_menu_match1_no_note():
    core, posts = _core({"t_ms": 0, "home": 14, "away": 7, "quarter": 4, "clock_s": 0, "clock_red": True})
    for k in range(6):
        core.on_frame(10_000 + k * 500, FIN)
    f = [p for p in posts if p["type"] == "final" and p["basis"] == "end_menu"][0]
    assert (f["away"], f["home"]) == (7, 14) and f["notes"] == []
    assert f["final_confirmed"] is True and f["delta_corroboration"]["kind"] == "exact_match"


def test_core_rejects_inconsistent_or_pre_q4():
    core, posts = _core({"t_ms": 0, "home": 3, "away": 0, "quarter": 4, "clock_s": 0})
    for k in range(6):
        core.on_frame(10_000 + k * 500, RST)        # 0-3 -> 0-20 is not one play: refuse, stay unconfirmed
    assert not any(p.get("basis") == "end_menu" for p in posts) and core.end_menu_rejected
    core, posts = _core({"t_ms": 0, "home": 17, "away": 0, "quarter": 2, "clock_s": 0})
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


def test_core_rejects_end_menu_before_clock_zero():
    """Doctored end-menu frames matching a mid-Q4 score must not end the match early."""
    core, posts = _core({"t_ms": 0, "home": 20, "away": 0, "quarter": 4, "clock_s": 30, "clock_red": False})
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
        # No pre-seeded delta_corroboration — must come from the bundle via load_delta_corroboration_from_bundle
        return ReaderCore(post, None, None, Paused(), detect_final, final_screen_detector=madden_final_screen,
                          end_menu_reader=R)

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
