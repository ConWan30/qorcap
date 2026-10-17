"""Sunday Night (primetime) scorebug profile ``madden27_sunday_night`` (v0.3.11). Real frames: match-20261017-002326
and match-20261017-004308 (HOU "QOR" at CLE "EVE"; both online games disconnected, in Q1 and in Q2, so real score
digits are 0 1 3 4 6 7 and real quarters 1 and 2; the rest are synthetic, see the pack provenance)."""
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
from qorcap_reader import madden_final_screen, prep  # noqa: E402

FX = HERE / "fixtures"
SNP = ["m27_snp_q1_kickoff_0_0.jpg", "m27_snp_q1_6_0.jpg", "m27_snp_q1_7_0.jpg", "m27_snp_q1_7_7.jpg", "m27_snp_g2_q2_7_14.jpg"]


def img(name):
    return cv2.imread(str(FX / name))


@pytest.fixture(scope="module")
def snp():
    return S.SundayNightReader()


@pytest.mark.parametrize("name,away,home,q,clock", [
    ("m27_snp_q1_kickoff_0_0.jpg", 0, 0, 1, "6:00"),
    ("m27_snp_q1_6_0.jpg", 6, 0, 1, "4:32"),      # touchdown before the extra point
    ("m27_snp_q1_7_0.jpg", 7, 0, 1, "4:28"),
    ("m27_snp_q1_7_7.jpg", 7, 7, 1, "1:43"),      # home "7" white on the red (team colour) panel
    ("m27_snp_g2_q1_0_7.jpg", 0, 7, 1, "2:00"),   # second match
    ("m27_snp_g2_q2_7_7.jpg", 7, 7, 2, "6:00"),
    ("m27_snp_g2_q2_7_13.jpg", 7, 13, 2, "4:49"),
    ("m27_snp_g2_q2_7_14.jpg", 7, 14, 2, "4:09"),
])
def test_sunday_night_reads(snp, name, away, home, q, clock):
    fr = snp.read(img(name))
    assert fr.ok, fr.reason
    assert (fr.left_score, fr.right_score, fr.quarter, fr.clock_text, fr.clock_red) == (away, home, q, clock, False)
    assert fr.profile_id == "madden27_sunday_night" and fr.score_conf >= 0.8


@pytest.mark.parametrize("name", ["m27_snp_ps_menu.jpg", "m27_snp_intro.jpg", "m27_mnp_q1.jpg", "m27_mnp_q2.jpg",
                                  "m27_mnp_q4_final.jpg", "m27_mnp_end_menu_online.jpg", "m27_end_menu_finish.jpg",
                                  "m27_pause_menu.jpg"])
def test_not_sunday_night_is_layout_unknown(snp, name):
    assert snp.read(img(name)).reason == "layout_unknown"


@pytest.mark.parametrize("name", SNP)
def test_other_profiles_do_not_claim_sunday_night(name):
    from qor_receipt._vendor.qoresence.vision.local_scorebug.reader import ScorebugReader
    assert M.MondayNightReader().read(img(name)).reason == "layout_unknown"
    assert ScorebugReader().read(prep(img(name))).ok is False


def test_half_resolution_frame_never_misreads(snp):
    for name, pair in zip(SNP, [(0, 0), (6, 0), (7, 0), (7, 7), (7, 14)]):
        fr = snp.read(cv2.resize(img(name), (640, 360), interpolation=cv2.INTER_AREA))
        assert fr.ok is False or (fr.left_score, fr.right_score) == pair


def test_tampered_score_digit_fails_closed(snp):
    im = img("m27_snp_q1_7_0.jpg").copy()
    im[672:694, 515:545] = im[672:694, 515:545].min()   # erase the away score
    fr = snp.read(im)
    assert not fr.ok and fr.reason.startswith("score_away")


def test_clock_colon_is_cleared():
    # at 720p the colon touches the next digit; clear_colons must leave exactly three clock glyphs (m:ss)
    for name in SNP:
        assert len(S.clock_glyphs(S.hsv_of(img(name)))) == 3


def test_clear_colons_keeps_digits():
    m = np.zeros((14, 20), bool)
    m[0:14, 0:2] = True                        # a digit stroke (has ink in the top rows)
    m[3:6, 5:7] = m[9:12, 5:7] = True          # a colon (two dots)
    out = S.clear_colons(m)
    assert out[:, 0:2].all() and not out[:, 5:7].any()


def test_pack_provenance_marks_synthetic_digits(snp):
    real_score = {d for d in "0123456789" if snp.source[f"score:{d}"].startswith("real")}
    # 2 was harvested live from match-20261017-021003 (eye-checked); 5/9 have not been seen on a real score yet
    assert real_score == {"0", "1", "2", "3", "4", "6", "7", "8"}
    assert all(snp.source[f"score:{d}"] == "synthetic:clock_font_resized" for d in "59")
    assert all(snp.source[f"clock:{d}"].startswith("real") for d in "0123456789")
    assert snp.source["quarter:1"].startswith("real") and snp.source["quarter:2"].startswith("real")
    assert snp.source["quarter:3"].startswith("real")             # harvested live from match-20261017-021003
    assert snp.source["quarter:4"].startswith("real")             # harvested live from match-20261017-021003
    assert set(snp.groups["quarter"][0]) == {"1", "2", "3", "4"}


def test_unseen_quarter_fails_closed_not_wrong():
    """With only skeleton (synthetic) templates for 2/3/4, every real 2nd-quarter frame read quarter_unsure, never a
    wrong quarter (measured on match-20261017-004308 before real 2ND frames were added); keep that property."""
    d = np.load(str(S.PACK))
    keep = [i for i, lab in enumerate(d["quarter_labels"]) if str(lab) != "2"]
    r = S.SundayNightReader()
    r.groups["quarter"] = ([str(d["quarter_labels"][i]) for i in keep], d["quarter_vecs"][keep].astype(np.float32))
    for name in ["m27_snp_g2_q2_7_7.jpg", "m27_snp_g2_q2_7_13.jpg", "m27_snp_g2_q2_7_14.jpg"]:
        fr = r.read(img(name))
        assert not fr.ok and fr.reason == "quarter_unsure"


def test_auto_with_real_readers_picks_sunday_night():
    from qor_receipt._vendor.qoresence.vision.local_scorebug.reader import ScorebugReader
    a = M.AutoProfileReader(ScorebugReader(), prep=prep)
    assert [M.AutoProfileReader._pid(r) for r in a.readers[1:]] == ["madden27_monday_night", "madden27_sunday_night",
                                                                               "madden27_thursday_night"]
    fr = a.read(img("m27_snp_q1_7_7.jpg"))
    assert fr.ok and (fr.left_score, fr.right_score) == (7, 7) and a.active_profile == "madden27_sunday_night"
    assert a.read(img("m27_mnp_q2.jpg")).profile_id == "madden27_monday_night"   # still switches back
    assert a.read(img("m27_end_menu_finish.jpg")).reason == "layout_unknown"


@pytest.mark.parametrize("name", SNP)
def test_end_menu_reader_ignores_sunday_night_gameplay(name):
    from qorcap_endmenu import EndMenuReader
    assert not madden_final_screen(prep(img(name)))
    assert EndMenuReader().read(img(name)).ok is False


def test_reference_band_is_the_bar(snp):
    band, why = snp.reference_band(img("m27_snp_q1_7_0.jpg"))
    assert why == "ok" and band.shape == (S.BAR_Y[1] - S.BAR_Y[0], 1280)
