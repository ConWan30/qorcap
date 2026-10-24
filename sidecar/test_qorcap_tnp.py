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
from qorcap_standard import standard_reader  # noqa: E402
from qorcap_template_guard import TEMPLATES_MISSING  # noqa: E402

FX = HERE / "fixtures"
TNP = ["m27_tnp_q1_6_0.jpg", "m27_tnp_q1_7_6.jpg", "m27_tnp_q1_7_13.jpg", "m27_tnp_q2_7_14.jpg", "m27_tnp_q2_7_14_oos.jpg",
       "m27_tnp_q3_27_20.jpg", "m27_tnp_q4_34_26.jpg"]
PAIRS = [(6, 0), (7, 6), (7, 13), (7, 14), (7, 14), (27, 20), (34, 26)]
OTHERS = ["m27_snp_ps_menu.jpg", "m27_snp_intro.jpg", "m27_snp_q1_7_7.jpg", "m27_snp_g2_q2_7_14.jpg", "m27_mnp_q1.jpg",
          "m27_mnp_q2.jpg", "m27_mnp_q4_final.jpg", "m27_mnp_end_menu_online.jpg",
          "m27_pause_menu.jpg", "m27_tnp_no_bar.jpg"]


def abstain(fr):
    """A removed pack must not guess a score, a quarter, or a clock."""
    assert fr.ok is False and fr.reason == TEMPLATES_MISSING, getattr(fr, "reason", fr)
    assert fr.left_score is None and fr.right_score is None
    assert fr.quarter is None and fr.clock_text is None


def img(name):
    return cv2.imread(str(FX / name))


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
    """The Thursday Night pack is not an allowlisted template source, so the old eye label is not emitted."""
    fr = tnp.read(img(name))
    abstain(fr)
    assert (fr.left_score, fr.right_score, fr.quarter, fr.clock_text) != (away, home, q, clock)


def test_red_final_seconds_clock(tnp):
    abstain(tnp.read(img("m27_tnp_q2_red_clock.jpg")))


@pytest.mark.parametrize("name", OTHERS)
def test_not_thursday_night_is_layout_unknown(tnp, name):
    """No pack is loaded, so the badge check never runs and every frame abstains."""
    assert tnp.read(img(name)).reason == TEMPLATES_MISSING


@pytest.mark.parametrize("name", TNP)
def test_other_profiles_do_not_claim_thursday_night(name):
    assert M.MondayNightReader().read(img(name)).reason == TEMPLATES_MISSING
    assert S.SundayNightReader().read(img(name)).reason == TEMPLATES_MISSING
    abstain(standard_reader().read(img(name)))


def test_half_resolution_frame_never_misreads(tnp):
    for name, pair in zip(TNP, PAIRS):
        fr = tnp.read(cv2.resize(img(name), (640, 360), interpolation=cv2.INTER_AREA))
        assert fr.ok is False or (fr.left_score, fr.right_score) == pair


def test_tampered_score_digit_fails_closed(tnp):
    im = img("m27_tnp_q2_7_14.jpg").copy()
    im[644:676, 580:602] = im[644:676, 580:602].min()   # erase the away score
    abstain(tnp.read(im))


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
    """010703 / 091651 / 232726 / 002316 are not allowlisted template matches. The pack file is gone."""
    assert tnp._missing and tnp.source == {} and tnp.groups == {} and tnp.badge is None
    assert not Path(T.PACK).is_file()


def test_unseen_digit_fails_closed_not_wrong():
    """With no score templates loaded, a 7-14 frame abstains. It does not become another number."""
    r = T.ThursdayNightReader()
    for name in ["m27_tnp_q2_7_14.jpg", "m27_tnp_q2_7_14_oos.jpg"]:
        abstain(r.read(img(name)))


def test_auto_picks_thursday_night_and_switches_back():
    a = M.AutoProfileReader(standard_reader(), prep=prep)
    assert [M.AutoProfileReader._pid(r) for r in a.readers[1:]] == [
        "madden27_monday_night", "madden27_sunday_night", "madden27_thursday_night",
    ]
    for name in ("m27_tnp_q2_7_14.jpg", "m27_snp_q1_7_7.jpg", "m27_mnp_q2.jpg", "m27_tnp_q1_7_6.jpg"):
        abstain(a.read(img(name)))
    assert a.profile_counts == {}


