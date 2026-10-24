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
from qorcap_standard import standard_reader  # noqa: E402
from qorcap_template_guard import TEMPLATES_MISSING  # noqa: E402

FX = HERE / "fixtures"
SNP = ["m27_snp_q1_kickoff_0_0.jpg", "m27_snp_q1_6_0.jpg", "m27_snp_q1_7_0.jpg", "m27_snp_q1_7_7.jpg", "m27_snp_g2_q2_7_14.jpg"]


def img(name):
    return cv2.imread(str(FX / name))


def abstain(fr):
    assert fr.ok is False and fr.reason == TEMPLATES_MISSING, getattr(fr, "reason", fr)
    assert fr.left_score is None and fr.right_score is None
    assert fr.quarter is None and fr.clock_text is None


@pytest.fixture(autouse=True)
def _empty_provenance(monkeypatch, tmp_path):
    """A committed harvest must not turn these abstentions into layout_unknown."""
    import json
    import qorcap_template_guard as guard

    prov = tmp_path / "provenance.json"
    prov.write_text(json.dumps({
        "schema": "qorcap-template-provenance/v0",
        "harvest_script": "tools/harvest_templates.py",
        "harvest_version": "3",
        "templates": [],
    }), encoding="utf-8")
    monkeypatch.setattr(guard, "PROVENANCE_PATH", prov)


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
    """The Sunday Night pack mixed held-out match 021003 into its means. These frames abstain."""
    fr = snp.read(img(name))
    abstain(fr)
    assert (fr.left_score, fr.right_score, fr.quarter, fr.clock_text) != (away, home, q, clock)


@pytest.mark.parametrize("name", ["m27_snp_ps_menu.jpg", "m27_snp_intro.jpg", "m27_mnp_q1.jpg", "m27_mnp_q2.jpg",
                                  "m27_mnp_q4_final.jpg", "m27_mnp_end_menu_online.jpg",
                                  "m27_pause_menu.jpg"])
def test_not_sunday_night_is_layout_unknown(snp, name):
    assert snp.read(img(name)).reason == TEMPLATES_MISSING


@pytest.mark.parametrize("name", SNP)
def test_other_profiles_do_not_claim_sunday_night(name):
    assert M.MondayNightReader().read(img(name)).reason == TEMPLATES_MISSING
    abstain(standard_reader().read(img(name)))


def test_half_resolution_frame_never_misreads(snp):
    for name, pair in zip(SNP, [(0, 0), (6, 0), (7, 0), (7, 7), (7, 14)]):
        fr = snp.read(cv2.resize(img(name), (640, 360), interpolation=cv2.INTER_AREA))
        assert fr.ok is False or (fr.left_score, fr.right_score) == pair


def test_tampered_score_digit_fails_closed(snp):
    im = img("m27_snp_q1_7_0.jpg").copy()
    im[672:694, 515:545] = im[672:694, 515:545].min()   # erase the away score
    abstain(snp.read(im))


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
    """The Sunday Night pack named held-out match 021003 inside its means. The file is gone."""
    assert snp._missing and snp.source == {} and snp.groups == {}
    assert not Path(S.PACK).is_file()


def test_unseen_quarter_fails_closed_not_wrong():
    """No quarter templates are loaded, so a 2nd-quarter frame abstains instead of posting a quarter."""
    r = S.SundayNightReader()
    for name in ["m27_snp_g2_q2_7_7.jpg", "m27_snp_g2_q2_7_13.jpg", "m27_snp_g2_q2_7_14.jpg"]:
        abstain(r.read(img(name)))


def test_auto_with_real_readers_picks_sunday_night():
    a = M.AutoProfileReader(standard_reader(), prep=prep)
    assert [M.AutoProfileReader._pid(r) for r in a.readers[1:]] == ["madden27_monday_night", "madden27_sunday_night",
                                                                               "madden27_thursday_night"]
    abstain(a.read(img("m27_snp_q1_7_7.jpg")))
    abstain(a.read(img("m27_mnp_q2.jpg")))
    abstain(a.read(img("m27_pause_menu.jpg")))
    assert a.profile_counts == {}


@pytest.mark.parametrize("name", SNP)
def test_end_menu_reader_ignores_sunday_night_gameplay(name):
    from qorcap_endmenu import EndMenuReader
    assert not madden_final_screen(prep(img(name)))
    assert EndMenuReader().read(img(name)).ok is False


def test_reference_band_is_the_bar(snp):
    band, why = snp.reference_band(img("m27_snp_q1_7_0.jpg"))
    assert why == "ok" and band.shape == (S.BAR_Y[1] - S.BAR_Y[0], 1280)
