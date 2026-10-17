"""Thursday Night (primetime "TNP") scorebug profile ``madden27_thursday_night`` (v0.3.11). Real frames:
match-20261017-010703 (CLE "EVE" away, left, at IND "QOR" home, right). The pack holds real glyphs only: digits not
yet seen are absent, so they fail closed (see test_pack_is_real_only)."""
import os
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, os.environ.get("QORTIME", "/workspace/qortime-src"))
import qorcap_mnp as M  # noqa: E402
import qorcap_snp as S  # noqa: E402
import qorcap_tnp as T  # noqa: E402
from qorcap_reader import madden_final_screen, prep  # noqa: E402

FX = HERE / "fixtures"
TNP = ["m27_tnp_q1_6_0.jpg", "m27_tnp_q1_7_6.jpg", "m27_tnp_q1_7_13.jpg", "m27_tnp_q2_7_14.jpg", "m27_tnp_q2_7_14_oos.jpg",
       "m27_tnp_q3_27_20.jpg", "m27_tnp_q4_34_26.jpg"]
PAIRS = [(6, 0), (7, 6), (7, 13), (7, 14), (7, 14), (27, 20), (34, 26)]
OTHERS = ["m27_snp_ps_menu.jpg", "m27_snp_intro.jpg", "m27_snp_q1_7_7.jpg", "m27_snp_g2_q2_7_14.jpg", "m27_mnp_q1.jpg",
          "m27_mnp_q2.jpg", "m27_mnp_q4_final.jpg", "m27_mnp_end_menu_online.jpg", "m27_end_menu_finish.jpg",
          "m27_pause_menu.jpg", "m27_tnp_no_bar.jpg"]


def img(name):
    return cv2.imread(str(FX / name))


@pytest.fixture(scope="module")
def tnp():
    return T.ThursdayNightReader()


@pytest.mark.parametrize("name,away,home,q,clock", [
    ("m27_tnp_q1_6_0.jpg", 6, 0, 1, "3:55"),      # away touchdown before the extra point
    ("m27_tnp_q1_7_6.jpg", 7, 6, 1, "1:17"),
    ("m27_tnp_q1_7_13.jpg", 7, 13, 1, "0:05"),    # two-digit home score
    ("m27_tnp_q2_7_14.jpg", 7, 14, 2, "3:43"),
    ("m27_tnp_q2_7_14_oos.jpg", 7, 14, 2, "2:35"),
    ("m27_tnp_q3_27_20.jpg", 27, 20, 3, "1:52"),
    ("m27_tnp_q4_34_26.jpg", 34, 26, 4, "3:52"),
])
def test_thursday_night_reads(tnp, name, away, home, q, clock):
    fr = tnp.read(img(name))
    assert fr.ok, fr.reason
    assert (fr.left_score, fr.right_score, fr.quarter, fr.clock_text, fr.clock_red) == (away, home, q, clock, False)
    assert fr.profile_id == "madden27_thursday_night" and fr.score_conf >= 0.8


def test_red_final_seconds_clock(tnp):
    fr = tnp.read(img("m27_tnp_q2_red_clock.jpg"))
    assert fr.ok and (fr.left_score, fr.right_score, fr.quarter, fr.clock_text, fr.clock_red) == (14, 20, 2, "0:06", True)


@pytest.mark.parametrize("name", OTHERS)
def test_not_thursday_night_is_layout_unknown(tnp, name):
    assert tnp.read(img(name)).reason == "layout_unknown"


@pytest.mark.parametrize("name", TNP)
def test_other_profiles_do_not_claim_thursday_night(name):
    from qor_receipt._vendor.qoresence.vision.local_scorebug.reader import ScorebugReader
    assert M.MondayNightReader().read(img(name)).reason == "layout_unknown"
    assert S.SundayNightReader().read(img(name)).reason == "layout_unknown"
    assert ScorebugReader().read(prep(img(name))).ok is False


def test_half_resolution_frame_never_misreads(tnp):
    for name, pair in zip(TNP, PAIRS):
        fr = tnp.read(cv2.resize(img(name), (640, 360), interpolation=cv2.INTER_AREA))
        assert fr.ok is False or (fr.left_score, fr.right_score) == pair


def test_tampered_score_digit_fails_closed(tnp):
    im = img("m27_tnp_q2_7_14.jpg").copy()
    im[644:676, 580:602] = im[644:676, 580:602].min()   # erase the away score
    fr = tnp.read(im)
    assert not fr.ok and fr.reason.startswith("score_away")


def test_clock_glyphs_are_m_ss():
    for name in TNP:
        assert len(T.clock_glyphs(S.hsv_of(img(name)))) == 3


def test_clear_colons_and_bridge():
    m = np.zeros((14, 20), bool)
    m[0:14, 0:2] = True                        # digit stroke
    m[2:6, 5:7] = m[8:11, 5:7] = True           # colon (two tall dots, as at 720p)
    out = T.clear_colons(m)
    assert out[:, 0:2].all() and not out[:, 5:7].any()
    b = np.zeros((3, 9), bool)
    b[1, [0, 2]] = True                        # one-column break -> bridged
    b[1, [5, 8]] = True                        # two-column gap -> kept apart
    assert T.bridge(b)[1].tolist() == [True, True, True, False, False, True, False, False, True]


def test_pack_is_real_only(tnp):
    assert all(v.startswith("real:match-20261017-010703") for v in tnp.source.values())
    seen = lambda g: set(tnp.groups[g][0])  # noqa: E731
    assert seen("clock") == set("0123456789")
    assert {"0", "1", "2", "3", "4", "6", "7"} <= seen("score")   # 5/8/9 added only when seen in real frames
    assert seen("quarter") == {"1", "2", "3", "4"}


def test_unseen_digit_fails_closed_not_wrong():
    """Drop the real "4" score templates: the 7-14 frames must fail closed (score_home_unsure), never read another number."""
    r = T.ThursdayNightReader()
    labs, vecs = r.groups["score"]
    keep = [i for i, lab in enumerate(labs) if lab != "4"]
    r.groups["score"] = ([labs[i] for i in keep], vecs[keep])
    for name in ["m27_tnp_q2_7_14.jpg", "m27_tnp_q2_7_14_oos.jpg"]:
        fr = r.read(img(name))
        assert not fr.ok and fr.reason == "score_home_unsure"


def test_auto_picks_thursday_night_and_switches_back():
    from qor_receipt._vendor.qoresence.vision.local_scorebug.reader import ScorebugReader
    a = M.AutoProfileReader(ScorebugReader(), prep=prep)
    fr = a.read(img("m27_tnp_q2_7_14.jpg"))
    assert fr.ok and (fr.left_score, fr.right_score, fr.quarter) == (7, 14, 2) and a.active_profile == "madden27_thursday_night"
    assert a.read(img("m27_snp_q1_7_7.jpg")).profile_id == "madden27_sunday_night"
    assert a.read(img("m27_mnp_q2.jpg")).profile_id == "madden27_monday_night"
    assert a.read(img("m27_tnp_q1_7_6.jpg")).profile_id == "madden27_thursday_night"


@pytest.mark.parametrize("name", TNP)
def test_end_menu_reader_ignores_thursday_night_gameplay(name):
    from qorcap_endmenu import EndMenuReader
    assert not madden_final_screen(prep(img(name)))
    assert EndMenuReader().read(img(name)).ok is False


def test_reference_band_is_the_bar(tnp):
    band, why = tnp.reference_band(img("m27_tnp_q2_7_14.jpg"))
    assert why == "ok" and band.shape == (T.BAR_Y[1] - T.BAR_Y[0], 1280)