@pytest.mark.parametrize("name", TNP)
def test_end_menu_reader_ignores_thursday_night_gameplay(name):
    from qorcap_endmenu import EndMenuReader
    assert not madden_final_screen(prep(img(name)))
    assert EndMenuReader().read(img(name)).ok is False


@pytest.mark.parametrize("name,away,home,q,clock,red", [
    ("m27_tnp_091651_q3_3_0.png", 3, 0, 3, "0:18", False),
    ("m27_tnp_091651_q4_10_7.png", 10, 7, 4, "0:05", True),
    ("m27_tnp_091651_ot_kick.png", 10, 10, 5, "2:00", False),
    ("m27_tnp_091651_ot_10_16.jpg", 10, 16, 5, "1:55", False),
    # The eye label on this frame says 0:37. The clock glyphs are a 2 and two zeros.
    ("m27_tnp_091651_q1_0_0.jpg", 0, 0, 1, "2:00", False),
])
def test_match_091651_reads(tnp, name, away, home, q, clock, red):
    fr = tnp.read(img(name))
    abstain(fr)
    assert (fr.left_score, fr.right_score, fr.quarter, fr.clock_text, fr.clock_red) != (away, home, q, clock, red)


def test_red_021_fails_closed_not_wrong(tnp):
    """No clock templates: the frame abstains. It does not invent 10-7 or another clock."""
    abstain(tnp.read(img("m27_tnp_091651_q4_021.png")))


def test_unknown_quarter_still_returns_clock_and_locks_on_three_reads(tnp):
    """No templates at all: three frames post no score. The quarter-soft lock is covered with synthetic reads."""
    from types import SimpleNamespace as NS
    from qor_receipt._vendor.qoresence.vision.local_scorebug.agreement import AgreementWindow
    from qorcap_reader import ReaderCore
    r = T.ThursdayNightReader()
    im = img("m27_tnp_q2_7_14.jpg")
    abstain(r.read(im))
    posts = []
    core = ReaderCore(posts.append, r, AgreementWindow(), None, lambda tl: NS(final=False))
    for i in range(3):
        core.on_frame(i * 250, im)
    assert not [p for p in posts if p["type"] == "score"]
    assert any(row.get("obs") == TEMPLATES_MISSING for row in core.timeline)


def test_auto_counts_thursday_night_when_quarter_fails():
    a = M.AutoProfileReader(standard_reader(), prep=prep)
    abstain(a.read(img("m27_tnp_q2_7_14.jpg")))
    assert a.profile_counts == {}


def test_auto_reads_ot_walkoff_not_the_orange_endzone():
    a = M.AutoProfileReader(standard_reader(), prep=prep)
    abstain(a.read(img("m27_tnp_091651_ot_10_16.jpg")))
    assert a.profile_counts == {}


def test_start_match_passes_official_challenges_by_default():
    """A double-clicked live match turns official challenges on.

    Either explicit flag (``--official-challenges`` or ``--no-official-challenges``)
    is the caller's choice and is left alone. ``finalize`` is an offline sign of a
    finished bundle, so the launcher must not inject the live flag: argparse would
    reject it before any clip is cut. The gate is the intent of that default, not
    one exact source line.
    """
    text = (HERE.parent / "start-match.ps1").read_text(encoding="utf-8")
    code = "\n".join(ln for ln in text.splitlines() if not ln.strip().startswith("#"))
    assert any(
        "hasChallengeFlag" in ln and "--official-challenges" in ln and "--no-official-challenges" in ln
        for ln in code.splitlines()
    ), "either explicit challenge flag should count as the caller's choice"
    assign = '$fwd = @("--official-challenges") + $fwd'
    pos = code.find(assign)
    assert pos != -1, "a live match should prepend --official-challenges"
    assert code.find(assign, pos + len(assign)) == -1, "the default flag is injected in one place"
    gate = code[code.rfind("if ", 0, pos):pos]
    compact = "".join(gate.split())
    assert "-not$hasChallengeFlag" in compact, gate
    assert "-not$isFinalize" in compact, gate
    src = (HERE / "qorcap_match.py").read_text(encoding="utf-8")
    assert 'ap.add_argument("--official-challenges", action="store_true"' in src
    assert 'ap.add_argument("--no-official-challenges", action="store_true"' in src
    opt_out = src.split("if a.no_official_challenges:", 1)
    assert len(opt_out) == 2 and "official_challenges = False" in opt_out[1][:240]


