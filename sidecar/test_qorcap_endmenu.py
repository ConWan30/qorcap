"""End-of-game menu score reader (v0.3.4). Fixtures are real frames from Con's 2026-10-16 matches:
m27_end_menu_finish.jpg = match-20261016-125430 (AWAY 7 Saints - HOME 14 Giants, cursor FINISH GAME),
m27_end_menu_restart.jpg = match-20261016-143212 (AWAY 0 - HOME 20, cursor RESTART; scorebug last read 0-17)."""
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
    core, posts = _core({"t_ms": 0, "home": 17, "away": 0, "quarter": 4, "clock_s": 0, "clock_red": True})
    for k in range(6):
        core.on_frame(10_000 + k * 500, RST)
    fins = [p for p in posts if p["type"] == "final"]
    em = [f for f in fins if f["basis"] == "end_menu"]
    assert len(em) == 1
    f = em[0]
    assert (f["away"], f["home"], f["method"]) == (0, 20, "end_menu_read")
    assert f["scorebug_last"]["home"] == 17 and "final play score not shown on scorebug" in f["notes"][0]
    assert f["end_menu"]["frames"] >= 3


def test_core_end_menu_match1_no_note():
    core, posts = _core({"t_ms": 0, "home": 14, "away": 7, "quarter": 4, "clock_s": 0, "clock_red": True})
    for k in range(6):
        core.on_frame(10_000 + k * 500, FIN)
    f = [p for p in posts if p["type"] == "final" and p["basis"] == "end_menu"][0]
    assert (f["away"], f["home"]) == (7, 14) and f["notes"] == []


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
          "scorebug_last": {"home": 17, "away": 0, "quarter": 4, "t_ms": 2}, "notes": ["final play score not shown on scorebug"]}
    r = qm.apply_final(base, em)
    assert r["final_confirmed"] is True and r["method"] == "end_menu_read" and r["home_score"] == 20
    r = qm.apply_final(base, {"basis": "clock_zero", "home": 17, "away": 0, "quarter": 4, "t_ms": 3})
    assert r["final_confirmed"] is False and r["final_basis"] == "clock_zero"
    r = qm.apply_final(base, None, fallback={"home": 17, "away": 0, "quarter": 4, "t_ms": 3})
    assert r["final_confirmed"] is False and r["method"] == "last_scorebug_read_unconfirmed" and "final_basis" not in r
