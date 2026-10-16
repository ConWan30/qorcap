"""Monday Night (primetime) scorebug profile + auto profile selection (v0.3.5). Frames: match-20261016-170000."""
import os
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import cv2
import pytest

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, os.environ.get("QORTIME", "/workspace/qortime-src"))
import qorcap_mnp as M  # noqa: E402
from qorcap_reader import madden_final_screen, prep  # noqa: E402

FX = HERE / "fixtures"


def img(name):
    return cv2.imread(str(FX / name))


@pytest.fixture(scope="module")
def mnp():
    return M.MondayNightReader()


@pytest.mark.parametrize("name,away,home,q,clock,red", [
    ("m27_mnp_q1.jpg", 0, 0, 1, "4:07", False),
    ("m27_mnp_q2.jpg", 14, 13, 2, "2:27", False),
    ("m27_mnp_q4_final.jpg", 34, 29, 4, "0:00", True),   # red final-minute clock, touching "00"
])
def test_monday_night_reads(mnp, name, away, home, q, clock, red):
    fr = mnp.read(img(name))
    assert fr.ok, fr.reason
    assert (fr.left_score, fr.right_score, fr.quarter, fr.clock_text, fr.clock_red) == (away, home, q, clock, red)
    assert fr.profile_id == "madden27_monday_night"


def test_half_resolution_frame_still_reads_scores(mnp):
    fr = mnp.read(cv2.resize(img("m27_mnp_q2.jpg"), (640, 360), interpolation=cv2.INTER_AREA))
    assert fr.ok is False or (fr.left_score, fr.right_score) == (14, 13)  # never a wrong score


@pytest.mark.parametrize("name", ["m27_end_menu_finish.jpg", "m27_pause_menu.jpg", "m27_mnp_end_menu_online.jpg"])
def test_not_monday_night_is_layout_unknown(mnp, name):
    assert mnp.read(img(name)).reason == "layout_unknown"


def test_pack_provenance(mnp):
    assert mnp.source["score:5"].startswith("synthetic")
    assert all(mnp.source[f"score:{d}"].startswith("real") for d in "012346789")
    assert set(mnp.groups["quarter"][0]) == {"1ST", "2ND", "3RD", "4TH"}


class Fixed:
    def __init__(self, reason, profile_id, ok=False):
        self.reason, self.profile_id, self.ok = reason, profile_id, ok
        self.calls = 0

    def read(self, im):
        self.calls += 1
        return NS(ok=self.ok, reason=self.reason, profile_id=self.profile_id)


def test_auto_prefers_standard_and_keeps_it():
    std, mn = Fixed("ok", "madden27_standard", ok=True), Fixed("layout_unknown", "madden27_monday_night")
    a = M.AutoProfileReader(std, [mn])
    for _ in range(3):
        assert a.read(None).profile_id == "madden27_standard"
    assert mn.calls == 0 and a.active_profile == "madden27_standard"


def test_auto_falls_back_to_monday_night_and_sticks():
    std, mn = Fixed("layout_unknown", "madden27_standard"), Fixed("ok", "madden27_monday_night", ok=True)
    a = M.AutoProfileReader(std, [mn])
    assert a.read(None).profile_id == "madden27_monday_night"
    std.calls = 0
    a.read(None)
    assert std.calls == 0  # active profile tried first
    assert a.active_profile == "madden27_monday_night" and a.profile_counts == {"madden27_monday_night": 2}


def test_auto_standard_misread_is_not_masked():
    std, mn = Fixed("score_unsure", "madden27_standard"), Fixed("ok", "madden27_monday_night", ok=True)
    assert M.AutoProfileReader(std, [mn]).read(None).reason == "score_unsure"  # standard layout seen: no fallback


def test_auto_with_real_readers():
    from qor_receipt._vendor.qoresence.vision.local_scorebug.reader import ScorebugReader
    a = M.AutoProfileReader(ScorebugReader(), prep=prep)
    fr = a.read(img("m27_mnp_q2.jpg"))
    assert fr.ok and (fr.left_score, fr.right_score) == (14, 13) and a.active_profile == "madden27_monday_night"
    assert a.read(img("m27_end_menu_finish.jpg")).reason == "layout_unknown"


def test_online_end_menu_is_final_screen_and_fails_closed_on_occluded_away():
    from qorcap_endmenu import EndMenuReader
    im = img("m27_mnp_end_menu_online.jpg")
    assert madden_final_screen(prep(im))  # gamertag banners no longer hide the end menu
    rd = EndMenuReader().read(im)
    assert not rd.ok and rd.reason.startswith("away_")  # name tags cover the away score: never guessed


def test_end_menu_real_9_template_still_xchecked():
    from qorcap_endmenu import EndMenuReader, side_glyphs
    r, im = EndMenuReader(), prep(img("m27_mnp_end_menu_online.jpg"))
    d2, d9 = (r._digit(g) for g in side_glyphs(im, "home"))
    assert d2[0] == "2"
    assert d9[1]["label"] == "9" and d9[1]["source"] == "real" and d9[1]["best"] > 0.9
    # QorTime's scorebug pack calls this glyph "0": the cross-check still fails it closed (v0.3.4 rule kept)
    assert d9[0] is None and d9[1]["why"] == "xcheck_disagrees"