def test_reference_band_is_the_bar(tnp):
    band, why = tnp.reference_band(img("m27_tnp_q2_7_14.jpg"))
    assert why == "ok" and band.shape == (T.BAR_Y[1] - T.BAR_Y[0], 1280)


def test_quarter_digit_is_cut_from_the_suffix_and_a_fused_ordinal_fails_closed(tnp):
    """match-20261017-091651 draws 1ST as one blob. Classifying that whole blob matched the 1ST template
    closely enough to label 2ND frames as Q1. The digit is cut off; the fused blob itself does not clear the gate."""
    import cv2
    hsv = T.hsv_of(img("m27_tnp_091651_q1_0_0.jpg"))
    g = T.quarter_glyph(hsv)
    assert g is not None and g.shape[1] <= T.QUARTER_DIGIT_MAX_W
    m = T.quarter_mask(hsv).astype(np.uint8)
    n, lab, st, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
    best = min((i for i in range(1, n) if T.QUARTER_H[0] <= st[i][3] <= T.QUARTER_H[1] + 4), key=lambda i: st[i][0])
    x, y, w, h = (int(t) for t in st[best][:4])
    assert w > T.QUARTER_BLOB_MAX_W  # the uncut ordinal is wider than a digit or OT
    blob = (lab[y:y + h, x:x + w] == best).astype(np.uint8)
    assert blob.any()
    assert T.GATE["quarter"][1] >= 0.30
    assert "quarter" not in tnp.groups
    abstain(tnp.read(img("m27_tnp_q2_7_14.jpg")))


def test_quarter_does_not_go_backwards():
    """The live reader posts nothing from these frames. Quarter order is covered by synthetic ReaderCore tests."""
    from types import SimpleNamespace as NS
    from qor_receipt._vendor.qoresence.vision.local_scorebug.agreement import AgreementWindow
    from qorcap_reader import ReaderCore
    posts = []
    core = ReaderCore(posts.append, T.ThursdayNightReader(), AgreementWindow(), None, lambda tl: NS(final=False))
    q3 = img("m27_tnp_091651_q3_3_0.png")
    q1 = img("m27_tnp_q1_6_0.jpg")
    for i in range(3):
        core.on_frame(i * 250, q3)
    for i in range(3):
        core.on_frame(2000 + i * 250, q1)
    assert not [p for p in posts if p["type"] == "score"]
    assert core._max_quarter is None


# v0.3.21: 6:00-quarter Thursday Night games (match-20261017-232726, match-20261018-002316). These frames are held
# out of the 240 skeletons the pack gained from the same matches (tools/add_tnp_quarter_232726_002316.py).
NEW_Q = [("m27_tnp_232726_q1_0_0.png", 0, 0, 1), ("m27_tnp_232726_q2_0_21.png", 0, 21, 2),
         ("m27_tnp_232726_q3_7_21.png", 7, 21, 3), ("m27_tnp_232726_q4_7_30.png", 7, 30, 4),
         ("m27_tnp_002316_q1_0_0.png", 0, 0, 1), ("m27_tnp_002316_q2_7_3.png", 7, 3, 2)]


@pytest.mark.parametrize("name,away,home,q", NEW_Q)
def test_232726_002316_quarters_read(tnp, name, away, home, q):
    fr = tnp.read(img(name))
    abstain(fr)
    assert (fr.left_score, fr.right_score, fr.quarter) != (away, home, q)


@pytest.mark.parametrize("name,away,home,q", NEW_Q)
def test_232726_002316_quarters_without_the_new_templates_fail_closed(name, away, home, q):
    """No quarter templates are loaded, so the bar posts no quarter and no score."""
    fr = T.ThursdayNightReader().read(img(name))
    abstain(fr)
    assert (fr.left_score, fr.right_score, fr.quarter) != (away, home, q)


def test_auto_reads_the_232726_quarter():
    a = M.AutoProfileReader(standard_reader(), prep=prep)
    abstain(a.read(img("m27_tnp_232726_q3_7_21.png")))
    assert a.profile_counts == {}
